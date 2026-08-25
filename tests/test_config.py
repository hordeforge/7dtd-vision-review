"""Config loading: base + gitignored local, precedence, discovery.

The committed `config.toml` in the repo root is deliberately excluded from
these tests: every case pins a controlled `DEADEYE_CONFIG_DIR` and resets the
process-wide cache, so the checkout's own file cannot leak into assertions.
"""

from __future__ import annotations

import pytest

from deadeye import config
from deadeye.cli import _resolve_provider
from deadeye.providers.nvidia import build_body
from deadeye.providers.base import ReviewRequest
from deadeye.providers.base import MediaPayload


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    """Every test gets a clean cache and its own config directory."""
    config.reset()
    directory = tmp_path / "cfg"
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv("DEADEYE_CONFIG_DIR", str(directory))
    yield directory
    config.reset()


def _write(directory, name: str, body: str) -> None:  # noqa: ANN001
    (directory / name).write_text(body, encoding="utf-8")


def test_no_config_anywhere_is_empty() -> None:
    assert config.load().directory is None
    assert config.value("default_provider") is None
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) is None


def test_base_and_local_merge_with_local_winning(_isolated_config, monkeypatch) -> None:  # noqa: ANN001
    _write(
        _isolated_config,
        "config.toml",
        '[providers.nvidia]\nmodel = "a-model"\nmax_tokens = 1000\n',
    )
    _write(
        _isolated_config,
        "config.local.toml",
        '[providers.nvidia]\napi_key = "nvapi-local"\nmax_tokens = 2000\n',
    )
    loaded = config.load()
    assert loaded.sources() == [
        _isolated_config / "config.toml",
        _isolated_config / "config.local.toml",
    ]
    # Nested merge: local's api_key joins base's model instead of replacing
    # the whole [providers.nvidia] table.
    assert config.value(("providers", "nvidia", "model")) == "a-model"
    assert config.value(("providers", "nvidia", "api_key")) == "nvapi-local"
    assert config.value(("providers", "nvidia", "max_tokens")) == 2000


def test_local_without_base_is_fine(_isolated_config) -> None:  # noqa: ANN001
    _write(_isolated_config, "config.local.toml", 'api_key = "nvapi-top"\n')
    assert config.value("api_key") == "nvapi-top"


def test_environment_wins_over_local(_isolated_config, monkeypatch) -> None:  # noqa: ANN001
    _write(_isolated_config, "config.local.toml", '[providers.nvidia]\napi_key = "from-local"\n')
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "from-local"
    monkeypatch.setenv("NVIDIA_API_KEY", "from-env")
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "from-env"


def test_top_level_api_key_is_a_per_provider_fallback(_isolated_config) -> None:  # noqa: ANN001
    _write(_isolated_config, "config.local.toml", 'api_key = "nvapi-top"\n')
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "nvapi-top"
    _write(
        _isolated_config,
        "config.local.toml",
        'api_key = "nvapi-top"\n[providers.nvidia]\napi_key = "nvapi-specific"\n',
    )
    config.reset()
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "nvapi-specific"


def test_a_malformed_config_fails_loudly_and_doctor_reports_it(_isolated_config, capsys) -> None:  # noqa: ANN001
    _write(_isolated_config, "config.toml", "this is not [ toml\n")
    with pytest.raises(ValueError, match="cannot read config file"):
        config.load()
    from deadeye.cli import main

    assert main(["doctor"]) == 0
    assert "config error:" in capsys.readouterr().out


def test_default_provider_comes_from_config(_isolated_config) -> None:  # noqa: ANN001
    _write(_isolated_config, "config.toml", 'default_provider = "nvidia"\n')
    assert _resolve_provider(None) == "nvidia"
    assert _resolve_provider("fake") == "fake"


def test_nvidia_generation_params_flow_from_config(_isolated_config) -> None:  # noqa: ANN001
    _write(
        _isolated_config,
        "config.toml",
        "[providers.nvidia]\nmax_tokens = 1234\nreasoning_budget = 567\ntemperature = 0.2\n",
    )
    frame = MediaPayload(name="f.png", mime_type="image/png", kind="frame", data=b"x")
    body = build_body(ReviewRequest(prompt="p", media=(frame,), model="m", timeout_seconds=1.0))
    assert body["max_tokens"] == 1234
    assert body["reasoning_budget"] == 567
    assert body["temperature"] == 0.2
    # Unset keys keep the built-in defaults.
    assert body["top_p"] == 0.95


def test_provider_credential_reads_config_local(_isolated_config) -> None:  # noqa: ANN001
    _write(_isolated_config, "config.local.toml", '[providers.nvidia]\napi_key = "nvapi-local"\n')
    from deadeye.providers.nvidia import NvidiaProvider

    provider = NvidiaProvider()
    assert provider.is_configured()
    assert provider.credential() == "nvapi-local"


def test_default_model_precedence(_isolated_config, tmp_path) -> None:  # noqa: ANN001
    """--model flag > top-level default_model > provider's own default."""
    from deadeye.providers.fake import FakeProvider
    from deadeye.providers.gemini import GeminiProvider
    from deadeye.review import run_review

    _write(
        _isolated_config,
        "config.toml",
        'default_model = "top-level-model"\n[providers.gemini]\nmodel = "per-provider-model"\n',
    )
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    intent = tmp_path / "i.json"
    intent.write_text('{"purpose": "p"}', encoding="utf-8")

    # Top-level default_model beats the provider's own default.
    provider = FakeProvider()
    run_review(clip, provider=provider, intent_path=intent, allow_network=True)
    assert provider.requests[-1].model == "top-level-model"

    # The --model flag beats the config default.
    provider = FakeProvider()
    run_review(clip, provider=provider, intent_path=intent, allow_network=True, model="flag-model")
    assert provider.requests[-1].model == "flag-model"

    # Per-provider model is what a config-aware provider defaults to when no
    # top-level default_model is set.
    config.reset()
    _write(
        _isolated_config,
        "config.toml",
        '[providers.gemini]\nmodel = "per-provider-model"\n',
    )
    assert GeminiProvider().default_model == "per-provider-model"

    # No config default at all: the provider's built-in default applies.
    config.reset()
    _write(_isolated_config, "config.toml", "")
    provider = FakeProvider()
    run_review(clip, provider=provider, intent_path=intent, allow_network=True)
    assert provider.requests[-1].model == "deadeye-fake-vision-v1"
