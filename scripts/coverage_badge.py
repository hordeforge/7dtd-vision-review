#!/usr/bin/env python3
"""Render the line-coverage badge SVG from the local .coverage file.

Must be run by an interpreter that has coverage importable: the Makefile
`coverage` target arranges that by running it under `$(PYTHON)` (`uv run
--frozen python3`), and `scripts/bootstrap` syncs the dev dependency group
from the committed lockfile, so `coverage` is present in the project venv.
Usage: coverage_badge.py OUTPUT.svg
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def percentage() -> int:
    out = Path(".coverage.json")
    subprocess.run(
        [sys.executable, "-m", "coverage", "json", "-q", "-o", str(out)],
        check=True,
    )
    # Explicit encoding: a C-locale host (common on dedicated servers) makes
    # the locale default ASCII, and any non-ASCII byte in the report would
    # then crash the badge build.
    data = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    pct = float(data["totals"]["percent_covered"])
    return round(pct)


def colour(pct: int) -> str:
    if pct >= 90:
        return "#4c1"
    if pct >= 75:
        return "#97ca00"
    if pct >= 60:
        return "#dfb317"
    if pct >= 40:
        return "#fe7d37"
    return "#e05d44"


_FONT_STACK = "Verdana,Geneva,DejaVu Sans,sans-serif"


def badge(pct: int, fill: str) -> str:
    label_w, value_w = 64, 36
    gradient = (
        '<linearGradient id="s" x2="0" y2="100%">'
        '<stop offset="0" stop-color="#bbb" stop-opacity=".1"/>'
        '<stop offset="1" stop-opacity=".1"/></linearGradient>'
    )
    clip = (
        f'<clipPath id="r"><rect width="{label_w + value_w}" height="20" rx="3" fill="#fff"/>'
        "</clipPath>"
    )
    bars = (
        f'<g clip-path="url(#r)"><rect width="{label_w}" height="20" fill="#555"/>'
        f'<rect x="{label_w}" width="{value_w}" height="20" fill="{fill}"/>'
        f'<rect width="{label_w + value_w}" height="20" fill="url(#s)"/></g>'
    )
    labels = (
        f'<g fill="#fff" text-anchor="middle" font-family="{_FONT_STACK}" font-size="11">'
        f'<text x="{label_w / 2}" y="14">coverage</text>'
        f'<text x="{label_w + value_w / 2}" y="14">{pct}%</text></g>'
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{label_w + value_w}" '
        f'height="20" role="img" aria-label="coverage: {pct}%">\n'
        f"<title>coverage: {pct}%</title>\n"
        f"{gradient}\n{clip}\n{bars}\n{labels}\n</svg>\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: coverage_badge.py OUTPUT.svg", file=sys.stderr)
        return 2
    pct = percentage()
    Path(argv[1]).write_text(badge(pct, colour(pct)), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
