# -*- coding: utf-8 -*-
"""探针：连字在 pdfplumber 各层的表现（到底哪一层多了一个 fi）。

层次：
  A. page.chars            —— 原始字符
  B. page.crop(...).chars  —— 裁剪后字符
  C. extract_text_lines(return_chars=True) 的 line["chars"]
  D. line["text"]
  E. extract_words(chars) 的结果

跑法：python microbench/_probe_ligature_layers.py "<pdf>" <页码>
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pdfplumber  # noqa: E402
from pdfplumber.utils import extract_words  # noqa: E402


def show(tag, chars):
    hits = [(i, c.get("text"), float(c.get("x0") or 0))
            for i, c in enumerate(chars)
            if any(ord(ch) > 127 for ch in (c.get("text") or ""))]
    print("  %-34s 字符 %3d  非ASCII %d" % (tag, len(chars), len(hits)))
    for i, t, x0 in hits[:8]:
        print("       [%d] %r U+%s x0=%.2f"
              % (i, t, " ".join("%04X" % ord(ch) for ch in t), x0))


def main() -> int:
    pdf_path = Path(sys.argv[1])
    pno = int(sys.argv[2])
    with pdfplumber.open(str(pdf_path)) as doc:
        page = doc.pages[pno - 1]
        show("A. page.chars", page.chars)
        box = (0, 0, float(page.width), float(page.height))
        cropped = page.crop(box)
        show("B. cropped.chars", cropped.chars)
        lines = cropped.extract_text_lines(x_tolerance=1.5, return_chars=True)
        lc = []
        for ln in lines:
            lc.extend(ln.get("chars") or [])
        show("C. line['chars'] 汇总", lc)
        for ln in lines:
            txt = ln.get("text") or ""
            if "electric" in txt:
                print("  D. line['text']      = %r" % txt[:110])
                cs = [c for c in (ln.get("chars") or []) if (c.get("text") or "")]
                cs.sort(key=lambda c: float(c.get("x0") or 0.0))
                w = extract_words(cs, x_tolerance=1.5, y_tolerance=3,
                                  keep_blank_chars=False, use_text_flow=False)
                print("  E. extract_words     = %r"
                      % " ".join((x.get("text") or "") for x in w)[:110])
                w2 = extract_words(cs, x_tolerance=1.5, y_tolerance=3,
                                   keep_blank_chars=False, use_text_flow=False,
                                   expand_ligatures=False)
                print("  F. words(no expand) = %r"
                      % " ".join((x.get("text") or "") for x in w2)[:110])
                # 文本层里 'ﬁ' 到底在第几个字符
                joined = "".join((c.get("text") or "") for c in cs)
                print("  G. chars 拼接         = %r" % joined[:110])
                import inspect
                from pdfplumber.utils import text as T
                sig = inspect.signature(T.WordExtractor.__init__)
                print("  H. expand_ligatures 默认 = %r" % sig.parameters["expand_ligatures"].default)
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
