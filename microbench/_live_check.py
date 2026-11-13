# -*- coding: utf-8 -*-
"""真实链路实测：走**真接口**，把「喂给模型的原文块」和「模型吐回来的译文」都打出来。

跑法：python microbench/_live_check.py <pdf 相对路径> <页码> [--translate]
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"


def api(path: str, payload: dict | None = None) -> dict:
    url = BASE + path
    if payload is None:
        with urllib.request.urlopen(url, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    rel = sys.argv[1].replace("\\", "/")
    page = int(sys.argv[2])
    do_translate = "--translate" in sys.argv

    q = urllib.parse.quote(rel, safe="")
    print("=" * 78)
    print("① 真接口 /api/paper/page_blocks —— 这是**喂给模型的原文**")
    print("=" * 78)
    pb = api(f"/api/paper/page_blocks?relative_path={q}&page={page}")
    blocks = pb.get("blocks") or []
    print("块数 %d" % len(blocks))
    for b in blocks:
        txt = b.get("text") or ""
        print("-" * 70)
        print("[%s] col=%s kind=%s bbox=%s" % (
            b.get("i"), b.get("col"), b.get("kind"),
            [round(float(v), 1) for v in (b.get("bbox") or [])]))
        print(txt)

    if not do_translate:
        return 0

    print()
    print("=" * 78)
    print("② 真调模型 /api/paper/translate_blocks —— **模型吐回来的译文**")
    print("=" * 78)
    tb = api("/api/paper/translate_blocks", {
        "paper_id": "livecheck", "page": page, "relative_path": rel,
        "force": False, "model": "",
    })
    print("from_cache=%s" % tb.get("from_cache"))
    for b in (tb.get("blocks") or []):
        txt = (b.get("translation") or "").strip()
        if not txt:
            continue
        print("-" * 70)
        print("[%s] %s" % (b.get("i"), txt))
    return 0


if __name__ == "__main__":
    sys.exit(main())
