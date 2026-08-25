"""Provider adapters for deadeye. `fake` ships with the core; real adapters
land one per vendor under the same narrow protocol."""

from .base import (
    MediaPayload,
    ProviderLimits,
    ReviewRequest,
    ReviewResponse,
    VideoReviewProvider,
)
from .fake import FakeProvider

__all__ = [
    "FakeProvider",
    "MediaPayload",
    "ProviderLimits",
    "ReviewRequest",
    "ReviewResponse",
    "VideoReviewProvider",
]
