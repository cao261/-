# -*- coding: utf-8 -*-
"""「中译替换版 PDF」的**真机实测**（Playwright 打开真页面）。

用户 2026-11-15 诉求：
  「直接就是原文上直接把翻译打上去……这篇论文一共十页，
    然后我翻译完之后就变成二十页……然后原本的图表还都保持不变。」

这个脚本走**完整用户路径**：
  打开论文 → PDF 双栏 → 点「🧾 中译替换版」→ 生成 → 在阅读器里打开 →
  逐页核对（原文页有英文 / 替换页有中文 / 图表还在）。

跑法：python microbench/_live_replaced_pdf_test.py
"""
from __future__ import annotations

import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"
OUT = REPO / "logs" / "replaced_pdf"

DEFAULT_REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
               "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")


def main() -> int:
    rel = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REL).replace("\\", "/")
    OUT.mkdir(parents=True, exist_ok=True)
    url = BASE + "/?open_pdf=" + urllib.parse.quote(rel, safe="")

    errors: list[str] = []
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if cond:
            print("  PASS  " + name)
        else:
            print("  FAIL  " + name + ("  → " + detail if detail else ""))
            fails.append(name)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000},
                                  device_scale_factor=1)
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append("[console] " + m.text)
              if m.type == "error" else None)
        # 记下所有后端调用 —— 用来断言「没有为了这份 PDF 白翻一遍」
        api_calls = []
        pg.on("request", lambda r: api_calls.append(r.url)
              if "/api/" in (r.url or "") else None)
        pg.goto(url, wait_until="domcontentloaded", timeout=120000)

        try:
            pg.wait_for_selector(".pdf-page-canvas", timeout=120000)
        except Exception as exc:
            print("✗ 等不到 PDF canvas：%s" % exc)
            browser.close()
            return 1
        print("✓ PDF 已打开")
        time.sleep(2.0)

        # ---- 点「🧾 中译替换版」 ----
        btn = pg.query_selector("#pdf-replaced-toggle")
        check("工具条上有「中译替换版」按钮", btn is not None)
        if btn is None:
            browser.close()
            return 1
        btn.click()
        print("✓ 已点「中译替换版」")
        calls_before = len(api_calls)

        # 等 toast（生成成功）或报错
        #
        # ⚠ 选择器必须用 **#toast**：页面上的提示元素是
        #   `<div id="toast" role="status">` —— **没有 class**。
        #   早先写 `.toast, [class*=toast]` 一个都匹配不上 → 永远抓不到提示，
        #   误判成「生成失败」。
        def wait_toast(seconds=180):
            end = time.time() + seconds
            msg = ""
            while time.time() < end:
                msg = pg.evaluate("""() => {
                    const el = document.getElementById('toast');
                    if (!el) return '';
                    const shown = el.style.display !== 'none';
                    const txt = (el.innerText || el.textContent || '');
                    return shown ? txt : '';
                }""")
                if ("已生成" in msg) or ("失败" in msg):
                    break
                time.sleep(0.5)
            return msg

        toast = wait_toast()
        print("   toast: %s" % (toast or "(没抓到)"))
        check("生成成功（有「已生成」提示）", "已生成" in toast, toast)
        if "已生成" not in toast:
            browser.close()
            return 1

        # ---- 再点一次：必须还能从**原论文**重建 ----
        # ⚠ 生成完阅读器里打开的是替换版，`_pdfState` 也就变成了它；
        #   若不记住源，第二次就会去找「替换版的快照」→ 必然 409。
        btn2 = pg.query_selector("#pdf-replaced-toggle")
        if btn2:
            btn2.click()
            toast2 = wait_toast(120)
            check("再点一次仍能从原论文重建", "已生成" in toast2, toast2)
            time.sleep(2.0)

        # ---- 阅读器应该已经切到新生成的 PDF ----
        time.sleep(4.0)
        state = pg.evaluate("""() => {
            // ⚠ `_readerState` / `_pdfState` 是脚本作用域的 let 绑定，
            //   **不在 window 上** → 从 window 读永远是 undefined。
            //   要看当前打开的是哪份 PDF，只能读 DOM 上的标题。
            const t = document.getElementById('reader-title');
            const canvases = document.querySelectorAll('#pdf-pages-list .pdf-page-canvas');
            return {
                title: t ? (t.textContent || '') : '',
                canvasCount: canvases.length,
            };
        }""")
        print("   阅读器标题: %s" % state["title"])
        check("阅读器切到了「中译替换版」",
              "中译替换版" in (state["title"] or ""), state["title"])

        # ---- 替换版视图：右栏收起 + **不再调模型翻一遍** ----
        right = pg.evaluate("""() => {
            const c = document.getElementById('pdf-translations-container');
            return c ? c.style.display : '(没这个元素)';
        }""")
        check("右栏（对照译文）已收起，PDF 占满宽度",
              right == "none", "display=%s" % right)

        time.sleep(4.0)
        new_calls = api_calls[calls_before:]
        tr = [u for u in new_calls if "translate" in u.lower()]
        check("没有为了这份 PDF 重复调用翻译接口（不白花钱）",
              not tr, "；".join(tr[:3]) or "无")
        print("   生成过程中的后端调用: %s"
              % ("；".join(u.split("/api/")[-1] for u in new_calls) or "无"))

        # ---- 逐页核对：原文页有英文、替换页有中文 ----
        page_texts = pg.evaluate("""() => {
            const out = [];
            document.querySelectorAll('#pdf-pages-list .pdf-page-block').forEach((el, i) => {
                out.push({i: i + 1, cls: el.className});
            });
            return out;
        }""")
        print("   页卡数: %d" % len(page_texts))

        # 直接查生成出来的 PDF（比在渲染出来的画布上找字更可靠）
        import fitz
        # ⚠ 必须取**最新**的那个：同目录下可能有别的论文的产物
        #   （glob 取第一个 = 随机命中别人的文件 → 判据全错）。
        cands = list((REPO / "01_Literature/04_Survey_综述/05_译文替换版_PDF").glob("*.pdf"))
        gen = max(cands, key=lambda p: p.stat().st_mtime) if cands else None
        if gen is None:
            check("生成的文件在 vault 里", False, "没找到 05_译文替换版_PDF/*.pdf")
        else:
            # ⚠ 期望值**从 toast 里解析**，不写死：
            #   写死的话，护栏会把「哪几页没替换」这个 bug 当成正确行为钉住
            #   （2026-11-17 用户报「最后几页都没替换」时，这条护栏是绿的）。
            #   现在断言的是**不变量**：页数 = 原文 + 替换、且严格交替。
            import re as _re
            m = _re.search(r"已生成\s*(\d+)\s*页（(\d+)\s*原文\s*\+\s*(\d+)\s*替换）", toast or "")
            if not m:
                check("能从提示里读出「原文几页 + 替换几页」", False,
                      "toast=%r" % (toast or "")[:200])
                n_orig = n_repl = None
            else:
                n_orig = int(m.group(2))
                n_repl = int(m.group(3))
                check("能从提示里读出「原文几页 + 替换几页」", True,
                      "原文 %d + 替换 %d" % (n_orig, n_repl))
            doc = fitz.open(str(gen))
            if n_orig is None:
                doc.close()
                return 1
            check("输出页数 = 原文页数 + 替换页数（%d → %d）"
                  % (n_orig, n_orig + n_repl),
                  doc.page_count == n_orig + n_repl, "实际 %d 页" % doc.page_count)
            orig_hits = repl_hits = 0
            pattern = []
            for i in range(doc.page_count):
                txt = doc.load_page(i).get_text()
                cn = sum(1 for c in txt if "一" <= c <= "鿿")
                en = sum(1 for c in txt if c.isascii() and c.isalpha())
                # ⚠ 替换页**必然还留着英文** —— 公式块 / 表格块不替换，
                #   放不下的块也保留英文 → 判据只能是「有没有中文」。
                if cn > 40:
                    repl_hits += 1
                    pattern.append("中")
                elif en > 40:
                    orig_hits += 1
                    pattern.append("英")
                else:
                    pattern.append("?")
                print("     p%-2d 中文 %-5d 英文 %-5d %s"
                      % (i + 1, cn, en, "(替换页)" if cn > 40 else "(原文页)"))
            print("   页面序列: %s" % "".join(pattern))
            check("原文页保留英文", orig_hits >= 4, "识别到 %d 个原文页" % orig_hits)
            check("替换页是中文", repl_hits >= 4, "识别到 %d 个替换页" % repl_hits)
            # 期望序列 = 严格「原文/替换」交替 × 替换页数
            # + 剩下的原文页（没翻过的页只有原文页）
            expect = ("英中" * n_repl) + ("英" * (n_orig - n_repl))
            check("原文页与替换页交替出现",
                  "".join(pattern) == expect,
                  "".join(pattern) + " ≠ " + expect)
            # 图表保留：第 3/4 页那组图（JACERS 第 2 页有图）
            imgs_orig = len(doc.load_page(2).get_images(full=True))
            imgs_repl = len(doc.load_page(3).get_images(full=True))
            check("替换页里的图片还在（图表没被涂掉）",
                  imgs_repl >= imgs_orig, "原文页 %d 张图 / 替换页 %d 张图"
                  % (imgs_orig, imgs_repl))
            doc.close()

        pg.screenshot(path=str(OUT / "reader_after_build.png"), full_page=False)
        print("   截图: %s" % (OUT / "reader_after_build.png"))
        browser.close()

    if errors:
        print("\n页面报错：")
        for e in errors[:8]:
            print("   " + e)
    if fails:
        print("\nFAIL %d 项：%s" % (len(fails), "; ".join(fails)))
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
