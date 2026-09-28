"""Print the values the deadeye e2e reads out of files it just produced.

Three one-liners in `e2e.sh` were `python3 -c` bodies, and the closing
summary was a heredoc: JSON parsing and formatting that the linter and the
type checker never saw. Here each is a named command over a file path.

Usage:
  e2e_report.py suite PROVIDER_JSON   the suite id shamway reported
  e2e_report.py size PATH             a file's size in bytes
  e2e_report.py summary EVIDENCE CLIP the human summary of a review envelope

`suite` and `size` print one value for the shell to interpolate; `summary`
prints the block the e2e closes on. A missing key prints `?` rather than
failing the run: a partial envelope is still worth showing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

USAGE = "usage: e2e_report.py {suite PROVIDER_JSON|size PATH|summary EVIDENCE CLIP}"
UNKNOWN = "?"
MAX_REPORTED_ISSUES = 5


def _load(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        document: Any = json.load(handle)
    return document if isinstance(document, dict) else {}


def _text(value: Any, fallback: str = UNKNOWN) -> str:
    """A value as it should read in the summary; anything empty reads `?`."""
    if isinstance(value, (str, int, float)) and value != "":
        return str(value)
    return fallback


def _summary(evidence_path: str, clip_path: str) -> str:
    evidence = _load(evidence_path)
    result = evidence.get("result")
    result = result if isinstance(result, dict) else {}
    provider = evidence.get("provider")
    provider = provider if isinstance(provider, dict) else {}
    raw_issues = result.get("issues")
    issues = (
        [issue for issue in raw_issues if isinstance(issue, dict)]
        if isinstance(raw_issues, list)
        else []
    )
    lines = [
        "",
        "E2E REVIEWED",
        f"  clip       {clip_path}",
        f"  provider   {_text(provider.get('name'))} / {_text(provider.get('model_reported'))}",
        f"  review_id  {_text(evidence.get('review_id'))}",
        f"  verdict    {_text(result.get('summary'), '(no summary)')}",
        f"  confidence {_text(result.get('confidence'))}",
        f"  issues     {len(issues)}",
    ]
    listed = issues[:MAX_REPORTED_ISSUES]
    lines.extend(f"    - {_text(issue.get('description'))}" for issue in listed)
    lines.append(f"  evidence   {evidence_path}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(USAGE, file=sys.stderr)
        return 2
    command, arguments = argv[1], argv[2:]
    if command == "suite" and len(arguments) == 1:
        print(_text(_load(arguments[0]).get("suite"), ""))
    elif command == "size" and len(arguments) == 1:
        print(Path(arguments[0]).stat().st_size)
    elif command == "summary" and len(arguments) == 2:
        print(_summary(arguments[0], arguments[1]))
    else:
        print(USAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
