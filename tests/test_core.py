"""Fast tests for the non-media logic. Run: PYTHONPATH=src pytest -q"""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from autopilot import captions, cli, render, state
from autopilot.scriptwriter import iter_segments, teleprompter_text

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def short_script():
    return json.loads((FIX / "short_script.json").read_text())


@pytest.fixture
def tmp_content(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    return tmp_path


def test_segments_include_loop_line(short_script):
    segs = iter_segments("short", short_script)
    assert segs[-1]["text"] == short_script["loop_line"]
    assert "ten minutes" in teleprompter_text("short", short_script)


def test_shot_plan_covers_audio_without_gaps(short_script):
    segs = iter_segments("short", short_script)
    text = " ".join(s["text"] for s in segs)
    words = captions.estimate_words(text, 45.0)
    shots = render.plan_shots(words, 45.0, render._segment_lookup(segs), 2.6)
    assert shots[0].start == 0 and shots[-1].end == 45.0
    assert all(a.end == b.start for a, b in zip(shots, shots[1:]))
    assert all(s.end - s.start >= 0.8 for s in shots)
    assert {s.segment for s in shots} == set(range(len(segs)))  # every segment gets footage


def test_chapters_validation():
    assert render._valid_chapters([(0, "a"), (5, "b"), (20, "c")]) == []          # only 2 valid
    assert len(render._valid_chapters([(0, "a"), (15, "b"), (30, "c")])) == 3


def test_episode_refs_are_unique_and_stable(tmp_content, short_script):
    a = state.Episode.create("short", short_script, [])
    b = state.Episode.create("short", short_script, [])
    assert a.ref != b.ref and a.id != b.id
    assert state.find_by_ref(a.ref.lower()).id == a.id
    a.telegram_message_ids = [11, 12]
    a.save()
    assert state.find_by_message(12).id == a.id


def test_publish_time_parsing():
    now = state.now_local()
    later = (now + timedelta(hours=2)).strftime("%H:%M")
    assert cli._parse_publish_time("now", "short") is None
    assert cli._parse_publish_time(later, "short").date() == now.date() or now.hour >= 22
    earlier = (now - timedelta(hours=1)).strftime("%H:%M")
    assert cli._parse_publish_time(earlier, "short") > now
    with pytest.raises(ValueError):
        cli._parse_publish_time("2020-01-01 10:00", "short")


def test_audio_routing_by_reply(tmp_content, short_script, monkeypatch):
    sent = []
    monkeypatch.setattr(cli.tg, "send", lambda *a, **k: sent.append(a[0]) or 1)
    ep = state.Episode.create("short", short_script, [])
    ep.telegram_message_ids = [100]
    ep.save()
    msg = {"message_id": 200, "reply_to_message": {"message_id": 100}, "audio": {"file_id": "F", "file_size": 10}}
    cli._handle_audio(msg, "F", 10)
    reloaded = state.Episode.load(ep.id)
    assert reloaded.status == "recorded" and reloaded.voice_file_id == "F"


def test_ass_escapes_braces(tmp_path):
    from autopilot.config import video_format
    words = [captions.Word("{evil}", 0, 0.5), captions.Word("ok.", 0.5, 1.0)]
    out = captions.build_ass(words, video_format("short"), tmp_path / "c.ass", hook_text="hi")
    body = out.read_text()
    assert "{evil}" not in body.upper() and "(EVIL)" in body
