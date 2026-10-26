/*
 * Unit tests for 专注模式（Zen / Focus）+ PDF 画布重绘链.
 *
 * 目标：屏幕上只留「论文页 + 译文页」，其余界面默认收起；
 *      工具条（= 原有 .reader-topbar）需要时从顶部滑出。
 *
 * 这些函数住在 index.html 里，所以本文件把它们从**发布出去的那份源码**里切出来，
 * 在 node 里用最小 DOM 桩 eval —— 测的是真代码，不是抄一份。
 *
 * ⚠️ 切片范围**必须覆盖被测逻辑本身**：
 *    2026-10-27 独立审查抓到 —— 第一版只切了专注模式块，
 *    把 PDF 重绘链（_pdfRelayoutIfNeeded / _pdfIsRelayoutBusy /
 *    _pdfVerifyRenderedWidth）用桩替换掉了，
 *    结果 3 个「改坏被测逻辑」的变异体在两套护栏下**全绿**。
 *    现在切片从 `let _zenMode = false;` 一直到 `loadPdfDocument` 之前，
 *    重绘链是**真代码**在跑。
 *
 * ⚠️ 自带「变异自检」：把源码改坏（改类名 / 改阈值 / 删守卫）后重跑同一套断言，
 *    必须**出现失败**。否则这套断言就是恒真的、等于没写。
 *
 * Run:  node microbench/_test_zen_mode.js
 *       node microbench/_test_zen_mode.js <另一个 index.html>
 */
const fs = require("fs");
const path = require("path");

const TARGET = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, "static", "index.html");
// 归一化行尾：仓库约定 LF，但 Windows 上的脚本可能写成 CRLF，
// 而下面按标记找边界 —— CRLF 会让它找不到。
const SRC = fs.readFileSync(TARGET, "utf8").replace(/\r\n/g, "\n");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// 最小 DOM 桩
// ---------------------------------------------------------------------------
function makeClassList(el) {
  const parts = () => el.className.split(/\s+/).filter(Boolean);
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
    clientWidth: 0,
    _listeners: {},
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    removeEventListener() {},
    closest() { return null; },
    contains() { return false; },
    dispatchEvent() { return true; },
    appendChild(c) { return c; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  el.classList = makeClassList(el);
  return el;
}

function makeEnv() {
  const elements = {
    "reader-modal": makeEl("reader-modal"),
    "reader-title": makeEl("reader-title", "div"),
    "zen-handle": makeEl("zen-handle"),
    "zen-handle-title": makeEl("zen-handle-title", "span"),
    "zen-single-btn": makeEl("zen-single-btn", "button"),
    // PDF 列表容器：重绘链量的就是它的 clientWidth
    "pdf-pages-list": makeEl("pdf-pages-list"),
  };
  elements["reader-title"].textContent = "一篇论文的标题";
  elements["reader-modal"].style.display = "flex";

  const docListeners = {};
  const document = {
    getElementById(id) { return elements[id] || null; },
    addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener() {},
    _fire(t, ev) { (docListeners[t] || []).forEach((fn) => fn(ev)); },
    _listeners: docListeners,
  };

  const windowEvents = [];
  const window = {
    dispatchEvent(ev) { windowEvents.push(ev && ev.type); return true; },
    addEventListener(t, fn) { (window._listeners[t] = window._listeners[t] || []).push(fn); },
    _listeners: {},
    _events: windowEvents,
  };

  const persisted = [];
  const localStorage = {
    getItem() { return null; },
    setItem(k, v) { persisted.push([k, v]); },
  };

  const toasts = [];
  const env = {
    elements, document, window, localStorage, toasts, persisted,
    switchedModes: [],      // switchReaderMode 被调用过什么
    relayouts: [],          // _pdfRelayoutIfNeeded(force) 的调用记录
    pdfReloads: [],         // loadPdfDualColumn 被调用过什么（= 真的重绘了）
    showToast(msg) { toasts.push(String(msg)); },
    _persistLocalState() {},
    _persistReaderState() {},
  };
  // ⚠️ 用箭头函数闭包，别用对象方法 —— 桩里这两个回调是被裸调用的
  //    （__onSwitchMode(m)），对象方法会丢 this。
  env.__onSwitchMode = (m) => { env.switchedModes.push(m); };
  env.__onRelayout = (force) => { env.relayouts.push(force); };
  env.__onLoadPdf = (opts) => { env.pdfReloads.push(opts); };
  return env;
}

// ---------------------------------------------------------------------------
// 从 index.html 切出「专注模式 + PDF 重绘链」代码块
// ---------------------------------------------------------------------------
function sliceFocusBlock(src) {
  const startMarker = "    let _zenMode = false;";
  const endMarker = "    async function loadPdfDocument(";
  const start = src.indexOf(startMarker);
  if (start < 0) throw new Error("找不到专注模式状态变量（let _zenMode = false;）");
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error("找不到 loadPdfDocument（切片终点）");
  return src.slice(start, end);
}

function buildApi(focusSrc, env) {
  // 切片之外、但它会引用到的外部符号 —— 必须在桩里补上。
  // ⚠️ **PDF 重绘链本身不在这里** —— 它在切片里，是真代码在跑。
  const prelude =
    "let _readerMode = 'section';\n" +
    "let _readerState = { sections: [{ section: 's', title: 't', text: 'x' }] };\n" +
    "let _pdfState = null;\n" +
    "const __observers = [];\n" +
    "class ResizeObserver { constructor(cb){ this.cb = cb; __observers.push(this); } " +
    "observe(el){ this.el = el; } unobserve(){} disconnect(){} }\n" +
    "function switchReaderMode(m){ _readerMode = m; if (typeof __onSwitchMode === 'function') __onSwitchMode(m); }\n" +
    "function loadPdfDualColumn(opts){ if (typeof __onLoadPdf === 'function') __onLoadPdf(opts); }\n";
  const factory = new Function(
    "document", "window", "localStorage", "showToast",
    "_persistLocalState", "_persistReaderState",
    "__onSwitchMode", "__onRelayout", "__onLoadPdf",
    prelude + focusSrc +
      "\nreturn {" +
      "  enterZenMode, exitZenMode, toggleZenMode," +
      "  _zenOpenBar, _zenCloseBar, _zenSyncLabels," +
      "  toggleZenNav, toggleZenAux, toggleZenSingle, zenTogglePane," +
      "  isZenMode, _zenActive, _initZenMode," +
      "  _pdfRelayoutIfNeeded, _pdfVerifyRenderedWidth, _initPdfWidthObserver," +
      "  getMode: () => _zenMode, getPref: () => _zenPref," +
      "  setPref: (v) => { _zenPref = !!v; }," +
      "  getReaderMode: () => _readerMode," +
      "  setReaderMode: (v) => { _readerMode = v; }," +
      "  setSections: (v) => { _readerState.sections = v; }," +
      "  setPdfState: (v) => { _pdfState = v; }," +
      "  getPdfState: () => _pdfState," +
      "  setBusy: (n) => { _pdfRelayoutBusyCount = n; }," +
      "  observers: __observers," +
      "};",
  );
  return factory(
    env.document, env.window, env.localStorage, env.showToast,
    env._persistLocalState, env._persistReaderState,
    env.__onSwitchMode, env.__onRelayout, env.__onLoadPdf,
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
    throw new Error((msg || "值不对") + "：期望 " + JSON.stringify(expected) + "，实际 " + JSON.stringify(actual));
  }
}

/** 造一个「PDF 已加载、栏宽已知」的读者环境。 */
function makePdfEnv(api, env, listWidth, lastRenderWidth) {
  api.setReaderMode("pdf");
  api.setPdfState({
    pdfDoc: { numPages: 1 },
    lastRenderWidth: lastRenderWidth,
    rendering: { generation: 0, cancelled: false },
  });
  env.elements["pdf-pages-list"].clientWidth = listWidth;
  env.pdfReloads.length = 0;
  return env;
}

async function runSuite(focusSrc, label) {
  log("\n=== " + label + " ===");
  const env = makeEnv();
  const api = buildApi(focusSrc, env);
  const modal = env.elements["reader-modal"];

  // ------------------------------------------------------------------
  // 专注模式状态机
  // ------------------------------------------------------------------
  await check("默认不是专注模式（不影响现有用户）", () => {
    assertEq(api.getMode(), false, "初始 _zenMode");
    assertEq(api.getPref(), false, "初始 _zenPref");
    assert(!modal.classList.contains("zen"), "初始不该有 zen 类");
  });

  await check("进入专注：加 zen 类 + 打开状态位 + 记住偏好", () => {
    api.enterZenMode();
    assert(modal.classList.contains("zen"), "缺 zen 类");
    assertEq(api.getMode(), true, "_zenMode 应为 true");
    assertEq(api.getPref(), true, "_zenPref 应为 true");
    assertEq(api.isZenMode(), true, "isZenMode() 应为 true");
  });

  await check("进入专注会给提示（告诉用户怎么退出）", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode();
    assertEq(env2.toasts.length, 1, "应弹一条提示");
    assert(/Esc/.test(env2.toasts[0]), "提示里应说明怎么退出：" + env2.toasts[0]);
  });

  await check("静默进入不弹提示（自动恢复偏好时用）", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode({ silent: true });
    assertEq(env2.toasts.length, 0, "silent 时不该弹提示");
  });

  await check("退出专注：所有 zen 类都清掉 + 状态位归零", () => {
    api.enterZenMode();
    api.toggleZenNav();
    api.toggleZenAux();
    api.toggleZenSingle();
    api.zenTogglePane();
    api.exitZenMode();
    ["zen", "zen-bar-open", "zen-single", "zen-show-orig", "zen-nav-open", "zen-aux-open"]
      .forEach((c) => assert(!modal.classList.contains(c), "退出后还残留 " + c));
    assertEq(api.getMode(), false, "_zenMode");
    assertEq(api.getPref(), false, "_zenPref");
  });

  await check("toggleZenMode 来回切", () => {
    api.toggleZenMode();
    assert(modal.classList.contains("zen"), "第一次切应进入专注");
    api.toggleZenMode();
    assert(!modal.classList.contains("zen"), "第二次切应退出专注");
  });

  await check("工具条：滑出 / 收起", () => {
    api.enterZenMode();
    api._zenOpenBar(true);
    assert(modal.classList.contains("zen-bar-open"), "工具条没滑出");
    api._zenCloseBar();
    assert(!modal.classList.contains("zen-bar-open"), "工具条没收起");
  });

  await check("工具条：非专注状态下调用是空操作", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2._zenOpenBar(true);
    assert(!env2.elements["reader-modal"].classList.contains("zen-bar-open"),
      "没进专注就不该有工具条");
  });

  await check("工具条：滑动触发会挂一个自动收起的定时器", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode();
    const calls = [];
    const realSetTimeout = global.setTimeout;
    global.setTimeout = (fn, ms) => { calls.push(ms); return 0; };
    try {
      api2._zenOpenBar(false);
    } finally {
      global.setTimeout = realSetTimeout;
    }
    assert(calls.some((ms) => ms === 2600),
      "应有 2600ms 自动收起定时器，实际 " + JSON.stringify(calls));
  });

  await check("章节导航 / AI 提问区：各自独立开关", () => {
    api.enterZenMode();
    api.toggleZenNav();
    assert(modal.classList.contains("zen-nav-open"), "章节导航没打开");
    assert(!modal.classList.contains("zen-aux-open"), "提问区不该跟着开");
    api.toggleZenAux();
    assert(modal.classList.contains("zen-aux-open"), "提问区没打开");
    api.toggleZenNav();
    assert(!modal.classList.contains("zen-nav-open"), "章节导航没关上");
    assert(modal.classList.contains("zen-aux-open"), "提问区不该被误关");
  });

  await check("单栏模式：按钮文案跟着变（单栏 ↔ 双栏）", () => {
    api.enterZenMode();
    const btn = env.elements["zen-single-btn"];
    api._zenSyncLabels();
    assertEq(btn.textContent, "单栏", "双栏时按钮应显示「单栏」");
    api.toggleZenSingle();
    assert(modal.classList.contains("zen-single"), "没进单栏");
    assertEq(btn.textContent, "双栏", "单栏时按钮应显示「双栏」");
  });

  await check("单栏模式：论文页 ⇄ 译文页 互斥", () => {
    api.enterZenMode();
    api.toggleZenSingle();
    api.zenTogglePane();
    assert(modal.classList.contains("zen-show-orig"), "没切到论文页");
    api.zenTogglePane();
    assert(!modal.classList.contains("zen-show-orig"), "没切回译文页");
  });

  await check("「章节」在 PDF 模式下要先切到章节视图再展开导航", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.setReaderMode("pdf");
    api2.enterZenMode();
    env2.switchedModes.length = 0;
    api2.toggleZenNav();
    assert(env2.switchedModes.includes("section"),
      "PDF 模式下点「章节」应先切到章节视图，实际 " + JSON.stringify(env2.switchedModes));
    assert(env2.elements["reader-modal"].classList.contains("zen-nav-open"),
      "切完视图后章节导航应打开");
    assert(env2.toasts.length > 0, "应该提示用户为什么换了视图");
  });

  await check("「提问」在 PDF 模式下要先切到章节视图再展开提问区", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.setReaderMode("pdf");
    api2.enterZenMode();
    env2.switchedModes.length = 0;
    api2.toggleZenAux();
    assert(env2.switchedModes.includes("section"),
      "PDF 模式下点「提问」应先切到章节视图，实际 " + JSON.stringify(env2.switchedModes));
    assert(env2.elements["reader-modal"].classList.contains("zen-aux-open"),
      "切完视图后提问区应打开");
  });

  await check("章节视图里点「章节」只切换导航显隐，不切视图", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.setReaderMode("section");
    api2.enterZenMode();
    env2.switchedModes.length = 0;
    api2.toggleZenNav();
    assert(env2.switchedModes.length === 0,
      "已经是章节视图了，不该再切视图，实际 " + JSON.stringify(env2.switchedModes));
    assert(env2.elements["reader-modal"].classList.contains("zen-nav-open"),
      "章节导航应打开");
    api2.toggleZenNav();
    assert(!env2.elements["reader-modal"].classList.contains("zen-nav-open"),
      "再点一次章节导航应关闭");
  });

  await check("没有章节数据时不切章节视图，只提示（别给用户一片空白）", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.setSections([]);
    api2.setReaderMode("pdf");
    api2.enterZenMode();
    env2.switchedModes.length = 0;
    env2.toasts.length = 0;
    api2.toggleZenNav();
    assert(env2.switchedModes.length === 0,
      "没有章节数据就不该切视图，实际 " + JSON.stringify(env2.switchedModes));
    assert(env2.toasts.length > 0, "应该提示用户为什么没切视图");
    assert(!env2.elements["reader-modal"].classList.contains("zen-nav-open"),
      "没切视图就不该动章节导航的开关");
  });

  await check("没有章节数据时，章节视图里点「章节」也不开空列", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.setSections([]);
    api2.setReaderMode("section");
    api2.enterZenMode();
    env2.toasts.length = 0;
    api2.toggleZenNav();
    assert(!env2.elements["reader-modal"].classList.contains("zen-nav-open"),
      "没有章节数据时不该开出一条 200px 的空导航列");
    assert(env2.toasts.length > 0, "应该提示为什么没开");
  });

  await check("阅读器关着时不算活跃（首页不该被这些监听器影响）", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode();
    env2.elements["reader-modal"].style.display = "none";
    assertEq(api2._zenActive(), false, "阅读器关着时不该算活跃");
  });

  await check("_initZenMode 能跑通（监听器接线不抛异常）", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2._initZenMode();
    assert(Object.keys(env2.document._listeners).length > 0, "没注册任何监听器");
  });

  await check("Z 快捷键不会在输入框里误触发", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode();
    api2._initZenMode();
    const kb = env2.document._listeners["keydown"] || [];
    assert(kb.length > 0, "没注册 keydown 监听");
    const before = api2.getMode();
    kb.forEach((fn) => fn({
      key: "z", target: { tagName: "TEXTAREA", isContentEditable: false },
      preventDefault() {}, ctrlKey: false, metaKey: false, altKey: false,
    }));
    assertEq(api2.getMode(), before, "在 textarea 里按 z 不该切换专注模式");
  });

  await check("Z 快捷键在正文里能切换专注", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode();
    api2._initZenMode();
    const kb = env2.document._listeners["keydown"] || [];
    const before = api2.getMode();
    kb.forEach((fn) => fn({
      key: "z", target: { tagName: "DIV", isContentEditable: false },
      preventDefault() {}, ctrlKey: false, metaKey: false, altKey: false,
    }));
    assert(api2.getMode() !== before, "正文里按 z 应切换专注模式");
  });

  await check("Esc：先收工具条，再按才退出专注", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode();
    api2._initZenMode();
    const kb = env2.document._listeners["keydown"] || [];
    const modal2 = env2.elements["reader-modal"];
    api2._zenOpenBar(true);
    kb.forEach((fn) => fn({ key: "Escape", target: { tagName: "DIV" }, preventDefault() {} }));
    assert(!modal2.classList.contains("zen-bar-open"), "Esc 应先收起工具条");
    assertEq(api2.getMode(), true, "第一次 Esc 不该退出专注");
    kb.forEach((fn) => fn({ key: "Escape", target: { tagName: "DIV" }, preventDefault() {} }));
    assertEq(api2.getMode(), false, "第二次 Esc 应退出专注");
  });

  await check("Esc：上层弹窗开着时让路（不退出专注）", () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    api2.enterZenMode();
    api2._initZenMode();
    // 模拟「论文快照弹窗」开着
    env2.elements["snapshot-modal"] = makeEl("snapshot-modal");
    env2.elements["snapshot-modal"].style.display = "flex";
    const kb = env2.document._listeners["keydown"] || [];
    kb.forEach((fn) => fn({ key: "Escape", target: { tagName: "DIV" }, preventDefault() {} }));
    assertEq(api2.getMode(), true,
      "快照/诊断弹窗开着时，Esc 归它 —— 不能一次按键干两件事");
  });

  // ------------------------------------------------------------------
  // PDF 重绘链（真代码在跑，不是桩）
  // ------------------------------------------------------------------
  await check("PDF 重绘：栏宽变化 < 40px 且没 force → 不重绘", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 610, 600);   // 只差 10px
    api2._pdfRelayoutIfNeeded(false);
    await sleep(330);
    assertEq(env2.pdfReloads.length, 0, "变化 < 40px 不该全量重绘");
  });

  await check("PDF 重绘：栏宽变化 ≥ 40px → 重绘", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 1200, 600);
    api2._pdfRelayoutIfNeeded(false);
    await sleep(330);
    assertEq(env2.pdfReloads.length, 1, "栏宽大变就该重绘");
    assertEq(env2.pdfReloads[0].keepTranslations, true, "重绘要保留已有译文");
  });

  await check("PDF 重绘：force=true 时无视 40px 阈值", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 610, 600);   // 只差 10px
    api2._pdfRelayoutIfNeeded(true);
    await sleep(330);
    assertEq(env2.pdfReloads.length, 1,
      "force=true 必须绕过「变化太小不值得重绘」的省算逻辑");
  });

  await check("PDF 重绘：忙标记期间不排重绘（防自激循环）", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 1200, 600);
    api2.setBusy(1);
    api2._pdfRelayoutIfNeeded(true);
    await sleep(330);
    assertEq(env2.pdfReloads.length, 0, "重绘进行中不该再排一次（会自激循环）");
    api2.setBusy(0);
  });

  await check("PDF 重绘：容器宽度为 0（阅读器还没显示）→ 不重绘", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 0, 600);
    api2._pdfRelayoutIfNeeded(true);
    await sleep(330);
    assertEq(env2.pdfReloads.length, 0, "量不到宽度时不该重绘（宽度不可信）");
  });

  await check("PDF 重绘：渲染中才变可见 → 渲染结束后补一次重绘", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 1200, 600);
    // 渲染开始时宽度是 0（阅读器还没显示），结束时变成 1200
    api2._pdfVerifyRenderedWidth(0, env2.elements["pdf-pages-list"]);
    await sleep(330);
    assertEq(env2.pdfReloads.length, 1,
      "渲染中途阅读器才变可见 → 渲染结束后必须补一次重绘（否则画布停在 520px）");
  });

  await check("PDF 重绘：渲染前后宽度没变 → 不补重绘", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 1200, 1200);
    api2._pdfVerifyRenderedWidth(1200, env2.elements["pdf-pages-list"]);
    await sleep(330);
    assertEq(env2.pdfReloads.length, 0, "宽度没变就不该补重绘");
  });

  await check("PDF 重绘：ResizeObserver 回调走同一套判断", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    makePdfEnv(api2, env2, 1200, 600);
    api2._initPdfWidthObserver();
    const obs = api2.observers;
    assert(obs.length > 0, "没注册 ResizeObserver");
    // 模拟容器尺寸变化
    obs.forEach((o) => o.cb && o.cb([], o));
    await sleep(330);
    assertEq(env2.pdfReloads.length, 1, "容器宽度变了就该重绘");
  });

  await check("进出专注不做全量重绘（栏宽没变），切单栏才 force", async () => {
    const env2 = makeEnv();
    const api2 = buildApi(focusSrc, env2);
    // 只差 10px → 只有 force=true 才会绕过 40px 阈值触发重绘
    makePdfEnv(api2, env2, 610, 600);
    api2.enterZenMode();
    await sleep(340);
    assertEq(env2.pdfReloads.length, 0,
      "进出专注不改变 PDF 栏宽 → 不该 force 全量重绘" +
      "（391 页的论文每点一次「专注」就全量重绘一遍 = 明显卡顿）");
    api2.toggleZenSingle();
    await sleep(700);   // 切单栏 300ms 延迟 + 重绘定时器 250ms
    assertEq(env2.pdfReloads.length, 1,
      "切单栏改变栏宽 → 必须 force 重绘");
  });

  return { passed, failed };
}

// ---------------------------------------------------------------------------
// 变异自检：把源码改坏，同一套断言必须**出现失败**。
// 没有这一段，上面那些断言可能全是恒真的（等于没写）。
//
// ⚠️ 变异必须覆盖「被调方」—— 2026-10-27 独立审查抓到：
//    第一版的 7 个变异**全都只改调用方**，最该被守的重绘链一个没碰，
//    结果 3 个改坏被测逻辑的变异在两套护栏下全绿。
// ---------------------------------------------------------------------------
const MUTATIONS = [
  { name: "把类名 zen 改坏", from: 'classList.add("zen")', to: 'classList.add("zenBOGUS")' },
  { name: "把类名 zen-bar-open 改坏", from: 'classList.add("zen-bar-open")', to: 'classList.add("zen-bar-open-BOGUS")' },
  { name: "把类名 zen-single 改坏", from: 'classList.toggle("zen-single")', to: 'classList.toggle("zen-single-BOGUS")' },
  { name: "把退出时的清理去掉", from: 'modal.classList.remove("zen", "zen-bar-open", "zen-single",', to: 'modal.classList.remove("zzz-nothing", "zen-bar-open", "zen-single",' },
  { name: "把快捷键跳过输入框的判断去掉", from: 'if (tag === "input" || tag === "textarea" || tag === "select" ||', to: 'if (false ||' },
  { name: "把「章节」按钮的先切视图去掉", from: 'switchReaderMode("section");', to: '/* 不切视图 */' },
  // ↓↓↓ 下面三个是「被调方」变异 —— 审查点名说这几个原来没被守
  { name: "把重绘的 force 短路去掉（!force → 无条件）", from: 'if (!force && last && Math.abs(w - last) < 40) return;', to: 'if (last && Math.abs(w - last) < 40) return;' },
  { name: "把重绘的忙标记守卫去掉（自激循环保护消失）", from: 'if (_pdfIsRelayoutBusy()) return;', to: '/* no busy guard */;' },
  { name: "把渲染后的宽度复核去掉（画布停在兜底宽度）", from: 'if (Math.abs(renderW0 - el.clientWidth) > 40) _pdfRelayoutIfNeeded(true);', to: 'if (false) _pdfRelayoutIfNeeded(true);' },
  { name: "把 40px 阈值改成 0（该跳过的也重绘）", from: 'Math.abs(w - last) < 40', to: 'Math.abs(w - last) < 0' },
  { name: "把切单栏的 force 去掉（栏宽变了不重绘）", from: '_zenRelayoutPdf(true); }, 300);', to: '_zenRelayoutPdf(false); }, 300);' },
];

async function main() {
  console.log("被测文件: " + TARGET);
  const focusSrc = sliceFocusBlock(SRC);
  const result = await runSuite(focusSrc, "专注模式 + PDF 重绘链");

  console.log("\n=== 变异自检（断言必须能抓到回归）===");
  let mutationCaught = 0;
  for (const m of MUTATIONS) {
    if (!focusSrc.includes(m.from)) {
      console.log("  FAIL  变异 [" + m.name + "] 没匹配到源码 —— 变异自检本身失效");
      result.failed += 1;
      continue;
    }
    const mutated = focusSrc.replace(m.from, m.to);
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
