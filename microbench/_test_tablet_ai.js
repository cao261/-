/*
 * AI 面板的行为测试。
 *
 * 这个面板最容易出的问题是「**发完了输入框不清空**」——
 * 上一句问题还留在框里，用户顺手按回车就把上一句又发一遍。
 * 那种问题在真机上靠肉眼很难发现，所以这里钉死。
 *
 * Run:  node microbench/_test_tablet_ai.js
 */
const path = require("path");

// ---- 一个够用的 DOM shim ------------------------------------------------
function mkEl(tag) {
  return {
    tagName: String(tag || "").toUpperCase(),
    className: "",
    _text: "",
    _html: "",
    children: [],
    style: {},
    hidden: false,
    value: "",
    scrollTop: 0,
    listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); },
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k]; },
    removeAttribute(k) { delete this[k]; },
    addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); },
    removeEventListener() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    closest() { return null; },
    focus() {},
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.children = []; },
  };
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
  documentElement: { setAttribute() {}, getAttribute: () => null },
  body: mkEl("body"),
};
global.indexedDB = { open: () => ({}) };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.matchMedia = () => ({ matches: false });
global.getSelection = () => ({ toString: () => "" });
global.katex = {
  renderToString(tex, opts) {
    return `<span class="katex${opts && opts.displayMode ? " katex-display" : ""}" data-tex="${tex}"></span>`;
  },
};

// ---- fetch 桩：记录请求，回一段 SSE ---------------------------------------
const calls = [];
let nextSse = "";
global.fetch = async (url, opts) => {
  calls.push({ url, opts, payload: JSON.parse(opts.body) });
  const enc = new TextEncoder();
  const text = nextSse;
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
  };
};

require(path.join(__dirname, "static", "tablet", "app.js"));
const T = global.__MB_TEST__;
if (!T) {
  console.error("FAIL: app.js 没有暴露 __MB_TEST__");
  process.exit(1);
}

let pass = 0, fail = 0;
function check(cond, label) {
  if (cond) { pass++; console.log("  PASS  " + label); }
  else { fail++; console.log("  FAIL  " + label); }
}

function sseOf(text) {
  return 'data: {"choices":[{"delta":{"content":"' + text + '"}}]}\n\ndata: [DONE]\n\n';
}

// 打开一个「干净的」应用状态
function reset() {
  calls.length = 0;
  nextSse = sseOf("这是回答。");
  byId.clear();
  T.state.ai.key = "test-key";
  T.state.ai.base = "https://api.example.com/v1";
  T.state.ai.model = "MiniMax-M3.1-Flash";
  T.state.notes.clear();
  T.state.notes.set("note-1", { id: "note-1", title: "测试笔记", body: "笔记正文", rel: "note-1.md" });
  T.state.currentId = "note-1";
  T.state.aiHistory.clear();
}

/** 收集一棵 DOM 树里出现过的所有标签名 */
function collectTags(node, out) {
  out = out || [];
  if (!node) return out;
  if (node.tagName) out.push(node.tagName);
  (node.children || []).forEach((c) => collectTags(c, out));
  return out;
}

/** 收集一棵 DOM 树里所有元素的 _text（文本节点内容） */
function collectText(node, out) {
  out = out || [];
  if (!node) return out;
  if (node._text) out.push(node._text);
  (node.children || []).forEach((c) => collectText(c, out));
  return out;
}

(async function main() {
  console.log("\n[1] 发送之后输入栏必须清空（用户明确要求）");
  reset();
  getEl("ai-question").value = "  磁电传感器的工作原理是什么？  ";
  await T.runAI("ask");
  check(getEl("ai-question").value === "",
    "输入栏已清空（实际: " + JSON.stringify(getEl("ai-question").value) + "）");
  check(calls.length === 1, "确实发出去了 1 次请求");
  check(calls[0].payload.messages.some((m) => m.content.includes("磁电传感器的工作原理是什么？")),
    "请求里带上了用户的问题（两边都 trim 过）");

  console.log("\n[2] 问题不会因为清空输入栏而丢失");
  reset();
  getEl("ai-question").value = "TMR 和 SOT 的区别？";
  await T.runAI("ask");
  const turns = getEl("ai-output").children;
  check(turns.length >= 1, "对话区有内容");
  const turnText = JSON.stringify(turns.map((t) => ({
    cls: t.className, txt: t._text, kids: t.children.map((c) => c._text),
  })));
  check(turnText.includes("TMR 和 SOT 的区别？"),
    "提问作为一条记录留在对话区里");

  console.log("\n[3] 没输入 / 没配 Key 时不能发");
  reset();
  await T.runAI("ask");
  check(calls.length === 0, "问题为空时没有发请求");
  reset();
  T.state.ai.key = "";
  getEl("ai-question").value = "随便问问";
  await T.runAI("ask");
  check(calls.length === 0, "没有 API Key 时没有发请求");
  check(getEl("ai-question").value === "随便问问",
    "没发出去时输入栏保持原样（不能把用户写的问题吃掉）");

  console.log("\n[4] 多轮上下文：追问要带上前文");
  reset();
  getEl("ai-question").value = "第一个问题";
  await T.runAI("ask");
  getEl("ai-question").value = "第二个问题";
  await T.runAI("ask");
  check(calls.length === 2, "发了 2 次");
  const msgs = calls[1].payload.messages;
  check(msgs[0].role === "system", "system 消息在最前面");
  check(msgs.some((m) => m.content === "第一个问题"), "第 2 次请求带上了第 1 轮的问题");
  check(msgs.some((m) => m.content === "这是回答。"), "第 2 次请求带上了第 1 轮的回答");
  const lastUser = msgs[msgs.length - 1];
  check(lastUser.role === "user" && lastUser.content.includes("第二个问题"),
    "本次问题在最后一条");

  console.log("\n[5] 提问内容不会被当成 HTML");
  reset();
  const nasty = '<img src=x onerror="alert(1)">';
  getEl("ai-question").value = nasty;
  await T.runAI("ask");
  const outEl = getEl("ai-output");
  const tags = collectTags(outEl);
  check(tags.indexOf("IMG") < 0, "没有真的创建 img 元素（标签: " + tags.join(",") + "）");
  check(collectText(outEl).indexOf(nasty) >= 0,
    "提问被当作纯文本原样保存（textContent，不是 innerHTML）");
  check(getEl("ai-question").value === "", "输入栏照样清空");

  console.log("\n[6] Enter 发送 / Shift+Enter 换行 / 输入法选词时不发送");
  reset();
  const mkEvent = (opts) => {
    const e = Object.assign({
      key: "Enter", shiftKey: false, ctrlKey: false, metaKey: false,
      isComposing: false,
    }, opts || {});
    e.prevented = false;
    e.preventDefault = function () { this.prevented = true; };
    return e;
  };
  const tick = () => new Promise((r) => setTimeout(r, 0));

  getEl("ai-question").value = "回车发送的问题";
  const e1 = mkEvent();
  T.aiOnKeyDown(e1);
  await tick();
  check(calls.length === 1, "Enter 触发了发送");
  check(e1.prevented === true, "Enter 被 preventDefault（不会在输入框里换行）");
  check(getEl("ai-question").value === "", "Enter 发送后输入栏也清空了");

  const e2 = mkEvent({ isComposing: true });
  T.aiOnKeyDown(e2);
  await tick();
  check(calls.length === 1, "输入法正在选词时的 Enter 不发送（中文用户必踩的坑）");
  check(e2.prevented === false, "选词时的 Enter 没有被拦截");

  const e3 = mkEvent({ shiftKey: true });
  T.aiOnKeyDown(e3);
  await tick();
  check(calls.length === 1, "Shift+Enter 是换行，不发送");

  console.log("\n[7] 清空对话");
  reset();
  getEl("ai-question").value = "问题";
  await T.runAI("ask");
  check(getEl("ai-output").children.length > 0, "先有一条对话");
  T.state.aiHistory.clear();
  getEl("ai-output").innerHTML = "";
  check(getEl("ai-output").children.length === 0, "清空后对话区为空");

  console.log("\n" + "=".repeat(60));
  console.log("PASS " + pass + "  FAIL " + fail);
  process.exit(fail === 0 ? 0 : 1);
})();
