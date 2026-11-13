# -*- coding: utf-8 -*-
"""平板「圈选区域操作条」的**真实渲染**护栏（2026-11-12）。

用户原话："我直接用手写笔圈一部分，然后直接能问 AI"。

静态断言（`_test_tablet_region_ask.js`）能证明代码写了，但证明不了
**浮条在浏览器里真的能点到**。而这里恰恰最容易翻车：

  · 手写层 `.tb-pdf-ink.is-on` 是 `pointer-events:auto` 且 `inset:0`
    → 它铺满整页。浮条要是没盖在它上面，点击全被手写层吃掉
    （表现就是「按了没反应」，用户会以为功能没做）。
  · 浮条挂在 `.tb-pdf-orig` 里（不是 `.tb-pdf-page`）——
    宽屏「原文+译文」并排时，页卡的原点跟原文画布**不是同一个**，
    挂错容器 → 按钮跑到译文那一栏上面去了。

这两条只有真渲染能回答，所以用 headless Edge 实测。

运行：python microbench/_test_tablet_region_render.py
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
APPJS = REPO / "microbench" / "static" / "tablet" / "app.js"

# 复用桌面端那套「带状态机的括号配平」抽取器（抽真代码用）
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "_desk_bili", REPO / "microbench" / "_test_desktop_bilingual_render.py")
_desk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_desk)
extract_function = _desk.extract_function


def extract_const(src: str, name: str) -> str:
    """抽 app.js 里的一行 const（护栏要抽真代码，不能抄）。"""
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=[^;\n]*;",
                  src)
    if not m:
        raise KeyError(f"app.js 里找不到常量 {name}")
    return m.group(0).strip()

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

RULE_PATTERNS = [
    r"(?m)^\.tb-pdf-body\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-orig\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page canvas\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-ink\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-ink\.is-on\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-ink canvas\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-trans\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-region\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-region\[hidden\]\s*\{[^}]*\}",
    r"(?m)^\.tb-pdf-region \.tb-btn\s*\{[^}]*\}",
    r"(?m)^\.tb-btn\s*\{[^}]*\}",
    r"(?m)^\.tb-btn-sm\s*\{[^}]*\}",
    r"(?m)^\.tb-btn-primary\s*\{[^}]*\}",
    # 「原文+译文」并排布局（2026-11-13 改成类名驱动，不再是 @media 强制）
    r"(?m)^\.tb-pdf-page\.lay-both[^{]*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page\.side-cols[^{]*\{[^}]*\}",
    r"(?m)^\.tb-pdf-page:not\(\.side-cols\)[^{]*\{[^}]*\}",
]

MEDIA_RE = re.compile(r"@media \(min-width: 900px\)\s*\{(?:[^{}]|\{[^{}]*\})*\}")


def find_browser() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def extract_rules(css: str) -> str:
    out = []
    for pat in RULE_PATTERNS:
        for m in re.finditer(pat, css):
            out.append(m.group(0))
    for m in MEDIA_RE.finditer(css):
        out.append(m.group(0))
    return "\n".join(out)


# ⚠ 页面结构照抄 app.js 的 renderPdfPage()：
#   页卡 > 原文层 > (canvas, 手写层, 圈选浮条)
#   浮条是**动态挂进原文层**的（showRegionBar → orig.appendChild(bar)），
#   并且排在手写层**之后**（后写的盖在上面）。
PROBE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>probe</title>
<style>
html,body{{margin:0;padding:0;height:100%;}}
/* ⚠ 真实样式表里有全局 box-sizing:border-box 重置，
   少了它量出来的宽度会虚高（译文栏左右 padding 共 32px）。 */
*{{box-sizing:border-box;}}
:root{{
  --line-soft:#E6E1D8; --surface-raised:#fff; --fg-base:#2A2724;
  --fg-strong:#15130F; --surface-quiet:#F2EEE7; --accent:#2F6B4F;
  --accent-hover:#26573F; --font-ui:system-ui,sans-serif;
}}
.tb-shell{{position:fixed;top:0;left:0;right:0;bottom:0;display:flex;flex-direction:column;}}
{rules}
</style></head>
<body>
<div class="tb-shell">
  <div class="tb-pdf-body" id="body">
    <div class="tb-pdf-page lay-both" id="page">
      <div class="tb-pdf-orig" id="orig">
        <canvas id="cv"></canvas>
        <div class="tb-pdf-anno-layer" id="anno"></div>
        <div class="tb-pdf-ink is-on" id="ink"><canvas id="inkcv"></canvas></div>
        <div class="tb-pdf-region" id="region">
          <button class="tb-btn tb-sm tb-btn-primary" id="b1">&#9997; 问这块</button>
          <button class="tb-btn tb-sm" id="b2">总结这块</button>
          <button class="tb-btn tb-sm" id="b3">清除</button>
        </div>
      </div>
      <div class="tb-pdf-trans" id="trans">译文栏</div>
    </div>
  </div>
</div>
<script>
{code}
function setSize(w,h){{
  var cv=document.getElementById('cv');
  cv.style.width=w+'px'; cv.style.height=h+'px'; cv.width=w; cv.height=h;
  var ic=document.getElementById('inkcv');
  ic.style.width=w+'px'; ic.style.height=h+'px'; ic.width=w; ic.height=h;
}}
var results=[];
function check(n,c,d){{results.push({{name:n,pass:!!c,detail:d||''}});}}
var body=document.getElementById('body');
// ⚠ 宽度要够「原文+译文」并排 —— 挂错容器的症状只在并排时才会暴露
//   （窄屏是上下堆叠，按钮跑到译文栏上也是"看起来对"的）。
//   ⚠ 并排与否现在由**真代码** applyOrigSideLayout 按实际宽度决定
//   （不再由 @media 强制），所以必须调它，探针才和真页面一致。
body.style.width='1200px';
var pageCard=document.getElementById('page');
// ⚠⚠ **先定尺寸再算布局**：applyOrigSideLayout 量的是 canvas 的宽度，
//   canvas 没尺寸时量到的是默认值 → 布局算错（2026-11-13 踩过）。
setSize(600,800);
applyOrigSideLayout(pageCard);

// 原文 600x800，圈在左栏偏上：x 10%~45%, y 20%~32%
setSize(600,800);
var orig=document.getElementById('orig');
var region=document.getElementById('region');
region.style.left='27.5%';
region.style.top='20%';
region.style.transform='translate(-50%, -118%)';
body.getBoundingClientRect();

var origR=orig.getBoundingClientRect();
var regR=region.getBoundingClientRect();
var ink=document.getElementById('ink');
var inkR=ink.getBoundingClientRect();
var transR=document.getElementById('trans').getBoundingClientRect();

check('浮条挂在原文层里（不是页卡 —— 宽屏并排时坐标系不同）',
      orig.contains(region), 'parent='+(region.parentElement&&region.parentElement.id));
check('浮条宽度 > 0、高度 > 0（能看见）',
      regR.width>20 && regR.height>20,
      'w='+regR.width.toFixed(0)+' h='+regR.height.toFixed(0));
check('浮条横向落在原文层范围内（left 百分比是相对原文层的）',
      regR.left>=origR.left-2 && regR.right<=origR.right+2,
      'bar=['+regR.left.toFixed(0)+','+regR.right.toFixed(0)+'] orig=['+
      origR.left.toFixed(0)+','+origR.right.toFixed(0)+']');
// ⚠ 布局无关的判据：浮条和译文栏的**矩形不能相交**
//   （并排时看 x，上下堆叠时看 y）—— 旧写法只看 x，
//   堆叠时译文栏的 left 就是页卡左边 → 必然误判。
var hitTrans = !(regR.right<=transR.left+2 || regR.left>=transR.right-2 ||
                 regR.bottom<=transR.top+2 || regR.top>=transR.bottom-2);
check('浮条没跑到译文栏上去（挂错容器的典型症状）',
      !hitTrans,
      'bar=['+regR.left.toFixed(0)+','+regR.top.toFixed(0)+','+
      regR.right.toFixed(0)+','+regR.bottom.toFixed(0)+'] trans=['+
      transR.left.toFixed(0)+','+transR.top.toFixed(0)+','+
      transR.right.toFixed(0)+','+transR.bottom.toFixed(0)+']');
check('宽度够时确实是「原文+译文」并排（side-cols）',
      pageCard.classList.contains('side-cols'), pageCard.className);
check('并排时译文栏在原页右侧且不超容器',
      transR.left>=origR.right-2 && transR.right<=body.clientWidth+1,
      'orig.r='+origR.right.toFixed(0)+' trans=['+transR.left.toFixed(0)+','+
      transR.right.toFixed(0)+'] body='+body.clientWidth);

// ⚠ 最关键的一条：手写层铺满整页且 pointer-events:auto，
//   浮条要是被它盖住 → 点「问这块」会变成「又画一笔」→ 用户以为没反应。
var cx=regR.left+regR.width/2, cy=regR.top+regR.height/2;
var hit=document.elementFromPoint(cx,cy);
var hitOk=false, hitDesc='null';
if(hit){{
  hitDesc=hit.id||hit.className||hit.tagName;
  hitOk=(hit===region)||(region.contains(hit));
}}
check('浮条在手写层上面（点它不会变成「又画一笔」）', hitOk, '命中='+hitDesc);
check('命中的确实是浮条里的按钮', hit&&hit.tagName==='BUTTON', '命中='+hitDesc);
check('手写层确实是铺满整页且能收指针事件（对照组）',
      Math.abs(inkR.width-origR.width)<2 && Math.abs(inkR.height-origR.height)<2 &&
      getComputedStyle(ink).pointerEvents==='auto',
      'ink='+inkR.width.toFixed(0)+'x'+inkR.height.toFixed(0));

// ---------- 容器不够宽 → 退回堆叠，浮条仍不能压到译文栏 ----------
body.style.width='600px';
applyOrigSideLayout(pageCard);
var origR2=document.getElementById('orig').getBoundingClientRect();
var transR2=document.getElementById('trans').getBoundingClientRect();
var regR2=region.getBoundingClientRect();
var hitTrans2 = !(regR2.right<=transR2.left+2 || regR2.left>=transR2.right-2 ||
                  regR2.bottom<=transR2.top+2 || regR2.top>=transR2.bottom-2);
check('宽度不够时确实退回堆叠（摘掉 side-cols）',
      !pageCard.classList.contains('side-cols'), pageCard.className);
check('堆叠时浮条仍不压到译文栏',
      !hitTrans2,
      'bar=['+regR2.left.toFixed(0)+','+regR2.top.toFixed(0)+','+
      regR2.right.toFixed(0)+','+regR2.bottom.toFixed(0)+'] trans=['+
      transR2.left.toFixed(0)+','+transR2.top.toFixed(0)+','+
      transR2.right.toFixed(0)+','+transR2.bottom.toFixed(0)+']');

// 隐藏时不能占位（否则空白的黑条会浮在页面上）
region.hidden=true;
body.getBoundingClientRect();
var hidR=region.getBoundingClientRect();
check('隐藏后不占位（display:none，不是 visibility）',
      hidR.width===0 && hidR.height===0,
      'w='+hidR.width.toFixed(0)+' h='+hidR.height.toFixed(0));

document.title='RESULT:'+JSON.stringify(results);
</script></body></html>
"""


def run_probe(browser: str, rules: str, code: str = "") -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "probe.html"
        html.write_text(PROBE_HTML.format(rules=rules, code=code), encoding="utf-8")
        cmd = [
            browser, "--headless", "--disable-gpu", "--no-sandbox",
            # ⚠ 必须给**宽窗口**：宽屏才会走 `@media (min-width:900px)` 的
            #   「原文+译文并排」布局 —— 挂错容器的症状只在并排时才会暴露
            #   （窄屏是上下堆叠，按钮跑到译文栏上也是"看起来对"的）。
            "--window-size=1400,900",
            "--virtual-time-budget=3000", "--dump-dom",
            html.resolve().as_uri(),
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("headless 浏览器超时")
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
    if ".tb-pdf-region" not in rules:
        print("FAIL: 没从 styles.css 提取到 .tb-pdf-region 规则 —— 规则名变了？")
        return 1

    # ⚠ 抽**真代码**：并排/堆叠现在由 app.js 的 applyOrigSideLayout 决定，
    #   护栏不跑它 → 探针的布局和真页面不一致（2026-11-13 踩过）。
    code = ""
    try:
        src = APPJS.read_text(encoding="utf-8")
        code = "\n\n".join([
            extract_const(src, "TB_SIDE_MIN_TRANS_WIDTH"),
            extract_const(src, "TB_SIDE_GAP"),
            extract_function(src, "applyOrigSideLayout"),
        ])
    except (KeyError, ValueError) as e:
        print("FAIL: 抽取真代码失败：%s" % e)
        return 1

    try:
        results = run_probe(browser, rules, code)
    except Exception as e:
        print("FAIL: 渲染探针失败：%s" % e)
        return 1

    npass = sum(1 for r in results if r["pass"])
    nfail = len(results) - npass
    print("=== 圈选操作条 · 真实渲染护栏（%s）===" % Path(browser).name)
    for r in results:
        print("  %s  %s%s" % ("PASS" if r["pass"] else "FAIL", r["name"],
                              ("  [" + r["detail"] + "]") if r["detail"] else ""))
    print("\nPASS %d  FAIL %d" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
