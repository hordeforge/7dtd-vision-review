"""Newline-delimited JSON-RPC frame I/O for the stdio transport.

The MCP server itself is tools and dispatch; the byte-level framing a client
puts on the wire is a separate concern with a separate contract (never retain
more than the cap, answer a refused frame exactly once, keep the reader
aligned for the next one). It lives here so `mcp.py` reads as the tool surface
and so the framing rules have one home to be read and changed in.

Nothing here knows what a tool is: the functions move raw lines in and raw
frames out, and `mcp.serve` owns the JSON parse and the dispatch between them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from itertools import chain
from typing import Any, TextIO, TypeVar

__all__ = [
    "MAX_FRAME_BYTES",
    "READ_CHUNK_BYTES",
    "iter_stdio_frames",
    "write_frame",
]

# One JSON-RPC frame is a path plus a small intent document, never media.
# Without a cap the long-lived stdio loop retains whatever a client writes
# until the next newline, so a missing delimiter (or a multi-megabyte
# `intent_text`) becomes an unbounded allocation. One MiB is far above any
# honest tools/call and still small enough to refuse before the process
# grows with the input.
MAX_FRAME_BYTES = 1 * 1024 * 1024
READ_CHUNK_BYTES = 8192

_Line = TypeVar("_Line", bytes, str)


def _read_chunks(read: Callable[[int], Any]) -> Iterator[_Line]:
    """Every subsequent non-empty chunk, stopping at end of stream."""
    while True:
        chunk = read(READ_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def _split_stdio_frames(
    read: Callable[[int], Any],
    first: _Line,
    newline: _Line,
    max_bytes: int,
) -> Iterator[_Line | None]:
    """Chunked newline split that never retains more than `max_bytes` of a frame.

    `None` means the current frame exceeded the cap and was discarded through
    its terminating newline (or EOF), so the next yield is still aligned.

    The segments of one frame accumulate in a list and join once, at the
    frame's boundary. Holding the pending frame as a single string made every
    read and every delimiter search walk all of it, so a frame arriving in
    `READ_CHUNK_BYTES` pieces cost time quadratic in its size: a client
    sending a near-cap `intent_text` copied megabytes per read to find a
    newline it could have located in the chunk that held it. Each read now
    contributes one slice of its own chunk and nothing else. Only the last
    `len(newline) - 1` characters can hold a delimiter split across two reads,
    and those are the one piece carried into the next window.
    """
    empty: _Line = newline[:0]
    edge = len(newline) - 1
    segments: list[_Line] = []
    carried: _Line = empty
    # Bytes banked in `segments`, and whether the frame in hand has already
    # been refused for running past the cap with no newline in sight.
    #
    # Counted with `_frame_size` rather than `len`, because `len` counts
    # characters on a text frame and the cap is named in bytes: a frame of
    # four-byte characters is four times the size `len` says it is.
    # `_iter_pre_split_frames` already measures that way, and one cap has to
    # mean one thing at both doors.
    banked = 0
    oversize = False
    for chunk in chain((first,), _read_chunks(read)):
        if not isinstance(chunk, type(carried)):
            return
        window = carried + chunk
        start = 0
        index = window.find(newline)
        while index >= 0:
            line = window[start:index]
            # The frame is every segment banked from earlier reads plus what
            # this window contributes, not this window's slice alone.
            frame_bytes = banked + _frame_size(line)
            if oversize:
                # The newline ends the frame already refused for exceeding
                # the cap; the next line opens a fresh one.
                oversize = False
                banked = 0
                segments = []
            elif frame_bytes > max_bytes:
                banked = 0
                segments = []
                yield None
            else:
                segments.append(line)
                yield empty.join(segments)
                segments = []
                banked = 0
            start = index + len(newline)
            index = window.find(newline, start)
        tail = window[start:]
        carried = tail[len(tail) - edge :] if edge else empty
        if len(tail) > edge:
            piece = tail[: len(tail) - edge]
            banked += _frame_size(piece)
            segments.append(piece)
        if not oversize and banked + _frame_size(carried) > max_bytes:
            # Over the cap with no newline in this window, so the frame runs
            # into the next read: answer once for it and keep none of it.
            oversize = True
            segments = []
            banked = 0
            yield None
    if oversize:
        # Already answered for the frame that was still unterminated at EOF.
        return
    if segments or carried:
        segments.append(carried)
        yield empty.join(segments)


def _frame_size(payload: bytes | str) -> int:
    """A frame's size in the bytes `_MAX_FRAME_BYTES` is named in.

    The stdio transport is bytes, so bytes is the unit the cap counts there.
    A text frame reaches the same cap through a different door, and measuring
    it in code points would admit a frame of four-byte characters at four
    times the intended size. `surrogatepass` keeps the measure total over
    every `str`, so a lone surrogate in a text source cannot raise here
    either; the bytes transport cannot carry one, and refusing the frame it
    belongs to is the transport's business, not the counter's.
    """
    if isinstance(payload, bytes):
        return len(payload)
    return len(payload.encode("utf-8", "surrogatepass"))


def _iter_pre_split_frames(source: Iterable[Any], max_bytes: int) -> Iterator[bytes | str | None]:
    """Bound frames that already arrive one line at a time (a list, a test double)."""
    for raw_line in source:
        if isinstance(raw_line, bytes):
            payload: bytes | str = raw_line.removesuffix(b"\n")
        else:
            payload = raw_line.removesuffix("\n")
        yield None if _frame_size(payload) > max_bytes else payload


def iter_stdio_frames(source: Any, max_bytes: int) -> Iterator[bytes | str | None]:
    """One raw newline-delimited frame at a time, or None when a frame is oversized."""
    read = getattr(source, "read", None)
    if not callable(read):
        yield from _iter_pre_split_frames(source, max_bytes)
        return
    first = read(READ_CHUNK_BYTES)
    if not first:
        return
    newline: bytes | str = b"\n" if isinstance(first, bytes) else "\n"
    yield from _split_stdio_frames(read, first, newline, max_bytes)


def write_frame(stdout: TextIO, frame: dict[str, Any]) -> None:
    """Write one response frame and flush it; the transport is unbuffered."""
    print(json.dumps(frame), file=stdout)
    stdout.flush()
