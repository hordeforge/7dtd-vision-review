"""Config loading: base + gitignored local, precedence, discovery.

The committed `config.toml` in the repo root is deliberately excluded from
these tests: every case pins a controlled `DEADEYE_CONFIG_DIR` and resets the
process-wide cache, so the checkout's own file cannot leak into assertions.
"""

from __future__ import annotations

import ast
import json
from importlib import import_module
from pathlib import Path

import pytest

from deadeye import config
from deadeye.errors import DeadeyeError
from deadeye.providers.base import MediaPayload, ReviewRequest
from deadeye.providers.nvidia import build_body
from deadeye.surface import resolve_provider, resolve_timeout

pytestmark = pytest.mark.usefixtures("isolated_config")


def _write(directory, name: str, body: str) -> None:
    (directory / name).write_text(body, encoding="utf-8")


def test_no_config_anywhere_is_empty() -> None:
    assert config.load().directory is None
    assert config.value(("default_provider",)) is None
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) is None


def test_base_and_local_merge_with_local_winning(isolated_config, monkeypatch) -> None:
    _write(
        isolated_config,
        "config.toml",
        '[providers.nvidia]\nmodel = "a-model"\nmax_tokens = 1000\n',
    )
    _write(
        isolated_config,
        "config.local.toml",
        '[providers.nvidia]\napi_key = "nvapi-local"\nmax_tokens = 2000\n',
    )
    loaded = config.load()
    assert loaded.sources() == [
        isolated_config / "config.toml",
        isolated_config / "config.local.toml",
    ]
    # Nested merge: local's api_key joins base's model instead of replacing
    # the whole [providers.nvidia] table.
    assert config.value(("providers", "nvidia", "model")) == "a-model"
    assert config.value(("providers", "nvidia", "api_key")) == "nvapi-local"
    assert config.value(("providers", "nvidia", "max_tokens")) == 2000


def test_each_value_remembers_which_file_supplied_it(isolated_config) -> None:
    _write(
        isolated_config,
        "config.toml",
        '[providers.nvidia]\nmodel = "a-model"\nmax_tokens = 1000\n',
    )
    _write(
        isolated_config,
        "config.local.toml",
        '[providers.nvidia]\napi_key = "nvapi-local"\nmax_tokens = 2000\n',
    )
    loaded = config.load()
    # A leaf only the committed file sets is attributed to config.toml; a
    # leaf the gitignored file supplies or overrides is attributed to
    # config.local.toml; an unset key has no origin.
    assert loaded.provenance(("providers", "nvidia", "model")) == "config.toml"
    assert loaded.provenance(("providers", "nvidia", "api_key")) == "config.local.toml"
    assert loaded.provenance(("providers", "nvidia", "max_tokens")) == "config.local.toml"
    assert loaded.provenance(("providers", "nvidia", "temperature")) is None
    assert config.provenance(("providers", "nvidia", "api_key")) == "config.local.toml"


def test_local_without_base_is_fine(isolated_config) -> None:
    _write(isolated_config, "config.local.toml", 'api_key = "nvapi-top"\n')
    assert config.value(("api_key",)) == "nvapi-top"


def test_environment_wins_over_local(isolated_config, monkeypatch) -> None:
    _write(isolated_config, "config.local.toml", '[providers.nvidia]\napi_key = "from-local"\n')
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "from-local"
    monkeypatch.setenv("NVIDIA_API_KEY", "from-env")
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "from-env"


def test_top_level_api_key_is_a_per_provider_fallback(isolated_config) -> None:
    _write(isolated_config, "config.local.toml", 'api_key = "nvapi-top"\n')
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "nvapi-top"
    _write(
        isolated_config,
        "config.local.toml",
        'api_key = "nvapi-top"\n[providers.nvidia]\napi_key = "nvapi-specific"\n',
    )
    config.reset()
    assert config.credential_for("nvidia", ("NVIDIA_API_KEY",)) == "nvapi-specific"


def test_a_malformed_config_fails_loudly_and_doctor_reports_it(isolated_config, capsys) -> None:
    _write(isolated_config, "config.toml", "this is not [ toml\n")
    with pytest.raises(ValueError, match="cannot read config file"):
        config.load()
    from deadeye.cli import main

    assert main(["doctor"]) == 0
    assert "config error:" in capsys.readouterr().out


def test_doctor_json_reports_a_malformed_config_on_stderr(isolated_config, capsys) -> None:
    """The machine form must not read a broken config as 'no credential'.

    A failed parse makes every provider `unavailable`, which in the array on
    stdout is byte-identical to a missing API key. The fault has to ride
    stderr under the usual `ERROR:` prefix, or a script's operator hunts for a
    credential that was never the problem.
    """
    _write(isolated_config, "config.toml", "this is not [ toml\n")
    from deadeye.cli import main

    assert main(["doctor", "--json"]) == 0
    captured = capsys.readouterr()
    states = json.loads(captured.out)
    assert {state["name"] for state in states} == {"fake", "gemini", "nvidia"}
    assert captured.err.startswith("ERROR: cannot read config file")


def test_a_review_over_a_broken_config_names_the_file_not_the_credential(
    isolated_config, tmp_path
) -> None:
    """The submission path must not read a broken config as 'no credential':
    that would send the operator hunting for an API key while the real fault
    is one bad line of TOML."""
    from deadeye.providers.fake import FakeProvider
    from deadeye.review import run_review

    _write(isolated_config, "config.toml", "this is not [ toml\n")
    clip = tmp_path / "clip"
    clip.mkdir()
    (clip / "frame-0000.png").write_bytes(b"x")
    intent = tmp_path / "i.json"
    intent.write_text('{"purpose": "p"}', encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read config file"):
        run_review(clip, provider=FakeProvider(), intent_path=intent, allow_network=True)


def test_default_provider_comes_from_config(isolated_config) -> None:
    _write(isolated_config, "config.toml", 'default_provider = "nvidia"\n')
    assert resolve_provider(None) == "nvidia"
    assert resolve_provider("fake") == "fake"


def test_unknown_default_provider_is_refused_not_silently_swapped(isolated_config) -> None:
    """A typo'd provider must not quietly send billable reviews to the default."""
    _write(isolated_config, "config.toml", 'default_provider = "nvda"\n')
    with pytest.raises(DeadeyeError, match="nvda"):
        resolve_provider(None)


def test_non_string_default_provider_is_refused(isolated_config) -> None:
    _write(isolated_config, "config.toml", "default_provider = 3\n")
    with pytest.raises(DeadeyeError, match="default_provider"):
        resolve_provider(None)


def test_timeout_resolution_flag_over_config_over_default(isolated_config) -> None:
    _write(isolated_config, "config.toml", "timeout_seconds = 30\n")
    assert resolve_timeout(None) == 30.0
    assert resolve_timeout(5) == 5.0
    # An explicitly empty config falls back to the built-in default.
    config.reset()
    _write(isolated_config, "config.toml", "")
    config.reset()
    assert resolve_timeout(None) == 120.0


def test_timeout_refuses_unusable_values_instead_of_failing_late(isolated_config) -> None:
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(DeadeyeError, match="positive number of seconds"):
            resolve_timeout(bad)
    _write(isolated_config, "config.toml", 'timeout_seconds = "120"\n')
    with pytest.raises(DeadeyeError, match="positive number of seconds"):
        resolve_timeout(None)


def test_endpoint_override_unset_or_non_string_reads_as_fallback(isolated_config) -> None:
    fallback = "https://fallback.example/v1"
    assert config.endpoint(("providers", "nvidia", "endpoint"), fallback) == fallback
    _write(isolated_config, "config.toml", "[providers.nvidia]\nendpoint = 7\n")
    assert config.endpoint(("providers", "nvidia", "endpoint"), fallback) == fallback


def test_endpoint_override_accepts_https_and_loopback_http(isolated_config) -> None:
    _write(
        isolated_config,
        "config.toml",
        '[providers.nvidia]\nendpoint = "https://proxy.internal/v1"\n',
    )
    assert (
        config.endpoint(("providers", "nvidia", "endpoint"), "https://fallback")
        == "https://proxy.internal/v1"
    )
    config.reset()
    _write(
        isolated_config, "config.toml", '[providers.nvidia]\nendpoint = "http://localhost:8080"\n'
    )
    assert (
        config.endpoint(("providers", "nvidia", "endpoint"), "https://fallback")
        == "http://localhost:8080"
    )


def test_endpoint_override_refuses_remote_plaintext_before_any_submission(isolated_config) -> None:
    _write(
        isolated_config,
        "config.toml",
        '[providers.nvidia]\nendpoint = "http://proxy.example.com/v1"\n',
    )
    with pytest.raises(DeadeyeError, match="https"):
        config.endpoint(("providers", "nvidia", "endpoint"), "https://fallback")


def test_xdg_config_home_is_the_home_fallback_when_set(tmp_path, monkeypatch) -> None:
    """Installed-tool discovery follows XDG_CONFIG_HOME instead of hardcoding
    ~/.config, so a Linux host that relocated its config dir is still found."""
    config.reset()
    monkeypatch.delenv("DEADEYE_CONFIG_DIR", raising=False)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    xdg = tmp_path / "xdg"
    dest = xdg / "deadeye"
    dest.mkdir(parents=True)
    (dest / "config.toml").write_text('default_provider = "fake"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    loaded = config.load()
    assert loaded.directory == dest
    assert config.value(("default_provider",)) == "fake"


def test_home_dot_config_is_the_fallback_when_xdg_is_unset(tmp_path, monkeypatch) -> None:
    config.reset()
    monkeypatch.delenv("DEADEYE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setattr(config.sys, "platform", "linux")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    home = tmp_path / "home"
    dest = home / ".config" / "deadeye"
    dest.mkdir(parents=True)
    (dest / "config.toml").write_text("timeout_seconds = 9\n", encoding="utf-8")
    monkeypatch.setattr(config.Path, "home", lambda *args: home)
    loaded = config.load()
    assert loaded.directory == dest
    assert config.value(("timeout_seconds",)) == 9


def test_macos_falls_back_to_application_support_not_dot_config(tmp_path, monkeypatch) -> None:
    """macOS keeps per-user configuration under Application Support, and it
    never sets XDG_CONFIG_HOME, so the Linux fallback would hide the config in
    a dotfile directory macOS itself does not read."""
    config.reset()
    monkeypatch.delenv("DEADEYE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    monkeypatch.setattr(config.sys, "platform", "darwin")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    home = tmp_path / "home"
    dest = home / "Library" / "Application Support" / "deadeye"
    dest.mkdir(parents=True)
    (dest / "config.toml").write_text("timeout_seconds = 11\n", encoding="utf-8")
    monkeypatch.setattr(config.Path, "home", lambda *args: home)
    loaded = config.load()
    assert loaded.directory == dest
    assert config.value(("timeout_seconds",)) == 11


def test_macos_still_honors_an_explicit_xdg_config_home(tmp_path, monkeypatch) -> None:
    """The variable is the override on every platform: a macOS user who exports
    it must not have their config relocated by the platform fallback."""
    config.reset()
    monkeypatch.delenv("DEADEYE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(config.sys, "platform", "darwin")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    home = tmp_path / "home"
    monkeypatch.setattr(config.Path, "home", lambda *args: home)
    xdg = tmp_path / "xdg"
    dest = xdg / "deadeye"
    dest.mkdir(parents=True)
    (dest / "config.toml").write_text("timeout_seconds = 13\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    loaded = config.load()
    assert loaded.directory == dest
    assert config.value(("timeout_seconds",)) == 13


def test_explicit_config_dir_without_files_is_reported_not_silent(
    isolated_config, monkeypatch
) -> None:
    empty = isolated_config / "empty"
    empty.mkdir()
    monkeypatch.setenv("DEADEYE_CONFIG_DIR", str(empty))
    config.reset()
    assert config.load().directory is None
    note = config.discovery_note()
    assert note is not None and "DEADEYE_CONFIG_DIR" in note


def test_nvidia_generation_params_flow_from_config(isolated_config) -> None:
    _write(
        isolated_config,
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


def test_non_finite_float_knobs_are_refused_not_silently_defaulted(isolated_config) -> None:
    """TOML spells them `nan`, `inf`, and `-inf`; passed through they would
    reach the request body as bare `NaN`/`Infinity` tokens no provider-side
    JSON reader accepts. Refusing with the key named beats sending a
    silently different parameter than the one configured."""
    _write(
        isolated_config,
        "config.toml",
        "[providers.nvidia]\ntemperature = nan\ntop_p = inf\n",
    )
    frame = MediaPayload(name="f.png", mime_type="image/png", kind="frame", data=b"x")
    request = ReviewRequest(prompt="p", media=(frame,), model="m", timeout_seconds=1.0)
    with pytest.raises(DeadeyeError, match=r"providers\.nvidia\.temperature.*nan"):
        build_body(request)


def test_wrong_typed_generation_knobs_are_refused_not_silently_defaulted(isolated_config) -> None:
    """A present-but-unusable value must not quietly become the built-in
    default: the submission would differ from the configuration on record."""
    _write(
        isolated_config,
        "config.toml",
        '[providers.nvidia]\nmax_tokens = "65536"\nreasoning_budget = false\n',
    )
    frame = MediaPayload(name="f.png", mime_type="image/png", kind="frame", data=b"x")
    request = ReviewRequest(prompt="p", media=(frame,), model="m", timeout_seconds=1.0)
    with pytest.raises(DeadeyeError, match=r"providers\.nvidia\.max_tokens"):
        build_body(request)
    config.reset()
    _write(isolated_config, "config.toml", "[providers.nvidia]\nreasoning_budget = false\n")
    with pytest.raises(DeadeyeError, match=r"providers\.nvidia\.reasoning_budget"):
        build_body(request)
    # The gemini adapter reads through the same validated readers.
    from deadeye.providers.gemini import GeminiProvider

    config.reset()
    _write(
        isolated_config,
        "config.local.toml",
        "[providers.gemini]\napi_key = \"test\"\nmax_output_tokens = 'high'\n",
    )
    with pytest.raises(DeadeyeError, match=r"providers\.gemini\.max_output_tokens"):
        GeminiProvider().review(request)


def test_doctor_reports_an_unusable_endpoint_override(isolated_config, capsys) -> None:
    """A bad endpoint override surfaces at diagnosis time, before any review;
    doctor stays offline and never crashes over it."""
    from deadeye.cli import main

    _write(
        isolated_config,
        "config.toml",
        '[providers.nvidia]\nendpoint = "http://proxy.example.com/v1"\n',
    )
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "endpoint:" in out
    assert "providers.nvidia.endpoint" in out
    assert "'http://proxy.example.com/v1'" in out


def test_provider_credential_reads_config_local(isolated_config) -> None:
    _write(isolated_config, "config.local.toml", '[providers.nvidia]\napi_key = "nvapi-local"\n')
    from deadeye.providers.nvidia import NvidiaProvider

    provider = NvidiaProvider()
    assert provider.is_configured()
    assert provider.credential() == "nvapi-local"


def test_default_model_precedence(isolated_config, tmp_path) -> None:
    """--model flag > top-level default_model > provider's own default."""
    from deadeye.providers.fake import FakeProvider
    from deadeye.providers.gemini import GeminiProvider
    from deadeye.review import run_review

    _write(
        isolated_config,
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
        isolated_config,
        "config.toml",
        '[providers.gemini]\nmodel = "per-provider-model"\n',
    )
    assert GeminiProvider().default_model == "per-provider-model"

    # No config default at all: the provider's built-in default applies.
    config.reset()
    _write(isolated_config, "config.toml", "")
    provider = FakeProvider()
    run_review(clip, provider=provider, intent_path=intent, allow_network=True)
    assert provider.requests[-1].model == "deadeye-fake-vision-v1"


def test_a_misspelled_key_is_refused_by_name_instead_of_ignored(isolated_config) -> None:
    """A name deadeye does not read must not leave the built-in default in
    force while its author believes the file was honored."""
    _write(isolated_config, "config.toml", 'default_provder = "nvidia"\n')
    with pytest.raises(ValueError, match=r"default_provder"):
        config.load()
    # The fail-soft readers read as unset, and doctor names the fault.
    assert config.value(("default_provder",)) is None
    from deadeye.cli import main

    assert main(["doctor"]) == 0


def test_a_misspelled_provider_or_knob_is_refused_by_name(isolated_config) -> None:
    _write(isolated_config, "config.local.toml", '[providers.geminie]\napi_key = "k"\n')
    with pytest.raises(ValueError, match=r"providers\.geminie"):
        config.load()
    config.reset()
    _write(isolated_config, "config.local.toml", "[providers.nvidia]\nmax_token = 4096\n")
    with pytest.raises(ValueError, match=r"providers\.nvidia\.max_token"):
        config.load()
    assert "max_tokens" in config.PROVIDER_KEYS["nvidia"]


def test_documented_settings_load_and_an_empty_table_is_not_a_setting(isolated_config) -> None:
    """Every documented key, plus the empty `[providers.fake]` a reader may
    write, must load: the refusal above must not reject real configuration."""
    _write(
        isolated_config,
        "config.toml",
        'default_provider = "nvidia"\ndefault_model = "m"\ntimeout_seconds = 30\n'
        'api_key = "top"\n\n[providers.fake]\n\n[providers.gemini]\n'
        'model = "g"\napi_key = "g"\nmax_output_tokens = 1024\n'
        'endpoint = "https://example.test/v1"\n\n[providers.nvidia]\n'
        'model = "n"\ntemperature = 0.1\ntop_p = 0.5\nreasoning_budget = 8\n',
    )
    loaded = config.load()
    assert loaded.value(("default_provider",)) == "nvidia"
    assert loaded.value(("providers", "gemini", "max_output_tokens")) == 1024


def test_the_key_schema_covers_the_registry_and_every_key_the_adapters_read() -> None:
    """Drift guard: an adapter reading a key the schema omits would have that
    key refused as unreadable, so the schema must track the code."""
    from deadeye.surface import PROVIDERS

    assert set(config.PROVIDER_KEYS) == set(PROVIDERS)
    for name in sorted(PROVIDERS):
        known = config.PROVIDER_KEYS[name]
        for key in _keys_read_by(import_module(f"deadeye.providers.{name}"), name):
            assert key in known, f"providers.{name}.{key} is read but not in the schema"


def _keys_read_by(module: object, provider: str) -> set[str]:
    """Per-provider config keys an adapter module reads, from its own source.

    The two shapes an adapter uses: a `config.<reader>(("providers", name,
    key))` key path, and the `int_setting`/`float_setting` knob readers that
    take the key as an argument. The knob readers arrive as bare names
    (`int_setting(...)`, imported from `base`), not as attributes, so the
    name is matched as well: matching only the attribute form would find no
    knob at all and let the schema drift unnoticed.
    """
    readers = {"value", "text", "endpoint", "endpoint_problem"}
    knobs = {"int_setting", "float_setting"}
    source = Path(str(module.__file__))
    keys: set[str] = set()
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called = node.func.id
        elif isinstance(node.func, ast.Attribute):
            called = node.func.attr
        else:
            continue
        if called in readers and node.args:
            path = node.args[0]
            parts = list(path.elts) if isinstance(path, ast.Tuple) else []
            strings = [part.value for part in parts if isinstance(part, ast.Constant)]
            if len(parts) == 3 and len(strings) == 3 and strings[:2] == ["providers", provider]:
                keys.add(strings[2])
        elif called in knobs and len(node.args) > 1:
            key = node.args[1]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
    return keys


def test_a_config_edited_after_load_is_picked_up_without_a_restart(isolated_config) -> None:
    """The MCP server is long-lived and config files are written outside it,
    so a cached config must not outlive the file it was built from."""
    _write(isolated_config, "config.toml", 'default_provider = "nvidia"\n')
    assert config.value(("default_provider",)) == "nvidia"
    _write(isolated_config, "config.toml", 'default_provider = "gemini"\n')
    assert config.value(("default_provider",)) == "gemini"
    # A file that appears after the first load is a write path too: a
    # credential dropped into the gitignored local file must be read.
    assert config.value(("api_key",)) is None
    _write(isolated_config, "config.local.toml", 'api_key = "nvapi-top"\n')
    assert config.value(("api_key",)) == "nvapi-top"
    assert config.load().provenance(("api_key",)) == "config.local.toml"


def test_a_fixed_config_recovers_from_a_cached_parse_failure(isolated_config) -> None:
    """The failure is cached so a broken file is named once, not re-read on
    every call; correcting it must still take effect in the same process."""
    _write(isolated_config, "config.toml", 'default_provder = "nvidia"\n')
    with pytest.raises(ValueError, match=r"default_provder"):
        config.load()
    with pytest.raises(ValueError, match=r"default_provder"):
        config.load()
    _write(isolated_config, "config.toml", 'default_provider = "nvidia"\n')
    assert config.value(("default_provider",)) == "nvidia"
    assert config.load_failure() is None


def test_unchanged_config_is_not_reparsed(isolated_config) -> None:
    """The cache still caches: an unchanged source file costs a stat, not a
    fresh `Config` and a fresh identity every call."""
    _write(isolated_config, "config.toml", 'default_provider = "nvidia"\n')
    assert config.load() is config.load()
