# -*- coding: utf-8 -*-
"""探针：确认 extract_text_lines(return_chars=True) 到底给不给 chars。

用法：python microbench/_probe_return_chars.py "<pdf>" <页码> <y0> <y1>
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
        cropped = page.crop((0, y0 - 16, page.width, y1 + 16))
        try:
            raw = cropped.extract_text_lines(x_tolerance=1.5, return_chars=True)
        except TypeError as exc:
            print("extract_text_lines 不支持 return_chars：%s" % exc)
            return 1
        print("行数 =", len(raw))
        for ln in raw[:3]:
            print("  keys =", sorted(ln.keys()))
            print("  text =", repr((ln.get("text") or "")[:120]))
            ch = ln.get("chars")
            print("  chars =", type(ch).__name__, len(ch) if ch is not None else None)
            if ch:
                c0 = ch[0]
                print("  char[0] keys =", sorted(c0.keys()))
                print("  char[0] =", {k: c0.get(k) for k in
                                     ("text", "x0", "x1", "size", "fontname")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
