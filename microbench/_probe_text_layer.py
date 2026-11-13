# -*- coding: utf-8 -*-
"""探针：看某一页某个 y 带里，PDF 文本层**到底给了什么**。

用法：python microbench/_probe_text_layer.py "<pdf>" <页码> <y0> <y1>
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def main() -> int:
    pdf = Path(sys.argv[1])
    pno = int(sys.argv[2])
    y0 = float(sys.argv[3])
    y1 = float(sys.argv[4])
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        page = doc.pages[pno - 1]
        band = page.crop((0, y0, page.width, y1))
        print("--- extract_text() ---")
        print(repr(band.extract_text()))
        print("--- extract_words() (x_tolerance=1.5) ---")
        for w in band.extract_words(x_tolerance=1.5, use_text_flow=False,
                                    keep_blank_chars=False):
            print("  x=[%7.2f,%7.2f] top=%7.2f  %r" % (
                w["x0"], w["x1"], w["top"], w["text"]))
        print("--- chars（前 60 个，看有没有空格字符）---")
        chars = band.chars
        print("  字符数 =", len(chars))
        for c in chars[:60]:
            print("    x=%7.2f  %r  font=%s size=%.1f" % (
                c["x0"], c["text"], (c.get("fontname") or "")[:28], c.get("size", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
