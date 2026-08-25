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
handles the merge itself.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

CONFIG_ENV = "DEADEYE_CONFIG_DIR"
BASE_NAME = "config.toml"
LOCAL_NAME = "config.local.toml"


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

    def value(self, keys: str | tuple[str, ...]) -> Any:
        """A value by dotted key path (e.g. `providers.nvidia.api_key`), or None."""
        path = keys.split(".") if isinstance(keys, str) else keys
        current: Any = self.data
        for key in path:
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


def load() -> Config:
    """The process-wide merged config; a parse failure surfaces once, loudly."""
    if _Cache.loaded is None and _Cache.failed is None:
        try:
            _Cache.loaded = Config(_discover())
        except ValueError as exc:
            _Cache.failed = str(exc)
            raise
    if _Cache.loaded is None:
        raise ValueError(_Cache.failed or "config failed to load")
    return _Cache.loaded


def load_failure() -> str | None:
    """The parse error from the failed `load()`, or None; for doctor."""
    return _Cache.failed


def reset() -> None:
    """Forget the cached config (tests)."""
    _Cache.loaded = None
    _Cache.failed = None


def value(keys: str | tuple[str, ...]) -> Any:
    """Convenience: `value("providers.nvidia.api_key")` on the merged config.

    Fail-soft: a config that failed to parse reads as no value everywhere
    (providers report unavailable), and `load_failure()` names the error.
    """
    if _Cache.failed is not None:
        return None
    if _Cache.loaded is None:
        try:
            return load().value(keys)
        except ValueError:
            return None
    return _Cache.loaded.value(keys)


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
    configured = value(("providers", provider, "api_key"))
    if isinstance(configured, str) and configured:
        return configured
    top_level = value(("api_key",))
    if isinstance(top_level, str) and top_level:
        return top_level
    return None
