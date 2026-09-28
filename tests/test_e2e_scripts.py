"""The e2e's helper scripts under a host whose locale is not UTF-8.

`scripts/e2e.sh` runs these with a bare `python3`, so nothing puts deadeye on
the path and the library's own `_streams` binding never applies. The closing
summary prints a review's verdict, which is a model's own words and not ASCII
by construction. Under C or POSIX (cron, a service unit, a CI job with no
LANG) Python binds stdout to ASCII and one such character raises out of
`print`, which is how the e2e died after the review was already billed and the
verdict validated. These tests pin the binding, and the decode of the
doctor's UTF-8 stdout, against the shipped scripts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

ASCII_LOCALE_ENV = {
    **os.environ,
    "LC_ALL": "C",
    "LANG": "C",
    "PYTHONCOERCECLOCALE": "0",
    "PYTHONUTF8": "0",
}


def _run(script: str, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=ASCII_LOCALE_ENV,
        check=False,
    )


def test_doctor_query_prints_a_non_ascii_credential_detail(tmp_path: Path) -> None:
    """The detail is read from a file and piped in, never through argv: under
    this locale the parent's own argv encoding is ASCII, so a name written
    there would be testing the harness rather than the script."""
    doctor = [
        {"name": "nvidia", "state": "configured", "detail": "key from 設定ファイル"},
    ]
    result = _run("doctor_query.py", "detail", "nvidia", stdin=json.dumps(doctor))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "key from 設定ファイル"


def test_doctor_query_reads_utf8_stdin_under_an_ascii_locale() -> None:
    """`deadeye doctor --json` binds its own stdout to UTF-8, so the bytes
    arriving here are UTF-8 whatever the reader's locale says. The decode
    belongs to the writer's declared encoding, not to the locale the caller
    happens to run under."""
    doctor = [{"name": "nvidia", "state": "configured", "detail": "key from 設定ファイル"}]
    result = _run("doctor_query.py", "select", stdin=json.dumps(doctor))

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nvidia"


def test_e2e_report_summarizes_a_non_ascii_verdict(tmp_path: Path) -> None:
    """The closing summary of a real, billable review, printed on a host whose
    locale cannot encode it."""
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "review_id": "r-1",
                "provider": {"name": "nvidia", "model_reported": "テスト"},
                "result": {
                    "summary": "字幕の品質は良好です",
                    "confidence": 0.9,
                    "issues": [{"description": " Reciprocity glitch at the turn"}],
                },
            }
        ),
        encoding="utf-8",
    )

    result = _run("e2e_report.py", "summary", str(evidence), str(tmp_path / "clip.mp4"))

    assert result.returncode == 0, result.stderr
    assert "字幕の品質は良好です" in result.stdout
    assert "issues     1" in result.stdout
