# -*- coding: utf-8 -*-
"""阅读器浮层护栏：Toast 层级 / 夜间对比度 / Esc 栈 / 缩放防抖 —— **真实渲染**。

2026-11-25 全代码库审计抓到的四个缺陷（全部在本护栏里被钉住）：

  ① **Toast 被阅读器整块盖住**（P0）
     `#toast` 与 `#reader-modal` 原来都是 `z-index: 1000`，而
     `#reader-modal` 在 DOM 里排在 `#toast` **后面** → 同层级下后画的
     盖住先画的 → 阅读器一打开，**所有 toast 全部不可见**
     （续读回位、保存批注、替换版提示、模型降级提醒…全被吞掉）。

  ② **夜间模式整张深色卡片隐形**（P0）
     `.studio-dark-card` 底色 = `--dark-bg`（夜间 = `--surface-raised`
     = #1E1C17 深色），而 `--dark-text` 跟着 `--fg-inverse` 走，夜间
     `--fg-inverse` 被改成 **#15140F（近黑）** → **深底深字**，
     实测对比度只剩 ~1.07:1（组会周报 / 快速沉淀避坑经验 整张看不见）。

  ③ **一次 Esc 关掉整个阅读器**（P1）
     阅读器自己的 Esc 处理器与弹窗的处理器是**同一个节点（document）
     上的两个监听器** → `stopPropagation` 只阻止往父节点传，
     **不阻止同一节点上的其它监听器** → 按一次 Esc 既关弹窗又关阅读器。
     而且 `#sync-modal` / `#anno-editor` / `#anno-drawer` 压根没登记
     到任何 Esc 机制 → 这些浮层开着时按 Esc = 直接关掉阅读器。

  ④ **缩放没有防抖**（P1）
     连点「放大」时每次点击都排一次重绘 → 多个重绘**并发**跑：
     各自清空列表、各自重建 `_pdfState.pages` → 后半篇译文被丢掉。

⚠ 这份护栏**不读源码文本当断言**（`assert "xxx" in src` 那种）：
  那种断言改坏行为也照样绿（AGENTS §4.92）。
  → 这里把 index.html 里的**真 CSS** 与**真 JS** 抽出来，喂进探针页面，
    用 headless 浏览器**真渲染**后量层级、量对比色、跑真实按键。

运行：python microbench/_test_reader_overlay_render.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

try:  # 作为脚本跑 / 作为包被导入，两种都要能用
    from _headless_browser import dump_dom, find_browser as _find_browser
except ImportError:  # pragma: no cover
    from microbench._headless_browser import dump_dom, find_browser as _find_browser

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "microbench" / "static" / "index.html"


# ---------------------------------------------------------------------------
# 从 index.html 里抽真代码
# ---------------------------------------------------------------------------
def _scan_js(src: str, start: int) -> int:
    """返回从 start 开始的函数体结束位置（**真代码**，不是抄一遍）。"""
    i = src.index("{", start)
    depth = 0
    n = len(src)
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
    """抽出一个函数（连 `async` 一起抽）。"""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise KeyError(f"index.html 里找不到函数 {name}")
    end = _scan_js(src, m.start())
    return src[m.start():end]


def extract_block(src: str, start_marker: str, end_marker: str) -> str:
    """抽一段连续代码（从 start_marker 到 end_marker 之前）。"""
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def extract_decl(src: str, name: str) -> str:
    """抽一行 const/let/var 声明（护栏要抽真代码，不能抄）。"""
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name)
                  + r"\s*=[^;\n]*;", src)
    if not m:
        raise KeyError(f"index.html 里找不到声明 {name}")
    return m.group(0).strip()


def extract_style_block(src: str) -> str:
    m = re.search(r"<style[^>]*>(.*?)</style>", src, re.S)
    if not m:
        raise KeyError("index.html 里找不到 <style> 块")
    return m.group(1)


def extract_reader_esc_handler(src: str) -> str:
    """抽阅读器自己的 Esc 处理器（`_readerEscHandler = (e) => {...};`）。

    ⚠ 它是**赋值表达式**不是函数声明 → 不能用 extract_function。
    """
    m = re.search(r"_readerEscHandler\s*=\s*\(e\)\s*=>\s*\{", src)
    if not m:
        raise KeyError("index.html 里找不到 _readerEscHandler")
    end = _scan_js(src, m.end() - 1)
    return src[m.start():end] + ";"


# ---------------------------------------------------------------------------
# 探针页面：真 CSS + 真 JS + 桩件
# ---------------------------------------------------------------------------
PROBE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="{theme}"><head><meta charset="utf-8"><title>probe</title>
<style>
{style}
html,body{{margin:0;padding:0;background:#111;}}
</style></head>
<body>
<div id="pdf-pages-list"></div>
<!--
  ⚠ DOM 顺序必须与真实 index.html **一致**：
  真实页面里 #toast 在前（L2796）、#reader-modal 在后（L12034）
  → 同层级时后画的阅读器盖住 toast → 这正是 ① 的根因。
  探针要是把顺序摆反，层级断言就永远绿（测不到东西）。
-->
<div id="toast" style="display:block;">已回到上次读到的位置</div>
<div id="reader-modal" style="display:flex;">
  <div class="reader-shell"><div class="reader-body"><div class="reader-pane">
    <div class="studio-dark-card">
      <div class="studio-title">组会周报</div>
      <div class="studio-block-note">快速沉淀避坑经验</div>
      <label class="field-label">标签</label>
    </div>
  </div></div></div>
</div>
<div id="anno-editor" style="display:none;"></div>
<div id="anno-drawer" style="display:none;"></div>
<div id="sync-modal" style="display:none;"></div>
<script>
var __closed = {{ reader: 0, annoEditor: 0, annoDrawer: 0, sync: 0, zoom: 0 }};
function closeReaderModal(){{ __closed.reader++; }}
function _captureReadPos(){{}}
function _persistLocalState(){{}}
function _dedupePdfBlocks(){{}}
function updateZoomLabel(){{}}
function loadPdfDualColumn(opts){{ __closed.zoom++; }}
// 缩放路径上的画布清理（不是本次要验的东西，用桩件顶掉）
function _pdfCancelAllCanvasRenders(){{}}
var _pdfState = {{
  pdfDoc: {{}}, rendering: {{ generation: 0, cancelled: false }}, pages: [],
}};
var _pdfZoom = 1.0;
var _readerState = {{}};

// ---------------- 真代码 ----------------
{code}

// ---------------- 探针 ----------------
var results = [];
function check(name, ok, detail){{ results.push({{name:name, pass:!!ok, detail:detail||''}}); }}
function sleep(ms){{ return new Promise(function(r){{ setTimeout(r, ms); }}); }}

function parseRGB(c){{
  var m = String(c).match(/[\\d.]+/g) || [0,0,0];
  return [parseFloat(m[0]), parseFloat(m[1]), parseFloat(m[2])];
}}
function lum(c){{
  function f(v){{ v = v/255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); }}
  return 0.2126*f(c[0]) + 0.7152*f(c[1]) + 0.0722*f(c[2]);
}}
function contrast(a, b){{
  var la = lum(a), lb = lum(b);
  var hi = Math.max(la,lb), lo = Math.min(la,lb);
  return (hi + 0.05) / (lo + 0.05);
}}
function escKey(){{
  return new KeyboardEvent('keydown', {{key:'Escape', bubbles:true, cancelable:true}});
}}

function run(){{
  var theme = document.documentElement.getAttribute('data-theme') || 'light';

  // ---------- ① Toast 必须盖在阅读器之上 ----------
  var toast = document.getElementById('toast');
  var tr = toast.getBoundingClientRect();
  var hit = document.elementFromPoint(tr.left + tr.width/2, tr.top + tr.height/2);
  var onToast = !!(hit && hit.closest && hit.closest('#toast'));
  check('阅读器打开时 Toast 真的在最上层（点得到）',
        onToast,
        '命中=' + (hit ? (hit.id || hit.className || hit.tagName) : 'null')
          + ' toastZ=' + getComputedStyle(toast).zIndex
          + ' readerZ=' + getComputedStyle(document.getElementById('reader-modal')).zIndex);

  // ---------- ② 深色卡片的对比度 ----------
  var card = document.querySelector('.studio-dark-card');
  var cs = getComputedStyle(card);
  var c = contrast(parseRGB(cs.backgroundColor), parseRGB(cs.color));
  check('主题 ' + theme + '：深色卡片文字对比度 ≥ 4.5:1',
        c >= 4.5,
        c.toFixed(2) + ':1（底 ' + cs.backgroundColor + ' / 字 ' + cs.color + '）');
  var lbl = card.querySelector('.field-label');
  if (lbl) {{
    var ls = getComputedStyle(lbl);
    var lc = contrast(parseRGB(cs.backgroundColor), parseRGB(ls.color));
    check('主题 ' + theme + '：深色卡片里的标签文字对比度 ≥ 4.5:1',
          lc >= 4.5, lc.toFixed(2) + ':1（字 ' + ls.color + '）');
  }}

  return (async function(){{
    // ---------- ③ Esc 栈：一次按键只关最上面那个 ----------
    var reader = document.getElementById('reader-modal');
    reader.style.display = 'flex';
    // 阅读器自己的 Esc 处理器（真代码）
    var _readerEscHandler = null;
    {reader_esc}
    document.addEventListener('keydown', _readerEscHandler);

    // 三个浮层依次打开（后开的在上面）
    var editor = document.getElementById('anno-editor');
    editor.style.display = 'flex';
    _escBind(editor, function(){{ editor.style.display = 'none'; __closed.annoEditor++; }});
    var drawer = document.getElementById('anno-drawer');
    drawer.style.display = 'flex';
    _escBind(drawer, function(){{ drawer.style.display = 'none'; __closed.annoDrawer++; }});
    var sync = document.getElementById('sync-modal');
    sync.style.display = 'flex';
    _escBind(sync, function(){{ sync.style.display = 'none'; __closed.sync++; }});

    document.dispatchEvent(escKey());
    await sleep(30);
    check('Esc：只关掉最上面那个浮层',
          __closed.sync === 1 && __closed.annoDrawer === 0 && __closed.annoEditor === 0,
          'sync=' + __closed.sync + ' drawer=' + __closed.annoDrawer
            + ' editor=' + __closed.annoEditor);
    check('Esc：阅读器**没有**被一起关掉',
          __closed.reader === 0 && reader.style.display !== 'none',
          'readerClosed=' + __closed.reader + ' display=' + reader.style.display);

    document.dispatchEvent(escKey());
    await sleep(30);
    check('Esc：第二下关掉下一个浮层，仍不动阅读器',
          __closed.annoDrawer === 1 && __closed.reader === 0,
          'drawer=' + __closed.annoDrawer + ' reader=' + __closed.reader);

    // 浮层都关掉之后，Esc 才轮到阅读器
    document.dispatchEvent(escKey());
    document.dispatchEvent(escKey());
    await sleep(30);
    check('Esc：浮层全关后，Esc 才关阅读器',
          __closed.reader === 1,
          'reader=' + __closed.reader);

    // ---------- ④ 缩放防抖 ----------
    __closed.zoom = 0;
    setReaderZoom(1); setReaderZoom(1); setReaderZoom(1);
    await sleep(500);
    check('连点三次缩放只重绘一次（防抖）',
          __closed.zoom === 1, '重绘次数=' + __closed.zoom);

    document.title = 'RESULT:' + JSON.stringify(results);
  }})();
}}

run().catch(function(e){{
  check('探针执行没抛异常', false, String(e && e.message || e));
  document.title = 'RESULT:' + JSON.stringify(results);
}});
</script></body></html>
"""


def build_probe(style: str, code: str, reader_esc: str, theme: str) -> str:
    return PROBE_HTML.format(style=style, code=code, reader_esc=reader_esc,
                              theme=theme)


def run_probe(browser: str, style: str, code: str, reader_esc: str,
              theme: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "probe.html"
        html.write_text(build_probe(style, code, reader_esc, theme),
                        encoding="utf-8")
        dom = dump_dom(html, browser=browser, window_size="1400,1000",
                       virtual_time_budget=15000)
    m = re.search(r"RESULT:(\[.*?\])</title>", dom, re.S)
    if not m:
        raise RuntimeError("没拿到探针结果（DOM 长度 %d）" % len(dom))
    raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
    return json.loads(raw)


def probe_from_html(browser: str, html: str, theme: str) -> list[dict]:
    """从一份 index.html 文本跑一遍探针。"""
    style = extract_style_block(html)
    code = "\n\n".join([
        # Esc 栈（真代码，含唯一的那个全局 keydown 监听器）
        extract_block(html, "const _escStack = [];", "function _wireModalEsc"),
        extract_function(html, "_wireModalEsc"),
        # 缩放防抖（真代码）
        extract_decl(html, "_pdfZoomReloadTimer"),
        extract_function(html, "setReaderZoom"),
    ])
    reader_esc = extract_reader_esc_handler(html)
    return run_probe(browser, style, code, reader_esc, theme)


# ---------------------------------------------------------------------------
# 变异自检：把 index.html 改坏后，同一套断言必须**出现失败**
# （没有这一段，上面那些断言可能全是恒真的、等于没写）
#
# ⚠ 目标串必须**在源码里唯一**（§4.119）：JS 的 String.replace 只替换第一处，
#   改错地方 → 护栏「红了」但是假阳性，真正的缺陷没测出来。
# ---------------------------------------------------------------------------
MUTATIONS = [
    ("Toast 层级又退回 1000（与阅读器同级 → 被整块盖住）",
     [("z-index: var(--z-toast);", "z-index: 1000;")], "light"),
    ("夜间深色卡片文字又跟着 --fg-inverse 走（深底深字）",
     [("--dark-text:       var(--fg-strong);   /* #F0EDE6 */",
       "--dark-text:       var(--fg-inverse);")], "night"),
    # ⚠ Esc 这条是**整体回退**：修复有「统一栈 + stopImmediatePropagation
    #   + 阅读器让路」三道防线，坏一道还撑得住 → 必须三道一起没才复现原 bug。
    ("Esc 整体回退到旧实现（一次按键关掉整个阅读器）",
     [("    e.stopImmediatePropagation();", "    /* 变异：不再拦截同行监听器 */"),
      ("if (_escOverlayOpen()) return;", "/* 变异：不再让路 */")], "light"),
    ("缩放又不防抖（连点会并发重绘 → 丢译文）",
     [("if (_pdfZoomReloadTimer) clearTimeout(_pdfZoomReloadTimer);",
       "/* 变异：不防抖 */")], "light"),
]


def main() -> int:
    browser = _find_browser()
    if not browser:
        print("❌ 找不到 headless 浏览器（护栏必须红，不许静默跳过）")
        return 1
    src = INDEX.read_text(encoding="utf-8")
    failures: list[str] = []
    for theme in ("light", "night"):
        results = probe_from_html(browser, src, theme)
        for r in results:
            mark = "✅" if r["pass"] else "❌"
            print(f"  {mark} [{theme}] {r['name']}"
                  + (f"  — {r['detail']}" if r.get("detail") else ""))
            if not r["pass"]:
                failures.append(f"[{theme}] {r['name']}: {r.get('detail','')}")

    # ---- 变异自检 ----
    print("\n—— 变异自检（改坏后必须变红）——")
    for label, subs, theme in MUTATIONS:
        mutated = src
        broken = False
        for old, new in subs:
            if old not in mutated:
                print(f"  ❌ 变异目标串在源码里找不到（护栏本身坏了）: {old!r}")
                failures.append(f"变异目标缺失: {old!r}")
                broken = True
                break
            mutated = mutated.replace(old, new, 1)
        if broken:
            continue
        try:
            res = probe_from_html(browser, mutated, theme)
        except Exception as e:  # 变异把页面搞崩也算「测出来了」
            print(f"  ✅ 变异被抓住（页面报错）: {label} — {e}")
            continue
        bad = [r for r in res if not r["pass"]]
        if bad:
            print(f"  ✅ 变异被抓住: {label}")
            for r in bad:
                print(f"       → 变红: {r['name']} ({r.get('detail','')})")
        else:
            print(f"  ❌ 变异**没**被抓住（护栏是恒真的）: {label}")
            failures.append(f"变异未被抓住: {label}")

    if failures:
        print("\n❌ 护栏失败：")
        for f in failures:
            print("   - " + f)
        return 1
    print("\n✅ 阅读器浮层护栏全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
