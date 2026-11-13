# -*- coding: utf-8 -*-
"""探针：连字（U+FB01 ﬁ）在 pdfplumber 里的展开行为。

现象（块层 A/B）：
  旧 "dualelectric-field-andmagnetic-field-tunable"
  新 "dual electric-fifield- and magnetic-fifield-tunable"
  → 重切之后**多出了 "fi"**。

怀疑：PDF 里那个位置是一个连字字符 U+FB01（ﬁ），
      原始行文本里它被渲染成 "fi"（1 个字符 → 2 个字母），
      而按间隙重切时又把它当成了一个独立 token，
      于是 "electric-ﬁfield" 变成 "electric-" + "fi" + "field" …
      或 "electric-" + "ﬁ"→"fi" + "field-" 粘回一起 → "electric-fifield"。

跑法：python microbench/_probe_ligature.py "<pdf>" <页码> "<要匹配的片段>"
"""
from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pdfplumber  # noqa: E402
from pdfplumber.utils import extract_words  # noqa: E402

from microbench.paper_text import _crop_lines, _split_chars_into_words  # noqa: E402


def main() -> int:
    pdf_path = Path(sys.argv[1])
    pno = int(sys.argv[2])
    needle = sys.argv[3] if len(sys.argv) > 3 else ""
    with pdfplumber.open(str(pdf_path)) as doc:
        page = doc.pages[pno - 1]
        lines = _crop_lines(page, (0, 0, float(page.width), float(page.height)))
        for ln in lines:
            text = ln.get("text") or ""
            if needle and needle not in text:
                continue
            print("=" * 70)
            print("行文本: %r" % text)
            cs = [c for c in (ln.get("chars") or []) if (c.get("text") or "")]
            cs.sort(key=lambda c: float(c.get("x0") or 0.0))
            print("字符数 %d" % len(cs))
            # 打印非 ASCII 字符
            for i, c in enumerate(cs):
                t = c.get("text") or ""
                if any(ord(ch) > 127 for ch in t):
                    print("  [%d] %r  U+%s  name=%s  x0=%.2f x1=%.2f"
                          % (i, t, " ".join("%04X" % ord(ch) for ch in t),
                             unicodedata.name(t[0], "?"),
                             float(c.get("x0") or 0), float(c.get("x1") or 0)))
            # 在 needle 附近打印字符级细节
            if needle:
                idx = text.find(needle)
                if idx >= 0:
                    print("--- needle 位置 %d，前后的原始字符 ---" % idx)
                    # 找字符序列里对应的位置（按文本拼接）
                    acc = ""
                    for i, c in enumerate(cs):
                        acc += c.get("text") or ""
                    print("拼接长度 %d" % len(acc))
                    lo = max(0, idx - 12)
                    hi = min(len(acc), idx + len(needle) + 12)
                    for i in range(lo, hi):
                        c = cs[i]
                        print("   %3d %r  x0=%.2f x1=%.2f"
                              % (i, c.get("text"),
                                 float(c.get("x0") or 0), float(c.get("x1") or 0)))
            # 重切结果
            for tol in (0.4, 0.8, 1.2, 1.35, 1.45):
                w = _split_chars_into_words(cs, tol)
                if w:
                    print("  tol=%.2f → %s" % (tol, " ".join(w)[:220]))
            # 原始行文本是怎么来的：再跑一次 extract_words（x_tol=1.5）
            w15 = extract_words(cs, x_tolerance=1.5, y_tolerance=3,
                                keep_blank_chars=False, use_text_flow=False)
            print("  原始切词(x_tol=1.5) → %s"
                  % " ".join((x.get("text") or "") for x in w15)[:220])
    return 0


if __name__ == "__main__":
    sys.exit(main())
