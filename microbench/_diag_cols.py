# -*- coding: utf-8 -*-
"""临时诊断：打印某一页每个块的 col/kind/bbox（查表格归栏对不对）。"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
       "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")
PAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 2

q = urllib.parse.quote(REL, safe="")
with urllib.request.urlopen(
        f"http://127.0.0.1:5000/api/paper/page_blocks?relative_path={q}&page={PAGE}",
        timeout=300) as r:
    d = json.loads(r.read().decode("utf-8"))
for b in d.get("blocks") or []:
    txt = (b.get("text") or "").replace("\n", " ")[:52]
    print("[%2s] col=%-2s kind=%-8s bbox=%s  %r" %
          (b["i"], b.get("col"), b.get("kind"), b.get("bbox"), txt))
