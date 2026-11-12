# -*- coding: utf-8 -*-
"""段落块抽取的**诊断 + 可视化**脚本（2026-11-12）。

用途：证明「段落块抓得准不准」——这决定「译文能不能放回原文位置」。

输出：
  1) 每页的块统计（总数 / 分栏数 / 各栏块数 / 平均块高）
  2) 一张 PNG：原页 + 每个段落块的框（左栏蓝、右栏绿、整宽橙）+ 序号

跑法：
    python microbench/_diag_page_blocks.py --pdf "<pdf路径>" --pages 3 --out logs/blockdiag
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench import paper_text  # noqa: E402


COLORS = {"L": (37, 99, 235), "R": (16, 133, 96), "S": (217, 119, 6)}
LABEL_BG = {"L": (37, 99, 235), "R": (16, 133, 96), "S": (217, 119, 6)}


def render(pdf_path: Path, page_no: int, blocks: list[dict], out_png: Path) -> bool:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("  (没有 PyMuPDF，跳过渲染)")
        return False
    try:
        doc = fitz.open(str(pdf_path))
        page = doc.load_page(page_no - 1)
        zoom = 2.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        import io
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        draw = ImageDraw.Draw(img)
        pw = float(page.rect.width)
        ph = float(page.rect.height)
        for b in blocks:
            x0, y0, x1, y1 = b["bbox"]
            col = COLORS.get(b.get("col") or "S", COLORS["S"])
            # PDF 点坐标 → 像素
            box = [x0 * zoom, y0 * zoom, x1 * zoom, y1 * zoom]
            draw.rectangle(box, outline=col, width=3)
            # 序号标签
            lab = str(b.get("i", ""))
            tw = max(18, 12 + 8 * len(lab))
            tx = min(max(box[0] - 2, 0), img.width - tw - 2)
            ty = max(box[1] - 20, 0)
            draw.rectangle([tx, ty, tx + tw, ty + 18], fill=LABEL_BG.get(b.get("col") or "S", LABEL_BG["S"]))
            draw.text((tx + 4, ty + 3), lab, fill=(255, 255, 255))
        out_png.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_png)
        doc.close()
        return True
    except Exception as e:
        print("  渲染失败：%s" % e)
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", default="1-4", help="页码范围，如 3 或 1-4")
    ap.add_argument("--out", default="logs/blockdiag")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print("找不到 PDF：%s" % pdf_path)
        return 1
    if "-" in args.pages:
        a, b = args.pages.split("-", 1)
        pages = range(int(a), int(b) + 1)
    else:
        pages = [int(args.pages)]

    out_dir = Path(args.out)
    print("=== %s ===" % pdf_path.name)
    for p in pages:
        blocks = paper_text.extract_page_blocks(pdf_path, p)
        cols = {}
        for b in blocks:
            cols[b.get("col")] = cols.get(b.get("col"), 0) + 1
        kinds = {}
        for b in blocks:
            kinds[b.get("kind")] = kinds.get(b.get("kind"), 0) + 1
        heights = [b["bbox"][3] - b["bbox"][1] for b in blocks] or [0]
        print("\n第 %d 页：%d 块" % (p, len(blocks)))
        print("  分栏：%s" % (cols or "-"))
        print("  类型：%s" % (kinds or "-"))
        print("  块高：中位 %.1fpt / 最大 %.1fpt" % (
            sorted(heights)[len(heights) // 2], max(heights)))
        for b in blocks:
            print("   #%-2d %-6s %-4s %-7s bbox=%s  %s" % (
                b["i"], b.get("col"), b.get("kind"), b.get("split"),
                [round(v, 1) for v in b["bbox"]],
                (b.get("text") or "")[:52].replace("\n", " ")))
        png = out_dir / ("%s_p%d.png" % (pdf_path.stem[:40], p))
        if render(pdf_path, p, blocks, png):
            print("  图：%s" % png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
