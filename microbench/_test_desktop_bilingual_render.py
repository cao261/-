# -*- coding: utf-8 -*-
"""桌面端「对照模式（译文按原文排版）」的**真实渲染**护栏（2026-11-12）。

为什么静态断言不够：静态断言只能证明「代码写了」，证明不了
**左栏段落的译文真的在左栏、右栏段落的译文真的在右栏**。
而这恰恰是整个功能的命门 —— 用户原话：
    "原文是双栏，他们也是双栏"

所以这里用 headless Edge 真渲染一遍：
把 index.html 里**真实的** `renderBilingualPage` 函数抽出来，
配上前端同款的 CSS 和几个 mock，喂一假的两栏数据，然后量坐标。

⚠ 抽的是**真代码**，不是抄一遍 —— 抄的那份通过测试没有意义。

运行：python microbench/_test_desktop_bilingual_render.py
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
INDEX = REPO / "microbench" / "static" / "index.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


# --------------------------------------------------------------------------
# 从 index.html 里抽 JS 函数（带字符串/模板串/注释感知的括号配平）
# --------------------------------------------------------------------------
def _scan_js(src: str, start: int) -> int:
    """返回从 start 开始的**函数体**结束位置（下一个顶层 `function` 之前）。

    做法：找到第一个 `{`，然后带状态机地配平括号（跳过字符串/模板串/注释/
    **正则字面量**）。

    ⚠ **正则字面量必须单独处理**（2026-11-12 踩过）：
       `h.replace(/"/g, "&quot;")` 这种正则里有个**引号**——
       只按字符串扫描的话，那个 `"` 会把状态机带偏
       （把 `/g, &quot` 当成字符串内容），后面的花括号就数错了
       → 抽出来的函数**被截断**（实测 formatMarkdownLite 只抽到 2.4KB，
       后半截的表格渲染全丢了）。
    """
    i = src.index("{", start)
    depth = 0
    n = len(src)
    # 上一个有效字符（用来判断 `/` 是「除号」还是「正则开头」）
    prev = ""
    while i < n:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            prev = " "
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
            prev = " "
            continue
        if ch in ("'", '"'):
            q = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == q:
                    break
                i += 1
            i += 1
            prev = "x"
            continue
        if ch == "`":
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == "`":
                    break
                i += 1
            i += 1
            prev = "x"
            continue
        # 正则字面量：/ 前面不能是 标识符/数字/)/]  → 那是除号
        if ch == "/" and prev not in ("", "x", "0123456789)]"):
            j = i + 1
            in_class = False
            ok = False
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "\n":
                    break
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    ok = True
                    break
                j += 1
            if ok:
                i = j + 1
                prev = "x"
                continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        if not ch.isspace():
            prev = ch
        i += 1
    raise ValueError("括号没配平")


def extract_function(src: str, name: str) -> str:
    """抽出函数源码。

    ⚠ **必须连 `async` 一起抽**（2026-11-12 踩过）：
       原来正则只找 `function name(` → 抽出来的是
       `function renderFormulaShot(...){ await ... }`
       → **非 async 函数里用 await = SyntaxError**，
       整页脚本不执行 → 浏览器里啥也不发生（看起来像"卡住"）。
    """
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise KeyError(f"index.html 里找不到函数 {name}")
    end = _scan_js(src, m.start())
    return src[m.start():end]


def extract_const(src: str, name: str) -> str:
    """抽一行 const（护栏要抽真代码，不能抄）。

    ⚠ 不能用 `.*?;` —— 行尾注释里没有 `;`，会一路吃到**下一行**的 `;`
       （实测把两个 const 粘成了一坨）。用 `[^;\\n]*;` 只吃本行。
    """
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=[^;\n]*;",
                  src)
    if not m:
        raise KeyError(f"index.html 里找不到常量 {name}")
    return m.group(0).strip()


def extract_css(html: str) -> str:
    """抽出对照模式用到的 CSS 规则（连同 @media 块）。"""
    out = []
    for pat in [
        r"(?m)^\s*\.bilingual-page\s*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-span\s*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-cols\s*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-col\s*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-block\s*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-block\.kind-heading[^{]*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-block\.kind-formula[^{]*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-block\.kind-table[^{]*\{[^}]*\}",
        r"(?m)^\s*\.bb-orig\s*\{[^}]*\}",
        r"(?m)^\s*\.bb-trans\s*\{[^}]*\}",
        r"(?m)^\s*\.bilingual-note\s*\{[^}]*\}",
        # 「原页图」并排布局要用到页卡/画布的样式
        r"(?m)^\s*\.pdf-page-block\s*\{[^}]*\}",
        r"(?m)^\s*\.pdf-page-canvas-wrapper\s*\{[^}]*\}",
        r"(?m)^\s*\.pdf-page-canvas\s*\{[^}]*\}",
        r"(?m)^\s*\.pdf-page-label\s*\{[^}]*\}",
        # ⚠ 「原页图 + 对照译文」并排的规则**不在 @media 里**了
        #   （2026-11-13：并排与否由 JS 按**阅读器栏实际宽度**决定，
        #    @media 看的是视口宽 → 1600px 视口下阅读器栏只有 790px，
        #    并排后译文列只剩 43px）。
        r"(?m)^\s*\.pdf-page-block\.bilingual-orig-wide[^{]*\{[^}]*\}",
    ]:
        out.extend(m.group(0) for m in re.finditer(pat, html))
    # 「对照模式 + 原页图」的并排布局在 @media 块里（宽屏才并排）——
    # 简单正则抽不到，**必须**把整个 @media 块一起抽出来。
    for m in re.finditer(r"@media[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", html):
        if "bilingual-orig" in m.group(0):
            out.append(m.group(0))
    return "\n".join(out)


# --------------------------------------------------------------------------
# 探针页面：用**真函数** + mock，跑一版权威的假两栏数据
# --------------------------------------------------------------------------
PROBE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>probe</title>
<style>
html,body{{margin:0;padding:0;background:#fff;}}
:root{{
  --surface-raised:#fff; --fg-strong:#15130F; --fg-subtle:#8a857c;
  --fg-muted:#5d584f; --line-hairline:#E6E1D8; --accent-2:#C4703A;
  --font-cn:"Microsoft YaHei","PingFang SC",sans-serif;
  --font-sans:system-ui,sans-serif; --s-3:6px; --s-4:10px; --s-5:14px;
  --r-md:8px; --t-sm:12px;
}}
#host{{width:1200px;margin:0 auto;}}
{rules}
</style></head>
<body>
<div id="host">
  <div class="pdf-page-block" id="page-block">
    <div class="pdf-page-label">PAGE 1</div>
    <div class="pdf-page-canvas-wrapper" id="canvas-wrap"
         style="width:520px;height:660px;position:relative;background:#fff;">
      <canvas class="pdf-page-canvas" style="width:520px;height:660px;"></canvas>
    </div>
    <div class="bilingual-page" id="bi-page"></div>
  </div>
</div>
<script>
// ---- mock：前端那几依赖的外部函数 ----
function formatMarkdownLite(t){{
  // 只保留段落与表格两种形态，够验证渲染链路
  var lines = String(t||'').split('\\n');
  var out='', inTable=false;
  for (var i=0;i<lines.length;i++){{
    var L=lines[i].trim();
    if (L.charAt(0)==='|'){{
      if (!inTable){{ out += '<table class="md-table"><tbody>'; inTable=true; }}
      out += '<tr><td>'+L.replace(/^\\||\\|$/g,'')+'</td></tr>';
      continue;
    }}
    if (inTable){{ out += '</tbody></table>'; inTable=false; }}
    if (L) out += '<p>'+L+'</p>';
  }}
  if (inTable) out += '</tbody></table>';
  return out;
}}
function renderFormulaImages(el){{
  // 把 ⟦FORMULA:x,y,z,w⟧ 换成一张假图（真实前端换成 PDF 裁图）
  el.innerHTML = el.innerHTML.replace(/⟦FORMULA:[^⟧]*⟧/g,
    '<img class="fake-formula" alt="formula">');
}}
function renderMathIn(el){{}}
function getJson(url){{ return Promise.resolve({{blocks:[],blocks_version:0}}); }}
function showToast(){{}}
function _scheduleAutoSnapshot(){{}}
function fetchModelStatus(){{}}
function _recordDiag(){{}}
function escapeHtml(s){{ return String(s==null?'':s); }}

// ---- 被测的真实代码（从 index.html 抽出来）----
{code}

// ---- 假数据：一页两栏，左栏 2 段、右栏 2 段，外加整宽标题 ----
var _pdfState = {{
  paperId: 'probe', relativePath: 'x.pdf', bilingual: true,
  pages: [{{ num: 1, blocks: [
    {{ i:0, kind:'body',    col:'S', bbox:[60,21,562,31], text:'February 2008 ME Laminate Composites' }},
    {{ i:1, kind:'body',    col:'L', bbox:[60,453,299,480], text:'Fig.2. An optical micrograph of the fracture surface.' }},
    {{ i:2, kind:'heading', col:'L', bbox:[60,492,299,505], text:'(3) Ceramic and Terfenol-D Alloy ME Composites' }},
    {{ i:3, kind:'formula', col:'L', bbox:[76,552,299,578], text:'vdp em' }},
    {{ i:4, kind:'body',    col:'R', bbox:[323,227,562,290], text:'Fig.3. Transverse ME voltage coefficient at room temperature.' }},
    {{ i:5, kind:'table',   col:'R', bbox:[323,302,562,392], text:'' }}
  ], blockTranslations: {{
    0:'2008 年 2 月《层状复合材料》',
    1:'图 2. 断裂面的光学显微镜图像。',
    2:'（3）陶瓷与 Terfenol-D 合金 ME 复合材料',
    3:'⟦FORMULA:76.4,552.2,299.6,577.9⟧',
    4:'图 3. 室温下横向磁电电压系数随磁场的变化。',
    5:'| 参数 | 数值 |\\n| d31 | 16.5 |'
  }} }}]
}};
document.getElementById('bi-page').id = 'bilingual-page-1';
renderBilingualPage(1);

// ---- 量坐标 ----
var results=[];
function check(n,c,d){{results.push({{name:n,pass:!!c,detail:d||''}});}}
var host=document.getElementById('bilingual-page-1');
var cols=host.querySelector('.bilingual-cols');
var colEls=host.querySelectorAll('.bilingual-col');
var blocks=host.querySelectorAll('.bilingual-block');

check('对照视图渲染出了内容', blocks.length>=6, '块数='+blocks.length);
check('左右栏分成两列', colEls.length===2, '列数='+colEls.length);
var lR=colEls[0].getBoundingClientRect(), rR=colEls[1].getBoundingClientRect();
check('左栏在左、右栏在右（不是上下堆叠）', lR.left < rR.left,
      'L.left='+lR.left.toFixed(0)+' R.left='+rR.left.toFixed(0));
check('两栏都有实际宽度', lR.width>40 && rR.width>40,
      'L.w='+lR.width.toFixed(0)+' R.w='+rR.width.toFixed(0));
check('两栏横向并排（不是上下叠）', Math.abs(lR.top-rR.top)<8,
      'L.top='+lR.top.toFixed(0)+' R.top='+rR.top.toFixed(0));

// 整宽标题块应该在两栏**上面**
var span=host.querySelector('.bilingual-span');
check('整宽块（标题）单独放在两栏上面', !!span && span.getBoundingClientRect().bottom <= cols.getBoundingClientRect().top+2,
      span? ('span.bottom='+span.getBoundingClientRect().bottom.toFixed(0)+' cols.top='+cols.getBoundingClientRect().top.toFixed(0)) : '没有 span');

// 段落对段落：左栏第 2 块是标题，右栏第 1 块是正文
var lBlocks=colEls[0].querySelectorAll('.bilingual-block');
var rBlocks=colEls[1].querySelectorAll('.bilingual-block');
check('左栏里只有左栏的段落', lBlocks.length===3, '左栏块数='+lBlocks.length);
check('右栏里只有右栏的段落', rBlocks.length===2, '右栏块数='+rBlocks.length);
check('左栏第 2 块是标题（kind-heading）', lBlocks[1] && lBlocks[1].className.indexOf('kind-heading')>=0,
      lBlocks[1]?lBlocks[1].className:'');
check('左栏第 3 块是公式（kind-formula）', lBlocks[2] && lBlocks[2].className.indexOf('kind-formula')>=0,
      lBlocks[2]?lBlocks[2].className:'');
check('公式块不显示原文（碎片没法看）',
      lBlocks[2] && !lBlocks[2].querySelector('.bb-orig'), '');
check('公式块贴的是公式原图（不是文字）',
      lBlocks[2] && !!lBlocks[2].querySelector('img.fake-formula'), '');
check('右栏第 2 块是表格（kind-table）', rBlocks[1] && rBlocks[1].className.indexOf('kind-table')>=0,
      rBlocks[1]?rBlocks[1].className:'');
check('表格块渲染成真正的 <table>',
      rBlocks[1] && !!rBlocks[1].querySelector('table'), '');
check('标题块的译文是加粗的',
      lBlocks[1] && getComputedStyle(lBlocks[1].querySelector('.bb-trans')).fontWeight >= 600,
      lBlocks[1]? getComputedStyle(lBlocks[1].querySelector('.bb-trans')).fontWeight : '');
check('正文块同时有原文和译文',
      lBlocks[0] && lBlocks[0].querySelector('.bb-orig') && lBlocks[0].querySelector('.bb-trans'), '');
check('译文不是空的（有内容）',
      lBlocks[0] && (lBlocks[0].querySelector('.bb-trans').textContent||'').trim().length>3,
      lBlocks[0]? (lBlocks[0].querySelector('.bb-trans').textContent||'').trim().slice(0,30):'');

// ---------- 「🖼 原页图」开关（遗留项：对照视图里看不到图片/图表）----------
var pageBlock=document.getElementById('page-block');
var canvasWrap=document.getElementById('canvas-wrap');

// ① 开着对照、**不**显示原页图 → 原页 canvas 必须藏起来（这是旧行为）
_pdfState.bilingualOrig=false;
_showBilingualVisibility(true);
check('对照模式且不显示原页图 → 原页 canvas 被藏起来',
      canvasWrap.style.display==='none', 'display='+canvasWrap.style.display);
check('不显示原页图时页卡没有 bilingual-orig 类',
      !pageBlock.classList.contains('bilingual-orig'), pageBlock.className);

// ② 开着对照 + 显示原页图 → canvas 必须**可见**
_pdfState.bilingualOrig=true;
_showBilingualVisibility(true);
check('对照模式 + 原页图 → 原页 canvas 可见（图看得见了）',
      canvasWrap.style.display!=='none', 'display='+canvasWrap.style.display);
check('页卡带上了 bilingual-orig 类',
      pageBlock.classList.contains('bilingual-orig'), pageBlock.className);

// ③ 宽屏 → 原页图在左、对照译文在右（并排，不是上下堆叠）
var cR=canvasWrap.getBoundingClientRect();
var bR=host.getBoundingClientRect();
check('宽屏下原页图在左、对照译文在右（并排）',
      cR.left < bR.left, 'canvas.left='+cR.left.toFixed(0)+' bilingual.left='+bR.left.toFixed(0));
check('原页图和对照译文横向不重叠',
      cR.right <= bR.left+2, 'canvas.right='+cR.right.toFixed(0)+' bilingual.left='+bR.left.toFixed(0));
check('并排时对照译文列够宽可读（>=380）',
      bR.width >= 380, 'w='+bR.width.toFixed(0));
check('并排时页卡带上了 bilingual-orig-wide 类',
      pageBlock.classList.contains('bilingual-orig-wide'), pageBlock.className);

// ---------- ⑤ 阅读器栏不够宽 → 必须**退回上下堆叠**（2026-11-13 回归）----------
//   ⚠ 这条是实测算出来的真缺陷：原来并排与否只看**视口**宽（@media 900px），
//   而 1600px 的窗口里阅读器栏只有 ~790px → 并排后译文列只剩 **43px**，
//   挤成一条竖线根本没法读。
document.getElementById('host').style.width='700px';
_pdfState.bilingualOrig=true;
_showBilingualVisibility(true);
var cR2=canvasWrap.getBoundingClientRect();
var bR2=host.getBoundingClientRect();
check('阅读器栏不够宽时退回上下堆叠（不再硬挤）',
      !pageBlock.classList.contains('bilingual-orig-wide'), pageBlock.className);
check('堆叠时对照译文在原页图**下方**',
      bR2.top >= cR2.bottom-2,
      'bi.top='+bR2.top.toFixed(0)+' canvas.bottom='+cR2.bottom.toFixed(0));
check('堆叠时对照译文列是整栏宽（可读，不是 43px 竖线）',
      bR2.width >= 600, 'w='+bR2.width.toFixed(0));
check('堆叠时原页图与译文不横向压在一起',
      !(bR2.top < cR2.bottom-2 && bR2.left < cR2.right-2 && bR2.right > cR2.left+2),
      'bi='+bR2.left.toFixed(0)+'..'+bR2.right.toFixed(0)+
      ' canvas='+cR2.left.toFixed(0)+'..'+cR2.right.toFixed(0));
document.getElementById('host').style.width='1200px';
_pdfState.bilingualOrig=false;
_showBilingualVisibility(false);

// ④ 关掉对照模式 → 原页 canvas 回到正常显示
_pdfState.bilingualOrig=false;
_showBilingualVisibility(false);
check('关掉对照模式 → 原页 canvas 正常显示',
      canvasWrap.style.display!=='none', 'display='+canvasWrap.style.display);
check('关掉对照模式 → 页卡的 bilingual-orig 类被摘掉',
      !pageBlock.classList.contains('bilingual-orig'), pageBlock.className);

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
        cmd = [
            browser, "--headless", "--disable-gpu", "--no-sandbox",
            "--window-size=1400,1000",
            "--virtual-time-budget=4000", "--dump-dom",
            html.resolve().as_uri(),
        ]
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

    html = INDEX.read_text(encoding="utf-8")
    rules = extract_css(html)
    if ".bilingual-cols" not in rules:
        print("FAIL: 没从 index.html 提取到 .bilingual-cols 规则 —— 规则名变了？")
        return 1

    # 抽真代码
    try:
        code = "\n\n".join([
            extract_const(html, "_BILINGUAL_MIN_TRANS_WIDTH"),
            extract_function(html, "_bilingualHost"),
            extract_function(html, "_bilingualRenderInto"),
            extract_function(html, "renderBilingualPage"),
            extract_function(html, "_applyBilingualOrigLayout"),
            extract_function(html, "_showBilingualVisibility"),
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
    print("=== 桌面端对照模式 · 真实渲染护栏（%s）===" % Path(browser).name)
    for r in results:
        print("  %s  %s%s" % ("PASS" if r["pass"] else "FAIL", r["name"],
                              ("  [" + r["detail"] + "]") if r["detail"] else ""))
    print("\nPASS %d  FAIL %d" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
