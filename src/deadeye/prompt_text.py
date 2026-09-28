"""Untrusted text made safe to interpolate into prompt text.

Filenames, authored intent fields, and provider error prose all reach the
reviewer instruction as lines, and each is authored-local text that a reviewer
model reads as the pipeline's own words. A name carrying a newline or any
other control character can forge a label-shaped or instruction-shaped line
there, and a name carrying the author-statement fence marker can close the
data-only block the instruction declares. This module holds the one
flattening rule every such value passes through, so a field added to a prompt
later cannot forget it, and the one check every value rendered into the
reviewer instruction passes through, so a filename discovered from a clip
directory is held to the same rule as a field read from an intent file. It is
a leaf: nothing here imports from the rest of the package except the error
type, and every module that renders untrusted text into a prompt depends on
this and nothing else.
"""

from __future__ import annotations

import unicodedata

from .errors import DeadeyeError

__all__ = [
    "FENCE_MARKERS",
    "carries_fence_marker",
    "fence_marker_error",
    "flat_label_text",
    "flat_prompt_text",
]

# The reviewer prompt fences every authored block between the BEGIN/END AUTHOR
# STATEMENT markers and declares that block data-only (`prompt.py`). A value
# carrying a marker line of its own could close that fence early and move
# everything after it outside the data-only declaration, so the markers are
# refused wherever untrusted text is rendered into the instruction.
FENCE_MARKERS = ("-----BEGIN AUTHOR STATEMENT", "-----END AUTHOR STATEMENT")

# Code points a model reads as the ASCII HYPHEN-MINUS the fence markers are
# built from. A marker spelled with any of them holds no ASCII substring and
# would pass a raw test while rendering as the real fence line to the model
# that has to decide where the data-only block ends. Some have a compatibility
# decomposition NFKC resolves (FULLWIDTH HYPHEN-MINUS, SMALL EM DASH) and
# some have none (HYPHEN, NON-BREAKING HYPHEN, MINUS SIGN), so the check folds
# in both directions rather than trusting normalization to have reached a
# particular one. Spelled as code points: this inventory is by definition the
# set of look-alikes the project's own confusable lint rule flags, so writing
# the characters literally would make the constant unreadable.
#
# Folding happens only inside `carries_fence_marker`; the value that reaches
# the prompt is the author's own text, unmodified.
DASH_LOOKALIKES = frozenset(
    {
        chr(0x2010),  # HYPHEN
        chr(0x2011),  # NON-BREAKING HYPHEN
        chr(0x2012),  # FIGURE DASH
        chr(0x2212),  # MINUS SIGN
        chr(0xFE58),  # SMALL EM DASH (NFKC decomposes this to EM DASH)
        chr(0xFE63),  # SMALL HYPHEN-MINUS
        chr(0x30FC),  # KATAKANA-HIRAGANA PROLONGED SOUND MARK
    }
)
"""Characters a reviewer model renders as a short dash, mapped to HYPHEN-MINUS."""

DASH_FOLD = str.maketrans(dict.fromkeys(DASH_LOOKALIKES, "-"))


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

    Flattening is a line-shape fix only. Text that reaches the reviewer
    instruction goes through `flat_prompt_text`, which adds the fence-marker
    check; this stays the bare rule for provider prose on a refusal line,
    where a marker is harmless because no data-only block is in play.
    """
    if value.isprintable():
        return value
    return "".join(char if char.isprintable() else " " for char in value)


def carries_fence_marker(value: str) -> bool:
    """Whether `value` carries an author-statement fence marker in any spelling.

    The raw substring test is not enough on its own. The marker is judged by a
    language model reading the rendered prompt, not by this module, and a model
    reads U+2010 HYPHEN, U+2011 NON-BREAKING HYPHEN, and U+FF0D FULLWIDTH
    HYPHEN-MINUS as the dashes the fence is built from. A value carrying
    `-----\uff0dBEGIN AUTHOR STATEMENT` therefore holds no ASCII marker and
    passes the raw test, yet reads at the model as the fence opening, and
    everything after it sits outside the data-only block the instruction
    declares.

    Compatibility decomposition closes the width class, and `DASH_LOOKALIKES`
    closes the ones normalization preserves: together they fold every spelling
    of the marker's dashes onto ASCII, so the match sees the characters a
    reader sees. Every marker is pure ASCII, so this leaves an honest ASCII
    string identical. Only the check widens: nothing that parses today is
    rejected, and no parsed value is rewritten.

    It does not fold cross-script letter lookalikes (a Cyrillic A for the Latin
    one), which would need a per-character confusable table. The marker is a
    control the author of a local intent file or a local filename would have
    to work to reach, and the structural defence is the two-role split in
    `prompt.py`, which keeps authored text out of the instruction's slot
    whatever the text says.
    """
    normalized = value.translate(DASH_FOLD)
    normalized = unicodedata.normalize("NFKC", normalized).translate(DASH_FOLD)
    return any(marker in normalized for marker in FENCE_MARKERS)


def fence_marker_error(origin: str) -> DeadeyeError:
    """The one refusal for a value carrying a fence marker, naming where it came from."""
    return DeadeyeError(
        f"{origin} contains an author-statement fence marker "
        f"({' or '.join(FENCE_MARKERS)}); reword it without that line so the "
        "reviewer prompt's data-only fence cannot be escaped"
    )


def flat_prompt_text(value: str) -> str:
    """`value` flattened and refused if it carries a fence marker.

    The rule every value rendered into the reviewer instruction passes
    through, whatever route brought it there: a reference filename, a clip's
    own filename discovered from a directory, or a sampling note that quotes
    one. The intent parser checks its prose fields for the marker by key, but a
    clip's filenames are never parsed and are rendered wherever the media
    summary and the attachment labels name them, so the check has to live at
    the rendering rule rather than at one of its callers.

    Refused, not rewritten: a name carrying the marker is not a name the
    prompt can carry honestly, and silently editing the author's filename
    into the submitted text would leave the rendered prompt naming a file that
    does not exist.
    """
    flattened = flat_label_text(value)
    if carries_fence_marker(flattened):
        raise fence_marker_error(f"the filename {value!r}")
    return flattened
