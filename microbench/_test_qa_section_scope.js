/*
 * 划词答疑：「选中一个标题 → 用本小节完整正文当上下文」—— 前端行为级护栏
 * （node 跑 index.html 里的**真实源码**）。
 *
 * 为什么要有这个文件（用户 2026-11-18 原话）：
 *   「我一般的使用场景是选中一个标题，让 ai 先帮我总结一下这一小节的内容，
 *     要注意这个上下文的给法，使其能做到给我合适的阅读学习帮助」
 *
 * 这一轮要盯住的高危点（都是「改坏行为也照样绿」的那种）：
 *   ① 选中标题时**没走「小节全文」** → 上下文还是整页 → AI 总结的是整页，
 *      不是「这一小节」（用户一眼就看出来不对）；
 *   ② 命中标题后**还去问 LLM 路由** → 白等 3~8 秒，而且路由可能把上下文
 *      降级成「紧邻 800 字」→ 标题旁边那半页正文全丢；
 *   ③ 载荷里**没带 selection_kind / heading_title** → 服务端不知道用户选的是
 *      标题，提示里没有「只总结这一小节」的约束 → 模型会扯到别的章节；
 *   ④ **没带本小节跨页的公式坐标** → 小节后一半页的公式推导模型看不到
 *      （AGENTS.md §4.6：错公式比没公式危险得多）；
 *   ⑤ 超预算时**只留头部** → 小节结尾的结论段被丢掉。
 *
 * ⚠️ 自带「变异自检」：把 index.html 改坏后重跑同一套断言，必须**出现失败**。
 *    没有这一段，上面那些断言可能全是恒真的、等于没写。
 *
 * Run:  node microbench/_test_qa_section_scope.js
 *       node microbench/_test_qa_section_scope.js <另一个 index.html>
 */
const fs = require("fs");
const path = require("path");

const TARGET = process.argv[2]
  ? path.resolve(process.argv[2])
  : require("path").join(__dirname, "static", "index.html");
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

// 切一个「括号配对」的语句（const _SCOPE_LADDER = [...] 这种多行的）
function sliceBalanced(src, marker, openCh, closeCh) {
  const start = src.indexOf(marker);
  if (start < 0) throw new Error("找不到语句：" + marker);
  const open = src.indexOf(openCh, start);
  if (open < 0) throw new Error("语句后面没有 " + openCh + "：" + marker);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (c === openCh) depth += 1;
    else if (c === closeCh) {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error("语句没闭合：" + marker);
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
    sliceLine(src, "const _QA_SECTION_BUDGET"),
    sliceLine(src, "const _MAX_CONTEXT_CHARS"),
    sliceLine(src, "const _MAX_SCOPE_BUDGET"),
    sliceBalanced(src, "const _SCOPE_CAPS", "{", "}"),
    sliceBalanced(src, "const _QA_ROUTER_MODES", "[", "]"),
    sliceBalanced(src, "const _SCOPE_LADDER", "[", "]"),
    sliceFn(src, "function _normQaHeading(s) {"),
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
    sliceFn(src, "function _buildScopeContextRaw(scope) {"),
    sliceFn(src, "function _buildScopeContext(scope) {"),
    sliceFn(src, "function _sliceAroundSelection(fullText, selection, budget) {"),
    sliceFn(src, "function _collectPageFormulaRects(pageNum) {"),
    sliceFn(src, "async function _ensureQaOutlineReady() {"),
    sliceFn(src, "async function submitReaderQA() {"),
    sliceFn(src, "async function _refineQA(answerBody, direction, feedbackText) {"),
  ].join(NL + NL);
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
    parentElement: { scrollTop: 0, scrollHeight: 0 },
    querySelector() { return null; },
    focus() {},
  };
}

// 答疑面板里「上一版答案」那个容器（_refineQA 的入参）
function makeAnswerBody(priorMode) {
  const els = {
    "[data-stream-target]": makeEl("stream"),
    "[data-qa-scope-badge]": makeEl("badge"),
    "[data-qa-model-badge]": makeEl("model"),
    "[data-qa-stage-badge]": makeEl("stage"),
  };
  return {
    dataset: {
      question: "帮我总结这一小节",
      priorAnswer: "上一版答案",
      priorScope: "小节全文",
      priorScopeMode: priorMode || "section",
    },
    querySelector(sel) { return els[sel] || null; },
  };
}

// 一份「像真的一样」的大纲（抄自 JACERS 综述的真实结构）
const OUTLINE = [
  {
    index: 0, title: "(前言/摘要)", title_plain: "(前言/摘要)", level: 0,
    page_start: 1, page_end: 1, text: "题目、作者、摘要……", chars: 30,
    formulas: [],
  },
  {
    index: 1, title: "I. Introduction", title_plain: "Introduction", level: 1,
    page_start: 1, page_end: 1,
    text: "T HE magnetoelectric (ME) effect is defined by the electric field. " +
          "【公式1】 This is the introduction body.",
    chars: 120,
    formulas: [{ n: 1, page: 1, rect: [74.4, 361.7, 301.5, 375.5] }],
  },
  {
    index: 2, title: "II. Experimental Procedure", title_plain: "Experimental Procedure",
    level: 1, page_start: 1, page_end: 2,
    text: "The ME effect in piezoelectric laminates. " +
          "【公式1】 constitutive equations. " +
          "【公式2】 coupling coefficient.",
    chars: 200,
    formulas: [
      { n: 1, page: 1, rect: [45.1, 138.3, 272.3, 169.5] },
      { n: 2, page: 2, rect: [308.1, 453.4, 535.3, 483.2] },
    ],
  },
  {
    index: 3, title: "V. Summary", title_plain: "Summary", level: 1,
    page_start: 7, page_end: 8, text: "结论段。", chars: 5, formulas: [],
  },
];

function makeEnv() {
  const elements = {
    "qa-question-input": makeEl("qa-question-input", "textarea"),
    "qa-welcome-placeholder": makeEl("qa-welcome-placeholder"),
    "qa-answer-body": makeEl("qa-answer-body"),
    "qa-submit-btn": makeEl("qa-submit-btn", "button"),
    "paper-category": makeEl("paper-category", "select"),
  };
  elements["qa-question-input"].value = "帮我总结这一小节";
  const asks = [];
  const routerCalls = [];
  const env = {
    elements, asks, routerCalls,
    // 每一页的段落块（真前端里由 ensurePageBlocks → /api/paper/page_blocks 拿）
    blocksByPage: {},
    document: {
      getElementById(id) { return elements[id] || null; },
      querySelector() { return null; },
    },
    showToast() {},
    escapeHtml(s) { return String(s == null ? "" : s); },
    _aiFormatMs(ms) { return String(ms) + "ms"; },
    formatMarkdownLite(s) { return String(s); },
    _persistLocalState() {},
    _routeQuestionScope: async (q, preset, sel) => {
      routerCalls.push({ q, preset, sel });
      return { mode: "mid", budget: 8000, label: "章节上下文 (~8K 字)",
               source: "ai", reason: "桩路由" };
    },
    _captureSelectionPageImage: () => null,
    askLLMStream(btn, url, payload, opts) {
      asks.push({ btn, url, payload, opts });
      return Promise.resolve();
    },
  };
  env.ensurePageBlocks = async (pageNum) => (env.blocksByPage[pageNum] || []);
  return env;
}

// ---------------------------------------------------------------------------
// 把切出来的真代码装进一个作用域
// ---------------------------------------------------------------------------
function buildApi(src, env) {
  const js = sliceAll(src);
  const prelude = `
let _pdfState = null;
let _readerState = { title: "某论文", sections: [], translations: {}, activeSection: 0 };
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
    "_persistLocalState", "_routeQuestionScope",
    "_captureSelectionPageImage", "askLLMStream",
    // ⚠ 新加：版面几何判定要拿这一页的段落块（真前端里是 ensurePageBlocks）
    "ensurePageBlocks",
    // ⚠ new Function 的作用域里没有 require / __dirname（§4.109 同款坑）
    "require", "__dirname",
    prelude + js + `
return {
  _matchQaHeading,
  _normQaHeading,
  _buildSectionContext,
  _clipQaContext,
  _buildScopeContext,
  _buildScopeContextRaw,
  submitReaderQA,
  scopeCaps: _SCOPE_CAPS,
  scopeLadder: _SCOPE_LADDER,
  sectionBudget: _QA_SECTION_BUDGET,
  setPdfState: (v) => { _pdfState = v; },
  setReaderState: (v) => { _readerState = v; },
  setSelState: (v) => { _readerSelState = v; },
  setOutline: (v) => { _qaOutline = v; },
  setHeadingHit: (v) => { _qaHeadingHit = v; },
  setSectionBySelection: (v) => { _qaSectionBySelection = v; },
  findSectionBySelection: _findQaSectionBySelection,
  scopeCapsJson: JSON.stringify(_SCOPE_CAPS),
  routerModes: _QA_ROUTER_MODES.slice(),
  getHeadingHit: () => _qaHeadingHit,
  _refineQA,
};
`,
  );
  return factory(
    env.document, env.showToast, env.escapeHtml, env._aiFormatMs,
    env.formatMarkdownLite, env._persistLocalState, env._routeQuestionScope,
    env._captureSelectionPageImage, env.askLLMStream,
    env.ensurePageBlocks,
    require, __dirname,
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

function withOutline(api) {
  api.setOutline({
    path: "a/b.pdf", sections: OUTLINE, ready: true, loading: false, promise: null,
  });
  // ⚠ 大纲要「**属于当前这篇论文**」才会被采用（§4.110）→
  //   当前论文也得指向同一路径，否则护栏里的大纲会被当成「别人的大纲」。
  api.setReaderState({
    title: "某论文", pdfPath: "a/b.pdf",
    sections: [], translations: {}, activeSection: 0,
  });
}

// ---------------------------------------------------------------------------
// 主套件
// ---------------------------------------------------------------------------
async function runSuite(src, label) {
  log("\n=== " + label + " ===");
  const env = makeEnv();
  const api = buildApi(src, env);
  envScopeCapsJson = api.scopeCapsJson;
  envRouterModesJson = JSON.stringify(api.routerModes);
  withOutline(api);

  // ---- ① 标题识别 ---------------------------------------------------------
  await check("完整标题（含编号）→ 命中", async () => {
    const hit = api._matchQaHeading("II. Experimental Procedure");
    assert(hit, "没命中");
    assertEq(hit.title, "II. Experimental Procedure", "命中的小节不对");
  });

  await check("大小写/多余空白/尾部标点 → 照样命中", async () => {
    const hit = api._matchQaHeading("  ii.  experimental procedure.  ");
    assert(hit, "没命中");
    assertEq(hit.title, "II. Experimental Procedure", "命中的小节不对");
  });

  await check("只选了标题的一部分（≥一半）→ 命中", async () => {
    const hit = api._matchQaHeading("Experimental Procedure");
    assert(hit, "没命中");
    assertEq(hit.title, "II. Experimental Procedure", "命中的小节不对");
  });

  await check("正文句子 → **不**命中（不能把正文当标题）", async () => {
    const hit = api._matchQaHeading(
      "The magnetoelectric effect is defined by the electric field under application");
    assert(!hit, "正文被误判成标题了：" + JSON.stringify(hit && hit.title));
  });

  await check("只是碰巧含有标题词的长句 → **不**命中", async () => {
    const hit = api._matchQaHeading(
      "前面一大段正文，里面恰好提到 II. Experimental Procedure 这个短语，还有更多内容");
    assert(!hit, "长句被误判成标题了：" + JSON.stringify(hit && hit.title));
  });

  await check("两三个字母的碎片 → 不命中", async () => {
    assert(!api._matchQaHeading("ME"), "'ME' 不该命中");
    assert(!api._matchQaHeading("a"), "单字符不该命中");
  });

  // ---- ② 上下文构建 --------------------------------------------------------
  await check("命中标题 → 上下文换成本小节的正文", async () => {
    const hit = api._matchQaHeading("II. Experimental Procedure");
    const ctx = api._buildSectionContext(hit, 16000);
    assert(ctx.indexOf("Experimental Procedure") >= 0, "上下文里没有小节标题");
    assert(ctx.indexOf("constitutive equations") >= 0,
      "上下文里没有本小节的正文（还在用整页？）");
    assert(ctx.indexOf("V. Summary") < 0, "上下文里混进了别的小节");
  });

  await check("上下文里有本小节的中文译文（用户真正在读的那份）", async () => {
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [
        { num: 1, translation: "第一页的中文译文" },
        { num: 2, translation: "第二页的中文译文 ⟦FORMULA:1,2,3,4⟧" },
      ],
    });
    const hit = api._matchQaHeading("II. Experimental Procedure");
    const ctx = api._buildSectionContext(hit, 16000);
    assert(ctx.indexOf("中文译文") >= 0, "上下文里没有中文译文");
    assert(ctx.indexOf("⟦FORMULA:") < 0, "译文里的坐标标记没清掉（纯噪声）");
    api.setPdfState(null);
  });

  await check("超预算时**头尾都保留**（小节结尾的结论不能丢）", async () => {
    const long = "开头".repeat(50) + "中间".repeat(50) + "结尾的结论";
    const clipped = api._clipQaContext(long, 200);
    assert(clipped.length <= 260, "截断后还是太长：" + clipped.length);
    assert(clipped.indexOf("开头") === 0, "头部丢了");
    assert(clipped.indexOf("结尾的结论") >= 0, "尾部丢了（只剩头部截断）");
  });

  await check("截断结果**不超预算**（省略标记也算进去）", async () => {
    // ⚠ 2026-11-19：省略标记是「额外」加的 → 产物比 budget 长几十个字
    //   → 服务端再按 scope 上限裁一刀时，**恰好砍掉我们特意保留的尾部**
    //     （小节的结论就在尾部）—— 前端白保留。
    for (const budget of [2000, 8000, 16000]) {
      const clipped = api._clipQaContext("X".repeat(budget * 3), budget);
      assert(clipped.length <= budget,
        "预算 " + budget + " 却裁出 " + clipped.length + " 字 → 服务端会再砍一刀");
    }
  });

  await check("中文译文标签如实说明「整页翻译」（含本小节之外的段落）", async () => {
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{ num: 1, translation: "第一页的中文译文" }],
    });
    const hit = api._matchQaHeading("II. Experimental Procedure");
    const ctx = api._buildSectionContext(hit, 16000);
    assert(ctx.indexOf("整页") >= 0,
      "译文标签没说「整页」→ 模型会把摘要/引言当成本节内容一起总结：" +
      ctx.slice(0, 200));
    api.setPdfState(null);
  });

  await check("小节上下文不超预算（后端也会再卡一道）", async () => {
    const api2 = buildApi(src, makeEnv());
    withOutline(api2);
    const big = {
      title: "II. Experimental Procedure", page_start: 1, page_end: 2,
      text: "X".repeat(60000), formulas: [], source: "pdf",
    };
    const ctx = api2._buildSectionContext(big, 16000);
    assert(ctx.length <= 17000, "上下文超预算：" + ctx.length);
  });

  await check("小节档预算 = 16K，且上下限/档位三处一致", async () => {
    // ⚠ 这三个数必须与后端 app.py 的 scope_caps["section"] 逐字一致，
    //   否则前端算好的上下文会被服务端再截一刀（徽标显示的字数就是假的）。
    const api2 = buildApi(src, makeEnv());
    withOutline(api2);
    assertEq(api2.sectionBudget, 16000, "小节预算常量不对");
    assertEq(api2.scopeCaps.section, 16000, "scope 上限不对");
    const rung = api2.scopeLadder.find(s => s.mode === "section");
    assert(rung, "档位梯子里没有 section 这一档");
    assertEq(rung.budget, 16000, "档位梯子里的 section 预算不对");
  });

  // ---- ③ 请求载荷 ----------------------------------------------------------
  async function askWith(selText, pdfState) {
    const e = makeEnv();
    const a = buildApi(src, e);
    withOutline(a);
    a.setPdfState(pdfState || null);
    a.setSelState({ text: selText, source: "pdf", pageNum: 1, sectionIdx: null, rects: [] });
    await a.submitReaderQA();
    assertEq(e.asks.length, 1, "应发出一次答疑请求");
    return { ask: e.asks[0], env: e };
  }

  await check("选中标题 → 载荷里带 selection_kind=heading + 小节定位信息", async () => {
    const { ask } = await askWith("II. Experimental Procedure");
    assertEq(ask.payload.selection_kind, "heading", "selection_kind 不对");
    assertEq(ask.payload.heading_title, "II. Experimental Procedure", "标题不对");
    assertEq(ask.payload.force_scope, "section", "scope 不对");
    assert(Array.isArray(ask.payload.section_pages), "section_pages 应是数组");
    assert(ask.payload.section_pages.indexOf(2) >= 0,
      "小节跨到第 2 页，section_pages 里却没有：" +
      JSON.stringify(ask.payload.section_pages));
  });

  await check("选中标题 → 带上**本小节跨页**的公式坐标", async () => {
    const { ask } = await askWith("II. Experimental Procedure");
    const rects = ask.payload.formula_rects_with_page;
    assert(Array.isArray(rects), "formula_rects_with_page 应是数组");
    assertEq(rects.length, 2, "本小节有 2 条公式坐标，实际 " +
      JSON.stringify(rects));
    assertEq(rects[0][0], 1, "第 1 条公式的页码不对");
    assertEq(rects[1][0], 2, "第 2 条公式的页码不对（跨页那条丢了）");
    assertEq(rects[1][1], 308.1, "第 2 条公式的 x0 不对");
  });

  await check("选中标题 → **不**问 LLM 路由（省一次 3~8s 往返）", async () => {
    const { env } = await askWith("II. Experimental Procedure");
    assertEq(env.routerCalls.length, 0,
      "命中标题还去问路由，白等一次往返：" + env.routerCalls.length);
  });

  await check("选中正文 → 照常问路由，且 selection_kind=body", async () => {
    const { ask, env } = await askWith(
      "The magnetoelectric effect is defined by the electric field");
    assertEq(env.routerCalls.length, 1, "正文提问应该问路由");
    assertEq(ask.payload.selection_kind, "body", "selection_kind 不对");
    assertEq(ask.payload.force_scope, "mid", "scope 不对");
    assertEq(ask.payload.formula_rects_with_page.length, 0,
      "正文提问不该带小节公式坐标");
  });

  await check("章节视图 → 命中大纲里的小节，且带上章节视图那一节的译文", async () => {
    const api3 = buildApi(src, makeEnv());
    api3.setOutline({
      path: "a/b.pdf", sections: OUTLINE, ready: true, loading: false, promise: null,
    });
    // 章节视图：_readerState.sections 是**语义章节**（Abstract/Introduction/…）
    api3.setReaderState({
      title: "某论文", pdfPath: "a/b.pdf",
      sections: [
        { section: "introduction", title: "Introduction", text: "章节视图里的引言正文。",
          page_start: 1, page_end: 2 },
        { section: "methods", title: "Methods", text: "章节视图里的方法正文。",
          page_start: 3, page_end: 4 },
      ],
      translations: ["章节视图里的引言译文。", ""],
      activeSection: 0,
    });
    const hit = api3._matchQaHeading("I. Introduction");
    assert(hit, "章节视图的标题没命中");
    assertEq(hit.source, "pdf", "应命中大纲里的小节（PDF 里的真实标题）");
    const ctx = api3._buildSectionContext(hit, 16000);
    assert(ctx.indexOf("This is the introduction body") >= 0,
      "上下文里没有本小节正文：" + ctx.slice(0, 160));
    assert(ctx.indexOf("章节视图里的引言译文") >= 0,
      "上下文里没有章节视图那一节的译文（用户真正在读的那份）");
  });

  await check("大纲不可用 → 退回章节视图自己的标题与译文", async () => {
    const api6 = buildApi(src, makeEnv());
    api6.setOutline({
      path: null, sections: [], ready: false, loading: false, promise: null,
    });
    api6.setReaderState({
      title: "某论文", pdfPath: "a/b.pdf",
      sections: [
        { section: "methods", title: "Methods", text: "章节视图里的方法正文。",
          page_start: 3, page_end: 4 },
      ],
      translations: ["章节视图里的方法译文。"],
      activeSection: 0,
    });
    const hit = api6._matchQaHeading("Methods");
    assert(hit, "没命中");
    assertEq(hit.source, "section", "应退回章节视图自己的标题");
    const ctx = api6._buildSectionContext(hit, 16000);
    assert(ctx.indexOf("章节视图里的方法正文") >= 0, "上下文里没有章节正文");
    assert(ctx.indexOf("章节视图里的方法译文") >= 0, "上下文里没有章节译文");
  });

  await check("PDF 模式下章节视图的 sections 残留 → 仍然用大纲（§4.110 复发防线）", async () => {
    // ⚠ 这是 2026-11-18 Playwright 真浏览器实测抓到的真 bug：
    //   openVaultPdf() 在 PDF 模式下**仍然**填了 _readerState.sections，
    //   而综述类论文它常常只有「Full Text」一桶 →
    //   旧判据（sections 非空就优先）把候选锁死成 ["Full Text"]
    //   → 用户在 PDF 原文里划「II. Experimental Procedure」永远命中不了。
    const api4 = buildApi(src, makeEnv());
    withOutline(api4);
    api4.setReaderState({
      title: "某论文", pdfPath: "a/b.pdf",
      sections: [
        { section: "full_text", title: "Full Text", text: "整篇正文……",
          page_start: 1, page_end: 8 },
      ],
      translations: {}, activeSection: 0,
    });
    const hit = api4._matchQaHeading("Experimental Procedure");
    assert(hit, "没命中（旧 bug：候选被锁死成 ['Full Text']）");
    assertEq(hit.title, "II. Experimental Procedure", "命中的不是大纲里的小节");
    assertEq(hit.source, "pdf", "命中来源不是大纲");
  });

  await check("上一篇论文的大纲残留 → 不许当候选（跨论文串台）", async () => {
    const api5 = buildApi(src, makeEnv());
    api5.setOutline({
      path: "old/previous.pdf", sections: OUTLINE, ready: true, loading: false, promise: null,
    });
    api5.setReaderState({
      title: "另一篇论文", pdfPath: "a/b.pdf",
      sections: [], translations: {}, activeSection: 0,
    });
    const hit = api5._matchQaHeading("II. Experimental Procedure");
    assert(!hit, "拿上一篇论文的大纲当候选了：" +
      JSON.stringify(hit && hit.title));
  });

  await check("上下文确实送出去了（本小节正文在 payload 里）", async () => {
    const { ask } = await askWith("II. Experimental Procedure");
    const ctx = ask.payload.surrounding_context || "";
    assert(ctx.indexOf("constitutive equations") >= 0,
      "payload 的上下文里没有本小节正文：" + ctx.slice(0, 200));
    assert(ctx.length <= 17000, "上下文超预算：" + ctx.length);
  });

  // ---- ④ 重新回答（⬆️/⬇️ 调范围）时的**一致性** ----------------------
  // ⚠ 服务端在 selection_kind="heading" 时会跟模型说「下面是这一小节的完整正文」。
  //   用户把范围调到 wide/full 后，上下文已经换成整页/全文了
  //   → 这时还报 heading = **对模型说假话** → 它以为整页都是这一节 → 总结跑偏。
  const HEADING_HIT = {
    title: "II. Experimental Procedure", page_start: 1, page_end: 2,
    text: "constitutive equations …", source: "pdf",
    formulas: [{ n: 1, page: 2, rect: [45.1, 138.3, 272.3, 169.5] }],
  };

  await check("重新回答：范围仍落在「小节全文」→ 继续报 heading + 小节定位", async () => {
    const e = makeEnv();
    const a = buildApi(src, e);
    withOutline(a);
    a.setHeadingHit(HEADING_HIT);
    // 上一版是 mid → ⬆️ 升级到 section（小节全文）
    await a._refineQA(makeAnswerBody("mid"), "up", "");
    assertEq(e.asks.length, 1, "应发出一次重新回答请求");
    const p = e.asks[0].payload;
    assertEq(p.force_scope, "section", "scope 不对");
    assertEq(p.selection_kind, "heading", "selection_kind 不对");
    assertEq(p.heading_title, "II. Experimental Procedure", "标题不对");
    assertEq(p.section_pages.length, 2, "section_pages 不对");
    assertEq(p.formula_rects_with_page.length, 1, "公式坐标不对");
  });

  await check("重新回答：⬆️ 升到 wide（上下文已不是这一节）→ **别再报 heading**", async () => {
    const e = makeEnv();
    const a = buildApi(src, e);
    withOutline(a);
    a.setHeadingHit(HEADING_HIT);
    // 上一版就是 section → ⬆️ 升级到 wide
    await a._refineQA(makeAnswerBody("section"), "up", "");
    const p = e.asks[0].payload;
    assertEq(p.force_scope, "wide", "scope 不对");
    assertEq(p.selection_kind, "body",
      "上下文已经换成 wide 了还报 heading = 对模型说假话");
    assertEq(p.heading_title, "", "heading_title 不该带");
    assertEq(p.section_pages.length, 0, "section_pages 不该带");
    assertEq(p.formula_rects_with_page.length, 0, "公式坐标不该带");
  });

  // ---- ⑤ section 档打通（2026-11-19）--------------------------------------
  await check("router 白名单含 section 档（与后端 QA_SCOPE_MODES 对齐）", async () => {
    const api12 = buildApi(src, makeEnv());
    const modes = api12.routerModes;
    assert(modes.indexOf("section") >= 0,
      "前端 router 白名单里没有 section 档 —— router 返回 section 会被判非法");
    assertEq(modes.length, 5, "白名单档位数不对：" + JSON.stringify(modes));
  });

  await check("划正文 → 能定位它落在哪个小节（section 档才有用）", async () => {
    const api8 = buildApi(src, makeEnv());
    withOutline(api8);
    const hit = await api8.findSectionBySelection("constitutive equations");
    assert(hit, "没定位到所属小节");
    assertEq(hit.title, "II. Experimental Procedure", "定位到的小节不对");
  });

  await check("选区太短 → **不**定位（宁可不认，也不给错小节）", async () => {
    const api9 = buildApi(src, makeEnv());
    withOutline(api9);
    assert(!await api9.findSectionBySelection("the"), "太短的选区也敢定位小节");
    assert(!await api9.findSectionBySelection(""), "空选区也敢定位小节");
  });

  await check("选区不在这篇论文里 → 不定位", async () => {
    const api10 = buildApi(src, makeEnv());
    withOutline(api10);
    assert(!await api10.findSectionBySelection("一段完全不属于本文的外部文字xyz"),
      "把不相干的选区也定位到了某个小节");
  });

  await check("section 档但没命中小节 → **不能给空上下文**（落回当前页）", async () => {
    const api11 = buildApi(src, makeEnv());
    withOutline(api11);
    api11.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{ num: 1, text: "第 1 页的正文内容", translation: "" }],
    });
    api11.setSelState({ text: "Experimental Procedure", source: "pdf",
                        pageNum: 1, sectionIdx: null, rects: [] });
    const ctx = api11._buildScopeContextRaw({ mode: "section", budget: 12000 });
    assert(ctx && ctx.length > 0,
      "section 档没命中小节时给了**空上下文** → 模型只拿到划的那一句");
    assert(ctx.indexOf("第 1 页的正文内容") >= 0,
      "没有落回当前页上下文：" + String(ctx).slice(0, 120));
  });

  // ---- ⑥ 划正文时**定位到哪个小节**（2026-11-19 真机实测抓到的错）------------
  //
  // ⚠ 实测：JACERS 综述里「magnetostrictive one」这一句**同时**出现在
  //     I. Introduction(p1, 3758 字) / II. Experimental Procedure(p1-2, 1015 字)
  //     / (3) Resonance Enhancement(p7, 2492 字) 三个小节里。
  //   旧判据「多个小节都包含时取**正文最长**的」→ 永远选 I. Introduction
  //   → 用户在第 2 页划的这句，AI 却去总结 I. Introduction（**别人那一节**）。
  //   现在按**位置**判：页码 → 版面几何；都定不了就不给（落回当前页）。
  const OUTLINE_MULTI = [
    { index: 0, title: "I. Introduction", level: 1, page_start: 1, page_end: 1,
      text: "T HE magnetoelectric effect is defined … magnetostrictive one appears in the intro too",
      chars: 120, formulas: [] },
    { index: 1, title: "II. Experimental Procedure", level: 1, page_start: 1, page_end: 2,
      text: "The ME effect in piezoelectric magnetostrictive one laminates …",
      chars: 90, formulas: [] },
    { index: 2, title: "(3) Resonance Enhancement", level: 2, page_start: 7, page_end: 7,
      text: "Resonance enhancement discussion magnetostrictive one …",
      chars: 80, formulas: [] },
  ];

  // 同一页里两个候选 + 真实段落块（bbox 是页面绝对 pt）
  const OUTLINE_GEO = [
    { index: 0, title: "I. Introduction", level: 1, page_start: 1, page_end: 1,
      text: "Introduction body paragraph one magnetostrictive one appears in the intro too",
      chars: 90, formulas: [] },
    { index: 1, title: "II. Experimental Procedure", level: 1, page_start: 1, page_end: 2,
      text: "where a is transverse ME voltage coefficient and v is the volume fraction magnetostrictive one",
      chars: 110, formulas: [] },
  ];

  // 两个候选的正文里有**同一段共用文字** → 几何指认不唯一
  const OUTLINE_GEO_AMB = [
    { index: 0, title: "I. Introduction", level: 1, page_start: 1, page_end: 1,
      text: "shared phrase magnetostrictive one appears in both sections here",
      chars: 70, formulas: [] },
    { index: 1, title: "II. Experimental Procedure", level: 1, page_start: 1, page_end: 2,
      text: "shared phrase magnetostrictive one appears in both sections here as well",
      chars: 80, formulas: [] },
  ];

  function apiWithOutline(sections, env) {
    const a = buildApi(src, env || makeEnv());
    a.setOutline({
      path: "a/b.pdf", sections, ready: true, loading: false, promise: null,
    });
    a.setReaderState({
      title: "某论文", pdfPath: "a/b.pdf",
      sections: [], translations: {}, activeSection: 0,
    });
    return a;
  }

  await check("同一句在 3 个小节里出现 → 按**页码**定，不按「谁最长」", async () => {
    const api = apiWithOutline(OUTLINE_MULTI);
    // 用户在**第 2 页**划的这句 → 正确答案：II. Experimental Procedure
    const hit = await api.findSectionBySelection("magnetostrictive one", 2, []);
    assert(hit, "没定位到小节");
    assertEq(hit.title, "II. Experimental Procedure",
      "定位到的小节不对（旧判据会选最长的 I. Introduction）");
  });

  await check("页码落在第 7 页 → 选那一页的小节", async () => {
    const api = apiWithOutline(OUTLINE_MULTI);
    const hit = await api.findSectionBySelection("magnetostrictive one", 7, []);
    assert(hit, "没定位到小节");
    assertEq(hit.title, "(3) Resonance Enhancement", "第 7 页应定位到该小节");
  });

  await check("没有页码线索 → 认不出就返回 null（不猜「最长的」）", async () => {
    const api = apiWithOutline(OUTLINE_MULTI);
    const hit = await api.findSectionBySelection("magnetostrictive one", null, []);
    assert(!hit, "没有位置线索也敢定位（旧判据 = 取最长的那个）");
  });

  await check("同一页多个候选 → 用版面几何定（选区落在哪个段落块）", async () => {
    const env = makeEnv();
    const api = apiWithOutline(OUTLINE_GEO, env);
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{ num: 1, ptW: 612, ptH: 792 }],
    });
    env.blocksByPage[1] = [
      { i: 0, kind: "body", col: "L", bbox: [31, 150, 270, 250],
        text: "where a is transverse ME voltage coefficient and v is the volume fraction" },
      { i: 1, kind: "body", col: "L", bbox: [31, 260, 270, 400],
        text: "Introduction body paragraph one" },
    ];
    // 选区在页面上 20% 的高度 → 落在第 1 个块里 → 那个块属于 II. Experimental Procedure
    const hit = await api.findSectionBySelection("magnetostrictive one", 1,
      [{ x: 0.05, y: 0.2, w: 0.1, h: 0.01 }]);
    assert(hit, "没定位到小节");
    assertEq(hit.title, "II. Experimental Procedure", "几何判定选错了小节");
  });

  await check("块的文本同时落在两个候选小节里 → 认不出（不猜）", async () => {
    const env = makeEnv();
    const api = apiWithOutline(OUTLINE_GEO_AMB, env);
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{ num: 1, ptW: 612, ptH: 792 }],
    });
    env.blocksByPage[1] = [
      { i: 0, kind: "body", col: "L", bbox: [31, 150, 270, 250],
        text: "shared phrase magnetostrictive one appears in both sections here" },
    ];
    const hit = await api.findSectionBySelection("magnetostrictive one", 1,
      [{ x: 0.05, y: 0.2, w: 0.1, h: 0.01 }]);
    assert(!hit, "指认不唯一也敢给一个小节（宁可落回当前页）");
  });

  await check("选区落在任何段落块之外 → 认不出（不猜）", async () => {
    const env = makeEnv();
    const api = apiWithOutline(OUTLINE_GEO, env);
    api.setPdfState({
      relativePath: "a/b.pdf",
      pages: [{ num: 1, ptW: 612, ptH: 792 }],
    });
    env.blocksByPage[1] = [
      { i: 0, kind: "body", col: "L", bbox: [31, 150, 270, 250],
        text: "where a is transverse ME voltage coefficient and v is the volume fraction" },
    ];
    // 选区在页面 95% 的高度（页脚区域）→ 离任何块都很远
    const hit = await api.findSectionBySelection("magnetostrictive one", 1,
      [{ x: 0.05, y: 0.95, w: 0.1, h: 0.01 }]);
    assert(!hit, "离所有块都很远的选区也敢定位");
  });

  await check("页面没渲染（拿不到物理尺寸）→ 认不出", async () => {
    const env = makeEnv();
    const api = apiWithOutline(OUTLINE_GEO, env);
    api.setPdfState({ relativePath: "a/b.pdf", pages: [{ num: 1 }] });
    env.blocksByPage[1] = [
      { i: 0, kind: "body", col: "L", bbox: [31, 150, 270, 250],
        text: "where a is transverse ME voltage coefficient and v is the volume fraction" },
    ];
    const hit = await api.findSectionBySelection("magnetostrictive one", 1,
      [{ x: 0.05, y: 0.2, w: 0.1, h: 0.01 }]);
    assert(!hit, "没有页面物理尺寸也敢定位");
  });

  return { passed, failed };
}

// ---------------------------------------------------------------------------
// 变异自检：把 index.html 改坏，同一套断言必须**出现失败**
// ---------------------------------------------------------------------------
const MUTATIONS = [
  {
    name: "命中标题也不走「小节全文」（退回整页上下文）",
    from: 'const headingHit = _matchQaHeading(_readerSelState.text || "");',
    to: 'const headingHit = null;',
  },
  {
    name: "载荷里不告诉服务端「用户选的是标题」",
    from: 'selection_kind: headingHit ? "heading" : "body",',
    to: 'selection_kind: "body",',
  },
  {
    name: "不带本小节跨页的公式坐标（后半页推导模型看不到）",
    from: "formula_rects_with_page: headingHit ? _sectionFormulaRects(headingHit) : [],",
    to: "formula_rects_with_page: [],",
  },
  {
    name: "命中标题却用 mid 档上下文（只剩 8K 的整页）",
    from: 'mode: "section",\n          budget: _QA_SECTION_BUDGET,',
    to: 'mode: "mid",\n          budget: _QA_SECTION_BUDGET,',
  },
  {
    name: "超预算时只留头部（小节结尾的结论被丢掉）",
    from: "return s.slice(0, head)",
    to: "return s.slice(0, budget)",
  },
  {
    name: "标题判定放宽（正文里含标题词的长句被误判成标题）",
    from: "sel.length <= t.length * 1.6",
    to: "sel.length <= t.length * 99",
  },
  {
    name: "小节上下文不带中文译文",
    from: "if (pg && pg.translation) {",
    to: "if (false && pg && pg.translation) {",
  },
  {
    name: "小节预算写错（与服务端不同步）",
    from: "const _QA_SECTION_BUDGET = 16000;",
    to: "const _QA_SECTION_BUDGET = 999999;",
  },
  {
    name: "PDF 模式下章节视图的 sections 残留把候选锁死（§4.110 旧 bug 复发）",
    from: "for (const s of _qaOutlineSections()) {",
    to: "for (const s of ((_readerState && _readerState.sections) || [])) {",
  },
  {
    name: "不校验大纲归属（上一篇论文的大纲被当成本篇的小节）",
    from: "if (!_qaOutline.path || !cur || _qaOutline.path !== cur) return [];",
    to: "if (false) return [];",
  },
  {
    name: "重新回答时上下文已换成别的档，却还报 heading（对模型说假话）",
    from: 'selection_kind: headingScoped ? "heading" : "body",',
    to: 'selection_kind: _qaHeadingHit ? "heading" : "body",',
  },
  {
    name: "页码过滤失效（同一句在多个小节里时又挑到最长的那个）",
    from: "if (onPage.length) pool = onPage;",
    to: "if (false) pool = onPage;",
  },
  {
    name: "几何指认不唯一也硬给（猜错小节）",
    from: "if (hit.length !== 1) return null;",
    to: "if (false) return null;",
  },
  {
    name: "定不了小节时硬给候选里第一个（宁给错也不给少）",
    from: "        return null;      // 定不了 → 不给错的小节",
    to: "        return _qaSectionPayload(pool[0]);",
  },
  {
    name: "选区坐标换算比例写错（几何定位全偏）",
    from: "return { x: (x + w / 2) * pg.ptW, y: (y + h / 2) * pg.ptH };",
    to: "return { x: (x + w / 2) * pg.ptW, y: (y + h / 2) * 10 };",
  },
];

let envScopeCapsJson = "";
let envRouterModesJson = "";

// ⚠ 参数错位不会立刻报错（没用到那个参数就静默过）→ 显式数一遍
function _factoryArgCounts(src) {
  const decl = src.match(/const factory = new Function\(([\s\S]*?)\n  \);/);
  const call = src.match(/return factory\(([\s\S]*?)\n  \);/);
  if (!decl || !call) return null;
  const names = decl[1]
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("//") && !l.startsWith("prelude"))
    .join(" ")
    .split(",")
    .map((s) => s.trim().replace(/^"|"$/g, ""))
    .filter(Boolean);
  const args = call[1]
    .split("\n")
    .map((l) => l.trim().replace(/,$/, ""))
    .filter((l) => l && !l.startsWith("//"))
    .join(" ")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return { names, args };
}

async function main() {
  console.log("被测文件: " + TARGET);
  const result = await runSuite(SRC, "划词答疑 · 小节上下文");

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

  const counts = _factoryArgCounts(SRC);
  if (counts && counts.names.length !== counts.args.length) {
    console.log("  FAIL  factory 形参 " + counts.names.length +
      " 个 / 实参 " + counts.args.length + " 个 —— **参数错位**");
    result.failed += 1;
  }
  console.log("SCOPE_CAPS_JSON: " + (envScopeCapsJson || "{}"));
  console.log("ROUTER_MODES_JSON: " + (envRouterModesJson || "[]"));
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
