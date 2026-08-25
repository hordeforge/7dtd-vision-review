"""The offline stand-in adapter.

It answers from the request metadata alone and sees nothing, which is the
point: the tests assert on what it *received* — the exact media bytes, by
hash, the frame count, and the complete prompt — so the boundary's contract is
pinned without any network. It is also the dry-run lane for a caller who wants
to prove an intent file and evidence plumbing end to end before paying for a
real submission.
"""

from __future__ import annotations

import hashlib
import json

from ..errors import DeadeyeError
from .base import ProviderLimits, ReviewRequest, ReviewResponse


class FakeProvider:
    name = "fake"
    endpoint_mode = "in-process-fake"
    requires_credential = False
    _limits = ProviderLimits(
        suffixes=(".png", ".jpg", ".jpeg", ".webp", ".mp4"),
        max_bytes=20 * 1024 * 1024,
        max_frames=8,
        accepts_video=True,
        max_video_bytes=8 * 1024 * 1024,
    )

    def __init__(self) -> None:
        self.requests: list[ReviewRequest] = []

    @property
    def default_model(self) -> str:
        return "deadeye-fake-vision-v1"

    @property
    def limits(self) -> ProviderLimits:
        return self._limits

    def is_configured(self) -> bool:
        return True

    def configuration_hint(self) -> str:
        return "the fake provider needs no credentials; it exists for offline plumbing checks"

    def review(self, request: ReviewRequest) -> ReviewResponse:
        self.requests.append(request)
        digests = {
            payload.name: hashlib.sha256(payload.data).hexdigest() for payload in request.media
        }
        candidate = request.media[0]
        payload = {
            "summary": (
                f"Received {len(request.media)} file(s) named "
                f"{', '.join(payload.name for payload in request.media)}; "
                f"candidate {candidate.name!r} is {len(candidate.data)} bytes "
                f"(sha256 {digests[candidate.name][:16]}). The fake provider sees "
                "nothing and critiques from the request envelope only."
            ),
            "strengths": ["the submission crossed the provider boundary intact"],
            "issues": [
                {
                    "description": (
                        "every submitted byte is suspect by construction: this "
                        "verdict came from the fake provider, not from seeing"
                    ),
                    "at_frame": [0, 1],
                }
            ],
            "recommended_changes": [
                "rerun against a configured real provider for an actual review"
            ],
            "rubric_scores": {"semantic_fit": None, "motion_plausibility": None},
            "confidence": 0.42,
            "limitations": [
                "the fake adapter received media and prompt but cannot see",
                "prompt digest prefix "
                + hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()[:16],
            ],
        }
        # usage stays None on purpose: unavailable must be reported as
        # unavailable, never estimated.
        return ReviewResponse(
            raw_text=json.dumps(payload), usage=None, model_reported=self.default_model
        )


def describe_received(request: ReviewRequest) -> dict[str, object]:
    """The envelope-only description the fake adapter's tests assert against."""
    if not request.media:
        raise DeadeyeError("fake provider received no media")
    return {
        "files": [
            {
                "name": payload.name,
                "mime_type": payload.mime_type,
                "kind": payload.kind,
                "sha256": hashlib.sha256(payload.data).hexdigest(),
            }
            for payload in request.media
        ],
        "prompt": request.prompt,
        "model": request.model,
    }
