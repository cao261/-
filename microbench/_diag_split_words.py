# -*- coding: utf-8 -*-
"""调试：合成字符能不能被 pdfplumber 的 extract_words 切开。

用法：python microbench/_diag_split_words.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench import paper_text as pt  # noqa: E402


def _char(text, x0, x1, size=7.0):
    return {"text": text, "x0": x0, "x1": x1, "size": size,
            "top": 617.0, "bottom": 625.0, "doctop": 617.0,
            "upright": True, "fontname": "TestFont"}


def main() -> int:
    words = ["Whered", "31andd", "33arepiezoelectricconstants;e,",
             "dielectricconstant"]
    chars = []
    x = 40.0
    for i, w in enumerate(words):
        if i:
            x += 1.4
        for ch in w:
            chars.append(_char(ch, x, x + 3.0))
            x += 3.0
    print("字符数 =", len(chars))
    for tol in (0.5, 1.0, 1.26, 1.4, 2.0):
        try:
            from pdfplumber.utils import extract_words
            ws = extract_words(chars, x_tolerance=tol, y_tolerance=3,
                               keep_blank_chars=False, use_text_flow=False)
            print("tol=%.2f → %d 词: %s" % (
                tol, len(ws), " ".join(w["text"] for w in ws)))
        except Exception as exc:
            print("tol=%.2f → 抛异常: %r" % (tol, exc))
    print("走封装:", pt._split_chars_into_words(chars, 1.26))
    return 0


if __name__ == "__main__":
    sys.exit(main())
