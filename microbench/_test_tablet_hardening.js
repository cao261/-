/*
 * 平板工作台的「数据安全 / 不崩」回归测试。
 *
 * 这几条都是**静默**出问题的类型 —— 页面上看不出坏了，
 * 只有数据丢了、或者笔记打不开才知道。所以必须有护栏。
 *
 *  1. 种子不能覆盖本地正文（刷新丢改动 + 旧内容写回云端）
 *  2. Markdown 的 alt / href 不能变成 HTML 属性（XSS）
 *  3. 公式落在引用块 / 多行列表里不能把整篇笔记渲染崩
 *  4. PDF 划词气泡的监听不能绑在会被清掉的节点上
 *
 * Run:  node microbench/_test_tablet_hardening.js
 */
const path = require("path");
const fs = require("fs");

// ---- DOM shim -------------------------------------------------------------
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
    scrollHeight: 0,
    clientHeight: 0,
    offsetWidth: 0,
    listeners: {},
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

// ---- 假 IndexedDB ---------------------------------------------------------
const STORE_KEYS = { notes: "id", meta: "k", pdfcache: "id" };

function fakeIndexedDB() {
  const data = { notes: new Map(), meta: new Map(), pdfcache: new Map() };

  function makeReq(result) {
    const r = { result, onsuccess: null, onerror: null };
    setTimeout(() => { if (r.onsuccess) r.onsuccess(); }, 0);
    return r;
  }

  function makeStore(name) {
    const key = STORE_KEYS[name];
    return {
      put(v) { data[name].set(v[key], JSON.parse(JSON.stringify(v))); return makeReq(undefined); },
      get(k) { return makeReq(data[name].get(k)); },
      getAll() { return makeReq([...data[name].values()].map((x) => JSON.parse(JSON.stringify(x)))); },
      getAllKeys() { return makeReq([...data[name].keys()]); },
      delete(k) { data[name].delete(k); return makeReq(undefined); },
      clear() { data[name].clear(); return makeReq(undefined); },
      createIndex() { return {}; },
    };
  }

  function makeTx() {
    const t = { objectStore: makeStore, oncomplete: null, onerror: null, onabort: null };
    setTimeout(() => { if (t.oncomplete) t.oncomplete(); }, 0);
    return t;
  }

  const db = {
    objectStoreNames: { contains: (n) => Object.prototype.hasOwnProperty.call(data, n) },
    createObjectStore(name) { data[name] = new Map(); return makeStore(name); },
    transaction() { return makeTx(); },
  };

  return {
    data,
    api: {
      open() {
        const req = { result: db, onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null };
        setTimeout(() => {
          if (req.onupgradeneeded) req.onupgradeneeded();
          if (req.onsuccess) req.onsuccess();
        }, 0);
        return req;
      },
    },
  };
}

const fakeDb = fakeIndexedDB();
global.indexedDB = fakeDb.api;
global.window = global;
global.document = {
  addEventListener: () => {},
  getElementById: (id) => getEl(id),
  createElement: (tag) => mkEl(tag),
  documentElement: { setAttribute() {}, getAttribute: () => null },
  body: mkEl("body"),
};
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.matchMedia = () => ({ matches: false });
global.getSelection = () => ({ toString: () => "", rangeCount: 0, getRangeAt: () => ({ getBoundingClientRect: () => ({}) }) });
global.katex = {
  renderToString(tex, opts) {
    return `<span class="katex${opts && opts.displayMode ? " katex-display" : ""}" data-tex="${tex}"></span>`;
  },
};

const APP = path.join(__dirname, "static", "tablet", "app.js");
require(APP);
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

(async function main() {
  console.log("\n[1] 种子不能覆盖本地正文（刷新丢改动 + 旧内容写回云端）");
  // 本地已有一篇被改过的笔记
  const LOCAL_BODY = "# 笔记\n\n平板上的新内容\n";
  fakeDb.data.notes.set("00_Dashboard/a.md", {
    id: "00_Dashboard/a.md",
    title: "笔记",
    group: "00_Dashboard",
    rel: "00_Dashboard/a.md",
    body: LOCAL_BODY,
    html: "",
    pdfs: [],
    dirty: 1,
    deleted: 0,
    baseHash: T.hash(LOCAL_BODY),
    mtime: 111,
  });
  // 种子里是**导出那一刻**的旧快照
  getEl("mb-data").textContent = JSON.stringify({
    notes: [{
      id: "00_Dashboard/a.md",
      title: "笔记",
      rel: "00_Dashboard/a.md",
      group: "00_Dashboard",
      body: "# 笔记\n\nSEED 原文\n",
      mtime: 0,
      pdfs: [],
    }],
    pdfs: {},
  });
  await T.openDB();
  await T.loadSeed();
  const after = fakeDb.data.notes.get("00_Dashboard/a.md");
  check(after.body === LOCAL_BODY,
    "本地正文没被种子覆盖（实际: " + JSON.stringify(after.body) + "）");
  check(after.dirty === 1, "dirty 标记保住了");
  check(after.baseHash === T.hash(after.body),
    "baseHash 与正文一致（不一致的话下次 push 会把旧文本写回云端）");
  check(after.mtime === 111, "本地 mtime 保住了");

  console.log("\n[2] Markdown 插值不能变成 HTML 属性（XSS）");

  /**
   * 真��解析一个标签的属性名。
   *
   * ⚠ 不能用 `/on[a-z]+=/` 这种正则去扫 —— 属性**值**里出现的
   *   "onerror=" 只是文本，不是属性。正则分不清「属性名」和「属性值里的内容」。
   *   这里按 HTML 规则走一遍：引号里的内容整段跳过，
   *   遇到未加引号的属性值才按空白结束。
   */
  function attrNames(tag) {
    const out = [];
    let i = 1;                          // 跳过 '<'
    while (i < tag.length) {
      while (i < tag.length && /\s/.test(tag[i])) i++;
      if (i >= tag.length || tag[i] === ">" || tag[i] === "/") break;
      const nm = /^[^\s/>=]+/.exec(tag.slice(i));
      if (!nm) break;
      const name = nm[0];
      i += name.length;
      while (i < tag.length && /\s/.test(tag[i])) i++;
      if (tag[i] === "=") {
        i++;
        while (i < tag.length && /\s/.test(tag[i])) i++;
        const q = tag[i];
        if (q === '"' || q === "'") {
          const end = tag.indexOf(q, i + 1);
          i = end < 0 ? tag.length : end + 1;
        } else {
          while (i < tag.length && !/[\s>]/.test(tag[i])) i++;
        }
      }
      out.push(name);
    }
    return out;
  }

  function eventAttrs(html) {
    const bad = [];
    const re = /<[a-zA-Z][^>]*>/g;
    let m;
    while ((m = re.exec(html)) !== null) {
      attrNames(m[0]).forEach((n) => {
        if (/^on/i.test(n) || n === "style") bad.push(n);
      });
    }
    return bad;
  }

  const vectors = [
    ['![x" onerror="alert(1)](a.png)', "图片 alt 里的引号"],
    ['![x"onerror="alert(1)](a.png)', "图片 alt 无空格引号"],
    ['[x](y"onmouseover="alert(1))', "链接 href 里的引号"],
    ['<a href="x"onclick="alert(1)">dian</a>', "原始 HTML 的事件属性"],
    ['<div style="position:fixed;top:0;left:0;width:100%;height:100%">盖住全屏</div>', "style 属性"],
    ['<img src="x" onerror="alert(1)">', "原始 HTML 的 img onerror"],
  ];
  vectors.forEach(([src, label]) => {
    const html = T.renderMarkdown(src);
    const bad = eventAttrs(html);
    check(bad.length === 0,
      label + " 没有变成可执行属性" + (bad.length ? " -> " + bad.join(",") : ""));
  });
  const ent = T.renderMarkdown('[x](jav&#x61;script:alert(1))');
  check(!/href="[^"]*javascript/i.test(ent), "实体绕过的 javascript: 被拦下 -> " + ent);
  const tab = T.renderMarkdown('[x](java\tscript:alert(1))');
  check(!/href="[^"]*script:/i.test(tab), "制表符绕过的 javascript: 被拦下 -> " + tab);
  const wik = T.renderMarkdown('[[某篇笔记|<img src=x onerror=alert(1)>]]');
  check(eventAttrs(wik).length === 0, "双链别名不会被当成 HTML -> " + wik);

  console.log("\n[3] 公式落在引用块 / 多行列表里不能把整篇笔记渲染崩");
  const cases = [
    ["> 引用里的行内公式 $E = mc^2$ 结束", "引用块行内公式"],
    ["> 引用里的块级公式\n>\n> $$\n> \\int_0^1 x dx\n> $$\n", "引用块块级公式"],
    ["- 第一行 $a^2$\n  续行 $b^2$\n", "多行列表项公式"],
    ["- 项 $x$\n  - 子项 $y$\n", "嵌套列表公式"],
  ];
  cases.forEach(([src, label]) => {
    let html = null, threw = null;
    try { html = T.renderMarkdown(src); } catch (e) { threw = e; }
    check(!threw, label + " 没有抛异常" + (threw ? " -> " + threw.message : ""));
    if (html) {
      check(html.indexOf("katex") >= 0, label + " 公式渲染出来了 -> " + html.slice(0, 130));
      check(html.indexOf("@@MBK") < 0, label + " 没有残留占位符");
    }
  });
  // 渲染失败也要有兜底，不能白屏
  const ok = T.renderMarkdown("普通段落 $x$ 结束");
  check(ok.indexOf("katex") >= 0, "普通段落的公式仍然正常");

  console.log("\n[4] PDF 划词气泡的监听不能绑在会被清掉的节点上");
  const src = fs.readFileSync(APP, "utf8");
  const initSel = src.split("function initPdfSelection")[1] || "";
  check(initSel.indexOf('const body = $("pdf-body")') >= 0,
    "initPdfSelection 取的是 #pdf-body");
  check(initSel.indexOf('layer.addEventListener') < 0,
    "没有再绑在会被 innerHTML=\"\" 清掉的 textLayer 上");
  const html = fs.readFileSync(path.join(__dirname, "static", "tablet", "index.html"), "utf8");
  check(html.indexOf('id="pdf-textlayer"') < 0,
    "index.html 里没有静态的 #pdf-textlayer 占位");

  console.log("\n" + "=".repeat(60));
  console.log("PASS " + pass + "  FAIL " + fail);
  process.exit(fail === 0 ? 0 : 1);
})();
