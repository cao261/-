# -*- coding: utf-8 -*-
"""诊断：模型在段落协议下到底返回了什么（定位 502 的真因）。

用法：python microbench/_diag_block_protocol.py [页码]
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from microbench import bilingual  # noqa: E402
from microbench import llm as llm_mod  # noqa: E402
from microbench import paper_text  # noqa: E402

REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
       "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")


def main() -> int:
    page = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    pdf = REPO / REL
    blocks = paper_text.extract_page_blocks(str(pdf), page)
    print("块：", [(b["i"], b["kind"], len(b.get("text") or "")) for b in blocks])
    todo = [b for b in blocks
            if (b.get("text") or "").strip() and b.get("kind") not in ("formula", "table")]
    print("送模型的段号：", [b["i"] for b in todo])

    user_msg = bilingual.build_block_input(todo)
    print("\n----- 输入（前 900 字）-----")
    print(user_msg[:900])

    async def run():
        return await asyncio.to_thread(
            llm_mod.chat,
            messages=[{"role": "system", "content": bilingual._BLOCK_SYS},
                      {"role": "user", "content": user_msg}],
            max_tokens=16000, temperature=0.0, timeout=300,
            disable_thinking=True, model_name="", force_fallback=False,
        )

    raw = asyncio.run(run())
    out = REPO / "logs" / "blockdiag"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"block-raw-p{page}.txt").write_text(raw or "", encoding="utf-8")
    print("\n----- 模型返回（前 3000 字）-----")
    print((raw or "")[:3000])
    print("\n----- 解析 -----")
    got = bilingual.parse_block_translations(raw or "")
    print("parse_block_translations →", {k: v[:40] for k, v in list(got.items())[:6]},
          "共", len(got), "段；期望", len(todo), "段")
    al = bilingual._align_by_paragraphs(raw or "", todo)
    print("_align_by_paragraphs → 共", len(al), "段")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
