"""The single error type every deadeye path raises.

One message, user-actionable, prefixed `ERROR: ` by the CLI on stderr. The
gate calls here check the exit code, not prose, so the message is for people
and the exit code is the contract.
"""

from __future__ import annotations

from typing import Any


class DeadeyeError(Exception):
    """A refusal or fault with a single user-actionable message."""


class EvidenceWriteError(DeadeyeError):
    """An evidence envelope could not be persisted after a completed review.

    The submission had already succeeded and been billed, so losing the
    envelope here would force a caller to resubmit the media to recover the
    verdict: a second billable review of the same bytes. `document` carries
    the full envelope (validated result included) so every transport can hand
    it to the caller alongside the refusal; the run still reports failure.
    """

    def __init__(self, message: str, *, document: dict[str, Any]) -> None:
        super().__init__(message)
        self.document = document
