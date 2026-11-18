/*
 * 划词答疑：公式坐标采集 + 请求载荷 —— 前端行为级护栏
 * （node 跑 index.html 里的**真实源码**）。
 *
 * 为什么要有这个文件：
 *   用户 2026-11-17：「划译文的词句问问题……公式是直接截图放在译文里的，
 *   ai 看上下文的时候完全看不到公式推导，只能猜」。
 *   两层根因都在前端/协议层：
 *     ① 译文栏划选走 source_type="translation"，而截图门禁写的是
 *        `source === "pdf"` → 整页截图根本没发给模型；
 *     ② 就算发了整页图，公式在整页图上只有 ~18px 高 → 模型照样读不出来
 *        → 必须把公式**按坐标单独裁成高清图**一起发。
 *   这两件事都是「改坏行为也照样绿」的高危点：
 *     把 payload 里的 formula_rects 删掉、把正则改错、把 page_num 置 null、
 *     把译文栏的门禁改回去 —— 源码里该有的字符串还在 → 旧式源码文本护栏全绿。
 *   → 所以改成**行为级**：把 index.html 里那几段真代码切出来，在最小桩里
 *     真跑一遍，断言**可观察的后果**（请求里到底带没带公式坐标）。
 *
 * ⚠️ 自带「变异自检」：把 index.html 改坏后重跑同一套断言，必须**出现失败**。
 *    没有这一段，上面那些断言可能全是恒真的、等于没写。
 *
 * Run:  node microbench/_test_qa_formula_frontend.js
 *       node microbench/_test_qa_formula_frontend.js <另一个 index.html>
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
  const open = src.indexOf("{" + NL, start);
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

// 切一行（const _QA_SECTION_BUDGET = 16000; 这种单行语句）
function sliceLine(src, marker) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error("找不到语句：" + marker);
  const end = src.indexOf(NL, start);
  return src.slice(start, end < 0 ? src.length : end);
}

function sliceAll(src) {
  return [
    sliceFn(src, "function _collectPageFormulaRects(pageNum) {"),
    // ⚠ 2026-11-18：submitReaderQA 多了一条「选中标题 → 小节上下文」的支线，
    //   它依赖下面这几个函数。**必须一起切进来** —— 少切一个，
    //   这套护栏就会因为「函数不存在」而崩（2026-11-18 真跑过一次）。
    sliceLine(src, "const _QA_SECTION_BUDGET"),
    sliceFn(src, "function _normQaHeading(s) {"),
    // ⚠ 2026-11-18 又加了一条依赖：_qaHeadingCandidates 改成先问
    //   _qaOutlineSections()（校验「这份大纲属于当前这篇论文」）。
    //   **必须一起切进来** —— 少切一个，这套护栏就会因为
    //   「函数不存在」而崩（§4.109 同一个坑，踩第二次）。
    sliceFn(src, "function _qaOutlineSections() {"),
    sliceFn(src, "function _qaSectionPayload(sec) {"),
    sliceFn(src, "function _qaSectionCoversPage(sec, pageNum) {"),
    sliceFn(src, "function _qaSelectionPointPt(pageNum, selRects) {"),
    sliceFn(src, "function _qaBlockAtPoint(blocks, px, py) {"),
    sliceFn(src, "async function _qaSectionByGeometry(pool, pageNum, selRects) {"),
    sliceFn(src, "async function _findQaSectionBySelection(selectionText, pageNum, selRects) {"),
    sliceFn(src, "function _qaHeadingCandidates() {"),
    sliceFn(src, "function _matchQaHeading(selectionText) {"),
    sliceFn(src, "function _clipQaContext(text, budget) {"),
    sliceFn(src, "function _stripQaMarkers(text) {"),
    sliceFn(src, "function _sectionChineseText(hit) {"),
    sliceFn(src, "function _buildSectionContext(hit, budget) {"),
    sliceFn(src, "function _sectionPageList(hit) {"),
    sliceFn(src, "function _sectionFormulaRects(hit) {"),
    sliceFn(src, "async function _ensureQaOutlineReady() {"),
    sliceFn(src, "async function submitReaderQA() {"),
  ].join("\n\n");
}

// ---------------------------------------------------------------------------
// 最小 DOM 桩
// ---------------------------------------------------------------------------
function makeEl(id, tag) {
  return {
    id,
    tagName: String(tag || "div").toUpperCase(),
    className: "",
    style: {},
    textContent: "",
    value: "",
    innerHTML: "",
    dataset: {},
    disabled: false,
    querySelector() { return null; },
    focus() {},
  };
}

function makeEnv() {
  const elements = {
    "qa-question-input": makeEl("qa-question-input", "textarea"),
    "qa-welcome-placeholder": makeEl("qa-welcome-placeholder"),
    "qa-answer-body": makeEl("qa-answer-body"),
    "qa-submit-btn": makeEl("qa-submit-btn", "button"),
    "paper-category": makeEl("paper-category", "select"),
  };
  elements["qa-question-input"].value = "这个系数怎么定义的？";

  const toasts = [];
  const asks = [];

  return {
    elements,
    toasts,
    asks,
    document: {
      getElementById(id) { return elements[id] || null; },
      querySelector() { return null; },
    },
    showToast(msg, isErr) { toasts.push({ msg: String(msg), isErr: !!isErr }); },
    escapeHtml(s) { return String(s == null ? "" : s); },
    _aiFormatMs(ms) { return String(ms) + "ms"; },
    formatMarkdownLite(s) { return String(s); },
    _persistLocalState() {},
    _routeQuestionScope: async () => ({
      mode: "mid", label: "中等", budget: 8000, reason: "桩", source: "preset",
    }),
    _buildScopeContext: () => "桩上下文",
    _captureSelectionPageImage: () => "data:image/png;base64,AAAA",
    // 定位小节那条链要用页面段落块（真前端里是 /api/paper/page_blocks）
    ensurePageBlocks: async () => [],
    askLLMStream(btn, url, payload, opts) {
      asks.push({ btn, url, payload, opts });
      return Promise.resolve();
    },
  };
}

// ---------------------------------------------------------------------------
// 把切出来的真代码装进一个作用域
// ---------------------------------------------------------------------------
function buildApi(src, env) {
  const js = sliceAll(src);
  const prelude = `
let _pdfState = null;
let _readerState = { title: "某论文", sections: [], translations: {} };
let _readerSelState = { text: "", source: "general", pageNum: null, sectionIdx: null, rects: [] };
let _qaModel = "";
let _currentQAPreset = "explain";
let _lastAnswerText = "";
let _lastQAItem = null;
let _sessionQAList = [];
let _qaOutline = { path: null, sections: [], ready: false, loading: false, promise: null };
let _qaHeadingHit = null;
let _qaSectionBySelection = null;
const console = { error: () => {}, warn: () => {}, log: () => {} };
`;
  const factory = new Function(
    "document", "showToast", "escapeHtml", "_aiFormatMs", "formatMarkdownLite",
    "_persistLocalState", "_routeQuestionScope", "_buildScopeContext",
    "_captureSelectionPageImage", "askLLMStream",
    // ⚠ 2026-11-19：submitReaderQA 现在会定位「划的正文落在哪个小节」
    //   → 那条链依赖 ensurePageBlocks（页面段落块）
    "ensurePageBlocks",
    prelude + js + `
return {
  _collectPageFormulaRects,
  submitReaderQA,
  setPdfState: (v) => { _pdfState = v; },
  setReaderState: (v) => { _readerState = v; },
  setSelState: (v) => { _readerSelState = v; },
};
`,
  );
  return factory(
    env.document, env.showToast, env.escapeHtml, env._aiFormatMs,
    env.formatMarkdownLite, env._persistLocalState, env._routeQuestionScope,
    env._buildScopeContext, env._captureSelectionPageImage, env.askLLMStream,
    env.ensurePageBlocks,
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

// 一条公式标记（坐标随便取，但必须是「带坐标」的那种）
const F1 = "⟦FORMULA:76.4,363.7,299.6,373.5⟧";
const F2 = "⟦FORMULA:310.1,455.4,533.3,481.2⟧";

// ---------------------------------------------------------------------------
// 主套件
// ---------------------------------------------------------------------------
async function runSuite(src, label) {
  log("\n=== " + label + " ===");
  const env = makeEnv();
  const api = buildApi(src, env);

  // ---- 公式坐标采集 ------------------------------------------------------
  await check("整页译文里的 ⟦FORMULA:x0,y0,x1,y1⟧ 被抽成坐标数组", async () => {
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{
        num: 3,
        translation: "前面的正文 " + F1 + " 中间的正文 " + F2 + " 后面的正文",
        blockTranslations: {},
      }],
    });
    const rects = api._collectPageFormulaRects(3);
    assertEq(rects.length, 2, "应抽出 2 条公式坐标，实际 " + JSON.stringify(rects));
    assertEq(rects[0][0], 76.4, "x0 不对");
    assertEq(rects[0][1], 363.7, "y0 不对");
    assertEq(rects[0][2], 299.6, "x1 不对");
    assertEq(rects[0][3], 373.5, "y1 不对");
    assertEq(rects[1][0], 310.1, "第二条 x0 不对");
  });

  await check("对照模式（blockTranslations）里的公式也能抽到", async () => {
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{
        num: 1,
        translation: "",
        blockTranslations: { 0: "正文", 1: "公式：" + F1, 2: "公式：" + F2 },
      }],
    });
    const rects = api._collectPageFormulaRects(1);
    assertEq(rects.length, 2, "应抽出 2 条，实际 " + JSON.stringify(rects));
    assertEq(rects[0][0], 76.4, "x0 不对");
  });

  await check("同一条公式重复出现只算一次（整页译文 + 段落译文会重复）", async () => {
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{
        num: 1,
        translation: "译文里也有 " + F1,
        blockTranslations: { 0: "段落里也有 " + F1 },
      }],
    });
    const rects = api._collectPageFormulaRects(1);
    assertEq(rects.length, 1, "重复的公式应去重，实际 " + JSON.stringify(rects));
  });

  await check("这一页没有公式 → 返回空数组，不抛异常", async () => {
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{ num: 2, translation: "纯正文，没有任何公式标记。", blockTranslations: {} }],
    });
    const rects = api._collectPageFormulaRects(2);
    assertEq(rects.length, 0, "应为空，实际 " + JSON.stringify(rects));
  });

  await check("没有页码 / 没有该页 → 返回空数组，不抛异常", async () => {
    api.setPdfState({ relativePath: "a/b.pdf", pages: [{ num: 1, translation: F1 }] });
    assertEq(api._collectPageFormulaRects(null).length, 0, "无页码应为空");
    assertEq(api._collectPageFormulaRects(99).length, 0, "页码不存在应为空");
    api.setPdfState(null);
    assertEq(api._collectPageFormulaRects(1).length, 0, "没有 _pdfState 应为空");
  });

  // ---- 请求载荷 ----------------------------------------------------------
  async function askWith(selState, pdfState) {
    const e = makeEnv();
    const a = buildApi(src, e);
    a.setPdfState(pdfState);
    a.setSelState(selState);
    await a.submitReaderQA();
    assertEq(e.asks.length, 1, "应发出一次答疑请求");
    return e.asks[0];
  }

  await check("译文栏划选提问 → 请求里带上了该页的公式坐标", async () => {
    const ask = await askWith(
      { text: "磁电电压系数", source: "translation", pageNum: 3,
        sectionIdx: null, rects: [] },
      {
        relativePath: "a/b.pdf",
        pages: [{ num: 3, translation: "正文 " + F1 + " 正文", blockTranslations: {} }],
      },
    );
    assertEq(ask.url, "/api/paper/explain_selection_stream", "端点不对");
    const rects = ask.payload.formula_rects;
    assert(Array.isArray(rects), "payload 里没有 formula_rects 数组：" +
      JSON.stringify(Object.keys(ask.payload)));
    assertEq(rects.length, 1, "公式坐标没带上：" + JSON.stringify(rects));
    assertEq(rects[0][0], 76.4, "公式坐标不对");
  });

  await check("译文栏划选 → 页码也要带上（否则服务端连整页图都不渲染）", async () => {
    const ask = await askWith(
      { text: "磁电电压系数", source: "translation", pageNum: 3,
        sectionIdx: null, rects: [] },
      { relativePath: "a/b.pdf", pages: [{ num: 3, translation: F1 }] },
    );
    assertEq(ask.payload.page_num, 3, "译文栏划选没带页码");
    assertEq(ask.payload.source_type, "translation", "来源类型不对");
  });

  await check("没有公式的页 → formula_rects 是空数组（不是 undefined）", async () => {
    const ask = await askWith(
      { text: "随便问问", source: "translation", pageNum: 2,
        sectionIdx: null, rects: [] },
      { relativePath: "a/b.pdf", pages: [{ num: 2, translation: "纯正文" }] },
    );
    assert(Array.isArray(ask.payload.formula_rects),
      "formula_rects 应是数组，实际 " + JSON.stringify(ask.payload.formula_rects));
    assertEq(ask.payload.formula_rects.length, 0, "应为空");
  });

  return { passed, failed };
}

// ---------------------------------------------------------------------------
// 变异自检：把 index.html 改坏，同一套断言必须**出现失败**
// ---------------------------------------------------------------------------
const MUTATIONS = [
  {
    name: "把载荷里的 formula_rects 掏空（模型又只能猜了）",
    from: "formula_rects: _collectPageFormulaRects(_readerSelState.pageNum),",
    to: "formula_rects: [],",
  },
  {
    name: "把页码置空（服务端连整页图都不渲染）",
    from: "page_num: _readerSelState.pageNum || null,",
    to: "page_num: null,",
  },
  {
    name: "把公式标记的正则改坏（一条都抽不到）",
    from: "const RE = /⟦FORMULA:(-?\\d+(?:\\.\\d+)?),(-?\\d+(?:\\.\\d+)?),(-?\\d+(?:\\.\\d+)?),(-?\\d+(?:\\.\\d+)?)⟧/g;",
    to: "const RE = /__NOTHING__/g;",
  },
  {
    name: "不读整页译文（普通模式翻的页就抽不到公式了）",
    from: "push(pg.translation);",
    to: "/* 不读整页译文 */;",
  },
  {
    name: "不读对照模式的段落译文",
    from: "Object.keys(bt).forEach(k => push(bt[k]));",
    to: "/* 不读段落译文 */;",
  },
  {
    name: "把去重删掉（同一条公式被裁两遍）",
    from: "if (seen.has(key)) continue;",
    to: "/* 不去重 */;",
  },
];

async function main() {
  console.log("被测文件: " + TARGET);
  const result = await runSuite(SRC, "划词答疑 · 公式坐标");

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
      console.log("  PASS  变异 [" + m.name + "] 被抓到了（" + mutantFailed + " 条失败）");
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
