# -*- coding: utf-8 -*-
"""平板端 2026-10-27 独立审计的 8 处缺陷回归护栏。

**为什么要有这个文件**

平板工作台是**另一份前端**（`microbench/static/tablet/app.js`，~3900 行，
直连模型、不走桌面端后端）→ 桌面端修好的 bug **一行都不会**自动到平板。
2026-10-27 独立审查在平板端定位了 8 处已确认的缺陷，每一处都配了护栏：

| # | 缺陷 | 症状 |
|---|---|---|
| ① | 原生桥回包丢掉 `status` | APK 上 429 不换模型、400 不剥参，界面显示英文原文 |
| ② | 批注没有删除墓碑 | 桌面端删掉的批注在平板上复活并被推回云端 |
| ③ | AI 流式每个 delta 全量重渲染 | 4000 字答案 = 200 次全量 Markdown，低端平板冻结数秒 |
| ④ | 「清空本地数据」只清 notes | **API Key**（meta store）和 **76MB 原文**（pdfcache store）留在机器上 |
| ⑤ | 批注 id 可能超 64 字符 | 桌面端 `AnnotationItem.id` max_length=64 → 422 → 那篇论文在电脑上存不了批注 |
| ⑥ | 8 处 `fetch` 全无超时 | WebDAV 挂住 → 界面一直转圈，只能强杀应用 |
| ⑦ | 内存/存储无上限 | canvas 每页 7~8MB 累积；PDF 缓存无配额检查；`state.pdfs`/`aiHistory` 只增不减 |
| ⑧ | `buildNav()` 每次全量重建 DOM | 每次搜索/保存都把整个目录扔了重建，低端平板卡顿 |

**护栏形态**

两层都红才算数：

1. **源码级断言**（本文件，快）—— 结构性约束（不许有裸 `fetch(`、CSP 唯一、
   `deleted` 字段名必须与桌面端一致…）。
2. **行为级断言**（本文件驱动 node，见 `_test_audit_2026_10_27_tablet.js`）——
   真把 app.js 装进一个 DOM/IndexedDB/原生桥的沙箱里跑，
   断言「桥回 429 → 备用模型真的被调了 2 次」这种**行为**。

行为级那层是照抄仓库既有模式写的：
`tests/test_tablet_frontend.py`（node 跑 JS 套件）+ `microbench/_test_tablet_hardening.js`
（DOM/IndexedDB 沙箱）+ `tests/test_tablet_qa_budget.py`（源码级断言）。

跑法:  python -m pytest tests/test_audit_2026_10_27_tablet.py -q
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TABLET_DIR = REPO / "microbench" / "static" / "tablet"
APP_JS = TABLET_DIR / "app.js"
INDEX_HTML = TABLET_DIR / "index.html"


# ---------------------------------------------------------------------------
# 源码级断言
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def src() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ① 原生桥回包丢掉 status
# ---------------------------------------------------------------------------

def test_native_bridge_keeps_status(src: str):
    """原生桥 reject 时必须把 `status` 挂到 Error 上。

    Java 侧**确实**带了 status（`DavBridge.java:314-316` 回的是
    `{"ok":false,"status":429,"error":"..."}`）—— 是 JS 侧 `new Error(payload.error)`
    把它整个丢了 → 上层 `Number(e1.status) || 0` 恒为 0 →
    「429 换模型」「400 剥 reasoning_effort/thinking」两条兜底**永远不命中**。
    """
    fn = src[src.index("window.__mbNativeResolve = function"):][:900]
    assert "err.status" in fn, \
        "原生桥 reject 时没有把 payload.status 挂到 Error 上（429 换模型永远不生效）"
    assert "payload.status" in fn or "p.status" in fn, \
        "status 不是从回包里取的"


def test_ai_request_converts_native_failure_to_friendly_chinese(src: str):
    """原生桥失败也要转成中文人话（并保留 status）。"""
    fn = src[src.index("async function aiRequest("):][:2600]
    assert "friendlyHttpError" in fn, "原生桥失败没有走友好文案"
    assert "err.status = st" in fn, "原生桥失败时 status 没有带出去"


def test_test_llm_does_not_blame_cors_in_apk(src: str):
    """APK 里没有跨域问题 —— 不能再一律说「可能是浏览器跨域限制」。"""
    fn = src[src.index("async function testLLM("):][:2600]
    start = fn.index('typeof nativeBridge.httpPost === "function"')
    # 原生分支到它自己的 return 为止（后面才是浏览器分支）
    native_part = fn[start:fn.index("const res = await fetchWithTimeout(url,", start)]
    # 剥掉注释 —— 注释里会写「原来一律说『可能是浏览器跨域限制』」这种说明
    native_code = _strip_comments(native_part)
    assert "浏览器跨域限制" not in native_code, \
        "原生分支还在说「可能是浏览器跨域限制」（APK 里是误导）"
    assert "friendlyHttpError" in native_code, "原生分支没有按状态码区分"


# ---------------------------------------------------------------------------
# ② 批注删除墓碑
# ---------------------------------------------------------------------------

def test_annotation_tombstone_protocol_matches_desktop(src: str):
    """墓碑字段名必须与桌面端**逐字一致**。

    桌面端 `AnnotationSaveRequest`（`microbench/app.py:5221`）里是
    `deleted: list[str]`；`_merge_annotation_lists`（app.py:4875）最后应用它。
    平板自创字段名 = 桌面端认不出来 = 墓碑白写。
    """
    fn = src[src.index("async function pushAnnosToCloud("):][:2600]
    assert "deleted" in fn, "pushAnnosToCloud 没有上报 deleted 墓碑"
    m = re.search(r"deleted:\s*deleted", fn)
    assert m, "payload 里的字段名不是 deleted（必须与 app.py 一致）"
    # 墓碑必须最后应用（先合并再删）
    merge = src[src.index("function mergeAnnos("):][:900]
    assert "map.delete" in merge, "mergeAnnos 没有应用墓碑"
    assert merge.index("map.set") < merge.index("map.delete"), \
        "墓碑必须在合并**之后**应用（否则同 id 会被 upsert 又加回来）"


def test_annotation_tombstone_needs_a_base(src: str):
    """墓碑不能只看「云端有而本地没有」—— 那会把电脑端**刚加**的批注删掉。

    判据要靠 `base`（上次同步时**云端有的** id）+ `cloudKnown`
    （云端状态未知时一条墓碑都不许推算）。
    """
    fn = src[src.index("function annoTombstones("):][:1600]
    assert "base" in fn, "annoTombstones 没有用 base 判断「是不是刚加的」"
    assert "cloudIds" in fn, "annoTombstones 没有比较云端的 id 集合"
    assert "cloudKnown" in fn, \
        "annoTombstones 没有区分「云端读到了」和「云端状态未知」"
    assert "if (cloudKnown)" in fn, \
        "云端状态未知时还在推算墓碑（会把平板自己的批注全当成被删了）"


def test_desktop_protocol_field_is_deleted():
    """桌面端那边也得是 `deleted` —— 两端必须一致（防桌面端被改坏）。"""
    app_py = (REPO / "microbench" / "app.py").read_text(encoding="utf-8")
    assert re.search(r"deleted:\s*list\[str\]", app_py), \
        "桌面端 AnnotationSaveRequest.deleted 不见了（两端字段名对不上）"


# ---------------------------------------------------------------------------
# ③ AI 流式渲染节流
# ---------------------------------------------------------------------------

def test_streaming_render_is_throttled(src: str):
    """流式渲染必须节流（onDelta 收到的是全量累积文本）。"""
    assert "AI_RENDER_THROTTLE_MS" in src, "没有渲染节流阈值"
    fn = src[src.index("const onDelta = (txt) =>"):][:1200]
    assert "flushRender" in fn, "onDelta 没有走节流渲染"
    assert "buf.innerHTML = renderMarkdown(txt)" not in fn, \
        "onDelta 还在每个 delta 全量重渲染（O(n²)）"


def test_math_rendering_is_cached(src: str):
    """公式渲染要缓存（同一条公式不该反复跑 KaTeX）。"""
    assert "_mathCache" in src, "公式没有缓存"
    assert "MATH_CACHE_MAX" in src, "公式缓存没有上限（换个内存泄漏）"


# ---------------------------------------------------------------------------
# ④ 清空本地数据
# ---------------------------------------------------------------------------

def test_reset_all_clears_every_store(src: str):
    """「清空本地数据」必须清**三个** store。

    meta store 里明文存着 API Key，pdfcache store 里躺着 76MB 原文 ——
    只清 notes 等于没清。
    """
    fn = src[src.index("async resetAll("):][:600]
    for store in ("clearNotes", "clearMeta", "clearPdfCache"):
        assert store in fn, f"resetAll 没有清 {store}"
    btn = src[src.index('$("btn-reset").onclick'):][:600]
    assert "DB.resetAll()" in btn, "「清空本地数据」按钮没有调 DB.resetAll()"
    assert "localStorage.clear()" in btn, "按钮没有清 localStorage"
    assert "location.reload()" in btn, "清完没有重载页面"


# ---------------------------------------------------------------------------
# ⑤ 批注 id 长度
# ---------------------------------------------------------------------------

def test_ink_anno_id_is_short_and_stable(src: str):
    """手写批注 id 必须 ≤64 且同论文稳定。"""
    assert "function inkAnnoId(" in src, "没有抽出 inkAnnoId"
    fn = src[src.index("function inkAnnoId("):][:400]
    assert "hash(" in fn, "inkAnnoId 不是用稳定哈希算的（同一篇论文会算出不同的 id）"
    body = src[src.index("async function saveInk("):][:2000]
    assert "inkAnnoId(key)" in body, "saveInk 还在用旧的长 id"


# ---------------------------------------------------------------------------
# ⑥ fetch 超时
# ---------------------------------------------------------------------------

def _strip_comments(src: str) -> str:
    """剥掉注释，只留代码（否则注释里的字样会被当成真代码）。

    ⚠ 行注释的正则要带 `(?<!:)` —— 否则 `https://…` 里的 `//` 会被当成注释起点。
    """
    code = re.sub(r"/\*[\s\S]*?\*/", "", src)
    code = re.sub(r"(?<!:)//[^\n]*", "", code)
    return code


def test_every_fetch_has_a_timeout(src: str):
    """**不许**再有裸 `fetch(`。

    ⚠ 只查 `await fetch(` 这种写法不够 —— `return fetch(...)` 也是裸的。
    这里把所有 `fetch(` 出现的地方列出来（fetchWithTimeout 的**实现本身**除外），
    剩下的必须一个都没有。
    """
    code = _strip_comments(src)
    helper_start = code.index("function fetchWithTimeout(")
    # 按大括号配平，找到这个函数的结束位置
    depth = 0
    end = helper_start
    opened = False
    for j in range(helper_start, len(code)):
        if code[j] == "{":
            depth += 1
            opened = True
        elif code[j] == "}":
            depth -= 1
            if opened and depth == 0:
                end = j
                break
    bare = []
    for m in re.finditer(r"(?<![\w.])fetch\s*\(", code):
        if helper_start <= m.start() <= end:
            continue          # 这就是 fetchWithTimeout 的实现本身
        line = code[:m.start()].count("\n") + 1
        bare.append((line, code.splitlines()[line - 1].strip()))
    assert not bare, "还有裸 fetch（没有超时）：\n" + "\n".join(
        f"  app.js:{ln}: {t}" for ln, t in bare)


def test_fetch_timeout_helper_exists(src: str):
    """统一超时助手要用 AbortController。"""
    assert "function fetchWithTimeout(" in src, "没有 fetchWithTimeout"
    fn = src[src.index("function fetchWithTimeout("):][:1500]
    assert "AbortController" in fn, "fetchWithTimeout 没用 AbortController"
    assert "clearTimeout" in fn, "fetchWithTimeout 没清定时器"


def test_ai_fetch_timeout_is_wider_than_qa_timeout(src: str):
    """AI 请求的 fetch 超时必须**比答疑时限宽**。

    否则 fetch 先超时 → 抛 AbortError → runAI 当成「用户点了停止」，
    用户看到的是「已停止」而不是「等太久了」。
    """
    ai_ms = int(re.search(r"const AI_FETCH_TIMEOUT_MS\s*=\s*(\d+)", src).group(1))
    qa_ms = int(re.search(r"const AI_QA_TIMEOUT_MS\s*=\s*(\d+)", src).group(1))
    assert ai_ms > qa_ms, \
        f"AI fetch 超时 {ai_ms}ms 必须大于答疑时限 {qa_ms}ms"


# ---------------------------------------------------------------------------
# ⑦ 内存 / 存储上限
# ---------------------------------------------------------------------------

def test_pdf_canvas_window_is_bounded(src: str):
    """canvas 只保留当前页 ±2（每页 7~8MB，累积起来会把平板撑爆）。"""
    assert "PDF_CANVAS_WINDOW" in src, "没有 canvas 保留窗口"
    m = re.search(r"const PDF_CANVAS_WINDOW\s*=\s*(\d+)", src)
    assert m, "PDF_CANVAS_WINDOW 不是常量"
    assert int(m.group(1)) <= 5, \
        f"canvas 保留窗口是 {m.group(1)} 页 —— 太大了，等于没限制"
    assert "function pruneCanvases(" in src, "没有释放离屏页 canvas 的逻辑"
    fn = src[src.index("function pruneCanvases("):][:1200]
    assert "canvas.width = 0" in fn, "释放 canvas 时没有把位图尺寸归零"
    assert "function restoreCanvas(" in src, "离屏页滚回来时没有补画"


def test_pdf_cache_checks_quota(src: str):
    """缓存全部原文前要先看配额（QuotaExceededError 不能被吞成「失败 24」）。"""
    fn = src[src.index("async function cacheAllPdfs("):][:4000]
    assert "navigator.storage" in fn and "estimate" in fn, \
        "缓存原文前没有预检存储配额"
    assert "QuotaExceededError" in fn, "配额用满被当成普通下载失败吞掉了"
    assert "空间不够" in fn or "空间已满" in fn, "配额不够时没有明确提示"


def test_state_lists_are_capped(src: str):
    """`state.pdfs` 和 `aiHistory` 都要有上限。"""
    assert "PDF_LIST_MAX" in src, "state.pdfs 没有上限"
    assert "AI_HISTORY_MAX_NOTES" in src, "aiHistory 没有上限"
    fn = src[src.index("function aiHistory("):][:900]
    assert "AI_HISTORY_MAX_NOTES" in fn, "aiHistory 没有真的按上限回收"


# ---------------------------------------------------------------------------
# ⑧ buildNav 增量重建
# ---------------------------------------------------------------------------

def test_build_nav_is_incremental(src: str):
    """目录树必须增量重建（每次搜索/保存全量重建 DOM = 低端平板卡顿）。"""
    assert "function reconcileNav(" in src, "没有增量对齐逻辑"
    fn = src[src.index("function buildNav("):][:2400]
    assert "navItemNode" in fn, "buildNav 没有复用已有的节点"
    assert "reconcileNav(list, wanted)" in fn, "buildNav 没有走增量对齐"
    # 清空只允许出现在「一条都没有」的空态分支里
    #（重建时把整个列表 innerHTML="" 扔掉 = 又回到全量重建）
    tail = fn[fn.index("reconcileNav(list, wanted)"):]
    assert 'list.innerHTML = ""' not in tail, "重建路径上还是在清空整个目录"


# ---------------------------------------------------------------------------
# 额外小修
# ---------------------------------------------------------------------------

def test_safe_err_masks_bearer_token(src: str):
    """`Bearer <token>` 必须**整体**脱敏。

    原来只抹掉 "Bearer" 那个词 → `Authorization: *** abcdefghijklmnopqrst`
    —— 令牌原样留在界面上。
    """
    fn = src[src.index("function safeErr("):][:1200]
    assert re.search(r"bearer\\s\+", fn, re.I), "safeErr 没有处理 Bearer 令牌"


def test_dead_state_is_gone(src: str):
    """`state.trans` / `state.annos` 是死状态（全文件无写入）→ 删掉。"""
    # 剥掉行注释再查 —— 注释里会写「以前这里还有 trans: new Map()」这种说明，
    # 正则会把它当成真代码。
    code = re.sub(r"//[^\n]*", "", src)
    assert not re.search(r"state\.trans\s*=", code), "state.trans 还在"
    assert not re.search(r"state\.annos\s*=", code), "state.annos 还在"
    assert "trans: new Map()" not in code, "死状态 trans 还在"
    assert "annos: new Map()" not in code, "死状态 annos 还在"


def test_dead_file_input_is_gone(html: str):
    """`#file-dir`（webkitdirectory）从来没被接线 → 删掉。"""
    code = re.sub(r"<!--[\s\S]*?-->", "", html)
    assert "file-dir" not in code, "死元素 #file-dir 还在"


# ---------------------------------------------------------------------------
# CSP
# ---------------------------------------------------------------------------

def test_csp_meta_exists_and_is_unique(html: str):
    """CSP 只能有**一条**。

    ⚠ 多个 CSP meta 的策略是**叠加**的（取最严）——
    2026-10-27 就出过一次：两条各写各的，少了 `connect-src blob:`
    → PDF.js 的 `fetch(blobUrl)` 被打死 → **原文根本打不开**。
    """
    metas = re.findall(r'<meta[^>]*http-equiv="Content-Security-Policy"[^>]*>', html)
    assert len(metas) == 1, f"CSP meta 应该有且只有 1 条，实际 {len(metas)} 条：\n" + "\n".join(metas)


def test_csp_blocks_external_scripts(html: str):
    """CSP 必须挡住**外域脚本**（页面能拿到用户的 API Key）。"""
    m = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html)
    assert m, "没有找到 CSP meta"
    policy = m.group(1)
    assert "script-src" in policy, "CSP 没有 script-src"
    script_src = policy.split("script-src")[1].split(";")[0]
    assert "'self'" in script_src, "script-src 没有 'self'"
    assert "https:" not in script_src, "script-src 放行了外域脚本"
    assert "http:" not in script_src, "script-src 放行了外域脚本"
    assert "'unsafe-eval'" not in script_src, "script-src 放了 'unsafe-eval'"


def test_csp_allows_what_the_app_needs(html: str):
    """CSP 不能把**现有功能**打死（这几条都是实测过的硬需求）。"""
    m = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', html)
    assert m, "没有找到 CSP meta"
    policy = m.group(1)

    def directive(name: str) -> str:
        assert name in policy, f"CSP 少了 {name}"
        return policy.split(name)[1].split(";")[0]

    # ① KaTeX 的字体是 data: URI 内联的 → 不给 data: 公式会掉字体
    assert "data:" in directive("font-src"), "font-src 没放行 data:（KaTeX 字体会被打死）"
    # ② PDF.js 是用 getDocument({url: blobUrl}) 打开原文的 → 那是真实的 fetch(blob:)
    assert "blob:" in directive("connect-src"), \
        "connect-src 没放行 blob:（PDF.js 打不开原文）"
    # ③ PDF.js 的 worker 是 Blob URL 起的
    assert "blob:" in directive("worker-src"), "worker-src 没放行 blob:（PDF.js 起不来）"
    # ④ app.js / pdf.js / katex 全是内联的
    assert "'unsafe-inline'" in directive("script-src"), "内联的 app.js 会被 CSP 打死"
    assert "'unsafe-inline'" in directive("style-src"), "KaTeX 动态插的 style 会被打死"
    # ⑤ AI 网关是 https
    assert "https:" in directive("connect-src"), "connect-src 没放行 https:（AI 连不上）"
    # ⑥ 笔记里的图片可以是外链 https
    assert "https:" in directive("img-src"), "img-src 没放行 https:（笔记里的外链图会碎）"


def test_csp_has_no_duplicate_meta_in_build_template(html: str):
    """打包模板里也不能有第二条 CSP（export_offline 会原样带进 APK）。"""
    assert html.count("Content-Security-Policy") == 1, \
        "index.html 里出现了多处 Content-Security-Policy"


# ---------------------------------------------------------------------------
# 行为级护栏（node 沙箱）
# ---------------------------------------------------------------------------

HARNESS = r"""
/* 平板端 2026-10-27 审计的行为级护栏。
 * 由 tests/test_audit_2026_10_27_tablet.py 调用：
 *     node _test_audit_2026_10_27_tablet.js <group>
 * 沙箱模式照抄 microbench/_test_tablet_hardening.js（DOM / IndexedDB shim）。
 */
const path = require("path");
const APP = process.env.MB_APP;
const group = process.argv[2] || "all";

// ---------------------------------------------------------------- DOM shim
function mkText(t) {
  const n = {
    nodeType: 3,
    nodeValue: String(t == null ? "" : t),
    parentNode: null,
  };
  n.textContent = n.nodeValue;
  return n;
}

function mkEl(tag) {
  const node = {
    tagName: String(tag || "").toUpperCase(),
    nodeType: 1,
    className: "",
    _text: "",
    _html: "",
    children: [],
    parentNode: null,
    style: {},
    hidden: false,
    value: "",
    scrollTop: 0, scrollHeight: 0, clientHeight: 0, offsetWidth: 0, offsetTop: 0,
    listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { c.parentNode = node; node.children.push(c); return c; },
    insertBefore(c, ref) {
      c.parentNode = node;
      const i = ref ? node.children.indexOf(ref) : -1;
      if (i < 0) node.children.push(c); else node.children.splice(i, 0, c);
      return c;
    },
    removeChild(c) {
      const i = node.children.indexOf(c);
      if (i >= 0) node.children.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k]; },
    removeAttribute(k) { delete this[k]; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    focus() {},
    get firstChild() { return this.children[0] || null; },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = String(v);
      this.children = [];
      if (global.__htmlWrites) {
        global.__htmlWrites.push({ tag: this.tagName, len: this._html.length });
      }
    },
  };
  return node;
}

const byId = new Map();
function getEl(id) {
  if (!byId.has(id)) byId.set(id, mkEl("div"));
  return byId.get(id);
}

global.window = global;
global.document = {
  addEventListener: () => {},
  getElementById: (id) => getEl(id),
  createElement: (tag) => mkEl(tag),
  createTextNode: (t) => mkText(t),
  documentElement: { setAttribute() {}, getAttribute: () => null },
  body: mkEl("body"),
  querySelector: () => null,
  querySelectorAll: () => [],
};
global.localStorage = {
  getItem: () => null, setItem: () => {}, removeItem: () => {},
  clear() { global.__lsCleared = true; },
};
global.matchMedia = () => ({ matches: false });
global.getSelection = () => ({
  toString: () => "", rangeCount: 0,
  getRangeAt: () => ({ getBoundingClientRect: () => ({}) }),
});
global.location = { hash: "", reload() { global.__reloaded = true; } };

// KaTeX 桩 —— 顺便数一数到底被调了几次（公式缓存的护栏要用）
global.__katexCalls = 0;
global.katex = {
  renderToString(tex, opts) {
    global.__katexCalls++;
    return '<span class="katex' + (opts && opts.displayMode ? " katex-display" : "")
      + '" data-tex="' + tex + '"></span>';
  },
};

// ------------------------------------------------------------ IndexedDB 桩
const STORE_KEYS = { notes: "id", meta: "k", pdfcache: "id" };

function fakeIndexedDB() {
  const data = { notes: new Map(), meta: new Map(), pdfcache: new Map() };
  function makeReq(result) {
    const r = { result, onsuccess: null, onerror: null };
    setTimeout(() => { if (r.onsuccess) r.onsuccess(); }, 0);
    return r;
  }
  function makeStore(name) {
    const key = STORE_KEYS[name];
    return {
      put(v) { data[name].set(v[key], JSON.parse(JSON.stringify(v))); return makeReq(undefined); },
      get(k) { return makeReq(data[name].get(k)); },
      getAll() { return makeReq([...data[name].values()].map((x) => JSON.parse(JSON.stringify(x)))); },
      getAllKeys() { return makeReq([...data[name].keys()]); },
      delete(k) { data[name].delete(k); return makeReq(undefined); },
      clear() { data[name].clear(); return makeReq(undefined); },
      createIndex() { return {}; },
    };
  }
  function makeTx() {
    const t = { objectStore: makeStore, oncomplete: null, onerror: null, onabort: null };
    setTimeout(() => { if (t.oncomplete) t.oncomplete(); }, 0);
    return t;
  }
  const db = {
    objectStoreNames: { contains: (n) => Object.prototype.hasOwnProperty.call(data, n) },
    createObjectStore(name) { data[name] = new Map(); return makeStore(name); },
    transaction() { return makeTx(); },
  };
  return {
    data,
    api: {
      open() {
        const req = { result: db, onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null };
        setTimeout(() => {
          if (req.onupgradeneeded) req.onupgradeneeded();
          if (req.onsuccess) req.onsuccess();
        }, 0);
        return req;
      },
    },
  };
}

const fakeDb = fakeIndexedDB();
global.indexedDB = fakeDb.api;

// --------------------------------------------------- 原生桥（① / ② / ⑨ 组要）
const bridgeCalls = [];
const bridgePuts = [];
const bridgeAllowHosts = [];
// 还能失败几次（用来模拟「第一次 429、第二次成功」）
let bridgeFailLeft = 0;
if (group === "native429" || group === "anno" || group === "allowhost") {
  global.MicroBench = {
    davConfigured() { return true; },
    allowAiHost(base) { bridgeAllowHosts.push(base); },
    putSync(path, content, cbId) {
      let payload = {};
      try { payload = JSON.parse(content); } catch (e) { payload = {}; }
      bridgePuts.push({ path, payload });
      // ⚠ 原生桥必须回调（真实 Java 侧就是回调 __mbNativeResolve），
      //   不回调的话 nativeCall 会一直等到 150s 超时。
      if (typeof cbId === "string") {
        setTimeout(() => global.__mbNativeResolve(cbId, true, { ok: true }), 0);
      }
    },
    httpPost(url, body, auth, cbId) {
      let payload = {};
      try { payload = JSON.parse(body); } catch (e) { payload = {}; }
      bridgeCalls.push({ url, body, auth, model: payload.model });
      const fail = bridgeFailLeft > 0;
      if (fail) bridgeFailLeft--;
      setTimeout(() => {
        if (fail) {
          // Java 侧回的就是这种包（DavBridge.java:314-316）
          global.__mbNativeResolve(cbId, false, {
            ok: false, status: 429, error: "Token Plan limit exceeded (quota)",
          });
        } else {
          global.__mbNativeResolve(cbId, true, {
            ok: true, status: 200,
            body: 'data: {"choices":[{"delta":{"content":"备用模型的答案"}}]}\n\ndata: [DONE]\n\n',
          });
        }
      }, 0);
    },
  };
}

require(APP);
const T = global.__MB_TEST__;
if (!T) { console.error("FAIL: app.js 没有暴露 __MB_TEST__"); process.exit(1); }

let pass = 0, fail = 0;
function check(cond, label) {
  if (cond) { pass++; console.log("  PASS  " + label); }
  else { fail++; console.log("  FAIL  " + label); }
}

function statusText() {
  // 取**最后一个**状态条 —— 一次 runAI 会新起一个回答块，
  // 取第一个的话会拿到上一轮的陈旧状态。
  const out = getEl("ai-output");
  let found = "";
  for (const turn of out.children) {
    for (const child of turn.children) {
      if (child.className === "tb-ai-status") found = child.textContent;
    }
  }
  return found;
}

// ------------------------------------------------------------------ ① 429
async function checkNative429() {
  // ①a: Error 上有 status（这是「429 换模型」能生效的前提）
  bridgeFailLeft = 1;
  let err = null;
  try {
    await T.nativeCall("httpPost", ["https://api.example.com/v1/chat/completions", "{}", "Bearer k"]);
  } catch (e) { err = e; }
  check(!!err, "原生桥失败会 reject");
  check(err && err.status === 429,
    "Error 上带着 status=429（实际 " + (err && err.status) + "）");

  // ①b: 429 → 换备用模型 → 备用模型真的被调了（而且成功了）
  bridgeCalls.length = 0;
  bridgeFailLeft = 1;                 // 第一次 429，第二次成功
  T.resetThrottle();
  T.state.ai.key = "sk-test-0123456789";
  T.state.ai.base = "https://api.example.com/v1";
  T.state.ai.model = "MiniMax-M3.1-Flash";
  T.state.ai.modelT = "MiniMax-M3";
  getEl("ai-question").value = "这个实验说明了什么？";
  await T.runAI("ask");
  check(bridgeCalls.length === 2,
    "429 之后换了备用模型重试（实际调用 " + bridgeCalls.length + " 次，期望 2）");
  check(bridgeCalls.length === 2 && bridgeCalls[0].model !== bridgeCalls[1].model,
    "第二次用的是**另一个**模型（" + bridgeCalls.map((c) => c.model).join(" -> ") + "）");
  check(/完成/.test(statusText()),
    "备用模型答出来了（状态: " + statusText() + "）");
  check(!/Token Plan limit exceeded/.test(statusText()),
    "界面上没有裸露的上游英文报错（状态: " + statusText() + "）");

  // ①c: 两个模型都 429 → 界面必须是中文人话，不是英文原文
  bridgeCalls.length = 0;
  bridgeFailLeft = 2;                 // 两次都失败
  T.resetThrottle();
  getEl("ai-question").value = "再来一次";
  await T.runAI("ask");
  check(bridgeCalls.length === 2, "两个模型各试一次就停（实际 " + bridgeCalls.length + " 次）");
  check(/额度|配额|429/.test(statusText()),
    "全失败时给的是中文文案（状态: " + statusText() + "）");
  check(!/Token Plan limit exceeded/.test(statusText()),
    "全失败时**没有**把上游英文甩给用户（状态: " + statusText() + "）");
}

// ------------------------------------------------------------ ② + ⑤ 批注
async function checkAnno() {
  // ②a: 墓碑在合并里生效
  const merged = T.mergeAnnos(
    [{ id: "a1", quote: "云端的" }],
    [{ id: "a2", quote: "本地的" }],
    ["a1"]);
  check(merged.length === 1 && merged[0].id === "a2",
    "墓碑能把云端那条删掉（实际 " + JSON.stringify(merged) + "）");

  // ②b: 墓碑的判据
  //   base = 「上次同步时云端有的 id」→ 现在云端没了 = 电脑端删了
  const t1 = T.annoTombstones({ base: ["a1", "a2"], deleted: [] }, [{ id: "a1" }], [{ id: "a1" }, { id: "a2" }], [], true);
  check(t1.indexOf("a2") >= 0, "云端删掉的 id 进墓碑（" + JSON.stringify(t1) + "）");
  const t2 = T.annoTombstones({ base: ["a1"], deleted: [] }, [], [{ id: "a1" }], [], true);
  check(t2.indexOf("a1") >= 0, "云端删掉的 id 进墓碑（别再推回去）");
  const t3 = T.annoTombstones({ base: [], deleted: [] }, [{ id: "new" }], [], [], true);
  check(t3.indexOf("new") < 0, "电脑端刚加的批注不能被当成墓碑");
  const t4 = T.annoTombstones({ base: [], deleted: ["x"] }, [], [], [], true);
  check(t4.indexOf("x") >= 0, "本地删掉的 id（rec.deleted）进墓碑");
  // ⚠⚠ 云端状态未知（离线 / DAV 挂了）→ 一条墓碑都不许推算，
  //   否则平板自己的批注会被当成「被电脑端删了」→ 全清空。
  const t5 = T.annoTombstones({ base: ["a1", "a2"], deleted: [] }, [], [{ id: "a1" }], [], false);
  check(t5.length === 0,
    "云端状态未知时不推算墓碑（否则会把自己的批注全删了）: " + JSON.stringify(t5));

  // ②c: 端到端 —— 桌面端删掉的批注在平板上不会复活
  await T.openDB();
  const rel = "01_Literature/01_论文原文_PDF/测试论文.pdf";
  const key = T.safePaperKey(T.paperIdFor(rel));
  await T.DB.putAnnos(key, {
    items: [{ id: "a1", quote: "本地的" }], dirty: 0, at: 0,
    base: ["a1"], deleted: [],
  });
  global.fetch = async () => ({
    ok: true,
    json: async () => ({ annotations: [{ id: "a2", quote: "云端的" }] }),
  });
  const out = await T.loadAnnotations(rel);
  check(!out.some((a) => a.id === "a1"),
    "云端删掉的批注在平板上不会复活（实际 " + JSON.stringify(out) + "）");
  check(out.some((a) => a.id === "a2"), "云端新增的批注会拉下来");

  // ②d: 电脑端删掉的批注**不能**被推回云端（墓碑随 payload 上报）
  //   场景：平板本地有 a1、a2（都同步过），电脑端把 a1 删了 → 云端只剩 a2。
  await T.DB.putAnnos(key, {
    items: [{ id: "a1", quote: "本地的" }, { id: "a2", quote: "云端的" }],
    dirty: 1, at: 0, base: ["a1", "a2"], deleted: [],
  });
  global.fetch = async () => ({
    ok: true,
    json: async () => ({ annotations: [{ id: "a2", quote: "云端的" }] }),
  });
  bridgePuts.length = 0;
  await T.saveAnnotations(rel, [
    { id: "a1", quote: "本地的" }, { id: "a2", quote: "云端的" },
  ], true);
  const pushed = bridgePuts[0] && bridgePuts[0].payload;
  check(pushed && !pushed.annotations.some((a) => a.id === "a1"),
    "电脑端删掉的批注**没有**被推回云端（实际: "
    + JSON.stringify(pushed && pushed.annotations) + "）");
  check(pushed && Array.isArray(pushed.deleted) && pushed.deleted.indexOf("a1") >= 0,
    "墓碑 deleted 随 payload 上报（协议字段名与电脑端一致）: "
    + JSON.stringify(pushed && pushed.deleted));

  // ②e: 云端读失败 → **不能写**（否则用平板这一份覆盖掉云端那半边）
  global.fetch = async () => { throw new Error("offline"); };
  bridgePuts.length = 0;
  const okPush = await T.saveAnnotations(rel, [{ id: "a1", quote: "本地的" }], true);
  check(okPush === false, "云端读不到时不同步（本地那份留着，下次再试）");
  check(bridgePuts.length === 0,
    "云端状态未知时**没有**往云端写（实际写了 " + bridgePuts.length + " 次）");

  // ⑤: id 长度
  const longRel = "01_Literature/" + "A".repeat(70) + ".pdf";
  const longKey = T.safePaperKey(T.paperIdFor(longRel));
  const id = T.inkAnnoId(longKey);
  check(id.length <= 64, "手写批注 id 不超过 64（实际 " + id.length + "）");
  check(id === T.inkAnnoId(longKey), "同一篇论文的 id 稳定");
  check(("ink_" + longKey).length > 64,
    "旧写法（ink_ + paper_id）确实超了 64（" + ("ink_" + longKey).length + "）—— 证明护栏不是空跑");
}

// ---------------------------------------------------------------- ⑥ 超时
async function checkFetchTimeout() {
  let aborted = false;
  global.fetch = (url, opts) => new Promise((resolve, reject) => {
    const sig = opts && opts.signal;
    if (!sig) return;
    const onAbort = () => {
      aborted = true;
      const e = new Error("aborted"); e.name = "AbortError"; reject(e);
    };
    if (sig.aborted) onAbort(); else sig.addEventListener("abort", onAbort);
  });
  let err = null;
  // ⚠ 看门狗：不 abort 的话这个 Promise 永远不会 settle → 不能干等
  const res = await Promise.race([
    T.fetchWithTimeout("https://example.invalid/x", {}, 60).then(() => null, (e) => e),
    new Promise((r) => setTimeout(() => r("no-timeout"), 3000)),
  ]);
  if (res && res !== "no-timeout") err = res;
  check(!!err && err !== "no-timeout", "超时会 reject");
  check(!!err && err.timeout === true,
    "超时错误带 timeout 标记（实际: " + (err && err.message) + "）");
  check(aborted, "底层请求真的被 abort 了");
}

// ------------------------------------------------------------ ④ 清空数据
async function checkResetAll() {
  await T.openDB();
  await T.DB.putNote({ id: "n1", title: "t", group: "g", rel: "n1", body: "b" });
  await T.DB.setMeta("ai", { key: "sk-secret-value" });
  await T.DB.cachePdf("p1", { size: 10 });
  check(fakeDb.data.notes.size === 1 && fakeDb.data.meta.size >= 1
    && fakeDb.data.pdfcache.size === 1, "三个 store 都有东西（前提）");
  await T.DB.resetAll();
  check(fakeDb.data.notes.size === 0, "notes 清空了");
  check(fakeDb.data.meta.size === 0, "meta 清空了（API Key 不再留在机器上）");
  check(fakeDb.data.pdfcache.size === 0, "pdfcache 清空了（76MB 原文不再占地方）");
}

// ------------------------------------------------------------ ③ 节流渲染
async function checkThrottle() {
  const CHUNKS = 200;
  const full = ("$x$" + "字".repeat(17)).repeat(CHUNKS);   // 4000 字 / 200 个公式
  const parts = [];
  for (let i = 0; i < CHUNKS; i++) {
    const piece = full.slice(i * 20, (i + 1) * 20);
    parts.push("data: " + JSON.stringify({ choices: [{ delta: { content: piece } }] }) + "\n");
  }
  parts.push("data: [DONE]\n");
  global.fetch = async () => ({
    ok: true,
    body: {
      getReader: () => {
        let i = 0;
        return {
          read: async () => {
            if (i >= parts.length) return { done: true };
            const v = parts[i++];
            return { done: false, value: new TextEncoder().encode(v) };
          },
        };
      },
    },
  });
  global.__htmlWrites = [];
  global.__katexCalls = 0;
  T.resetThrottle();
  T.state.ai.key = "sk-test-0123456789";
  T.state.ai.base = "https://api.example.com/v1";
  getEl("ai-question").value = "问题";
  await T.runAI("ask");
  const writes = global.__htmlWrites;
  const fullRenders = writes.filter((w) => w.len >= 1000);
  check(fullRenders.length <= 5,
    "流式渲染被节流了（200 片只全量渲染了 " + fullRenders.length + " 次）");
  check(global.__katexCalls <= 3,
    "公式渲染走了缓存（200 个公式只调了 " + global.__katexCalls + " 次 KaTeX）");
  // 最终输出内容必须一致
  const last = writes.filter((w) => w.len >= 1000).pop();
  const expect = T.renderMarkdown(full);
  check(!!last && last.len === expect.length,
    "最终渲染的内容长度一致（" + (last && last.len) + " vs " + expect.length + "）");
}

// ------------------------------------------------------------ ⑧ 目录增量
async function checkNav() {
  T.state.notes.clear();
  for (let i = 0; i < 60; i++) {
    T.state.notes.set("n" + i, {
      id: "n" + i, title: "笔记" + i, group: "00_Dashboard",
      rel: "n" + i, body: "正文", dirty: 0,
    });
  }
  T.buildNav();
  const list = getEl("nav-list");
  const first = list.children.slice();
  check(first.length === 61,
    "目录渲染了 60 条 + 1 个分组标题（实际 " + first.length + "）");
  T.buildNav();
  const second = list.children.slice();
  let reused = 0;
  second.forEach((n, i) => { if (n === first[i]) reused++; });
  check(reused === first.length,
    "重建**复用**了原来的节点（复用 " + reused + "/" + first.length + "）");
  check(second.length === first.length, "重建后条目数不变");
  T.state.filter = "笔记1";
  T.buildNav();
  check(getEl("nav-list").children.length < first.length, "搜索后条目变少");
  T.state.filter = "";
  T.buildNav();
  check(getEl("nav-list").children.length === first.length, "清掉搜索条件后条目恢复");
}

// ------------------------------------------------- ⑨ 自定义网关登记到原生白名单
async function checkAllowHost() {
  await T.openDB();
  // ① 保存设置时把网关地址登记给原生白名单
  bridgeAllowHosts.length = 0;
  getEl("cfg-base").value = "https://my-gateway.example.com/v1";
  getEl("cfg-key").value = "sk-test-0123456789";
  getEl("cfg-model").value = "MiniMax-M3.1-Flash";
  getEl("cfg-model-t").value = "MiniMax-M3";
  await T.saveSettings();
  check(bridgeAllowHosts.length >= 1,
    "保存设置时登记了网关白名单（实际 " + bridgeAllowHosts.length + " 次）");
  check(bridgeAllowHosts.length >= 1
    && bridgeAllowHosts[bridgeAllowHosts.length - 1] === T.state.ai.base,
    "登记的参数就是 state.ai.base（实际: "
    + JSON.stringify(bridgeAllowHosts[bridgeAllowHosts.length - 1])
    + " vs " + JSON.stringify(T.state.ai.base) + "）");
  check(bridgeAllowHosts.length >= 1
    && !bridgeAllowHosts[bridgeAllowHosts.length - 1].includes("sk-"),
    "登记给原生的是**地址**，不能带 API key");
  check(T.state.ai.base === "https://my-gateway.example.com/v1",
    "设置照常保存了（state.ai.base = " + T.state.ai.base + "）");

  // ② 原生侧**没有** allowAiHost（老 APK / 浏览器路径）→ 不能抛异常
  const saved = global.MicroBench.allowAiHost;
  delete global.MicroBench.allowAiHost;
  let threw = null;
  try {
    getEl("cfg-base").value = "https://another.example.com/v1";
    await T.saveSettings();
  } catch (e) { threw = e; }
  check(!threw, "原生侧没有 allowAiHost 时保存**不会抛异常**（"
    + (threw ? String(threw && threw.message) : "") + "）");
  check(T.state.ai.base === "https://another.example.com/v1",
    "原生侧没有 allowAiHost 时设置照常保存");
  global.MicroBench.allowAiHost = saved;
}

// ------------------------------------------------------------ 额外：脱敏
function checkSafeErr() {  const s = T.safeErr("Authorization: Bearer abcdefghijklmnopqrst");
  check(s.indexOf("abcdefghijklmnopqrst") < 0,
    "Bearer 令牌整个被抹掉（实际: " + s + "）");
  const j = T.safeErr('{"api_key":"sk-abcdefghijkl"}');
  check(j.indexOf("abcdefghijkl") < 0, "JSON 形态的 api_key 仍然被抹掉（" + j + "）");
  const u = T.safeErr("https://user:pass@example.com/v1");
  check(u.indexOf("pass") < 0, "URL userinfo 仍然被抹掉（" + u + "）");
}

const GROUPS = {
  native429: checkNative429,
  anno: checkAnno,
  fetchtimeout: checkFetchTimeout,
  resetall: checkResetAll,
  throttle: checkThrottle,
  nav: checkNav,
  safeerr: checkSafeErr,
  allowhost: checkAllowHost,
};

(async function main() {
  const fn = GROUPS[group];
  if (!fn) { console.error("FAIL: 未知分组 " + group); process.exit(1); }
  try {
    await fn();
  } catch (e) {
    fail++;
    console.log("  FAIL  [" + group + "] 抛异常: " + (e && e.stack ? e.stack : e));
  }
  console.log("PASS " + pass + "  FAIL " + fail);
  process.exit(fail === 0 ? 0 : 1);
})();
"""


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("没装 node —— 平板页面侧的行为级护栏跳过")
    return exe


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("tablet_audit") / "_test_audit_2026_10_27_tablet.js"
    p.write_text(HARNESS, encoding="utf-8")
    return p


def _run_group(group: str, harness: Path) -> str:
    node = _node()
    env = dict(os.environ)
    env["MB_APP"] = str(APP_JS)
    proc = subprocess.run(
        [node, str(harness), group],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=str(REPO), env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"平板行为级护栏失败（{group}）:\n"
        f"  退出码: {proc.returncode}\n"
        + "\n".join("  " + ln for ln in out.strip().splitlines()[-30:])
    )
    assert "FAIL 0" in out, f"{group} 没有报 FAIL 0（脚本本身可能坏了）:\n{out[-800:]}"
    return out


# ---------------------------------------------------------------------------
# 行为级护栏
# ---------------------------------------------------------------------------

def test_behaviour_native_429_switches_model(harness: Path):
    """① 桥回 429 → Error 带 status → **备用模型真的被调了 2 次**。"""
    out = _run_group("native429", harness)
    assert "备用模型" in out or "429" in out


def test_behaviour_annotation_tombstone(harness: Path):
    """② 云端有、本地没有 → 合并后本地也没有（墓碑生效）。"""
    _run_group("anno", harness)


def test_behaviour_fetch_timeout_aborts(harness: Path):
    """⑥ 超时会 abort。"""
    _run_group("fetchtimeout", harness)


def test_behaviour_reset_all_clears_three_stores(harness: Path):
    """④ 三个 store 都被清。"""
    _run_group("resetall", harness)


def test_behaviour_streaming_is_throttled(harness: Path):
    """③ 流式渲染被节流、公式走缓存、最终内容不变。"""
    _run_group("throttle", harness)


def test_behaviour_nav_is_incremental(harness: Path):
    """⑧ 目录重建复用节点。"""
    _run_group("nav", harness)


def test_behaviour_safe_err_masks_bearer(harness: Path):
    """额外小修：Bearer 令牌整体脱敏。"""
    _run_group("safeerr", harness)


def test_behaviour_saving_settings_registers_ai_host(harness: Path):
    """⑨ 保存设置时把网关地址登记给原生白名单（自定义网关否则会被拒）。"""
    _run_group("allowhost", harness)


# ---------------------------------------------------------------------------
# ⑨ 自定义网关 → 原生主机白名单（配合 Android 原生层的审计 C-1 修复）
# ---------------------------------------------------------------------------

def test_save_settings_registers_ai_host(src: str):
    """`saveSettings()` 必须调用 `nativeBridge.allowAiHost(state.ai.base)`。

    ⚠ 原生的 httpPost 有目标主机白名单（防开放代理），
      而 AI 网关地址存在网页 IndexedDB 里、原生读不到 →
      用户的**自定义网关**不登记就会被拒（报「网关域名不在白名单」）。
    """
    fn = src[src.index("async function saveSettings("):][:900]
    assert "allowAiHost" in fn, "saveSettings 没有登记网关白名单"
    assert "state.ai.base" in fn, "登记用的不是 state.ai.base"
    # ⚠ 绝不能把 API key 一起传过去
    assert "state.ai.key" not in fn.split("allowAiHost")[1][:80], \
        "登记给原生的参数里带着 API key"


def test_ai_host_is_also_registered_on_startup(src: str):
    """启动/恢复设置时也要登记一次（升级 APK 后不用重新保存设置）。"""
    boot = src[src.index('const ai = await DB.getMeta("ai", null);'):][:600]
    assert "allowAiHost" in boot, \
        "恢复设置后没有登记网关白名单（升级后用户得重新保存设置才能用）"
    assert "state.ai.base" in boot, "登记用的不是 state.ai.base"


def test_allow_ai_host_is_forgiving_when_missing(src: str):
    """原生侧没有 allowAiHost（老 APK / 浏览器路径）时不能抛异常。"""
    fn = src[src.index("function allowAiHost("):][:900]
    assert "typeof nativeBridge.allowAiHost" in fn, \
        "没有先判断原生侧有没有这个方法"
    assert "try" in fn and "catch" in fn, "登记失败没有兜底（会挡住保存设置）"
