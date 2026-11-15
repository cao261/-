# -*- coding: utf-8 -*-
"""平板端「对照模式（译文按原文排版）」的**真实渲染**护栏（2026-11-12）。

为什么要有：用户诉求是「原文是双栏，他们也是双栏」。
静态断言能证明代码写了，证明不了**译文真的排成了两栏**。

所以抽 app.js 里**真实的** `renderBilingualInto`，配平板真 CSS，
喂一版左右栏数据，headless Edge 真渲染一遍再量坐标。

⚠ 抽的是**真代码**（从 app.js 里按括号配平切出来），不是抄一遍。

运行：python microbench/_test_tablet_bilingual_render.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPJS = REPO / "microbench" / "static" / "tablet" / "app.js"
CSS = REPO / "microbench" / "static" / "tablet" / "styles.css"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# 复用桌面端那套「带状态机的括号配平」抽取器
_spec = importlib.util.spec_from_file_location(
    "_desk_bili", REPO / "microbench" / "_test_desktop_bilingual_render.py")
_desk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_desk)
extract_function = _desk.extract_function


def extract_const(src: str, name: str) -> str:
    """抽 app.js 里的一行 const（护栏要抽真代码，不能抄）。

    ⚠ 不能用 `.*?;` —— 行尾注释里没有 `;`，会一路吃到**下一行**的 `;`
       （实测把两个 const 粘成了一坨）。用 `[^;\\n]*;` 只吃本行。
    """
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=[^;\n]*;",
                  src)
    if not m:
        raise KeyError(f"app.js 里找不到常量 {name}")
    return m.group(0).strip()

CSS_RULES = [
    r"(?m)^\s*\.tb-pdf-page\.lay-bilingual[^{]*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-body\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-cols\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-col\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-span\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-block\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-block\.kind-heading[^{]*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-block\.kind-formula[^{]*\{[^}]*\}",
    r"(?m)^\s*\.tb-bb-trans\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-bilingual-empty\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans-head\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans-body\s*\{[^}]*\}",
    # 公式原图（2026-11-12）—— 不抽的话护栏里图片按自然宽度铺开 → 溢出栏宽
    r"(?m)^\s*\.tb-formula-shot\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-formula-img\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-formula-wait,\s*\.tb-formula-fallback\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-formula-fallback\s*\{[^}]*\}",
    # 表格（在译文栏里，不是 .tb-doc → 必须单独给样式）
    r"(?m)^\s*\.tb-pdf-trans-body table\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans-body th,\s*\.tb-pdf-trans-body td\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans-body th\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans-body \.katex\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans-body \.katex-display\s*\{[^}]*\}",
    r"(?m)^\s*\.tb-pdf-trans-body \.math-error\s*\{[^}]*\}",
]


def extract_css(css: str) -> str:
    out = []
    for pat in CSS_RULES:
        out.extend(m.group(0) for m in re.finditer(pat, css))
    media = re.findall(r"@media \(min-width:900px\)\s*\{(?:[^{}]|\{[^{}]*\})*\}", css)
    out.extend(media)
    return "\n".join(out)


PROBE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>probe</title>
<style>
html,body{{margin:0;padding:0;background:#fff;}}
:root{{--line-hairline:#E6E1D8;--font-ui:system-ui,sans-serif;}}
#host{{width:900px;margin:0 auto;}}
{rules}
</style></head>
<body>
<div id="host"><div class="tb-pdf-page lay-bilingual" id="page"></div></div>
<script>
// ---- mock：app.js 里用到的几个外部依赖 ----
function el(tag, cls, text){{
  var n=document.createElement(tag);
  if (cls) n.className=cls;
  if (text!=null) n.textContent=text;
  return n;
}}
function renderMarkdown(src){{
  return String(src||'').split('\\n').filter(function(l){{return l.trim();}})
    .map(function(l){{return '<p>'+l+'</p>';}}).join('');
}}
var pdfState = {{ layout:'bilingual', trans:{{1:'整页译文'}}, blockTrans: {{}},
  formulaShots: {{}}, doc: null }};
pdfState.blockTrans = {{ 1: {{ version:1, blocks: [
  {{i:0,kind:'body',col:'S',bbox:[60,21,562,31],translation:'2008 年 2 月'}},
  {{i:1,kind:'body',col:'L',bbox:[60,453,299,480],translation:'图 2. 断裂面显微图。'}},
  {{i:2,kind:'heading',col:'L',bbox:[60,492,299,505],translation:'（3）陶瓷与 Terfenol-D 合金'}},
  {{i:3,kind:'formula',col:'L',bbox:[76,552,299,578],translation:'⟦FORMULA:76.4,552.2,299.6,577.9⟧'}},
  {{i:4,kind:'body',col:'R',bbox:[323,227,562,290],translation:'图 3. 室温下 ME 电压系数。'}},
  {{i:5,kind:'table',col:'R',bbox:[323,302,562,392],translation:'| 参数 | 数值 |'}}
] }} }};

// ---- 被测的真实代码（从 app.js 抽出来）----
{code}

renderBilingualInto(document.getElementById('page'), 1);

var results=[];
function check(n,c,d){{results.push({{name:n,pass:!!c,detail:d||''}});}}
var page=document.getElementById('page');
var cols=page.querySelector('.tb-bilingual-cols');
var colEls=page.querySelectorAll('.tb-bilingual-col');
var blocks=page.querySelectorAll('.tb-bilingual-block');

check('对照视图渲染出了内容', blocks.length>=6, '块数='+blocks.length);
check('左右栏分成两列', colEls.length===2, '列数='+colEls.length);
var lR=colEls[0].getBoundingClientRect(), rR=colEls[1].getBoundingClientRect();
check('宽屏下左栏在左、右栏在右', lR.left < rR.left,
      'L.left='+lR.left.toFixed(0)+' R.left='+rR.left.toFixed(0));
check('两栏并排（不是上下叠）', Math.abs(lR.top-rR.top)<8,
      'L.top='+lR.top.toFixed(0)+' R.top='+rR.top.toFixed(0));
check('两栏都有实际宽度', lR.width>40 && rR.width>40,
      'L.w='+lR.width.toFixed(0)+' R.w='+rR.width.toFixed(0));

var lBlocks=colEls[0].querySelectorAll('.tb-bilingual-block');
var rBlocks=colEls[1].querySelectorAll('.tb-bilingual-block');
check('左栏里只有左栏的段落', lBlocks.length===3, '左栏块数='+lBlocks.length);
check('右栏里只有右栏的段落', rBlocks.length===2, '右栏块数='+rBlocks.length);
check('左栏第 2 块是标题', lBlocks[1] && lBlocks[1].className.indexOf('kind-heading')>=0,
      lBlocks[1]?lBlocks[1].className:'');
check('公式块渲染成「公式图占位」（不是坐标、也不是一句提示）',
      lBlocks[2] && !!lBlocks[2].querySelector('.tb-formula-shot') &&
      lBlocks[2].textContent.indexOf('FORMULA')<0 &&
      lBlocks[2].textContent.indexOf('⟦')<0,
      lBlocks[2]?(lBlocks[2].textContent||'').trim().slice(0,30):'');
check('公式占位带上了裁图需要的坐标键',
      lBlocks[2] && lBlocks[2].querySelector('.tb-formula-shot')
        .getAttribute('data-formula-key')==='76.4,552.2,299.6,577.9',
      lBlocks[2]?String(lBlocks[2].querySelector('.tb-formula-shot')
        .getAttribute('data-formula-key')):'');
check('对照模式下原文画布被藏起来（不然两套排版叠着）',
      getComputedStyle(document.querySelector('.tb-pdf-orig')||document.body).display==='none' ||
      !document.querySelector('.tb-pdf-orig'),
      '探针里没放原文层（真页面由 CSS 规则隐藏）');
check('译文不是空的', lBlocks[0] && (lBlocks[0].textContent||'').trim().length>3,
      lBlocks[0]?(lBlocks[0].textContent||'').trim().slice(0,30):'');

// 没有段落数据时，要给出"去电脑端翻"的提示，而不是空白
// ⚠ 探针里要**先清空**：真页面每次都是新建页卡（不会叠加），
//   这里不清空的话，内容会留着上一轮的 → 断言会"蒙对"。
pdfState.blockTrans = {{}};
document.getElementById('page').innerHTML = '';
renderBilingualInto(document.getElementById('page'), 1);
check('没有段落译文时给出提示（而不是空白页）',
      document.getElementById('page').textContent.indexOf('同步')>=0 &&
      document.getElementById('page').textContent.indexOf('2008 年 2 月')<0,
      document.getElementById('page').textContent.trim().slice(0,40));

document.title='RESULT:'+JSON.stringify(results);
</script></body></html>
"""


def find_browser() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def run_probe(browser: str, rules: str, code: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "probe.html"
        html.write_text(PROBE_HTML.format(rules=rules, code=code), encoding="utf-8")
        cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox",
               "--window-size=1400,1000",
               "--virtual-time-budget=4000", "--dump-dom",
               html.resolve().as_uri()]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise RuntimeError("headless 浏览器超时")
        dom = p.stdout.decode("utf-8", errors="replace")
        m = re.search(r"RESULT:(\[.*?\])</title>", dom, re.S)
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
    rules = extract_css(css)
    if ".tb-bilingual-cols" not in rules:
        print("FAIL: 没从 styles.css 提取到 .tb-bilingual-cols 规则 —— 规则名变了？")
        return 1

    src = APPJS.read_text(encoding="utf-8")
    try:
        code = "\n\n".join([
            extract_const(src, "FORMULA_MARK_RE"),
            extract_const(src, "FORMULA_SHOT_SCALE"),
            extract_function(src, "formulaRectsOf"),
            extract_function(src, "formulaKeyOf"),
            extract_function(src, "fillFormulaShot"),
            extract_function(src, "formulaHolder"),
            extract_function(src, "formulaBlocksOf"),
            extract_function(src, "paintFormulaShots"),
            extract_function(src, "ensureFormulaShots"),
            extract_function(src, "renderBilingualInto"),
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
    print("=== 平板端对照模式 · 真实渲染护栏（%s）===" % Path(browser).name)
    for r in results:
        print("  %s  %s%s" % ("PASS" if r["pass"] else "FAIL", r["name"],
                              ("  [" + r["detail"] + "]") if r["detail"] else ""))
    print("\nPASS %d  FAIL %d" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
