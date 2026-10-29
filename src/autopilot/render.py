"""Turn (script + your voice) into a finished video.

Pipeline:
    voice.m4a ─▶ clean/tighten ─▶ transcribe (word timings)
                                   │
          script segments ─────────┼─▶ shot plan (cut on segment changes / every N sec)
                                   │        │
                                   │        └─▶ Pexels B-roll per shot ─▶ per-shot encode ─▶ concat
                                   └─▶ karaoke ASS captions
    voice + ducked music ─────────────────────────────────────────────▶ final mux + burn captions
"""
from __future__ import annotations

import logging
import shutil
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

from . import captions, media
from .broll import BrollLibrary, Clip
from .config import ROOT, WORK_DIR, Format, cfg, video_format
from .scriptwriter import iter_segments, teleprompter_text
from .state import Episode
from .thumbnail import make_thumbnail

log = logging.getLogger(__name__)
FPS = 30


@dataclass
class Shot:
    start: float
    end: float
    segment: int
    clip: Clip | None = None
    offset: float = 0.0


@dataclass
class RenderResult:
    video: Path
    duration: float
    thumbnail: Path | None = None
    chapters: list[tuple[float, str]] = field(default_factory=list)


def _segment_lookup(segments: list[dict]) -> list[float]:
    """Cumulative word-share boundaries per segment, e.g. [0.12, 0.30, ... 1.0]."""
    counts = [max(len(s["text"].split()), 1) for s in segments]
    total, acc, bounds = sum(counts), 0, []
    for c in counts:
        acc += c
        bounds.append(acc / total)
    return bounds


def _segment_of(word_idx: int, n_words: int, bounds: list[float]) -> int:
    """Map a spoken word to a script segment by relative position.

    Position-based mapping tolerates ad-libs and skipped lines far better than
    exact text matching, and B-roll only needs to be roughly in sync.
    """
    ratio = (word_idx + 0.5) / max(n_words, 1)
    return min(bisect_right(bounds, ratio), len(bounds) - 1)


def plan_shots(words: list[captions.Word], total: float, bounds: list[float], target: float) -> list[Shot]:
    shots: list[Shot] = []
    n = len(words)
    start, seg = 0.0, _segment_of(0, n, bounds) if n else 0
    for i, w in enumerate(words):
        s = _segment_of(i, n, bounds)
        long_enough = w.start - start >= target
        if i and (s != seg and w.start - start >= 1.2 or long_enough):
            shots.append(Shot(start, w.start, seg))
            start, seg = w.start, s
    shots.append(Shot(start, total, seg))
    # merge slivers (<0.8s) into the previous shot
    merged: list[Shot] = []
    for sh in shots:
        if merged and sh.end - sh.start < 0.8:
            merged[-1].end = sh.end
        else:
            merged.append(sh)
    return merged


def _assign_broll(shots: list[Shot], segments: list[dict], lib: BrollLibrary) -> None:
    per_seg: dict[int, list[Clip]] = {}
    use_count: dict[int, int] = {}
    for sh in shots:
        clips = per_seg.setdefault(sh.segment, [])
        k = use_count.get(sh.segment, 0)
        dur = sh.end - sh.start
        if len(clips) < 2:
            clip = lib.clip_for(segments[sh.segment].get("broll", ""), dur)
            if clip:
                clips.append(clip)
        if clips:
            sh.clip = clips[k % len(clips)]
            # later uses of the same clip start further in, so repeats don't look identical
            sh.offset = (k // len(clips)) * dur % max(sh.clip.duration - dur, 0.01)
        use_count[sh.segment] = k + 1


def _encode_shot(sh: Shot, idx: int, fmt: Format, out: Path) -> None:
    W, H = fmt.width, fmt.height
    frames = max(round(sh.end * FPS) - round(sh.start * FPS), 1)
    dur = frames / FPS
    direction = "" if idx % 2 == 0 else "1-"
    grade = "eq=brightness=-0.03:saturation=1.08"
    if sh.clip:
        vf = (f"scale={int(W * 1.08)}:{int(H * 1.08)}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H}:x='(iw-{W})*({direction}t/{dur:.3f})':y='(ih-{H})/2',"
              f"fps={FPS},{grade},format=yuv420p,setsar=1")
        inp = ["-stream_loop", "-1", "-ss", f"{sh.offset:.2f}", "-i", str(sh.clip.path)]
    else:
        vf = f"fps={FPS},format=yuv420p,setsar=1"
        inp = ["-f", "lavfi", "-i", f"color=c=0x14213d:s={W}x{H}:r={FPS}"]
    media.run(["ffmpeg", "-y", *inp, "-frames:v", str(frames), "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", str(out)])


def render_episode(ep: Episode, voice_src: Path) -> RenderResult:
    fmt = video_format(ep.kind)
    work = WORK_DIR / ep.id
    shutil.rmtree(work, ignore_errors=True)
    (work / "shots").mkdir(parents=True)

    # 1. audio
    voice = media.prepare_voice(voice_src, work / "voice.wav")
    total = media.duration(voice)
    if ep.kind == "short" and total > 175:
        raise ValueError(f"Short voiceover is {total:.0f}s; YouTube Shorts must be under 3 minutes")
    log.info("voice duration %.1fs", total)

    # 2. word timings
    script_text = teleprompter_text(ep.kind, ep.script)
    try:
        words = captions.transcribe(voice, script_text)
    except Exception as exc:  # noqa: BLE001 - keep the pipeline alive, captions just get less precise
        log.warning("transcription failed (%s); estimating timings from script", exc)
        words = []
    if len(words) < 5:
        words = captions.estimate_words(" ".join(s["text"] for s in iter_segments(ep.kind, ep.script)), total)

    # 3. shot plan + B-roll
    segments = iter_segments(ep.kind, ep.script)
    bounds = _segment_lookup(segments)
    shots = plan_shots(words, total, bounds, fmt.shot_seconds)
    _assign_broll(shots, segments, BrollLibrary(fmt))
    log.info("%d shots, %d with footage", len(shots), sum(1 for s in shots if s.clip))

    # 4. chapters / section titles (long-form)
    chapters: list[tuple[float, str]] = []
    if ep.kind == "long":
        first_word_of_seg: dict[int, float] = {}
        for i, w in enumerate(words):
            first_word_of_seg.setdefault(_segment_of(i, len(words), bounds), w.start)
        for si, seg in enumerate(segments):
            if seg.get("section") and si in first_word_of_seg:
                chapters.append((0.0 if not chapters else first_word_of_seg[si], seg["section"]))

    # 5. captions
    ass = captions.build_ass(words, fmt, work / "captions.ass",
                             hook_text=ep.script.get("hook_text", "") if fmt.is_short else "",
                             section_marks=chapters)

    # 6. video
    list_file = work / "shots.txt"
    with open(list_file, "w") as fh:
        for i, sh in enumerate(shots):
            p = work / "shots" / f"{i:04d}.mp4"
            _encode_shot(sh, i, fmt, p)
            fh.write(f"file '{p.as_posix()}'\n")
    base = work / "base.mp4"
    media.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(base)])

    mix = media.mix_with_music(voice, work / "mix.wav")
    final = work / f"{ep.id}.mp4"
    fonts_dir = (ROOT / cfg()["render"]["caption_font_file"]).parent
    vf = f"ass={ass.as_posix()}:fontsdir={fonts_dir.as_posix()}"
    media.run(["ffmpeg", "-y", "-i", str(base), "-i", str(mix), "-vf", vf,
               "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "faster", "-crf", "20",
               "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest", "-movflags", "+faststart",
               str(final)])

    thumb = None
    if ep.kind == "long":
        frame_t = (shots[0].start + shots[0].end) / 2
        thumb = make_thumbnail(base, frame_t, ep.script.get("thumbnail_text", ep.script["title"]), work / "thumb.jpg")

    return RenderResult(final, media.duration(final), thumb, _valid_chapters(chapters))


def _valid_chapters(ch: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """YouTube needs: first chapter at 0:00, ≥3 chapters, each ≥10s apart."""
    out: list[tuple[float, str]] = []
    for t, title in ch:
        if not out or t - out[-1][0] >= 10:
            out.append((t, title))
    return out if len(out) >= 3 else []
