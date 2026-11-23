# -*- coding: utf-8 -*-
"""「重新回答」（⬆️ 多看点上下文 / ⬇️ 少看上下文 / ✏️ 带反馈重答）的
**请求载荷** —— **真实渲染**行为护栏。

2026-11-22 独立代码审查实测复现的两个缺陷（都在「重新回答」这条路径上）：

  ① 「重新回答」的 payload **一个图相关的字段都没带**
     （没有 pdf_relative_path / page_num / formula_rects）
     → 服务端解析不出 PDF → **一张图都拿不到** → 连「附件说明」都不生成
     → 而上下文里的【公式N】是**裸占位** → 模型只见占位符 → **瞎编公式**
     （AGENTS §4.6：错公式比没公式危险得多；用户原话「ai 看上下文的时候
       完全看不到公式推导，只能猜」）。

  ② 「重新回答」用的是**实时选区**而不是**提问当时的快照**
     `_readerSelState` 会被**任何一次带选区的 mouseup** 改写
     → 用户问完又划了别的段落，再点「⬆️ 多看点上下文」
     → 模型拿到「旧问题 + 新选区」→ 答偏。

⚠ 这份护栏**不读源码文本当断言**（`assert "pdf_relative_path" in src` 那种）：
   把 payload 里 pdf_relative_path 删掉、把 page_num 置 null、
   把 selText 换成实时选区 —— 源码里该有的字符串还在 → 旧式源码文本护栏全绿。
   → 这里把 index.html 里的**真 JS** 抽出来，喂进探针页面，
     用 headless 浏览器**真跑**一遍真实用户路径：

       输入问题 → 点发送 → 拿到第一次的 payload
                → 改掉实时选区（模拟用户又划了别的段落）
                → **真点**「⬆️ 多看点上下文」按钮 → 拿到第二次的 payload

     然后断言**实际发出去的字节**。

⚠ 自带「变异自检」：把 index.html 改坏后重跑同一套断言，必须**出现失败**。

运行：python microbench/_test_qa_refine_payload.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

try:  # 作为脚本跑 / 作为包被导入，两种都要能用
    from _headless_browser import dump_dom, find_browser as _find_browser
    from _test_qa_panel_render import _scan_js
except ImportError:  # pragma: no cover
    from microbench._headless_browser import dump_dom, find_browser as _find_browser
    from microbench._test_qa_panel_render import _scan_js

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "microbench" / "static" / "index.html"


# ---------------------------------------------------------------------------
# 从 index.html 里抽真代码
# ---------------------------------------------------------------------------
def _scan_brackets(src: str, start: int, open_ch: str, close_ch: str) -> int:
    """从 start 处的 open_ch 开始，返回配平后的结束下标（不含）。

    与 _scan_js 同样的坑：字符串 / 模板串 / 注释里的括号不算。
    """
    i = src.index(open_ch, start)
    depth = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if ch in ("'", '"', "`"):
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
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("括号没配平")


def extract_function(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise KeyError("index.html 里找不到函数 %s" % name)
    end = _scan_js(src, m.start())
    return src[m.start():end]


def extract_const(src: str, name: str) -> str:
    """抽一行 const/let（护栏要抽真代码，不能抄）。"""
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=[^;\n]*;",
                  src)
    if not m:
        raise KeyError("index.html 里找不到常量 %s" % name)
    return m.group(0).strip()


def extract_array_decl(src: str, name: str) -> str:
    """抽一个**多行**数组声明（_SCOPE_LADDER 就是这种）。"""
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=", src)
    if not m:
        raise KeyError("index.html 里找不到声明 %s" % name)
    end = _scan_brackets(src, m.end() - 1, "[", "]")
    if src[end:end + 1] == ";":
        end += 1
    return src[m.start():end]


# ---------------------------------------------------------------------------
# 探针页面：真 JS + 桩件
# （用 __REAL_CODE__ 占位再 replace，避免 Python 花括号转义地狱）
# ---------------------------------------------------------------------------
PROBE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>probe</title></head>
<body>
<input id="qa-question-input" value="这个公式的推导过程是什么？">
<div id="qa-welcome-placeholder">欢迎</div>
<div id="qa-answer-body" style="display:none"></div>
<button id="qa-submit-btn">发送提问</button>
<script>
// ---------------- 桩件：只桩「外部依赖」，被测代码一律是真的 ----------------
var __calls = [];
function askLLMStream(btn, url, payload, opts) {
  __calls.push({url: url, payload: payload});
  if (opts && opts.onSuccess) opts.onSuccess('模型答案', {model: 'probe-model'});
  return Promise.resolve();
}
var _readerSelState = { text: 'SELECTED-TEXT', pageNum: 7, source: 'translation', rects: [] };
var _pdfState = { relativePath: '01_Literature/PAPER.pdf' };
var _readerState = { title: '探针论文' };
var _qaHeadingHit = null;
var _qaSectionBySelection = null;
var _qaModel = '';
var _currentQAPreset = 'explain';
var _lastAnswerText = '';
var _lastQAItem = null;
var _sessionQAList = [];
function _ensureQaOutlineReady() { return Promise.resolve(); }
function _matchQaHeading(t) { return null; }
function _findQaSectionBySelection(t, p, r) { return Promise.resolve(null); }
function _routeQuestionScope(q, preset, sel) {
  return Promise.resolve({mode: 'mid', budget: 8000,
    label: '章节上下文 (~8K 字)', source: 'ai', reason: 'probe'});
}
function _buildScopeContextBundle(scope) {
  return {text: 'CTX-' + scope.mode, parts: [{label: 'probe', text: 'CTX'}]};
}
function _renderQaContextBox() {}
function _captureSelectionPageImage() { return 'PAGE_IMAGE_B64'; }
function _collectPageFormulaRects(p) { return p ? [[p, 10, 20, 110, 40]] : []; }
function _sectionPageList() { return []; }
function _sectionFormulaRects() { return []; }
function escapeHtml(s) { return String(s); }
function escapeJsAttr(s) { return String(s); }
function formatMarkdownLite(s) { return String(s); }
function _aiFormatMs(ms) { return String(ms); }
function showToast(m) {}
function _persistLocalState() {}
function fetchModelStatus() {}

// ---------------- 被测真代码 ----------------
/*__REAL_CODE__*/

// ---------------- 探针 ----------------
var results = [];
function check(name, ok, detail) { results.push({name: name, pass: !!ok, detail: detail || ''}); }
function sleep(ms) { return new Promise(function(r) { setTimeout(r, ms); }); }
function isArr(v) { return Object.prototype.toString.call(v) === '[object Array]'; }

function run() {
  var answerBody = document.getElementById('qa-answer-body');
  return (async function () {
    // ---------- ① 第一次提问 ----------
    await submitReaderQA();
    var p1 = (__calls[0] || {}).payload;
    check('第一次提问带上 pdf_relative_path（服务端据此按 PDF 原始坐标重渲染整页图）',
      !!p1 && p1.pdf_relative_path === '01_Literature/PAPER.pdf',
      p1 ? String(p1.pdf_relative_path) : '(没有 payload)');
    check('第一次提问带上 page_num',
      !!p1 && p1.page_num === 7, p1 ? String(p1.page_num) : '-');
    check('第一次提问带上 formula_rects（服务端按坐标裁高清公式原图）',
      !!p1 && isArr(p1.formula_rects) && p1.formula_rects.length > 0,
      p1 ? JSON.stringify(p1.formula_rects) : '-');

    // ---------- ② 「提问当时」的快照被存下来 ----------
    check('提问后把「提问当时」的 PDF 路径存进 answerBody',
      answerBody.dataset.pdfPath === '01_Literature/PAPER.pdf',
      String(answerBody.dataset.pdfPath));
    check('提问后把「提问当时」的选区存进 answerBody',
      answerBody.dataset.selText === 'SELECTED-TEXT',
      String(answerBody.dataset.selText));
    check('提问后把「提问当时」的页码存进 answerBody',
      answerBody.dataset.pageNum === '7',
      String(answerBody.dataset.pageNum));

    // ---------- ③ 用户问完又划了别的段落（实时选区被改写）----------
    _readerSelState.text = 'LATER-SELECTION';
    _readerSelState.pageNum = 99;
    _readerSelState.source = 'pdf';

    // ---------- ④ 真点「⬆️ 多看点上下文」----------
    var up = answerBody.querySelector('[data-refine="up"]');
    check('「⬆️ 多看点上下文」按钮渲染出来了', !!up, '');
    if (up) { up.click(); await sleep(300); }
    var p2 = (__calls[1] || {}).payload;
    check('重新回答仍然带上 pdf_relative_path（否则服务端一张图都拿不到 → 模型瞎编公式）',
      !!p2 && p2.pdf_relative_path === '01_Literature/PAPER.pdf',
      p2 ? String(p2.pdf_relative_path) : '(没有第二次 payload)');
    check('重新回答仍然带上 page_num',
      !!p2 && p2.page_num === 7, p2 ? String(p2.page_num) : '-');
    check('重新回答仍然带上 formula_rects',
      !!p2 && isArr(p2.formula_rects) && p2.formula_rects.length > 0,
      p2 ? JSON.stringify(p2.formula_rects) : '-');
    check('重新回答用的是**提问当时**的选区，不是后来划的那段',
      !!p2 && p2.selected_text === 'SELECTED-TEXT',
      p2 ? String(p2.selected_text) : '-');
    check('重新回答用的是**提问当时**的 source_type',
      !!p2 && p2.source_type === 'translation',
      p2 ? String(p2.source_type) : '-');
    check('用户已经划到别的页 → 不发浏览器截图（避免图文不符）',
      !!p2 && p2.page_image === null,
      p2 ? String(p2.page_image) : '-');
    check('重新回答带上上一轮的答案（模型才知道要改什么）',
      !!p2 && p2.prior_answer === '模型答案',
      p2 ? String(p2.prior_answer) : '-');

    document.title = 'RESULT:' + JSON.stringify(results);
  })();
}

run().catch(function (e) {
  check('探针执行没抛异常', false, String(e && e.message || e));
  document.title = 'RESULT:' + JSON.stringify(results);
});
</script></body></html>
"""


def probe_from_html(browser: str, html: str) -> list[dict]:
    """从一份 index.html 文本跑一遍探针（返回 [{name,pass,detail}]）。"""
    code = "\n\n".join([
        extract_const(html, "_QA_SECTION_BUDGET"),
        extract_const(html, "_MAX_SCOPE_BUDGET"),
        extract_array_decl(html, "_SCOPE_LADDER"),
        extract_function(html, "_refineQA"),
        extract_function(html, "submitReaderQA"),
    ])
    page = PROBE_HTML.replace("/*__REAL_CODE__*/", code)
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "probe.html"
        f.write_text(page, encoding="utf-8")
        dom = dump_dom(f, browser=browser, window_size="1400,1000",
                       virtual_time_budget=20000)
    m = re.search(r"RESULT:(\[.*?\])</title>", dom, re.S)
    if not m:
        raise RuntimeError("没拿到探针结果（DOM 长度 %d）" % len(dom))
    raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# 变异自检：把 index.html 改坏后，同一套断言必须**出现失败**
# （没有这一段，上面那些断言可能全是恒真的、等于没写）
#
# ⚠ 目标串必须**在源码里唯一**（§4.119）：JS 的 String.replace 只替换第一处，
#   改错地方 → 护栏「红了」但是假阳性，真正的缺陷没测出来。
# ---------------------------------------------------------------------------
MUTATIONS = [
    ("「重新回答」的 payload 不带 pdf_relative_path（= 服务端一张图都拿不到）",
     [("        pdf_relative_path: snapPdfPath || null,\n", "")]),
    ("「重新回答」的 payload 不带 page_num",
     [("        page_num: snapPageNum || null,\n", "        page_num: null,\n")]),
    ("「重新回答」的 payload 不带 formula_rects",
     [("        formula_rects: _collectPageFormulaRects(snapPageNum),\n", "")]),
    ("「重新回答」改回用实时选区（丢掉提问当时的快照）",
     [("        selected_text: (snapSelText || ", "        selected_text: (_readerSelState.text || ")]),
    ("「重新回答」改回用实时 source_type",
     [("        source_type: snapSourceType,\n",
       "        source_type: _readerSelState.source || \"general\",\n")]),
    ("提问当时根本没存 PDF 路径快照",
     [("          answerBody.dataset.pdfPath = (_pdfState && _pdfState.relativePath) || \"\";\n", "")]),
    ("提问当时根本没存选区快照",
     [("          answerBody.dataset.selText = _readerSelState.text || \"\";\n", "")]),
]


def run_mutation_selfcheck(browser: str, html: str) -> list[tuple[str, bool, str]]:
    out = []
    for name, edits in MUTATIONS:
        bad = [old for old, _ in edits if html.count(old) != 1]
        if bad:
            out.append((name, False, "目标串在源码里出现 %d 次（必须唯一）：%s"
                        % (html.count(bad[0]), bad[0][:60])))
            continue
        mutated = html
        for old, new in edits:
            mutated = mutated.replace(old, new)
        try:
            results = probe_from_html(browser, mutated)
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
        print("FAIL: 找不到 Edge/Chrome/Chromium，渲染护栏不能静默跳过（AGENTS §4.16）")
        return 1

    html = INDEX.read_text(encoding="utf-8")
    try:
        results = probe_from_html(browser, html)
    except (KeyError, ValueError) as e:
        print("FAIL: 抽取真代码失败：%s" % e)
        return 1
    except Exception as e:
        print("FAIL: 渲染探针失败：%s" % e)
        return 1

    npass = sum(1 for r in results if r["pass"])
    nfail = len(results) - npass
    print("=== 「重新回答」请求载荷 · 真实渲染护栏（%s）===" % Path(browser).name)
    for r in results:
        print("  %s  %s%s" % ("PASS" if r["pass"] else "FAIL", r["name"],
                              ("  [" + r["detail"] + "]") if r["detail"] else ""))
    print("\nPASS %d  FAIL %d" % (npass, nfail))

    # ---- 变异自检 ----
    print("\n=== 变异自检（断言必须能抓到回归）===")
    m_ok = 0
    for name, ok, detail in run_mutation_selfcheck(browser, html):
        print("  %s  变异 [%s] %s" % ("PASS" if ok else "FAIL", name, detail))
        if ok:
            m_ok += 1
    print("  变异捕获 %d/%d" % (m_ok, len(MUTATIONS)))
    if m_ok != len(MUTATIONS):
        nfail += 1

    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
