"""Untrusted text made safe to interpolate into prompt text.

Filenames, authored intent fields, and provider error prose all reach the
reviewer instruction as lines, and each is authored-local text that a reviewer
model reads as the pipeline's own words. A name carrying a newline or any
other control character can forge a label-shaped or instruction-shaped line
there. This module holds the one flattening rule every such value passes
through, so a field added to a prompt later cannot forget it. It is a leaf:
nothing here imports, and every module that renders untrusted text into a
prompt depends on this and nothing else.
"""

from __future__ import annotations

__all__ = ["flat_label_text"]


def flat_label_text(value: str) -> str:
    """A filename made safe to interpolate into reviewer-prompt text.

    Every non-printable character becomes a space, which covers the whole
    separator and format categories (`\\n`, U+2028, U+2029, NEL, bidi controls,
    zero-width joiners) that a newline check alone misses. Evidence keeps the
    true path and the raw author bytes; only prompt-facing renderings are
    flattened, so the change is visible in the envelope rather than hidden
    from it.

    `isprintable()` settles the common name on its own: a string with nothing
    to flatten is returned unchanged, so the per-character Python walk runs
    only for the hostile names this exists to catch.
    """
    if value.isprintable():
        return value
    return "".join(char if char.isprintable() else " " for char in value)
