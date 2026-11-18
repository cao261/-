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
    sliceBalanced(src, "const _SCOPE_LADDER", "[", "]"),
    sliceFn(src, "function _normQaHeading(s) {"),
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
    sliceFn(src, "function _collectPageFormulaRects(pageNum) {"),
    sliceFn(src, "async function _ensureQaOutlineReady() {"),
    sliceFn(src, "async function submitReaderQA() {"),
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
    querySelector() { return null; },
    focus() {},
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
  return {
    elements, asks, routerCalls,
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
const console = { error: () => {}, warn: () => {}, log: () => {} };
`;
  const factory = new Function(
    "document", "showToast", "escapeHtml", "_aiFormatMs", "formatMarkdownLite",
    "_persistLocalState", "_routeQuestionScope",
    "_captureSelectionPageImage", "askLLMStream",
    prelude + js + `
return {
  _matchQaHeading,
  _normQaHeading,
  _buildSectionContext,
  _clipQaContext,
  _buildScopeContext,
  submitReaderQA,
  scopeCaps: _SCOPE_CAPS,
  scopeLadder: _SCOPE_LADDER,
  sectionBudget: _QA_SECTION_BUDGET,
  setPdfState: (v) => { _pdfState = v; },
  setReaderState: (v) => { _readerState = v; },
  setSelState: (v) => { _readerSelState = v; },
  setOutline: (v) => { _qaOutline = v; },
  getHeadingHit: () => _qaHeadingHit,
};
`,
  );
  return factory(
    env.document, env.showToast, env.escapeHtml, env._aiFormatMs,
    env.formatMarkdownLite, env._persistLocalState, env._routeQuestionScope,
    env._captureSelectionPageImage, env.askLLMStream,
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
}

// ---------------------------------------------------------------------------
// 主套件
// ---------------------------------------------------------------------------
async function runSuite(src, label) {
  log("\n=== " + label + " ===");
  const env = makeEnv();
  const api = buildApi(src, env);
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

  await check("章节视图（非 PDF 模式）→ 用章节自己的标题与译文", async () => {
    const api3 = buildApi(src, makeEnv());
    api3.setOutline({
      path: "a/b.pdf", sections: OUTLINE, ready: true, loading: false, promise: null,
    });
    // 章节视图：_readerState.sections 非空（PDF 模式会把它清空）
    api3.setReaderState({
      title: "某论文",
      sections: [
        { section: "introduction", title: "Introduction", text: "章节视图里的引言正文。",
          page_start: 1, page_end: 2 },
        { section: "methods", title: "Methods", text: "章节视图里的方法正文。",
          page_start: 3, page_end: 4 },
      ],
      translations: ["章节视图里的引言译文。", ""],
      activeSection: 0,
    });
    const hit = api3._matchQaHeading("Introduction");
    assert(hit, "章节视图的标题没命中");
    assertEq(hit.source, "section", "命中来源不对");
    const ctx = api3._buildSectionContext(hit, 16000);
    assert(ctx.indexOf("章节视图里的引言正文") >= 0, "上下文里没有本节正文");
    assert(ctx.indexOf("章节视图里的引言译文") >= 0, "上下文里没有本节译文");
  });

  await check("上下文确实送出去了（本小节正文在 payload 里）", async () => {
    const { ask } = await askWith("II. Experimental Procedure");
    const ctx = ask.payload.surrounding_context || "";
    assert(ctx.indexOf("constitutive equations") >= 0,
      "payload 的上下文里没有本小节正文：" + ctx.slice(0, 200));
    assert(ctx.length <= 17000, "上下文超预算：" + ctx.length);
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
    name: "章节视图里也去用 PDF 大纲（命中的是别的章节）",
    from: "      const secs = (_readerState && _readerState.sections) || [];\n      if (secs.length) {",
    to: "      const secs = (_readerState && _readerState.sections) || [];\n      if (false && secs.length) {",
  },
];

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
