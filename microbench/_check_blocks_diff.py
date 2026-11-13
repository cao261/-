# -*- coding: utf-8 -*-
"""A/B 比对：改动后的**段落块** vs 改动前（git HEAD）的段落块。

与 `_check_text_unchanged.py`（比页级文本）配套：
  - 页级文本必须**逐字不变** → EXTRACTOR_VERSION 才敢不动；
  - 段落块**允许**变（这一轮就是改块层），但必须能说清**哪些页变了、为什么变**。

跑法：python microbench/_check_blocks_diff.py [pdf 路径 或 目录]
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def load_old():
    """载入 HEAD 里的旧版 paper_text（每次都重新取，避免陈基准假红）。"""
    import importlib
    path = REPO / "microbench" / "_paper_text_old_tmp.py"
    path.write_bytes(subprocess.run(
        ["git", "-C", str(REPO), "show", "HEAD:microbench/paper_text.py"],
        capture_output=True, check=True).stdout)
    spec = importlib.util.spec_from_file_location("microbench._paper_text_old_tmp", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["microbench._paper_text_old_tmp"] = mod
    spec.loader.exec_module(mod)
    return mod


def _key(blocks):
    return [(b.get("col"), b.get("kind"),
             tuple(round(float(v), 1) for v in (b.get("bbox") or [])),
             (b.get("text") or "")) for b in blocks]


def main() -> int:
    old = load_old()
    from microbench import paper_text as new

    arg = sys.argv[1] if len(sys.argv) > 1 else str(REPO / "01_Literature")
    p = Path(arg)
    pdfs = sorted(p.rglob("*.pdf")) if p.is_dir() else [p]

    same = diff = 0
    changed_pages = []
    for pdf in pdfs:
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf)) as doc:
                n = len(doc.pages)
        except Exception:
            continue
        for pno in range(1, n + 1):
            try:
                a = _key(old.extract_page_blocks(pdf, pno))
            except Exception:
                a = [("<EXC>",)]
            try:
                b = _key(new.extract_page_blocks(pdf, pno))
            except Exception:
                b = [("<EXC>",)]
            if a == b:
                same += 1
            else:
                diff += 1
                changed_pages.append((pdf.name, pno, len(a), len(b)))
                if diff <= 8:
                    print("✗ %s 第 %s 页：旧 %d 块 → 新 %d 块" % (
                        pdf.name, pno, len(a), len(b)))
                    for i in range(max(len(a), len(b))):
                        x = a[i] if i < len(a) else None
                        y = b[i] if i < len(b) else None
                        if x != y:
                            print("   [%s] 旧 %s" % (i, x))
                            print("        新 %s" % (y,))
                            break
    print("\n块层比对：一致 %d 页 / 变了 %d 页" % (same, diff))
    for name, pno, na, nb in changed_pages:
        print("   %s 第 %s 页（%d→%d 块）" % (name, pno, na, nb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
