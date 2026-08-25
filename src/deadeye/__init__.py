"""deadeye — the shared vision-model review gateway for hordeforge.

Forwards a clip (a muxed video or a frame sequence) plus the author's recorded
intent to a configured vision-capable model, samples the media down when the
provider's frame/size budget demands it, and normalizes whatever comes back
into one stable, pipeline-owned result shape. Consumed programmatically by
`7dtd-asset-pipeline` (`shamway review-video`) and `7dtd-playtest`
(`review_video.py`), and usable standalone through the `deadeye` CLI.
"""

from ._version import __version__

__all__ = ["__version__"]
