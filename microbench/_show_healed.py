# -*- coding: utf-8 -*-
"""打印所有被修复的行（旧 → 新），便于人工核对修复是否正确。

跑法：python microbench/_show_healed.py [pdf 路径 或 目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pdfplumber  # noqa: E402

from microbench.paper_text import _crop_lines, _heal_line_spaces  # noqa: E402


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "01_Literature")
    p = Path(arg)
    pdfs = sorted(p.rglob("*.pdf")) if p.is_dir() else [p]
    total = 0
    for pdf in pdfs:
        try:
            with pdfplumber.open(str(pdf)) as doc:
                for pno, page in enumerate(doc.pages, 1):
                    try:
                        lines = _crop_lines(page, (0, 0, float(page.width),
                                                   float(page.height)))
                    except Exception:
                        continue
                    for ln in lines:
                        text = ln.get("text") or ""
                        new = _heal_line_spaces(ln)
                        if new == text:
                            continue
                        total += 1
                        print("--- %s p%s" % (pdf.name[:46], pno))
                        print("  旧 %s" % text)
                        print("  新 %s" % new)
        except Exception:
            continue
    print("共 %d 行被修复" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
