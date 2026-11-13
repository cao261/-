# -*- coding: utf-8 -*-
"""桌面端**真实前端**实测：用 Playwright 打开用户真正用的那个页面，
按真实路径操作（打开论文 → 🔀 对照 → 🖼 原页图），量真实坐标、截真实图。

⚠ 这不是探针页（`_shot_bilingual.py` 那种），是 **http://127.0.0.1:5000/**
   那个真应用：真 CSS、真 PDF.js、真 API、真模型。

跑法：python microbench/_live_ui_test.py [pdf 相对路径] [页码]
"""
from __future__ import annotations

import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"
OUT = REPO / "logs" / "blockdiag"

DEFAULT_REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
               "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")


def main() -> int:
    rel = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REL).replace("\\", "/")
    page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    OUT.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    url = BASE + "/?open_pdf=" + urllib.parse.quote(rel, safe="")
    print("打开：%s" % url[:160])
    errors: list[str] = []
    layout_fail: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  device_scale_factor=1)
        pg = ctx.new_page()
        pg.on("console", lambda m: errors.append("[console.%s] %s" % (
            m.type, m.text)) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errors.append("[pageerror] %s" % e))
        pg.goto(url, wait_until="domcontentloaded", timeout=120000)

        # ---- 等 PDF.js 把第一页画出来 ----
        try:
            pg.wait_for_selector(".pdf-page-canvas", timeout=120000)
        except Exception as exc:
            print("✗ 等不到 PDF canvas：%s" % exc)
            for e in errors[:10]:
                print("   " + e)
            browser.close()
            return 1
        print("✓ PDF canvas 已渲染")
        time.sleep(2.0)

        # ---- 滚到指定页 ----
        pg.evaluate("""(n) => {
            const cards = document.querySelectorAll('.pdf-page-block');
            if (cards[n-1]) cards[n-1].scrollIntoView({block:'start'});
        }""", page_no)
        time.sleep(1.0)

        # ---- 开对照模式（**先开**，队列才会去翻这一页）----
        pg.click("#pdf-bilingual-toggle")
        print("✓ 已点「🔀 对照」")
        time.sleep(3.0)

        # ---- 等**这一页**的段落译文贴上来 ----
        deadline = time.time() + 600
        ready = False
        txt = 0
        while time.time() < deadline:
            state = pg.evaluate("""(n) => {
                const card = document.querySelectorAll('.pdf-page-block')[n-1];
                const host = card && card.querySelector('.bilingual-page');
                if (!host) return {txt:0, pending:true};
                const t = (host.innerText || '');
                return {txt:t.trim().length,
                        pending: t.indexOf('还没翻') >= 0};
            }""", page_no)
            txt = state.get("txt", 0)
            if txt > 200 and not state.get("pending"):
                ready = True
                break
            time.sleep(3.0)
        print(("✓ 第 %d 页段落译文已贴上（文本长度 %d）" % (page_no, txt))
              if ready else
              "⚠ 600s 内没等到第 %d 页的段落译文（长度 %d）" % (page_no, txt))
        # 翻完了就**立刻**暂停队列，别把整篇都烧了
        try:
            pg.click("#pdf-queue-pause-btn", timeout=5000)
            print("✓ 已暂停翻译队列")
        except Exception:
            print("· 暂停队列按钮点不到（忽略）")
        time.sleep(2.0)

        # ---- 开原页图 ----
        pg.click("#pdf-bilingual-orig-toggle")
        print("✓ 已点「🖼 原页图」")
        time.sleep(3.0)

        # ---- 量真实坐标（**只量这一页**）----
        measure = pg.evaluate("""(n) => {
            const card = document.querySelectorAll('.pdf-page-block')[n-1];
            if (!card) return {err:'no card'};
            const canvas = card.querySelector('.pdf-page-canvas');
            const biling = card.querySelector('.bilingual-page');
            const host = biling;
            const r = e => e ? e.getBoundingClientRect() : null;
            const c = r(canvas), b = r(biling);
            // 逐页调查：同一篇里**每页**的并排/堆叠必须一致（2026-11-13 修的缺陷：
            // 当时逐页各算各的 → 第 1 页全宽、第 2 页被挤成 43px，滚起来忽宽忽窄）
            const survey = Array.from(document.querySelectorAll('.pdf-page-block')).map((cc,i)=>{
                const cv=cc.querySelector('.pdf-page-canvas');
                const bp=cc.querySelector('.bilingual-page');
                const rr=e=>e?e.getBoundingClientRect():null;
                const a=rr(cv), bb=rr(bp);
                return {
                    page: i+1,
                    wide: cc.classList.contains('bilingual-orig-wide'),
                    cardW: cc.clientWidth,
                    cw: a?Math.round(a.width):0,
                    bw: bb?Math.round(bb.width):0,
                    sameRow: (a&&bb)?Math.abs(a.top-bb.top)<40:null,
                    overl: (a&&bb)?Math.round(a.right-bb.left):null,
                };
            });
            return {
                hasOrigClass: card.classList.contains('bilingual-orig'),
                wideClass: card.classList.contains('bilingual-orig-wide'),
                cardW: card.clientWidth,
                canvas: c ? {l:c.left, r:c.right, t:c.top, w:c.width, h:c.height} : null,
                biling: b ? {l:b.left, r:b.right, t:b.top, w:b.width, h:b.height} : null,
                tables: host ? host.querySelectorAll('table').length : 0,
                formulaImgs: host ? host.querySelectorAll('img').length : 0,
                textLen: host ? (host.innerText||'').trim().length : 0,
                text: host ? (host.innerText||'').trim() : '',
                origBtn: (document.getElementById('pdf-bilingual-orig-toggle')||{}).textContent,
                bilingualBtn: (document.getElementById('pdf-bilingual-toggle')||{}).textContent,
                survey: survey,
            };
        }""", page_no)
        print("\n--- 实测结果（第 %d 页）---" % page_no)
        for k in ("hasOrigClass", "origBtn", "bilingualBtn", "tables",
                  "formulaImgs", "textLen"):
            print("  %-14s %s" % (k, measure.get(k)))
        print("  canvas        %s" % (measure.get("canvas"),))
        print("  bilingual     %s" % (measure.get("biling"),))
        if measure.get("canvas") and measure.get("biling"):
            overlap = measure["canvas"]["r"] - measure["biling"]["l"]
            same_row = abs(measure["canvas"]["t"] - measure["biling"]["t"]) < 40
            print("  横向重叠      %.1f px %s" % (
                overlap,
                "（同排时的重叠量，>0 = 压到一起了 ✗）" if same_row
                else "（上下堆叠，x 区间本来就会重叠，忽略）"))
            print("  上下并排      canvas.top=%.0f  biling.top=%.0f  %s" % (
                measure["canvas"]["t"], measure["biling"]["t"],
                "✓ 同排" if abs(measure["canvas"]["t"]
                                - measure["biling"]["t"]) < 40 else ""))
            bw = measure["biling"]["w"]
            print("  对照译文列宽  %.0f px %s" % (
                bw, "✓ 可读" if bw >= 360 else
                "✗ 太窄（译文被挤成一条竖线！）"))
            print("  页卡可用宽    %.0f px   并排类名 %s" % (
                measure.get("cardW") or 0,
                "bilingual-orig-wide ✓" if measure.get("wideClass") else "无（堆叠）"))

        # ---- 逐页一致性 + 可读宽度判定（2026-11-13 缺陷回归）----
        survey = measure.get("survey") or []
        print("\n--- 逐页调查（共 %d 页）---" % len(survey))
        for s in survey:
            print("  第 %d 页  并排=%-5s  页卡宽=%-4d 原页=%-4d 译文=%-4d 同排=%-5s 重叠=%s" % (
                s["page"], s["wide"], s["cardW"], s["cw"], s["bw"],
                s["sameRow"], s["overl"]))
        fails = []
        if survey:
            wide_flags = set(bool(s["wide"]) for s in survey)
            if len(wide_flags) > 1:
                fails.append("同一篇里各页的并排/堆叠不一致（忽宽忽窄）")
            if all(wide_flags):
                for s in survey:
                    if s["bw"] < 380:
                        fails.append("第 %d 页并排时译文列只有 %dpx（<380 不可读）"
                                     % (s["page"], s["bw"]))
                    if s["sameRow"] is False:
                        fails.append("第 %d 页标了并排但实际上下叠" % s["page"])
            else:
                for s in survey:
                    if s["sameRow"] is True:
                        fails.append("第 %d 页说堆叠但实际同排" % s["page"])
            for s in survey:
                # 只有**同排**（并排）时横向重叠才是问题；堆叠时两者上下分开，
                # x 区间重叠是正常的
                if s["sameRow"] and s["overl"] is not None and s["overl"] > 0:
                    fails.append("第 %d 页原页与译文横向重叠 %dpx" % (s["page"], s["overl"]))
        if fails:
            print("\n✗ 布局判定不通过：")
            for f in fails:
                print("   - " + f)
            layout_fail.extend(fails)
        else:
            print("\n✓ 布局判定通过（各页一致 / 译文列可读 / 无横向重叠）")

        body = measure.get("text", "")
        print("\n--- 这一页的对照译文（全文）---")
        print(body[:2600])

        shot = OUT / ("live-ui-desktop-p%d.png" % page_no)
        pg.locator(".pdf-page-block").nth(page_no - 1).screenshot(path=str(shot))
        print("\n✓ 截图 %s（%d KB）" % (shot, shot.stat().st_size // 1024))
        shot2 = OUT / ("live-ui-desktop-viewport-p%d.png" % page_no)
        pg.screenshot(path=str(shot2))
        print("✓ 视口截图 %s（%d KB）" % (shot2, shot2.stat().st_size // 1024))

        # ---- 证据截图：滚到「原页图底边 ↔ 对照译文开头」的交界处 ----
        #    桌面端阅读器栏放不下两栏 → 一定是**上下堆叠**：
        #    上面是原页图（图片/图表看得见），下面是按原文排版的对照译文。
        pg.evaluate("""(n) => {
            const card = document.querySelectorAll('.pdf-page-block')[n-1];
            const bi = card && card.querySelector('.bilingual-page');
            if (!bi) return;
            // ⚠ 阅读器栏是**内部滚动容器**，滚 window 没反应 →
            //   往上找第一个 overflow-y 是 auto/scroll 的祖先。
            let sc = bi;
            while (sc && sc !== document.body) {
                const st = getComputedStyle(sc);
                if (/(auto|scroll)/.test(st.overflowY)) break;
                sc = sc.parentElement;
            }
            if (!sc || sc === document.body) sc = document.scrollingElement;
            const top = bi.getBoundingClientRect().top
                      - sc.getBoundingClientRect().top + sc.scrollTop;
            // 让「原页图底部」落在视口上半部分 → 同屏能看到图 + 译文开头
            sc.scrollTop = top - 260;
        }""", page_no)
        time.sleep(1.5)
        shot3 = OUT / ("live-ui-desktop-orig-plus-trans-p%d.png" % page_no)
        pg.screenshot(path=str(shot3))
        print("✓ 原页图+对照译文同屏截图 %s（%d KB）"
              % (shot3, shot3.stat().st_size // 1024))

        if errors:
            print("\n--- 控制台错误（%d 条）---" % len(errors))
            for e in errors[:12]:
                print("   " + e[:200])
        browser.close()
    return 1 if layout_fail else 0


if __name__ == "__main__":
    sys.exit(main())
