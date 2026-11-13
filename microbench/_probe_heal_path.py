# -*- coding: utf-8 -*-
"""探针：为什么 _check_heal_invariant.py 报「修复了 0 行」。

逐层查：
  1) _crop_lines 返回几行？行里带 chars 吗？
  2) 带 chars 的行里，有多少行满足 _heal_line_spaces 的**前置条件**？
  3) 真正的 JACERS 脚注行走到哪一步被挡住了？
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pdfplumber  # noqa: E402

from microbench.paper_text import (  # noqa: E402
    _COMMON_EN,
    _crop_lines,
    _heal_line_spaces,
    _split_chars_into_words,
)


def probe(pdf_path: Path, want: str = "") -> None:
    print("=" * 70)
    print(pdf_path.name)
    with pdfplumber.open(str(pdf_path)) as doc:
        for pno, page in enumerate(doc.pages, 1):
            lines = _crop_lines(page, (0, 0, float(page.width), float(page.height)))
            n_chars = sum(1 for ln in lines if ln.get("chars"))
            healed = [ln for ln in lines if _heal_line_spaces(ln) != (ln.get("text") or "")]
            print("  p%s: 行 %d（带chars %d）→ 触发修复 %d"
                  % (pno, len(lines), n_chars, len(healed)))
            for ln in healed:
                print("     旧 %r" % (ln.get("text") or "")[:100])
                print("     新 %r" % _heal_line_spaces(ln)[:100])
            if want:
                for ln in lines:
                    if want in (ln.get("text") or ""):
                        print("  [命中 %s] p%s" % (want, pno))
                        t = ln.get("text") or ""
                        print("     text=%r" % t[:140])
                        print("     chars=%d  max_token=%d"
                              % (len(ln.get("chars") or []),
                                 max((len(x) for x in t.split()), default=0)))
                        cs = [c for c in (ln.get("chars") or []) if (c.get("text") or "")]
                        cs.sort(key=lambda c: float(c.get("x0") or 0.0))
                        gaps = []
                        for i in range(1, len(cs)):
                            g = float(cs[i].get("x0") or 0.0) - float(cs[i - 1].get("x1") or 0.0)
                            if g > 0.05:
                                gaps.append(round(g, 3))
                        print("     正间隙 %d 个: %s" % (len(gaps), sorted(set(gaps))[:20]))
                        hits = sum(1 for x in t.split()
                                   if x.strip(".,;:'()[]").lower() in _COMMON_EN)
                        print("     原有常见词 %d" % hits)
                        for tol in (0.4, 0.6, 0.8, 1.0, 1.2, 1.35, 1.4):
                            w = _split_chars_into_words(cs, tol)
                            if not w:
                                continue
                            h = sum(1 for x in w
                                    if x.strip(".,;:'()[]").lower() in _COMMON_EN)
                            print("     tol=%.2f → %d 词 / 最长 %d / 常见词 %d : %s"
                                  % (tol, len(w), max(len(x) for x in w), h,
                                     " ".join(w)[:90]))


if __name__ == "__main__":
    arg = sys.argv[1]
    want = sys.argv[2] if len(sys.argv) > 2 else ""
    p = Path(arg)
    if p.is_dir():
        for f in sorted(p.rglob("*.pdf")):
            try:
                probe(f, want)
            except Exception as e:
                print("  !! %s" % e)
    else:
        probe(p, want)
