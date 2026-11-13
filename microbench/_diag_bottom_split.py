# -*- coding: utf-8 -*-
"""调试：为什么 `_find_bottom_column_split` 认不出合成的两栏正文。

用法：python microbench/_diag_bottom_split.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench import paper_text as pt  # noqa: E402


def _word(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top,
            "bottom": top + 8.0}


def main() -> int:
    height = 780.0
    width = 612.0
    left = ["alpha", "beta", "gamma", "delta", "epsilon"]
    right = ["zeta", "eta", "theta", "iota", "kappa"]
    words = []
    for i in range(6):
        top = height * 0.60 + i * 9.0
        x = 60.0
        for w in left:
            words.append(_word(w, x, x + 40.0, top))
            x += 44.0
        x = 320.0
        for w in right:
            words.append(_word(w, x, x + 40.0, top))
            x += 44.0
    band_h = max(8.0, height / 40.0)
    print("band_h = %.2f" % band_h)
    y = height * 0.45
    while y < height:
        bw = [w for w in words if y <= w["top"] < y + band_h]
        if bw:
            mid = pt._band_column_gap(bw, width)
            print("带 y=%.1f..%.1f  词=%d  缝=%s" % (
                y, y + band_h, len(bw),
                ("%.1f" % mid) if mid is not None else "None"))
            if mid is not None:
                key = int(round(mid / width * pt._X_BINS))
                print("      key=%d  像正文=%s" % (
                    key, pt._looks_like_body_text(bw, mid)))
        y += band_h
    print("最终:", pt._find_bottom_column_split(words, width, height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
