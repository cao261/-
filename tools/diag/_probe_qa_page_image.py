# -*- coding: utf-8 -*-
"""探针：划**译文栏**里的句子提问时，AI 到底有没有收到页面截图。

用户 2026-11-17 报：「划译文的词句问问题，因为公式是直接截图放在译文里的，
ai 看上下文的时候完全看不到公式推导，只能猜」。

本探针**不发真的 LLM 请求**（Playwright 拦截并 abort），只看前端发出去的
payload 里有没有 `page_image`。

跑法：python microbench/_probe_qa_page_image.py
"""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:5000"
DEFAULT_REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
               "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")


def main() -> int:
    rel = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REL).replace("\\", "/")
    url = BASE + "/?open_pdf=" + urllib.parse.quote(rel, safe="")

    captured: list[dict] = []

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        pg = ctx.new_page()

        def on_request(route):
            try:
                body = route.request.post_data or ""
                captured.append(json.loads(body))
            except Exception as e:
                captured.append({"__parse_error": str(e)})
            route.abort()

        pg.route("**/api/paper/explain_selection_stream", on_request)
        pg.goto(url, wait_until="domcontentloaded", timeout=120000)
        pg.wait_for_timeout(15000)

        print("== 页面状态 ==")
        info = pg.evaluate("""() => ({
          pdfState: !!(window._pdfState),
          pages: (window._pdfState && window._pdfState.pages || []).length,
          translated: (window._pdfState && window._pdfState.pages || [])
              .filter(p => p.translated).length,
          withBlocks: (window._pdfState && window._pdfState.pages || [])
              .filter(p => p.blocks && p.blocks.length).length,
          transBlocks: document.querySelectorAll('.pdf-translation-block').length,
        })""")
        print(json.dumps(info, ensure_ascii=False))

        # ---- 在**译文栏**里划一句 ----
        picked = pg.evaluate("""() => {
          const blocks = document.querySelectorAll('.pdf-translation-block');
          const walker = document.createTreeWalker(
            document.body, NodeFilter.SHOW_TEXT, null);
          for (const b of blocks) {
            const t = b.querySelector('.page-text');
            if (!t) continue;
            // 在这个块里找一个够长的文本节点
            const w = document.createTreeWalker(t, NodeFilter.SHOW_TEXT, null);
            let n;
            while ((n = w.nextNode())) {
              const text = n.textContent || "";
              if (text.trim().length < 40) continue;
              const range = document.createRange();
              range.setStart(n, 0);
              range.setEnd(n, Math.min(30, text.length));
              const sel = window.getSelection();
              sel.removeAllRanges();
              sel.addRange(range);
              const r = range.getBoundingClientRect();
              return {ok: true, text: text.slice(0, 30),
                      x: r.left + r.width / 2, y: r.top + r.height / 2,
                      inTransBlock: !!t.closest('.pdf-translation-block')};
            }
          }
          return {ok: false};
        }""")
        print("== 译文栏划选 ==", picked)
        if picked.get("ok"):
            pg.mouse.move(picked["x"], picked["y"])
            pg.mouse.down()
            pg.mouse.up()
            pg.wait_for_timeout(1200)
            # 看看 _readerSelState 判成了什么
            st = pg.evaluate("""() => ({
              source: (window._readerSelState || {}).source,
              pageNum: (window._readerSelState || {}).pageNum,
              len: ((window._readerSelState || {}).text || '').length,
            })""")
            print("== _readerSelState ==", st)

            # 触发提问（后端请求会被拦截，不会真的调模型）
            pg.evaluate("""() => {
              const inp = document.getElementById('qa-question-input');
              if (inp) { inp.value = '这里的公式是怎么推出来的？'; }
              if (typeof submitReaderQA === 'function') submitReaderQA();
            }""")
            pg.wait_for_timeout(6000)

        browser.close()

    print("\n== 拦截到的答疑请求 ==")
    if not captured:
        print("（没有拦截到请求）")
        return 2
    for c in captured:
        img = c.get("page_image")
        rects = c.get("formula_rects") or []
        print(json.dumps({
            "source_type": c.get("source_type"),
            "page_num": c.get("page_num"),
            "has_page_image": bool(img),
            "page_image_len": len(img) if isinstance(img, str) else 0,
            # ⚠ 公式坐标：服务端按它裁**高清公式图**给模型
            #   （整页图上公式只有 ~18px 高，读不出来 → AGENTS §4.97）
            "formula_rects_count": len(rects) if isinstance(rects, list) else 0,
            "formula_rects": rects[:4] if isinstance(rects, list) else [],
            "ctx_len": len(c.get("surrounding_context") or ""),
            "sel_len": len(c.get("selected_text") or ""),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
