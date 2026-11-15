# -*- coding: utf-8 -*-
"""A/B 比对：改动后的抽取层 vs 改动前（git HEAD）的抽取层。

**逐字**比较同一批真实 PDF 的每一页文本。
只要有一页不一样 → 抽取层被改坏了（这是 EXTRACTOR_VERSION 不动的底线）。

⚠ 基准是 **当前 HEAD** 里那份 `paper_text.py`：
  - 改抽取层 → 跑这个 → 页级文本必须逐字不变 → `EXTRACTOR_VERSION` 才敢不动；
  - 已提交之后 → 这个检查自动变成「工作区 vs 上一次提交」，依然有意义；
  - 刚提交完再跑 → 两侧同一份代码，必然全一致（这时候它证明不了什么，
    但也不会假红）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

VAULT = REPO / "01_Literature"


def load_old():
    """载入 HEAD 里的旧版 paper_text。

    ⚠ 必须放在 microbench 包里导入 —— 旧模块里有相对导入
      （from .paper_fetcher import ...），单独 import 会炸。
    ⚠ **每次都重新取**：旧版副本落在磁盘上，如果只在「文件不存在时」才生成，
      那么 HEAD 变了以后它会一直留着**陈的**基准 → 以后任何抽取层改动
      都会跟一个老版本比 → 明明没动页级文本也报「不一致」＝ 假红。
    """
    import importlib
    import subprocess
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
    old = load_old()
    from microbench import paper_text as new

    same = diff = 0
    checked = 0
    for pdf in sorted(VAULT.rglob("*.pdf")):
        try:
            import pdfplumber
            with pdfplumber.open(str(pdf)) as doc:
                n_pages = len(doc.pages)
        except Exception:
            continue
        # 只取前 3 页（够抓回归，又不用跑几十分钟）
        for page_no in range(1, min(n_pages, 3) + 1):
            try:
                a = old.extract_page_text_from_path(pdf, page_no)
            except Exception:
                a = "<EXC>"
            try:
                b = new.extract_page_text_from_path(pdf, page_no)
            except Exception:
                b = "<EXC>"
            checked += 1
            if a == b:
                same += 1
            else:
                diff += 1
                if diff <= 3:
                    print("✗ %s 第 %s 页不一致（旧 %d 字 / 新 %d 字）" % (
                        pdf.name, page_no, len(a), len(b)))
                    for i, (x, y) in enumerate(zip(a, b)):
                        if x != y:
                            print("   旧 …%s…" % a[max(0, i - 40):i + 40].replace("\n", "\\n"))
                            print("   新 …%s…" % b[max(0, i - 40):i + 40].replace("\n", "\\n"))
                            break
                    else:
                        print("   前缀相同、长度不同")
    print("\n比对 %d 页：一致 %d / 不一致 %d" % (checked, same, diff))
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main())
