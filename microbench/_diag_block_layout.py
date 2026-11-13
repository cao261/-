# -*- coding: utf-8 -*-
"""诊断：打印一本 PDF 每一页的块结构（col/kind/bbox/文本首段）。

用来肉眼核对「块的分栏归位」对不对，特别是纯图页底部的左右两栏。

用法：python microbench/_diag_block_layout.py "<pdf 路径>" [页码...]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.paper_text import extract_page_blocks  # noqa: E402


def main() -> int:
    pdf = Path(sys.argv[1])
    pages = [int(x) for x in sys.argv[2:]] or None
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        npages = len(doc.pages)
        todo = pages or list(range(1, npages + 1))
        for pno in todo:
            if pno > npages:
                continue
            page = doc.pages[pno - 1]
            w = float(page.width)
            blocks = extract_page_blocks(pdf, pno)
            print("=" * 90)
            print("第 %s 页  页宽=%.0f  块数=%d" % (pno, w, len(blocks)))
            for b in blocks:
                bb = b.get("bbox") or [0, 0, 0, 0]
                bw = float(bb[2]) - float(bb[0])
                cross = "  ⚠跨栏缝" if (float(bb[0]) < w * 0.40
                                       and float(bb[2]) > w * 0.60) else ""
                txt = (b.get("text") or "").replace("\n", " ")
                print("  [%2s] col=%-2s kind=%-8s x=[%6.1f,%6.1f] w=%5.1f%s" % (
                    b.get("i"), b.get("col"), b.get("kind"), bb[0], bb[2], bw, cross))
                print("       %r" % txt[:150])
    return 0


if __name__ == "__main__":
    sys.exit(main())
