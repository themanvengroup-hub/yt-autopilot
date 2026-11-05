"""Paisa Decoded brand assets: logo badge, corner watermark and the animated end card.

The logo lives at assets/brand/logo.png (the channel's green-and-gold coin, transparent outside
the circle). Replace that file to rebrand; the corner watermark and the end card pick it up
automatically. If the file is missing, a simple fallback coin is generated from code.
"""
from __future__ import annotations

import functools
import logging
import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import ROOT, Format, cfg

log = logging.getLogger(__name__)
LOGO = ROOT / "assets" / "brand" / "logo.png"
NAVY = (10, 20, 44)
GOLD_TOP, GOLD_BOT = (255, 226, 150), (214, 150, 42)
AMBER = (248, 165, 30)


def _font(size: int, serif: bool = False) -> ImageFont.FreeTypeFont:
    r = cfg()["render"]
    cands = ([ROOT / "assets/fonts/DMSerifDisplay-Regular.ttf"] if serif else []) + [
        ROOT / r.get("sub_font_file", "assets/fonts/Poppins-Bold.ttf"),
        ROOT / r.get("caption_font_file", "assets/fonts/Poppins-ExtraBold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")]
    for p in cands:
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def _gradient(w: int, h: int, top=GOLD_TOP, bot=GOLD_BOT) -> Image.Image:
    g = np.linspace(0, 1, h)[:, None, None]
    arr = (np.array(top) * (1 - g) + np.array(bot) * g).repeat(w, 1).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def make_logo(size: int = 1024) -> Image.Image:
    """Navy coin, gold ring, gold ₹ framed by thin white ‹ › brackets."""
    S = size * 2  # supersample
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    c = S / 2
    # soft outer glow
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse((S * .04, S * .04, S * .96, S * .96), fill=110)
    im.paste((214, 150, 42, 255), (0, 0), glow.filter(ImageFilter.GaussianBlur(S * .02)))
    # gold ring (gradient) + navy face with a subtle radial lift
    ring = Image.new("L", (S, S), 0)
    ImageDraw.Draw(ring).ellipse((S * .05, S * .05, S * .95, S * .95), fill=255)
    gold = _gradient(S, S).convert("RGBA")
    im.paste(gold, (0, 0), ring)
    yy, xx = np.mgrid[0:S, 0:S].astype(np.float32)
    rr = np.sqrt((xx - c) ** 2 + (yy - c * .85) ** 2) / (S * .45)
    face = np.stack([NAVY[0] + 26 * (1 - rr), NAVY[1] + 34 * (1 - rr), NAVY[2] + 60 * (1 - rr)], -1).clip(0, 255)
    face_im = Image.fromarray(face.astype(np.uint8), "RGB").convert("RGBA")
    fm = Image.new("L", (S, S), 0)
    ImageDraw.Draw(fm).ellipse((S * .085, S * .085, S * .915, S * .915), fill=255)
    im.paste(face_im, (0, 0), fm)
    # thin inner gold hairline
    ImageDraw.Draw(im).ellipse((S * .115, S * .115, S * .885, S * .885), outline=(214, 150, 42, 170), width=int(S * .006))
    # ₹ in gold gradient
    f = _font(int(S * .50))
    mask = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(mask)
    l, t, r, b = d.textbbox((0, 0), "₹", font=f)
    d.text((c - (l + r) / 2, c - (t + b) / 2 - S * .005), "₹", font=f, fill=255)
    sh = mask.filter(ImageFilter.GaussianBlur(S * .012))
    im.paste((0, 0, 0, 150), (int(S * .006), int(S * .012)), sh)
    im.paste(_gradient(S, S).convert("RGBA"), (0, 0), mask)
    # decode brackets
    bd = ImageDraw.Draw(im)
    w = int(S * .022)
    for sgn in (-1, 1):
        x0 = c + sgn * S * .30
        x1 = c + sgn * S * .245
        bd.line([(x1, c - S * .10), (x0, c), (x1, c + S * .10)], fill=(255, 255, 255, 235), width=w, joint="curve")
        for (x, y) in ((x1, c - S * .10), (x0, c), (x1, c + S * .10)):
            bd.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=(255, 255, 255, 235))
    return im.resize((size, size), Image.LANCZOS)


@functools.lru_cache(maxsize=1)
def logo() -> Image.Image:
    if LOGO.exists():
        return Image.open(LOGO).convert("RGBA")
    log.info("assets/brand/logo.png missing; generating the default Paisa Decoded logo")
    im = make_logo()
    try:
        LOGO.parent.mkdir(parents=True, exist_ok=True)
        im.save(LOGO)
    except OSError:
        pass
    return im


def wordmark(size: int, tracking: float = 0.08) -> Image.Image:
    """'PAISA' white + 'DECODED' amber, letter-spaced, transparent background."""
    f = _font(size)
    words = cfg()["channel"]["name"].upper().split()
    parts = [(words[0], (255, 255, 255)), (" ".join(words[1:]), AMBER)]
    sp = size * tracking
    total = sum(sum(f.getlength(ch) + sp for ch in txt) for txt, _ in parts) + size * 0.35
    im = Image.new("RGBA", (int(total + size), int(size * 1.6)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x = size * 0.5
    for i, (txt, col) in enumerate(parts):
        for ch in txt:
            d.text((x, size * 0.25), ch, font=f, fill=col + (255,))
            x += f.getlength(ch) + sp
        x += size * 0.35 - sp
    return im.crop(im.getbbox())


def _shadowed(im: Image.Image, blur: float, alpha: float = .6, dy: int = 6) -> Image.Image:
    pad = int(blur * 3)
    out = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    a = im.getchannel("A").point(lambda v: int(v * alpha))
    sh = Image.new("L", out.size, 0)
    sh.paste(a, (pad, pad + dy))
    out.paste((0, 0, 0, 255), (0, 0), sh.filter(ImageFilter.GaussianBlur(blur)))
    out.alpha_composite(im, (pad, pad))
    return out


def watermark_png(fmt: Format, out: Path) -> tuple[Path, int, int]:
    """Corner watermark: small logo badge + two-tone wordmark. Returns (png, x, y)."""
    W, H = fmt.width, fmt.height
    badge = 74 if fmt.is_short else 62
    lg = logo().resize((badge, badge), Image.LANCZOS)
    wm = wordmark(30 if fmt.is_short else 26)
    gap = 14
    im = Image.new("RGBA", (badge + gap + wm.width, badge), (0, 0, 0, 0))
    im.alpha_composite(lg, (0, 0))
    im.alpha_composite(wm, (badge + gap, (badge - wm.height) // 2))
    im = _shadowed(im, 6, .55, 2)
    a = im.getchannel("A").point(lambda v: int(v * .92))
    im.putalpha(a)
    im.save(out)
    if fmt.is_short:   # Shorts: top-left (top-right holds the player's buttons)
        x, y = 30, int(H * 0.04)
    else:
        x, y = W - im.width - 30, 22
    return out, x, y


# ── animated end card ────────────────────────────────────────
def _ease_out_back(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    c1 = 1.70158
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def _ease_out(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


def _paste(cv: Image.Image, im: Image.Image, cx: float, cy: float, scale: float = 1.0, alpha: float = 1.0) -> None:
    if alpha <= 0.01 or scale <= 0.01:
        return
    if abs(scale - 1) > 1e-3:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.BILINEAR)
    if alpha < 0.999:
        im = im.copy()
        im.putalpha(im.getchannel("A").point(lambda v: int(v * alpha)))
    cv.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2))) if (
        cx - im.width / 2 >= 0 and cy - im.height / 2 >= 0 and cx + im.width / 2 <= cv.width and cy + im.height / 2 <= cv.height
    ) else cv.paste(im, (int(cx - im.width / 2), int(cy - im.height / 2)), im)


def end_card(fmt: Format, out: Path, seconds: float, fps: int = 30, seed: int = 0) -> Path:
    """Navy backdrop with slow gold light rays and rising particles; the logo lands with a
    bounce and a glow pulse, then wordmark, tagline and a SUBSCRIBE pill cascade in."""
    W, H = fmt.width, fmt.height
    r = cfg()["render"]
    n = int(round(seconds * fps))
    cy = int(H * (0.40 if fmt.is_short else 0.40))
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    rr = np.sqrt(((xx - W / 2) / max(W, H)) ** 2 + ((yy - cy) / max(W, H)) ** 2)
    # deep emerald → near-black, matching the green/gold channel logo
    bg = np.stack([10 - 8 * rr, 38 - 30 * rr, 24 - 18 * rr], -1).clip(0, 255).astype(np.uint8)
    bg_im = Image.fromarray(bg, "RGB").convert("RGBA")
    show_wm = bool(r.get("end_card_wordmark", False))   # the logo already carries the channel name
    logo_px = int(min(W, H) * ((0.46 if show_wm else 0.70) if fmt.is_short else (0.36 if show_wm else 0.52)))
    lg = _shadowed(logo().resize((logo_px, logo_px), Image.LANCZOS), 18, .6, 10)
    wm = _shadowed(wordmark(int(min(W, H) * (0.075 if fmt.is_short else 0.062))), 8, .6, 4)
    tag_f = _font(int(min(W, H) * (0.040 if fmt.is_short else 0.034)), serif=True)
    tagline = r.get("end_card_tagline", "Know the money behind the headlines")
    tb = tag_f.getbbox(tagline)
    tag = Image.new("RGBA", (tb[2] - tb[0] + 20, tb[3] - tb[1] + 20), (0, 0, 0, 0))
    ImageDraw.Draw(tag).text((10 - tb[0], 10 - tb[1]), tagline, font=tag_f, fill=(222, 196, 140, 255))
    pill_f = _font(int(min(W, H) * (0.040 if fmt.is_short else 0.032)))
    label = r.get("end_card_cta", "SUBSCRIBE")
    pb = pill_f.getbbox(label)
    pw, ph = pb[2] - pb[0] + 110, int((pb[3] - pb[1]) * 2.3)
    pill = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    pill_bg = _gradient(pw, ph, (255, 222, 140), (212, 152, 48)).convert("RGBA")
    pm = Image.new("L", (pw, ph), 0)
    ImageDraw.Draw(pm).rounded_rectangle((0, 0, pw - 1, ph - 1), ph // 2, fill=255)
    pill.paste(pill_bg, (0, 0), pm)
    # small bell-ish play triangle on the left of the label
    tx, ty = 38, ph / 2
    ink = (12, 46, 28, 255)   # dark green text on the gold pill
    pd.polygon([(tx - 9, ty - 12), (tx + 13, ty), (tx - 9, ty + 12)], fill=ink)
    pd.text((64 - pb[0], (ph - (pb[3] - pb[1])) / 2 - pb[1]), label, font=pill_f, fill=ink)
    pill = _shadowed(pill, 10, .5, 5)
    rng = np.random.default_rng(seed)
    parts = [(rng.uniform(0, W), rng.uniform(0, H), rng.uniform(40, 160), rng.uniform(1.5, 4.5), rng.uniform(60, 170))
             for _ in range(70)]
    gap = int(min(W, H) * 0.05)
    y_wm = cy + logo_px / 2 + gap + wm.height / 2
    y_tag = (y_wm + wm.height / 2 + gap * 0.5 if show_wm else cy + logo_px / 2 + gap * 0.9) + tag.height / 2
    y_pill = y_tag + tag.height / 2 + gap * 1.1 + pill.height / 2

    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                            "-r", str(fps), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
                            "-pix_fmt", "yuv420p", str(out)], stdin=subprocess.PIPE)
    assert enc.stdin
    small = (W // 4, H // 4)
    for i in range(n):
        t = i / fps
        fr = bg_im.copy()
        fx = Image.new("RGBA", small, (0, 0, 0, 0))
        d = ImageDraw.Draw(fx)
        sx, sy = W / 8, cy / 4
        for k in range(16):   # slow rotating rays
            a = k * math.pi / 8 + t * 0.22
            d.polygon([(sx, sy), (sx + math.cos(a) * 900, sy + math.sin(a) * 900),
                       (sx + math.cos(a + .08) * 900, sy + math.sin(a + .08) * 900)], fill=(212, 170, 80, 16))
        fx = fx.resize((W, H), Image.BILINEAR)
        d2 = ImageDraw.Draw(fx)
        for x, y0, sp, rad, al in parts:   # rising gold dust
            y = (y0 - t * sp) % H
            d2.ellipse((x - rad, y - rad, x + rad, y + rad), fill=(240, 205, 125, int(al)))
        fr.alpha_composite(fx)
        # glow behind the logo, pulsing once when it lands
        pulse = 1 + 0.25 * math.exp(-((t - 0.55) / 0.18) ** 2)
        gr = logo_px * 0.62 * pulse
        gl = Image.new("L", (W // 4, H // 4), 0)
        ImageDraw.Draw(gl).ellipse(((W / 2 - gr) / 4, (cy - gr) / 4, (W / 2 + gr) / 4, (cy + gr) / 4),
                                   fill=int(150 * _ease_out(t / 0.5)))
        fr.paste((232, 180, 80, 255), (0, 0), gl.filter(ImageFilter.GaussianBlur(18)).resize((W, H), Image.BILINEAR))
        _paste(fr, lg, W / 2, cy, 0.35 + 0.65 * _ease_out_back(t / 0.55), min(t / 0.18, 1))
        u = _ease_out((t - 0.55) / 0.4)
        if show_wm:
            _paste(fr, wm, W / 2, y_wm + 24 * (1 - u), 1, u)
        u = _ease_out((t - 0.8) / 0.4)
        _paste(fr, tag, W / 2, y_tag + 20 * (1 - u), 1, u)
        u = (t - 1.05) / 0.35
        _paste(fr, pill, W / 2, y_pill, 0.6 + 0.4 * _ease_out_back(u), min(max(u * 2, 0), 1))
        if t > seconds - 0.4:   # fade to black
            k = (t - (seconds - 0.4)) / 0.4
            fr = Image.blend(fr, Image.new("RGBA", (W, H), (0, 0, 0, 255)), min(k, 1))
        enc.stdin.write(fr.convert("RGB").tobytes())
    enc.stdin.close()
    enc.wait()
    return out
