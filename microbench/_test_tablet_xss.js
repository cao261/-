/*
 * 平板工作台的 XSS 载荷测试 —— 交接文档 §8.3 点名要补的那条。
 *
 * 为什么要单独一条：
 *   平板端 `static/tablet/app.js` 是**独立前端**（不共用桌面的 index.html），
 *   桌面端那轮安全修复一行都不过来。而且平板这边历来**零载荷测试**：
 *   原来的 `_test_tablet_hardening.js` 只测了 markdown 插值路径，
 *   消毒器本身没被任何载荷碰过 ——
 *   于是 `esc()` 的 `<` / `>` 转义坏了一年多都没人发现。
 *
 * 测的是**发布出去的那份代码**：
 *   这个 app.js 会被 export_offline.py 原样内联进平板单文件 HTML，
 *   再打进 APK 的 assets/index.html。所以 require() 它 = 测发布物。
 *
 * 载荷清单按交接文档的要求：
 *   5 个容器标签（xmp / noembed / noframes / plaintext / title）
 *   + onerror / onload / javascript: / <iframe> / style= 注入
 *   + **未闭合标签**（没有 `>`）—— 浏览器会在 EOF 处隐式闭合它
 * 并**同时断言正常内容没被误伤**（表格 / 代码块 / class="katex" / & 实体
 *   / vault 真实在用的那批标签）。
 *
 * Run:  node microbench/_test_tablet_xss.js
 */
const path = require("path");
const fs = require("fs");

// ---- DOM shim（只求 require() 不炸，boot 不跑） ----------------------------
function mkEl(tag) {
  return {
    tagName: String(tag || "").toUpperCase(),
    className: "", _text: "", _html: "", children: [], style: {},
    hidden: false, value: "", scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    offsetWidth: 0, listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; },
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
  addEventListener: () => {},          // ← 关键：不触发 boot()
  getElementById: (id) => getEl(id),
  createElement: (tag) => mkEl(tag),
  documentElement: { setAttribute() {}, getAttribute: () => null },
  body: mkEl("body"),
};
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.matchMedia = () => ({ matches: false });
global.getSelection = () => ({ toString: () => "", rangeCount: 0, rangeGetAt: () => ({}) });
global.katex = {
  renderToString(tex, opts) {
    return `<span class="katex${opts && opts.displayMode ? " katex-display" : ""}" data-tex="${tex}"></span>`;
  },
};

const APP = path.join(__dirname, "static", "tablet", "app.js");

/* ------------------------------------------------------------------
 * 切**发布出去的那份源码**（交接文档 §8.3 要求的 sliceFn 做法）。
 *
 * 为什么不用 require() 就完事：
 *   require() 测的是「整个模块跑起来之后」的行为 —— 一旦某段代码被改坏，
 *   往往先在 export 上炸掉，测试变成「函数不存在」的崩溃，
 *   而不是「断言抓到 bug」。切片能对**每一段逻辑**单独下断言。
 *
 * 切片范围：E_AMP 常量 → esc → 消毒白名单 → sanitizeHtml → safeHref
 *          → safeErr → friendlyHttpError
 *   这一段在源码里是**连续的**，所以一次切出来就是自洽的。
 *   唯一的外部依赖是 safeErr 里读到的 state.ai.key —— 用桩注入。
 * ------------------------------------------------------------------ */
const SRC = fs.readFileSync(APP, "utf8").replace(/\r\n/g, "\n");

/**
 * 切一段源码出来。
 * 找不到就返回 null —— **不要抛异常**：
 *   抛异常会让测试「崩掉」而不是「判红」，
 *   而崩溃证明不了「断言能抓到 bug」。
 */
function sliceBlock(from, toFn) {
  const s = SRC.indexOf(from);
  if (s < 0) return null;
  const f = SRC.indexOf("function " + toFn + "(");
  if (f < 0) return null;
  const e = SRC.indexOf("\n  }\n", f);
  if (e < 0) return null;
  return SRC.slice(s, e + "\n  }\n".length);
}

// state 桩：safeErr 会读 state.ai.key
const STATE_STUB = { ai: { base: "", key: "", model: "", modelT: "" } };

// 核心段（esc + 白名单常量 + sanitizeHtml + safeHref）：旧代码里也有 →
// 断言能在旧代码上**干净地判红**，这才是有效的反向验证。
const coreSrc = sliceBlock("const E_AMP = String.fromCharCode(38);", "safeHref");
if (!coreSrc) {
  console.error("FAIL: 切不出核心消毒代码（app.js 结构变了？）");
  process.exit(1);
}
const S = new Function("state",
  coreSrc + "\nreturn {sanitizeHtml, safeHref, esc};")(STATE_STUB);

// 护栏段（safeErr / friendlyHttpError）：新加的 → 旧代码里没有，
// 缺失时下面那两节要**报 FAIL**（护栏没接上），而不是崩掉。
const extraSrc = sliceBlock("function safeErr(", "friendlyHttpError");
const X = extraSrc
  ? new Function("state", extraSrc + "\nreturn {safeErr, friendlyHttpError};")(STATE_STUB)
  : null;

// 同时用 require() 跑一遍**真实模块**（渲染出口那一段必须走真模块）
require(APP);
const T = global.__MB_TEST__;
if (!T) {
  console.error("FAIL: app.js 没有暴露 __MB_TEST__");
  process.exit(1);
}

let pass = 0, fail = 0;
function check(cond, label, extra) {
  if (cond) { pass++; console.log("  PASS  " + label); }
  else { fail++; console.log("  FAIL  " + label + (extra ? "  ->  " + extra : "")); }
}

/* ------------------------------------------------------------------
 * 判定：输出里还有没有「浏览器会当成真标签」的构造。
 *
 * ⚠ 不能用 `/<script/i.test(out)` 这种粗判 ——
 *   转义后的文本 `&lt;script&gt;` 里也有 "script"，
 *   会把「安全地显示成文字」误判成「漏了」。
 *   所以这里按浏览器的宽松规则解析一遍：
 *   `<` + 字母 → 标签开始；属性按 HTML 规则读；
 *   标签没被 `>` 闭合时，浏览器会在 EOF 处隐式闭合它。
 * ------------------------------------------------------------------ */
const SAFE_TAGS = new Set([
  "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr",
  "strong", "b", "em", "i", "u", "s", "del", "mark", "small", "sub", "sup",
  "code", "pre", "blockquote", "ul", "ol", "li",
  "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
  "a", "img", "span", "div", "figure", "figcaption",
  "details", "summary", "dl", "dt", "dd", "abbr", "cite", "q",
]);

function liveTags(html) {
  const out = [];
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf("<", i);
    if (lt < 0) break;
    const m = /^<([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|'[^']*'|[^>"'])*)/.exec(html.slice(lt));
    if (!m) { i = lt + 1; continue; }
    const tag = m[1].toLowerCase();
    const closed = html[lt + m[0].length] === ">";
    const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))/g;
    const attrs = [];
    let am;
    while ((am = attrRe.exec(m[2] || "")) !== null) {
      const name = am[1].toLowerCase();
      const val = am[3] != null ? am[3] : (am[4] != null ? am[4] : (am[5] || ""));
      attrs.push([name, val]);
    }
    const bad = [];
    attrs.forEach(([n, v]) => {
      if (/^on/.test(n)) bad.push(n);
      else if (n === "style") bad.push("style");
      else if ((n === "href" || n === "src" || n === "xlink:href")
               && /^\s*(javascript|vbscript|data):/i.test(v)) bad.push(n + "=" + v);
    });
    if (!SAFE_TAGS.has(tag) || bad.length) {
      out.push({ tag, closed, bad, attrs: attrs.map((a) => a[0]) });
    }
    i = lt + m[0].length + (closed ? 1 : 0);
  }
  return out;
}

console.log("\n[1] XSS 载荷：消毒器直接吃");
const payloads = [
  ["onerror（闭合）", '<img src=x onerror=alert(1)>'],
  ["onerror（缺 >）", '<img src=x onerror=alert(1)'],
  ["onerror（缺 > + 尾文本）", '<img src=x onerror=alert(1) tail'],
  ["onload（闭合）", '<a onload=alert(1) href="#">x</a>'],
  ["onload（缺 >）", '<a onload=alert(1) href="#"'],
  ["onclick（缺 >）", '<p onclick=alert(1)'],
  ["svg onload（闭合）", '<svg onload=alert(1)>'],
  ["svg onload（缺 >）", '<svg onload=alert(1)'],
  ["<script>", '<script>alert(1)</script>'],
  ["<script> 无闭合", '<script>alert(1)'],
  ["<iframe>", '<iframe src="javascript:alert(1)"></iframe>'],
  ["style= 注入", '<div style="position:fixed;top:0;left:0;width:100%;height:100%">盖住全屏</div>'],
  ["javascript: 链接", '<a href="javascript:alert(1)">点</a>'],
  ["实体绕过的 javascript:", '<a href="jav&#x61;script:alert(1)">点</a>'],
  ["制表符绕过的 javascript:", '<a href="java\tscript:alert(1)">点</a>'],
  ["data: 链接", '<a href="data:text/html,<script>alert(1)</script>">点</a>'],
  ["容器 xmp", '<xmp><img src=x onerror=alert(1)></xmp>'],
  ["容器 noembed", '<noembed><img src=x onerror=alert(1)></noembed>'],
  ["容器 noframes", '<noframes><img src=x onerror=alert(1)></noframes>'],
  ["容器 plaintext", '<plaintext><img src=x onerror=alert(1)>'],
  ["容器 title", '<title><img src=x onerror=alert(1)></title>'],
  ["容器 textarea", '<textarea><img src=x onerror=alert(1)></textarea>'],
  ["body onload", '<body onload=alert(1)>'],
  ["meta refresh", '<meta http-equiv="refresh" content="0;url=javascript:alert(1)">'],
  ["base href", '<base href="javascript:alert(1)//">'],
  ["换行分隔属性", '<img\nsrc=x\nonerror=alert(1)>'],
  ["大小写混淆", '<IMG SRC=x ONERROR=alert(1)>'],
  ["slash 分隔属性", '<img/src=x/onerror=alert(1)>'],
  ["注释遮蔽", '<!--><img src=x onerror=alert(1)>-->'],
  ["嵌套引号", '<a href="x" title=\'<img src=x onerror=alert(1)>\'>t</a>'],
];
payloads.forEach(([label, p]) => {
  const out = S.sanitizeHtml(p);
  const live = liveTags(out);
  check(live.length === 0,
    label + " 没有活标签",
    live.length ? JSON.stringify(live) + "  出=" + out : "出=" + out);
});

console.log("\n[2] XSS 载荷：经由 renderMarkdown（真实渲染出口）");
payloads.forEach(([label, p]) => {
  const out = T.renderMarkdown(p);
  const live = liveTags(out);
  check(live.length === 0,
    label + " 在渲染结果里没有活标签",
    live.length ? JSON.stringify(live) + "  出=" + out : "出=" + out);
});

console.log("\n[3] 代码块里的 <script> 不能真的执行");
// 原来 esc() 的 < 转义是坏的（E_LT + "lt;" -> "<lt;"），
// 代码块走的是 esc() 而不是消毒器 → 里面能塞活的 <script>。
const cb = T.renderMarkdown("```html\n<script>alert(1)</script>\n```");
check(cb.indexOf("<pre><code") >= 0, "代码块渲染成 <pre><code>", cb.slice(0, 120));
check(!/<script/i.test(cb), "代码块里没有活的 <script>", cb.slice(0, 160));
check(cb.indexOf("&lt;script") >= 0, "代码块里的 <script> 被转义成文本", cb.slice(0, 160));

console.log("\n[4] 正常内容不能被误伤");
const table = [
  "| 性能 | BaTiO3 | PZT-5 |",
  "|---|---|---|",
  "| d31 (pC/N) | 90 | 175 |",
  "| Tc (°C) | 152 | 360 |",
].join("\n");
const th = T.renderMarkdown(table);
check(th.indexOf("<table") >= 0 && th.indexOf("<th>") >= 0, "表格还在", th.slice(0, 160));
check(th.indexOf("<td>175</td>") >= 0, "表格单元格值还在", th.slice(0, 200));

const katex = T.renderMarkdown("其中 $E = mc^2$ 成立");
check(katex.indexOf("katex") >= 0, "公式渲染成 KaTeX", katex.slice(0, 140));
check(katex.indexOf('class="katex') >= 0, "KaTeX 的 class 保留", katex.slice(0, 140));

const ent = T.renderMarkdown("AT&T 的 d31 是 90 pC/N");
check(ent.indexOf("ATT") < 0, "实体没被吃掉（AT&T 不能变成 ATT）", ent);
check(ent.indexOf("AT&T") >= 0 || ent.indexOf("AT&amp;T") >= 0, "AT&T 保留", ent);

// vault 里真实在用的那批标签 —— 白名单必须都放行，
// 否则修好转义之后**现有笔记会变成一堆字面文本**（这是本次改动的回归红线）。
const vaultTags = ["br", "p", "strong", "div", "details", "b", "summary", "h2", "h3", "h4"];
vaultTags.forEach((tag) => {
  const out = S.sanitizeHtml("<" + tag + ">正文</" + tag + ">");
  check(out.indexOf("<" + tag) >= 0 && out.indexOf("&lt;" + tag) < 0,
    "vault 在用的 <" + tag + "> 没被误转义", out);
});
const voidTags = ["br", "img"];
voidTags.forEach((tag) => {
  const out = S.sanitizeHtml("<" + tag + ">");
  check(out.indexOf("&lt;") < 0, "vault 在用的 <" + tag + "> 没被误转义", out);
});
// 闭合标签也要放行
check(S.sanitizeHtml("</p>").indexOf("</p>") >= 0, "闭合标签 </p> 放行",
  S.sanitizeHtml("</p>"));

console.log("\n[5] esc() 的转义（这三个字符以前全是坏的）");
const esc = S.esc;
check(esc("<") === "&lt;", "esc('<') == &lt;", esc("<"));
check(esc(">") === "&gt;", "esc('>') == &gt;", esc(">"));
check(esc("&") === "&amp;", "esc('&') == &amp;", esc("&"));
check(esc('"') === "&quot;", "esc('\"') == &quot;", esc('"'));
check(esc("a<b>c&d\"e") === "a&lt;b&gt;c&amp;d&quot;e",
  "esc 组合起来正确", esc("a<b>c&d\"e"));

console.log("\n[6] 错误出口：不能带出密钥 / 绝对路径");
if (!X) {
  check(false, "护栏没接上：app.js 里没有 safeErr（错误出口必须脱敏）");
} else {
  const keyErr = X.safeErr("失败：https://user:sk-abcdef123456@gw.internal/v1/chat");
  check(keyErr.indexOf("sk-abcdef123456") < 0, "URL userinfo 里的 key 被抹掉", keyErr);
  check(keyErr.indexOf("***") >= 0, "抹掉的位置留了标记", keyErr);
  const pathErr = X.safeErr("打不开 E:\\Users\\cao\\AppData\\notes.json");
  check(pathErr.indexOf("cao") < 0, "绝对路径被抹掉", pathErr);
  const jsonKey = X.safeErr('{"api_key":"sk-zzzzzzzzzzzz","code":1}');
  check(jsonKey.indexOf("sk-zzzzzzzzzzzz") < 0, "JSON 形态的 key 被抹掉", jsonKey);
}

console.log("\n[7] 上游 429 要说人话（配额 vs 限流要分开）");
if (!X) {
  check(false, "护栏没接上：app.js 里没有 friendlyHttpError（429 必须说人话）");
} else {
  const rateLimited = X.friendlyHttpError(429, '{"error":{"message":"RateLimitError: too many requests"}}');
  check(rateLimited.indexOf("限流") >= 0, "限流说成「限流」", rateLimited);
  check(rateLimited.indexOf("配额") < 0, "限流**不**说成「配额耗尽」", rateLimited);
  const quota = X.friendlyHttpError(429,
    '降格模型调用失败: HTTP 429: Too Many Requests (原主模型报错: HTTP 429: Token Plan limit exceeded)');
  check(quota.indexOf("配额") >= 0, "配额耗尽说成「配额」", quota);
  check(quota.indexOf("限流") < 0, "配额耗尽**不**说成「限流」", quota);
  const auth = X.friendlyHttpError(401, "invalid api key");
  check(auth.indexOf("key") >= 0 || auth.indexOf("鉴权") >= 0, "401 说人话", auth);
  check(X.friendlyHttpError(0, "网络断了").length > 0, "没有状态码也有话说");
}

console.log("\n" + "=".repeat(60));
console.log("PASS " + pass + "  FAIL " + fail);
process.exit(fail === 0 ? 0 : 1);
