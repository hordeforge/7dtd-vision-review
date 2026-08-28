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

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config, sampling
from .errors import DeadeyeError, EvidenceWriteError
from .evidence import build_envelope, ensure_writable, sha256_file, write_evidence
from .intent import ReviewIntent, load_intent, redact_json_text
from .prompt import build_prompt
from .providers import MediaPayload, ProviderLimits, ReviewRequest
from .result import parse_model_json, validate_result
from .sampling import base64_wire_bytes, mime_for_suffix

if TYPE_CHECKING:
    from collections.abc import Callable

    from .providers import VideoReviewProvider

DEFAULT_TIMEOUT_SECONDS = 120.0


def run_review(
    clip: Path,
    *,
    provider: VideoReviewProvider,
    intent_path: Path | None = None,
    intent_text: str | None = None,
    model: str | None = None,
    allow_network: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    keep_raw_response: bool = False,
    output: Path | None = None,
    force: bool = False,
    notify: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Submit the actual clip media plus recorded intent, return the envelope.

    Order matters and is tested: consent gate, evidence-path guard, local
    intent validation, clip discovery, provider configuration, local
    format/size limits, sampling, disclosure, submission, structural
    validation, evidence. A failure at any step raises one user-actionable
    message and preserves no partial verdict as a completed review; a fault
    at the last step (after a billed submission) carries the full envelope
    on `EvidenceWriteError` so the verdict survives it.
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
    if model is not None:
        resolved_model = model
    else:
        resolved_model = config.text(("default_model",)) or provider.default_model
    if not provider.is_configured():
        raise DeadeyeError(
            f"provider {provider.name!r} is not configured: {provider.configuration_hint()}"
        )

    submission = _prepare_submission(media, intent, provider.limits, provider_name=provider.name)

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

    media_summary = _media_summary(submission.record, submission.total_bytes)
    frame_note = _frame_timing_note(submission.record)
    prompt = build_prompt(intent, media_summary=media_summary, frame_timing_note=frame_note)

    payload_list: list[MediaPayload] = []
    for (path, kind), data in zip(submission.files, submission.file_bytes, strict=True):
        p = Path(path)
        payload_list.append(
            MediaPayload(
                name=p.name,
                mime_type=mime_for_suffix(p.suffix),
                kind=kind,
                data=data,
            )
        )
    payloads = tuple(payload_list)
    request = ReviewRequest(
        prompt=prompt,
        media=payloads,
        model=resolved_model,
        timeout_seconds=timeout_seconds,
    )
    # Wall-clock latency of the provider call, recorded in the envelope beside
    # usage: token counts alone say nothing about how long the model thought.
    submitted_at = time.perf_counter()
    try:
        response = provider.review(request)
    except TimeoutError as exc:
        # An adapter let its own timeout escape. The request was sent, so the
        # provider may still complete and bill it: resubmitting is a second
        # billable review, never a retry of this one.
        raise DeadeyeError(
            f"provider {provider.name!r} did not answer within {timeout_seconds:g}s; "
            "no verdict arrived, and the submission may still have completed "
            "and billed server-side: submitting again is a new billable "
            "review, not a retry of this one"
        ) from exc
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
            prompt=prompt,
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
    except DeadeyeError:
        if keep_raw_response and output is not None:
            document = envelope_for(
                result=None,
                error="the model response failed structural validation; see raw_provider_response",
                raw_response=redact_json_text(response.raw_text),
                params={},
            )
            try:
                write_evidence(output, document, force=force)
            except DeadeyeError as exc:
                raise _evidence_write_fault(exc, document) from exc
            raise DeadeyeError(
                "the model response failed structural validation; a redacted raw "
                f"response was preserved at {output} because keep-raw was requested"
            ) from None
        raise

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

    evidence: dict[str, str | None] = {"path": None, "sha256": None}
    if output is not None:
        try:
            evidence_path, evidence_sha256 = write_evidence(output, document, force=force)
        except DeadeyeError as exc:
            # The submission completed and was billed; losing the envelope to
            # a local write fault would make recovery a second billable
            # review of the same bytes. The refusal carries the full document
            # so every transport can still deliver the verdict.
            raise _evidence_write_fault(exc, document) from exc
        evidence = {"path": str(evidence_path), "sha256": evidence_sha256}
    document["evidence"] = evidence
    return document


def _evidence_write_fault(exc: DeadeyeError, document: dict[str, Any]) -> EvidenceWriteError:
    """Wrap a failed evidence write so the billed verdict survives the refusal."""
    return EvidenceWriteError(
        f"{exc} the provider returned a complete verdict for this billed "
        "submission; the full envelope rides this failure (stdout on the CLI, "
        "the tool result over MCP) so recovering it needs no second submission",
        document=document,
    )


@dataclass(frozen=True)
class _Submission:
    """What a submission would send: the sampling decision and every entry."""

    record: sampling.SamplingRecord
    files: tuple[tuple[str, sampling.MediaKind], ...]
    """(path, kind) per file sent, clip media first, then references."""
    entries: tuple[dict[str, Any], ...]
    """The envelope's `media` entries, hashed once here."""
    total_bytes: int
    file_bytes: tuple[bytes, ...]
    """Cached file contents, one per entry, read during hashing."""


def _prepare_submission(
    media: sampling.ClipMedia,
    intent: ReviewIntent,
    limits: ProviderLimits,
    *,
    provider_name: str,
) -> _Submission:
    """The local-only phase before anything is contacted.

    Reference checks, sampling to the provider's declared limits, hashing, and
    the total-size budget all happen here, so every refusal is cheap and no
    byte is hashed twice.
    """
    for reference in intent.references:
        if not reference.path.is_file():
            raise DeadeyeError(f"no such reference file: {reference.path}")
        if reference.path.suffix.lower() not in limits.suffixes:
            raise DeadeyeError(
                f"reference {reference.path} ({reference.path.suffix or 'no suffix'}) is not "
                f"a format provider {provider_name!r} accepts ({', '.join(limits.suffixes)})"
            )

    record = sampling.sample(
        media,
        max_frames=limits.max_frames,
        video_capable=limits.accepts_video,
        max_video_bytes=limits.max_video_bytes,
    )
    files: list[tuple[str, sampling.MediaKind]] = [
        *record.submitted_files,
        *((str(reference.path), "reference") for reference in intent.references),
    ]
    # Per entry, not per unique path: the same file listed twice (a repeated
    # reference, a reference inside the clip) is uploaded twice, and the
    # disclosure must count every byte that leaves the machine.
    hashed = [sha256_file(Path(path)) for path, _ in files]
    total_bytes = sum(size for _, size, _ in hashed)
    cached_bytes = tuple(data for _, _, data in hashed)
    # Adapters submit inline base64 (3 raw bytes become 4 on the wire), so
    # the per-request budget is compared against the encoded total: a raw
    # byte count would pass a submission the provider refuses after the
    # upload. The disclosure still reports raw bytes, the files' true sizes.
    wire_bytes = sum(base64_wire_bytes(size) for _, size, _ in hashed)
    if limits.max_bytes is not None and wire_bytes > limits.max_bytes:
        raise DeadeyeError(
            f"submission is {total_bytes} bytes ({wire_bytes} as submitted base64); "
            f"provider {provider_name!r} accepts at "
            f"most {limits.max_bytes} per request. Sample fewer frames, shorten the "
            "clip, or drop reference media"
        )
    entries = [
        {
            "path": path,
            "sha256": digest,
            "bytes": size,
            "mime_type": mime_for_suffix(Path(path).suffix),
            "kind": kind,
        }
        for (path, kind), (digest, size, _) in zip(files, hashed, strict=True)
    ]
    return _Submission(
        record=record,
        files=tuple(files),
        entries=tuple(entries),
        total_bytes=total_bytes,
        file_bytes=cached_bytes,
    )


def _media_summary(
    record: sampling.SamplingRecord,
    total_bytes: int,
) -> str:
    if record.submitted_files and record.submitted_files[0][1] == "video":
        return f"a single muxed video file ({total_bytes} bytes). " + record.note
    if record.frames_submitted == 0:
        return "nothing (the provider could not ingest any of the media)"
    return (
        f"{record.frames_submitted} frame image(s) of the clip's "
        f"{record.frames_available} frames ({total_bytes} bytes). " + record.note
    )


def _frame_timing_note(record: sampling.SamplingRecord) -> str:
    if not record.submitted_files or record.submitted_files[0][1] != "frame":
        return ""
    return (
        "Frames arrive in the order listed; an issue's at_frame index refers to "
        "that order (0 = the first submitted frame), while at_seconds refers to "
        "seconds from the clip's start."
    )
