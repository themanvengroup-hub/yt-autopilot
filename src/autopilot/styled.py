"""On-screen text layer in the style of the reference reels (Citylights / SIA Properties).

What we took from the references (Video Editing/References, analysed Nov 2026):
  * Phrase subtitles in the centre of the frame, two lines: a white lead-in on top and
    the amber payoff underneath ("Data package laaga / Unlimited FSI chesesaru").
    Heavy geometric sans (Poppins Bold), tight tracking, no box. A soft dark halo keeps
    the text readable on bright footage.
  * When the payoff is a number, it gets bigger ("EOI Price is / ₹6699++").
  * One clean info card per beat-heavy section: white rounded card, amber pill,
    label → value rows (the "PHASE 1 / Contractor / Length / Budget" cards).
  * A small two-tone wordmark in the corner for the whole video.
  * Long-form ends on a black card with a gold serif wordmark and a tagline.

Everything is rendered as one .ass file and burned in by ffmpeg/libass, so it costs
almost nothing at render time and needs no extra Python dependencies on the runner.
"""
from __future__ import annotations

import functools
import logging
import re
from pathlib import Path

from .captions import Word, _clean, _ts
from .config import ROOT, Format, cfg

log = logging.getLogger(__name__)

WHITE = "&H00FFFFFF&"
DARK = "&H00271811&"        # #111827 slate-900 (BGR)
GREY = "&H00807265&"        # #657280
RULE = "&H00EBE7E5&"        # #E5E7EB
BLACK = "&H00000000&"


def _r() -> dict:
    return cfg()["render"]


def _amber() -> str:
    return _r().get("sub_accent", "&H001EA5F8&")   # #F8A51E, the reference orange


def _gold() -> str:
    return _r().get("end_card_gold", "&H004AA6D4&")  # #D4A64A


# ── text measurement (for line breaking and card layout) ─────
@functools.lru_cache(maxsize=8)
def _pil_font(size: int):
    try:
        from PIL import ImageFont
        p = ROOT / _r().get("sub_font_file", "assets/fonts/Poppins-Bold.ttf")
        if p.exists():
            return ImageFont.truetype(str(p), size)
    except Exception:  # noqa: BLE001
        pass
    return None


def text_width(text: str, size: int, spacing: float = 0.0) -> float:
    f = _pil_font(size)
    base = f.getlength(text) if f else len(text) * size * 0.58
    return base + spacing * max(len(text) - 1, 0)


# ── phrase building ──────────────────────────────────────────
_NUM = re.compile(r"[₹$€%]|\d")


# words a phrase should not end on (English + the Hinglish/Telugish connectors we use)
_WEAK_END = {"a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "from", "by",
             "is", "are", "was", "that", "this", "your", "my", "their", "its", "our", "ki", "ka", "ke", "ko",
             "lo", "ga", "se", "aur", "every", "each", "per", "so", "than", "as", "into", "how", "why", "what"}


def _phrases(words: list[Word], max_px: float, size: int, spacing: float, max_words: int = 7) -> list[list[Word]]:
    """Sentences (or pause-separated runs) become phrases; long ones are split recursively at
    the most natural point: after a comma, never after a weak word, roughly balanced."""
    def w_of(ws: list[Word]) -> float:
        return text_width(" ".join(x.text for x in ws), size, spacing)

    def fits(ws: list[Word]) -> bool:
        return len(ws) <= max_words and w_of(ws) <= 1.75 * max_px

    def split(ws: list[Word]) -> list[list[Word]]:
        if fits(ws) or len(ws) < 2:
            return [ws]
        best, best_k = None, len(ws) // 2
        for k in range(1, len(ws)):
            left, right = ws[:k], ws[k:]
            word = left[-1].text.lower().strip(",.;:!?\"'")
            cost = abs(len(left) - len(right)) * 1.0
            if re.search(r"[,;:]$", left[-1].text):
                cost -= 4
            if word in _WEAK_END:
                cost += 6
            if len(left) < 2 or len(right) < 2:
                cost += 5
            if right[0].start - left[-1].end > 0.25:
                cost -= 2                      # the speaker paused here
            if best is None or cost < best:
                best, best_k = cost, k
        return split(ws[:best_k]) + split(ws[best_k:])

    runs: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and w.start - cur[-1].end > 0.5:
            runs.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?]$", w.text):
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [p for run in runs for p in split(run)]


def _split(ph: list[Word], size: int, spacing: float, max_px: float) -> tuple[str, str]:
    """Two balanced lines (white lead-in, amber payoff); short phrases stay on one line."""
    toks = [w.text for w in ph]
    full = " ".join(toks)
    if len(toks) <= 3 and text_width(full, size, spacing) <= 0.8 * max_px:
        return "", full
    best, best_k = None, 1
    for k in range(1, len(toks)):
        a, b = " ".join(toks[:k]), " ".join(toks[k:])
        wa, wb = text_width(a, size, spacing), text_width(b, size, spacing)
        cost = max(wa, wb) + (0.25 * (wa - wb) if wa > wb else 0)  # prefer a shorter lead-in
        if _NUM.search(toks[k]):
            cost -= size * 1.2        # numbers read best at the start of the payoff line
        if re.search(r"[,;:]$", toks[k - 1]):
            cost -= size * 1.5        # natural pause = natural line break
        if toks[k - 1].lower().strip(",.;:!?\"'") in _WEAK_END:
            cost += size * 2.2        # don't leave "of" / "the" hanging at the end of line 1
        if best is None or cost < best:
            best, best_k = cost, k
    return " ".join(toks[:best_k]), " ".join(toks[best_k:])


def _strip_punct(s: str) -> str:
    # the references drop trailing commas/full stops on screen; keep ? and !
    return re.sub(r"[,.;:]+(?=\s|$)", "", s).strip()


# ── ASS helpers ──────────────────────────────────────────────
def rrect(w: float, h: float, r: float) -> str:
    """Rounded rectangle as an ASS vector path, origin top-left."""
    r = min(r, w / 2, h / 2)
    k = 0.5523 * r
    f = lambda v: f"{v:.1f}"  # noqa: E731
    return (f"m {f(r)} 0 l {f(w - r)} 0 b {f(w - r + k)} 0 {f(w)} {f(r - k)} {f(w)} {f(r)} "
            f"l {f(w)} {f(h - r)} b {f(w)} {f(h - r + k)} {f(w - r + k)} {f(h)} {f(w - r)} {f(h)} "
            f"l {f(r)} {f(h)} b {f(r - k)} {f(h)} 0 {f(h - r + k)} 0 {f(h - r)} "
            f"l 0 {f(r)} b 0 {f(r - k)} {f(r - k)} 0 {f(r)} 0")


def _dlg(layer: int, t0: float, t1: float, style: str, text: str) -> str:
    return f"Dialogue: {layer},{_ts(t0)},{_ts(t1)},{style},,0,0,0,,{text}"


def _text_pair(layer: int, t0: float, t1: float, x: int, y: int, text: str, *, size: int,
               colour: str, rise: int = 10, delay_ms: int = 0, halo: float = 1.0) -> list[str]:
    """Text with a soft dark halo underneath (two events), sliding up a few px as it fades in."""
    mv = f"\\an5\\move({x},{y + rise},{x},{y},{delay_ms},{delay_ms + 200})" if rise else f"\\an5\\pos({x},{y})"
    fade = "\\fad(90,80)"
    d0, d1 = delay_ms, delay_ms + 140
    bord = max(size * 0.17, 6) * halo
    halo_rev = f"\\3a&HFF&\\t({d0},{d1},\\3a&H8C&)" if delay_ms else "\\3a&H8C&"
    main_rev = f"\\1a&HFF&\\4a&HFF&\\t({d0},{d1},\\1a&H00&\\4a&H70&)" if delay_ms else "\\4a&H70&"
    halo_tag = (f"{{{mv}{fade}\\fs{size}\\1a&HFF&\\3c{BLACK}{halo_rev}\\bord{bord:.0f}"
                f"\\blur{bord * 1.2:.0f}\\shad0}}")
    main_tag = (f"{{{mv}{fade}\\fs{size}\\1c{colour}\\bord0\\4c{BLACK}{main_rev}\\shad{max(size // 22, 2)}}}")
    return [_dlg(layer, t0, t1, "Sub", halo_tag + text), _dlg(layer + 1, t0, t1, "Sub", main_tag + text)]


# ── components ───────────────────────────────────────────────
def _subtitles(fmt: Format, words: list[Word], busy: list[tuple[float, float]]) -> list[str]:
    W, H = fmt.width, fmt.height
    size = int(_r().get("sub_size_short" if fmt.is_short else "sub_size_long", 72 if fmt.is_short else 74))
    spacing = -1.0
    max_px = W * (0.84 if fmt.is_short else 0.62)
    cy = int(H * (0.60 if fmt.is_short else 0.50))
    low_y = int(H * (0.80 if fmt.is_short else 0.86))       # while an info card is on screen
    lh = int(size * 1.12)
    phrases = _phrases(words, max_px, size, spacing, 6 if fmt.is_short else 8)
    lines: list[str] = []
    for i, ph in enumerate(phrases):
        t0 = max(ph[0].start - 0.06, 0)
        nxt = phrases[i + 1][0].start if i + 1 < len(phrases) else None
        t1 = (nxt - 0.02) if nxt is not None and nxt - ph[-1].end < 0.7 else ph[-1].end + 0.35
        t1 = max(t1, t0 + 0.5)
        a, b = _split(ph, size, spacing, max_px)
        a, b = _clean(_strip_punct(a)), _clean(_strip_punct(b))
        if not (a or b):
            continue
        under_card = any(s - 0.05 <= t0 < e for s, e in busy)
        sz = int(size * 0.82) if under_card else size
        base_y = low_y if under_card else cy
        # big number payoff ("EOI Price is / ₹6699++")
        b_size = int(sz * 1.45) if (b and _NUM.search(b) and len(b.split()) <= 2 and not under_card) else sz
        fit = lambda s, fs: min(1.0, max_px / max(text_width(s, fs, spacing), 1))  # noqa: E731
        if a:
            ya, yb = base_y - lh // 2 - (b_size - sz) // 4, base_y + lh // 2 + (b_size - sz) // 4
            fa = fit(a, sz)
            lines += _text_pair(10, t0, t1, W // 2, ya, a, size=int(sz * fa), colour=WHITE)
            fb = fit(b, b_size)
            lines += _text_pair(10, t0, t1, W // 2, yb, b, size=int(b_size * fb), colour=_amber(), delay_ms=70)
        else:
            colour = _amber() if _NUM.search(b) else WHITE
            fb = fit(b, b_size)
            lines += _text_pair(10, t0, t1, W // 2, base_y, b, size=int(b_size * fb), colour=colour)
    return lines


def _card(fmt: Format, t0: float, t1: float, card: dict) -> list[str]:
    """White info card: amber pill, optional title, label → value rows. Rows cascade in."""
    W, H = fmt.width, fmt.height
    rows = [r for r in (card.get("rows") or []) if isinstance(r, (list, tuple)) and len(r) >= 2][:4]
    if not rows:
        return []
    s = 1.0 if fmt.is_short else 0.9
    cw = 940 if fmt.is_short else 900
    pad, pill_h, row_h = int(52 * s), int(66 * s), int(94 * s)
    title = _clean(str(card.get("title", ""))[:34])
    title_h = int(78 * s) if title else 0
    ch = pad + pill_h + int(18 * s) + title_h + row_h * len(rows) + int(pad * 0.7)
    x0 = (W - cw) // 2
    y0 = int(H * (0.40 if fmt.is_short else 0.47)) - ch // 2
    rise = 36
    mv = lambda x, y: f"\\an7\\move({x},{y + rise},{x},{y},0,260)"  # noqa: E731
    fade = "\\fad(160,160)"
    out: list[str] = []
    # dim the footage so the card reads as a separate layer
    out.append(_dlg(6, t0, t1, "Box", f"{{\\an7\\pos(0,0)\\fad(200,200)\\1c{BLACK}\\1a&H9C&\\p1}}"
                                       f"m 0 0 l {W} 0 l {W} {H} l 0 {H}{{\\p0}}"))
    # soft drop shadow + card
    out.append(_dlg(21, t0, t1, "Box", f"{{{mv(x0, y0 + 14)}{fade}\\1c{BLACK}\\1a&H70&\\blur22\\p1}}"
                                       f"{rrect(cw, ch, 28 * s)}{{\\p0}}"))
    out.append(_dlg(22, t0, t1, "Box", f"{{{mv(x0, y0)}{fade}\\1c{WHITE}\\1a&H06&\\p1}}{rrect(cw, ch, 28 * s)}{{\\p0}}"))
    # pill
    pill = _clean(str(card.get("pill", "KEY FACTS")).upper()[:22])
    pfs = int(36 * s)
    pw = int(text_width(pill, pfs, 1.5) + 52 * s)
    py = y0 + pad
    out.append(_dlg(23, t0, t1, "Box", f"{{{mv(x0 + pad, py)}{fade}\\1c{_amber()}\\p1}}{rrect(pw, pill_h, pill_h / 2)}{{\\p0}}"))
    out.append(_dlg(24, t0, t1, "Card", f"{{\\an4\\move({x0 + pad + int(26 * s)},{py + pill_h // 2 + rise},"
                                        f"{x0 + pad + int(26 * s)},{py + pill_h // 2},0,260){fade}\\fs{pfs}"
                                        f"\\1c{WHITE}\\fsp1.5}}{pill}"))
    y = py + pill_h + int(18 * s)
    if title:
        out.append(_dlg(24, t0, t1, "Card", f"{{\\an4\\move({x0 + pad},{y + title_h // 2 + rise},{x0 + pad},"
                                            f"{y + title_h // 2},0,260){fade}\\fs{int(54 * s)}\\1c{DARK}}}{title}"))
        y += title_h
    label_x, value_x = x0 + pad, x0 + int(cw * 0.42)
    for i, (label, value) in enumerate(rows):
        d = 260 + i * 140   # cascade
        ry = y + i * row_h + row_h // 2
        rv = f"\\alpha&HFF&\\t({d},{d + 180},\\alpha&H00&)"
        lab = _clean(str(label))[:22]
        val = _clean(str(value))[:30]
        out.append(_dlg(24, t0, t1, "Card", f"{{\\an4\\pos({label_x},{ry}){fade}{rv}\\fs{int(38 * s)}\\b0\\1c{GREY}}}{lab}"))
        vfs = int(42 * s)
        vfit = min(1.0, (x0 + cw - pad - value_x) / max(text_width(val, vfs), 1))
        out.append(_dlg(24, t0, t1, "Card", f"{{\\an4\\pos({value_x},{ry}){fade}{rv}\\fs{int(vfs * vfit)}\\1c{DARK}}}{val}"))
        if i < len(rows) - 1:
            out.append(_dlg(23, t0, t1, "Box", f"{{\\an7\\pos({label_x},{ry + row_h // 2}){fade}{rv}\\1c{RULE}\\p1}}"
                                               f"m 0 0 l {cw - 2 * pad} 0 l {cw - 2 * pad} 2 l 0 2{{\\p0}}"))
    return out


def _hook(fmt: Format, text: str) -> list[str]:
    """Opening title (Shorts): big two-tone line at the top, last word in amber."""
    W, H = fmt.width, fmt.height
    toks = _clean(text.upper()).split()
    if not toks:
        return []
    size = 92
    head, tail = " ".join(toks[:-1]), toks[-1]
    full = f"{head} {{\\1c{_amber()}}}{tail}" if head else f"{{\\1c{_amber()}}}{tail}"
    fit = min(1.0, W * 0.88 / max(text_width(" ".join(toks), size), 1))
    sz = int(size * fit)
    pop = "\\fscx112\\fscy112\\t(0,180,0.6,\\fscx100\\fscy100)"
    y = int(H * 0.17)
    bord = sz * 0.17
    return [
        _dlg(30, 0.05, 2.6, "Sub", f"{{\\an5\\pos({W // 2},{y})\\fad(80,160){pop}\\fs{sz}\\1a&HFF&\\3c{BLACK}\\3a&H8C&"
                                   f"\\bord{bord:.0f}\\blur{bord * 1.2:.0f}\\shad0}}{full.replace(_amber(), BLACK)}"),
        _dlg(31, 0.05, 2.6, "Sub", f"{{\\an5\\pos({W // 2},{y})\\fad(80,160){pop}\\fs{sz}\\1c{WHITE}\\shad3\\4a&H70&}}{full}"),
    ]


def _callouts(fmt: Format, callouts: list[tuple[float, str]], start_after: float) -> list[str]:
    W, H = fmt.width, fmt.height
    size = 128 if fmt.is_short else 110
    y = int(H * (0.30 if fmt.is_short else 0.5))
    out, busy = [], start_after
    for t, text in sorted(callouts):
        t = max(t, busy)
        out += _text_pair(15, t, t + 1.9, W // 2, y, _clean(text), size=size, colour=_amber(), rise=0)
        busy = t + 2.2
    return out


def _sections(fmt: Format, marks: list[tuple[float, str]]) -> list[str]:
    W, H = fmt.width, fmt.height
    out = []
    for t, title in marks:
        out.append(_dlg(12, t, t + 2.8, "Lower", f"{{\\an1\\fad(200,250)\\move(70,{H - 120},90,{H - 120})}}"
                                                 f"{{\\1c{_amber()}}}▍{{\\1c{WHITE}}}{_clean(title.upper())}"))
    return out


def _watermark(fmt: Format, until: float) -> list[str]:
    W, H = fmt.width, fmt.height
    size = 34 if fmt.is_short else 30
    name = cfg()["channel"]["name"].upper().split()
    first, rest = name[0], " ".join(name[1:])
    if fmt.is_short:   # top-right is covered by the Shorts player's buttons
        pos = f"\\an7\\pos(56,{int(H * 0.055)})"
    else:
        pos = f"\\an9\\pos({W - 56},44)"
    txt = f"{{{pos}\\fs{size}\\fsp2\\1a&H20&\\shad1\\4a&HA0&\\1c{WHITE}}}{first} {{\\1c{_amber()}}}{rest}"
    return [_dlg(5, 0, until, "Mark", txt)]


def _end_card(fmt: Format, t0: float, dur: float) -> list[str]:
    W, H = fmt.width, fmt.height
    t1 = t0 + dur
    name = _clean(cfg()["channel"]["name"])
    tag = _clean(_r().get("end_card_tagline", "Know the money behind the headlines"))
    serif = _r().get("end_card_font", "DM Serif Display")
    s = 1.0 if not fmt.is_short else 0.9
    cy = H // 2
    return [
        _dlg(40, t0, t1, "Box", f"{{\\an7\\pos(0,0)\\fad(350,0)\\1c{BLACK}\\p1}}m 0 0 l {W} 0 l {W} {H} l 0 {H}{{\\p0}}"),
        _dlg(41, t0 + 0.25, t1, "End", f"{{\\an5\\pos({W // 2},{cy - int(18 * s)})\\fn{serif}\\fs{int(124 * s)}"
                                       f"\\1c{_gold()}\\fad(400,300)\\blur0.6\\fscx94\\fscy94"
                                       f"\\t(0,900,0.5,\\fscx100\\fscy100)}}{name}"),
        _dlg(41, t0 + 0.7, t1, "End", f"{{\\an5\\pos({W // 2},{cy + int(70 * s)})\\fn{serif}\\fs{int(34 * s)}"
                                      f"\\1c&H00B4C8D8&\\fsp1\\fad(400,300)}}{tag}"),
    ]


# ── entry point ──────────────────────────────────────────────
def build(fmt: Format, out: Path, *, words: list[Word], hook_text: str, callouts: list[tuple[float, str]],
          section_marks: list[tuple[float, str]], cards: list[tuple[float, float, dict]],
          end_card_at: float | None) -> Path:
    r = _r()
    font = r.get("sub_font", "Poppins")
    if not (ROOT / r.get("sub_font_file", "assets/fonts/Poppins-Bold.ttf")).exists():
        font = "DejaVu Sans"
        log.warning("subtitle font file missing; falling back to %s", font)
    W, H = fmt.width, fmt.height
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,{font},72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H70000000,-1,0,0,0,100,100,-1,0,1,0,3,5,0,0,0,1
Style: Card,{font},32,&H00271811,&H00271811,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,0,0,4,0,0,0,1
Style: Box,{font},20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Lower,{font},{46 if not fmt.is_short else 52},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,2,0,1,0,4,1,0,0,0,1
Style: Mark,{font},28,&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,2,0,1,0,1,7,0,0,0,1
Style: End,{font},120,&H004AA6D4,&H004AA6D4,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    busy = [(t0, t1) for t0, t1, _ in cards]
    for t0, t1, card in cards:
        lines += _card(fmt, t0, t1, card)
    if words:
        lines += _subtitles(fmt, words, busy)
    else:
        lines += _callouts(fmt, callouts, 2.7 if hook_text else 0.0)
    if hook_text:
        lines += _hook(fmt, hook_text)
    lines += _sections(fmt, section_marks)
    total_end = end_card_at if end_card_at is not None else (words[-1].end + 5 if words else 3600)
    if r.get("watermark", True):
        lines += _watermark(fmt, total_end)
    if end_card_at is not None:
        lines += _end_card(fmt, end_card_at, float(r.get("end_card_seconds", 3.0)))
    out.write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
    return out
