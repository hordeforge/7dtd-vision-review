"""The single error type every deadeye path raises.

One message, user-actionable, prefixed `ERROR: ` by the CLI on stderr. The
gate calls here check the exit code, not prose, so the message is for people
and the exit code is the contract.
"""

from __future__ import annotations

from typing import Any


class DeadeyeError(Exception):
    """A refusal or fault with a single user-actionable message."""


def did_not_answer(provider: str, timeout_seconds: float) -> DeadeyeError:
    """The one refusal for a submission that ran out of time.

    `urllib`'s timeout is per socket operation, so it never fires on a
    response that keeps trickling bytes; the shared reader carries an overall
    deadline for that reason. An adapter whose own timeout escapes reaches
    the same refusal from `review.py`. One message for both: the request was
    sent, so the provider may still complete and bill it, and submitting
    again is a new billable review, never a retry of this one.
    """
    return DeadeyeError(
        f"provider {provider!r} did not answer within {timeout_seconds:g}s; "
        "no verdict arrived, and the submission may still have completed "
        "and billed server-side: submitting again is a new billable "
        "review, not a retry of this one"
    )


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
