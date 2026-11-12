# -*- coding: utf-8 -*-
"""诊断：全库扫一遍，看「章节标题」判据切得准不准。

用法：
    python microbench/_diag_headings.py            # 扫全库
    python microbench/_diag_headings.py --limit 8   # 只看前 8 篇
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from microbench.paper_text import extract_page_blocks  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    pdfs = sorted((REPO / "01_Literature").rglob("*.pdf"))
    if limit:
        pdfs = pdfs[:limit]

    total_blocks = 0
    total_headings = 0
    pages = 0
    samples: list[str] = []
    per_paper: list[tuple[str, int, int]] = []

    for pdf in pdfs:
        h_cnt = 0
        b_cnt = 0
        p_cnt = 0
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf)) as doc:
                npages = min(len(doc.pages), 12)
                for pno in range(1, npages + 1):
                    blocks = extract_page_blocks(pdf, pno)
                    if not blocks:
                        continue
                    p_cnt += 1
                    b_cnt += len(blocks)
                    for b in blocks:
                        if b.get("kind") == "heading":
                            h_cnt += 1
                            if len(samples) < 60:
                                samples.append(
                                    "  %s p%-3d %s" % (
                                        pdf.name[:38], pno,
                                        (b.get("text") or "").replace("\n", " | ")[:90]))
        except Exception as e:  # pragma: no cover - 诊断脚本
            print(f"  ! {pdf.name}: {e}")
            continue
        pages += p_cnt
        total_blocks += b_cnt
        total_headings += h_cnt
        if p_cnt:
            per_paper.append((pdf.name[:48], h_cnt, b_cnt))

    print("=" * 72)
    print("扫了 %d 篇 / %d 页 → %d 块，其中 heading %d 块 (%.1f%%)"
          % (len(per_paper), pages, total_blocks, total_headings,
             (100.0 * total_headings / total_blocks) if total_blocks else 0))
    print("=" * 72)
    for name, h, b in per_paper:
        if h:
            print("  %-50s heading %2d / 块 %3d" % (name, h, b))
    print("-" * 72)
    print("切出来的标题样例：")
    for s in samples:
        print(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
