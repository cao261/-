# -*- coding: utf-8 -*-
"""平板 PDF 布局的**真实渲染**护栏（2026-10-28）。

用户实测反馈的两个问题：
  ① 手写「没有任何作用」
  ② 缩放放大后「只能显示一部分，动不了了」

这份护栏用 **headless Edge 真渲染**验证，不做静态字符串断言 ——
因为「旧 CSS 到底坏没坏」只有真渲染能回答。

实测对照（同一份页面，只换 CSS）：

    旧 CSS (max-width:100% + overflow:hidden):
        页卡宽 = 585px（内容 1800px 被裁成 1/3）
        scrollWidth = 605 → **横向滚不到右半边**
    新 CSS (width:max-content):
        页卡宽 = 1800px
        scrollWidth = 1820 → 完整可滚

运行：python microbench/_test_tablet_pdf_layout.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSS = REPO / "microbench" / "static" / "tablet" / "styles.css"
APP_JS = REPO / "microbench" / "static" / "tablet" / "app.js"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# 只取需要验证的规则（其余样式对布局无关）
#
# ⚠⚠ 选择器必须**精确**（带前导的空白/换行锚定），否则会误匹配到
#    @media 块里的 `.lay-trans .tb-pdf-orig { display:none; }` 的尾部
#    → 提取出 `.tb-pdf-orig { display:none; }` 顶层规则 → 原文被隐藏 → 测出 orig=0。
RULE_PATTERNS = [
    r"(?m)^\.tb-pdf-body\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-orig\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page canvas\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-ink\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-ink\.is-on\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-ink canvas\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-trans\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page\.lay-orig \.tb-pdf-trans\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page\.lay-trans \.tb-pdf-orig\s*\{[^}]*\}",
]

# ⚠ @media 块要**整块**取（含里面的规则），不能只取第一行
MEDIA_RE = re.compile(r"@media \(min-width: 900px\)\s*\{(?:[^{}]|\{[^{}]*\})*\}")


def find_browser() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    found = shutil.which("msedge") or shutil.which("chrome")
    return found


def extract_rules(css: str) -> str:
    out = []
    for pat in RULE_PATTERNS:
        for m in re.finditer(pat, css):
            out.append(m.group(0))
    for m in MEDIA_RE.finditer(css):
        out.append(m.group(0))
    return "\n".join(out)


PROBE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>probe</title>
<style>
html,body{{margin:0;padding:0;height:100%;}}
.tb-shell{{position:fixed;top:0;left:0;right:0;bottom:0;display:flex;flex-direction:column;}}
{rules}
</style></head>
<body>
<div class="tb-shell">
  <div class="tb-pdf-body" id="body">
    <div class="tb-pdf-page lay-orig" id="page">
      <div class="tb-pdf-orig" id="orig">
        <canvas id="cv"></canvas>
        <div class="tb-pdf-ink is-on" id="ink"><canvas id="inkcv"></canvas></div>
      </div>
      <div class="tb-pdf-trans">译文</div>
    </div>
  </div>
</div>
<script>
function setSize(zoom){{
  var w=Math.round(600*zoom), h=Math.round(800*zoom);
  var cv=document.getElementById('cv');
  cv.style.width=w+'px'; cv.style.height=h+'px'; cv.width=w; cv.height=h;
}}
var results=[];
function check(n,c,d){{results.push({{name:n,pass:!!c,detail:d||''}});}}
var body=document.getElementById('body');
body.style.width='600px';

// 放大 3 倍
setSize(3);
body.getBoundingClientRect();
var pageR=document.getElementById('page').getBoundingClientRect();
var origR=document.getElementById('orig').getBoundingClientRect();
var bodyR=body.getBoundingClientRect();
var ink=document.getElementById('ink');
var inkR=ink.getBoundingClientRect();
var inkCs=getComputedStyle(ink);
var pageCs=getComputedStyle(document.getElementById('page'));

check('放大 3 倍后页卡宽度 > 容器宽度（内容没被夹住）',
      pageR.width > bodyR.width+50,
      'page='+pageR.width.toFixed(0)+' body='+bodyR.width.toFixed(0));
check('放大 3 倍后原文实际宽度 = 1800（没被裁）',
      Math.abs(origR.width-1800)<5, 'orig='+origR.width.toFixed(0));
check('滚动容器 scrollWidth 覆盖内容宽（能横向滚到右半边）',
      body.scrollWidth >= origR.width-2,
      'scrollWidth='+body.scrollWidth+' contentW='+origR.width.toFixed(0));
check('页卡 overflow 可见（不是 hidden 裁掉）',
      pageCs.overflow!=='hidden', 'overflow='+pageCs.overflow);
check('手写层 touch-action = none（手指拖动不会被当滚动手势）',
      inkCs.touchAction==='none', 'touch-action='+inkCs.touchAction);
check('手写层 pointer-events = auto（开着手写时能收到事件）',
      inkCs.pointerEvents==='auto', 'pointer-events='+inkCs.pointerEvents);
check('手写层尺寸 = 原文尺寸（笔迹能画在正确位置）',
      Math.abs(inkR.width-origR.width)<2 && Math.abs(inkR.height-origR.height)<2,
      'ink='+inkR.width.toFixed(0)+'x'+inkR.height.toFixed(0)+
      ' orig='+origR.width.toFixed(0)+'x'+origR.height.toFixed(0));

// 1 倍
setSize(1);
body.getBoundingClientRect();
var p1=document.getElementById('page').getBoundingClientRect();
var b1=body.getBoundingClientRect();
check('1 倍时页卡宽度 >= 内容宽（不小于内容区）',
      p1.width >= (b1.width-20)-2,
      'page='+p1.width.toFixed(0)+' contentW='+(b1.width-20).toFixed(0));

document.title='RESULT:'+JSON.stringify(results);
</script></body></html>
"""


def run_probe(browser: str, rules: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "probe.html"
        html.write_text(PROBE_HTML.format(rules=rules), encoding="utf-8")
        out = Path(td) / "out.html"
        cmd = [
            browser, "--headless", "--disable-gpu", "--no-sandbox",
            "--virtual-time-budget=3000", "--dump-dom",
            html.resolve().as_uri(),
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("headless 浏览器超时")
        out.write_bytes(p.stdout)
        dom = p.stdout.decode("utf-8", errors="replace")
        m = re.search(r"RESULT:(\[.*?\])</title>", dom)
        if not m:
            raise RuntimeError("没拿到探针结果（DOM 长度 %d）" % len(dom))
        raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
        return json.loads(raw)


def main() -> int:
    browser = find_browser()
    if not browser:
        print("SKIP: 找不到 Edge/Chrome，跳过渲染护栏")
        return 0

    css = CSS.read_text(encoding="utf-8")
    rules = extract_rules(css)
    if ".tb-pdf-page" not in rules:
        print("FAIL: 没从 styles.css 提取到 .tb-pdf-page 规则 —— 规则名变了？")
        return 1

    try:
        results = run_probe(browser, rules)
    except Exception as e:
        print("FAIL: 渲染探针失败：%s" % e)
        return 1

    npass = sum(1 for r in results if r["pass"])
    nfail = len(results) - npass
    print("=== 平板 PDF 布局 · 真实渲染护栏（%s）===" % Path(browser).name)
    for r in results:
        print("  %s  %s%s" % ("PASS" if r["pass"] else "FAIL", r["name"],
                              ("  [" + r["detail"] + "]") if r["detail"] else ""))

    # ---- 源码级补充断言（渲染抓不到的语义）----
    js = APP_JS.read_text(encoding="utf-8")
    setup = js[js.find("function setupInkLayer"):js.find("function inkToggle")]
    # ⚠ 只看**代码**不看注释（注释里解释「为什么删掉 pointerleave」也算命中）
    setup_code = "\n".join(
        ln for ln in setup.splitlines()
        if not ln.strip().startswith(("*", "//", "/*"))
    )
    # ⚠ 画布尺寸：必须**先判断尺寸为 0 就跳过**，再算位图尺寸。
    #   旧写法 `Math.floor((r.width || 1) * dpr)` 会把 0 宽画布撑成 1×1 →
    #   笔迹画在 1 像素的画布上 = 看不见。
    #   → 断言「先判空、后算尺寸」，且**不能**出现 `|| 1)` 兜底。
    fit_body = setup[setup.find("function fit()"):setup.find("function pt(")]
    fit_code = "\n".join(
        ln for ln in fit_body.splitlines()
        if not ln.strip().startswith(("*", "//", "/*"))
    )
    extra = [
        ("手写层不监听 pointerleave（否则半笔被丢掉）",
         "pointerleave" not in setup_code),
        ("手写用 pointer capture 兜住划出边界的手势",
         "setPointerCapture" in setup),
        ("手写层尺寸跟着布局重测（ResizeObserver）",
         "ResizeObserver" in setup),
        ("画布尺寸为 0 时跳过（不落到 1×1）",
         "!r.width || !r.height" in fit_code),
        ("画布位图尺寸没有 `|| 1` 兜底（0 宽不会被撑成 1×1）",
         "|| 1)" not in fit_code),
    ]
    for name, cond in extra:
        if cond:
            npass += 1
            print("  PASS  %s" % name)
        else:
            nfail += 1
            print("  FAIL  %s" % name)

    print("\nPASS %d  FAIL %d" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
