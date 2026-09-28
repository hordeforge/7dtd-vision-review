"""Release-contract pins: what consumers depend on, guarded mechanically.

CHANGELOG.md and CONTRIBUTING.md declare the intent schema, the result shape,
and the evidence envelope breaking-change surfaces for `7dtd-asset-pipeline`
and `7dtd-playtest`. These tests make an accidental change to any of them fail
`make check test` instead of shipping; a deliberate change updates the pin and
lands a changelog entry in the same commit.
"""

from __future__ import annotations

import re
import tarfile
import tomllib
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import rmtree

import pytest

from deadeye._version import __version__
from deadeye.evidence import EVIDENCE_SCHEMA_VERSION, build_envelope
from deadeye.intent import INTENT_SCHEMA_VERSION, ReviewIntent, parse_intent
from deadeye.prompt import PROMPT_VERSION
from deadeye.result import ADVISORY_NOTE, RESULT_KEYS, RUBRIC_VERSION
from deadeye.sampling import SamplingRecord

ROOT = Path(__file__).resolve().parent.parent

BREAKING = (
    "This is a declared consumer contract (see CONTRIBUTING.md, 'Changing a "
    "contract'): update this pin, bump schema_version where one applies, and "
    "record the change under a breaking heading in CHANGELOG.md."
)

FINAL_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
# PEP 508 exact pin: name==version, no extras, no range operators.
EXACT_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^,;<>=!~\s]+)$")


def test_manifest_and_version_mirror_agree() -> None:
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert manifest["project"]["version"] == __version__, (
        f"pyproject.toml says {manifest['project']['version']} but "
        f"src/deadeye/_version.py says {__version__}; keep both declarations "
        "in sync (CONTRIBUTING.md, 'Releases')"
    )


def test_version_is_a_final_release_triple() -> None:
    # The tag gate in .github/workflows/release.yml pairs a vX.Y.Z tag with
    # this version. A pre-release needs a conscious change here first.
    assert FINAL_VERSION.fullmatch(__version__), BREAKING


def test_dev_dependencies_are_exact_pins_matching_the_lock() -> None:
    # A range here lets a lock-less install pick a newer major; ruff/mypy
    # verdicts and the suite itself then disagree with CI. Every tool in
    # the dev group, and the build backend, is one exact version.
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {pkg["name"]: pkg["version"] for pkg in lock["package"]}

    def require_exact(requirement: str, *, role: str) -> tuple[str, str]:
        matched = EXACT_PIN.fullmatch(requirement)
        assert matched is not None, (
            f"{role} {requirement!r} is not an exact name==version pin; "
            "bump the pin and uv.lock together, never reopen a range"
        )
        name, version = matched.group(1), matched.group(2)
        assert locked.get(name) == version, (
            f"{role} pins {name}=={version} but uv.lock resolves "
            f"{name}=={locked.get(name)!r}; keep the pin and the lock in sync"
        )
        return name, version

    backend = manifest["build-system"]["requires"]
    assert len(backend) == 1, "one build backend; extra requires grow the isolated build env"
    backend_name, backend_version = require_exact(backend[0], role="build-system.requires")

    pinned: dict[str, str] = {}
    for requirement in manifest["dependency-groups"]["dev"]:
        name, version = require_exact(requirement, role="dependency-groups.dev")
        pinned[name] = version
    assert pinned.get(backend_name) == backend_version, (
        f"dev group must pin {backend_name}=={backend_version} to match "
        "[build-system]; bump both together"
    )


def test_result_key_set_is_pinned() -> None:
    assert tuple(RESULT_KEYS) == (
        "summary",
        "strengths",
        "issues",
        "recommended_changes",
        "rubric_scores",
        "confidence",
        "limitations",
    ), BREAKING


def _envelope(*, elapsed_seconds: float = 0.0) -> dict[str, object]:
    return build_envelope(
        media_entries=(),
        sampling=SamplingRecord(
            frames_available=0,
            frames_submitted=0,
            sampled=False,
            frame_indices=(),
            submitted_files=(),
            note="",
        ),
        intent=ReviewIntent(
            purpose="p",
            subject="",
            camera_path="",
            desired_qualities="",
            avoid=(),
            references=(),
            questions=(),
            suite="",
            case="",
        ),
        intent_raw=b"",
        provider_name="fake",
        endpoint_mode="default",
        model_requested="m",
        model_reported=None,
        prompt="",
        result=None,
        error=None,
        raw_response=None,
        usage=None,
        total_bytes=0,
        params={},
        elapsed_seconds=elapsed_seconds,
    )


def test_evidence_envelope_top_level_keys_are_pinned() -> None:
    assert set(_envelope()) == {
        "kind",
        "schema_version",
        "tool_version",
        "created_utc",
        "review_id",
        "advisory_only",
        "note",
        "intent",
        "media",
        "sampling",
        "provider",
        "rubric_version",
        "prompt_version",
        "prompt",
        "result",
        "error",
        "raw_provider_response",
        "usage",
        "disclosure",
        "parameters",
    }, BREAKING


def test_intent_wire_field_set_is_pinned() -> None:
    document = {
        "schema_version": INTENT_SCHEMA_VERSION,
        "purpose": "show the silhouette",
        "subject": "s",
        "camera_path": "fixed",
        "desired_qualities": "q",
        "avoid": ["a"],
        "references": [{"path": "ref.png", "purpose": "compare"}],
        "questions": ["?"],
        "suite": "suite",
        "case": "case",
    }
    parsed = parse_intent(document, "intent")
    assert set(document) == set(ReviewIntent.__dataclass_fields__) | {"schema_version"}, BREAKING
    assert isinstance(parsed, ReviewIntent)


def test_created_utc_is_an_aware_utc_instant() -> None:
    """`created_utc` is an RFC 3339 instant, never host-local or zone-less.

    A naive stamp would be interpreted in the consumer's TZ (a 23:30 UTC
    run becoming the previous calendar day in US zones) and a host-local
    offset would freeze whatever TZ the review machine happened to use.
    """
    stamp = _envelope()["created_utc"]
    assert isinstance(stamp, str)
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # Aware UTC `isoformat(timespec="seconds")` emits `+00:00`, never `Z`
    # and never a missing offset. Pin that so a later naive or local stamp
    # cannot ship as the same field.
    assert stamp.endswith("+00:00")
    assert "T" in stamp
    # Second precision: the field is an audit instant, not a unique id
    # (`review_id` is). Subseconds must not reappear as a format change.
    assert parsed == parsed.replace(microsecond=0)
    assert abs((parsed - datetime.now(UTC)).total_seconds()) < 5


def test_elapsed_seconds_is_recorded_to_milliseconds() -> None:
    provider = _envelope(elapsed_seconds=1.23456)["provider"]
    assert isinstance(provider, dict)
    assert provider["elapsed_seconds"] == 1.235


def test_contract_versions_are_recorded_in_the_envelope() -> None:
    envelope = _envelope()
    assert envelope["kind"] == "deadeye-review"
    assert envelope["schema_version"] == EVIDENCE_SCHEMA_VERSION
    assert envelope["tool_version"] == __version__
    assert envelope["rubric_version"] == RUBRIC_VERSION
    assert envelope["prompt_version"] == PROMPT_VERSION
    assert envelope["advisory_only"] is True
    assert envelope["note"] == ADVISORY_NOTE


def test_current_schema_versions_are_one() -> None:
    # Consumers compare these to decide whether their recorded intents and
    # stored evidence still parse; a jump must be deliberate (BREAKING above).
    assert INTENT_SCHEMA_VERSION == 1
    assert EVIDENCE_SCHEMA_VERSION == 1


def test_rubric_and_prompt_versions_are_pinned() -> None:
    # Both ride in every envelope beside `schema_version`, and a consumer
    # comparing them to decide whether it can trust an older verdict needs
    # them to move only on purpose. `prompt_version` reads 3 because the
    # reviewer instruction travels as the system instruction and the author's
    # statement is the only authored text in the user turn.
    assert RUBRIC_VERSION == "1", BREAKING
    assert PROMPT_VERSION == "3", BREAKING


# --- Release artifacts -----------------------------------------------------
#
# A vX.Y.Z tag publishes the sdist and wheel `uv build` produces (see
# .github/workflows/release.yml). These tests build both with the declared
# backend and pin what ships, so a file that silently drops out of an
# artifact fails `make check test` instead of surfacing in a release.

SDIST_PREFIX = f"7dtd_vision_review-{__version__}"
DIST_INFO = f"7dtd_vision_review-{__version__}.dist-info"


@pytest.fixture
def built_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    from setuptools import build_meta

    monkeypatch.chdir(ROOT)
    sdist_name = build_meta.build_sdist(str(tmp_path))
    wheel_name = build_meta.build_wheel(str(tmp_path))
    assert sdist_name == f"{SDIST_PREFIX}.tar.gz", (
        "the sdist name must embed the mirrored __version__, not a stale one"
    )
    assert wheel_name == f"{SDIST_PREFIX}-py3-none-any.whl"
    with tarfile.open(tmp_path / sdist_name) as archive:
        sdist_members = archive.getnames()
    with zipfile.ZipFile(tmp_path / wheel_name) as archive:
        wheel_members = archive.namelist()
    # build_meta leaves egg-info and build/ beside the sources; those are
    # gitignored byproducts of every artifact build, but the suite should
    # leave no tarball or wheel behind inside the checkout.
    rmtree(ROOT / "build", ignore_errors=True)
    return {"sdist": sdist_members, "wheel": wheel_members}


def test_sdist_is_a_complete_source_tree(built_artifacts: dict[str, list[str]]) -> None:
    members = built_artifacts["sdist"]
    must_ship = (
        # Build inputs and metadata.
        f"{SDIST_PREFIX}/pyproject.toml",
        f"{SDIST_PREFIX}/README.md",
        f"{SDIST_PREFIX}/LICENSE",
        f"{SDIST_PREFIX}/uv.lock",
        f"{SDIST_PREFIX}/Makefile",
        f"{SDIST_PREFIX}/config.toml",
        f"{SDIST_PREFIX}/src/deadeye/config.local.toml.example",
        # The committed test suite must be runnable from the tarball:
        # conftest.py defines the fixtures every test module imports.
        f"{SDIST_PREFIX}/tests/conftest.py",
        f"{SDIST_PREFIX}/tests/test_cli.py",
        # The contributor workflow README documents from a checkout.
        f"{SDIST_PREFIX}/scripts/bootstrap",
        f"{SDIST_PREFIX}/docs/architecture.md",
        f"{SDIST_PREFIX}/CHANGELOG.md",
        f"{SDIST_PREFIX}/SECURITY.md",
        f"{SDIST_PREFIX}/CONTRIBUTING.md",
    )
    missing = [name for name in must_ship if name not in members]
    assert not missing, (
        f"sdist is missing {missing}; extend MANIFEST.in so an unpacked "
        "release tarball behaves like a checkout"
    )
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in members), (
        "compiled bytecode never ships in an sdist; MANIFEST.in must exclude it"
    )


def test_wheel_ships_exactly_the_package(built_artifacts: dict[str, list[str]]) -> None:
    members = built_artifacts["wheel"]
    shipped = {name for name in members if not name.startswith(DIST_INFO)}
    assert "deadeye/py.typed" in shipped, (
        "PEP 561 marker missing: the package is strictly typed, so consumers' "
        "type checkers must see that"
    )
    assert "deadeye/config.local.toml.example" in shipped, (
        "the config template is the one home for the config schema; a user who "
        "installs the wheel has no checkout to copy it from, and `deadeye "
        "doctor` names this exact path"
    )
    assert f"{DIST_INFO}/licenses/LICENSE" in members
    assert f"{DIST_INFO}/entry_points.txt" in members
    assert not any(name.startswith("tests/") or "__pycache__" in name for name in members), (
        "the wheel is a runtime artifact: tests and bytecode never ship in it"
    )

    src = ROOT / "src"
    on_disk = {p.relative_to(src).as_posix() for p in src.rglob("*.py")}
    dropped = on_disk - shipped
    assert not dropped, (
        f"modules on disk but absent from the wheel: {sorted(dropped)}; a "
        "subpackage was added without being packaged?"
    )


# Keep a Changelog's subsection order, with the breaking heading this repo
# requires first. The order is the release contract: a consumer reads
# `### Breaking` and nothing else, so an entry filed under `Changed` is
# invisible to the reader who needs it.
SECTION_ORDER = ("Breaking", "Added", "Changed", "Fixed", "Security")


def _assert_declared_headings(section: str, headings: list[str], rank: dict[str, int]) -> None:
    undeclared = [name for name in headings if name not in rank]
    assert not undeclared, (
        f"CHANGELOG.md section {section!r} uses heading(s) {undeclared}; the "
        f"declared ones are {list(SECTION_ORDER)}"
    )
    ranks = [rank[name] for name in headings]
    assert ranks == sorted(ranks), (
        f"CHANGELOG.md section {section!r} lists {headings} out of the declared "
        f"order {list(SECTION_ORDER)}"
    )


def test_changelog_sections_use_the_declared_headings() -> None:
    document = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "`### Breaking`" in document, (
        "the changelog preamble must state the breaking heading CONTRIBUTING.md "
        "'Changing a contract' requires, so a contributor finds it while "
        "writing the entry"
    )
    rank = {name: position for position, name in enumerate(SECTION_ORDER)}
    section = "the preamble"
    headings: list[str] = []
    for line in document.splitlines():
        if line.startswith("## "):
            _assert_declared_headings(section, headings, rank)
            section, headings = line.removeprefix("## ").strip(), []
        elif line.startswith("### "):
            headings.append(line.removeprefix("### ").strip())
    _assert_declared_headings(section, headings, rank)
