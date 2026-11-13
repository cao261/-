# -*- coding: utf-8 -*-
"""诊断：找「本该是左右两栏、却被并成一个宽块」的段落块。

**精确判据**（不用猜、不用模型）：
  拿这个宽块自己的 bbox 重新抽词 → 如果词的 x 覆盖直方图**中间**有一道
  零覆盖段，且缝两边各有 ≥ 8 个词 → 说明这块里其实藏着两栏正文，
  是被并起来的（症状：译文位置横跨两栏）。

用法：python microbench/_diag_wide_blocks.py [pdf 路径 或 目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.paper_text import (  # noqa: E402
    _X_BINS, _longest_gap, _x_cover, extract_page_blocks,
)


def _block_has_inner_gutter(page, bb) -> tuple[bool, str]:
    try:
        words = page.crop(tuple(bb)).extract_words(
            x_tolerance=1.5, use_text_flow=False, keep_blank_chars=False)
    except Exception as exc:
        return False, "抽词失败 %s" % exc
    if len(words) < 16:
        return False, "词太少(%d)" % len(words)
    width = float(bb[2]) - float(bb[0])
    if width <= 0:
        return False, "宽度为 0"
    cover = _x_cover(words, width, _X_BINS)
    a, ln = _longest_gap(cover, int(_X_BINS * 0.25), int(_X_BINS * 0.75))
    if a is None:
        return False, "中间没有零覆盖段"
    g0 = width * a / _X_BINS
    g1 = width * (a + ln) / _X_BINS
    left_n = sum(1 for w in words if w["x1"] <= g0 + 2)
    right_n = sum(1 for w in words if w["x0"] >= g1 - 2)
    if left_n < 8 or right_n < 8:
        return False, "缝两边词数不够(%d/%d)" % (left_n, right_n)
    return True, "块内栏缝在 x=%.0f..%.0f (左%d词/右%d词)" % (
        g0, g1, left_n, right_n)


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "01_Literature")
    p = Path(arg)
    pdfs = sorted(p.rglob("*.pdf")) if p.is_dir() else [p]
    hits = 0
    for pdf in pdfs:
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf)) as doc:
                for pno, page in enumerate(doc.pages, 1):
                    w = float(page.width)
                    try:
                        blocks = extract_page_blocks(pdf, pno)
                    except Exception:
                        continue
                    for b in blocks:
                        bb = b.get("bbox") or [0, 0, 0, 0]
                        bw = float(bb[2]) - float(bb[0])
                        if b.get("col") != "S" or bw < w * 0.62:
                            continue
                        ok, why = _block_has_inner_gutter(page, bb)
                        if not ok:
                            continue
                        hits += 1
                        txt = (b.get("text") or "").replace("\n", " ")
                        print("=" * 82)
                        print("%s  第 %s 页  块#%s kind=%s bbox=%s" % (
                            pdf.name, pno, b.get("i"), b.get("kind"),
                            [round(float(v), 1) for v in bb]))
                        print("   %s" % why)
                        print("   %r" % txt[:220])
        except Exception as exc:
            print("跳过 %s: %s" % (pdf.name, exc))
    print("\n共命中 %d 块" % hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
