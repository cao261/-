# -*- coding: utf-8 -*-
"""探针：不同 x_tolerance 下，extract_words 能把那条脚注切成什么样。

用法：python microbench/_probe_word_split.py "<pdf>" <页码> <y0> <y1>
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
        for tol in (0.3, 0.5, 0.8, 1.0, 1.2, 1.5):
            ws = band.extract_words(x_tolerance=tol, use_text_flow=False,
                                    keep_blank_chars=False)
            print("tol=%.1f → %d 词" % (tol, len(ws)))
            print("   " + " ".join(w["text"] for w in ws))
        # 逐字符间隙：看看词边界处的间隙是不是明显更大
        chars = band.chars
        print("\n--- 相邻字符的间隙（>0.8pt 的标出来）---")
        prev = None
        for c in chars:
            if prev is not None:
                gap = c["x0"] - prev["x1"]
                if gap > 0.8:
                    print("   gap=%.2f  %r → %r" % (gap, prev["text"], c["text"]))
            prev = c
    return 0


if __name__ == "__main__":
    sys.exit(main())
