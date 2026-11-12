# -*- coding: utf-8 -*-
"""实测：整块公式 / 表格在段落译文里长什么样，服务端裁图接口能不能出图。

用法：python microbench/_probe_formula.py
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "logs" / "blockdiag"
BASE = "http://127.0.0.1:5000"

PDF = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
       "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    rel = PDF.replace("\\", "/")
    q = urllib.parse.quote(rel, safe="")
    pb = get(f"/api/paper/page_blocks?relative_path={q}&page=3")
    blocks = pb.get("blocks") or []
    print(f"blocks_version={pb.get('blocks_version')} extractor={pb.get('extractor_version')}")

    tb = post("/api/paper/translate_blocks", {
        "paper_id": "probe-jacers", "page": 3,
        "relative_path": rel, "force": False, "model": "",
    })
    trs = {b["i"]: b.get("translation", "") for b in (tb.get("blocks") or [])}

    for b in blocks:
        kind = b.get("kind")
        t = trs.get(b["i"], "")
        flag = ""
        if kind in ("formula", "table"):
            flag = "  <<<"
        print(f"[{b['i']:>2}] col={b['col']} kind={kind:<7} bbox={b.get('bbox')} "
              f"textlen={len(b.get('text') or '')}{flag}")
        if kind in ("formula", "table"):
            print(f"      译文 = {t[:200]!r}")

    # 服务端裁图
    OUT.mkdir(parents=True, exist_ok=True)
    for b in blocks:
        if b.get("kind") != "formula":
            continue
        bb = b.get("bbox") or []
        if len(bb) != 4:
            print("  bbox 不是 4 元组，跳过")
            continue
        rect = ",".join(f"{float(x):.1f}" for x in bb)
        url = (f"/api/paper/formula_image?relative_path={urllib.parse.quote(rel, safe='')}"
               f"&page=3&rect={urllib.parse.quote(rect, safe=',')}&scale=4")
        with urllib.request.urlopen(BASE + url, timeout=120) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type")
        p = OUT / f"probe-formula-p3-{b['i']}.png"
        p.write_bytes(data)
        print(f"  裁图 {p.name}: {len(data)} bytes, {ctype}, rect={rect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
