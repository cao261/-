# -*- coding: utf-8 -*-
"""诊断：找「整页大图 + 底部一条两栏窄条」的页（单栏分支漏切栏缝的那种）。

判据（全部在**块层/几何层**，不涉及模型）：
  1) 这一页走了「单栏分支」—— 投票法探不到栏缝（x_mid is None）；
  2) 但页面**下部**确实有一道明显的栏缝（下部词的 x 覆盖直方图中间有零覆盖段）；
  3) 且下部两边的词都够多（各 ≥ 8 个），即确实是一段两栏正文。

满足 1+2+3 的页 = 「本该分两栏、却被并成一个宽块」的页。

用法：python microbench/_diag_single_col_slit.py [pdf 路径 或 目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.paper_text import (  # noqa: E402
    _X_BINS, _find_vertical_split, _longest_gap, _vote_column_gap,
    _x_cover, extract_page_blocks,
)


def _page_is_double_col(words, width, height, y_split) -> bool:
    """全局（或逐带投票）能不能探到栏缝。"""
    below = [w for w in words if w["top"] >= y_split]
    if len(below) >= 8:
        cover = _x_cover(below, width, _X_BINS)
        a, ln = _longest_gap(cover, int(_X_BINS * 0.15), int(_X_BINS * 0.85))
        if a is not None and ln >= 2:
            g0 = width * a / _X_BINS
            g1 = width * (a + ln) / _X_BINS
            if g1 - g0 >= width * 0.01:
                left_n = sum(1 for w in below if w["x1"] <= g0 + 1)
                right_n = sum(1 for w in below if w["x0"] >= g1 - 1)
                if left_n >= 8 and right_n >= 8:
                    return True
    return _vote_column_gap(words, width, height, y_split) is not None


def _bottom_has_slit(words, width, height) -> tuple[bool, str]:
    """页面下部有没有一道能分成两栏的缝。"""
    y0 = height * 0.55
    low = [w for w in words if w["top"] >= y0]
    if len(low) < 16:
        return False, "下部词太少(%d)" % len(low)
    cover = _x_cover(low, width, _X_BINS)
    a, ln = _longest_gap(cover, int(_X_BINS * 0.25), int(_X_BINS * 0.75))
    if a is None:
        return False, "下部没有零覆盖段"
    g0 = width * a / _X_BINS
    g1 = width * (a + ln) / _X_BINS
    left_n = sum(1 for w in low if w["x1"] <= g0 + 1)
    right_n = sum(1 for w in low if w["x0"] >= g1 - 1)
    if left_n < 8 or right_n < 8:
        return False, "下部两边词数不够(%d/%d)" % (left_n, right_n)
    return True, "缝在 x=%.0f..%.0f (左%d词/右%d词)" % (g0, g1, left_n, right_n)


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
                    try:
                        words = page.extract_words(
                            x_tolerance=1.5, use_text_flow=False,
                            keep_blank_chars=False)
                    except Exception:
                        continue
                    if len(words) < 40:
                        continue
                    width, height = float(page.width), float(page.height)
                    y_split = _find_vertical_split(words, width, height)
                    if _page_is_double_col(words, width, height, y_split):
                        continue          # 正常双栏页，不是目标
                    ok, why = _bottom_has_slit(words, width, height)
                    if not ok:
                        continue
                    hits += 1
                    print("=" * 80)
                    print("%s  第 %s 页" % (pdf.name, pno))
                    print("   下部有栏缝: %s" % why)
                    blocks = extract_page_blocks(pdf, pno)
                    for b in blocks:
                        bb = b.get("bbox") or [0, 0, 0, 0]
                        txt = (b.get("text") or "").replace("\n", " ")
                        print("   [%2s] col=%-2s kind=%-8s bbox=%s" % (
                            b.get("i"), b.get("col"), b.get("kind"),
                            [round(float(v), 1) for v in bb]))
                        print("        %r" % txt[:160])
        except Exception as exc:
            print("跳过 %s: %s" % (pdf.name, exc))
    print("\n共命中 %d 页" % hits)
    return 0


if __name__ == "__main__":
    sys.exit(main())
