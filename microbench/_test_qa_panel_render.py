# -*- coding: utf-8 -*-
"""答疑浮层（划词答疑面板）：按钮可见性 + 拖动边界 + 按钮状态 —— **真实渲染**护栏。

用户 2026-11-19 报的现象（附截图）：

    「ai 解答有时候会出现这种情况，就非常麻烦，因为那个错掉的键就没了，
      虽然能随意缩放，但拖动有时候会出现问题，上面的 x 键也在某些比例下会消失」

逐条定位（取证见下）——**三个症状、四条独立根因**：

  ① 「错掉的键就没了」= **夜色主题下主按钮白字配奶油底**
     `.btn-pill { background: var(--fg-strong); color: var(--text-light) }`
     夜色主题里 `--fg-strong` 是奶油色 #F0EDE6，而 `--text-light` 恒为纯白
     → 对比度 **1.17:1** → 「发送提问」四个字彻底看不见（截图实证）。
     同族的 toast / 悬浮钮 / AI 活动条本来就是 `color: var(--surface-canvas)`，
     只有主按钮例外。

  ② 「错掉的键就没了」（另一种形态）= **按钮永久停在「⏳ 解答中…」**
     `askLLMStream` 每次都 `const originalHTML = btn.innerHTML` ——
     第二次请求捕获到的是**上一轮注入的流式标签** → 跑完恢复成它 → 按钮再也发不出下一问。
     另有：被别的入口顶掉的按钮，因为序号守卫而**没人恢复它**。

  ③ 「拖动有时候会出现问题」= **收起的竖条被拖一下就误展开**
     拖动结束后浏览器会补一个 click → 标题栏的 click 处理器把它当成「点竖条展开」。

  ④ 「x 键在某些比例下会消失」= **拖动/还原的横向钳制按固定 60/80px 算**
     面板本身有 460px 宽 → 面板大半个在屏幕外 → 右上角的「×」够不着；
     而且这个越界位置**会被存进 localStorage** → 下次打开面板还是缺一块。

⚠ 这份护栏**不读源码文本当断言**（`assert "xxx" in src` 那种）：
   元护栏 tests/test_guard_efficacy.py 把那一类钉死不许再涨 ——
   那种断言改坏行为也照样绿。
   → 这里把 index.html 里的**真 CSS** 与**真 JS**抽出来，喂进一个探针页面，
     用 headless 浏览器**真渲染**后量坐标、量对比色、跑真实事件。

运行：python microbench/_test_qa_panel_render.py
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
# 从 index.html 里抽真代码（括号配平要跳过字符串/模板串/注释/**正则**）
# ---------------------------------------------------------------------------
def _scan_js(src: str, start: int) -> int:
    """返回从 start 开始的函数体结束位置（**真代码**，不是抄一遍）。

    ⚠ **正则字面量必须单独处理**：正则里可能有引号，
       只按字符串扫描会把状态机带偏 → 抽出来的函数被截断。
    """
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


def extract_const(src: str, name: str) -> str:
    """抽一行 const/let（护栏要抽真代码，不能抄）。"""
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=[^;\n]*;",
                  src)
    if not m:
        raise KeyError(f"index.html 里找不到常量 {name}")
    return m.group(0).strip()


def extract_style_block(src: str) -> str:
    """抽出整个 <style> 块（主题变量 + .btn-pill + 面板样式都要）。"""
    m = re.search(r"<style[^>]*>(.*?)</style>", src, re.S)
    if not m:
        raise KeyError("index.html 里找不到 <style> 块")
    return m.group(1)


def extract_panel_markup(src: str) -> str:
    """抽出 `#reader-qa-panel` 那一整块 DOM（按标签深度配平）。"""
    start = src.find('<div id="reader-qa-panel"')
    if start < 0:
        raise KeyError("index.html 里找不到 #reader-qa-panel")
    depth = 0
    i = start
    tag_re = re.compile(r"<(/?)div\b", re.I)
    while True:
        m = tag_re.search(src, i)
        if not m:
            raise KeyError("#reader-qa-panel 的标签没配平")
        depth += -1 if m.group(1) else 1
        i = m.end()
        if depth == 0:
            end = src.index(">", i) + 1
            return src[start:end]


# ---------------------------------------------------------------------------
# 探针页面：真 CSS + 真 JS + 桩件
# ---------------------------------------------------------------------------
PROBE_HTML = """<!DOCTYPE html>
<html lang="zh-CN" data-theme="{theme}"><head><meta charset="utf-8"><title>probe</title>
<style>
{style}
html,body{{margin:0;padding:0;background:#111;}}
#probe-btns{{position:fixed;left:12px;top:12px;z-index:99999;display:flex;gap:8px;}}
</style></head>
<body>
<div id="probe-btns">
  <button id="qa-probe-btn" class="btn-pill">发送提问</button>
  <button id="qa-probe-btn2" class="btn-pill">章节问AI</button>
</div>
{panel}
<script>
// ---------------- 桩件：askLLMStream 的外部依赖 ----------------
var __toasts = [];
function showToast(msg){{ __toasts.push(msg); }}
function _aiTaskStart(){{ return 'probe-task'; }}
function _aiTaskEnd(){{}}
function _aiTaskStage(){{}}
function openReaderQAPanel(){{}}
function toggleReaderQAPanel(){{}}
function minimizeReaderQAPanel2(){{}}
function triggerQAPreset(){{}}
function clearQAQuotedText(){{}}
function submitReaderQA(){{}}
function onQAModelChange(){{}}
var _pdfTransModel = "";

// ---------------- fetch 桩：可控「挂住 / 立刻返回」----------------
// ⚠ 一定要**真的 abort 能 reject**：否则被顶掉的旧请求永远不 settle，
//    它的 finally 永远不跑 → 测的就不是「谁负责复位按钮」了。
window.fetch = function(url, opts){{
  var q = '';
  try {{ q = (JSON.parse(opts.body)||{{}}).question || ''; }} catch(e) {{}}
  if (q.indexOf('挂住') >= 0) {{
    return new Promise(function(resolve, reject){{
      if (opts.signal) {{
        opts.signal.addEventListener('abort', function(){{
          reject(new DOMException('aborted', 'AbortError'));
        }});
      }}
    }});
  }}
  var sse = 'data: {{"text": "答案"}}\\n\\ndata: [DONE]\\n\\n';
  var enc = new TextEncoder().encode(sse);
  var sent = false;
  return Promise.resolve({{
    ok: true, status: 200,
    body: {{ getReader: function(){{
      return {{ read: function(){{
        if (sent) return Promise.resolve({{done:true, value:undefined}});
        sent = true;
        return Promise.resolve({{done:false, value:enc}});
      }} }};
    }} }}
  }});
}};

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

function run(){{
  var theme = document.documentElement.getAttribute('data-theme') || 'light';
  var btn = document.getElementById('qa-probe-btn');

  // ---------- ① 主按钮在本主题下的对比度 ----------
  // ⚠ 主题必须在**页面加载时就定死**：运行时改 data-theme 后，Chrome
  //   不会重解析依赖该自定义属性的 background-color（实测读到的还是旧值）
  //   → 所以两个主题各跑一遍页面，而不是在同一页里来回切。
  var cs0 = getComputedStyle(btn);
  var bg0 = cs0.backgroundColor, fg0 = cs0.color;
  var c0 = contrast(parseRGB(bg0), parseRGB(fg0));
  check('主题 ' + theme + '：主按钮对比度 ≥ 4.5:1',
        c0 >= 4.5,
        c0.toFixed(2) + ':1（底 ' + bg0 + ' / 字 ' + fg0 + '）');

  // ---------- ② 拖动：面板不能被推出屏幕 ----------
  var panel = document.getElementById('reader-qa-panel');
  panel.style.display = 'flex';
  try {{ localStorage.removeItem('microbench.qaPanel.geometry'); }} catch(e) {{}}
  initReaderQAPanelDrag();

  return (async function(){{
    // 等入场动画（slideUpPanel 320ms，带 scale）结束 —— 否则量到的是
    // 缩放后的尺寸，拖动/落盘的口径会偏。
    await sleep(600);

    var head = panel.querySelector('.qa-panel-head');
    var hr = head.getBoundingClientRect();
    head.dispatchEvent(new MouseEvent('mousedown',
      {{bubbles:true, cancelable:true, button:0, clientX: hr.left+40, clientY: hr.top+8}}));
    document.dispatchEvent(new MouseEvent('mousemove',
      {{bubbles:true, clientX: 99999, clientY: hr.top+8}}));
    document.dispatchEvent(new MouseEvent('mouseup', {{bubbles:true}}));
    var pr = panel.getBoundingClientRect();
    var zoomBtns = head.querySelectorAll('button.btn-zoom-toggle');
    var closeBtn = zoomBtns[zoomBtns.length-1];
    var cr = closeBtn.getBoundingClientRect();
    check('拖到最右边后「×」关闭键仍在屏内',
          cr.right <= window.innerWidth + 1 && cr.right <= pr.right + 1,
          '×右缘=' + cr.right.toFixed(0) + ' 面板右缘=' + pr.right.toFixed(0)
            + ' 屏宽=' + window.innerWidth);
    var geo = null;
    var geoRaw = '(读不到)';
    try {{ geoRaw = String(localStorage.getItem('microbench.qaPanel.geometry')); }} catch(e) {{ geoRaw = 'ERR:' + e.name; }}
    try {{ geo = JSON.parse(geoRaw); }} catch(e) {{ geo = null; }}
    check('落盘的几何在屏内（不会被下次打开时还原成越界位置）',
          !!geo && geo.left + panel.offsetWidth <= window.innerWidth + 1,
          geoRaw + ' 面板宽=' + panel.offsetWidth);

    // ---------- ③ 收起成竖条：拖动不能误展开 ----------
    minimizeReaderQAPanel();
    var wasCollapsed = panel.classList.contains('qa-collapsed');
    var strip = head.getBoundingClientRect();
    head.dispatchEvent(new MouseEvent('mousedown',
      {{bubbles:true, cancelable:true, button:0, clientX: strip.left+strip.width/2, clientY: strip.top+20}}));
    document.dispatchEvent(new MouseEvent('mousemove',
      {{bubbles:true, clientX: strip.left+strip.width/2+120, clientY: strip.top+20}}));
    document.dispatchEvent(new MouseEvent('mouseup', {{bubbles:true}}));
    // 浏览器在 mouseup 之后会补一个 click —— 这里手动补上
    head.dispatchEvent(new MouseEvent('click', {{bubbles:true}}));
    check('收起的竖条被拖动后仍是收起态',
          wasCollapsed && panel.classList.contains('qa-collapsed'),
          '拖前=' + wasCollapsed + ' 拖后=' + panel.classList.contains('qa-collapsed'));

    // 单击（不拖）仍然要能展开
    head.dispatchEvent(new MouseEvent('click', {{bubbles:true}}));
    check('单击竖条仍能展开',
          !panel.classList.contains('qa-collapsed'),
          'collapsed=' + panel.classList.contains('qa-collapsed'));

    // ---------- ④ 按钮状态：连问两次 / 被别的入口顶掉 / 取消键可点 ----------
    var b = document.getElementById('qa-probe-btn');
    var a = document.getElementById('qa-probe-btn2');
    function reset(el, label){{
      el.innerHTML = label; el.disabled = false;
      try {{ el.removeAttribute('data-ask-original-html'); }} catch(e) {{}}
    }}

    reset(b, '发送提问');
    window.askLLMStream(b, '/probe', {{question:'第一问挂住', paper_title:'t'}}, {{}});
    await sleep(60);
    check('提问进行中：按钮显示「解答中」并带取消键',
          (b.textContent||'').indexOf('解答中') >= 0 && b.disabled,
          JSON.stringify((b.textContent||'').trim()));

    // 「✕ 取消」必须真的能点到（disabled 的父按钮会带 pointer-events:none）
    var cancel = b.querySelector('[data-cancel-ask]');
    var hit = null;
    if (cancel) {{
      var rc = cancel.getBoundingClientRect();
      var top = document.elementFromPoint(rc.left + rc.width/2, rc.top + rc.height/2);
      hit = top && top.closest ? !!top.closest('[data-cancel-ask]') : false;
    }}
    check('「✕ 取消」键真的能点到',
          !!cancel && hit,
          '存在=' + !!cancel + ' 命中=' + hit);

    // 第二问顶掉第一问，并立刻成功返回
    await window.askLLMStream(b, '/probe', {{question:'第二问正常', paper_title:'t'}}, {{}});
    check('同一按钮连问两次后按钮回到「发送提问」',
          (b.textContent||'').trim() === '发送提问' && !b.disabled,
          JSON.stringify((b.textContent||'').trim()) + ' disabled=' + b.disabled);

    // 别的入口把这一问顶掉 → 被顶掉的那个按钮也必须复位
    reset(b, '发送提问'); reset(a, '章节问AI');
    window.askLLMStream(a, '/probe', {{question:'章节那问挂住', paper_title:'t'}}, {{}});
    await sleep(60);
    await window.askLLMStream(b, '/probe', {{question:'答疑那问正常', paper_title:'t'}}, {{}});
    check('被顶掉的另一个按钮也复位了',
          !a.disabled && (a.textContent||'').indexOf('解答中') < 0,
          JSON.stringify((a.textContent||'').trim()) + ' disabled=' + a.disabled);

    document.title = 'RESULT:' + JSON.stringify(results);
  }})();
}}

run().catch(function(e){{
  check('探针执行没抛异常', false, String(e && e.message || e));
  document.title = 'RESULT:' + JSON.stringify(results);
}});
</script></body></html>
"""


def build_probe(style: str, panel: str, code: str, theme: str) -> str:
    return PROBE_HTML.format(style=style, panel=panel, code=code, theme=theme)


def run_probe(browser: str, style: str, panel: str, code: str, theme: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "probe.html"
        html.write_text(build_probe(style, panel, code, theme), encoding="utf-8")
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
    panel = extract_panel_markup(html)
    code = "\n\n".join([
        extract_const(html, "_qaGeo"),
        extract_const(html, "_activeAsk"),
        extract_const(html, "_qaRequestSeq"),
        extract_function(html, "cancelActiveAsk"),
        extract_function(html, "_qaAskOriginalHTML"),
        extract_function(html, "_qaResetAskButton"),
        extract_function(html, "askLLMStream"),
        extract_function(html, "_qaClampPanelX"),
        extract_function(html, "_qaClampPanelY"),
        extract_function(html, "_qaPanelStorageKey"),
        extract_function(html, "saveReaderQAPanelGeometry"),
        extract_function(html, "restoreReaderQAPanelGeometry"),
        extract_function(html, "initReaderQAPanelDrag"),
        extract_function(html, "minimizeReaderQAPanel"),
        extract_function(html, "_qaCollapsedHeadClick"),
    ])
    return run_probe(browser, style, panel, code, theme)


# ---------------------------------------------------------------------------
# 变异自检：把 index.html 改坏后，同一套断言必须**出现失败**
# （没有这一段，上面那些断言可能全是恒真的、等于没写）
#
# ⚠ 目标串必须**在源码里唯一**（§4.119）：JS 的 String.replace 只替换第一处，
#   改错地方 → 护栏「红了」但是假阳性，真正的缺陷没测出来。
# ---------------------------------------------------------------------------
MUTATIONS = [
    ("夜色主题主按钮又变回白字（--text-light）",
     [("color: var(--surface-canvas); /* 主按钮文字色必须跟着主题走 */",
       "color: var(--text-light);")], "night"),
    ("横向钳制又退回固定 60px（不看面板实际宽度）",
     [("const maxX = Math.max(0, vw - w);", "const maxX = Math.max(0, vw - 60);")], "light"),
    ("落盘几何不再钳到屏幕内",
     [("const x = _qaClampPanelX(panel, r.left);", "const x = Math.round(r.left);")], "light"),
    ("被顶掉的旧按钮不复位（它会永久卡在「解答中…」）",
     [("_qaResetAskButton(old);", "/* 变异：顶掉旧请求时不再复位 */")], "light"),
    ("拖动后不再抑制那次 click（竖条会被误展开）",
     [("_qaGeo.suppressClick = !!moved;", "_qaGeo.suppressClick = false;")], "light"),
    ("「✕ 取消」键不再自己打开 pointer-events",
     [("cursor:pointer; pointer-events:auto;", "cursor:pointer;")], "light"),
    # ⚠ 下面这条是**整体回退到旧实现**（三个点同时改回）——
    #   单点改不动：修复有「原始标签存 dataset / 捕获时优先读 dataset /
    #   顶掉旧请求时复位旧按钮」三层保险，坏一层还撑得住。
    #   → 真正的回归是「三层一起没」→ 必须这样一起变异才复现原 bug。
    ("按钮状态整体回退到旧实现（连问两次就永久卡在「解答中…」）",
     [("const originalHTML = _qaAskOriginalHTML(btn);", "const originalHTML = btn.innerHTML;"),
      ("_qaResetAskButton(old);", "/* 变异：不复位 */"),
      ("const orig = btn.dataset.askOriginalHTML || rec.originalHTML || btn.innerHTML;",
       "const orig = rec.originalHTML || btn.innerHTML;")], "light"),
]


def run_mutation_selfcheck(browser: str, html: str) -> list[tuple[str, bool, str]]:
    out = []
    for name, edits, theme in MUTATIONS:
        bad = [old for old, _ in edits if html.count(old) != 1]
        if bad:
            # ⚠ 目标串不唯一 → 变异会改错地方 → 这次自检**无效**，必须报出来
            out.append((name, False, "目标串在源码里出现 %d 次（必须唯一）：%s"
                        % (html.count(bad[0]), bad[0][:60])))
            continue
        mutated = html
        for old, new in edits:
            mutated = mutated.replace(old, new)
        try:
            results = probe_from_html(browser, mutated, theme)
        except Exception as e:
            out.append((name, True, "探针直接报错（也算抓住了）：%s" % e))
            continue
        failed = [r["name"] for r in results if not r["pass"]]
        out.append((name, bool(failed),
                    ("被抓住：" + "；".join(failed[:2])) if failed else "❌ 没被抓住"))
    return out


def main() -> int:
    browser = _find_browser()
    if not browser:
        print("SKIP: 找不到 Edge/Chrome，跳过渲染护栏")
        return 0

    html = INDEX.read_text(encoding="utf-8")
    try:
        results: list[dict] = []
        for theme in ("light", "night"):
            results += probe_from_html(browser, html, theme)
    except (KeyError, ValueError) as e:
        print("FAIL: 抽取真代码失败：%s" % e)
        return 1
    except Exception as e:
        print("FAIL: 渲染探针失败：%s" % e)
        return 1

    npass = sum(1 for r in results if r["pass"])
    nfail = len(results) - npass
    print("=== 答疑浮层 · 真实渲染护栏（%s）===" % Path(browser).name)
    for r in results:
        print("  %s  %s%s" % ("PASS" if r["pass"] else "FAIL", r["name"],
                              ("  [" + r["detail"] + "]") if r["detail"] else ""))
    print("\nPASS %d  FAIL %d" % (npass, nfail))

    # ---- 变异自检 ----
    print("\n=== 变异自检（断言必须能抓到回归）===")
    muts = run_mutation_selfcheck(browser, html)
    m_ok = 0
    for name, ok, detail in muts:
        print("  %s  变异 [%s] %s" % ("PASS" if ok else "FAIL", name, detail))
        if ok:
            m_ok += 1
    print("  变异捕获 %d/%d" % (m_ok, len(muts)))
    if m_ok != len(muts):
        nfail += 1

    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
