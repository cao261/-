# -*- coding: utf-8 -*-
"""扫一页页的段落块，找带表格/公式的页（用来验表格渲染）。

用法：python microbench/_scan_table_pages.py
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:5000"
PDFS = [
    "01_Literature/04_Survey_综述/01_论文原文_PDF/"
    "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf",
]


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    for rel in PDFS:
        q = urllib.parse.quote(rel, safe="")
        try:
            info = get(f"/api/pdf/info?relative_path={q}")
        except Exception as e:
            print("info 失败:", e)
            continue
        pages = int(info.get("pages") or 0)
        print(f"\n=== {Path(rel).name}  共 {pages} 页 ===")
        for p in range(1, pages + 1):
            try:
                pb = get(f"/api/paper/page_blocks?relative_path={q}&page={p}")
            except Exception as e:
                print(f"  p{p}: 失败 {e}")
                continue
            blocks = pb.get("blocks") or []
            kinds = {}
            for b in blocks:
                kinds[b.get("kind")] = kinds.get(b.get("kind"), 0) + 1
            if kinds.get("table") or kinds.get("formula"):
                print(f"  p{p}: {kinds}  共 {len(blocks)} 块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
