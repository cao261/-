# -*- coding: utf-8 -*-
"""平板端**真机模拟**实测：用 Playwright 拦截 `https://appassets.androidplatform.net/**`
—— 这正是 APK 里 WebView 的运行方式（`MainActivity.java` 的
`shouldInterceptRequest` 把这个源的请求全在本地拦截）。

  /assets/*  → APK 里的单文件（这里用导出的 MicroBench_平板工作台.html）
  /dav/*     → 坚果云 WebDAV（这里直接读本机同步根，等价于「云盘已同步」）

所以这一跑走的是**平板真前端**（app.js / styles.css / 内联 PDF.js / 内联 KaTeX），
不是探针页。

跑法：python microbench/_live_tablet_test.py [pdf 相对路径] [页码]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "logs" / "blockdiag"
BUNDLE = REPO / "MicroBench_平板工作台.html"
SYNC_ROOT = Path(os.environ.get(
    "ANNOTATIONS_DIR",
    r"C:/Users/30461/Nutstore/1/我的坚果云/MicroBench同步"))
ORIGIN = "https://appassets.androidplatform.net"

DEFAULT_REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
               "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")


def main() -> int:
    rel = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REL).replace("\\", "/")
    page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    OUT.mkdir(parents=True, exist_ok=True)
    bundle = BUNDLE.read_bytes()

    from playwright.sync_api import sync_playwright

    def handler(route):
        url = route.request.url
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        # ⚠ APK 的 Java 侧拿到的 path 已经解码过（DavPathHandler 注释），
        #   而 Playwright 给的是**编码**形式 → 这里必须先 unquote。
        if path == "/assets/index.html":
            route.fulfill(status=200, content_type="text/html; charset=utf-8",
                          body=bundle)
            return
        if path.startswith("/assets/"):
            local = REPO / "microbench" / "static" / path[len("/assets/"):]
            if local.is_file():
                route.fulfill(status=200, body=local.read_bytes())
            else:
                route.fulfill(status=404, body=b"not found")
            return
        if path.startswith("/dav/sync/"):
            # ⚠ 路由约定（DavPathHandler）：
            #   GET /dav/sync/file/<path> → 读同步根里的文件
            #   GET /dav/sync/list/<path> → 列同步根里的目录
            #   「file/」「list/」是**类型前缀**，不是路径的一部分 → 必须剥掉。
            rest = path[len("/dav/sync/"):]
            kind, _, sub = rest.partition("/")
            local = (SYNC_ROOT / sub) if sub else SYNC_ROOT
            if kind == "file":
                if local.is_file():
                    ctype = ("application/json" if local.suffix == ".json"
                             else "application/octet-stream")
                    route.fulfill(status=200, content_type=ctype,
                                  body=local.read_bytes())
                else:
                    route.fulfill(status=404, body=b"not found")
                return
            if kind == "list":
                if local.is_dir():
                    names = [p.name for p in sorted(local.iterdir()) if p.is_file()]
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(names).encode())
                else:
                    route.fulfill(status=404, body=b"not found")
                return
            route.fulfill(status=404, body=b"not found")
            return
        if path.startswith("/dav/"):
            # /dav/file/<相对路径> → **vault 目录**（原文 PDF 在这儿）
            # /dav/list/<相对路径> → 列 vault 目录
            sub = path[len("/dav/"):]
            if sub.startswith("file/"):
                local = REPO / sub[len("file/"):]
                if local.is_file():
                    route.fulfill(status=200, body=local.read_bytes())
                else:
                    route.fulfill(status=404, body=b"not found")
                return
            if sub.startswith("list/"):
                local = REPO / sub[len("list/"):]
                if local.is_dir():
                    names = []
                    for p in sorted(local.iterdir()):
                        if p.is_dir():
                            names.append(p.name + "/")
                        elif p.is_file():
                            names.append(p.name)
                    route.fulfill(status=200, content_type="application/json",
                                  body=json.dumps(names).encode())
                else:
                    route.fulfill(status=404, body=b"not found")
                return
        route.fulfill(status=404, body=b"not found")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1180, "height": 1500},
                                  device_scale_factor=1,
                                  ignore_https_errors=True)
        # 模拟 APK 里的原生桥（MainActivity 注入的 window.MicroBench）——
        # 只需要「DAV 已配好」这一条，后端就会走 /dav/* 路由。
        ctx.add_init_script("""
            window.MicroBench = {
              davConfigured: function () { return true; },
              davInfo: function () {
                return JSON.stringify({host:'dav.jianguoyun.com',
                                       user:'sim', vaultRoot:'我的坚果云/MicroBench工作台',
                                       syncRoot:'我的坚果云/MicroBench同步'});
              },
              call: function () { return Promise.resolve(''); },
            };
        """)
        pg = ctx.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append("[pageerror] %s" % e))
        pg.on("console", lambda m: errors.append("[console] %s" % m.text)
              if m.type == "error" else None)
        pg.route("**/*", handler)
        pg.goto(ORIGIN + "/assets/index.html", wait_until="domcontentloaded",
                timeout=120000)
        print("✓ 平板单文件已加载（源 %s）" % ORIGIN)
        time.sleep(4.0)

        # ---- 打开这篇论文 ----
        opened = pg.evaluate("""(rel) => {
            const st = window.state || {};
            const pdfs = st.pdfs || {};
            for (const k of Object.keys(pdfs)) {
                const p = pdfs[k];
                const rp = (p && (p.relative_path || p.relativePath)) || '';
                if (rp.indexOf('JACERS') >= 0 || rp === rel) {
                    return {key: k, title: p && (p.title || p.name), rp: rp};
                }
            }
            return {keys: Object.keys(pdfs).slice(0, 8)};
        }""", rel)
        print("  论文条目: %s" % (opened,))

        # ---- 打开「文献库」→ 找这篇论文（纯 DOM 交互，跟用户一样）----
        picked = pg.evaluate("""() => {
            const btn = document.getElementById('btn-pdfs');
            if (btn) btn.click();
            return {clicked: !!btn};
        }""")
        print("  打开文献库: %s" % picked)
        time.sleep(5.0)

        picked = pg.evaluate("""() => {
            const items = document.querySelectorAll('.tb-pdf-item');
            const names = [];
            let target = null;
            items.forEach(it => {
                const t = (it.innerText || '');
                names.push(t.slice(0, 40));
                if (t.indexOf('JACERS') >= 0 ||
                    t.indexOf('Magnetoelectric laminate') >= 0) target = it;
            });
            if (target) target.click();
            return {items: items.length, names: names.slice(0, 6),
                    clicked: !!target};
        }""")
        print("  文献库: %s" % picked)
        time.sleep(10.0)

        # ---- 切对照 + 开原页图 ----
        picked = pg.evaluate("""() => {
            const btn = document.getElementById('pdf-orig-toggle');
            const lay = document.getElementById('pdf-layout');
            return {hasOrigBtn: !!btn, hasLayBtn: !!lay,
                    layText: lay ? lay.textContent : null};
        }""")
        print("  控件: %s" % picked)
        time.sleep(1.0)
        # 布局是**循环按钮**（原文+译文 → 对照 → 仅原文 → 仅译文）
        for _ in range(4):
            txt = pg.evaluate("""() => {
                const b = document.getElementById('pdf-layout');
                return b ? b.textContent : '';
            }""")
            if "对照" in (txt or ""):
                print("  布局已是「对照」（%s）" % txt)
                break
            pg.click("#pdf-layout")
            time.sleep(1.5)
        time.sleep(3.0)
        try:
            pg.click("#pdf-orig-toggle", timeout=5000)
            print("  已点「🖼 原页图」")
        except Exception as exc:
            print("  点原页图按钮失败: %s" % exc)
        time.sleep(4.0)

        state = pg.evaluate("""(n) => {
            const cards = document.querySelectorAll('.tb-pdf-page');
            const card = cards[n-1] || cards[0];
            const orig = card && card.querySelector('.tb-pdf-orig');
            const trans = card && card.querySelector('.tb-pdf-trans');
            const body = document.getElementById('pdf-body');
            const r = e => e ? e.getBoundingClientRect() : null;
            const o = r(orig), t = r(trans);
            const bt = card ? card.getAttribute('data-blocks') : null;
            const survey = Array.from(cards).map((cc,i)=>{
                const oo=cc.querySelector('.tb-pdf-orig');
                const tt=cc.querySelector('.tb-pdf-trans');
                const rr=e=>e?e.getBoundingClientRect():null;
                const a=rr(oo), b=rr(tt);
                return {
                    page: i+1,
                    side: cc.classList.contains('side-cols'),
                    cardW: cc.clientWidth,
                    ow: a?Math.round(a.width):0,
                    tw: b?Math.round(b.width):0,
                    sameRow: (a&&b)?Math.abs(a.top-b.top)<40:null,
                    overl: (a&&b)?Math.round(a.right-b.left):null,
                };
            });
            return {
                cards: cards.length,
                hasShowOrig: card ? card.classList.contains('show-orig') : null,
                hasBilingual: card ? card.classList.contains('lay-bilingual') : null,
                sideCols: card ? card.classList.contains('side-cols') : null,
                hostW: body ? body.clientWidth : null,
                bodyScrollW: body ? body.scrollWidth : null,
                origDisplay: orig ? getComputedStyle(orig).display : null,
                orig: o ? {l:o.left, r:o.right, t:o.top, w:o.width, h:o.height} : null,
                trans: t ? {l:t.left, r:t.right, t:t.top, w:t.width, h:t.height} : null,
                tables: trans ? trans.querySelectorAll('table').length : 0,
                textLen: trans ? (trans.innerText||'').trim().length : 0,
                text: trans ? (trans.innerText||'').trim().slice(0, 2000) : '',
                blockAttr: bt,
                survey: survey,
            };
        }""", page_no)
        print("\n--- 平板实测结果 ---")
        for k, v in state.items():
            if k not in ("text", "survey"):
                print("  %-16s %s" % (k, v))
        survey = state.get("survey") or []
        print("\n--- 逐页调查（共 %d 页）---" % len(survey))
        for s in survey:
            print("  第 %d 页  并排=%-5s  页卡宽=%-4d 原页=%-4d 译文=%-4d 同排=%-5s 重叠=%s" % (
                s["page"], s["side"], s["cardW"], s["ow"], s["tw"],
                s["sameRow"], s["overl"]))
        fails = []
        host_w = state.get("hostW") or 0
        if host_w:
            for s in survey:
                if s["tw"] > host_w + 1:
                    fails.append("第 %d 页译文栏宽 %dpx 超出可视宽 %dpx（横向溢出屏幕）"
                                 % (s["page"], s["tw"], host_w))
                if s["side"]:
                    if s["tw"] < 320:
                        fails.append("第 %d 页并排时译文栏只有 %dpx（<320 不可读）"
                                     % (s["page"], s["tw"]))
                    if s["sameRow"] is False:
                        fails.append("第 %d 页标了并排但实际上下叠" % s["page"])
                    if s["overl"] is not None and s["overl"] > 0:
                        fails.append("第 %d 页原页与译文横向重叠 %dpx" % (s["page"], s["overl"]))
                elif s["sameRow"] is True:
                    fails.append("第 %d 页说堆叠但实际同排" % s["page"])
            if len(set(bool(s["side"]) for s in survey)) > 1:
                fails.append("各页并排/堆叠不一致")
        if fails:
            print("\n✗ 平板布局判定不通过：")
            for f in fails:
                print("   - " + f)
        else:
            print("\n✓ 平板布局判定通过（译文栏不超屏 / 并排则可读 / 无横向重叠）")
        print("\n--- 平板对照译文 ---")
        print(state.get("text", "")[:2200])

        shot = OUT / ("live-ui-tablet-p%d.png" % page_no)
        pg.screenshot(path=str(shot), full_page=False)
        print("\n✓ 截图 %s（%d KB）" % (shot, shot.stat().st_size // 1024))
        if errors:
            print("\n--- 错误（%d）---" % len(errors))
            for e in errors[:10]:
                print("   " + e[:180])
        browser.close()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
