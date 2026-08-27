#!/usr/bin/env python3
"""Normalize release sdists so rebuilding one tag yields byte-identical files.

setuptools stamps wheel entries from SOURCE_DATE_EPOCH but leaves sdists
untouched: tar members keep the checkout's filesystem mtimes, generated
files (PKG-INFO, setup.cfg, egg-info) get build wall-clock time, the gzip
header carries the build moment, and the builder's uid and username leak
into every member header. This rewrites each sdist into a canonical form:

- member order sorted by name,
- every mtime set to SOURCE_DATE_EPOCH,
- uid/gid zeroed and owner names emptied,
- modes reduced to 0644, or 0755 when any execute bit is set,
- GNU-format tar (no pax subheaders carrying extra timestamps),
- gzip header stamped with the same epoch and no embedded filename.

Usage: reproducible_artifacts.py PATH... where PATH is an sdist or a
directory whose *.tar.gz files are all normalized in place. The release
workflow runs it right after `uv build` with SOURCE_DATE_EPOCH exported
from the tagged commit.
"""

from __future__ import annotations

import gzip
import io
import os
import sys
import tarfile
from pathlib import Path


def source_date_epoch() -> int:
    raw = os.environ.get("SOURCE_DATE_EPOCH", "")
    if not raw:
        print(
            "reproducible_artifacts: SOURCE_DATE_EPOCH is not set; refusing to "
            "stamp artifacts with a default",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return int(raw)
    except ValueError:
        print(
            f"reproducible_artifacts: SOURCE_DATE_EPOCH must be an integer, got {raw!r}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


def _canonical_mode(mode: int) -> int:
    return 0o755 if mode & 0o111 else 0o644


def normalise_sdist(path: Path, epoch: int) -> bool:
    """Rewrite `path` in place into canonical form.

    Returns True when bytes changed. Already-canonical archives are left
    untouched so the step converges on re-run instead of churning mtimes.
    """
    original = path.read_bytes()
    with tarfile.open(path, "r:gz") as archive:
        members = sorted(archive.getmembers(), key=lambda m: m.name)
        payloads: list[bytes | None] = []
        for member in members:
            stream = archive.extractfile(member)
            payloads.append(stream.read() if stream is not None else None)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as out:
        for member, payload in zip(members, payloads, strict=True):
            info = tarfile.TarInfo(name=member.name)
            info.size = member.size
            info.mode = _canonical_mode(member.mode)
            info.type = member.type
            info.linkname = member.linkname
            info.mtime = epoch
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if payload is None:
                out.addfile(info)
            else:
                info.size = len(payload)
                out.addfile(info, io.BytesIO(payload))

    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", compresslevel=9, mtime=epoch) as compressor:
        compressor.write(buf.getvalue())
    canonical = gz.getvalue()
    if canonical == original:
        return False
    path.write_bytes(canonical)
    return True


def _sdists(argument: str) -> list[Path]:
    candidate = Path(argument)
    if candidate.is_dir():
        return sorted(candidate.glob("*.tar.gz"))
    return [candidate]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: {Path(argv[0]).name} PATH...", file=sys.stderr)
        return 2
    epoch = source_date_epoch()
    for argument in argv[1:]:
        targets = _sdists(argument)
        if not targets:
            print(f"reproducible_artifacts: no sdist found at {argument}", file=sys.stderr)
            return 1
        for target in targets:
            if not target.is_file():
                print(f"reproducible_artifacts: no sdist at {target}", file=sys.stderr)
                return 1
            state = "normalised" if normalise_sdist(target, epoch) else "already canonical"
            print(f"reproducible_artifacts: {target} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
