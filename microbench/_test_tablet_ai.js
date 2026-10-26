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
  T.state.ai.modelT = "MiniMax-M3";
  T.state.notes.clear();
  T.state.notes.set("note-1", { id: "note-1", title: "测试笔记", body: "笔记正文", rel: "note-1.md" });
  T.state.currentId = "note-1";
  T.state.aiHistory.clear();
  T.resetThrottle();          // 别让限流器把测试拖慢
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

  console.log("\n[8] 429 自动换模型（交接文档 §2 ② —— 平板最该做的一条）");
  reset();
  const realFetch = global.fetch;
  let hit = 0;
  global.fetch = async (url, opts) => {
    hit++;
    if (hit === 1) {
      calls.push({ url, opts, payload: JSON.parse(opts.body) });
      return {
        ok: false,
        status: 429,
        text: async () => '{"error":{"message":"RateLimitError: too many requests"}}',
      };
    }
    return realFetch(url, opts);
  };
  getEl("ai-question").value = "主模型额度用完了怎么办";
  await T.runAI("ask");
  check(hit === 2, "429 之后自动重试了一次（实际请求 " + hit + " 次）");
  check(calls.length === 2, "两次请求都被记下来了");
  check(calls[0].payload.model === "MiniMax-M3.1-Flash",
    "第一次用的是主模型（" + calls[0].payload.model + "）");
  check(calls[1].payload.model === "MiniMax-M3",
    "重试时换成了另一个模型（" + calls[1].payload.model + "）");
  const outEl8 = getEl("ai-output");
  const turn8 = outEl8.children[outEl8.children.length - 1];
  const st8 = (turn8.children[0] || {})._text || "";
  check(st8.indexOf("完成") >= 0 && st8.indexOf("MiniMax-M3") >= 0,
    "完成后说的是**实际用的那个**模型 -> " + st8);
  check(st8.indexOf("限流") < 0 && st8.indexOf("失败") < 0,
    "换模型成功后不该再报错 -> " + st8);
  global.fetch = realFetch;

  console.log("\n[9] 两个模型都 429 时只试一次就报错（不能无限重试）");
  reset();
  let hit2 = 0;
  global.fetch = async (url, opts) => {
    hit2++;
    calls.push({ url, opts, payload: JSON.parse(opts.body) });
    return {
      ok: false,
      status: 429,
      text: async () => '{"error":{"message":"Token Plan limit exceeded"}}',
    };
  };
  getEl("ai-question").value = "两个模型都没额度";
  await T.runAI("ask");
  check(hit2 === 2, "最多试 2 次（主模型 + 备用），实际 " + hit2 + " 次");
  const outEl9 = getEl("ai-output");
  const turn9 = outEl9.children[outEl9.children.length - 1];
  const st9 = (turn9.children[0] || {})._text || "";
  check(st9.indexOf("失败") >= 0, "最终照实报错 -> " + st9);
  check(st9.indexOf("额度") >= 0, "说的是「额度用完」而不是「限流」 -> " + st9);
  global.fetch = realFetch;

  console.log("\n[10] 等待期看得见「已用时」（用户原话：等了半天什么也没输出）");
  reset();
  const realFetch2 = global.fetch;
  global.fetch = async (url, opts) => {
    await new Promise((r) => setTimeout(r, 1200));
    return realFetch2(url, opts);
  };
  getEl("ai-question").value = "慢一点的请求";
  const pending = T.runAI("ask");
  await new Promise((r) => setTimeout(r, 1100));
  const outEl10 = getEl("ai-output");
  const turn10 = outEl10.children[outEl10.children.length - 1];
  const st10 = (turn10.children[0] || {})._text || "";
  check(/\d+\s*s/.test(st10), "等待期状态条带已用时 -> " + JSON.stringify(st10));
  await pending;
  const st10b = (turn10.children[0] || {})._text || "";
  check(st10b.indexOf("思考中") < 0, "结束后状态条不再显示「思考中」 -> " + st10b);
  global.fetch = realFetch2;

  console.log("\n[11] 按模型限流（平板直连，得自己卡）");
  check(T.rpmFor("Agnes-3.0-Flash") === 11, "Agnes 记 11 RPM（免费版额度小）");
  check(T.rpmFor("MiniMax-M3") === 30, "普通模型记 30 RPM");
  T.resetThrottle();
  const t0 = Date.now();
  await T.throttleModel("MiniMax-M3");
  await T.throttleModel("MiniMax-M3");
  const dt = Date.now() - t0;
  check(dt >= 1500, "同一模型连发两次，第二次被限流器挡住（等了 " + dt + "ms）");
  T.resetThrottle();

  // ==================================================================
  // [12] 思考预算（抄电脑端 AGENTS.md §4.38，2026-10-27）
  //
  // 根因：M3.1-Flash **强制思考**，思考 token 与正文**共用**同一个
  // max_tokens 预算池 → 预算给小了，正文一个字都出不来。
  // 电脑端实测（7K 上下文 + 生产提示词 + 真实模型）：
  //     max_tokens=4000  → 59.9s / 正文 **0 字**
  //     max_tokens=16000 → 86.2s / 正文 **4884 字**
  // 平板是直连模型的（不走后端），所以这套预算必须自己写一份。
  // ==================================================================
  console.log("\n[12] 思考预算：答疑必须给足「思考 + 正文」的额度");
  reset();
  getEl("ai-question").value = "磁电传感器的工作原理是什么？";
  await T.runAI("ask");
  check(calls[0].payload.max_tokens >= 16000,
    "答疑的 max_tokens >= 16000（实际 " + calls[0].payload.max_tokens + "）");
  check(T.AI_QA_MAX_TOKENS >= 16000,
    "AI_QA_MAX_TOKENS = " + T.AI_QA_MAX_TOKENS + "（实测需要 >=16000）");

  console.log("\n[13] 翻译的预算按「正文」算，不能照抄答疑的数");
  reset();
  global.getSelection = () => ({ toString: () => "选中的论文段落" });
  await T.runAI("translate");
  check(calls[0].payload.max_tokens === 4000,
    "翻译用 4000（直译型模型关思考，按正文算）→ 实际 " + calls[0].payload.max_tokens);
  check(calls[0].payload.thinking && calls[0].payload.thinking.type === "disabled",
    "翻译关掉思考（MiniMax-M3 支持）");
  global.getSelection = () => ({ toString: () => "" });

  console.log("\n[14] 强制思考模型（M3.1-Flash）不能发 thinking=disabled（上游会 400）");
  check(T.modelForcesThinking("MiniMax-M3.1-Flash") === true,
    "M3.1-Flash 被认成强制思考模型");
  check(T.modelForcesThinking("MiniMax-M3") === false,
    "M3 不是强制思考模型");
  check(T.shouldSendThinkingDisabled("https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash") === false,
    "M3.1-Flash 不发 thinking=disabled");
  check(T.shouldSendThinkingDisabled("https://api.minimaxi.com/v1", "MiniMax-M3") === true,
    "MiniMax-M3 发 thinking=disabled（省时）");
  check(T.shouldSendThinkingDisabled("https://apihub.agnes-ai.com/v1", "Agnes-3.0-Flash") === false,
    "Agnes 不发 thinking=disabled（传了会静默吃光预算 → 正文 0 字）");
  check(T.shouldSendThinkingDisabled("https://api.deepseek.com/v1", "deepseek-flash") === true,
    "DeepSeek 必须发 thinking=disabled（不传反而正文 0 字）");

  console.log("\n[15] 思考强度 reasoning_effort（抄电脑端 §4.39）");
  check(T.reasoningEffortFor("https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash", "low") === "low",
    "MiniMax 网关拿得到 low");
  check(T.reasoningEffortFor("https://api.deepseek.com/v1", "deepseek-flash", "low") === "low",
    "DeepSeek 网关拿得到 low");
  check(T.reasoningEffortFor("https://example.com/v1", "my-model", "low") === null,
    "未知网关**不发** reasoning_effort（判错的后果是 HTTP 400）");
  check(T.reasoningEffortFor("https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash", "none") === "low",
    "强制思考模型拿 none 会 400 → 自动降级成 low");
  reset();
  getEl("ai-question").value = "随便问一句";
  await T.runAI("ask");
  check(calls[0].payload.reasoning_effort === undefined,
    "未知网关的请求体里没有 reasoning_effort（测试用的 base 是 example.com）");

  console.log("\n[16] 空正文要自己救一次：抬预算重试（不能只提示不改）");
  reset();
  let emptyHit = 0;
  const realFetch3 = global.fetch;
  global.fetch = async (url, opts) => {
    emptyHit++;
    const payload = JSON.parse(opts.body);
    if (emptyHit === 1) {
      // 第一次：只有思考，没有正文（预算被思考吃光的典型症状）
      calls.push({ url, opts, payload });
      const enc = new TextEncoder();
      const sse = 'data: {"choices":[{"delta":{"reasoning_content":"想了很久"}}]}\n\ndata: [DONE]\n\n';
      return {
        ok: true, status: 200,
        body: { getReader() { let d = false; return {
          async read() { if (d) return { done: true, value: undefined };
            d = true; return { done: false, value: enc.encode(sse) }; } }; } },
        text: async () => sse,
      };
    }
    // 第二次：正文出来了
    // ⚠ 不要再手动 push —— realFetch3 自己会记账（手动 push 会记成 3 次）
    return realFetch3(url, opts);
  };
  getEl("ai-question").value = "这个问题让模型想了很久";
  await T.runAI("ask");
  check(emptyHit === 2, "正文为空后自动重试了一次（实际请求 " + emptyHit + " 次）");
  check(calls.length === 2, "两次请求都记下来了");
  check(calls[1].payload.max_tokens > calls[0].payload.max_tokens,
    "重试时抬高了 max_tokens（" + calls[0].payload.max_tokens + " -> " + calls[1].payload.max_tokens + "）");
  check(calls[1].payload.max_tokens <= 32000,
    "抬到上限就停（<= 32000），别无限抬");
  check(calls[1].payload.thinking === undefined,
    "强制思考模型（M3.1-Flash）重试时**不**发 thinking=disabled（那是空操作）");
  const outEl16 = getEl("ai-output");
  const turn16 = outEl16.children[outEl16.children.length - 1];
  const st16 = (turn16.children[0] || {})._text || "";
  check(st16.indexOf("完成") >= 0, "重试成功后显示完成 -> " + st16);
  global.fetch = realFetch3;

  console.log("\n[17] 能关思考的模型：空正文时关掉思考重试（而不是原样重发）");
  reset();
  let emptyHit2 = 0;
  const realFetch4 = global.fetch;
  global.fetch = async (url, opts) => {
    emptyHit2++;
    const payload = JSON.parse(opts.body);
    calls.push({ url, opts, payload });
    if (emptyHit2 === 1) {
      const enc = new TextEncoder();
      const sse = 'data: {"choices":[{"delta":{"reasoning_content":"空转"}}]}\n\ndata: [DONE]\n\n';
      return {
        ok: true, status: 200,
        body: { getReader() { let d = false; return {
          async read() { if (d) return { done: true, value: undefined };
            d = true; return { done: false, value: enc.encode(sse) }; } }; } },
        text: async () => sse,
      };
    }
    return realFetch4(url, opts);
  };
  // 把主模型换成 MiniMax-M3（能关思考）
  T.state.ai.model = "MiniMax-M3";
  getEl("ai-question").value = "换个模型试试";
  await T.runAI("ask");
  check(emptyHit2 === 2, "空正文后重试了一次（实际 " + emptyHit2 + " 次）");
  check(calls[1].payload.thinking && calls[1].payload.thinking.type === "disabled",
    "重试时关掉了思考（MiniMax-M3 支持）");
  global.fetch = realFetch4;

  console.log("\n[18] 重答不死锁：新请求替换旧请求，旧回调不许覆盖新答案区");
  reset();
  const realFetch5 = global.fetch;
  let seq = 0;
  global.fetch = async (url, opts) => {
    seq++;
    const mine = seq;
    const enc = new TextEncoder();
    if (mine === 1) {
      // 第一个请求：慢，等它被第二个请求顶掉
      await new Promise((r) => setTimeout(r, 400));
      const sse = sseOf("旧请求的答案");
      return {
        ok: true, status: 200,
        body: { getReader() { let d = false; return {
          async read() { if (d) return { done: true, value: undefined };
            d = true; return { done: false, value: enc.encode(sse) }; } }; } },
        text: async () => sse,
      };
    }
    return realFetch5(url, opts);
  };
  getEl("ai-question").value = "第一个问题";
  const p1 = T.runAI("ask");
  await new Promise((r) => setTimeout(r, 80));
  getEl("ai-question").value = "第二个问题";
  const p2 = T.runAI("ask");
  await Promise.all([p1, p2]);
  const outEl18 = getEl("ai-output");
  const turns18 = outEl18.children;
  const lastTurn18 = turns18[turns18.length - 1];
  const lastSt18 = (lastTurn18.children[0] || {})._text || "";
  const lastBuf18 = (lastTurn18.children[1] || {})._html || "";
  check(lastBuf18.indexOf("这是回答。") >= 0,
    "新请求的答案区是**新答案**，没被旧请求覆盖 -> " + JSON.stringify(lastBuf18));
  check(lastSt18.indexOf("已完成") < 0 && lastSt18.indexOf("已停止") < 0,
    "新请求的状态条没被旧请求写成「已停止」-> " + lastSt18);
  global.fetch = realFetch5;

  console.log("\n[19] 能力边界：平板端是直连模型的，提示词必须自己写一份");
  reset();
  getEl("ai-question").value = "帮我上网找张实物图";
  await T.runAI("ask");
  const sysMsg = calls[0].payload.messages[0];
  check(sysMsg.role === "system", "第一条是 system 提示词");
  check(sysMsg.content.indexOf("不能联网") >= 0, "提示词写明了不能联网");
  check(sysMsg.content.indexOf("必须回答") >= 0, "提示词要求「必须回答」（不许交白卷）");
  check(sysMsg.content.indexOf("最后一句") >= 0, "能力边界放最后一句（用户明确改过口径）");

  console.log("\n" + "=".repeat(60));
  console.log("PASS " + pass + "  FAIL " + fail);
  process.exit(fail === 0 ? 0 : 1);
})();
