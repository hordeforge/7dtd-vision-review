"""Pins for `scripts/release_notes.py`, the changelog-section extractor.

The release workflow turns a tagged version's CHANGELOG.md section into the
GitHub Release notes. A heading form that silently fails to match would
publish default notes where consumers expect what changed, so every accepted
heading shape and the not-found exit are pinned here.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent

_NOT_FOUND = 3


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_notes", ROOT / "scripts" / "release_notes.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release_notes = _load()


def test_bare_heading_matches() -> None:
    section = release_notes.extract("## 0.1.0\n\n- added\n", "0.1.0")
    assert section == "- added"


def test_bracketed_heading_matches() -> None:
    assert release_notes.extract("## [0.1.0]\n\n- fixed\n", "0.1.0") == "- fixed"


def test_v_prefixed_heading_matches() -> None:
    assert release_notes.extract("## v0.1.0\n\n- added\n", "0.1.0") == "- added"


def test_dated_keep_a_changelog_heading_matches() -> None:
    document = "## [0.1.0] - 2026-08-26\n\n### Added\n\n- the feature\n"
    assert release_notes.extract(document, "0.1.0") == "### Added\n\n- the feature"


def test_unreleased_section_is_findable_by_name() -> None:
    document = "## Unreleased\n\n- pending\n\n## 0.1.0\n\n- shipped\n"
    assert release_notes.extract(document, "Unreleased") == "- pending"


def test_other_versions_do_not_leak_their_section() -> None:
    document = "## 0.1.0\n\n- shipped\n"
    assert release_notes.extract(document, "0.2.0") is None


def test_missing_version_returns_none() -> None:
    assert release_notes.extract("## Unreleased\n\n- pending\n", "9.9.9") is None


def _first_released_section() -> str:
    """The newest changelog heading with a body, as the CLI would be asked for it.

    Naming a fixed section here (it used to be "Unreleased") made the test fail
    the moment a release renamed it.
    """
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^##\s+(.+?)\s*$", changelog, flags=re.MULTILINE)
    for heading in headings:
        if release_notes.extract(changelog, heading):
            return heading
    raise AssertionError("CHANGELOG.md has no section with a body")


def test_cli_finds_a_present_section_and_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_notes.py"), _first_released_section()],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    # A released section may carry only Changed entries (upkeep releases), so
    # pin the subsection shape, not one specific heading.
    assert re.search(r"^### ", result.stdout, flags=re.MULTILINE)


def test_cli_exits_three_when_no_section_carries_the_version() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_notes.py"), "9.9.9"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == _NOT_FOUND
    assert "no CHANGELOG.md section" in result.stderr
