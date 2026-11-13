# -*- coding: utf-8 -*-
"""探针：打印某一页所有块的文本（看空格修复的效果）。

用法：python microbench/_probe_block_text.py "<pdf 路径>" [页码]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.paper_text import extract_page_blocks  # noqa: E402


def main() -> int:
    pdf = Path(sys.argv[1])
    pages = [int(x) for x in sys.argv[2:]] or [2]
    for pno in pages:
        print("=" * 80)
        print("第 %s 页" % pno)
        for b in extract_page_blocks(pdf, pno):
            print("[%2s] col=%-2s kind=%-8s %r" % (
                b["i"], b["col"], b["kind"], (b.get("text") or "")[:260]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
