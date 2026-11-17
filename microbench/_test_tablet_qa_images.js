/*
 * 平板工作台 · 划词提问的图片链路 + 文本层几何 —— 前端**行为级**护栏
 * （node 跑 static/tablet/app.js 的**真源码**）。
 *
 * 为什么要有这个文件（2026-11-17 全面审查 P0-1 / P1）：
 *   ① **P0-1**：平板上「划词 → 问 AI」原来**一张图都不发**
 *      （state.pendingSelection 只存文本），而公式在译文里是**截图**
 *      → 模型看不到公式推导 → 只能猜（用户原话：「完全看不到公式推导，只能猜」）。
 *      桌面端 11-17 修好了（整页图 + 按坐标裁的 scale=4 高清公式图），
 *      平板还停在旧行为 —— 同一个缺陷，平板更严重（连整页图都没有）。
 *   ② **P1**：平板的 PDF 文本层**没设 `--scale-factor`**（桌面 index.html:6004 设了）。
 *      pdf.js 3.11 的文本层 CSS 用 `calc(var(--scale-factor) * W px)` 算 span 尺寸，
 *      而这个变量**必须由调用方设**（pdf.min.js 里只有 var() 的用法，没有 setProperty）。
 *      变量没设 → span 塌成 0 宽 → **文本层与画布错位**
 *      → 平板上划词会选到**错的字**，批注高亮框也落在错位置。
 *
 * ⚠ 这两件事都是「改坏行为也照样绿」的高危点：
 *   把图片从 payload 里删掉、把 --scale-factor 那一行删掉、把版本闸门放宽 ——
 *   源码里该有的字符串还在 → 旧式源码文本护栏全绿。
 *   → 所以这里改成**行为级**：真跑 app.js，断言**可观察的后果**
 *     （请求里到底带没带图、提示词里有没有纪律、文本层几何对不对、
 *       旧版本的段落译文有没有被丢弃）。
 *
 * ⚠⚠ 自带「变异自检」：把 app.js 改坏后重跑同一套断言，必须**出现失败**。
 *    没有这一段，上面那些断言可能全是恒真的、等于没写。
 *
 * Run:  node microbench/_test_tablet_qa_images.js
 *       node microbench/_test_tablet_qa_images.js --suite-only <另一个 app.js>
 */
const fs = require("fs");
const path = require("path");
const os = require("os");
const { spawnSync } = require("child_process");

const SUITE_ONLY = process.argv.includes("--suite-only");
// ⚠ 被测文件是「第一个不是 --suite-only 的参数」——
//   变异自检会传 `--suite-only <临时 app.js>`，只看 argv[2] 会测错文件。
const APP_ARGS = process.argv.slice(2).filter((a) => a !== "--suite-only");
const APP = APP_ARGS.length
  ? path.resolve(APP_ARGS[0])
  : path.join(__dirname, "static", "tablet", "app.js");

// ---------------------------------------------------------------------------
// DOM shim（比 _test_tablet_ai.js 那个多一点：style 要支持 setProperty）
// ---------------------------------------------------------------------------
function mkStyle() {
  const s = {};
  s._props = {};
  s.setProperty = function (k, v) { s._props[k] = String(v); s[k] = String(v); };
  s.getPropertyValue = function (k) { return s._props[k] == null ? "" : s._props[k]; };
  s.removeProperty = function (k) { delete s._props[k]; delete s[k]; };
  return s;
}

function mkEl(tag) {
  const node = {
    tagName: String(tag || "").toUpperCase(),
    className: "",
    _text: "",
    _html: "",
    children: [],
    style: mkStyle(),
    dataset: {},
    hidden: false,
    value: "",
    clientWidth: 800,
    scrollTop: 0,
    listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); },
    setAttribute(k, v) { this[k] = v; this.dataset[k.replace(/^data-/, "")] = v; },
    getAttribute(k) { return this[k]; },
    removeAttribute(k) { delete this[k]; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    focus() {},
    getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; },
    getContext() {
      return {
        setTransform() {}, fillRect() {}, drawImage() {}, fillText() {},
        getImageData() { return { data: new Uint8ClampedArray(4) }; },
        set fillStyle(v) {}, get fillStyle() { return "#000"; },
      };
    },
    toDataURL() { return "data:image/png;base64,STUB"; },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; },
  };
  return node;
}

const byId = new Map();
function getEl(id) {
  if (!byId.has(id)) byId.set(id, mkEl("div"));
  return byId.get(id);
}

const created = [];
global.window = global;
global.document = {
  addEventListener: () => {},
  getElementById: (id) => getEl(id),
  createElement: (tag) => { const n = mkEl(tag); created.push(n); return n; },
  documentElement: { setAttribute() {}, getAttribute: () => null },
  body: mkEl("body"),
  readyState: "complete",
};
global.indexedDB = { open: () => ({}) };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.matchMedia = () => ({ matches: false });
global.getSelection = () => ({ toString: () => "", rangeCount: 0 });
global.katex = {
  renderToString(tex, opts) {
    return `<span class="katex${opts && opts.displayMode ? " katex-display" : ""}" data-tex="${tex}"></span>`;
  },
};
// ⚠ 假的 pdf.js（必须**在** require app.js 之前放好 —— loadPdfJs 会缓存它）
global.pdfjsLib = {
  GlobalWorkerOptions: {},
  renderTextLayer(opts) {
    if (opts && opts.container) opts.container.dataset.textLayerRendered = "1";
  },
};
global.URL = global.URL || { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} };
global.Blob = global.Blob || class Blob { constructor() {} };

// ---- fetch 桩：记请求；按 URL 返回不同东西 -------------------------------
const calls = [];
let sseText = "";
let snapshotJson = null;
global.fetch = async (url, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : null;
  calls.push({ url: String(url), opts: opts, payload: body });
  if (String(url).indexOf("snapshots/") >= 0 && snapshotJson) {
    return { ok: true, status: 200, json: async () => snapshotJson, text: async () => JSON.stringify(snapshotJson) };
  }
  const enc = new TextEncoder();
  const text = sseText;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        let done = false;
        return {
          async read() {
            if (done) return { done: true, value: undefined };
            done = true;
            return { done: false, value: enc.encode(text) };
          },
        };
      },
    },
    text: async () => text,
    json: async () => ({}),
  };
};

require(APP);
const T = global.__MB_TEST__;
if (!T) {
  console.error("FAIL: app.js 没有暴露 __MB_TEST__");
  process.exit(1);
}

let pass = 0, fail = 0;
const failures = [];
function check(cond, label, detail) {
  if (cond) { pass++; console.log("  PASS  " + label); }
  else {
    fail++; failures.push(label + (detail ? " — " + detail : ""));
    console.log("  FAIL  " + label + (detail ? " — " + detail : ""));
  }
}
function section(t) { console.log("\n=== " + t + " ==="); }

function sseOf(text) {
  return 'data: {"choices":[{"delta":{"content":"' + text + '"}}]}\n\ndata: [DONE]\n\n';
}

function reset() {
  calls.length = 0;
  sseText = sseOf("这是回答。");
  byId.clear();
  T.state.ai.key = "test-key";
  T.state.ai.base = "https://api.example.com/v1";
  T.state.ai.model = "MiniMax-M3.1-Flash";
  T.state.ai.modelT = "MiniMax-M3";
  T.state.notes.clear();
  T.state.notes.set("note-1", { id: "note-1", title: "测试笔记", body: "笔记正文", rel: "note-1.md" });
  T.state.currentId = "note-1";
  T.state.aiHistory.clear();
  T.state.pendingSelection = "";
  T.state.pendingRegionImage = "";
  T.state.pendingSelectionImages = [];
  T.resetThrottle();
}

/** 取最后一条 user 消息的内容（可能是字符串，也可能是 content 数组）。 */
function lastUserContent(msgs) {
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i] && msgs[i].role === "user") return msgs[i].content;
  }
  return null;
}
function imageUrls(content) {
  if (!Array.isArray(content)) return [];
  return content.filter((p) => p && p.type === "image_url")
    .map((p) => (p.image_url || {}).url);
}
function textOf(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.filter((p) => p && p.type === "text").map((p) => p.text).join("\n");
}

(async function main() {
  // -------------------------------------------------------------------------
  section("① 划词提问必须把图带进请求（整页图 + 高清公式图）");
  reset();
  T.state.pendingSelectionImages = [
    { url: "data:image/png;base64,FORMULA1", label: "公式原图（从 PDF 原页裁出，与论文一致）" },
    { url: "data:image/png;base64,PAGE1", label: "整页原文（布局参考）" },
  ];
  getEl("ai-question").value = "这个 d_33^m 是怎么推出来的？";
  await T.runAI("ask");
  check(calls.length === 1, "确实发出去 1 次请求", "calls=" + calls.length);
  const c1 = lastUserContent(calls[0].payload.messages);
  check(Array.isArray(c1), "user 消息变成了 content 数组（能带多张图）", typeof c1);
  const u1 = imageUrls(c1);
  check(u1.indexOf("data:image/png;base64,FORMULA1") >= 0, "**高清公式图**进了请求");
  check(u1.indexOf("data:image/png;base64,PAGE1") >= 0, "整页原文图进了请求");
  const t1 = textOf(c1);
  check(t1.indexOf("d_33^m") >= 0, "用户的问题还在");
  check(t1.indexOf("高清公式图") >= 0, "提示词里写明「以高清公式图为准」", t1.slice(-120));
  check(t1.indexOf("不要") >= 0 && t1.indexOf("猜测") >= 0,
    "提示词里写了「不要猜公式」的约束");
  check(textOf(calls[0].payload.messages[0].content).length > 0, "system 消息还在",
    JSON.stringify(calls[0].payload.messages[0]).slice(0, 160));

  section("② 图用完就清（下一句不能重复带同一张图）");
  check(T.state.pendingSelectionImages.length === 0, "pendingSelectionImages 已清空");
  reset();
  T.state.pendingSelectionImages = [{ url: "data:image/png;base64,X", label: "公式原图" }];
  getEl("ai-question").value = "第一个问题";
  await T.runAI("ask");
  getEl("ai-question").value = "第二个问题";
  await T.runAI("ask");
  check(calls.length === 2, "发了 2 次", "calls=" + calls.length);
  const second = imageUrls(lastUserContent(calls[1].payload.messages));
  check(second.length === 0, "第 2 次提问没有再带上一张图", JSON.stringify(second));

  section("③ 没有图时行为不变（纯文本提问）");
  reset();
  getEl("ai-question").value = "这个结论的依据是什么？";
  await T.runAI("ask");
  const c3 = lastUserContent(calls[0].payload.messages);
  check(typeof c3 === "string", "没有图时仍然是纯文本", typeof c3);
  check(c3.indexOf("这个结论的依据是什么") >= 0, "问题在");
  check(textOf(calls[0].payload.messages[0].content).indexOf("高清公式图") < 0,
    "没有图时不加「公式图」纪律（免得模型被无中生有的约束干扰）");

  section("④ qaImagesForPage 能从译文里的公式标记取到公式图");
  T.pdfState.formulaShots = { 3: { "10.0,20.0,110.0,40.0": "data:image/png;base64,CACHED" } };
  T.pdfState.trans = { 3: "结论 ⟦FORMULA:10,20,110,40⟧ 结束" };
  T.pdfState.blockTrans = null;
  T.pdfState.doc = null;
  const imgs = await T.qaImagesForPage(3);
  check(imgs.some((i) => i.url === "data:image/png;base64,CACHED"),
    "缓存里的公式图被取到了", JSON.stringify(imgs));
  const imgsNone = await T.qaImagesForPage(9);
  check(imgsNone.length === 0, "没有公式的页 → 没有图（不硬塞）");

  section("⑤ 段落译文的版本闸门（旧切分的段落必须被丢弃）");
  T.state.transVer = 5;
  T.state.blocksVer = 1;
  T.DB.getMeta = async () => null;         // 让本地缓存路径走空
  T.DB.setMeta = async () => {};          // 别让 indexedDB 桩的报错刷屏
  snapshotJson = {
    extractor_version: 5,
    pages: [
      { page_num: 1, translation: "整页译文", blocks: [{ i: 0, text: "x", translation: "段落译文" }], blocks_version: 0 },
    ],
  };
  await T.loadTranslation("2008-JACERS-综述-MagnetoelectriclaminatecompositesAnoverview_pgghcp");
  check(T.state.blocksStale === true, "旧段落版本被标记为 stale");
  check(T.pdfState.blockTrans === null, "旧段落译文被丢弃（不会贴到错的段落上）");
  snapshotJson = {
    extractor_version: 5,
    pages: [
      { page_num: 1, translation: "整页译文", blocks: [{ i: 0, text: "x", translation: "段落译文" }], blocks_version: 1 },
    ],
  };
  await T.loadTranslation("2008-JACERS-综述-MagnetoelectriclaminatecompositesAnoverview_pgghcp");
  check(T.state.blocksStale === false, "版本对得上 → 不标 stale");
  check(T.pdfState.blockTrans && T.pdfState.blockTrans["1"],
    "版本对得上的段落译文被保留", JSON.stringify(T.pdfState.blockTrans));

  section("⑥ PDF 文本层必须设 --scale-factor（否则划词会选到错的字）");
  const mockPage = {
    rotate: 0,
    view: [0, 0, 612, 792],
    getViewport({ scale }) {
      return {
        width: 612 * scale, height: 792 * scale,
        transform: [scale, 0, 0, -scale, 0, 792 * scale],
        rawDims: { pageWidth: 612, pageHeight: 792 },
      };
    },
    render() { return { promise: Promise.resolve() }; },
    getTextContent() { return Promise.resolve({ items: [], styles: {} }); },
  };
  T.pdfState.doc = { getPage: async () => mockPage };
  T.pdfState.numPages = 1;
  T.pdfState.rendered = new Set();
  created.length = 0;
  try {
    await T.renderPdfPage(1);
  } catch (e) {
    // 布局相关的部分在假 DOM 下可能抛 —— 文本层在抛之前就已经建好了
  }
  const layer = created.find((n) => n.className === "textLayer");
  check(!!layer, "文本层建出来了");
  if (layer) {
    const sf = layer.style.getPropertyValue("--scale-factor");
    check(sf !== "", "文本层设了 --scale-factor", JSON.stringify(sf));
    check(Number(sf) > 0, "--scale-factor 是正数", JSON.stringify(sf));
  }

  // -------------------------------------------------------------------------
  // 变异自检：改坏 app.js → 同一套断言必须**出现失败**
  // -------------------------------------------------------------------------
  if (SUITE_ONLY) {
    console.log("\nPASS " + pass + "  FAIL " + fail);
    process.exit(fail === 0 ? 0 : 1);
  }

  console.log("\n=== 变异自检（断言必须能抓到回归）===");
  const SRC = fs.readFileSync(APP, "utf8").replace(/\r\n/g, "\n");
  const MUTATIONS = [
    {
      name: "划词提问不带图",
      from: "parts.push({ type: \"image_url\", image_url: { url: it.url } });",
      to: "/* 变异：不把图放进请求 */",
    },
    {
      name: "提示词不带公式纪律",
      from: "content: String(base[1].content || \"\") + QA_IMAGE_RULE,",
      to: "content: String(base[1].content || \"\"),",
    },
    {
      name: "图用完不清空",
      from: "state.pendingSelectionImages = [];",
      to: "/* 变异：不清空 */",
    },
    {
      name: "段落版本闸门被放宽",
      from: "if (curBlocks == null || v !== Number(curBlocks)) {",
      to: "if (false) {",
    },
    {
      name: "文本层不设 --scale-factor",
      from: "textLayer.style.setProperty(\"--scale-factor\", fitScale);",
      to: "/* 变异：不设 --scale-factor */",
    },
  ];
  let caught = 0;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "mb-tablet-mut-"));
  for (const m of MUTATIONS) {
    if (SRC.indexOf(m.from) < 0) {
      console.log("  FAIL  变异 [" + m.name + "] 没匹配到源码 —— 变异自检本身失效");
      fail++;
      continue;
    }
    const mutated = SRC.replace(m.from, m.to);
    const tmp = path.join(tmpDir, "app.js");
    fs.writeFileSync(tmp, mutated, "utf8");
    const proc = spawnSync(process.execPath, [__filename, "--suite-only", tmp], {
      encoding: "utf8", timeout: 120000,
    });
    if (proc.status !== 0) {
      caught++;
      console.log("  PASS  变异 [" + m.name + "] 被抓到了");
    } else {
      console.log("  FAIL  变异 [" + m.name + "] 没被抓到 —— 这套断言是恒真的！");
      fail++;
    }
  }
  try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) { /* ignore */ }

  console.log("\n============================================================");
  console.log("PASS " + pass + "  FAIL " + fail +
              "  (变异捕获 " + caught + "/" + MUTATIONS.length + ")");
  console.log("============================================================");
  process.exit(fail === 0 && caught === MUTATIONS.length ? 0 : 1);
})().catch((e) => {
  console.error("护栏自身崩了: " + (e && e.stack));
  process.exit(1);
});
