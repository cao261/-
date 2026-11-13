# -*- coding: utf-8 -*-
"""检查：空格修复**只加空格、不改字符**。

不变式：`healed.replace(" ", "") == original.replace(" ", "")`
—— 任何一行违反它，就说明修复动到了字符本身（必须查清）。

跑法：python microbench/_check_heal_invariant.py [pdf 路径 或 目录]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.paper_text import _crop_lines, _heal_line_spaces  # noqa: E402


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "01_Literature")
    p = Path(arg)
    pdfs = sorted(p.rglob("*.pdf")) if p.is_dir() else [p]
    healed_lines = 0
    bad = 0
    samples = []
    import pdfplumber
    for pdf in pdfs:
        try:
            # ⚠ 必须在 with 块**内**遍历 page —— 文档一关，page 对象就失效，
            #   page.crop() 会抛异常 → 静默跳过 → 「修复了 0 行」的假阴性
            #   （2026-11-13 踩过：脚本这么写，JACERS 明明修复了 1 行，
            #     脚本报 0 行，差点把「不变式成立」当成结论）。
            with pdfplumber.open(str(pdf)) as doc:
                for pno, page in enumerate(doc.pages, 1):
                    try:
                        lines = _crop_lines(page, (0, 0, float(page.width),
                                                   float(page.height)))
                    except Exception:
                        continue
                    for ln in lines:
                        text = ln.get("text") or ""
                        new = _heal_line_spaces(ln)
                        if new == text:
                            continue
                        healed_lines += 1
                        if new.replace(" ", "") != text.replace(" ", ""):
                            bad += 1
                            if len(samples) < 12:
                                samples.append((pdf.name, pno, text[:200], new[:200]))
        except Exception:
            continue
    print("修复了 %d 行；其中**字符被改动**的：%d 行" % (healed_lines, bad))
    for name, pno, old, new in samples:
        print("  %s p%s" % (name[:40], pno))
        print("     旧 %r" % old)
        print("     新 %r" % new)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
