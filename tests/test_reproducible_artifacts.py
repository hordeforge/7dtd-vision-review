"""Pins for `scripts/reproducible_artifacts.py` epoch handling."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "reproducible_artifacts", ROOT / "scripts" / "reproducible_artifacts.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


artifacts = _load()


def test_source_date_epoch_is_the_unix_time_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    assert artifacts.source_date_epoch() == 1700000000


@pytest.mark.parametrize("raw", ["", "not-an-int", "1.5", "1e9"])
def test_source_date_epoch_refuses_a_non_integer(monkeypatch, raw: str) -> None:
    if raw:
        monkeypatch.setenv("SOURCE_DATE_EPOCH", raw)
    else:
        monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        artifacts.source_date_epoch()
    assert exc_info.value.code == 2


@pytest.mark.parametrize("raw", ["-1", "4294967296"])
def test_source_date_epoch_refuses_a_value_gzip_mtime_cannot_store(monkeypatch, raw: str) -> None:
    """gzip mtime is 32-bit unsigned: negative and 2**32 wrap or fail late."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", raw)
    with pytest.raises(SystemExit) as exc_info:
        artifacts.source_date_epoch()
    assert exc_info.value.code == 2


def test_source_date_epoch_accepts_the_32bit_unsigned_bounds(monkeypatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    assert artifacts.source_date_epoch() == 0
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "4294967295")
    assert artifacts.source_date_epoch() == 0xFFFFFFFF
