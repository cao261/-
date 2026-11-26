# -*- coding: utf-8 -*-
"""临时诊断：看某一页每个块的 kind 与译文（表格块为什么没出来）。"""
from __future__ import annotations

import json
import sys
import urllib.request

REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
       "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")
PAGE = int(sys.argv[1]) if len(sys.argv) > 1 else 2


def main() -> int:
    body = json.dumps({"paper_id": "probe-p%d" % PAGE, "page": PAGE,
                       "relative_path": REL, "force": False, "model": ""}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:5000/api/paper/translate_blocks", data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read().decode("utf-8"))
    for b in d.get("blocks") or []:
        t = (b.get("translation") or "")
        print("[%2s] kind=%-8s len=%5s  %r" % (b["i"], b.get("kind"), len(t), t[:120]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
