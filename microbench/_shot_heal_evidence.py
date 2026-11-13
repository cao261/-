# -*- coding: utf-8 -*-
"""生成「空格修复前后」的文字对照取证图。

跑法：python microbench/_shot_heal_evidence.py
产出：logs/blockdiag/heal-evidence.png
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pdfplumber  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from microbench.paper_text import _crop_lines, _heal_line_spaces  # noqa: E402

VAULT = REPO / "01_Literature" / "04_Survey_综述" / "01_论文原文_PDF"
JACERS = VAULT / "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf"
MEC = VAULT / "Magnetoelectric Composites.pdf"

CJK = r"C:\Windows\Fonts\msyh.ttc"
MONO = r"C:\Windows\Fonts\consola.ttf"

BG = (250, 250, 252)
INK = (32, 34, 38)
MUTED = (120, 124, 132)
BAD = (176, 42, 42)
GOOD = (26, 110, 66)
LINE = (222, 224, 230)


def _find_line(pdf: Path, pno: int, needle: str):
    with pdfplumber.open(str(pdf)) as doc:
        page = doc.pages[pno - 1]
        for ln in _crop_lines(page, (0, 0, float(page.width), float(page.height))):
            if needle in (ln.get("text") or ""):
                return ln.get("text") or "", _heal_line_spaces(ln)
    return None, None


def main() -> int:
    samples = []
    old, new = _find_line(JACERS, 2, "piezoelectricconstants")
    if old:
        samples.append((
            "① 表格脚注「没有空格字符」（JACERS 综述 第 2 页）",
            old, new))
    old, new = _find_line(MEC, 15, "dualelectric-field")
    if old:
        samples.append((
            "② 参考文献段落（Magnetoelectric Composites 第 15 页）",
            old, new))
    old, new = _find_line(MEC, 9, "dependenceofMEvoltage")
    if old:
        samples.append((
            "③ 正文段落（Magnetoelectric Composites 第 9 页）",
            old, new))

    W = 1500
    PAD = 46
    head_h = 92
    row_gap = 30
    f_title = ImageFont.truetype(CJK, 27)
    f_label = ImageFont.truetype(CJK, 19)
    f_body = ImageFont.truetype(MONO, 17)
    f_small = ImageFont.truetype(CJK, 16)

    # 先算高度
    rows = []
    for title, old, new in samples:
        o_lines = textwrap.wrap(old, width=105) or [""]
        n_lines = textwrap.wrap(new, width=105) or [""]
        box_h = (44 + 26 + len(o_lines) * 24 + 6 + 26 + len(n_lines) * 24 + 18)
        rows.append((title, o_lines, n_lines, box_h))
    H = head_h + sum(r[3] + row_gap for r in rows) + PAD

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.text((PAD, 30), "空格修复前后对照（块层 · 页级文本逐字未变）",
           font=f_title, fill=INK)
    d.text((PAD, 66),
           "根因：该 PDF 的文本层根本没有空格字符 —— 词边界只靠 1.33~1.48pt 的位置间隙，"
           "pdfplumber 固定 x_tolerance=1.5 把它判成了字母间距",
           font=f_small, fill=MUTED)

    y = head_h
    for title, o_lines, n_lines, box_h in rows:
        d.rectangle([PAD, y, W - PAD, y + box_h], fill=(255, 255, 255),
                    outline=LINE, width=1)
        d.text((PAD + 18, y + 14), title, font=f_label, fill=INK)
        yy = y + 44
        d.text((PAD + 18, yy), "修复前", font=f_label, fill=BAD)
        yy += 26
        for ln in o_lines:
            d.text((PAD + 108, yy), ln, font=f_body, fill=BAD)
            yy += 24
        yy += 6
        d.text((PAD + 18, yy), "修复后", font=f_label, fill=GOOD)
        yy += 26
        for ln in n_lines:
            d.text((PAD + 108, yy), ln, font=f_body, fill=GOOD)
            yy += 24
        y += box_h + row_gap

    out = REPO / "logs" / "blockdiag" / "heal-evidence.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print("已生成 %s（%d 字节）" % (out, out.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
