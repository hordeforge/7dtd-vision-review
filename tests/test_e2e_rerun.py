"""What a second `scripts/e2e.sh` finds, driven end to end with stub siblings.

The e2e is the one path here that submits to a real provider, so its re-run
property is the expensive one: a run that dies partway must not leave a marker
that makes the next run reuse a half-built fixture, and a run that completes
must leave one that a later run reuses instead of re-scaffolding (and, with
it, re-scaffolding the turntable case the review is about).

The whole chain is stubbed at its edges (`deadeye`, `ffmpeg`, `shamway`, the
capture script, and the three sibling checkouts) so the real script runs, in
order, over a real filesystem. That is the only way to pin the marker: the
property is a write ordering inside the script, not a function a test can
call.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
E2E = ROOT / "scripts" / "e2e.sh"

# `deadeye` answers the preflight, the doctor read, and the review. The review
# stub writes the envelope the summary step reads, so the run reaches its
# closing line the way a real one does.
DEADEYE_STUB = """#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
--help) echo "deadeye stub"; exit 0 ;;
doctor) echo '[{"name": "fake", "state": "configured", "detail": "offline stub"}]'; exit 0 ;;
review) ;;
*) echo "unexpected deadeye call: $*" >&2; exit 2 ;;
esac
output=""
while [[ $# -gt 0 ]]; do
    case "$1" in
    --output) output="$2"; shift 2 ;;
    *) shift ;;
    esac
done
[[ -n "$output" ]] || { echo "stub deadeye: no --output in $*" >&2; exit 2; }
cat > "$output" <<'JSON'
{
  "review_id": "stub-review",
  "provider": {"name": "fake", "model_reported": "deadeye-fake-vision-v1"},
  "result": {"summary": "stub verdict", "confidence": 0.4, "issues": []}
}
JSON
cat "$output"
"""

# Every command the e2e's scaffold block issues, so the script under test is
# the real one. `client hold` ends at the `--` and runs what follows, which is
# the deploy of the harness pair into the shared Mods folder.
SHAMWAY_STUB = """#!/usr/bin/env bash
set -euo pipefail
command="${1:-}"
shift || true
case "$command" in
init) printf '[mod]\\n' > .shamway.toml ;;
generate) mkdir -p "$(dirname "$2")" ;;
build) : ;;
acceptance-provider) echo '{"suite": "motion_thing"}' ;;
client)
    sub="${1:-}"
    shift || true
    if [[ "$sub" == "hold" ]]; then
        while [[ $# -gt 0 ]]; do
            [[ "$1" == "--" ]] && { shift; break; }
            shift
        done
        "$@"
    fi
    ;;
*) echo "unexpected shamway call: $command $*" >&2; exit 2 ;;
esac
"""

# Stands in for the in-game capture: the e2e checks the muxed clip exists and
# is non-empty, and hands its path to the review.
CAPTURE_STUB = """#!/usr/bin/env bash
set -euo pipefail
out=""
while [[ $# -gt 0 ]]; do
    case "$1" in
    --out) out="$2"; shift 2 ;;
    *) shift ;;
    esac
done
mkdir -p "$out"
printf 'stub muxed clip\\n' > "$out/motion_thing.mp4"
"""

FFMPEG_STUB = """#!/usr/bin/env bash
exit 0
"""


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def e2e_environment(tmp_path: Path) -> dict[str, str]:
    """A PATH and a set of roots in which the real e2e.sh runs to completion."""
    binaries = tmp_path / "bin"
    for name, body in (
        ("deadeye", DEADEYE_STUB),
        ("shamway", SHAMWAY_STUB),
        ("ffmpeg", FFMPEG_STUB),
        # Preflight only: every detection path is satisfied by an exported
        # GAME/COMPAT/server, so uv is never actually asked to resolve
        # anything.
        ("uv", "#!/usr/bin/env bash\nexit 0\n"),
    ):
        _write(binaries / name, body)

    pipeline = tmp_path / "pipeline"
    (pipeline / "src" / "sevendtd_asset_pipeline").mkdir(parents=True)

    playtest = tmp_path / "playtest"
    _write(playtest / "scripts" / "capture_video.sh", CAPTURE_STUB)
    # The harness is already built, so the e2e does not reach for `make`.
    (playtest / "dist" / "7dtd-playtest").mkdir(parents=True)
    (playtest / "dist" / "7dtd-playtest" / "7dtd-playtest.dll").write_bytes(b"stub")

    connect = tmp_path / "connect"
    _write(connect / "scripts" / "launch_client.sh", "#!/usr/bin/env bash\nexit 0\n")
    (connect / "dist" / "7dtd-fastconnect").mkdir(parents=True)

    game = tmp_path / "game"
    game.mkdir()
    (game / "7DaysToDie.exe").write_bytes(b"stub")

    server = tmp_path / "server"
    server.mkdir()
    (server / "7DaysToDieServer.x86_64").write_bytes(b"stub")

    # COMPAT is the Proton prefix root, and the e2e appends `pfx/drive_c/...`
    # to it itself, so the `pfx` level belongs to the tree below this one.
    compat = tmp_path / "compat"
    (compat / "pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/Mods").mkdir(parents=True)

    (tmp_path / "mod").mkdir()
    (tmp_path / "out").mkdir()

    return {
        **os.environ,
        "PATH": f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}",
        "ASSET_PIPELINE_ROOT": str(pipeline),
        "PLAYTEST_ROOT": str(playtest),
        "CONNECT_ROOT": str(connect),
        "E2E_MOD_DIR": str(tmp_path / "mod"),
        "E2E_OUT": str(tmp_path / "out"),
        "GAME": str(game),
        "COMPAT": str(compat),
        "SEVEN_DAYS_TO_DIE_SERVER_DIR": str(server),
    }


def _run(environment: dict[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(E2E), *arguments],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_second_e2e_run_reuses_the_fixture_and_completes(
    e2e_environment: dict[str, str], tmp_path: Path
) -> None:
    """Two runs of the same command, both green, one scaffold.

    The second run must take the reuse branch and leave the fixture's own
    files alone. Rebuilding it would be harmless here, but the e2e is the
    expensive gate: a re-run that re-scaffolds is a run that spends the
    capture and the submission again to learn what the first one knew.
    """
    first = _run(e2e_environment, "--provider", "fake")
    assert first.returncode == 0, first.stderr
    assert "scaffolding fixture modlet" in first.stdout

    marker = tmp_path / "mod" / ".suite"
    intent = tmp_path / "mod" / "thing.review.json"
    assert marker.read_text(encoding="utf-8").strip() == "motion_thing"
    assert '"suite": "motion_thing"' in intent.read_text(encoding="utf-8")

    second = _run(e2e_environment, "--provider", "fake")
    assert second.returncode == 0, second.stderr
    assert "reusing fixture modlet" in second.stdout
    assert "scaffolding fixture modlet" not in second.stdout
    # Both runs published their own evidence, under their own run directory,
    # and neither review landed on the other's file.
    evidence = sorted((tmp_path / "out").glob("*/evidence.json"))
    assert len(evidence) == 2
    assert marker.read_text(encoding="utf-8").strip() == "motion_thing"


def test_a_marker_left_without_the_fixture_it_names_does_not_wedge_the_rerun(
    e2e_environment: dict[str, str], tmp_path: Path
) -> None:
    """A killed scaffold must not leave a reuse record that cannot be used.

    `.suite` is what the reuse branch tests, so publishing it before the last
    artifact it claims would make every later run skip the scaffold and die on
    a file no run ever wrote. This is the state such a crash leaves behind, and
    the run has to re-scaffold its way out of it.
    """
    (tmp_path / "mod" / ".suite").write_text("motion_thing\n", encoding="utf-8")

    result = _run(e2e_environment, "--provider", "fake")
    assert result.returncode == 0, result.stderr
    assert "scaffolding fixture modlet" in result.stdout
    assert (tmp_path / "mod" / "thing.review.json").is_file()
    assert not (tmp_path / "mod" / "thing.review.json.in").exists()
    assert not (tmp_path / "mod" / ".suite.in").exists()
