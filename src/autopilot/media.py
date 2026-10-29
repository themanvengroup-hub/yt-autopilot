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


def mix_with_music(voice: Path, dst: Path) -> Path:
    """Loop a background track under the voice and duck it whenever you speak."""
    music = pick_music()
    if not music:
        log.info("no music in assets/music — voice only")
        run(["ffmpeg", "-y", "-i", str(voice), "-c:a", "pcm_s16le", str(dst)])
        return dst
    vol = float(cfg()["render"]["music_volume"])
    graph = (
        "[0:a]asplit=2[v1][v2];"
        f"[1:a]aresample=48000,aformat=channel_layouts=mono,volume={vol}[m];"
        "[m][v1]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[duck];"
        "[v2][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[out]"
    )
    run(["ffmpeg", "-y", "-i", str(voice), "-stream_loop", "-1", "-i", str(music),
         "-filter_complex", graph, "-map", "[out]", "-ac", "2", str(dst)])
    return dst
