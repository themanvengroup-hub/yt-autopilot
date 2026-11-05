"""Turn (script + your voice) into a finished, premium-looking video.

Edit doctrine (what makes it look "produced" rather than "slideshow"):
  * Cut on meaning: hard cuts inside a beat, a short motion transition only where the
    script changes beat (segment boundary). Transitions are seams, not decoration.
  * Every shot moves: an eased push-in or pull-out (alternating), never a static frame.
  * One look: the same grade on every clip (contrast curve, saturation, vignette, fine
    grain), so footage from different photographers feels like one film.
  * Text in the style of the reference reels (see styled.py): two-line phrase subtitles in
    the centre (white lead-in, amber payoff, numbers enlarged), one white info card where
    the script is fact-heavy, a corner wordmark, and a gold serif end card on long-form.
  * Brand on every video: logo badge + wordmark in the corner, animated logo end card
    joined with a zoom transition (brand.py).
  * Variety per video (seeded by the episode id): transition order from a motion-matched
    pool with an occasional flash, warm light-leak sweeps, shuffled camera moves
    (push / pull / pan / tilt), and its own variants of every sound effect (sfx.py).
  * Mix: voice is the anchor (-14 LUFS), music sits ~20 dB under and ducks when you speak,
    SFX sit 15-24 dB under the voice.

Pipeline:
    voice.m4a ─▶ clean/tighten ─▶ word timings (Whisper)
                                   │
          script segments ─────────┼─▶ shot plan ─▶ group shots by segment
                                   │        │
                                   │        └─▶ B-roll per shot ─▶ graded, moving shots ─▶ concat per segment
                                   │                                                      └▶ xfade chain across segments
    voice + ducked music + SFX pack ──────────────────────────────▶ final mux (+ logo, text layer, end card)
"""
from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import random
import zlib

from . import brand, captions, media, sfx
from .broll import BrollLibrary, Clip
from .config import ROOT, WORK_DIR, Format, cfg, video_format
from .scriptwriter import iter_segments, teleprompter_text
from .state import Episode
from .thumbnail import make_thumbnail

log = logging.getLogger(__name__)
FPS = 30
# Transition pool for segment changes: motion-matched ones only (no star wipes / page curls).
# Each video draws its own order from this pool (seeded by the episode id), so consecutive
# videos don't repeat the same sequence. "fadewhite" is a short flash, used sparingly.
TRANSITIONS = ["smoothleft", "zoomin", "smoothup", "hblur", "smoothright", "fade", "smoothdown", "zoomin"]
FLASH = "fadewhite"
# Camera moves per shot (also shuffled per video): push in, pull out, slow pan, slow tilt.
MOTIONS = ["push", "pan_r", "pull", "pan_l", "push", "tilt"]


def _seed(ep_id: str) -> int:
    return zlib.crc32(ep_id.encode()) & 0x7FFFFFFF


def _transition_plan(n: int, seed: int) -> tuple[list[str], list[int]]:
    """Transition names for n seams + the seams that also get a light-leak sweep."""
    rng = random.Random(seed)
    pool = TRANSITIONS[:]
    rng.shuffle(pool)
    names = [pool[i % len(pool)] for i in range(n)]
    for i in range(n):                      # roughly one flash per 7 seams, never the first
        if i and i % 7 == rng.randrange(3, 7):
            names[i] = FLASH
    leaks = [i for i in range(n) if names[i] != FLASH and rng.random() < 0.35]
    return names, leaks


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


def _r() -> dict:
    return cfg()["render"]


# ── timing ───────────────────────────────────────────────────
def _segment_lookup(segments: list[dict]) -> list[float]:
    """Cumulative word-share boundaries per segment, e.g. [0.12, 0.30, ... 1.0]."""
    counts = [max(len(s["text"].split()), 1) for s in segments]
    total, acc, bounds = sum(counts), 0, []
    for c in counts:
        acc += c
        bounds.append(acc / total)
    return bounds


def _segment_of(word_idx: int, n_words: int, bounds: list[float]) -> int:
    """Map a spoken word to a script segment by relative position (robust to ad-libs)."""
    ratio = (word_idx + 0.5) / max(n_words, 1)
    return min(bisect_right(bounds, ratio), len(bounds) - 1)


def plan_shots(words: list[captions.Word], total: float, bounds: list[float], target: float) -> list[Shot]:
    """Cut at every segment change (≥1.2 s in) and every `target` seconds within a segment,
    always on a word boundary so cuts land on speech rhythm."""
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
    merged: list[Shot] = []
    for sh in shots:  # merge slivers into the previous shot
        if merged and sh.end - sh.start < 0.8:
            merged[-1].end = sh.end
        else:
            merged.append(sh)
    return merged


def group_by_segment(shots: list[Shot], min_len: float = 1.6) -> list[list[Shot]]:
    """Consecutive shots of the same segment form one group; transitions sit between groups.
    Very short groups are merged into the previous one so transitions never stack up."""
    groups: list[list[Shot]] = []
    for sh in shots:
        if groups and groups[-1][-1].segment == sh.segment:
            groups[-1].append(sh)
        else:
            groups.append([sh])
    merged: list[list[Shot]] = []
    for g in groups:
        if merged and g[-1].end - g[0].start < min_len:
            merged[-1].extend(g)
        else:
            merged.append(g)
    return merged


def segment_starts(words: list[captions.Word], bounds: list[float]) -> dict[int, float]:
    first: dict[int, float] = {}
    for i, w in enumerate(words):
        first.setdefault(_segment_of(i, len(words), bounds), w.start)
    return first


# ── footage ──────────────────────────────────────────────────
def _queries(seg: dict) -> list[str]:
    q = seg.get("broll") or []
    q = [q] if isinstance(q, str) else list(q)
    return [x for x in q + list(seg.get("broll_alt") or []) if x]


def _assign_broll(shots: list[Shot], segments: list[dict], lib: BrollLibrary) -> None:
    """Each shot gets its own clip where possible; a clip is reused within a segment only
    when the library runs dry, and then from a different point in the clip."""
    per_seg: dict[int, list[Clip]] = {}
    use_count: dict[int, int] = {}
    for sh in shots:
        clips = per_seg.setdefault(sh.segment, [])
        k = use_count.get(sh.segment, 0)
        dur = sh.end - sh.start
        if len(clips) <= k and len(clips) < 3:
            clip = lib.clip_for(_queries(segments[sh.segment]), dur)
            if clip:
                clips.append(clip)
        if clips:
            sh.clip = clips[k % len(clips)]
            usable = max(sh.clip.duration - dur - 0.6, 0.0)
            # skip the first ~0.5 s (stock clips often fade in) and vary the start on reuse
            sh.offset = min(0.5 + (k // len(clips)) * dur, 0.5 + usable) if usable else 0.0
        use_count[sh.segment] = k + 1


# ── shot encoding ────────────────────────────────────────────
def _grade() -> str:
    if not _r().get("grade", True):
        return "null"
    # clean "premium real-estate" look (references): bright, slightly warm highlights,
    # soft contrast, very light vignette, almost no grain
    return ("curves=master='0/0.02 0.25/0.23 0.5/0.51 0.75/0.79 1/0.98',"
            "eq=saturation=1.08:contrast=1.03,"
            "colorbalance=rh=0.03:bh=-0.03:rm=0.01:bm=-0.01,"
            "vignette=angle=PI/7:mode=forward,"
            "noise=alls=1:allf=t")


def _encode_shot(sh: Shot, idx: int, fmt: Format, out: Path, dur: float, motion: str = "push") -> None:
    W, H = fmt.width, fmt.height
    frames = max(round(dur * FPS), 1)
    amp = 0.07 if fmt.is_short else 0.045          # push distance
    # ease-out cubic: fast start, gentle settle (reads as intentional camera move)
    ease = f"(1-pow(1-min(on/{frames},1),3))"
    glide = f"(0.5-0.5*cos(PI*min(on/{frames},1)))"   # ease-in-out for pans/tilts
    cx, cy = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    if motion == "pull":
        z, x, y = f"1+{amp}-{amp}*{ease}", cx, cy
    elif motion in ("pan_r", "pan_l"):
        pz = 1.12 if fmt.is_short else 1.09
        z, y = f"{pz}", cy
        x = f"(iw-iw/zoom)*{glide}" if motion == "pan_r" else f"(iw-iw/zoom)*(1-{glide})"
    elif motion == "tilt":
        pz = 1.10
        z, x = f"{pz}", cx
        y = f"(ih-ih/zoom)*(1-{glide})"
    else:
        z, x, y = f"1+{amp}*{ease}", cx, cy
    if sh.clip:
        # 2x oversample before zoompan removes the sub-pixel jitter zoompan is known for
        vf = (f"fps={FPS},scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
              f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={W}x{H}:fps={FPS},"
              f"{_grade()},format=yuv420p,setsar=1")
        inp = ["-stream_loop", "-1", "-ss", f"{sh.offset:.2f}", "-i", str(sh.clip.path)]
    else:
        vf = f"fps={FPS},format=yuv420p,setsar=1"
        inp = ["-f", "lavfi", "-i", f"gradients=s={W}x{H}:c0=0x0b132b:c1=0x1c2541:speed=0.01:r={FPS}"]
    media.run(["ffmpeg", "-y", *inp, "-frames:v", str(frames), "-vf", vf, "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", str(out)])


def _render_groups(groups: list[list[Shot]], fmt: Format, work: Path, T: float,
                   seed: int = 0) -> tuple[Path, list[float], list[str]]:
    """Encode shots → one clip per group → xfade chain. Returns video + transition times."""
    # encode every shot in parallel (zoompan is mostly single-threaded, so this scales ~linearly)
    jobs, lists, idx = [], [], 0
    for gi, g in enumerate(groups):
        paths = []
        for si, sh in enumerate(g):
            dur = sh.end - sh.start
            if si == len(g) - 1 and gi < len(groups) - 1:
                dur += T                              # overlap consumed by the transition
            p = work / "shots" / f"{idx:04d}.mp4"
            jobs.append((sh, idx, p, dur))
            paths.append(p)
            idx += 1
        lists.append(paths)
    moves = MOTIONS[:]
    random.Random(seed + 1).shuffle(moves)
    workers = max(os.cpu_count() or 2, 2)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda j: _encode_shot(j[0], j[1], fmt, j[2], j[3], moves[j[1] % len(moves)]), jobs))

    group_files: list[Path] = []
    group_durs: list[float] = []
    for gi, paths in enumerate(lists):
        lst = work / f"g{gi:03d}.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in paths))
        gf = work / f"g{gi:03d}.mp4"
        media.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(gf)])
        group_files.append(gf)
        group_durs.append(media.duration(gf))

    base = work / "base.mp4"
    if len(group_files) == 1 or T <= 0:
        lst = work / "groups.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in group_files))
        media.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(base)])
        return base, [], []

    inputs: list[str] = []
    for p in group_files:
        inputs += ["-i", str(p)]
    chain, times = [], []
    names, _ = _transition_plan(len(group_files) - 1, seed)
    acc, prev = group_durs[0], "[0:v]"
    for i in range(1, len(group_files)):
        offset = acc - T
        label = f"[x{i}]"
        chain.append(f"{prev}[{i}:v]xfade=transition={names[i - 1]}"
                     f":duration={T}:offset={offset:.3f}{label}")
        times.append(offset)
        acc += group_durs[i] - T
        prev = label
    media.run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(chain), "-map", prev,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", str(base)])
    return base, times, names


# ── main ─────────────────────────────────────────────────────
def render_episode(ep: Episode, voice_src: Path) -> RenderResult:
    fmt = video_format(ep.kind)
    r = _r()
    work = WORK_DIR / ep.id
    shutil.rmtree(work, ignore_errors=True)
    (work / "shots").mkdir(parents=True)

    # 1. audio
    voice = media.prepare_voice(voice_src, work / "voice.wav")
    total = media.duration(voice)
    if ep.kind == "short" and total > 175:
        raise ValueError(f"Short voiceover is {total:.0f}s; YouTube Shorts must be under 3 minutes")
    log.info("voice duration %.1fs", total)

    # 2. word timings (drive cut points, callouts and chapters)
    try:
        words = captions.transcribe(voice, teleprompter_text(ep.kind, ep.script))
    except Exception as exc:  # noqa: BLE001
        log.warning("transcription failed (%s); estimating timings from script", exc)
        words = []
    if len(words) < 5:
        words = captions.estimate_words(" ".join(s["text"] for s in iter_segments(ep.kind, ep.script)), total)

    # 3. shot plan + footage
    segments = iter_segments(ep.kind, ep.script)
    bounds = _segment_lookup(segments)
    shots = plan_shots(words, total, bounds, fmt.shot_seconds)
    _assign_broll(shots, segments, BrollLibrary(fmt))
    with_footage = sum(1 for s in shots if s.clip)
    log.info("%d shots, %d with footage", len(shots), with_footage)
    if with_footage < 0.7 * len(shots):
        # never upload a video full of blank frames: fail now, the nightly run retries it
        raise RuntimeError(f"only {with_footage}/{len(shots)} shots got stock footage "
                           "(stock site rate-limited or unreachable); will retry on the next run")
    groups = group_by_segment(shots)
    T = float(r.get("transition_seconds_short" if fmt.is_short else "transition_seconds_long", 0.3)) \
        if r.get("transitions", True) else 0.0

    # 4. chapters + callouts
    starts = segment_starts(words, bounds)
    chapters: list[tuple[float, str]] = []
    callouts: list[tuple[float, str]] = []
    for si, seg in enumerate(segments):
        t = starts.get(si)
        if t is None:
            continue
        if ep.kind == "long" and seg.get("section"):
            chapters.append((0.0 if not chapters else t, seg["section"]))
        if r.get("callouts", True) and seg.get("callout"):
            # one text element at a time: wait for a chapter title on the same beat to clear
            delay = 3.1 if (ep.kind == "long" and seg.get("section") and t > 1) else 0.15
            callouts.append((t + delay, seg["callout"]))

    # info cards (reference-style): one per fact-heavy segment, held while that beat is spoken
    cards: list[tuple[float, float, dict]] = []
    if r.get("info_cards", True):
        seg_ids = sorted(starts)
        for k, si in enumerate(seg_ids):
            card = segments[si].get("card")
            if not isinstance(card, dict) or not card.get("rows"):
                continue
            t0 = starts[si] + 0.15
            nxt = starts[seg_ids[k + 1]] if k + 1 < len(seg_ids) else total
            t1 = min(max(nxt, t0 + 3.0), t0 + 6.5, total)
            if t1 - t0 >= 2.0 and (not cards or t0 > cards[-1][1] + 4):
                cards.append((t0, t1, card))

    # 5. picture
    seed = _seed(ep.id)
    base, transition_times, transition_names = _render_groups(groups, fmt, work, T, seed)
    _, leak_idx = _transition_plan(len(transition_times), seed)
    leak_times = [transition_times[i] + T / 2 for i in leak_idx]
    base_dur = media.duration(base)

    # 6. branded end card (every video) — joined with a zoom transition
    end_s = float(r.get("end_card_seconds_short" if fmt.is_short else "end_card_seconds_long", 3.0))
    ET = 0.5
    end_at: float | None = None
    video = base
    if r.get("end_card", True):
        end_clip = brand.end_card(fmt, work / "end.mp4", end_s, FPS, seed)
        end_at = base_dur - ET
        video = work / "with_end.mp4"
        media.run(["ffmpeg", "-y", "-i", str(base), "-i", str(end_clip), "-filter_complex",
                   f"[0:v][1:v]xfade=transition={r.get('end_card_transition', 'zoomin')}:duration={ET}:"
                   f"offset={end_at:.3f},format=yuv420p[v]", "-map", "[v]",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", str(video)])
    final_dur = (end_at + end_s) if end_at is not None else base_dur

    # 7. text layer (hook, two-tone subtitles, info cards, section titles, light leaks)
    hook = ep.script.get("hook_text", "") if fmt.is_short else ""
    text_events: list[tuple[str, float]] = []
    overlay = captions.build_overlay(
        fmt, work / "overlay.ass",
        words=words if r.get("captions", False) else None,
        hook_text=hook, callouts=callouts,
        section_marks=chapters if r.get("section_titles", True) else None,
        cards=cards, end_card_at=end_at,
        leak_times=leak_times if r.get("light_leaks", True) else [], events=text_events,
    )

    # 8. sound: voice + ducked music + a per-video SFX pack on the edit's key moments
    mix = media.mix_with_music(voice, work / "mix.wav", events=_sfx_events(
        seed, work, transition_times, transition_names, T, cards, text_events,
        [t for t, _ in chapters if t > 1], end_at, bool(hook)) if r.get("sfx", True) else [],
        tail=max(final_dur - total, 0.0))

    # 9. final: corner logo (until the end card) + text layer, muxed with the mix
    wm, wx, wy = brand.watermark_png(fmt, work / "watermark.png")
    stop = end_at if end_at is not None else final_dur
    fc = (f"[2:v]format=rgba,fade=t=in:st=0.3:d=0.5:alpha=1[wm];"
          f"[0:v][wm]overlay=x={wx}:y={wy}:enable='lt(t,{stop + 0.2:.2f})':format=auto[v0]")
    if overlay:
        fonts_dir = (ROOT / r["caption_font_file"]).parent
        fc += f";[v0]ass={overlay.as_posix()}:fontsdir={fonts_dir.as_posix()}[v]"
    else:
        fc += ";[v0]null[v]"
    final = work / f"{ep.id}.mp4"
    media.run(["ffmpeg", "-y", "-i", str(video), "-i", str(mix), "-loop", "1", "-t", f"{final_dur:.2f}",
               "-i", str(wm), "-filter_complex", fc,
               "-map", "[v]", "-map", "1:a", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
               "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-t", f"{final_dur:.3f}", "-movflags", "+faststart",
               str(final)])

    thumb = None
    if ep.kind == "long":
        frame_t = (shots[0].start + shots[0].end) / 2
        thumb = make_thumbnail(base, frame_t, ep.script.get("thumbnail_text", ep.script["title"]), work / "thumb.jpg")

    return RenderResult(final, media.duration(final), thumb, _valid_chapters(chapters))


def _sfx_events(seed: int, work: Path, transitions: list[float], names: list[str], T: float,
                cards: list[tuple[float, float, dict]], text_events: list[tuple[str, float]],
                sections: list[float], end_at: float | None, has_hook: bool) -> list[tuple[float, Path, float]]:
    """Place this video's SFX variants: impact on the open, whoosh per transition (variants
    rotate), swoosh + row ticks on info cards, pop on number reveals, swoosh on chapter titles,
    riser → impact → shimmer → pop on the end card."""
    pk = sfx.pack(seed, work / "sfx")
    ev: list[tuple[float, Path, float]] = []

    def add(t: float, kind: str, gain: float = 0.0) -> None:
        path, level = pk.get(kind)
        ev.append((t, path, level + gain))

    add(0.0, "impact", -3 if has_hook else -5)
    for t, name in zip(transitions, names):
        add(max(t + T / 2 - 0.35, 0), "whoosh")
        if name == FLASH:
            add(t + T / 2 - 0.05, "impact", -6)
    for t0, _, card in cards:
        add(max(t0 - 0.1, 0), "swoosh")
        for i in range(min(len(card.get("rows") or []), 4)):
            add(t0 + 0.26 + i * 0.14, "tick")
    last = -9.0
    for kind, t in sorted(text_events, key=lambda e: e[1]):
        if t - last >= 1.5 and not any(c0 - 0.3 <= t <= c1 for c0, c1, _ in cards):
            add(t, kind)
            last = t
    for t in sections:
        add(t, "swoosh", -2)
    if end_at is not None:
        add(max(end_at - 0.6, 0), "riser")
        add(end_at + 0.45, "impact", -2)
        add(end_at + 0.85, "shimmer")
        add(end_at + 1.12, "pop", -2)
    return sorted(ev, key=lambda e: e[0])


def _valid_chapters(ch: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """YouTube needs: first chapter at 0:00, ≥3 chapters, each ≥10s apart."""
    out: list[tuple[float, str]] = []
    for t, title in ch:
        if not out or t - out[-1][0] >= 10:
            out.append((t, title))
    return out if len(out) >= 3 else []
