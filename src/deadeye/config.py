"""Configuration loading: `config.toml` (committed) + `config.local.toml`.

Mirrors the sibling llm-proxy convention: two TOML files in one directory,
loaded in order, the local file winning on conflict. The local file is
gitignored, which is where an API key goes instead of an `export` on every
shell.

Precedence depends on the setting: command-line review options override their
configured counterparts; provider credentials use environment variables first,
then the merged local/base configuration. All other settings come from the
merged configuration, with `config.local.toml` winning over `config.toml`,
then fall back to their built-in defaults.

Discovery (the first directory holding any config file wins, so a checkout
that carries one shadows the home one):

1. `DEADEYE_CONFIG_DIR` — an explicit directory; a leading `~` in it is
   expanded, as it is in `XDG_CONFIG_HOME`.
2. The current working directory (`./config.toml`, `./config.local.toml`).
3. `$XDG_CONFIG_HOME/deadeye/` when `XDG_CONFIG_HOME` is set, otherwise
   `~/Library/Application Support/deadeye/` on macOS and `~/.config/deadeye/`
   everywhere else — the home fallback for an installed tool.

Only the files that exist are loaded; a local file without a base file (or
vice versa) is fine. A file that sets a key deadeye does not read is refused
by name rather than quietly ignored, so a typo cannot leave the built-in
default in force while its author believes the file was honored. Values are
read through `value(keys)` so a caller never handles the merge itself, and
every leaf remembers which file supplied it, so `deadeye doctor` can name the
file a credential came from. Values with safety constraints get validated
readers: `endpoint()` refuses an API-root override that would send the
provider credential anywhere but https or a loopback proxy, and
`endpoint_problem()` reports the same fault for `deadeye doctor`. The
per-provider generation knobs have their validated readers in
`providers/base.py` (`int_setting`, `float_setting`), the one home every
adapter reads them through.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import DeadeyeError

CONFIG_ENV = "DEADEYE_CONFIG_DIR"
BASE_NAME = "config.toml"
LOCAL_NAME = "config.local.toml"
DEFAULT_TIMEOUT_SECONDS = 120.0

# How many times `load` re-reads a source file that moved under the read. The
# write that moves it is an editor or a secret manager, so one retry settles
# almost every case; the bound is what stops a file rewritten continuously
# from spinning this loop, and past it the read that did complete is returned
# uncached rather than none at all.
_LOAD_SIGNATURE_ATTEMPTS = 3

# The template ships inside the package, so it is on disk for an install from
# a wheel as well as from a checkout, and `deadeye doctor` can name a file the
# reader can actually open.
EXAMPLE_PATH = Path(__file__).resolve().parent / "config.local.toml.example"

# Hosts for which a plain-http endpoint override is tolerated: a local
# self-hosted proxy. Anywhere else, the credential must ride https.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

# Every key deadeye reads from a config file, and nothing else. A key outside
# these tables is a typo (`default_provder`, `providers.geminie.api_key`) or a
# setting another tool owns; loading refuses it instead of quietly running on
# the built-in default while the operator believes their file was honored.
# A test pins these tables against the provider registry and against the keys
# the adapters actually read, so a new adapter's key cannot land outside them.
TOP_LEVEL_KEYS = frozenset(
    {"api_key", "default_model", "default_provider", "providers", "timeout_seconds"}
)
PROVIDER_KEYS: dict[str, frozenset[str]] = {
    "fake": frozenset(),
    "gemini": frozenset({"api_key", "endpoint", "max_output_tokens", "model", "temperature"}),
    "nvidia": frozenset(
        {
            "api_key",
            "endpoint",
            "max_tokens",
            "model",
            "reasoning_budget",
            "temperature",
            "top_p",
        }
    ),
}


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` over `base`; nested tables merge, leaves replace."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _record_origins(
    data: dict[str, Any], origins: dict[tuple[str, ...], str], filename: str
) -> None:
    """Record the source file for every leaf of a loaded TOML table.

    Runs for each file in the same order as the merge, so the leaf's final
    entry names the file that won the merge (the gitignored local file when
    it touches the key, the committed file otherwise).
    """

    def walk(node: dict[str, Any], path: tuple[str, ...]) -> None:
        for key, value in node.items():
            child = (*path, key)
            if isinstance(value, dict):
                walk(value, child)
            else:
                origins[child] = filename

    walk(data, ())


def _unknown_provider_keys(providers: dict[str, Any]) -> list[str]:
    """Keys under `[providers.*]` that no adapter reads, as dotted paths."""
    unknown: list[str] = []
    for name, table in providers.items():
        known = PROVIDER_KEYS.get(name)
        if known is None:
            unknown.append(f"providers.{name}")
            continue
        if not isinstance(table, dict):
            continue
        for key, value in table.items():
            path = f"providers.{name}.{key}"
            if isinstance(value, dict):
                # A known key holding a table is as unusable as an unknown one.
                unknown.extend(_unknown_keys(value, ("providers", name, key)))
            elif key not in known:
                unknown.append(path)
    return unknown


def _known_leaf(path: tuple[str, ...]) -> bool:
    """Whether a setting at `path` is one deadeye reads."""
    if path[:1] == ("providers",) and len(path) == 3:
        known = PROVIDER_KEYS.get(path[1])
        return known is not None and path[2] in known
    return len(path) == 1 and path[0] in TOP_LEVEL_KEYS


def _known_table(path: tuple[str, ...]) -> bool:
    """Whether a `[table]` at `path` is one deadeye reads into.

    A table deadeye does not read is named as the fault, rather than judged by
    the keys under it: `[default_provder]` holding `model` is a misspelled
    table whose leaves happen to be spelled like settings elsewhere, and
    checking only the leaves let it load as though the table were honored.
    """
    if path[:1] == ("providers",):
        return len(path) == 1 or (len(path) == 2 and path[1] in PROVIDER_KEYS)
    return len(path) == 1 and path[0] in TOP_LEVEL_KEYS


def _unknown_keys(data: dict[str, Any], path: tuple[str, ...] = ()) -> list[str]:
    """Dotted paths of every setting in `data` that deadeye does not read."""
    unknown: list[str] = []
    for key, value in data.items():
        child = (*path, key)
        if child == ("providers",):
            if isinstance(value, dict):
                unknown.extend(_unknown_provider_keys(value))
            continue
        if isinstance(value, dict):
            if _known_table(child):
                unknown.extend(_unknown_keys(value, child))
            else:
                unknown.append(".".join(child))
        elif not _known_leaf(child):
            unknown.append(".".join(child))
    return unknown


def _unknown_setting_error(filename: str, unknown: list[str]) -> ValueError:
    listed = ", ".join(f"'{name}'" for name in sorted(unknown))
    plural = "keys" if len(unknown) > 1 else "key"
    return ValueError(
        f"config file {filename} sets {plural} deadeye does not read: {listed}; "
        "fix the name or drop the line, or the built-in default applies instead "
        "(docs/reference.md, Configuration, lists every key)"
    )


def _load_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} must contain a TOML table")
    return data


def _user_config_dir() -> Path:
    """The installed-tool config directory: XDG when set, else the platform's.

    `XDG_CONFIG_HOME` is the capability the spec names. Probing the variable
    is correct on any host that sets it, including a Linux box whose config
    lives outside `~/.config`. An empty value is treated as unset, per the
    spec; `~` in the value is expanded.

    With the variable unset the answer is a platform convention, not a probe:
    no capability distinguishes the two trees, so a macOS host gets
    `~/Library/Application Support/deadeye` (where the rest of its per-user
    configuration already lives) and everything else gets `~/.config/deadeye`.
    A macOS user who exports `XDG_CONFIG_HOME` still gets that directory, so
    the override stays the one way to name this path explicitly.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "deadeye"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "deadeye"
    return Path.home() / ".config" / "deadeye"


def _holds_config_file(directory: Path) -> bool:
    """Whether `directory` holds either config file."""
    return (directory / BASE_NAME).is_file() or (directory / LOCAL_NAME).is_file()


def _discover() -> Path | None:
    """The config directory to use, or None when no config file exists anywhere."""
    explicit = os.environ.get(CONFIG_ENV, "").strip()
    # `~` is expanded here for the same reason `_user_config_dir` expands it in
    # `XDG_CONFIG_HOME`: a quoted `DEADEYE_CONFIG_DIR="~/deadeye"` is a shell
    # that never expanded it, and a literal `~` directory is a silent miss on
    # every platform, not only the one where `~` is not a home alias.
    first = Path(explicit).expanduser() if explicit else Path.cwd()
    if _holds_config_file(first):
        return first
    if explicit:
        return None
    # The home fallback costs a `Path.home()` (which reads the password
    # database when `HOME` is unset) and two more stats, and it is consulted on
    # every single config read. A checkout holding its own config file never
    # reaches it, so resolving it is deferred until it is actually needed
    # rather than built into the candidate list up front.
    fallback = _user_config_dir()
    return fallback if _holds_config_file(fallback) else None


class Config:
    """One merged view of base + local config, loaded lazily and reloaded when
    the source files change (see `_Cache`)."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self.data: dict[str, Any] = {}
        # Which file supplied each leaf value. Doctor names the file a
        # credential came from, so a key sitting in the committed
        # `config.toml` is distinguishable from one in the gitignored
        # `config.local.toml`.
        self._origins: dict[tuple[str, ...], str] = {}
        if directory is None:
            return
        # Base then local, so the local file wins on conflict. Both go through
        # the same four steps, and the order the loop runs in is the order
        # every other reader of this class assumes.
        for name in (BASE_NAME, LOCAL_NAME):
            path = directory / name
            if not path.is_file():
                continue
            data = _load_file(path)
            self._reject_unread(path, data)
            _record_origins(data, self._origins, name)
            self.data = _merge(self.data, data)

    @staticmethod
    def _reject_unread(path: Path, data: dict[str, Any]) -> None:
        """Refuse a file holding settings deadeye does not read, naming them.

        The failure is the whole file, not the individual key: a name deadeye
        ignores is a name the operator believes it applied, and the built-in
        default it fell back to is the wrong value at review time.
        """
        unknown = _unknown_keys(data)
        if unknown:
            raise _unknown_setting_error(str(path), unknown)

    def value(self, keys: tuple[str, ...]) -> Any:
        """A value by key path (e.g. `("providers", "nvidia", "api_key")`), or None."""
        current: Any = self.data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current

    def provenance(self, keys: tuple[str, ...]) -> str | None:
        """The file that supplied the leaf at `keys` (`config.toml` or
        `config.local.toml`), or None when the key is unset."""
        return self._origins.get(keys)

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
    signature: tuple[Any, ...] | None = None


def _source_signature(directory: Path | None) -> tuple[Any, ...]:
    """What the cached config was built from: the directory discovery chose,
    the explicit-directory override, and each source file's identity and
    modification state.

    Config files are written outside this process (an operator's editor, a
    secret that just landed), so the cache is only valid while this signature
    is unchanged. A signature is a stat per file, never a parse: an unchanged
    file costs nothing to keep serving.

    `st_ctime_ns` rides with the modification time because a write that
    restores the mtime it found (a `cp -p`, a checkout that preserves it, a
    tool that sets it deliberately) still moves the inode's change time. On a
    filesystem whose timestamps are coarse enough for two writes of the same
    length to land in the same tick, mtime and size alone cannot tell the
    second write from the first, and a credential written over another would
    then be served from the cache until the file changed again.
    """
    entries: list[Any] = [
        os.environ.get(CONFIG_ENV, "").strip(),
        str(directory) if directory else None,
    ]
    if directory is None:
        return tuple(entries)
    for name in (BASE_NAME, LOCAL_NAME):
        path = directory / name
        try:
            status = path.stat()
        except OSError:
            continue
        entries.append(
            (name, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
        )
    return tuple(entries)


def _discovery_note(directory: Path | None) -> str | None:
    """Why the built-in defaults apply, when an explicit directory held nothing."""
    if directory is not None:
        return None
    explicit = os.environ.get(CONFIG_ENV, "").strip()
    if not explicit:
        return None
    return (
        f"{CONFIG_ENV}={explicit} names a directory holding "
        f"neither {BASE_NAME} nor {LOCAL_NAME}; built-in defaults apply"
    )


def load() -> Config:
    """The process-wide merged config, reloaded when its source files change.

    A parse failure is cached too, so a broken file is named once rather than
    re-read on every call, and the operator's fix takes effect as soon as the
    file's signature changes: the long-lived MCP server must not keep
    reporting a fault (or a missing credential) for a config that has since
    been corrected.
    """
    # Discovery runs again so a directory that only now holds a config file
    # (a fresh checkout, a new DEADEYE_CONFIG_DIR) invalidates the cache
    # without a restart. It runs exactly once per call and the result serves
    # the signature check, the load, and the cached signature alike: a review
    # and a `doctor` each read config a dozen times, and rediscovering per
    # read (walking the candidate directories and stat-ing both source files
    # each time) was the dominant cost of a cached read.
    directory = _discover()
    if (_Cache.loaded is not None or _Cache.failed is not None) and _source_signature(
        directory
    ) == _Cache.signature:
        if _Cache.loaded is None:
            raise ValueError(_Cache.failed or "config failed to load")
        return _Cache.loaded
    # The signature is taken before the read, not after it. Stat-ing last would
    # describe a file that may have been rewritten between the parse and the
    # stat, and a cache keyed on that description holds parsed content from
    # the older write for as long as the newer one stands still: the long-lived
    # MCP server would then serve a superseded credential indefinitely. So the
    # read is bracketed instead, and a file that moved under it is re-read
    # rather than pinned.
    for _ in range(_LOAD_SIGNATURE_ATTEMPTS):
        signature = _source_signature(directory)
        try:
            loaded = Config(directory)
        except ValueError as exc:
            _Cache.loaded = None
            _Cache.failed = str(exc)
            _Cache.note = None
            _Cache.signature = signature
            raise
        if _source_signature(directory) == signature:
            _Cache.loaded = loaded
            _Cache.failed = None
            _Cache.note = _discovery_note(directory)
            _Cache.signature = signature
            return loaded
        directory = _discover()
    # A file rewritten faster than it can be read, past the retry bound. The
    # read that finishes is returned, and the cache is left empty rather than
    # keyed on a signature this process never confirmed: the next call reads
    # again instead of serving content of unknown vintage.
    loaded = Config(directory)
    _Cache.loaded = None
    _Cache.failed = None
    _Cache.note = _discovery_note(directory)
    _Cache.signature = None
    return loaded


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
    _Cache.signature = None


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


def provenance(keys: tuple[str, ...]) -> str | None:
    """Fail-soft convenience mirroring `value`: the file that supplied the
    leaf at `keys`, or None when the key is unset or the config failed to
    load."""
    try:
        return load().provenance(keys)
    except ValueError:
        return None


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
    here, at review start with a named key, including a URL the reader cannot
    parse at all, instead of failing inside the HTTP stack after media was read.
    """
    raw = value(keys)
    if not isinstance(raw, str) or not raw.strip():
        return None
    root = raw.strip()
    try:
        parts = urlsplit(root)
        host = (parts.hostname or "").strip("[]").lower()
    except ValueError as exc:
        # `urlsplit` raises rather than returning a host it cannot read, on a
        # bracketed IPv6 literal that is never closed or never a valid address
        # (`http://[::1`). That is this reader's refusal, not a parser fault:
        # a raw ValueError would escape every caller named here, including
        # `endpoint_problem`, which is the only path that is supposed to
        # report an unusable override without raising.
        raise DeadeyeError(
            f"config '{'.'.join(keys)}' is not a URL this tool can read: {raw!r} ({exc})"
        ) from exc
    if (parts.scheme == "https" and parts.netloc) or (
        parts.scheme == "http" and host in LOOPBACK_HOSTS
    ):
        if parts.username is not None or parts.password is not None:
            # Refused before the raw value can be quoted into a refusal, which
            # is the whole point: the userinfo is the secret, and every other
            # message from this reader lands on stderr and in whatever reads
            # the CLI's error channel. Credentials travel in a header, never
            # in a URL, for the same reason the adapters never put a key in a
            # query string (a URL reaches access logs the header does not).
            raise DeadeyeError(
                f"config '{'.'.join(keys)}' must not carry a credential in the URL; "
                "put the key in the environment or under [providers.<name>] api_key"
            )
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
