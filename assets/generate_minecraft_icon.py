"""Generate a Minecraft crafting-table icon for MicroBench (multi-size ICO).

Palette inspired by vanilla Minecraft texture (texture-atlas /assets/minecraft/textures/block/crafting_table_*).
"""
from PIL import Image, ImageDraw


# Minecraft-style palette (hex)
WOOD_DARK   = (0x4A, 0x33, 0x20, 255)  # darkest shadow
WOOD_MID    = (0x7A, 0x4F, 0x2A, 255)  # mid wood
WOOD_LIGHT  = (0x9C, 0x6B, 0x3E, 255)  # base wood
WOOD_HIGHL  = (0xC6, 0x8E, 0x55, 255)  # highlight
GRID_FILL   = (0xE8, 0xE1, 0xD0, 255)  # cream-ish (3x3 grid cells)
GRID_SHADE  = (0xB5, 0xA7, 0x8C, 255)  # grid cell shade
IRON_DARK   = (0x37, 0x37, 0x37, 255)  # saw / iron dark
IRON_LIGHT  = (0x80, 0x80, 0x80, 255)  # saw / iron mid
TOOL_RED    = (0xA0, 0x36, 0x2A, 255)  # red accent


def _draw_crafting_table(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 32.0  # scale factor
    px = lambda x: int(round(x * s))

    # Outer wood panel (top-down view, like crafting_table_top.png)
    # Background wood plane (whole body)
    d.rectangle([px(2), px(5), px(29), px(28)], fill=WOOD_LIGHT)
    # Top edge highlight
    d.rectangle([px(2), px(5), px(29), px(7)], fill=WOOD_HIGHL)
    # Bottom edge shadow
    d.rectangle([px(2), px(26), px(29), px(28)], fill=WOOD_MID)
    # Left edge highlight
    d.rectangle([px(2), px(5), px(4), px(28)], fill=WOOD_HIGHL)
    # Right edge shadow
    d.rectangle([px(27), px(5), px(29), px(28)], fill=WOOD_MID)

    # Wood grain lines (horizontal streaks)
    for y in (9, 14, 18, 22):
        d.line([(px(4), px(y)), (px(27), px(y))], fill=WOOD_HIGHL, width=px(1))
    for y in (11, 16, 20):
        d.line([(px(4), px(y)), (px(27), px(y))], fill=WOOD_MID, width=px(1))

    # Dark border around the 3x3 grid
    grid_x1, grid_y1 = px(7), px(8)
    grid_x2, grid_y2 = px(24), px(25)
    d.rectangle([grid_x1 - px(1), grid_y1 - px(1), grid_x2 + px(1), grid_y2 + px(1)],
                fill=WOOD_DARK)

    # 3x3 grid (each cell 5x5 in 32-scale)
    for i in range(3):
        for j in range(3):
            x1 = px(7 + i * 6)
            y1 = px(8 + j * 6)
            x2 = x1 + px(5)
            y2 = y1 + px(5)
            d.rectangle([x1, y1, x2, y2], fill=GRID_FILL)
            # subtle shade on top-left corner
            d.line([(x1, y1), (x2, y1)], fill=GRID_SHADE, width=px(1))
            d.line([(x1, y1), (x1, y2)], fill=GRID_SHADE, width=px(1))

    # A subtle red highlight dot in top-left grid cell (mimicking
    # Minecraft crafting_table_top's red center mark)
    d.rectangle([px(8), px(9), px(10), px(11)], fill=TOOL_RED)
    # A tiny iron-grey saw / hammer mark in center cell
    d.line([(px(13), px(13)), (px(16), px(13))], fill=IRON_LIGHT, width=px(1))
    d.line([(px(13), px(13)), (px(13), px(15))], fill=IRON_DARK, width=px(1))

    return img


def main() -> None:
    out = "D:/Desktop/杂物/琐事/个人工作台/assets/microbench_icon.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base = _draw_crafting_table(256)  # draw once at max size, PIL will downsample
    base.save(out, format="ICO", sizes=sizes)
    # also write a 256x256 PNG for webview / favicon use
    base.save("D:/Desktop/杂物/琐事/个人工作台/assets/microbench_icon.png")
    print(f"Wrote {out} + microbench_icon.png (sizes {sizes})")


if __name__ == "__main__":
    main()