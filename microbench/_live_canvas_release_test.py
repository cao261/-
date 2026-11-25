# -*- coding: utf-8 -*-
"""真机端到端：画布位图「按需渲染 + 远端释放」在**真 PDF** 上是否成立。

为什么要有这个脚本（2026-11-25 审计 P1）：
  整页位图 = 2200×2800×4B ≈ **24MB/页**（DPR=2）→ 36 页论文 ≈ **880MB**
  → 长论文直接把标签页撑爆（白屏/OOM）。
  修法：位图**只在靠近视口时**存在，滚远了释放，滚回来重画。

⚠ 为什么护栏抓不到这个问题（这才是它值得单独存在的原因）：
  第一版实现是「画布创建时按尺寸分配 → 不画也占显存」——
  护栏只看「这一页画了没有」，**位图尺寸照样是 733×948**，
  显存一点没省。**只有真浏览器 + 真 PDF 才量得到这件事**。
  （本脚本第一版就抓到了这个洞：canvas.width 必须是 0 才算「真没占」。）

跑法（需要服务在 5000 端口起着）：
    python microbench/_live_canvas_release_test.py [pdf 相对路径]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"
# 默认用 36 页的 JAP 综述（长论文才有显存压力；8 页的看不出问题）
REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
       "2008-JAP-multiferroelectrics-review.pdf")

fail: list[str] = []
ok: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    (ok if cond else fail).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (("  → " + extra) if extra else ""))


def main() -> int:
    rel = (sys.argv[1] if len(sys.argv) > 1 else REL).replace("\\", "/")
    from playwright.sync_api import sync_playwright

    url = BASE + "/?open_pdf=" + urllib.parse.quote(rel, safe="")
    print("打开：" + url[:150])
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  device_scale_factor=1)
        pg = ctx.new_page()
        pg.on("console", lambda m: errors.append("[console] " + m.text)
              if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append("[pageerror] " + str(e)))
        pg.goto(url, wait_until="domcontentloaded", timeout=120000)

        # 等阅读器渲染出来（页块出现）
        pg.wait_for_selector("#pdf-page-block-1", timeout=120000)
        time.sleep(4.0)

        total = pg.evaluate(
            "() => document.querySelectorAll('.pdf-page-block').length")
        check("整篇 PDF 的页块都建出来了（DOM 结构保留）",
              bool(total and total > 8), "页数=" + str(total))

        def canvas_info(page_num: int):
            return pg.evaluate(
                """(n) => {
                    const b = document.getElementById('pdf-page-block-' + n);
                    if (!b) return null;
                    const c = b.querySelector('canvas.pdf-page-canvas');
                    if (!c) return null;
                    return {w: c.width, h: c.height,
                            cssW: c.style.width, cssH: c.style.height,
                            painted: !!(c.width && c.height)};
                }""", page_num)

        p1 = canvas_info(1)
        check("第 1 页（视口内）画布有位图", bool(p1 and p1["painted"]),
              json.dumps(p1) if p1 else "null")

        far = None
        for n in (total - 1, total - 2, total // 2):
            info = canvas_info(n)
            # ⚠ 判据是「**位图尺寸为 0**」，不是「没标记 painted」——
            #   一个 width=733 的空画布照样占着 2.7MB 显存。
            if info is not None and not info["painted"] and info["w"] == 0:
                far = (n, info)
                break
        check("远处页的画布是空的（位图已释放 → 显存不被长论文撑爆）",
              far is not None,
              # ⚠ 成功/失败要给**不同的**文案：早先这里无论成败都打印失败那句
              #   「查了 N 页都没找到空画布」→ 一眼看过去像红点，其实是通过的。
              ("已释放：第 %d 页 canvas.width=%d" % (far[0], far[1]["w"]))
              if far is not None
              else ("查了 " + str(total) + " 页都没找到空画布"))

        if far:
            n = far[0]
            pg.evaluate(
                """(n) => {
                    const b = document.getElementById('pdf-page-block-' + n);
                    if (b) b.scrollIntoView({block: 'center'});
                }""", n)
            time.sleep(3.5)
            after = canvas_info(n)
            check("滚过去之后这一页的位图自动画回来了",
                  bool(after and after["painted"]),
                  json.dumps(after) if after else "null")

        tl = pg.evaluate(
            """() => {
                const t = document.querySelector('#pdf-page-block-1 .textLayer');
                return t ? t.textContent.trim().length : -1;
            }""")
        check("文本层仍然在（划词/批注不受影响）", tl > 50, "文本层字数=" + str(tl))

        real_errors = [e for e in errors if "favicon" not in e.lower()]
        check("全程没有 JS 报错（PDF.js 重复渲染/取消不炸）", not real_errors,
              " | ".join(real_errors[:3]))

        browser.close()

    print("\n%d passed, %d failed" % (len(ok), len(fail)))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
