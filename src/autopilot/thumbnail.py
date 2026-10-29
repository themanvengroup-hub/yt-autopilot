"""Long-form thumbnail: a B-roll frame plus a dark gradient and 2-4 huge words.

This is a solid baseline. Swap it for a hand-made design (or add your face) on
the videos you expect to do best, because the thumbnail drives CTR more than anything else.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from .config import ROOT, cfg

W, H = 1280, 720


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in (ROOT / cfg()["render"]["caption_font_file"],
              Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")):
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def make_thumbnail(video: Path, at: float, text: str, out: Path) -> Path:
    frame = out.with_suffix(".frame.png")
    subprocess.run(["ffmpeg", "-y", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1", str(frame)],
                   check=True, capture_output=True)
    img = Image.open(frame).convert("RGB")
    img = img.resize((W, int(img.height * W / img.width))) if img.width != W else img
    top = max((img.height - H) // 2, 0)
    img = img.crop((0, top, W, top + H))
    img = ImageEnhance.Contrast(ImageEnhance.Color(img).enhance(1.35)).enhance(1.15)

    # left-to-right dark gradient so text reads on any footage
    grad = Image.new("L", (W, 1))
    for x in range(W):
        grad.putpixel((x, 0), int(220 * max(0.0, 1 - x / (W * 0.75))))
    shade = Image.new("RGB", (W, H), (0, 0, 0))
    img = Image.composite(shade, img, grad.resize((W, H)))

    draw = ImageDraw.Draw(img)
    words = text.upper().strip()
    lines = textwrap.wrap(words, width=max(8, len(words) // 2 + 1))[:3]
    size = 150 if len(lines) <= 2 else 118
    font = _font(size)
    y = (H - len(lines) * int(size * 1.05)) // 2
    for i, line in enumerate(lines):
        fill = (255, 229, 0) if i == len(lines) - 1 else (255, 255, 255)
        draw.text((60, y), line, font=font, fill=fill, stroke_width=10, stroke_fill=(0, 0, 0))
        y += int(size * 1.05)

    img.save(out, "JPEG", quality=90, optimize=True)  # YouTube limit is 2 MB
    frame.unlink(missing_ok=True)
    return out
