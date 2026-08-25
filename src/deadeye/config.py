"""Configuration loading: `config.toml` (committed) + `config.local.toml`.

Mirrors the sibling llm-proxy convention: two TOML files in one directory,
loaded in order, the local file winning on conflict. The local file is
gitignored, which is where an API key goes instead of an `export` on every
shell.

Precedence, from strongest to weakest:

    CLI flags > environment variables > config.local.toml > config.toml
    > built-in defaults

Discovery (the first directory holding any config file wins, so a checkout
that carries one shadows the home one):

1. `DEADEYE_CONFIG_DIR` — an explicit directory.
2. The current working directory (`./config.toml`, `./config.local.toml`).
3. `~/.config/deadeye/` — the home fallback for an installed tool.

Only the files that exist are loaded; a local file without a base file (or
vice versa) is fine. Values are read through `value(keys)` so a caller never
handles the merge itself. Values with safety constraints get validated
readers: `endpoint()` refuses an API-root override that would send the
provider credential anywhere but https or a loopback proxy, and
`endpoint_problem()` reports the same fault for `deadeye doctor`.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import DeadeyeError

CONFIG_ENV = "DEADEYE_CONFIG_DIR"
BASE_NAME = "config.toml"
LOCAL_NAME = "config.local.toml"

# Hosts for which a plain-http endpoint override is tolerated: a local
# self-hosted proxy. Anywhere else, the credential must ride https.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` over `base`; nested tables merge, leaves replace."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a TOML table")
    return data


def _discover() -> Path | None:
    """The config directory to use, or None when no config file exists anywhere."""
    explicit = os.environ.get(CONFIG_ENV, "").strip()
    candidates = [Path(explicit)] if explicit else [Path.cwd(), Path.home() / ".config" / "deadeye"]
    for directory in candidates:
        if (directory / BASE_NAME).is_file() or (directory / LOCAL_NAME).is_file():
            return directory
    return None


class Config:
    """One merged view of base + local config, loaded lazily once per process."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self.data: dict[str, Any] = {}
        if directory is None:
            return
        base = directory / BASE_NAME
        local = directory / LOCAL_NAME
        if base.is_file():
            self.data = _merge(self.data, _load_file(base))
        if local.is_file():
            self.data = _merge(self.data, _load_file(local))

    def value(self, keys: tuple[str, ...]) -> Any:
        """A value by key path (e.g. `("providers", "nvidia", "api_key")`), or None."""
        current: Any = self.data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current

    def sources(self) -> list[Path]:
        """The files actually loaded, base first, for `doctor`."""
        if self.directory is None:
            return []
        return [
            self.directory / name
            for name in (BASE_NAME, LOCAL_NAME)
            if (self.directory / name).is_file()
        ]


class _Cache:
    """Process-wide config cache; attribute writes keep this `global`-free."""

    loaded: Config | None = None
    failed: str | None = None
    note: str | None = None


def load() -> Config:
    """The process-wide merged config; a parse failure surfaces once, loudly."""
    if _Cache.loaded is None and _Cache.failed is None:
        try:
            directory = _discover()
            if directory is None:
                explicit = os.environ.get(CONFIG_ENV, "").strip()
                if explicit:
                    _Cache.note = (
                        f"{CONFIG_ENV}={explicit} names a directory holding "
                        f"neither {BASE_NAME} nor {LOCAL_NAME}; built-in "
                        "defaults apply"
                    )
            _Cache.loaded = Config(directory)
        except ValueError as exc:
            _Cache.failed = str(exc)
            raise
    if _Cache.loaded is None:
        raise ValueError(_Cache.failed or "config failed to load")
    return _Cache.loaded


def load_failure() -> str | None:
    """The parse error from the failed `load()`, or None; for doctor."""
    return _Cache.failed


def discovery_note() -> str | None:
    """Why no config file was found, when an explicit directory was named; for doctor."""
    return _Cache.note


def reset() -> None:
    """Forget the cached config (tests)."""
    _Cache.loaded = None
    _Cache.failed = None
    _Cache.note = None


def value(keys: tuple[str, ...]) -> Any:
    """Convenience: `value(("providers", "nvidia", "api_key"))` on the merged config.

    Fail-soft: a config that failed to parse reads as no value everywhere
    (providers report unavailable), and `load_failure()` names the error.
    """
    try:
        return load().value(keys)
    except ValueError:
        return None


def text(keys: tuple[str, ...]) -> str | None:
    """The value iff a non-empty string, else None; the one home for the
    configured-string idiom (`default_model`, an api_key) every reader shares."""
    found = value(keys)
    return found if isinstance(found, str) and found else None


def credential_for(provider: str, env_names: tuple[str, ...]) -> str | None:
    """A provider's key: environment first, then config, per the documented order.

    `providers.<name>.api_key` wins over a top-level `api_key`, so a
    one-key setup (`api_key = "nvapi-..."` like the sibling llm-proxy) and a
    per-provider setup both work.
    """
    for name in env_names:
        found = os.environ.get(name)
        if found:
            return found
    return text(("providers", provider, "api_key")) or text(("api_key",))


def _override_root(keys: tuple[str, ...]) -> str | None:
    """The configured API-root override, or None when unset; refuses a bad one.

    The override exists for a self-hosted proxy, so plain http is accepted
    only for a loopback host; anywhere else the bearer key or API key would
    travel in cleartext or reach an unintended host. Anything else is refused
    here — at review start with a named key — instead of failing inside the
    HTTP stack after media was read.
    """
    raw = value(keys)
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = raw.strip()
    parts = urlsplit(root)
    host = (parts.hostname or "").strip("[]").lower()
    if (parts.scheme == "https" and parts.netloc) or (
        parts.scheme == "http" and host in LOOPBACK_HOSTS
    ):
        return root
    raise DeadeyeError(
        f"config '{'.'.join(keys)}' must be an https:// URL (plain http only "
        f"for a loopback proxy such as http://localhost): got {raw!r}"
    )


def endpoint(keys: tuple[str, ...], fallback: str) -> str:
    """A provider API root override, validated before anything is submitted."""
    return _override_root(keys) or fallback


def endpoint_problem(keys: tuple[str, ...]) -> str | None:
    """Why an endpoint override cannot be used, or None; for `doctor`.

    Pure validation over the already-loaded config: no network, so capability
    discovery stays offline while still surfacing an unusable override before
    any review is attempted.
    """
    try:
        _override_root(keys)
    except DeadeyeError as exc:
        return str(exc)
    return None
