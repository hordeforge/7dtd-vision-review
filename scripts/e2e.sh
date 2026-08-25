#!/usr/bin/env bash
# scripts/e2e.sh - the vendored end-to-end test for deadeye.
#
# Runs the whole chain against a real 7 Days to Die client:
#
#   1. scaffold a fixture modlet (`.local/vision-e2e-mod`) whose turntable
#      acceptance case is a CaseDef.StagedClip (shamway, from a sibling
#      7dtd-asset-pipeline checkout; no Unity editor);
#   2. capture the clip IN GAME through 7dtd-playtest's StagedClip support
#      and scripts/capture_video.sh - every frame is the client process's
#      own ScreenCapture framebuffer, super-sized, muxed by ffmpeg. There is
#      no desktop, window, or screen recording anywhere in this test;
#   3. review the muxed clip with deadeye against the configured provider
#      (--allow-network) and write the evidence envelope;
#   4. print the verdict summary and artifact paths; exit non-zero on any
#      failure, so the script is usable as a gate.
#
# Reusable: no hardcoded host paths. Sibling checkouts are discovered
# relative to this repository (override with ASSET_PIPELINE_ROOT /
# PLAYTEST_ROOT / CONNECT_ROOT), the client install is found by
# 7dtd-playtest's own Steam-library scan (export GAME to override), and the
# dedicated server comes from SEVEN_DAYS_TO_DIE_SERVER_DIR or --game-srv
# (with a Steam-library scan as a convenience fallback).
#
# Usage:
#   scripts/e2e.sh [--provider NAME] [--model ID] [--game-srv DIR]
#                  [--clip PATH] [--intent FILE] [--fresh] [--help]
#
# Env:
#   ASSET_PIPELINE_ROOT / PLAYTEST_ROOT / CONNECT_ROOT  sibling checkouts
#   GAME                  client install (detected when unset)
#   SEVEN_DAYS_TO_DIE_SERVER_DIR   dedicated server (or --game-srv)
#   DEADEYE_CONFIG_DIR    deadeye config dir (default: this repo)
#   E2E_OUT               artifact root (default: .local/e2e)
#   E2E_MOD_DIR           fixture modlet dir (default: .local/vision-e2e-mod)
#
# Provider selection: --provider, else the config's default_provider when
# that provider has a key, else the first configured real provider, else a
# refusal naming what is missing.
#
# Exit codes:
#   0  full chain reviewed and evidence written
#   1  a step failed (preflight, capture, or review) - stderr says which
#   2  usage error
set -euo pipefail

die() {
    echo "ERROR: $*" >&2
    exit 1
}
usage() {
    sed -n '2,44p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT" # this test is defined by this repo; run from wherever you are
export DEADEYE_CONFIG_DIR="${DEADEYE_CONFIG_DIR:-$ROOT}"

# ------------------------------------------------------------------ config
ASSET_PIPELINE_ROOT="${ASSET_PIPELINE_ROOT:-$ROOT/../7dtd-asset-pipeline}"
PLAYTEST_ROOT="${PLAYTEST_ROOT:-$ROOT/../7dtd-playtest}"
CONNECT_ROOT="${CONNECT_ROOT:-$ROOT/../7dtd-fastconnect}"
MOD_DIR="${E2E_MOD_DIR:-$ROOT/.local/vision-e2e-mod}"
OUT_BASE="${E2E_OUT:-$ROOT/.local/e2e}"
CLIP_STEM="thing" # fixture asset stem; the generated case id is motion_<stem>
CLIP_ID="motion_${CLIP_STEM}"
PROVIDER=""
MODEL=""
GAME_SRV="${SEVEN_DAYS_TO_DIE_SERVER_DIR:-}"
GAME="${GAME:-}"
COMPAT="${COMPAT:-}"
CLIP=""
INTENT=""
FRESH=0

while (($#)); do
    case "$1" in
        --provider) PROVIDER="${2:-}"; shift 2 ;;
        --model) MODEL="${2:-}"; shift 2 ;;
        --game-srv) GAME_SRV="${2:-}"; shift 2 ;;
        --clip) CLIP="${2:-}"; shift 2 ;;
        --intent) INTENT="${2:-}"; shift 2 ;;
        --fresh) FRESH=1; shift ;;
        -h | --help) usage ;;
        *) echo "e2e.sh: unknown argument '$1' (try --help)" >&2; exit 2 ;;
    esac
done

say() { echo "e2e: $*"; }

# --------------------------------------------------------------- preflight
command -v deadeye >/dev/null 2>&1 || {
    if [[ -x "$ROOT/.venv/bin/deadeye" ]]; then
        export PATH="$ROOT/.venv/bin:$PATH"
    else
        die "deadeye CLI not found; run scripts/bootstrap or put deadeye on PATH"
    fi
}
deadeye --help >/dev/null 2>&1 || die "deadeye on PATH does not run (is the venv broken?)"
command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg is required to mux the captured clip"
command -v uv >/dev/null 2>&1 || die "uv is required (the playtest runner and detection run under it)"

[[ -d "$ASSET_PIPELINE_ROOT/src/sevendtd_asset_pipeline" ]] ||
    die "no 7dtd-asset-pipeline checkout at $ASSET_PIPELINE_ROOT (set ASSET_PIPELINE_ROOT)"
[[ -f "$PLAYTEST_ROOT/scripts/capture_video.sh" ]] ||
    die "no 7dtd-playtest checkout at $PLAYTEST_ROOT (set PLAYTEST_ROOT)"
[[ -f "$CONNECT_ROOT/scripts/launch_client.sh" ]] ||
    die "no 7dtd-fastconnect checkout at $CONNECT_ROOT (set CONNECT_ROOT)"

SHAMWAY="${SHAMWAY:-}"
if [[ -z "$SHAMWAY" ]]; then
    if [[ -x "$ASSET_PIPELINE_ROOT/.venv/bin/shamway" ]]; then
        SHAMWAY="$ASSET_PIPELINE_ROOT/.venv/bin/shamway"
    elif command -v shamway >/dev/null 2>&1; then
        SHAMWAY="$(command -v shamway)"
    else
        die "shamway (7dtd-asset-pipeline CLI) not found; set SHAMWAY"
    fi
fi

# The provider the run will use: --provider, else the config's
# default_provider when that one has a key, else the first configured real
# provider, else refuse. The e2e must validate a *configured* provider, so
# an unconfigured default is never silently used.
DOCTOR="$(deadeye doctor --json)" || die "deadeye doctor failed"
if [[ -z "$PROVIDER" ]]; then
    CONFIG_DEFAULT="$(sed -n 's/^default_provider[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$ROOT/config.toml" | head -1)"
    PROVIDER="$(printf '%s' "$DOCTOR" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
preferred = sys.argv[1]
for entry in doc:
    if entry.get("name") == preferred and entry.get("state") == "configured":
        print(preferred)
        sys.exit(0)
for entry in doc:
    if entry.get("state") == "configured" and entry.get("name") != "fake":
        print(entry.get("name"))
        sys.exit(0)
' "$CONFIG_DEFAULT" || true)"
fi
[[ -n "$PROVIDER" ]] || die "no configured provider; put a key in config.local.toml (see config.local.toml.example) or pass --provider NAME"
PROVIDER_STATE="$(printf '%s' "$DOCTOR" | python3 -c '
import json, sys
try:
    doc = json.load(sys.stdin)
except Exception:
    sys.exit(2)
for entry in doc:
    if entry.get("name") == sys.argv[1]:
        print(entry.get("state", ""))
        sys.exit(0)
sys.exit(1)
' "$PROVIDER" 2>/dev/null || true)"
if [[ "$PROVIDER_STATE" != "configured" ]]; then
    PROVIDER_DETAIL="$(printf '%s' "$DOCTOR" | python3 -c '
import json, sys
doc = json.load(sys.stdin)
for entry in doc:
    if entry.get("name") == sys.argv[1]:
        print(entry.get("detail", "no credential configured"))
        sys.exit(0)
print("unknown provider")
' "$PROVIDER" 2>/dev/null || true)"
    die "provider '$PROVIDER' is not configured: $PROVIDER_DETAIL (put the key in config.local.toml)"
fi
say "provider: $PROVIDER (configured); config dir: $DEADEYE_CONFIG_DIR"

# Detection helpers - all of them ask 7dtd-playtest's own code, which is the
# single place Steam installs are resolved in this workspace.
python_detect() { # $1 = python body; further args become sys.argv[2..]
    local body="$1"
    shift
    uv run --project "$PLAYTEST_ROOT" python3 -c "$body" "$PLAYTEST_ROOT/scripts" "$@" 2>/dev/null || true
}
detect_game() {
    python_detect '
import sys
sys.path.insert(0, sys.argv[1])
import playtest_run as p
print(p.client_game_dir() or "")
'
}
compat_for_game() {
    python_detect '
import pathlib, sys
sys.path.insert(0, sys.argv[1])
import playtest_run as p
print(p.client_compat_for_game(pathlib.Path(sys.argv[2])))
' "$1"
}
detect_server() {
    python_detect '
import sys
sys.path.insert(0, sys.argv[1])
import playtest_run as p
for lib in p.steam_library_dirs():
    cand = lib / "common" / "7 Days to Die Dedicated Server"
    if (cand / "7DaysToDieServer.x86_64").is_file():
        print(cand)
        sys.exit(0)
default = p.DEFAULT_GAME_SRV
if (default / "7DaysToDieServer.x86_64").is_file():
    print(default)
'
}

# In-game capture needs the client install and a dedicated server; the
# review-only mode (--clip) needs neither.
if [[ -z "$CLIP" ]]; then
    if [[ -z "$GAME" ]]; then
        GAME="$(detect_game)"
        [[ -n "$GAME" ]] || die "no 7 Days to Die client found; export GAME=<client install> (7dtd-playtest scans standard Steam libraries)"
    fi
    [[ -f "$GAME/7DaysToDie.exe" ]] || die "$GAME holds no 7DaysToDie.exe; GAME must name the client install"
    COMPAT="${COMPAT:-$(compat_for_game "$GAME")}"
    [[ -n "$COMPAT" ]] || die "cannot derive the Proton prefix for $GAME (not below a Steam library); export COMPAT"
    export GAME COMPAT CLIENT_PLATFORM="${CLIENT_PLATFORM:-local}"
    # shamway resolves the client install from SEVEN_DAYS_TO_DIE_DIR (not
    # GAME, which launch_client.sh reads); without it `acceptance-provider
    # --install` and `client deploy` cannot find the client's Mods folder.
    export SEVEN_DAYS_TO_DIE_DIR="$GAME"
    if [[ -z "$GAME_SRV" ]]; then
        GAME_SRV="$(detect_server)"
        [[ -n "$GAME_SRV" ]] || die "no dedicated server found; export SEVEN_DAYS_TO_DIE_SERVER_DIR=<server install> or pass --game-srv"
    fi
    [[ -f "$GAME_SRV/7DaysToDieServer.x86_64" ]] || die "$GAME_SRV holds no 7DaysToDieServer.x86_64; --game-srv must name a stock dedicated server"
    export SEVEN_DAYS_TO_DIE_SERVER_DIR="$GAME_SRV"
    say "client: $GAME"
    say "prefix: $COMPAT"
    say "server: $GAME_SRV"
fi

# ------------------------------------------------------- 1. fixture modlet
# The fixture is a synthesized box on a turntable. It is regenerable scratch
# under .local/ (gitignored); the e2e creates it, it is never committed.
HARNESS_DLL="$PLAYTEST_ROOT/dist/7dtd-playtest/7dtd-playtest.dll"
if [[ -z "$CLIP" ]]; then
    if [[ ! -f "$HARNESS_DLL" ]]; then
        say "building the 7dtd-playtest harness against $GAME"
        make -C "$PLAYTEST_ROOT" build GAME="$GAME" >/dev/null
    fi
    [[ -f "$HARNESS_DLL" ]] || die "no harness at $HARNESS_DLL even after build"

    if [[ -f "$MOD_DIR/.shamway.toml" && "$FRESH" -eq 0 ]]; then
        say "reusing fixture modlet at $MOD_DIR (--fresh to rebuild it)"
    else
        say "scaffolding fixture modlet at $MOD_DIR"
        rm -rf "$MOD_DIR"
        mkdir -p "$MOD_DIR"
        (
            cd "$MOD_DIR"
            "$SHAMWAY" init . --mod-name VisionE2E --bundle-name vision_e2e.unity3d \
                --bundle-source synthesized --game-dir "$GAME"
            "$SHAMWAY" generate mesh "assets-src/bundle/${CLIP_STEM}.glb" \
                --shape box --size 0.2 0.2 0.5
            cat >> .shamway.toml <<'EOF'

[acceptance]
motion_kinds = { thing = "turntable" }
EOF
            "$SHAMWAY" build
            "$SHAMWAY" acceptance-provider --harness-dll "$HARNESS_DLL" --install --json \
                > .provider.json
        )
        SUITE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["suite"])' "$MOD_DIR/.provider.json")"
        [[ -n "$SUITE" ]] || die "shamway acceptance-provider reported no suite id"
        printf '%s\n' "$SUITE" > "$MOD_DIR/.suite"
        cat > "$MOD_DIR/thing.review.json" <<EOF
{"schema_version": 1, "purpose": "verify the box reads as a solid, well-proportioned prop through a full turntable turn", "subject": "thing (synthesized box mesh)", "camera_path": "turntable", "desired_qualities": "proportions hold, silhouette reads from every side, no distortion during rotation", "avoid": ["clipping", "popping", "jitter", "scale errors"], "questions": ["does any face warp or pop during the turn?"], "suite": "$SUITE", "case": "$CLIP_ID"}
EOF
    fi
    SUITE="$(cat "$MOD_DIR/.suite")"
    [[ -n "$SUITE" ]] || die "fixture modlet at $MOD_DIR has no recorded suite (remove it or pass --fresh)"

    # Deploy the modlet, the generated provider, and the harness pair into
    # the client's Mods folder behind the shared client lock.
    MODS_DIR="$COMPAT/pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/Mods"
    say "deploying the fixture and harness into the client's Mods"
    (cd "$MOD_DIR" && "$SHAMWAY" client deploy .)
    for pair in "$PLAYTEST_ROOT/dist/7dtd-playtest:7dtd-playtest" "$CONNECT_ROOT/dist/7dtd-fastconnect:7dtd-fastconnect"; do
        src="${pair%%:*}"
        name="${pair##*:}"
        [[ -d "$src" ]] || die "missing deploy source: $src (build it in its own checkout)"
        # shellcheck disable=SC2016  # $0/$1/$2 are the inner shell's positionals, by design
        (cd "$MOD_DIR" && "$SHAMWAY" client hold --action "replace $name in the shared Mods folder" -- \
            bash -c 'rm -rf "${2:?}/$1" && cp -a "$0" "$2/$1"' "$src" "$name" "$MODS_DIR")
    done
fi

# --------------------------------------------------------- 2. in-game clip
STAMP="$(date -u +%Y%m%d-%H%M%S)"
RUN_DIR="$OUT_BASE/$STAMP"
mkdir -p "$RUN_DIR"

if [[ -n "$CLIP" ]]; then
    CLIP_INPUT="$CLIP"
    [[ -e "$CLIP_INPUT" ]] || die "--clip $CLIP_INPUT does not exist"
    say "reviewing existing clip: $CLIP_INPUT (capture skipped)"
else
    CLIENT_LOG="$COMPAT/pfx/drive_c/users/steamuser/AppData/Roaming/7DaysToDie/logs/output_log_client_7dtd_connect.txt"
    say "capturing the in-game turntable (suite $SUITE, clip $CLIP_ID)"
    PLAYTEST_CLIENT_LOG="$CLIENT_LOG" "$PLAYTEST_ROOT/scripts/capture_video.sh" \
        --suite "$SUITE" \
        --clip-id "$CLIP_ID" \
        --out "$RUN_DIR/capture" \
        --runner "$PLAYTEST_ROOT/scripts/playtest_run.py --server stock --game-srv $GAME_SRV --client-log $CLIENT_LOG --fresh-save --port 26900 --admin-port 8081 --session e2e-$STAMP --suite"
    CLIP_INPUT="$RUN_DIR/capture/$CLIP_ID.mp4"
    [[ -s "$CLIP_INPUT" ]] || die "no muxed video at $CLIP_INPUT; the capture failed"
fi

# -------------------------------------------------------- 3. review + gate
INTENT="${INTENT:-$MOD_DIR/thing.review.json}"
[[ -f "$INTENT" ]] || die "no intent file at $INTENT (pass --intent FILE or scaffold the fixture)"
EVIDENCE="$RUN_DIR/evidence.json"
CLIP_BYTES="$(stat -c %s "$CLIP_INPUT")"
say "submitting $CLIP_INPUT ($CLIP_BYTES bytes) to provider '$PROVIDER' -- billable, with --allow-network"
say "evidence will be written to $EVIDENCE"

ARGS=(review "$CLIP_INPUT" --intent "$INTENT" --provider "$PROVIDER")
[[ -n "$MODEL" ]] && ARGS+=(--model "$MODEL")
ARGS+=(--allow-network --json --output "$EVIDENCE")
deadeye "${ARGS[@]}" > "$RUN_DIR/evidence.stdout.json"
[[ -s "$EVIDENCE" ]] || die "review returned success but wrote no evidence"

# ------------------------------------------------------------- summary
python3 - "$EVIDENCE" "$CLIP_INPUT" <<'EOF'
import json, sys
evidence = json.load(open(sys.argv[1]))
result = evidence.get("result", {})
provider = evidence.get("provider") or {}
print()
print("E2E REVIEWED")
print(f"  clip       {sys.argv[2]}")
print(f"  provider   {provider.get('name', '?')} / {provider.get('model_reported', '?')}")
print(f"  review_id  {evidence.get('review_id', '?')}")
print(f"  verdict    {result.get('summary', '(no summary)')}")
print(f"  confidence {result.get('confidence', '?')}")
issues = result.get("issues") or []
print(f"  issues     {len(issues)}")
for issue in issues[:5]:
    print(f"    - {issue.get('description', '?')}")
print(f"  evidence   {sys.argv[1]}")
EOF
say "end-to-end test passed: in-game capture reviewed, evidence written to $EVIDENCE"
