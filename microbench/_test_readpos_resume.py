# -*- coding: utf-8 -*-
"""阅读进度续读（「像看小说一样」）—— **真实渲染**行为护栏。

用户 2026-11-20 的原话：

    「我这论文都很长嘛，我不想每次打开都要重新翻好久翻到我目前看的部分，
      需要你能做到像看小说一样，这次看到这里了，关掉应用，下次打开也还是这里」

这条护栏守的是**行为**，不是代码形态：

  ① 采集：在左栏（PDF 原文）滚到第 3 页中段 → 记下「第 3 页 + 页内位置」
  ② 落盘：进度写进 localStorage（**不碰坚果云共享目录**）
  ③ 恢复：重新打开同一篇 → 两栏都回到同一处，**页内误差 ≤ 2px**
     （只回到页码不算数 —— 长论文里一页可能有半屏字，
       差 200px 就会让人觉得「没跳对」，那就不是「像看小说」了）
  ④ 缩放变了：按「页内比例」换算，不是死记 px
  ⑤ 跨设备那条路（手动「推送 · 阅读进度」）的采集被修好了：
     原来只认右栏译文栏，用户看左栏原文时推出去的 page 恒为 0

⚠ 这份护栏**不读源码文本当断言**（`assert "xxx" in src` 那种）：
   元护栏 tests/test_guard_efficacy.py 把那一类钉死不许再涨 ——
   那种断言改坏行为也照样绿。
   → 这里把 index.html 里的**真 JS** 抽出来，喂进一个探针页面，
     用 headless 浏览器**真渲染**后真滚动、真量坐标。

运行：python microbench/_test_readpos_resume.py
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
def extract_function(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise KeyError(f"index.html 里找不到函数 {name}")
    end = _scan_js(src, m.start())
    return src[m.start():end]


def extract_decl(src: str, name: str) -> str:
    """抽一条 const/let/var 声明（支持多行对象字面量）。"""
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=", src)
    if not m:
        raise KeyError(f"index.html 里找不到声明 {name}")
    i = src.index("=", m.start()) + 1
    while i < len(src) and src[i].isspace():
        i += 1
    if i < len(src) and src[i] == "{":
        end = _scan_js(src, i)
        if end < len(src) and src[end] == ";":
            end += 1
    else:
        end = src.index(";", i) + 1
    return src[m.start():end]


# 被测代码：函数 + 状态声明（状态声明必须一起切进来，否则整套 ReferenceError —— §4.109）
FUNCTIONS = [
    "_readPosStore",
    "_writeReadPosStore",
    "_readPosKeyForPath",
    "_readPosKey",
    "_getReadPos",
    "_readPosAnchor",
    "_captureReadPos",
    "_scheduleSaveReadPos",
    "_restoreReadPosAnchor",
    "_applyReadPos",
    "_readPosReapply",
    "_initReadPosTracking",
    "_restoreSectionReadPos",
    "_readingProgress",
    "_applyReadingProgress",
]
DECLS = [
    "LS_READPOS_KEY",
    "_READPOS_MAX_PAPERS",
    "_READPOS_PANES",
    "_readPosSaveTimer",
    "_readPosTrackingBound",
    "_readPosRestored",
    "_readPosCurrent",
    "_readPosUserMoved",
    "_readPosExpected",
]


def slice_code(html: str) -> str:
    parts = [extract_decl(html, n) for n in DECLS]
    parts += [extract_function(html, n) for n in FUNCTIONS]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 探针页面
# ---------------------------------------------------------------------------
PROBE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>probe</title>
<style>
html,body{{margin:0;padding:0;}}
.wrap{{display:flex;align-items:flex-start;}}
.pane{{flex:1 1 50%;overflow-y:auto;height:400px;padding:24px 32px 48px;background:#f2f2f2;}}
#pdf-translations-container{{border-left:1px solid #bbb;}}
.pdf-page-block{{height:300px;margin:0 0 20px 0;background:#dde;}}
.pdf-translation-block{{height:200px;margin:0 0 20px 0;background:#eed;}}
</style></head>
<body>
<div class="wrap">
  <div class="pane" id="pdf-viewer-container"><div id="pdf-pages-list"></div></div>
  <div class="pane" id="pdf-translations-container"><div id="pdf-translations-list"></div></div>
</div>
<script>
(function () {{
// ---------------- localStorage：能用真就用真，不能用就上内存桩 ----------------
var __lsReal = false;
var __mem = {{}};
try {{
  window.localStorage.setItem('__probe__', '1');
  window.localStorage.removeItem('__probe__');
  __lsReal = true;
  var localStorage = window.localStorage;
}} catch (e) {{
  var localStorage = {{
    getItem: function (k) {{ return Object.prototype.hasOwnProperty.call(__mem, k) ? __mem[k] : null; }},
    setItem: function (k, v) {{ __mem[k] = String(v); }},
    removeItem: function (k) {{ delete __mem[k]; }},
    clear: function () {{ __mem = {{}}; }},
    key: function (i) {{ return Object.keys(__mem)[i] || null; }},
    get length() {{ return Object.keys(__mem).length; }}
  }};
}}

// ---------------- 桩件：被测代码用到的外部依赖 ----------------
var __toasts = [];
function showToast(msg) {{ __toasts.push(msg); }}
function updateZoomLabel() {{}}
function renderReader() {{}}

var _pdfZoom = 1.0;
var _readerMode = "pdf";
var _pdfState = {{
  paperId: "probe_paper_1",
  relativePath: "01_Literature/probe.pdf",
  paperTitle: "探针论文",
}};
var _readerState = {{
  pdfPath: "01_Literature/probe.pdf",
  title: "探针论文",
  sections: [],
  translations: {{}},
  activeSection: 0,
}};

// ---------------- 真代码 ----------------
{code}

// ---------------- 造页面 ----------------
function buildBlocks(listId, cls, idPrefix, n, h) {{
  var list = document.getElementById(listId);
  for (var i = 1; i <= n; i++) {{
    var d = document.createElement('div');
    d.className = cls;
    d.id = idPrefix + i;
    d.style.height = h + 'px';
    d.textContent = 'PAGE ' + i;
    list.appendChild(d);
  }}
}}
buildBlocks('pdf-pages-list', 'pdf-page-block', 'pdf-page-block-', 10, 300);
buildBlocks('pdf-translations-list', 'pdf-translation-block', 'pdf-trans-block-', 10, 200);

var results = [];
function check(name, ok, detail) {{ results.push({{name: name, pass: !!ok, detail: detail || ''}}); }}
function sleep(ms) {{ return new Promise(function (r) {{ setTimeout(r, ms); }}); }}
function J(v) {{ return JSON.stringify(v); }}

var leftPane = document.getElementById('pdf-viewer-container');
var rightPane = document.getElementById('pdf-translations-container');
var leftList = document.getElementById('pdf-pages-list');
var rightList = document.getElementById('pdf-translations-list');

/** 页顶相对滚动容器顶的偏移（负数 = 已滚进页里） */
function deltaOf(pane, blockId) {{
  var el = document.getElementById(blockId);
  return pane.getBoundingClientRect().top - el.getBoundingClientRect().top;
}}

function run() {{
  return (async function () {{
    check('localStorage 可用（真存储优先）', true, __lsReal ? '用真 localStorage' : '用内存桩');

    // ================= ① 采集 =================
    // 把左栏滚到「第 3 页顶之上 80px」= 已经滚进第 3 页 80px
    var b3 = document.getElementById('pdf-page-block-3');
    var d3 = b3.getBoundingClientRect().top - leftPane.getBoundingClientRect().top;
    leftPane.scrollTop += d3 + 80;
    // 右栏单独滚到第 2 页 —— 两栏的位置必须**分别**记、分别回
    rightPane.scrollTop = 300;
    var rec = _captureReadPos();
    check('采集到的是第 3 页', !!rec && !!rec.left && rec.left.page === 3,
          J(rec && rec.left));
    check('页内位置被记下来了（delta=-80，不是 0）',
          !!rec && !!rec.left && rec.left.delta === -80, J(rec && rec.left));
    check('页内比例被记下来了（0<ratio<1）',
          !!rec && !!rec.left && rec.left.ratio > 0.2 && rec.left.ratio < 0.35,
          J(rec && rec.left && rec.left.ratio));
    check('右栏**单独**记了自己的位置（第 2 页）',
          !!rec && !!rec.right && rec.right.page === 2, J(rec && rec.right));

    // ================= ② 落盘 =================
    var key = _readPosKey();
    var store = _readPosStore();
    check('进度落进了 localStorage（按论文分键）', !!store[key], key + ' → ' + J(!!store[key]));
    check('localStorage 里**只有**这一篇的键', Object.keys(store).length === 1,
          J(Object.keys(store)));

    // ================= ③ 恢复 =================
    leftPane.scrollTop = 0;
    rightPane.scrollTop = 0;
    var res = _applyReadPos(rec);
    check('恢复后精确落位（没被内容高度夹住）', res.leftOk === true, J(res));
    var gotDelta = deltaOf(leftPane, 'pdf-page-block-3');
    check('恢复后**页内位置**一致（±2px，同一行字还在原处）',
          Math.abs(gotDelta - 80) <= 2, '期望 +80 实际 ' + gotDelta.toFixed(1));
    // deltaOf 与代码里的 delta 反号 → 期望值取反
    check('恢复后右栏也精确回到记录的位置（第 2 页）',
          Math.abs(deltaOf(rightPane, 'pdf-trans-block-2') - (-rec.right.delta)) <= 2,
          '期望 ' + (-rec.right.delta).toFixed(1)
            + ' 实际 ' + deltaOf(rightPane, 'pdf-trans-block-2').toFixed(1));
    check('恢复后右栏确实在第 2 页（不是停在原地）',
          rightPane.scrollTop > 100, 'scrollTop=' + Math.round(rightPane.scrollTop));

    // ================= ⑤ 跨设备：采集认左栏原文 =================
    // 右栏此时在第 1 页、左栏在第 3 页 —— 旧实现只看右栏 → 推出去是 page 1
    var prog = _readingProgress();
    check('_readingProgress 认**左栏原文**的页码（修好了「恒为 0/1」）',
          !!prog && prog.page === 3, J(prog));

    // ================= ④ 缩放变了：按比例换算 =================
    // 模拟「缩放从 1.0 变成 1.5」→ 页高 300 → 450
    var ratio = rec.left.ratio;
    var blocks = leftList.querySelectorAll('.pdf-page-block');
    for (var i = 0; i < blocks.length; i++) blocks[i].style.height = '450px';
    _pdfZoom = 1.5;
    leftPane.scrollTop = 0;
    _applyReadPos(rec);
    var zoomDelta = deltaOf(leftPane, 'pdf-page-block-3');
    check('缩放变了以后按**比例**回位（不是死记 px）',
          Math.abs(zoomDelta - ratio * 450) <= 2,
          '期望 ' + (ratio * 450).toFixed(1) + ' 实际 ' + zoomDelta.toFixed(1));
    // 还原
    for (var j = 0; j < blocks.length; j++) blocks[j].style.height = '300px';
    _pdfZoom = 1.0;

    // ================= ⑥ 跨设备：跳页要能落到原文 =================
    // 把右栏的译文块全删掉 → 只认译文块的旧实现会「找不到 → 跳不过去」
    rightList.innerHTML = '';
    leftPane.scrollTop = 0;
    __toasts.length = 0;
    _applyReadingProgress({{paper_id: 'probe_paper_1', page: 3, zoom: 1.0}});
    await sleep(120);
    var jumped = leftPane.scrollTop;
    check('_applyReadingProgress 能跳到**原文**的第 3 页（没有译文块也跳得过去）',
          jumped > 0, 'scrollTop=' + Math.round(jumped));
    check('跳页后有「已跳到第 3 页」的提示', __toasts.length > 0, J(__toasts));

    // ================= ⑦ 存储上限 =================
    var big = {{}};
    for (var k = 0; k < 260; k++) big['p' + k] = {{updated_at: '2020-01-01T00:00:' + String(k % 60).padStart(2, '0') + '.000Z'}};
    _writeReadPosStore(big);
    check('本机最多记 200 篇（防 localStorage 膨胀）',
          Object.keys(_readPosStore()).length === 200,
          '实际 ' + Object.keys(_readPosStore()).length);

    document.title = 'RESULT:' + JSON.stringify(results);
  }})();
}}

run().catch(function (e) {{
  check('探针执行没抛异常', false, String((e && e.stack) || e));
  document.title = 'RESULT:' + JSON.stringify(results);
}});
}})();
</script></body></html>
"""


def build_probe(code: str) -> str:
    return PROBE_HTML.format(code=code)


def run_probe(browser: str, code: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "probe.html"
        html.write_text(build_probe(code), encoding="utf-8")
        dom = dump_dom(html, browser=browser, window_size="1400,900",
                       virtual_time_budget=20000)
    m = re.search(r"RESULT:(\[.*?\])</title>", dom, re.S)
    if not m:
        raise RuntimeError("没拿到探针结果（DOM 长度 %d）" % len(dom))
    raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
    return json.loads(raw)


def probe_from_html(browser: str, html: str) -> list[dict]:
    return run_probe(browser, slice_code(html))


# ---------------------------------------------------------------------------
# 变异自检：把 index.html 改坏后，同一套断言必须**出现失败**
# （没有这一段，上面那些断言可能全是恒真的、等于没写）
#
# ⚠ 目标串必须**在源码里唯一**（§4.119）：JS 的 String.replace 只替换第一处，
#   改错地方 → 护栏「红了」但是假阳性，真正的缺陷没测出来。
# ---------------------------------------------------------------------------
MUTATIONS = [
    ("采集时把页内位置丢掉（退化成只记页码）",
     [("        delta = d;", "        delta = 0;")]),
    ("采集时取错了页（取第一个而不是最后一个 delta<=0 的块）",
     [("        if (d > 0) {", "        if (d < 0) {")]),
    ("恢复时忘了把页顶挪到容器顶（少减 delta）",
     [("const target = scroller.scrollTop + (bRect.top - sTop) - signed;",
       "const target = scroller.scrollTop - signed;")]),
    ("恢复时永远定位到第 1 页",
     [("      const block = document.getElementById(pane.idPrefix + page);",
       "      const block = document.getElementById(pane.idPrefix + 1);")]),
    ("恢复时只回原文栏，右栏不回",
     [("      if (rec.right) {", "      if (rec.right && false) {")]),
    ("缩放变了也不按比例换算（死记 px）",
     [("        if (bh > 0) signed = -ratio * bh;", "        if (false) signed = -ratio * bh;")]),
    ("采集根本不落盘（进度只活在内存里）",
     [("      store[key] = rec;", "      /* 变异：不落盘 */")]),
    ("跨设备进度又只认译文栏（原文模式下推出去是错的页）",
     [("      const left = _readPosAnchor(_READPOS_PANES.pdf);\n"
       "      const right = _readPosAnchor(_READPOS_PANES.trans);\n"
       "      const primary = left || right;",
       "      const left = _readPosAnchor(_READPOS_PANES.pdf);\n"
       "      const right = _readPosAnchor(_READPOS_PANES.trans);\n"
       "      const primary = right || left;")]),
    ("跨设备跳页又只找译文块（原文页跳不过去）",
     [("        if (document.getElementById(pdfPane.idPrefix + prog.page)) {",
       "        if (false) {")]),
]


def run_mutation_selfcheck(browser: str, html: str) -> list[tuple[str, bool, str]]:
    out = []
    for name, edits in MUTATIONS:
        bad = [old for old, _ in edits if html.count(old) != 1]
        if bad:
            # ⚠ 目标串不唯一 → 变异会改错地方 → 这次自检**无效**，必须报出来
            out.append((name, False, "目标串在源码里出现 %d 次（必须唯一）：%s"
                        % (html.count(bad[0]), bad[0][:70].replace("\n", "\\n"))))
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
        print("SKIP: 找不到 Edge/Chrome，跳过续读护栏")
        return 0

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
    print("=== 阅读进度续读 · 真实渲染护栏（%s）===" % Path(browser).name)
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
