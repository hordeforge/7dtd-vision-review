"""The `deadeye` CLI end to end, offline through the fake provider."""

from __future__ import annotations

import json

import pytest

from deadeye.cli import main

MINIMAL_INTENT = '{"purpose": "show the asset in motion"}'


def _run(argv, capsys) -> int:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_review_requires_allow_network(clip_dir, tmp_path, capsys) -> None:
    intent = tmp_path / "i.json"
    intent.write_text(MINIMAL_INTENT)
    code, out, err = _run(
        ["review", str(clip_dir), "--intent", str(intent), "--provider", "fake", "--json"],
        capsys,
    )
    assert code == 1
    assert err.startswith("ERROR:")
    assert "--allow-network" in err
    assert out == ""


def test_review_refuses_missing_intent(clip_dir, capsys) -> None:
    code, out, err = _run(
        ["review", str(clip_dir), "--provider", "fake", "--allow-network", "--json"],
        capsys,
    )
    assert code == 1
    assert out == ""
    assert "exactly one of --intent" in err


def test_review_prints_the_envelope_with_json(clip_dir, tmp_path, capsys) -> None:
    intent = tmp_path / "i.json"
    intent.write_text(MINIMAL_INTENT)
    code, out, err = _run(
        [
            "review",
            str(clip_dir),
            "--intent",
            str(intent),
            "--provider",
            "fake",
            "--allow-network",
            "--json",
        ],
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


def test_review_human_summary_without_json(clip_dir, tmp_path, capsys) -> None:
    intent = tmp_path / "i.json"
    intent.write_text(MINIMAL_INTENT)
    code, out, _ = _run(
        ["review", str(clip_dir), "--intent", str(intent), "--provider", "fake", "--allow-network"],
        capsys,
    )
    assert code == 0
    assert "summary:" in out
    assert "issues: 1" in out


def test_review_refuses_unknown_provider(clip_dir, tmp_path, capsys) -> None:

    intent = tmp_path / "i.json"
    intent.write_text(MINIMAL_INTENT)
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "review",
                str(clip_dir),
                "--intent",
                str(intent),
                "--provider",
                "nope",
                "--allow-network",
            ]
        )
    assert exc_info.value.code == 2  # argparse choices refusal
    assert "invalid choice" in capsys.readouterr().err


def test_review_writes_evidence_with_output(clip_dir, tmp_path, capsys) -> None:
    intent = tmp_path / "i.json"
    intent.write_text(MINIMAL_INTENT)
    output = tmp_path / "evidence.json"
    code, out, _ = _run(
        [
            "review",
            str(clip_dir),
            "--intent",
            str(intent),
            "--provider",
            "fake",
            "--allow-network",
            "--json",
            "--output",
            str(output),
        ],
        capsys,
    )
    assert code == 0
    assert output.is_file()
    assert json.loads(out)["evidence"]["path"] == str(output)


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


def test_prompt_renders_the_injected_prompt_from_an_intent(tmp_path, capsys) -> None:  # noqa: ANN001
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


def test_prompt_derives_the_media_summary_from_a_clip(clip_dir, tmp_path, capsys) -> None:  # noqa: ANN001
    import json as _json

    intent = tmp_path / "i.json"
    intent.write_text(_json.dumps({"purpose": "p"}), encoding="utf-8")
    code, out, _ = _run(["prompt", "--intent", str(intent), "--clip", str(clip_dir)], capsys)
    assert code == 0
    assert "10 frame image(s) of the clip's 10 frames" in out


def test_prompt_requires_an_intent(capsys) -> None:  # noqa: ANN001
    code, _, err = _run(["prompt"], capsys)
    assert code == 1
    assert "exactly one of --intent" in err
