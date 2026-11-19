/*
 * 整篇问答请求体的行为级护栏（2026-11-19，25353 台）
 *
 * 跑的是 index.html 里的**真代码**（从 HTML 里按大括号配平抽出来执行），
 * 不是抄一遍 —— 抄一遍的版本改坏真代码也不会红，那是假护栏。
 *
 * 守的是「整篇问答」（/api/paper/explain_stream）这条路径：
 *   1. sections **全量**发送（不再 slice(0,20)）—— 长论文 20+ 节时，
 *      后半部分（恰恰是用户正在问的那部分）不能被静默丢掉；
 *   2. 带上 focus_index（用户当前所在的那一节）；
 *   3. 带上 number / page_start / page_end（后端靠 number 匹配「第 N 节」、
 *      靠页码定位所属节）。
 *
 * ⚠️ 划词答疑那条路径（explain_selection_stream）**不在这里** ——
 *   30461 台已把它做成更细的 scope 机制（term/mid/section/wide/full +
 *   真小节定位），护栏在 microbench/_test_qa_section_scope.js。
 *
 * 用法： node microbench/_test_focus_context.js
 * 输出： 一行 JSON；退出码 0 = 全过
 */
const fs = require("fs");
const path = require("path");

const REPO = process.env.MB_REPO || path.resolve(__dirname, "..");
// MB_INDEX 可指向**临时副本** —— 反向验证时用（把真代码改坏，看护栏会不会红）
const INDEX = process.env.MB_INDEX
  ? path.resolve(process.env.MB_INDEX)
  : path.join(REPO, "microbench", "static", "index.html");
const html = fs.readFileSync(INDEX, "utf8");

/** 从 HTML 里按大括号配平抽出一个函数定义（含名字）。 */
function extractFunc(name) {
  const marker = "function " + name + "(";
  const start = html.indexOf(marker);
  if (start < 0) throw new Error("index.html 里找不到函数 " + name);
  let i = html.indexOf("{", start);
  if (i < 0) throw new Error(name + " 找不到函数体");
  let depth = 0;
  for (; i < html.length; i++) {
    const c = html[i];
    if (c === "{") depth++;
    else if (c === "}") {
      depth--;
      if (depth === 0) { i++; break; }
    }
  }
  return html.slice(start, i);
}

const FUNCTIONS = ["_buildExplainPayload"];
const code = FUNCTIONS.map(extractFunc).join("\n\n");

/** 在给定的沙箱全局下跑这些真函数。 */
function load(sandbox) {
  const names = FUNCTIONS.join(", ");
  const factory = new Function(
    "_readerState", "_readerSelState", "_pdfState", "_pdfTransModel",
    code + "\nreturn { " + names + " };");
  return factory(
    sandbox.readerState, sandbox.readerSelState,
    sandbox.pdfState, sandbox.pdfTransModel);
}

function mkSections(n, textLen) {
  const out = [];
  for (let i = 0; i < n; i++) {
    out.push({
      section: "SEC" + String(i + 1).padStart(2, "0"),
      title: (i + 1) + ". 第" + (i + 1) + "节",
      number: String(i + 1),
      text: "【SEC" + String(i + 1).padStart(2, "0") + "】" + "x".repeat(textLen),
      page_start: i * 10 + 1,
      page_end: i * 10 + 10,
    });
  }
  return out;
}

const results = [];
function check(name, fn) {
  try {
    fn();
    results.push({ name: name, ok: true });
  } catch (e) {
    results.push({ name: name, ok: false, error: String(e && e.message || e) });
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// ---------------------------------------------------------------------------
// 1) 全量发送章节
// ---------------------------------------------------------------------------

check("payload/sends_all_sections", () => {
  const sections = mkSections(25, 100);
  const api = load({
    readerState: { sections: sections, activeSection: 17, title: "长论文" },
    readerSelState: { text: "", sectionIdx: null, pageNum: null },
    pdfState: null,
    pdfTransModel: "",
  });
  const payload = api._buildExplainPayload("总结第 18 节");
  assert(payload.sections.length === 25,
    "只发了 " + payload.sections.length + " 节 —— 长论文后半部分被截断了");
  assert(payload.sections[24].section === "SEC25", "最后���节没发出去");
});

check("payload/focus_index", () => {
  const sections = mkSections(25, 100);
  const api = load({
    readerState: { sections: sections, activeSection: 17, title: "长论文" },
    readerSelState: { text: "", sectionIdx: null, pageNum: null },
    pdfState: null,
    pdfTransModel: "",
  });
  const payload = api._buildExplainPayload("总结一下这一小节");
  assert(payload.focus_index === 17,
    "focus_index 是 " + payload.focus_index + "，应为 17");
});

check("payload/keeps_number_and_pages", () => {
  const sections = mkSections(3, 100);
  const api = load({
    readerState: { sections: sections, activeSection: 0, title: "t" },
    readerSelState: { text: "", sectionIdx: null, pageNum: null },
    pdfState: null,
    pdfTransModel: "",
  });
  const payload = api._buildExplainPayload("q");
  const s = payload.sections[2];
  assert(s.number === "3", "number 没带上 —— 后端靠它匹配「第 N 节」");
  assert(s.page_start === 21 && s.page_end === 30,
    "页码范围没带上 —— 后端/前端靠它定位所属节");
  assert(s.text.length > 0, "正文没带上");
});

check("payload/handles_no_sections", () => {
  const api = load({
    readerState: { sections: [], activeSection: null, title: "" },
    readerSelState: { text: "", sectionIdx: null, pageNum: null },
    pdfState: null,
    pdfTransModel: "",
  });
  const payload = api._buildExplainPayload("q");
  assert(Array.isArray(payload.sections) && payload.sections.length === 0,
    "没有章节时 payload.sections 必须是空数组");
  assert(payload.focus_index === null, "没有章节时 focus_index 必须是 null");
});

// ---------------------------------------------------------------------------
const failed = results.filter(r => !r.ok);
console.log(JSON.stringify({ total: results.length, failed: failed.length, results }, null, 2));
process.exit(failed.length === 0 ? 0 : 1);
