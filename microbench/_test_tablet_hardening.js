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
const STORE_KEYS = { notes: "id", meta: "k", pdfcache: "id", annos: "id" };

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

  console.log("\n[5] PDF 阅读器：竖排 + 适合宽度（别再横排只显示一半）");
  const css = fs.readFileSync(path.join(__dirname, "static", "tablet", "styles.css"), "utf8");
  // ⚠ 剥掉注释再查 —— 注释里会写「原来是 display:flex」这种说明，
  //   正则会把它当成真代码。
  const cssCode = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const bodyCss = (cssCode.split(".tb-pdf-body {")[1] || "").split("}")[0];
  check(bodyCss.indexOf(".tb-pdf-body") < 0, "找到了 .tb-pdf-body 的规则");
  check(!/display\s*:\s*flex/.test(bodyCss),
    ".tb-pdf-body 不是 flex 横排了（横排在窄屏上只看得到一两页）");
  check(!/justify-content\s*:\s*center/.test(bodyCss),
    ".tb-pdf-body 没有 flex 居中（居中溢出让左半边永远滚不到）");
  check(src.indexOf("pdfFitScale") >= 0, "有按容器宽度算的适合比例");
  check(src.indexOf("pdfState.zoom") >= 0, "缩放走独立的倍数，不改死 scale");
  check(src.indexOf("ratio * br.height") >= 0, "缩放后保留滚动锚点（不会被弹回第一页）");

  console.log("\n[6] 译文：paper_id 必须和电脑端的快照文件名对得上");
  const SNAP = path.join(__dirname, "..", "logs", "_pdf_snapshots");
  if (!fs.existsSync(SNAP)) {
    console.log("  SKIP  没有 logs/_pdf_snapshots（这台机器还没翻过论文）");
  } else {
    const stems = fs.readdirSync(SNAP).filter((f) => f.endsWith(".json")).map((f) => f.slice(0, -5));
    check(stems.length > 0, "找到了电脑端的译文快照");
    stems.forEach((stem) => {
      const data = JSON.parse(fs.readFileSync(path.join(SNAP, stem + ".json"), "utf8"));
      const rel = data.relative_path;
      const key = T.safePaperKey(T.paperIdFor(rel));
      check(key === stem,
        "平板算出的 key = 电脑端的文件名: " + key + (key === stem ? "" : " vs " + stem));
    });
  }
  check(src.indexOf('syncUrl("file", "snapshots/"') >= 0,
    "从同步根读译文快照");
  check(src.indexOf("state.transSeed") >= 0,
    "导出版里烤进来的译文也认（没连云盘时也有）");
  check(src.indexOf("renderTranslationInto") >= 0, "译文会渲染到页卡里");

  console.log("\n[7] 手写：笔迹要能存到共享目录");
  check(src.indexOf("putSync") >= 0, "写同步根的原生桥");
  check(src.indexOf("inkStrokes") >= 0, "笔迹按页存放");
  check(src.indexOf('strokes: strokes') >= 0, "笔迹存进批注条目（跟电脑端共享同一份）");

  console.log("\n[8] 译文里的公式要能渲染（这是论文阅读器的基本要求）");
  const SNAP2 = path.join(__dirname, "..", "logs", "_pdf_snapshots");
  if (!fs.existsSync(SNAP2)) {
    console.log("  SKIP  没有译文快照");
  } else {
    const files = fs.readdirSync(SNAP2).filter((f) => f.endsWith(".json"));
    let checked = 0, withMath = 0;
    files.forEach((f) => {
      const data = JSON.parse(fs.readFileSync(path.join(SNAP2, f), "utf8"));
      (data.pages || []).forEach((p) => {
        const t = p.translation || "";
        if (!t) return;
        checked++;
        if (!/\$[^$\n]+\$/.test(t)) return;
        withMath++;
        const html = T.renderMarkdown(t);
        if (html.indexOf("katex") < 0) {
          check(false, "译文里的公式没渲染成 KaTeX（第 " + p.page_num + " 页）");
          return;
        }
        // 渲染后不该还剩裸的 $…$
        check(!/\$[^$\n]+\$/.test(html.replace(/<[^>]*>/g, "")),
          "译文第 " + p.page_num + " 页的公式渲染后没有残留 $");
      });
    });
    check(checked > 0, "检查了 " + checked + " 页译文");
    if (withMath === 0) {
      console.log("  （这批译文里没有公式，跳过公式检查）");
    } else {
      check(true, "含公式的译文都渲染成了 KaTeX");
    }
  }

  console.log("\n[9] 本地优先：断网也要能存、能看批注");
  await T.openDB();
  const realFetch = global.fetch;
  global.fetch = async () => { throw new Error("offline"); };
  const rel = "01_Literature/01_论文原文_PDF/x.pdf";
  const key = T.safePaperKey(T.paperIdFor(rel));
  const items = [{ id: "a1", page: 1, quote: "离线写的批注", rects: [], strokes: [] }];
  const synced = await T.saveAnnotations(rel, items, true);
  check(synced === false, "离线时同步失败（预期）");
  const rec = await T.DB.getAnnos(key);
  check(rec && rec.items && rec.items.length === 1,
    "但本地已经存下来了");
  check(rec && rec.dirty === 1, "本地标记为「待同步」");
  const back = await T.loadAnnotations(rel);
  check(back.length === 1 && back[0].quote === "离线写的批注",
    "断网时也能把本地批注读出来");
  global.fetch = realFetch;

  console.log("\n[10] 批注合并：按 id 合并，不整份覆盖");
  const m = T.mergeAnnos(
    [{ id: "a1", quote: "云端的" }, { id: "a2", quote: "只有云端有" }],
    [{ id: "a1", quote: "本地的" }, { id: "a3", quote: "只有本地有" }]);
  check(m.length === 3, "合并后是 3 条（实际 " + m.length + "）");
  const a1 = m.find((x) => x.id === "a1");
  check(a1 && a1.quote === "本地的", "同 id 时本地那条为准（本地优先）");

  console.log("\n[11] 布局三选一（平板屏幕小，双栏要能切）");
  check(src.indexOf("pdf-layout") >= 0, "有布局切换按钮");
  check(src.indexOf('lay-" + (pdfState.layout') >= 0, "页卡按布局加 class");
  check(src.indexOf("仅原文") >= 0 && src.indexOf("仅译文") >= 0,
    "三种布局：原文+译文 / 仅原文 / 仅译文");
  const css2 = css.replace(/\/\*[\s\S]*?\*\//g, "");
  check(css2.indexOf(".tb-pdf-page.lay-orig .tb-pdf-trans") >= 0,
    "仅原文时隐藏译文栏");
  check(css2.indexOf(".tb-pdf-page.lay-trans .tb-pdf-orig") >= 0,
    "仅译文时隐藏原文栏");

  console.log("\n[12] 手写要能直接问 AI、也能存成批注");
  check(src.indexOf("captureInkPage") >= 0, "能把原文+手写合成图片");
  check(src.indexOf("askAiWithInk") >= 0, "有「带上手写问 AI」的入口");
  check(src.indexOf("image_url") >= 0, "用多模态的 image_url 把图发给模型");
  check(src.indexOf("saveQuoteAsAnno") >= 0, "划词能存成批注");
  check(src.indexOf("collectSelectionRects") >= 0, "选区矩形归一化（跟电脑端同一套）");
  check(src.indexOf('data-pdf-anno') >= 0 || src.indexOf("pdf-anno") >= 0,
    "划词气泡上有「标注」按钮");

  console.log("\n" + "=".repeat(60));
  console.log("PASS " + pass + "  FAIL " + fail);
  process.exit(fail === 0 ? 0 : 1);
})();
