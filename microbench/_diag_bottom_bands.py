# -*- coding: utf-8 -*-
"""诊断：底部逐带投票的细节（为什么表格页也被当成两栏正文）。

用法：python microbench/_diag_bottom_bands.py "<pdf>" <页码>
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pdfplumber  # noqa: E402

from microbench import paper_text as pt  # noqa: E402


def main() -> int:
    pdf = Path(sys.argv[1])
    pno = int(sys.argv[2])
    with pdfplumber.open(str(pdf)) as doc:
        page = doc.pages[pno - 1]
        width, height = float(page.width), float(page.height)
        words = page.extract_words(x_tolerance=pt.PDF_X_TOLERANCE,
                                   use_text_flow=False, keep_blank_chars=False)
        clean = []
        for w in words:
            ww = w["x1"] - w["x0"]
            hh = w["bottom"] - w["top"]
            if ww < 30 and hh > 2 * max(1.0, ww):
                continue
            clean.append(w)
        band_h = max(8.0, height / 40.0)
        y = height * 0.45
        print("band_h=%.2f  页宽 %.1f" % (band_h, width))
        while y < height:
            bw = [w for w in clean if y <= w["top"] < y + band_h]
            if bw:
                mid = pt._band_column_gap(bw, width)
                if mid is not None:
                    key = int(round(mid / width * pt._X_BINS))
                    left = [w for w in bw if w["x1"] <= mid]
                    right = [w for w in bw if w["x0"] >= mid]
                    body = pt._looks_like_body_text(bw, mid)
                    # 左栏/右栏的 x0 分布（真两栏：x0 聚拢；宽表格：x0 散开）
                    lx0 = [round(w["x0"], 1) for w in left]
                    rx1 = [round(w["x1"], 1) for w in right]
                    print("带 y=%6.1f..%6.1f 词=%3d 缝=%6.1f key=%3d 左%d右%d 像正文=%s"
                          % (y, y + band_h, len(bw), mid, key, len(left), len(right), body))
                    print("     左 x0: %s" % (lx0[:14],))
                    print("     右 x1: %s" % (rx1[:14],))
                    print("     样词: %s" % (" ".join(w["text"] for w in bw)[:110],))
            y += band_h
    print("最终:", pt._find_bottom_column_split(words, width, height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
