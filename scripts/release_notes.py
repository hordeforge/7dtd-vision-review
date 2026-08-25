"""Extract one version's section from CHANGELOG.md for GitHub Release notes.

Keep a Changelog flow: rename `## Unreleased` to `## X.Y.Z` before tagging,
then this script hands that section to `gh release create --notes-file`.
Headings are matched on their first word as the bare version, with an
optional leading "v" and optional square brackets, so `## 0.1.0`,
`## [0.1.0]`, `## v0.1.0`, and the dated `## [0.1.0] - 2026-08-26` all hit.

Exit 0 and print the section when found; exit 3 when no section carries the
version (the caller falls back); any other failure exits nonzero loudly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

_NOT_FOUND = 3


def _normalise(heading: str) -> str:
    """The bare version in a section heading: `0.1.0` out of `[0.1.0] - date`."""
    words = heading.split()
    if not words:
        return ""
    return words[0].strip("[]").removeprefix("v").strip()


def extract(changelog: str, version: str) -> str | None:
    wanted = _normalise(version)
    lines = changelog.splitlines()
    current: str | None = None
    body: list[str] = []
    for line in lines:
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current == wanted:
                return "\n".join(body).strip()
            current = _normalise(match.group(1))
            body = []
        elif current is not None:
            body.append(line)
    return "\n".join(body).strip() if current == wanted else None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} VERSION", file=sys.stderr)
        return 2
    section = extract(CHANGELOG.read_text(encoding="utf-8"), argv[1])
    if not section:
        print(f"no CHANGELOG.md section for {argv[1]}", file=sys.stderr)
        return _NOT_FOUND
    print(section)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
