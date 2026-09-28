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

from ..sampling import IMAGE_SUFFIXES, VIDEO_SUFFIXES
from .base import ProviderLimits, ReviewRequest, ReviewResponse

# How many submissions one instance records in `requests`. The list is the
# assertion seam the offline tests read (`requests[-1]`), and it holds each
# request whole, media bytes included: a request may carry a full 20 MiB
# request budget, so a caller that reuses one instance for a long run pins
# every clip it ever reviewed. The seam reads the most recent, so a small
# window costs it nothing.
MAX_RECORDED_REQUESTS = 16


class FakeProvider:
    name = "fake"
    endpoint_mode = "in-process-fake"
    requires_credential = False
    credential_env_names: tuple[str, ...] = ()
    # Discovery's own format table, not a hand-kept list: the offline lane
    # must refuse exactly what a hosted adapter refuses, or a plumbing check
    # against a `.mov` or `.webm` reference passes here and fails on a real
    # submission. A hand-listed subset once did exactly that.
    _limits = ProviderLimits(
        suffixes=IMAGE_SUFFIXES + VIDEO_SUFFIXES,
        max_bytes=20 * 1024 * 1024,
        max_frames=8,
        accepts_video=True,
        max_video_bytes=8 * 1024 * 1024,
    )

    def __init__(self) -> None:
        self.requests: list[ReviewRequest] = []
        """The most recent submissions, oldest first, capped at
        `MAX_RECORDED_REQUESTS`: what the offline tests assert on, and what
        this adapter deliberately does not accumulate past that."""

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
        # Bounded here, not in a `finally`: the newest request is always
        # recorded, so the seam `requests[-1]` reads survives a refusal, and
        # dropping the oldest is what keeps a reused instance from growing
        # one clip's bytes per review.
        del self.requests[:-MAX_RECORDED_REQUESTS]
        candidate = request.media[0]
        payload = {
            "summary": (
                f"Received {len(request.media)} file(s) named "
                f"{', '.join(item.name for item in request.media)}; "
                f"candidate {candidate.name!r} is {len(candidate.data)} bytes "
                f"(sha256 {hashlib.sha256(candidate.data).hexdigest()[:16]}). "
                "The fake provider sees nothing and critiques from the request "
                "envelope only."
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
                + hashlib.sha256(request.rendered.encode("utf-8")).hexdigest()[:16],
            ],
        }
        # usage stays None on purpose: unavailable must be reported as
        # unavailable, never estimated.
        return ReviewResponse(
            raw_text=json.dumps(payload), usage=None, model_reported=self.default_model
        )
