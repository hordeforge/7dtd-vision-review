"""The single error type every deadeye path raises.

One message, user-actionable, prefixed `ERROR: ` by the CLI on stderr. The
gate calls here check the exit code, not prose, so the message is for people
and the exit code is the contract.
"""


class DeadeyeError(Exception):
    """A refusal or fault with a single user-actionable message."""
