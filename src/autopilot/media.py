"""ffmpeg helpers: voice cleanup, music ducking, probing."""
from __future__ import annotations

import json
import logging
import random
import subprocess
from pathlib import Path

from .config import ASSETS_DIR, cfg

log = logging.getLogger(__name__)


def run(cmd: list[str]) -> None:
    log.debug("ffmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd[:6])}...\n{proc.stderr[-2000:]}")


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


def prepare_voice(src: Path, dst: Path) -> Path:
    """Phone recording → clean, tight, broadcast-loud mono WAV.

    highpass/lowpass  remove rumble & hiss
    afftdn            spectral denoise (room/fan noise)
    silenceremove     tighten pauses > max_pause (jump-cut pacing, +retention)
    acompressor       even out loud/soft words
    loudnorm          -14 LUFS, YouTube's playback target
    """
    p = float(cfg()["render"]["max_pause_seconds"])
    af = ",".join([
        "highpass=f=80", "lowpass=f=13000", "afftdn=nf=-25",
        "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
        f":stop_periods=-1:stop_duration={p}:stop_threshold=-45dB:stop_silence={p * 0.6:.2f}",
        "acompressor=threshold=-20dB:ratio=3:attack=5:release=120:makeup=2",
        "loudnorm=I=-14:TP=-1.5:LRA=11",
    ])
    run(["ffmpeg", "-y", "-i", str(src), "-af", af, "-ac", "1", "-ar", "48000", str(dst)])
    return dst


def pick_music() -> Path | None:
    tracks = [p for p in (ASSETS_DIR / "music").glob("*") if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"}]
    return random.choice(tracks) if tracks else None


def _db(x: float) -> float:
    return 10 ** (x / 20)


def mix_with_music(voice: Path, dst: Path, sfx_times: list[float] | None = None) -> Path:
    """Voice at 0 dB (already -14 LUFS), music ~-20 dB ducking under speech,
    a soft synthesized whoosh (~-18 dB) on each transition, gentle music fade-out."""
    r = cfg()["render"]
    music = pick_music()
    sfx_times = [t for t in (sfx_times or []) if t > 0.3]
    total = duration(voice)

    inputs = ["-i", str(voice)]
    parts = ["[0:a]aresample=48000,aformat=channel_layouts=mono,asplit=2[v1][v2]"]
    mix_labels = ["[v2]"]
    n = 1
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        vol = _db(float(r.get("music_db", -20)))
        parts.append(f"[{n}:a]aresample=48000,aformat=channel_layouts=mono,volume={vol:.4f},"
                     f"atrim=0:{total:.3f},afade=t=in:d=1.5,afade=t=out:st={max(total - 2.5, 0):.3f}:d=2.5[m]")
        parts.append("[m][v1]sidechaincompress=threshold=0.02:ratio=8:attack=10:release=400[duck]")
        mix_labels.append("[duck]")
        n += 1
    else:
        log.info("no music in assets/music — voice + sfx only")
        parts.append("[v1]anullsink")
    if sfx_times:
        # whoosh = pink noise through a sweeping band, swelled in/out (peaks at the cut)
        inputs += ["-f", "lavfi", "-i", "anoisesrc=d=0.6:c=pink:a=0.6:r=48000"]
        vol = _db(float(r.get("sfx_db", -18)))
        parts.append(f"[{n}:a]highpass=f=250,lowpass=f=5000,afade=t=in:d=0.35:curve=exp,"
                     f"afade=t=out:st=0.35:d=0.25,volume={vol:.4f},asplit={len(sfx_times)}"
                     + "".join(f"[w{i}]" for i in range(len(sfx_times))))
        for i, t in enumerate(sfx_times):
            ms = max(int((t - 0.2) * 1000), 0)     # swell lands as the transition happens
            parts.append(f"[w{i}]adelay={ms}|{ms}[d{i}]")
            mix_labels.append(f"[d{i}]")
        n += 1
    parts.append("".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=first:normalize=0,"
                 "alimiter=limit=0.95[out]")
    run(["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(parts), "-map", "[out]",
         "-ac", "2", "-t", f"{total:.3f}", str(dst)])
    return dst
