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

    model = WhisperModel(cfg()["render"]["whisper_model"], device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        str(wav), language="en", word_timestamps=True, vad_filter=True,
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
