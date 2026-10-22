#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Generate the PWA icons for MicroBench.

The workbench is installed on a tablet home screen, so it needs:
  icon-192.png            Android / manifest
  icon-512.png            Android / manifest (hi-dpi)
  icon-maskable-512.png   Android adaptive icon (safe zone)
  apple-touch-icon.png    iOS home screen (180x180, opaque, no alpha)

Design: the same "Ink & Linen" palette as the app — warm linen ground,
terracotta accent, the MicroBench wordmark's M/B ligature.

Run:  python microbench/_make_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent / "static"

# "Ink & Linen" palette
LINEN = (247, 244, 237, 255)      # --n-50
PAPER = (253, 252, 249, 255)      # --n-25
TERRACOTTA = (168, 72, 44, 255)   # --accent
INK = (28, 26, 23, 255)           # --n-900
SAND = (192, 168, 130, 255)       # --accent-3

DISPLAY_FONT = "Cormorant Garamond"
UI_FONT = "Plus Jakarta Sans"


def _find_font(*candidates: str) -> str | None:
    """First font file that exists on this machine."""
    for name in candidates:
        for root in (
            Path("C:/Windows/Fonts"),
            Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
        ):
            if not root.exists():
                continue
            for f in root.glob(name):
                return str(f)
    return None


def _font(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _centered(draw: ImageDraw.ImageDraw, xy, text: str, font, fill):
    """Draw text with its own bounding box centred on xy."""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((xy[0] - (right - left) / 2, xy[1] - (bottom - top) / 2), text, font=font, fill=fill)


def _monogram(size: int, *, maskable: bool = False, opaque: bool = True) -> Image.Image:
    """The MicroBench monogram on a linen ground.

    maskable -> keep the glyph inside the 80% safe zone Android crops to.
    opaque   -> drop the alpha channel (iOS forbids transparency in
                apple-touch-icon, it composites onto black instead).
    """
    img = Image.new("RGBA", (size, size), LINEN if opaque else (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # A faint warm inset frame so the flat square reads as a designed tile
    # rather than a blank background.
    inset = size * (0.085 if maskable else 0.065)
    draw.rounded_rectangle(
        [inset, inset, size - inset, size - inset],
        radius=size * 0.055,
        outline=(192, 168, 130, 90),
        width=max(1, int(size * 0.006)),
    )

    disp = _font(_find_font("GARA.TTF", "georgia.ttf", "times.ttf", "cambria.ttc"),
                 int(size * 0.40))

    # "M" in ink, "B" in terracotta — the app's own wordmark treatment.
    # Positions are derived from the real glyph metrics so the two letters
    # never collide and the rule never cuts through the descenders.
    if maskable:
        x_m, x_b, group_y = size * 0.325, size * 0.675, size * 0.415
    else:
        x_m, x_b, group_y = size * 0.330, size * 0.670, size * 0.405

    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((x_m, group_y), "M", font=disp, fill=INK, anchor="mm")
    ld.text((x_b, group_y), "B", font=disp, fill=TERRACOTTA, anchor="mm")
    glyph_box = layer.getbbox() or (0, 0, size, size)

    img = Image.alpha_composite(img, layer)
    draw = ImageDraw.Draw(img)

    # A short terracotta rule under the monogram, aligned with the wordmark.
    bar_w = size * (0.26 if maskable else 0.30)
    bar_h = max(2, int(size * 0.024))
    gap = size * 0.075
    bar_y = glyph_box[3] + gap
    draw.rounded_rectangle(
        [(size / 2 - bar_w / 2, bar_y), (size / 2 + bar_w / 2, bar_y + bar_h)],
        radius=bar_h / 2,
        fill=TERRACOTTA,
    )

    if opaque:
        img = img.convert("RGB")
    return img


def main() -> int:
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("需要 Pillow:  python -m pip install pillow", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    jobs = [
        ("icon-192.png", _monogram(192)),
        ("icon-512.png", _monogram(512)),
        ("icon-maskable-512.png", _monogram(512, maskable=True)),
        ("apple-touch-icon.png", _monogram(180, opaque=True)),
    ]
    for name, img in jobs:
        path = OUT_DIR / name
        img.save(path, "PNG")
        print(f"  {path.name}  {img.size[0]}x{img.size[1]}  {path.stat().st_size} bytes")

    manifest = """{
  "name": "MicroBench · 微电子科研工作台",
  "short_name": "MicroBench",
  "description": "面向微电子与半导体方向的个人学术工作台：文献精读、复现工程、避坑速记与组会周报。",
  "lang": "zh-CN",
  "start_url": "/",
  "scope": "/",
  "display": "standalone",
  "orientation": "any",
  "background_color": "#F7F4ED",
  "theme_color": "#F7F4ED",
  "icons": [
    { "src": "/pwa/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" },
    { "src": "/pwa/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" },
    { "src": "/pwa/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
"""
    mf = OUT_DIR / "manifest.webmanifest"
    mf.write_text(manifest, encoding="utf-8")
    print(f"  {mf.name}  {mf.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
