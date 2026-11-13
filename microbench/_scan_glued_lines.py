# -*- coding: utf-8 -*-
"""统计：全库有多少行是「没有空格字符、词粘在一起」的。

用法：python microbench/_scan_glued_lines.py [pdf 路径 或 目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.paper_text import _crop_lines, extract_page_blocks  # noqa: E402


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "01_Literature")
    p = Path(arg)
    pdfs = sorted(p.rglob("*.pdf")) if p.is_dir() else [p]
    total_lines = 0
    glued_lines = 0
    glued_blocks = 0
    samples = []
    for pdf in pdfs:
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf)) as doc:
                n = len(doc.pages)
        except Exception:
            continue
        for pno in range(1, n + 1):
            try:
                blocks = extract_page_blocks(pdf, pno)
            except Exception:
                continue
            for b in blocks:
                for ln in (b.get("text") or "").split("\n"):
                    ln = ln.strip()
                    if len(ln) < 20:
                        continue
                    total_lines += 1
                    toks = ln.split()
                    if max((len(t) for t in toks), default=0) >= 18:
                        glued_lines += 1
                        glued_blocks += 1
                        if len(samples) < 25:
                            samples.append((pdf.name, pno, ln[:150]))
    print("长行 %d 条，其中「粘在一起」的 %d 条（%.1f%%）" % (
        total_lines, glued_lines, 100.0 * glued_lines / max(1, total_lines)))
    for name, pno, s in samples:
        print("  %s p%s  %r" % (name[:38], pno, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
