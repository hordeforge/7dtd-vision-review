"""One review, end to end: consent, intent, limits, sampling, submission,
validation, evidence.

Three boundaries are load-bearing here, mirroring the sibling audio-review
pipeline:

- **Consent comes before everything.** The submission is networked, billable,
  and sends authored media to a third party. Every refusal below happens
  before the credential check except the consent gate itself, which happens
  first of all.
- **The result schema is ours, not the vendor's.** Provider payloads stay at
  the adapter boundary; callers see `validate_result`'s output. A raw response
  is preserved only when explicitly requested, redacted either way.
- **A verdict here is evidence, never acceptance.** Nothing in this module can
  mark an asset accepted; that remains a human look in the real context.

The judgement is traceable (hashes, versions, timestamps) but never
deterministic: two runs may disagree, and disagreement is preserved rather
than averaged.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config, sampling
from .errors import DeadeyeError, EvidenceWriteError, NoVerdictError, did_not_answer
from .evidence import build_envelope, ensure_writable, sha256_file, write_evidence
from .intent import ReviewIntent, load_intent
from .prompt import FRAME_TIMING_NOTE, PromptParts, build_prompt_parts
from .prompt_text import flat_prompt_text
from .providers import MediaPayload, ProviderLimits, ReviewRequest
from .redaction import redact_json_text
from .result import parse_model_json, validate_result
from .sampling import IMAGE_SUFFIXES, base64_wire_bytes, mime_for_suffix
from .surface import resolve_model

if TYPE_CHECKING:
    from collections.abc import Callable

    from .providers import VideoReviewProvider


def run_review(
    clip: Path,
    *,
    provider: VideoReviewProvider,
    intent_path: Path | None = None,
    intent_text: str | None = None,
    model: str | None = None,
    allow_network: bool = False,
    timeout_seconds: float = config.DEFAULT_TIMEOUT_SECONDS,
    keep_raw_response: bool = False,
    output: Path | None = None,
    force: bool = False,
    notify: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Submit the actual clip media plus recorded intent, return the envelope.

    Order matters and is tested: consent gate, evidence-path guard, the
    configuration read that must fail before a missing credential is blamed on
    the operator, local intent validation, clip discovery, provider
    configuration and model resolution, local format/size limits, sampling,
    disclosure, submission, structural validation, evidence. A failure at any
    step raises one user-actionable message and preserves no partial verdict as
    a completed review; a fault at the last step (after a billed submission)
    carries the full envelope on `EvidenceWriteError` so the verdict survives
    it.
    """
    if not allow_network:
        # First of all, before credentials are read or anything is contacted.
        raise DeadeyeError(
            "deadeye review sends the authored media to a third-party service; "
            "pass --allow-network to consent to that upload"
        )
    if output is not None:
        # Second of all, still before anything is contacted: a rerun into an
        # occupied evidence path is refused here, so obeying the guard never
        # costs a billable submission. `write_evidence` re-checks at write
        # time; this early check is what makes the plain rerun free.
        ensure_writable(output, force=force)
    # The submission path reads provider configuration, so a config that
    # cannot parse must fail here with its real cause. Reading it through the
    # fail-soft `config.value` instead would degrade silently: an unparseable
    # file would read as "no credential" and send the operator chasing an API
    # key while the actual fault is one bad line of TOML.
    config.load()
    intent, intent_raw = load_intent(intent_path, intent_text)

    media = sampling.discover(clip)
    resolved_model = resolve_model(provider, model)
    if not provider.is_configured():
        raise DeadeyeError(
            f"provider {provider.name!r} is not configured: {provider.configuration_hint()}"
        )

    submission, parts = _decide_submission(media, intent, provider.limits, provider.name)

    if notify is not None:
        notify(f"provider: {provider.name} ({provider.endpoint_mode})")
        notify(f"model: {resolved_model}")
        notify(
            f"submitting {len(submission.files)} file(s), {submission.total_bytes} bytes: "
            + ", ".join(path for path, _ in submission.files)
        )
        notify(
            f"warning: the media leaves this machine for {provider.name}; retention is "
            "governed by that provider's terms, so send only assets you may disclose"
        )

    # The budget names the whole request, and both prompt halves ride the same
    # request as the media: count their encoded bytes too. A media-only total
    # waves through a submission the provider refuses with 400 after the full
    # upload has already crossed the network.
    _enforce_wire_budget(
        submission.total_bytes,
        _request_wire_bytes(submission, parts),
        provider.limits.max_bytes,
        provider.name,
        detail="as submitted base64, prompt included",
    )

    payloads = tuple(
        _payload(path, kind, data)
        for (path, kind), data in zip(submission.files, submission.file_bytes, strict=True)
    )
    # Read once here, before anything is sent: the adapter reads the same
    # knobs again while it builds the body, and this is the copy the envelope
    # records. A configuration that cannot produce them is refused here,
    # before the upload, rather than after it.
    generation = provider.generation_settings()
    request = ReviewRequest(
        prompt=parts.user,
        system_prompt=parts.system,
        media=payloads,
        model=resolved_model,
        timeout_seconds=timeout_seconds,
    )
    # Monotonic latency of the provider call, recorded in the envelope beside
    # usage: token counts alone say nothing about how long the model thought.
    # `perf_counter` is the elapsed-time clock; `time.time()` would include
    # NTP steps and a manual clock change as if they were model latency.
    submitted_at = time.perf_counter()
    try:
        response = provider.review(request)
    except TimeoutError as exc:
        # An adapter let its own timeout escape; the shared HTTP reader maps
        # its own to the same refusal, so a caller reads one message.
        raise did_not_answer(provider.name, timeout_seconds) from exc
    elapsed_seconds = time.perf_counter() - submitted_at

    def envelope_for(
        *,
        result: dict[str, Any] | None,
        error: str | None,
        raw_response: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """The envelope for this submission; the one home for shared fields."""
        return build_envelope(
            media_entries=submission.entries,
            sampling=submission.record,
            intent=intent,
            intent_raw=intent_raw,
            provider_name=provider.name,
            endpoint_mode=provider.endpoint_mode,
            model_requested=resolved_model,
            model_reported=response.model_reported,
            generation=generation,
            prompt=request.rendered,
            usage=response.usage,
            total_bytes=submission.total_bytes,
            elapsed_seconds=elapsed_seconds,
            result=result,
            error=error,
            raw_response=raw_response,
            params=params,
        )

    try:
        parsed = parse_model_json(response.raw_text)
        result = validate_result(parsed)
    except DeadeyeError as exc:
        # The provider answered, so this submission was billed, and the answer
        # cannot be used. `NoVerdictError` says so: a transport holding an
        # idempotency key records the call as spent and replays this refusal
        # rather than offering a retry that would bill the same bytes again.
        if keep_raw_response and output is not None:
            document = envelope_for(
                result=None,
                error="the model response failed structural validation; see raw_provider_response",
                raw_response=redact_json_text(response.raw_text),
                params={},
            )
            # The same key every other envelope carries, naming the file this
            # one is about to become.
            document["evidence"] = {"path": str(output), "sha256": None}
            try:
                write_evidence(output, document, force=force)
            except DeadeyeError as write_exc:
                raise _evidence_write_fault(write_exc, document) from write_exc
            raise NoVerdictError(
                "the model response failed structural validation; a redacted raw "
                f"response was preserved at {output} because keep-raw was requested"
            ) from exc
        raise NoVerdictError(f"the model response failed structural validation: {exc}") from exc

    params = {
        "clip": str(clip),
        "intent": str(intent_path) if intent_path is not None else "(inline text)",
        "provider": provider.name,
        "model": resolved_model,
        "timeout_seconds": timeout_seconds,
        "keep_raw_response": keep_raw_response,
        "force": force,
        "allow_network": True,
    }
    document = envelope_for(
        result=result,
        error=None,
        raw_response=redact_json_text(response.raw_text) if keep_raw_response else None,
        params=params,
    )

    # Every envelope carries the `evidence` key, on every path that produces
    # one. A key that a successful run carries and the recovery path does not
    # is a KeyError for exactly the caller that can least afford it: the one
    # rebuilding a billed verdict out of the envelope a write fault left
    # undelivered. The persisted document names its own path but not its own
    # digest, which cannot contain its own hash; the digest rides the envelope
    # returned here.
    document["evidence"] = {"path": str(output) if output is not None else None, "sha256": None}
    if output is not None:
        try:
            evidence_path, evidence_sha256 = write_evidence(output, document, force=force)
        except DeadeyeError as exc:
            # The submission completed and was billed; losing the envelope to
            # a local write fault would make recovery a second billable
            # review of the same bytes. The refusal carries the full document
            # so every transport can still deliver the verdict.
            raise _evidence_write_fault(exc, document) from exc
        document["evidence"] = {"path": str(evidence_path), "sha256": evidence_sha256}
    return document


def _decide_submission(
    media: sampling.ClipMedia,
    intent: ReviewIntent,
    limits: ProviderLimits,
    provider_name: str,
) -> tuple[_Submission, PromptParts]:
    """The media this review will send, and the prompt it will send with it.

    The sampling decision and the whole-request budget are made here, before
    the disclosure and before any byte is read, so what the operator is told
    about leaving the machine is what the provider is actually sent, and the
    media decision costs one read of each file rather than a read per rejected
    plan. The decision is sized from file metadata, so `_materialize` checks
    the budget again against the bytes it actually reads: the files can change
    between the preflight and that read.
    """
    reference_sizes = _reference_sizes(intent, limits, provider_name)
    plan = _plan(
        media,
        intent,
        limits,
        reference_sizes,
        provider_name=provider_name,
        video_capable=limits.accepts_video,
    )
    if plan.record.primary_kind == "video" and media.frames:
        planned_parts = _prompt_parts(plan.record, plan.total_bytes, intent)
        if _over_budget(_request_wire_bytes(plan, planned_parts), limits):
            # `sampling.sample` already falls back to the frame sequence when
            # the video is over the provider's own video bound. This is the
            # same decision against the bound that actually decides, the whole
            # request with the prompt riding it: without it a video that misses
            # the request cap by the size of the prompt is refused outright,
            # with the frames that would have fitted sitting in the same
            # directory.
            frames = _plan(
                media,
                intent,
                limits,
                reference_sizes,
                provider_name=provider_name,
                video_capable=False,
                note_prefix=_video_over_request_budget(media, plan),
            )
            frame_parts = _prompt_parts(frames.record, frames.total_bytes, intent)
            if not _over_budget(_request_wire_bytes(frames, frame_parts), limits):
                plan = frames
    # One read per file, after the decision: the plan above is sized from
    # metadata, so what is finally hashed and submitted is the same set of
    # paths, read once.
    submission = _materialize(plan, limits, provider_name)
    return submission, _prompt_parts(submission.record, submission.total_bytes, intent)


def _payload(path: str, kind: sampling.MediaKind, data: bytes) -> MediaPayload:
    """One submission file as the adapter boundary describes it."""
    media_path = Path(path)
    return MediaPayload(
        name=media_path.name,
        mime_type=mime_for_suffix(media_path.suffix),
        kind=kind,
        data=data,
    )


def _prompt_parts(
    record: sampling.SamplingRecord, total_bytes: int, intent: ReviewIntent
) -> PromptParts:
    """The reviewer instruction for a planned submission, from what it sends."""
    return build_prompt_parts(
        intent,
        media_summary=_media_summary(record, total_bytes),
        frame_timing_note=_frame_timing_note(record),
    )


def _request_wire_bytes(sized: _Plan | _Submission, parts: PromptParts) -> int:
    """What the request carries on the wire: encoded media plus encoded prompt.

    Sized from a plan while the media is still being decided, and from the
    submission once its bytes are known: both answer the same question.
    """
    return sized.wire_bytes + _json_string_bytes(parts.system) + _json_string_bytes(parts.user)


def _over_budget(wire_bytes: int, limits: ProviderLimits) -> bool:
    """Whether a whole request is over what the provider accepts; no refusal."""
    return limits.max_bytes is not None and wire_bytes > limits.max_bytes


def _video_over_request_budget(media: sampling.ClipMedia, plan: _Plan) -> str:
    """Why the muxed video is dropped for the frame sequence, in evidence words."""
    video = media.video
    if video is None:  # unreachable: only called for a plan that took the video
        raise DeadeyeError(f"{media.source} holds no muxed video to replace")
    return (
        f"muxed video {flat_prompt_text(video.name)} is "
        f"{plan.sizes[0]} bytes, over the provider's whole-request "
        "budget once the prompt rides with it; sampled frames instead"
    )


def _evidence_write_fault(exc: DeadeyeError, document: dict[str, Any]) -> EvidenceWriteError:
    """Wrap a failed evidence write so the billed verdict survives the refusal."""
    return EvidenceWriteError(
        f"{exc} the provider returned a complete verdict for this billed "
        "submission; the full envelope rides this failure (stdout on the CLI, "
        "the tool result over MCP) so recovering it needs no second submission",
        document=document,
    )


@dataclass(frozen=True)
class _Plan:
    """A decided submission, sized from metadata and not yet read.

    Planning is metadata only, so the media decision (muxed video or sampled
    frames) costs no disk read and no retained bytes, and the files a rejected
    candidate named are never opened at all.
    """

    record: sampling.SamplingRecord
    files: tuple[tuple[str, sampling.MediaKind], ...]
    """(path, kind) per file sent, clip media first, then references."""
    sizes: tuple[int, ...]
    """The stat size of every file in `files`, parallel to it."""

    @property
    def total_bytes(self) -> int:
        return sum(self.sizes)

    @property
    def wire_bytes(self) -> int:
        """The same total as the media reach the wire as, once base64-encoded."""
        return sum(base64_wire_bytes(size) for size in self.sizes)


@dataclass(frozen=True)
class _Submission:
    """What a submission will send: the sampling decision and every entry."""

    record: sampling.SamplingRecord
    files: tuple[tuple[str, sampling.MediaKind], ...]
    """(path, kind) per file sent, clip media first, then references."""
    entries: tuple[dict[str, Any], ...]
    """The envelope's `media` entries, hashed once here."""
    total_bytes: int
    wire_bytes: int
    """The same total as the media reach the wire as, once base64-encoded."""
    file_bytes: tuple[bytes, ...]
    """Cached file contents, one per entry, read during hashing."""


def _reference_sizes(
    intent: ReviewIntent, limits: ProviderLimits, provider_name: str
) -> tuple[int, ...]:
    """Each reference's size, after refusing one the provider cannot ingest.

    Settled once per review rather than once per candidate plan. `_decide_submission`
    plans twice for the same intent when the muxed video misses the whole-request
    budget, and the reference checks depend on neither the video decision nor the
    sampling it drives: a missing file or an unacceptable format is refused the
    same way in both plans, and each one cost a stat per reference to learn it
    again.
    """
    for reference in intent.references:
        if not reference.path.is_file():
            raise DeadeyeError(f"no such reference file: {reference.path}")
        if reference.path.suffix.lower() not in limits.suffixes:
            raise DeadeyeError(
                f"reference {reference.path} ({reference.path.suffix or 'no suffix'}) is not "
                f"a format provider {provider_name!r} accepts ({', '.join(limits.suffixes)})"
            )
    return tuple(sampling.file_size(reference.path) for reference in intent.references)


def _plan(
    media: sampling.ClipMedia,
    intent: ReviewIntent,
    limits: ProviderLimits,
    reference_sizes: tuple[int, ...],
    *,
    provider_name: str,
    video_capable: bool,
    note_prefix: str | None = None,
) -> _Plan:
    """The local-only decision phase, before anything is contacted or read.

    Reference media rides the same request as the candidate, so its encoded
    size is already spent when the video budget decides what to submit.
    `reference_sizes` is what the caller read for this intent's references.

    `video_capable` is the caller's decision to consider the muxed video at
    all; `False` plans the frame sequence beside it. `note_prefix` records in
    the sampling note why this plan is the one that was chosen.
    """
    record = sampling.sample(
        media,
        max_frames=limits.max_frames,
        video_capable=video_capable,
        max_video_bytes=limits.max_video_bytes,
        reserved_wire_bytes=sum(base64_wire_bytes(size) for size in reference_sizes),
    )
    record = _hold_room_for_reference_images(record, media, intent, limits, provider_name)
    files: tuple[tuple[str, sampling.MediaKind], ...] = (
        *record.submitted_files,
        *((str(reference.path), "reference") for reference in intent.references),
    )
    # Reject an impossible request before reading any attachment. In
    # particular, references may total far more than a hosted provider's
    # request limit; retaining all of them just to refuse the request wastes
    # disk I/O and can create a large, avoidable memory spike.
    submitted_sizes = [sampling.file_size(Path(path)) for path, _ in record.submitted_files]
    _enforce_request_budget([*submitted_sizes, *reference_sizes], limits.max_bytes, provider_name)
    if note_prefix is not None:
        record = replace(record, note=f"{note_prefix}; {record.note}")
    return _Plan(record=record, files=files, sizes=(*submitted_sizes, *reference_sizes))


def _hold_room_for_reference_images(
    record: sampling.SamplingRecord,
    media: sampling.ClipMedia,
    intent: ReviewIntent,
    limits: ProviderLimits,
    provider_name: str,
) -> sampling.SamplingRecord:
    """Sample the frame sequence down to the images the references leave it.

    `max_frames` caps the images in one request, and a reference asset is an
    image in that same request. A clip sampled to the full cap and then given
    one comparison image sends one image over the cap, and the provider answers
    that with a refusal after the whole upload has already been billed: the one
    fault `ProviderLimits` exists to prevent (`base.py`). The cap is settled
    here, before the prompt, the disclosure, and the upload.

    Only a frame sequence is resized. A muxed video is not an image, and a
    request that carries one is not "submitting a frame sequence", so the
    references it rides beside it are not what the cap counts.
    """
    if limits.max_frames is None or record.primary_kind != "frame":
        return record
    images = sum(
        1 for reference in intent.references if reference.path.suffix.lower() in IMAGE_SUFFIXES
    )
    room = limits.max_frames - images
    if room >= record.frames_submitted:
        return record
    if room < 1:
        raise DeadeyeError(
            f"the intent names {images} reference image(s) and provider "
            f"{provider_name!r} accepts at most {limits.max_frames} images per "
            "request, which leaves no room for the clip itself. Drop a reference "
            "or review the clip without comparison media"
        )
    resampled = sampling.sample(
        media,
        max_frames=room,
        video_capable=False,
        max_video_bytes=limits.max_video_bytes,
    )
    return replace(
        resampled,
        note=(
            f"the {images} reference image(s) share the same "
            f"{limits.max_frames}-image request budget with the clip's frames; "
            f"{resampled.note}"
        ),
    )


def _materialize(plan: _Plan, limits: ProviderLimits, provider_name: str) -> _Submission:
    """Read and hash the decided files, once each.

    The budget is checked again against the bytes actually read: the files can
    change between the metadata preflight and this read, and a raw byte count
    would pass a submission the provider refuses after the upload. The
    disclosure reports those raw bytes, the files' true sizes.
    """
    # Per entry, not per unique path: the same file listed twice (a repeated
    # reference, a reference inside the clip) is uploaded twice, and the
    # disclosure must count every byte that leaves the machine.
    hashed = [sha256_file(Path(path)) for path, _ in plan.files]
    _enforce_request_budget([size for _, size, _ in hashed], limits.max_bytes, provider_name)
    entries = [
        {
            "path": path,
            "sha256": digest,
            "bytes": size,
            "mime_type": mime_for_suffix(Path(path).suffix),
            "kind": kind,
        }
        for (path, kind), (digest, size, _) in zip(plan.files, hashed, strict=True)
    ]
    return _Submission(
        record=plan.record,
        files=plan.files,
        entries=tuple(entries),
        total_bytes=sum(size for _, size, _ in hashed),
        wire_bytes=sum(base64_wire_bytes(size) for _, size, _ in hashed),
        file_bytes=tuple(data for _, _, data in hashed),
    )


def _enforce_request_budget(sizes: list[int], max_bytes: int | None, provider_name: str) -> None:
    """Refuse encoded media that cannot fit in one provider request."""
    _enforce_wire_budget(
        sum(sizes),
        sum(base64_wire_bytes(size) for size in sizes),
        max_bytes,
        provider_name,
    )


def _enforce_wire_budget(
    raw_bytes: int,
    wire_bytes: int,
    max_bytes: int | None,
    provider_name: str,
    *,
    detail: str = "as submitted base64",
) -> None:
    """Refuse a request whose encoded bytes exceed what the provider accepts.

    `detail` names what the encoded total covers, so a refusal never claims a
    prompt it did not count.
    """
    if max_bytes is not None and wire_bytes > max_bytes:
        raise DeadeyeError(
            f"submission is {raw_bytes} bytes ({wire_bytes} {detail}); "
            f"provider {provider_name!r} accepts at most {max_bytes} per request. "
            "Sample fewer frames, shorten the clip, or drop reference media"
        )


def _json_string_bytes(text: str) -> int:
    """The bytes `text` occupies inside a JSON request body.

    `json.dumps` escapes every non-ASCII character, so the request carries
    the escaped length, not `len(text.encode("utf-8"))`: an intent written in
    any non-Latin script would otherwise be undercounted by its own budget.
    """
    return len(json.dumps(text).encode("utf-8"))


def _media_summary(
    record: sampling.SamplingRecord,
    total_bytes: int,
) -> str:
    if record.primary_kind == "video":
        return f"a single muxed video file ({total_bytes} bytes). " + record.note
    if record.frames_submitted == 0:
        return "nothing (the provider could not ingest any of the media)"
    return (
        f"{record.frames_submitted} frame image(s) of the clip's "
        f"{record.frames_available} frames ({total_bytes} bytes). " + record.note
    )


def _frame_timing_note(record: sampling.SamplingRecord) -> str:
    if record.primary_kind != "frame":
        return ""
    return FRAME_TIMING_NOTE
