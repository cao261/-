# -*- coding: utf-8 -*-
"""调试：逐条打印 _heal_line_spaces 的判定过程（找出是哪一条验收没过）。

用法：python microbench/_diag_heal.py "<pdf>" <页码> <y0> <y1>
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.paper_text import _COMMON_EN, _crop_lines  # noqa: E402


def main() -> int:
    pdf = Path(sys.argv[1])
    pno = int(sys.argv[2])
    y0 = float(sys.argv[3])
    y1 = float(sys.argv[4])
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        page = doc.pages[pno - 1]
        lines = _crop_lines(page, (0, y0, page.width, y1))
    for ln in lines:
        text = ln.get("text") or ""
        if "piezoelectric" not in text:
            continue
        print("原文本: %r" % text)
        ascii_n = sum(1 for c in text if ord(c) < 128)
        print("① ASCII 比例 = %d/%d = %.2f" % (ascii_n, len(text),
                                            ascii_n / max(1, len(text))))
        orig = text.split()
        print("   原 token 数=%d 最长=%d" % (len(orig), max(len(t) for t in orig)))
        cs = [c for c in (ln.get("chars") or []) if c.get("text")]
        cs.sort(key=lambda c: float(c.get("x0") or 0.0))
        sizes = sorted(float(c.get("size") or 0.0) for c in cs if c.get("size"))
        size = sizes[len(sizes) // 2] if sizes else 0.0
        print("   字符数=%d 中位字号=%.2f" % (len(cs), size))
        gaps = []
        for i in range(1, len(cs)):
            g = float(cs[i].get("x0") or 0.0) - float(cs[i - 1].get("x1") or 0.0)
            if g > 0.01:
                gaps.append(g)
        gs = sorted(gaps)
        print("   间隙数=%d  最小=%.2f 最大=%.2f" % (len(gs), gs[0], gs[-1]))
        print("   间隙分布:", " ".join("%.2f" % g for g in gs))
        lo_need = max(0.8, size * 0.13)
        hi_cap = size * 0.40
        print("   lo_need=%.2f hi_cap=%.2f" % (lo_need, hi_cap))
        thresh = None
        for i in range(len(gs) - 1):
            lo, hi = gs[i], gs[i + 1]
            if lo < 0.2:
                continue
            if hi >= lo * 1.35 and lo_need <= hi <= hi_cap:
                thresh = hi * 0.9
                print("   跳变命中: lo=%.2f hi=%.2f → 阈值 %.3f" % (lo, hi, thresh))
                break
        if thresh is None:
            print("   ✗ 没找到跳变 → 不修")
            # 手动看看固定阈值会怎样
            for t in (0.9, 1.0, 1.1, 1.2, 1.3, 1.4):
                words, cur, prev = [], [], None
                for c in cs:
                    if prev is not None:
                        g = float(c.get("x0") or 0.0) - float(prev.get("x1") or 0.0)
                        if g > t:
                            words.append("".join(cur)); cur = []
                    cur.append(c.get("text")); prev = c
                if cur:
                    words.append("".join(cur))
                words = [w for w in words if w]
                hits = sum(1 for w in words
                           if w.strip(".,;:'()[]").lower() in _COMMON_EN)
                print("      t=%.2f → %d 词，最长=%d，均长=%.2f，常见词=%d" % (
                    t, len(words), max(len(w) for w in words),
                    sum(len(w) for w in words) / max(1, len(words)), hits))
                print("         " + " ".join(words)[:160])
            return 1
        words, cur, prev = [], [], None
        for c in cs:
            if prev is not None:
                g = float(c.get("x0") or 0.0) - float(prev.get("x1") or 0.0)
                if g > thresh:
                    words.append("".join(cur)); cur = []
            cur.append(c.get("text")); prev = c
        if cur:
            words.append("".join(cur))
        words = [w for w in words if w]
        hits = sum(1 for w in words if w.strip(".,;:'()[]").lower() in _COMMON_EN)
        print("   重切 → %d 词，最长=%d，均长=%.2f，常见词=%d" % (
            len(words), max(len(w) for w in words),
            sum(len(w) for w in words) / max(1, len(words)), hits))
        print("   " + " ".join(words)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
