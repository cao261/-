# -*- coding: utf-8 -*-
"""诊断：某一页的块层 A/B 差异（哪一块变了、是不是底部拆栏造成的）。

用法：python microbench/_diag_page_split.py "<pdf>" <页码>
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def load_old():
    path = REPO / "microbench" / "_paper_text_old_tmp.py"
    path.write_bytes(subprocess.run(
        ["git", "-C", str(REPO), "show", "HEAD:microbench/paper_text.py"],
        capture_output=True, check=True).stdout)
    spec = importlib.util.spec_from_file_location("microbench._paper_text_old_tmp", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["microbench._paper_text_old_tmp"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    pdf = Path(sys.argv[1])
    pno = int(sys.argv[2])
    old = load_old()
    from microbench import paper_text as new

    a = old.extract_page_blocks(pdf, pno)
    b = new.extract_page_blocks(pdf, pno)
    print("旧 %d 块 / 新 %d 块" % (len(a), len(b)))

    # 底部拆栏有没有触发？
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        page = doc.pages[pno - 1]
        words = page.extract_words(x_tolerance=new.PDF_X_TOLERANCE,
                                   use_text_flow=False, keep_blank_chars=False)
        got = new._find_bottom_column_split(words, float(page.width), float(page.height))
        print("底部拆栏判定: %s" % (got,))
        print("页宽 %.1f 页高 %.1f  词数 %d" % (page.width, page.height, len(words)))

    print("\n--- 旧块 ---")
    for i, x in enumerate(a):
        print("  [%2d] %s %s %s  %r" % (i, x.get("col"), x.get("kind"),
                                         [round(float(v), 1) for v in (x.get("bbox") or [])],
                                         (x.get("text") or "")[:70]))
    print("\n--- 新块 ---")
    for i, x in enumerate(b):
        print("  [%2d] %s %s %s  %r" % (i, x.get("col"), x.get("kind"),
                                         [round(float(v), 1) for v in (x.get("bbox") or [])],
                                         (x.get("text") or "")[:70]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
