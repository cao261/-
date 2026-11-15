# -*- coding: utf-8 -*-
"""专注模式「工具条怎么唤出」的**真机实测**（Playwright 打开真页面）。

用户 2026-11-14 明确要求：滚轮上滑**不得**唤出工具条 ——
工具条只认「鼠标扫到屏幕最上面」/「点顶部那条细把手」。

node 护栏（`_test_zen_mode.js`）跑的是**切片里的真函数**，
但它没有真实滚轮、没有真实鼠标坐标 —— 这一份补上真机那层。

跑法：python microbench/_live_zen_bar_test.py [pdf 相对路径]
"""
from __future__ import annotations

import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"

DEFAULT_REL = ("01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/"
               "2015_Control_of_Magnetic_Relaxation.pdf")


def main() -> int:
    rel = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REL).replace("\\", "/")
    url = BASE + "/?open_pdf=" + urllib.parse.quote(rel, safe="")
    errors: list[str] = []
    fails: list[str] = []

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  device_scale_factor=1)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(url, wait_until="domcontentloaded", timeout=120000)

        try:
            pg.wait_for_selector(".pdf-page-canvas", timeout=120000)
        except Exception as exc:
            print("✗ 等不到 PDF canvas：%s" % exc)
            for e in errors[:10]:
                print("   " + e)
            browser.close()
            return 1
        print("✓ PDF canvas 已渲染")
        time.sleep(1.5)

        # ---- 进专注模式 ----
        pg.click("#zen-toggle-btn")
        time.sleep(1.2)
        if not pg.evaluate(
                "document.getElementById('reader-modal').classList.contains('zen')"):
            print("✗ 没进专注模式（点了「专注」但 zen 类没加上）")
            browser.close()
            return 1
        print("✓ 已进专注模式")

        def bar_open() -> bool:
            return bool(pg.evaluate(
                "document.getElementById('reader-modal')"
                ".classList.contains('zen-bar-open')"))

        def check(name: str, cond: bool, detail: str = "") -> None:
            if cond:
                print("  PASS  " + name)
            else:
                print("  FAIL  " + name + ("  → " + detail if detail else ""))
                fails.append(name)

        check("进专注后工具条默认是收起的", not bar_open())

        # ① 鼠标停在内容区（不在顶部）→ 不该唤出
        pg.mouse.move(800, 600)
        time.sleep(0.4)
        check("鼠标在屏幕中间 → 工具条不出现", not bar_open())

        # ② 滚轮往上滑 → **不该**唤出（用户 2026-11-14 的要求）
        for _ in range(3):
            pg.mouse.wheel(0, -600)
            time.sleep(0.25)
        time.sleep(0.5)
        check("滚轮上滑 → 工具条不出现", not bar_open(),
              "滚轮又唤出工具条了（用户原话：不要我一往上滑就出来）")

        # ③ 滚到底 → 也不该唤出
        pg.mouse.wheel(0, 100000)
        time.sleep(0.9)
        check("滚到底 → 工具条不出现", not bar_open(),
              "滚到底又唤出工具条了")

        # ④ 鼠标扫到屏幕最上面 → 必须出来
        pg.mouse.move(800, 8)
        time.sleep(0.5)
        check("鼠标移到屏幕最上面 → 工具条滑出", bar_open(),
              "鼠标到顶部却没唤出工具条")

        # ⑤ 指针停在上方 → 自动收起定时器到点也**不该**收（用户还扶着它）
        time.sleep(3.2)
        check("指针停在最上面 → 工具条不自动收起", bar_open(),
              "指针还在顶部就被收走了（用户还得再扫一次鼠标）")

        # ⑥ 指针离开 → 1.2s 后收起
        pg.mouse.move(800, 600)
        time.sleep(2.0)
        check("指针离开最上面 → 工具条自动收起", not bar_open(),
              "指针离开了工具条还一直挂着")

        browser.close()

    print("")
    if errors:
        print("页面报错：")
        for e in errors[:10]:
            print("   " + e)
    if fails:
        print("FAIL %d 项：%s" % (len(fails), "; ".join(fails)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
