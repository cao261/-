# -*- coding: utf-8 -*-
"""生成 APK 图标资源（从工作台的 PWA 图标缩放而来）。"""
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "microbench" / "static" / "icon-512.png"
RES = HERE / "res"

# 各密度下 launcher 图标的标称尺寸
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

base = Image.open(SRC).convert("RGBA")
# 圆形遮罩（Android 8+ 会自己套形状，但圆形看着更干净）
for name, size in DENSITIES.items():
    d = RES / f"mipmap-{name}"
    d.mkdir(parents=True, exist_ok=True)
    img = base.resize((size, size), Image.LANCZOS)
    img.save(d / "ic_launcher.png")
    print(f"  {d.name}/ic_launcher.png  {size}x{size}")
print("ok")
