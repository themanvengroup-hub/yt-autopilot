"""Word-level transcription + animated ASS captions.

Captions use a karaoke style: 2-3 words on screen at a time, with the active
word highlighted. This is the dominant style for retention on Shorts, and
long-form viewers who watch muted also rely on captions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Format, cfg

log = logging.getLogger(__name__)


@dataclass
class Word:
    text: str
    start: float
    end: float


def transcribe(wav: Path, prompt_text: str) -> list[Word]:
    """faster-whisper with word timestamps.

    The script is passed as initial_prompt so names like "Zepto" or "Nykaa",
    and the ₹ figures, come out spelled the way the script has them.
    """
    from faster_whisper import WhisperModel  # heavy import, keep lazy

    import subprocess

    import numpy as np

    # Decode with ffmpeg ourselves (16 kHz mono float32) instead of letting faster-whisper
    # open the file via PyAV, whose API changes have broken file loading before.
    raw = subprocess.run(["ffmpeg", "-nostdin", "-i", str(wav), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32).copy()

    model = WhisperModel(cfg()["render"]["whisper_model"], device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        audio, language="en", word_timestamps=True, vad_filter=True,
        initial_prompt=prompt_text[:800], beam_size=5,
    )
    words = [Word(w.word.strip(), float(w.start), float(w.end))
             for seg in segments for w in (seg.words or []) if w.word.strip()]
    log.info("transcribed %d words", len(words))
    return words


def estimate_words(text: str, total: float) -> list[Word]:
    """Fallback when ASR is unavailable: spread script words over the audio by length."""
    tokens = text.split()
    weights = [len(t) + 2 for t in tokens]
    scale = total / max(sum(weights), 1)
    out, t = [], 0.0
    for tok, w in zip(tokens, weights):
        d = w * scale
        out.append(Word(tok, t, t + d * 0.9))
        t += d
    return out


# ── ASS generation ───────────────────────────────────────────
def _ts(sec: float) -> str:
    sec = max(sec, 0.0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _chunks(words: list[Word], max_words: int, max_gap: float = 0.45) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?,:;]$", w.text):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[Word], fmt: Format, out: Path, *, hook_text: str = "",
              section_marks: list[tuple[float, str]] | None = None) -> Path:
    r = cfg()["render"]
    font = r["caption_font"]
    if not (ROOT / r["caption_font_file"]).exists():
        font = "DejaVu Sans"  # always present on Ubuntu runners
        log.warning("caption font file missing; falling back to %s", font)
    hi = r["caption_highlight"]

    if fmt.is_short:
        size, outline, margin_v, max_words, upper = 84, 7, 620, 3, True
    else:
        size, outline, margin_v, max_words, upper = 62, 5, 70, 6, False

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {fmt.width}
PlayResY: {fmt.height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,{outline},3,2,60,60,{margin_v},1
Style: Hook,{font},{int(size * 0.95)},&H00000000,&H00000000,&H0000E5FF,&H0000E5FF,-1,0,0,0,100,100,1,0,3,18,0,8,80,80,{int(fmt.height * 0.14)},1
Style: Section,{font},{int(size * 0.9)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,1,0,3,16,0,7,80,80,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for chunk in _chunks(words, max_words):
        toks = [_clean(w.text.upper() if upper else w.text) for w in chunk]
        for i, w in enumerate(chunk):
            end = chunk[i + 1].start if i + 1 < len(chunk) else w.end + 0.08
            # subtle pop on the active word
            styled = [f"{{\\c{hi}\\fscx108\\fscy108}}{t}{{\\r}}" if j == i else t for j, t in enumerate(toks)]
            lines.append(f"Dialogue: 1,{_ts(w.start)},{_ts(end)},Cap,,0,0,0,,{' '.join(styled)}")

    if hook_text:
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(2.6)},Hook,,0,0,0,,{{\\fad(80,200)}}{_clean(hook_text.upper())}")
    for t, title in section_marks or []:
        if t > 1:
            lines.append(f"Dialogue: 2,{_ts(t)},{_ts(t + 3.2)},Section,,0,0,0,,{{\\fad(150,250)}}{_clean(title)}")

    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out


# ── premium overlay: hook card, two-tone subtitles, info cards, callouts, watermark, end card ──
def build_overlay(fmt: Format, out: Path, *, words: list[Word] | None = None, hook_text: str = "",
                  callouts: list[tuple[float, str]] | None = None,
                  section_marks: list[tuple[float, str]] | None = None,
                  cards: list[tuple[float, float, dict]] | None = None,
                  end_card_at: float | None = None) -> Path | None:
    """Return an .ass file, or None when there is nothing to draw.

    Motion follows the usual kinetic-type rules: elements enter fast with an ease-out
    overshoot (scale 112%→100% in 180 ms), hold, and leave quicker than they came
    (fade 150 ms). Big text elements never overlap each other.
    """
    callouts = callouts or []
    section_marks = [m for m in (section_marks or []) if m[0] > 1]
    r = cfg()["render"]
    if words and r.get("caption_style", "two_tone") == "karaoke":  # legacy karaoke captions
        return build_ass(words, fmt, out, hook_text=hook_text, section_marks=section_marks)
    if words or cards or end_card_at is not None or r.get("watermark", True):
        from . import styled  # two-tone subtitles + cards (reference "Citylights" look)
        return styled.build(fmt, out, words=words or [], hook_text=hook_text, callouts=callouts,
                            section_marks=section_marks, cards=cards or [], end_card_at=end_card_at)
    if not (hook_text or callouts or section_marks):
        return None

    r = cfg()["render"]
    font = r["caption_font"] if (ROOT / r["caption_font_file"]).exists() else "DejaVu Sans"
    W, H = fmt.width, fmt.height
    big = 128 if fmt.is_short else 110
    hook_size = 84 if fmt.is_short else 72
    accent = r.get("accent_colour", "&H0000C8FF")   # ASS BGR: warm amber

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},{hook_size},&H00FFFFFF,&H00FFFFFF,&H64000000,&H96000000,-1,0,0,0,100,100,0,0,1,0,6,8,90,90,{int(H * 0.16)},1
Style: Callout,{font},{big},{accent},{accent},&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,0,8,5,80,80,0,1
Style: Section,{font},{int(hook_size * 0.8)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,2,0,1,0,5,1,90,90,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pop = r"{\fad(120,150)\fscx112\fscy112\t(0,180,0.6,\fscx100\fscy100)}"
    lines = []
    busy_until = 0.0
    if hook_text:
        # thin accent bar above the hook text, both pop in together
        lines.append(f"Dialogue: 2,{_ts(0.05)},{_ts(2.6)},Hook,,0,0,0,,{pop}{_clean(hook_text.upper())}")
        busy_until = 2.7
    y_call = int(H * (0.30 if fmt.is_short else 0.5))
    for t, text in sorted(callouts):
        if t < busy_until:
            t = busy_until
        end = t + 1.9
        lines.append(f"Dialogue: 3,{_ts(t)},{_ts(end)},Callout,,0,0,0,,"
                     f"{{\\pos({W // 2},{y_call})}}{pop}{_clean(text)}")
        busy_until = end + 0.3
    for t, title in section_marks:
        lines.append(f"Dialogue: 2,{_ts(t)},{_ts(t + 2.8)},Section,,0,0,0,,"
                     f"{{\\fad(200,250)\\move(70,{H - 140},90,{H - 140})}}{{\\c{accent}}}▍{{\\c&H00FFFFFF&}}{_clean(title.upper())}")
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out
