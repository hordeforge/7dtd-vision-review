"""The provider boundary, pinned through the fake adapter.

The point of these tests is that the exact candidate media bytes — not a
path, not a description — and the complete intent reach the adapter, proven by
hash. If that ever stops being true, the review is no longer of the clip it
claims to be.
"""

from __future__ import annotations

import hashlib

from deadeye.providers.fake import FakeProvider
from deadeye.review import run_review


def _hash_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_exact_sampled_frames_reach_the_boundary(clip_dir, intent_path, capsys) -> None:
    provider = FakeProvider()
    run_review(
        clip_dir,
        provider=provider,
        intent_path=intent_path,
        allow_network=True,
        output=None,
    )
    request = provider.requests[-1]
    frames = [payload for payload in request.media if payload.kind == "frame"]
    # 10 frames, fake limit of 8 -> 8 sampled; every byte must match the file.
    assert len(frames) == 8
    for payload in frames:
        source = clip_dir / payload.name
        assert _hash_file(source) == hashlib.sha256(payload.data).hexdigest()


def test_the_complete_intent_reaches_the_boundary(clip_dir, intent_path) -> None:
    provider = FakeProvider()
    run_review(
        clip_dir,
        provider=provider,
        intent_path=intent_path,
        allow_network=True,
        output=None,
    )
    prompt = provider.requests[-1].prompt
    assert "purpose: show the garment survives a full turn without clipping" in prompt
    assert "subject: thing (worn garment)" in prompt
    assert "camera path: turntable" in prompt
    assert "qualities to avoid (flag any you see): clipping; popping" in prompt
    assert "the author specifically asks: does the grip read thin?" in prompt
    assert "suite: demo" in prompt
    assert "case: thing" in prompt


def test_reference_media_is_sent_after_the_candidate(clip_dir, tmp_path, intent_path) -> None:
    reference = tmp_path / "good.png"
    reference.write_bytes(b"known-good")
    intent = tmp_path / "intent.json"
    intent.write_text(
        '{"purpose": "compare", "references": [{"path": "' + str(reference) + '", '
        '"purpose": "known-good silhouette"}]}'
    )
    provider = FakeProvider()
    run_review(clip_dir, provider=provider, intent_path=intent, allow_network=True)
    kinds = [payload.kind for payload in provider.requests[-1].media]
    assert kinds[0] == "frame"
    assert kinds[-1] == "reference"
    assert provider.requests[-1].media[-1].name == "good.png"


def test_the_fake_verdict_echoes_the_boundary_view(clip_dir, intent_path) -> None:
    provider = FakeProvider()
    envelope = run_review(
        clip_dir,
        provider=provider,
        intent_path=intent_path,
        allow_network=True,
    )
    assert envelope["provider"]["name"] == "fake"
    assert envelope["result"]["rubric_scores"] == {
        "semantic_fit": None,
        "motion_plausibility": None,
    }
    assert envelope["advisory_only"] is True
    assert envelope["sampling"]["frames_submitted"] == 8
