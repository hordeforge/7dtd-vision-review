"""Release-contract pins: what consumers depend on, guarded mechanically.

CHANGELOG.md and CONTRIBUTING.md declare the intent schema, the result shape,
and the evidence envelope breaking-change surfaces for `7dtd-asset-pipeline`
and `7dtd-playtest`. These tests make an accidental change to any of them fail
`make check test` instead of shipping; a deliberate change updates the pin and
lands a changelog entry in the same commit.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from deadeye._version import __version__
from deadeye.evidence import EVIDENCE_SCHEMA_VERSION, build_envelope
from deadeye.intent import INTENT_SCHEMA_VERSION, ReviewIntent, parse_intent
from deadeye.result import ADVISORY_NOTE, PROMPT_VERSION, RESULT_KEYS, RUBRIC_VERSION
from deadeye.sampling import SamplingRecord

ROOT = Path(__file__).resolve().parent.parent

BREAKING = (
    "This is a declared consumer contract (see CONTRIBUTING.md, 'Changing a "
    "contract'): update this pin, bump schema_version where one applies, and "
    "record the change under a breaking heading in CHANGELOG.md."
)

FINAL_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


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
    assert FINAL_VERSION.match(__version__), BREAKING


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


def _envelope() -> dict[str, object]:
    return build_envelope(
        media_entries=(),
        sampling=SamplingRecord(
            frames_available=0,
            frames_submitted=0,
            sampled=False,
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
