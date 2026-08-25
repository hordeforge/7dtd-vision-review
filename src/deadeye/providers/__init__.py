"""Provider adapters for deadeye: this package's public import surface.

An adapter lands one per vendor under the same narrow protocol (`base`);
`fake` ships with the core. Wiring and discovery code takes adapters and the
boundary types from here; `_http` and every other helper stay private.
"""

from .base import (
    MediaPayload,
    ProviderLimits,
    ReviewRequest,
    ReviewResponse,
    VideoReviewProvider,
)
from .fake import FakeProvider
from .gemini import GeminiProvider
from .nvidia import NvidiaProvider

__all__ = [
    "FakeProvider",
    "GeminiProvider",
    "MediaPayload",
    "NvidiaProvider",
    "ProviderLimits",
    "ReviewRequest",
    "ReviewResponse",
    "VideoReviewProvider",
]
