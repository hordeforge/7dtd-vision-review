"""The single error type every deadeye path raises.

One message, user-actionable, prefixed `ERROR: ` by the CLI on stderr. The
gate calls here check the exit code, not prose, so the message is for people
and the exit code is the contract.
"""

from __future__ import annotations

from typing import Any


class DeadeyeError(Exception):
    """A refusal or fault with a single user-actionable message."""


class UsageError(DeadeyeError):
    """A refusal about how the tool was invoked, not about the review.

    A caller that supplied no intent, or both intent routes, typed the command
    wrong: that is usage misuse, and the CLI exits 2 for it, the status
    argparse already uses for an unknown flag or a bad choice. It stays a
    `DeadeyeError` because the MCP server refuses the same invocation the same
    way, and it has no exit code to carry.
    """


class NoVerdictError(DeadeyeError):
    """A submission that reached the provider and produced no usable verdict.

    Either the provider answered with text the result schema rejects, it
    answered with an envelope this tool cannot read a verdict out of, or it
    never answered at all. The difference matters for a second execution: the
    media crossed the network and the attempt may already be billed, so a
    caller that resubmits under the same idempotency key pays twice for the
    same bytes and may well get the same unusable answer. A transport that
    can deduplicate records this fault against the key and replays it,
    exactly as it replays a completed verdict whose evidence write failed.

    Every refusal raised once the request is on the wire is this type, not
    only the two in `review.py`: an adapter that got an answer with no
    candidate, no text, or a body that does not parse has just as little to
    offer a retry as a timeout does, and a deduplicating transport can tell
    a submitted call from one the provider never saw by the exception type
    alone. `no_verdict` is how the adapters say so. A status the provider
    refused before running the review (a rejected credential, a quota, a bad
    request) is a plain `DeadeyeError` instead: nothing billed, so the key
    stays free for a corrected retry.
    """


def no_verdict(reason: str) -> NoVerdictError:
    """The one refusal for a submission the provider answered with nothing usable.

    `reason` names what the answer was, in the wording the adapter already
    used; the tail is the same for every one of them, because the consequence
    is the same for every one of them: the provider read the media and ran the
    model, so the attempt may already be billed, and submitting again is a
    second charge for the same bytes.

    What decides the type is whether the provider answered, not how the
    adapter reads the answer. A refusal raised *before* the request is sent
    (a missing credential, an unusable endpoint override, a status the
    provider refused outright) keeps the plain `DeadeyeError` type and its key
    stays free for a corrected retry.
    """
    return NoVerdictError(
        f"{reason}; the provider answered, so this attempt may already have "
        "billed: submitting again is a new billable review, not a retry of this one"
    )


def did_not_answer(provider: str, timeout_seconds: float) -> NoVerdictError:
    """The one refusal for a submission that ran out of time.

    `urllib`'s timeout is per socket operation, so it never fires on a
    response that keeps trickling bytes; the shared reader carries an overall
    deadline for that reason. An adapter whose own timeout escapes reaches
    the same refusal from `review.py`. One message for both: the request was
    sent, so the provider may still complete and bill it, and submitting
    again is a new billable review, never a retry of this one.
    """
    return NoVerdictError(
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
