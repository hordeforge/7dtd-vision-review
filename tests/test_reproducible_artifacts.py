"""Pins for `scripts/reproducible_artifacts.py` epoch handling."""

from __future__ import annotations

import gzip
import importlib.util
import io
import tarfile
import time
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


def _unstamped_sdist(path: Path, payload: bytes) -> Path:
    """An sdist as setuptools leaves it: checkout mtimes, a builder's uid."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        info = tarfile.TarInfo(name="thing-0.1.0/PKG-INFO")
        info.size = len(payload)
        info.mode = 0o664
        info.mtime = 1600000000
        info.uid = 1000
        info.gid = 1000
        info.uname = "builder"
        info.gname = "builder"
        archive.addfile(info, io.BytesIO(payload))
        directory = tarfile.TarInfo(name="thing-0.1.0/")
        directory.type = tarfile.DIRTYPE
        directory.mtime = 1600000000
        directory.mode = 0o775
        archive.addfile(directory)
    gzipped = io.BytesIO()
    with gzip.GzipFile(fileobj=gzipped, mode="wb", mtime=1600000000) as compressor:
        compressor.write(buffer.getvalue())
    path.write_bytes(gzipped.getvalue())
    return path


def test_normalising_a_second_time_changes_nothing(tmp_path: Path) -> None:
    """The step claims to converge on re-run, and the claim is what a
    re-published release depends on: a second pass over an already-normalized
    archive must leave the bytes and the file's own mtime alone, not rewrite
    it with a fresh stamp and churn every downstream hash of the artifact."""
    path = _unstamped_sdist(tmp_path / "thing-0.1.0.tar.gz", b"Metadata-Version: 2.1\n")

    assert artifacts.normalise_sdist(path, 1700000000) is True
    canonical = path.read_bytes()
    stamped = path.stat().st_mtime

    time.sleep(0.01)
    assert artifacts.normalise_sdist(path, 1700000000) is False
    assert path.read_bytes() == canonical
    assert path.stat().st_mtime == stamped

    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    for member in members:
        assert member.mtime == 1700000000
        assert (member.uid, member.gid) == (0, 0)
        assert (member.uname, member.gname) == ("", "")
        assert member.mode in (0o644, 0o755)
