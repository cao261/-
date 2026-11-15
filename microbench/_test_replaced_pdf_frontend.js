/*
 * 「中译替换版 PDF」前端行为级护栏（node 跑 index.html 里的**真实源码**）。
 *
 * 为什么要有这个文件：
 *   2026-11-15 之前，前端的四条规则是用「读 index.html 源码文本」守的
 *   （assert 'xxx' in src）—— 那类断言改坏行为也照样绿：
 *     把 onclick="buildReplacedPdf()" 删掉、把 opts.replaced 丢掉、
 *     把 pdf-split-single 的类名改错、把「替换版不翻一遍」的刹车删掉，
 *   源码里该有的字符串还在 → 全绿。
 *   → 元护栏 tests/test_guard_efficacy.py 把这类断言钉死不许再涨。
 *
 * 这里改成**行为级**：把 index.html 里那几段真代码切出来，在最小 DOM 桩里
 * 真跑一遍，断言**可观察的后果**（请求发出去了吗 / 右栏收起了吗 / 翻了没翻）。
 *
 * ⚠️ 自带「变异自检」：把 index.html 改坏（HTML 或 JS 都算）后重跑同一套断言，
 *    必须**出现失败**。没有这一段，上面那些断言可能全是恒真的、等于没写。
 *
 * Run:  node microbench/_test_replaced_pdf_frontend.js
 *       node microbench/_test_replaced_pdf_frontend.js <另一个 index.html>
 */
const fs = require("fs");
const path = require("path");

const TARGET = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, "static", "index.html");
const SRC = fs.readFileSync(TARGET, "utf8").replace(/\r\n/g, "\n");
const NL = String.fromCharCode(10);

// ---------------------------------------------------------------------------
// 从 index.html 切出要测的函数（按函数头 + 花括号配对）
// ---------------------------------------------------------------------------
function sliceFn(src, marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error("找不到函数：" + marker);
  const open = src.indexOf("{" + String.fromCharCode(10), start);
  if (open < 0) throw new Error("函数头后面没有花括号：" + marker);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === "{") depth += 1;
    else if (c === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error("函数没闭合：" + marker);
}

function sliceAll(src) {
  return [
    sliceFn(src, "function _isReplacedPdfPath(p) {"),
    sliceFn(src, "async function openPdfDirect(relativePath, title, opts = {}) {"),
    sliceFn(src, "function _applyReplacedPdfView(on) {"),
    sliceFn(src, "async function buildReplacedPdf() {"),
    sliceFn(src, "async function processNextInPdfQueue() {"),
  ].join("\n\n");
}

// ---------------------------------------------------------------------------
// 最小 DOM 桩
// ---------------------------------------------------------------------------
function makeClassList(el) {
  const parts = () => String(el.className || "").split(/\s+/).filter(Boolean);
  return {
    add(...cs) {
      const p = parts();
      for (const c of cs) if (!p.includes(c)) p.push(c);
      el.className = p.join(" ");
    },
    remove(...cs) {
      el.className = parts().filter((x) => !cs.includes(x)).join(" ");
    },
    contains(c) { return parts().includes(c); },
    toggle(c, force) {
      const has = this.contains(c);
      const want = force === undefined ? !has : !!force;
      if (want) this.add(c); else this.remove(c);
      return want;
    },
  };
}

function makeEl(id, tag) {
  const el = {
    id,
    tagName: String(tag || "div").toUpperCase(),
    className: "",
    style: {},
    textContent: "",
    dataset: {},
    disabled: false,
  };
  el.classList = makeClassList(el);
  return el;
}

function makeEnv() {
  const elements = {
    "pdf-translations-container": makeEl("pdf-translations-container"),
    "pdf-replaced-toggle": makeEl("pdf-replaced-toggle", "button"),
    "pdf-bilingual-toggle": makeEl("pdf-bilingual-toggle", "button"),
    "pdf-bilingual-orig-toggle": makeEl("pdf-bilingual-orig-toggle", "button"),
  };
  // .pdf-split 是两列 grid → 藏掉右栏不等于变宽，必须挂单列类
  const split = makeEl("pdf-split", "div");
  split.className = "pdf-split";

  const document = {
    getElementById(id) { return elements[id] || null; },
    querySelector(sel) {
      return sel === "#reader-pdf-view .pdf-split" ? split : null;
    },
  };

  const toasts = [];
  const fetches = [];
  const loadPdfCalls = [];
  const modeSwitches = [];
  const rendered = [];
  const translated = [];
  const timers = [];

  const env = {
    elements,
    split,
    document,
    toasts,
    fetches,
    loadPdfCalls,
    modeSwitches,
    rendered,
    translated,
    timers,
    showToast(msg, isErr, dur) {
      toasts.push({ msg: String(msg), isErr: !!isErr, dur: dur });
    },
    fetch(url, init) {
      fetches.push({ url: String(url), init: init || null });
      return env._fetchImpl(url, init);
    },
    renderReader() { rendered.push(true); },
    openReaderModal() {},
    switchReaderMode(mode, opts) { modeSwitches.push({ mode, opts }); },
    loadPdfDualColumn(opts) { loadPdfCalls.push(opts || {}); },
    translatePdfPage(num) { translated.push({ kind: "page", num }); },
    translateBlocksForPage(num) { translated.push({ kind: "blocks", num }); },
    _showBilingualVisibility() {},
    _fetchImpl: async () => ({ ok: true, status: 200 }),
  };
  return env;
}

// ---------------------------------------------------------------------------
// 把切出来的真代码装进一个作用域
// ---------------------------------------------------------------------------
function buildApi(src, env) {
  const js = sliceAll(src);
  // 模块级状态（切片之外）：这些是**状态**，不是被测逻辑，必须在桩里声明
  const prelude = `
let _pdfState = null;
let _pdfReplacedView = false;
let _pdfReplacedSource = null;
let _readerState = { title: "", sections: [], translations: {}, translationModels: {},
                     activeSection: 0, pdfPath: "" };
let _pdfQueuePaused = false;
let _pdfChainOwnerPaperId = null;
let _pdfQueueTimer = null;
const setTimeout = (fn, ms) => { __timers.push([fn, ms]); return __timers.length; };
// 桩里把 console 静音：失败路径会 console.error 出整条栈，吵得看不清断言结果
const console = { error: () => {}, warn: () => {}, log: () => {} };
`;
  const factory = new Function(
    "document", "showToast", "fetch", "_readJsonSafe", "_httpErrorMessage",
    "renderReader", "openReaderModal", "switchReaderMode", "loadPdfDualColumn",
    "translatePdfPage", "translateBlocksForPage", "_showBilingualVisibility",
    "__timers",
    prelude + js + `
return {
  _isReplacedPdfPath,
  openPdfDirect,
  _applyReplacedPdfView,
  buildReplacedPdf,
  processNextInPdfQueue,
  setPdfState: (v) => { _pdfState = v; },
  getPdfState: () => _pdfState,
  setReplacedView: (v) => { _pdfReplacedView = !!v; },
  getReplacedView: () => _pdfReplacedView,
  setReplacedSource: (v) => { _pdfReplacedSource = v; },
  getReplacedSource: () => _pdfReplacedSource,
  setReaderTitle: (t) => { _readerState.title = t; },
  getReaderTitle: () => _readerState.title,
  setChainOwner: (v) => { _pdfChainOwnerPaperId = v; },
};
`,
  );
  return factory(
    env.document, env.showToast, env.fetch,
    async (res) => ({ data: res.__data || null, text: res.__text || "" }),
    (data, res, text, fallback) => (data && data.detail) || fallback || "失败",
    env.renderReader, env.openReaderModal, env.switchReaderMode,
    env.loadPdfDualColumn, env.translatePdfPage, env.translateBlocksForPage,
    env._showBilingualVisibility, env.timers,
  );
}

// ---------------------------------------------------------------------------
// 断言
// ---------------------------------------------------------------------------
let passed = 0;
let failed = 0;
let quiet = false;

function log(s) { if (!quiet) console.log(s); }

async function check(name, fn) {
  try {
    await fn();
    passed += 1;
    log("  PASS  " + name);
  } catch (e) {
    failed += 1;
    log("  FAIL  " + name + "  →  " + (e && e.message));
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg || "断言失败");
}

function assertEq(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error((msg || "值不对") + "：期望 " + JSON.stringify(expected) +
                    "，实际 " + JSON.stringify(actual));
  }
}

// ---------------------------------------------------------------------------
// 「🧾 中译替换版」按钮：真的在工具条里，且 onclick 真的接得上
// ---------------------------------------------------------------------------
function buttonTag(src) {
  const m = src.match(/<button[^>]*id="pdf-replaced-toggle"[^>]*>/);
  return m ? m[0] : null;
}

function onclickBody(tag) {
  const m = tag.match(/onclick="([^"]*)"/);
  return m ? m[1] : null;
}

// ---------------------------------------------------------------------------
// 主套件
// ---------------------------------------------------------------------------
async function runSuite(src, label) {
  log("\n=== " + label + " ===");
  const env = makeEnv();
  const api = buildApi(src, env);

  const SRC_REL = "01_Literature/04_Survey_综述/01_论文原文_PDF/某论文.pdf";
  const OUT_REL = "01_Literature/04_Survey_综述/05_译文替换版_PDF/某论文_中译替换版.pdf";
  const OK_DATA = {
    out_relative_path: OUT_REL,
    pages_total: 8, pages_replaced: 4,
    blocks_written: 68, blocks_unfit: 0,
  };

  function armPdfState() {
    api.setPdfState({
      paperId: "paper_abc123",
      relativePath: SRC_REL,
      pages: [{ num: 1, text: "x", translated: false }],
      rendering: { generation: 0, cancelled: false },
    });
    api.setReaderTitle("某论文");
    api.setChainOwner("paper_abc123");
  }

  async function clickButton() {
    const tag = buttonTag(src);
    assert(tag, "工具条里没有「🧾 中译替换版」按钮（id=pdf-replaced-toggle）");
    const body = onclickBody(tag);
    assert(body, "按钮没有 onclick 属性");
    const fn = new Function("buildReplacedPdf", "return (" + body.trim() + ")");
    return await fn(api.buildReplacedPdf);
  }

  // ---- 按钮接线 ----------------------------------------------------------
  await check("「🧾 中译替换版」按钮存在，且点了会真的发起生成请求", async () => {
    armPdfState();
    env._fetchImpl = async () => ({ ok: true, status: 200, __data: OK_DATA });
    await clickButton();
    assertEq(env.fetches.length, 1, "点一次按钮应发一次请求");
    assertEq(env.fetches[0].url, "/api/paper/build_replaced_pdf", "端点不对");
    assertEq(env.fetches[0].init.method, "POST", "方法不对");
    const body = JSON.parse(env.fetches[0].init.body);
    assertEq(body.relative_path, SRC_REL, "请求体里的源路径不对");
    assertEq(body.paper_id, "paper_abc123", "请求体里的 paper_id 不对");
  });

  await check("没打开 PDF 时点按钮 → 只提示，不发请求", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(src, env2);
    api2.setPdfState(null);
    const tag = buttonTag(src);
    assert(tag, "按钮不见了");
    const body = onclickBody(tag);
    const fn = new Function("buildReplacedPdf", "return (" + body.trim() + ")");
    await fn(api2.buildReplacedPdf);
    assertEq(env2.fetches.length, 0, "没打开 PDF 还去请求了");
    assert(env2.toasts.length === 1 && /先打开一篇 PDF/.test(env2.toasts[0].msg),
      "应提示「先打开一篇 PDF」，实际：" + JSON.stringify(env2.toasts));
  });

  // ---- 生成成功 → 打开替换版 --------------------------------------------
  await check("生成成功 → 打开的是替换版 PDF（replaced=true）", async () => {
    const env3 = makeEnv();
    const api3 = buildApi(src, env3);
    api3.setPdfState({
      paperId: "paper_abc123", relativePath: SRC_REL,
      pages: [], rendering: { generation: 0, cancelled: false },
    });
    api3.setReaderTitle("某论文");
    env3._fetchImpl = async () => ({ ok: true, status: 200, __data: OK_DATA });
    const tag = buttonTag(src);
    const fn = new Function("buildReplacedPdf", "return (" + onclickBody(tag).trim() + ")");
    await fn(api3.buildReplacedPdf);
    assertEq(env3.loadPdfCalls.length, 1, "应打开一次 PDF");
    assertEq(env3.loadPdfCalls[0].replaced, true,
      "打开替换版时没带 replaced=true → 右栏不会收起、还会再翻一遍");
    assertEq(api3.getReaderTitle(), "某论文（中译替换版）",
      "标题应带一次「（中译替换版）」");
  });

  await check("连点两次 → 标题不叠成「（中译替换版）（中译替换版）」", async () => {
    const env4 = makeEnv();
    const api4 = buildApi(src, env4);
    api4.setPdfState({
      paperId: "paper_abc123", relativePath: SRC_REL,
      pages: [], rendering: { generation: 0, cancelled: false },
    });
    api4.setReaderTitle("某论文");
    env4._fetchImpl = async () => ({ ok: true, status: 200, __data: OK_DATA });
    const tag = buttonTag(src);
    const fn = new Function("buildReplacedPdf", "return (" + onclickBody(tag).trim() + ")");
    await fn(api4.buildReplacedPdf);
    await fn(api4.buildReplacedPdf);
    assertEq(env4.fetches.length, 2, "应发两次请求");
    const title = api4.getReaderTitle();
    assert(!/（中译替换版）（中译替换版）/.test(title),
      "标题叠了：「中译替换版」只能出现一次，实际：" + title);
    // 第二次的源必须是**原论文**，不是刚生成的替换版
    const body2 = JSON.parse(env4.fetches[1].init.body);
    assertEq(body2.relative_path, SRC_REL,
      "第二次生成用了替换版自己当源 → 会去找「替换版的快照」→ 必然 409");
  });

  await check("生成失败 → 提示失败，且不打开任何 PDF", async () => {
    const env5 = makeEnv();
    const api5 = buildApi(src, env5);
    api5.setPdfState({
      paperId: "paper_abc123", relativePath: SRC_REL,
      pages: [], rendering: { generation: 0, cancelled: false },
    });
    api5.setReaderTitle("某论文");
    env5._fetchImpl = async () => ({
      ok: false, status: 409, __data: { detail: "段落切分算法已升级" }, __text: "",
    });
    const tag = buttonTag(src);
    const fn = new Function("buildReplacedPdf", "return (" + onclickBody(tag).trim() + ")");
    await fn(api5.buildReplacedPdf);
    assertEq(env5.loadPdfCalls.length, 0, "失败时不该打开 PDF");
    assert(env5.toasts.some((t) => /生成失败/.test(t.msg)),
      "应提示「生成失败」，实际：" + JSON.stringify(env5.toasts));
  });

  // ---- 视图：右栏收起 + PDF 独占整栏 ------------------------------------
  await check("替换版视图：右栏收起 + PDF 独占整栏（单列类挂上）", async () => {
    const env6 = makeEnv();
    const api6 = buildApi(src, env6);
    const trans = env6.elements["pdf-translations-container"];
    api6._applyReplacedPdfView(true);
    assertEq(trans.style.display, "none", "右栏（对照译文）没收起");
    assert(env6.split.classList.contains("pdf-split-single"),
      "没挂单列类 → .pdf-split 仍是两列 grid → PDF 只占半屏");
    api6._applyReplacedPdfView(false);
    assertEq(trans.style.display, "", "回到普通 PDF 时右栏该回来");
    assert(!env6.split.classList.contains("pdf-split-single"),
      "回到普通 PDF 时单列类该摘掉");
  });

  // ---- 「是不是替换版」跟着文件走 ---------------------------------------
  await check("替换版判定跟着文件名走（缩放重排后标志不能丢）", async () => {
    const env7 = makeEnv();
    const api7 = buildApi(src, env7);
    assertEq(api7._isReplacedPdfPath("a/b/某论文_中译替换版.pdf"), true, "应判为替换版");
    assertEq(api7._isReplacedPdfPath("a/b/某论文.pdf"), false, "普通 PDF 不该判为替换版");
    assertEq(api7._isReplacedPdfPath("a/b/某论文_中译替换版.PDF"), true, "应忽略大小写");
    assertEq(api7._isReplacedPdfPath(""), false, "空路径不该判为替换版");
    assertEq(api7._isReplacedPdfPath(undefined), false, "undefined 不该判为替换版");
  });

  // ---- 替换版不翻一遍 ---------------------------------------------------
  await check("替换版里翻译队列直接刹车（不再翻一遍）", async () => {
    const env8 = makeEnv();
    const api8 = buildApi(src, env8);
    api8.setPdfState({
      paperId: "paper_abc123", relativePath: SRC_REL,
      pages: [{ num: 1, text: "x", translated: false }],
      rendering: { generation: 0, cancelled: false },
    });
    api8.setChainOwner("paper_abc123");
    api8.setReplacedView(true);
    await api8.processNextInPdfQueue();
    assertEq(env8.translated.length, 0,
      "替换版又去翻了一遍 → 12 页白花钱（译文已经在 PDF 里了）");
  });

  await check("普通 PDF 仍然正常排队翻译（反向用例：刹车不能误伤）", async () => {
    const env9 = makeEnv();
    const api9 = buildApi(src, env9);
    api9.setPdfState({
      paperId: "paper_abc123", relativePath: SRC_REL,
      pages: [{ num: 1, text: "x", translated: false }],
      rendering: { generation: 0, cancelled: false },
    });
    api9.setChainOwner("paper_abc123");
    api9.setReplacedView(false);
    await api9.processNextInPdfQueue();
    assertEq(env9.translated.length, 1, "普通 PDF 没翻 → 主功能被误伤了");
    assertEq(env9.translated[0].kind, "page", "该走整页翻译");
  });

  return { passed, failed };
}

// ---------------------------------------------------------------------------
// 变异自检：把 index.html 改坏，同一套断言必须**出现失败**
// ---------------------------------------------------------------------------
const MUTATIONS = [
  { name: "把按钮的 onclick 接线去掉", from: 'onclick="buildReplacedPdf()"', to: 'onclick="void 0"' },
  { name: "把按钮整个删掉", from: 'id="pdf-replaced-toggle"', to: 'id="pdf-replaced-toggle-BOGUS"' },
  { name: "把请求的端点改坏", from: '"/api/paper/build_replaced_pdf"', to: '"/api/paper/build_replaced_pdf_BOGUS"' },
  { name: "把方法从 POST 改坏", from: 'method: "POST",' + NL + '          headers: { "Content-Type": "application/json" },' + NL + '          body: JSON.stringify({' + NL + '            paper_id: src.paper_id,', to: 'method: "GET",' + NL + '          headers: { "Content-Type": "application/json" },' + NL + '          body: JSON.stringify({' + NL + '            paper_id: src.paper_id,' },
  { name: "把打开时的 replaced 标志丢掉（右栏会冒出来）", from: "{ replaced: true });", to: "});" },
  { name: "把单列类名改坏（PDF 只占半屏）", from: 'classList.toggle("pdf-split-single", !!on)', to: 'classList.toggle("pdf-split-single-BOGUS", !!on)' },
  { name: "把右栏收起这段删掉", from: 'trans.style.display = on ? "none" : "";', to: '/* 不收右栏 */;' },
  { name: "把替换版的翻译刹车删掉（又翻一遍）", from: "if (_pdfReplacedView) return;", to: "/* 不刹车 */;" },
  { name: "把替换版判定改坏（标志跟着文件走这条没了）", from: 'return /_中译替换版\\.pdf$/i.test(p || "");', to: "return false;" },
  { name: "把标题去重去掉（标题会叠）", from: '.replace(/（中译替换版）\\s*$/, "")', to: '.replace(/__nothing__/, "")' },
  { name: "把「先打开一篇 PDF」的提前返回删掉", from: 'showToast("先打开一篇 PDF", true, 3000);', to: '/* 不提示 */;' },
  { name: "把失败提示删掉", from: 'showToast("生成失败: " + (err && err.message ? err.message : err), true, 6000);', to: '/* 静默 */;' },
];

async function main() {
  console.log("被测文件: " + TARGET);
  const result = await runSuite(SRC, "中译替换版 PDF 前端");

  console.log("\n=== 变异自检（断言必须能抓到回归）===");
  let mutationCaught = 0;
  for (const m of MUTATIONS) {
    if (!SRC.includes(m.from)) {
      console.log("  FAIL  变异 [" + m.name + "] 没匹配到源码 —— 变异自检本身失效");
      result.failed += 1;
      continue;
    }
    const mutated = SRC.replace(m.from, m.to);
    const savedPassed = passed;
    const savedFailed = failed;
    passed = 0;
    failed = 0;
    quiet = true;
    try {
      await runSuite(mutated, "mutant");
    } catch (e) {
      failed += 1;
    } finally {
      quiet = false;
    }
    const mutantFailed = failed;
    passed = savedPassed;
    failed = savedFailed;
    if (mutantFailed > 0) {
      mutationCaught += 1;
      console.log("  PASS  变异 [" + m.name + "] 被抓到（" + mutantFailed + " 条失败）");
    } else {
      console.log("  FAIL  变异 [" + m.name + "] 没被抓到 —— 这套断言是恒真的！");
      result.failed += 1;
    }
  }

  console.log("\n============================================================");
  console.log("PASS " + result.passed + "  FAIL " + result.failed +
              "  (变异捕获 " + mutationCaught + "/" + MUTATIONS.length + ")");
  console.log("============================================================");
  process.exit(result.failed === 0 && mutationCaught === MUTATIONS.length ? 0 : 1);
}

main().catch((e) => {
  console.error("护栏自身崩了: " + (e && e.stack));
  process.exit(1);
});
