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

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import config, sampling
from .errors import DeadeyeError
from .evidence import build_envelope, sha256_file, write_evidence
from .intent import load_intent_file, parse_intent_text, redact
from .prompt import build_prompt
from .providers.base import MediaPayload, ReviewRequest
from .result import BASE_RUBRIC, parse_model_json, validate_result
from .sampling import mime_for_suffix

if TYPE_CHECKING:
    from collections.abc import Callable

    from .providers.base import VideoReviewProvider

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

    Order matters and is tested: consent gate, local intent validation, clip
    discovery, provider configuration, local format/size limits, sampling,
    disclosure, submission, structural validation, evidence. A failure at any
    step raises one user-actionable message and preserves no partial verdict as
    a completed review.
    """
    if not allow_network:
        # First of all, before credentials are read or anything is contacted.
        raise DeadeyeError(
            "deadeye review sends the authored media to a third-party service; "
            "pass --allow-network to consent to that upload"
        )
    # The submission path reads provider configuration, so a config that
    # cannot parse must fail here with its real cause. Reading it through the
    # fail-soft `config.value` instead would degrade silently: an unparseable
    # file would read as "no credential" and send the operator chasing an API
    # key while the actual fault is one bad line of TOML.
    config.load()
    if intent_path is not None and intent_text is not None:
        raise DeadeyeError(
            "deadeye review takes exactly one of --intent PATH or --intent-text JSON, never both"
        )
    if intent_path is not None:
        intent, intent_raw = load_intent_file(Path(intent_path))
    elif intent_text is not None:
        intent, intent_raw = parse_intent_text(intent_text)
    else:
        raise DeadeyeError(
            "deadeye review needs exactly one of --intent PATH (the reproducible route) "
            "or --intent-text JSON"
        )

    media = sampling.discover(clip)
    configured_default = config.value(("default_model",))
    resolved_model = (
        model
        or (
            configured_default
            if isinstance(configured_default, str) and configured_default
            else None
        )
        or provider.default_model
    )
    if not provider.is_configured():
        raise DeadeyeError(
            f"provider {provider.name!r} is not configured: {provider.configuration_hint()}"
        )

    limits = provider.limits
    for reference in intent.references:
        if not reference.path.is_file():
            raise DeadeyeError(f"no such reference file: {reference.path}")
        if reference.path.suffix.lower() not in limits.suffixes:
            raise DeadeyeError(
                f"reference {reference.path} ({reference.path.suffix or 'no suffix'}) is not "
                f"a format provider {provider.name!r} accepts ({', '.join(limits.suffixes)})"
            )

    candidate_record = sampling.sample(
        media,
        max_frames=limits.max_frames,
        video_capable=limits.accepts_video,
        max_video_bytes=limits.max_video_bytes,
    )

    submitted: list[tuple[str, str, Path]] = [
        (path, kind, Path(path)) for path, kind in candidate_record.submitted_files
    ] + [(str(reference.path), "reference", reference.path) for reference in intent.references]
    digests = {path: sha256_file(Path(path)) for path, _, _ in submitted}
    total_bytes = sum(size for _, size in digests.values())
    if limits.max_bytes is not None and total_bytes > limits.max_bytes:
        raise DeadeyeError(
            f"submission is {total_bytes} bytes; provider {provider.name!r} accepts at "
            f"most {limits.max_bytes} per request. Sample fewer frames, shorten the "
            "clip, or drop reference media"
        )

    if notify is not None:
        notify(f"provider: {provider.name} ({provider.endpoint_mode})")
        notify(f"model: {resolved_model}")
        notify(
            f"submitting {len(submitted)} file(s), {total_bytes} bytes: "
            + ", ".join(path for path, _, _ in submitted)
        )
        notify(
            f"warning: the media leaves this machine for {provider.name}; retention is "
            "governed by that provider's terms, so send only assets you may disclose"
        )

    media_summary = _media_summary(candidate_record, media, total_bytes)
    frame_note = _frame_timing_note(candidate_record)
    prompt = build_prompt(
        intent, BASE_RUBRIC, media_summary=media_summary, frame_timing_note=frame_note
    )

    payloads = tuple(
        MediaPayload(
            name=Path(path).name,
            mime_type=mime_for_suffix(Path(path).suffix),
            kind=kind,
            data=Path(path).read_bytes(),
        )
        for path, kind, _ in submitted
    )
    request = ReviewRequest(
        prompt=prompt,
        media=payloads,
        model=resolved_model,
        timeout_seconds=timeout_seconds,
    )
    try:
        response = provider.review(request)
    except TimeoutError as exc:
        raise DeadeyeError(
            f"provider {provider.name!r} did not answer within {timeout_seconds:g}s; "
            "no verdict was produced"
        ) from exc

    media_entries: list[dict[str, Any]] = []
    for path, kind, _ in submitted:
        digest, size = digests[path]
        media_entries.append(
            {
                "path": path,
                "sha256": digest,
                "bytes": size,
                "mime_type": mime_for_suffix(Path(path).suffix),
                "kind": kind,
            }
        )

    try:
        parsed = parse_model_json(response.raw_text)
        result = validate_result(parsed)
    except DeadeyeError:
        if keep_raw_response and output is not None:
            document = build_envelope(
                media_entries=tuple(media_entries),
                sampling=candidate_record,
                intent=intent,
                intent_raw=intent_raw,
                provider_name=provider.name,
                endpoint_mode=provider.endpoint_mode,
                model_requested=resolved_model,
                model_reported=response.model_reported,
                prompt=prompt,
                result=None,
                error="the model response failed structural validation; see raw_provider_response",
                raw_response=redact(response.raw_text),
                usage=response.usage,
                total_bytes=total_bytes,
                params={},
            )
            write_evidence(output, document, force=force)
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
    document = build_envelope(
        media_entries=tuple(media_entries),
        sampling=candidate_record,
        intent=intent,
        intent_raw=intent_raw,
        provider_name=provider.name,
        endpoint_mode=provider.endpoint_mode,
        model_requested=resolved_model,
        model_reported=response.model_reported,
        prompt=prompt,
        result=result,
        error=None,
        raw_response=redact(response.raw_text) if keep_raw_response else None,
        usage=response.usage,
        total_bytes=total_bytes,
        params=params,
    )

    evidence: dict[str, str | None] = {"path": None, "sha256": None}
    if output is not None:
        evidence_path, evidence_sha256 = write_evidence(output, document, force=force)
        evidence = {"path": str(evidence_path), "sha256": evidence_sha256}
    document["evidence"] = evidence
    return document


def _media_summary(
    record: sampling.SamplingRecord,
    media: sampling.ClipMedia,
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
