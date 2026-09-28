"""The `deadeye` CLI end to end, offline through the fake provider."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from deadeye.cli import main


def _run(argv, capsys) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _review_argv(clip: Path, intent: Path | None, *flags: str) -> list[str]:
    """An offline review invocation; `flags` are what a test varies."""
    argv = ["review", str(clip), "--provider", "fake"]
    if intent is not None:
        argv += ["--intent", str(intent)]
    return argv + list(flags)


def test_review_requires_allow_network(clip_dir, minimal_intent, capsys) -> None:
    code, out, err = _run(_review_argv(clip_dir, minimal_intent, "--json"), capsys)
    assert code == 1
    assert err.startswith("ERROR:")
    assert "--allow-network" in err
    assert out == ""


def test_review_refuses_missing_intent(clip_dir, capsys) -> None:
    code, out, err = _run(_review_argv(clip_dir, None, "--allow-network", "--json"), capsys)
    assert code == 1
    assert out == ""
    assert "exactly one of --intent" in err


def test_an_io_fault_meets_the_one_error_line_contract(
    clip_dir, minimal_intent, capsys, monkeypatch
):
    """An unreadable clip or media file must refuse like every other failure
    (one ERROR line, exit 1), never an unhandled traceback."""
    from deadeye import cli

    def unreadable(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli, "run_review", unreadable)
    code, out, err = _run(
        _review_argv(clip_dir, minimal_intent, "--allow-network", "--json"),
        capsys,
    )
    assert code == 1
    assert out == ""
    assert err.startswith("ERROR:")
    assert "Permission denied" in err


def test_review_prints_the_envelope_with_json(clip_dir, minimal_intent, capsys) -> None:
    code, out, err = _run(
        _review_argv(clip_dir, minimal_intent, "--allow-network", "--json"),
        capsys,
    )
    assert code == 0
    envelope = json.loads(out)
    assert envelope["kind"] == "deadeye-review"
    assert envelope["provider"]["name"] == "fake"
    assert envelope["result"]["summary"]
    assert envelope["advisory_only"] is True
    # Disclosure went to stderr, keeping stdout machine-parseable.
    assert "warning: the media leaves this machine" in err


def test_review_human_summary_without_json(clip_dir, minimal_intent, capsys) -> None:
    code, out, _ = _run(_review_argv(clip_dir, minimal_intent, "--allow-network"), capsys)
    assert code == 0
    assert "summary:" in out
    assert "issues: 1" in out


def test_review_refuses_unknown_provider(clip_dir, minimal_intent, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "review",
                str(clip_dir),
                "--intent",
                str(minimal_intent),
                "--provider",
                "nope",
                "--allow-network",
            ]
        )
    assert exc_info.value.code == 2  # argparse choices refusal
    assert "invalid choice" in capsys.readouterr().err


def test_review_writes_evidence_with_output(clip_dir, minimal_intent, tmp_path, capsys) -> None:
    output = tmp_path / "evidence.json"
    code, out, _ = _run(
        _review_argv(
            clip_dir, minimal_intent, "--allow-network", "--json", "--output", str(output)
        ),
        capsys,
    )
    assert code == 0
    assert output.is_file()
    assert json.loads(out)["evidence"]["path"] == str(output)


def test_an_unwritable_output_path_is_named_in_the_refusal(
    clip_dir, minimal_intent, tmp_path, capsys, monkeypatch
) -> None:
    """A write failure at --output must refuse like every other failure, and
    the one ERROR line must name the evidence path instead of a bare errno,
    so the caller can tell which argument failed. The submission itself
    completed and was billed, so its verdict still reaches stdout: recovery
    never requires paying for the same media twice."""
    from deadeye import evidence

    def unwritable(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(evidence, "_atomic_write", unwritable)
    code, out, err = _run(
        _review_argv(
            clip_dir,
            minimal_intent,
            "--allow-network",
            "--json",
            "--output",
            str(tmp_path / "evidence.json"),
        ),
        capsys,
    )
    assert code == 1
    # The billed verdict rides the failure on the machine channel.
    envelope = json.loads(out)
    assert envelope["kind"] == "deadeye-review"
    assert envelope["result"]["summary"]
    # The refusal on stderr still names the evidence path and the reason,
    # never a bare errno.
    last = err.rstrip().splitlines()[-1]
    assert last.startswith("ERROR: cannot write evidence file ")
    assert "Permission denied" in last


def test_a_rerun_into_an_occupied_output_never_reaches_the_provider(
    clip_dir, minimal_intent, tmp_path, capsys, monkeypatch
) -> None:
    """The overwrite guard fires before anything is contacted, so obeying it
    costs no billable submission; nothing is printed to stdout either."""
    from deadeye.providers.fake import FakeProvider

    def unreachable(self, request):
        raise AssertionError("the provider must not be contacted for an occupied --output")

    monkeypatch.setattr(FakeProvider, "review", unreachable)
    output = tmp_path / "evidence.json"
    output.write_text("{}")
    code, out, err = _run(
        _review_argv(
            clip_dir, minimal_intent, "--allow-network", "--json", "--output", str(output)
        ),
        capsys,
    )
    assert code == 1
    assert out == ""
    assert "already holds an earlier review" in err


def test_doctor_reports_offline_state(capsys) -> None:
    code, out, _ = _run(["doctor", "--json"], capsys)
    assert code == 0
    states = json.loads(out)
    by_name = {state["name"]: state for state in states}
    assert set(by_name) == {"fake", "gemini", "nvidia"}
    assert by_name["fake"]["state"] == "configured"
    # gemini/nvidia's state depends on the host's env; what matters is that
    # doctor reports *something* without contacting any provider.
    assert by_name["gemini"]["state"] in ("configured", "unavailable")
    assert by_name["nvidia"]["state"] in ("configured", "unavailable")


def test_doctor_prints_the_effective_settings_without_crashing(capsys) -> None:
    """The effective knobs are inspectable even when configured badly."""
    code, out, _ = _run(["doctor"], capsys)
    assert code == 0
    assert "default_provider:" in out
    assert "timeout_seconds:" in out


def test_doctor_reports_an_unusable_default_provider(isolated_config, capsys) -> None:
    """A bad default_provider is surfaced by doctor, never crashes it."""
    (isolated_config / "config.toml").write_text('default_provider = "nvda"\n', encoding="utf-8")
    from deadeye import config

    config.reset()
    code, out, _ = _run(["doctor"], capsys)
    assert code == 0
    assert "default_provider: not usable" in out
    assert "nvda" in out


def test_doctor_never_attributes_a_key_to_the_keyless_provider(isolated_config, capsys) -> None:
    """A configured top-level api_key belongs to the real providers; the fake
    must still be reported as needing nothing, not as holding that key."""
    from deadeye import config

    (isolated_config / "config.local.toml").write_text('api_key = "nvapi-top"\n', encoding="utf-8")
    config.reset()
    code, out, _ = _run(["doctor"], capsys)
    assert code == 0
    err = capsys.readouterr().err
    assert err == ""
    fake_line = next(line for line in out.splitlines() if line.startswith("fake:"))
    assert fake_line == (
        "fake: configured (the fake provider needs no credentials; "
        "it exists for offline plumbing checks)"
    )
    # The real providers do report where their key came from, naming the
    # file that holds it (a key in the committed config.toml would be a
    # leak; config.local.toml is where it belongs).
    gemini_line = next(line for line in out.splitlines() if line.startswith("gemini:"))
    assert "key from configuration (top-level api_key, config.local.toml)" in gemini_line


def test_doctor_names_the_file_a_per_provider_key_came_from(isolated_config, capsys) -> None:
    """A per-provider key in the gitignored local file is reported with the
    file named, and the same key placed in the committed base file is
    reported as config.toml — a misplacement doctor must surface."""
    from deadeye import config

    local = isolated_config / "config.local.toml"
    local.write_text('[providers.gemini]\napi_key = "k-local"\n', encoding="utf-8")
    config.reset()
    code, out, _ = _run(["doctor"], capsys)
    assert code == 0
    gemini_line = next(line for line in out.splitlines() if line.startswith("gemini:"))
    assert "key from configuration (config.local.toml)" in gemini_line

    local.unlink()
    (isolated_config / "config.toml").write_text(
        '[providers.gemini]\napi_key = "k-committed"\n', encoding="utf-8"
    )
    config.reset()
    code, out, _ = _run(["doctor"], capsys)
    assert code == 0
    gemini_line = next(line for line in out.splitlines() if line.startswith("gemini:"))
    assert "key from configuration (config.toml)" in gemini_line


def test_schema_prints_the_contract(capsys) -> None:
    code, out, _ = _run(["schema"], capsys)
    assert code == 0
    schema = json.loads(out)
    assert schema["intent"]["required"] == ["purpose"]
    assert "summary" in schema["result"]["keys"]
    assert "clipping_risk" in schema["result"]["dimensions"]


def test_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("deadeye ")


def test_prompt_renders_the_injected_prompt_from_an_intent(tmp_path, capsys) -> None:
    import json as _json

    intent = tmp_path / "i.json"
    intent.write_text(
        _json.dumps({"purpose": "show the garment survives a turn"}), encoding="utf-8"
    )
    code, out, _ = _run(["prompt", "--intent", str(intent)], capsys)
    assert code == 0
    assert "You are reviewing a game-asset candidate on screen." in out
    assert "purpose: show the garment survives a turn" in out
    assert "popping_risk" in out
    assert '"summary": string' in out


def test_prompt_derives_the_media_summary_from_a_clip(clip_dir, tmp_path, capsys) -> None:
    import json as _json

    intent = tmp_path / "i.json"
    intent.write_text(_json.dumps({"purpose": "p"}), encoding="utf-8")
    code, out, _ = _run(["prompt", "--intent", str(intent), "--clip", str(clip_dir)], capsys)
    assert code == 0
    assert "10 frame image(s) of the clip's 10 frames" in out


def test_prompt_requires_an_intent(capsys) -> None:
    code, _, err = _run(["prompt"], capsys)
    assert code == 1
    assert "exactly one of --intent" in err


def test_python_dash_m_honors_the_exit_contract(tmp_path: Path) -> None:
    """`python -m deadeye` must propagate main()'s exit code, not swallow it:
    a script driving the module form reads the same contract as the console
    script (0 success, 1 refusal with one ERROR line on stderr)."""
    ok = subprocess.run(
        [sys.executable, "-m", "deadeye", "schema"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok.returncode == 0
    # Exit code alone proves nothing: a `schema` that printed nothing, or
    # printed a traceback, would still exit 0. The contract is the document.
    schema = json.loads(ok.stdout)
    assert set(schema) == {"intent", "result"}
    assert schema["result"]["keys"]

    missing = tmp_path / "no-such-clip"
    failed = subprocess.run(
        [
            sys.executable,
            "-m",
            "deadeye",
            "review",
            str(missing),
            "--provider",
            "fake",
            "--allow-network",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert failed.stderr.startswith("ERROR:")
    assert failed.stdout == ""


def test_an_interrupt_exits_130_without_a_traceback(clip_dir, minimal_intent, capsys, monkeypatch):
    """Ctrl+C mid-review exits the way a SIGINT-killed process would (130)
    with one stderr line, never an unhandled traceback."""
    from deadeye import cli

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_review", interrupted)
    code, out, err = _run(_review_argv(clip_dir, minimal_intent), capsys)
    assert code == 130
    assert out == ""
    assert err == "ERROR: interrupted\n"


def test_a_closed_stdout_pipe_exits_141(clip_dir, minimal_intent, capsys, monkeypatch):
    """A downstream reader closing the pipe on stdout (`... | head`) exits
    with the conventional SIGPIPE status instead of a shutdown-flush
    traceback and an unrelated exit code."""
    from deadeye import cli

    def pipe_closed(*args, **kwargs):
        raise BrokenPipeError

    monkeypatch.setattr(cli, "run_review", pipe_closed)
    code = cli.main(_review_argv(clip_dir, minimal_intent, "--allow-network", "--json"))
    captured = capsys.readouterr()
    assert code == 141
    assert captured.err == ""


def test_output_survives_an_ascii_bound_stdout(tmp_path: Path) -> None:
    """A review's prose and a filename's are not ASCII by construction, and
    Python binds stdout to the locale's encoding. Under C or POSIX (cron, a
    systemd unit, a CI job with no LANG) that is ASCII, and one non-ASCII
    character used to raise UnicodeEncodeError inside `print` after the
    submission had already been billed and the verdict validated: the caller
    lost exactly the result it paid for. The run must print the text and
    still exit 0."""
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
    }
    # The non-ASCII travels in a file, not in argv: under this locale the
    # parent's own argv encoding is ASCII, and a test that fails there would
    # be testing the harness rather than the tool.
    intent = tmp_path / "intent.json"
    intent.write_text('{"purpose": "中文字幕の品質"}', encoding="utf-8")
    shown = subprocess.run(
        [sys.executable, "-m", "deadeye", "prompt", "--intent", str(intent)],
        capture_output=True,
        env=env,
        check=False,
    )
    assert shown.returncode == 0, shown.stderr.decode("utf-8", "replace")
    assert "中文字幕の品質" in shown.stdout.decode("utf-8")


def test_an_unencodable_character_never_dies_in_print(tmp_path: Path) -> None:
    """A provider response may legally carry an unpaired surrogate
    (RFC 8259 section 8.2) and Python's JSON parser accepts it. The verdict
    must still reach stdout: an unencodable character is rendered as an
    escape, not raised."""
    from deadeye._streams import bind_utf8_output

    bound = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    bind_utf8_output(bound)
    assert bound.encoding == "utf-8"
    bound.write("lone: \ud800\n")
    bound.flush()
    assert b"lone: \\ud800" in bound.buffer.getvalue()
