"""The stdio framing codec, tested at its own boundary.

`mcp.serve` is exercised end to end elsewhere; these pin the module's
contract directly, so a change to the splitting rules is caught here rather
than only through a frame the server happens to serve.
"""

from __future__ import annotations

import io

from deadeye import _jsonrpc_frames
from deadeye._jsonrpc_frames import MAX_FRAME_BYTES, iter_stdio_frames, write_frame


def test_a_frame_arriving_in_pieces_is_joined_not_repeated() -> None:
    source = io.BytesIO(b'{"a":1}\n{"b":2}\n')
    assert list(iter_stdio_frames(source, MAX_FRAME_BYTES)) == [b'{"a":1}', b'{"b":2}']


def test_an_unterminated_final_frame_is_still_answered() -> None:
    """A client that writes a frame and waits for one must not wait forever."""
    assert list(iter_stdio_frames(io.BytesIO(b'{"a":1}'), MAX_FRAME_BYTES)) == [b'{"a":1}']


def test_an_oversized_frame_is_refused_once_and_the_next_is_aligned() -> None:
    source = io.BytesIO(b"x" * 40 + b"\n" + b'{"a":1}\n')
    assert list(iter_stdio_frames(source, 16)) == [None, b'{"a":1}']


def test_the_cap_is_measured_in_utf8_bytes_on_a_text_transport() -> None:
    """11 characters, 33 UTF-8 bytes: under the cap as characters, over as bytes."""
    source = ["\N{SNOWMAN}" * 11, '{"a":1}']
    assert list(iter_stdio_frames(source, 32)) == [None, '{"a":1}']


def test_write_frame_emits_one_line_and_flushes() -> None:
    stream = io.StringIO()
    write_frame(stream, {"jsonrpc": "2.0", "id": 1, "result": {}})
    assert stream.getvalue() == '{"jsonrpc": "2.0", "id": 1, "result": {}}\n'


def test_the_module_keeps_no_state_between_reads() -> None:
    """A fresh iterator over the same bytes yields the same frames."""
    payload = b'{"a":1}\n{"b":2}\n'
    first = list(iter_stdio_frames(io.BytesIO(payload), MAX_FRAME_BYTES))
    second = list(iter_stdio_frames(io.BytesIO(payload), MAX_FRAME_BYTES))
    assert first == second
    assert _jsonrpc_frames.MAX_FRAME_BYTES == MAX_FRAME_BYTES
