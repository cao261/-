/*
 * Unit tests for the reader's annotation (批注) behaviour.
 *
 * Two user-visible promises are locked here:
 *   1. 重译之后批注**记录不丢** —— 只是"位置失效"（找不到原高亮），
 *      PDF 侧标注（有 rects）不受重译影响，不算失效。
 *   2. 标注抽屉按**当前页**定位，不再每次都从第一页的第一条开始。
 *
 * applyAnnotationsToPage() / openAnnoDrawer() live inside index.html, so we
 * slice them out of the shipped file and eval them in node with tiny stubs.
 * That keeps the test honest: it exercises the *shipped* source, not a copy.
 *
 * Run:  node microbench/_test_reader_annotations.js
 *       node microbench/_test_reader_annotations.js <另一个 index.html>
 *       （第二个参数用于拿旧版本跑同一套断言，确认这些测试真的能抓到回归）
 */
const fs = require("fs");
const path = require("path");

const TARGET = process.argv[2]
  ? path.resolve(process.argv[2])
  : path.join(__dirname, "static", "index.html");
const SRC = fs.readFileSync(TARGET, "utf8");
console.log("被测文件: " + TARGET + "\n");

// Slice a top-level function out of index.html. The file uses 4-space
// indentation for script-block-level functions, so the function ends at the
// next "\n    }\n".
function sliceFn(name) {
  const start = SRC.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数 " + name);
  const end = SRC.indexOf("\n    }\n", start);
  if (end < 0) throw new Error("找不到 " + name + " 的结尾");
  return SRC.slice(start, end + "\n    }\n".length);
}

const escapeHtml = (s) => String(s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

// ---------------------------------------------------------------------------
// 最小 DOM 桩：只实现被测函数真正碰到的那几个方法。
// ---------------------------------------------------------------------------
function makeEl(tag) {
  const el = {
    tagName: String(tag || "div").toUpperCase(),
    className: "",
    dataset: {},
    style: {},
    children: [],
    parentNode: null,
    textContent: "",
    _html: "",
    scrolled: null,          // 记录 scrollIntoView 被调用过（抽屉定位用）
    classList: {
      add(c) {
        const parts = el.className.split(/\s+/).filter(Boolean);
        if (!parts.includes(c)) { parts.push(c); el.className = parts.join(" "); }
      },
      contains(c) { return el.className.split(/\s+/).includes(c); },
    },
    appendChild(child) { child.parentNode = el; el.children.push(child); return child; },
    insertBefore(child, ref) {
      const i = el.children.indexOf(ref);
      child.parentNode = el;
      el.children.splice(i < 0 ? el.children.length : i, 0, child);
      return child;
    },
    removeChild(child) {
      const i = el.children.indexOf(child);
      if (i >= 0) el.children.splice(i, 1);
      child.parentNode = null;
      return child;
    },
    remove() { if (el.parentNode) el.parentNode.removeChild(el); },
    querySelectorAll(sel) {
      const want = String(sel).replace(/^\./, "");
      const out = [];
      const walk = (node) => {
        for (const c of node.children) {
          if (c.className.split(/\s+/).includes(want)) out.push(c);
          walk(c);
        }
      };
      walk(el);
      return out;
    },
    querySelector(sel) { return el.querySelectorAll(sel)[0] || null; },
    scrollIntoView(opts) { el.scrolled = opts || {}; },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return el._html; },
    // 真实 DOM：设 innerHTML 会清空子节点
    set(v) { el._html = String(v); el.children = []; },
  });
  return el;
}

const domEls = {
  "anno-drawer": makeEl("div"),
  "anno-drawer-list": makeEl("div"),
  "anno-drawer-count": makeEl("span"),
};
const documentStub = {
  getElementById(id) { return domEls[id] || null; },
  createElement(tag) { return makeEl(tag); },
};

const ANNO_COLORS = [
  { id: "yellow", hex: "#FACC15" },
  { id: "terracotta", hex: "#B25336" },
];

// ---------------------------------------------------------------------------
// 被测函数（从 index.html 真实源码里切出来）
// ---------------------------------------------------------------------------
const applyFactory = new Function(
  "_annoState", "_wrapTextWithMark",
  sliceFn("applyAnnotationsToPage") + "\nreturn applyAnnotationsToPage;");
const drawerFactory = new Function(
  "document", "escapeHtml", "_ANNO_COLORS", "_annoState",
  sliceFn("openAnnoDrawer") + "\nreturn openAnnoDrawer;");

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ok   " + name); }
  else { fail++; console.log("  FAIL " + name + (extra ? "  → " + extra : "")); }
}

// ---------------------------------------------------------------------------
// 1) 批注失效判定
// ---------------------------------------------------------------------------
console.log("== 重译后批注：记录保留，只标位置失效 ==");

// 桩：引文还在译文里就认为能包上高亮；不在就包不上（= 重译后原文变了）
function makeWrapStub() {
  return (container, quote) => container.text.indexOf(quote) !== -1;
}

{
  const state = {
    items: [
      { id: "a1", page: 1, quote: "压电层与磁致伸缩层交替堆叠", note: "结构要点", color: "yellow", rects: [] },
      { id: "a2", page: 1, quote: "这句话已经被重译改掉了", note: "这条笔记不能丢", color: "yellow", rects: [] },
      { id: "a3", page: 1, quote: "PDF 原文侧的高亮", note: "PDF 侧标注", color: "yellow", rects: [[0.1, 0.2, 0.3, 0.05]] },
      { id: "a4", page: 7, quote: "别的页", note: "别的页的批注", color: "yellow", rects: [] },
    ],
  };
  const container = { text: "压电层与磁致伸缩层交替堆叠，构成层状复合材料。" };
  const applied = applyFactory(state, makeWrapStub())(1, container);

  check("能对上的引文被高亮（applied=1）", applied === 1, "applied=" + applied);
  check("对上的标注不是失效", state.items[0].orphan === false, "orphan=" + state.items[0].orphan);
  check("对不上的译文侧标注标为失效", state.items[1].orphan === true, "orphan=" + state.items[1].orphan);
  check("失效标注的笔记仍然保留", state.items[1].note === "这条笔记不能丢", JSON.stringify(state.items[1]));
  check("PDF 侧标注（有 rects）不算失效", state.items[2].orphan === false, "orphan=" + state.items[2].orphan);
  check("其它页的标注不受影响", state.items[3].orphan === undefined, "orphan=" + state.items[3].orphan);
}

{
  // 失效过一次之后，译文又变回原样 → 必须自愈（orphan=false）
  const state = { items: [{ id: "b1", page: 1, quote: "回来了", note: "n", color: "yellow", rects: [], orphan: true }] };
  const container = { text: "这句话又回来了" };
  applyFactory(state, makeWrapStub())(1, container);
  check("译文恢复原样后失效标记自愈", state.items[0].orphan === false, "orphan=" + state.items[0].orphan);
}

// ---------------------------------------------------------------------------
// 2) 标注抽屉：按当前页定位
// ---------------------------------------------------------------------------
console.log("== 标注列表按当前页定位 ==");

function openDrawer(items, pageNum) {
  const state = { items: JSON.parse(JSON.stringify(items)) };
  const fn = drawerFactory(documentStub, escapeHtml, ANNO_COLORS, state);
  fn(pageNum);
  const list = domEls["anno-drawer-list"];
  return { state, list };
}

const threePages = [
  { id: "p1", page: 1, quote: "第一页的引文", note: "第一条笔记", color: "yellow", rects: [] },
  { id: "p2", page: 2, quote: "第二页的引文", note: "第二条笔记", color: "yellow", rects: [] },
  { id: "p3", page: 3, quote: "第三页的引文", note: "第三条笔记", color: "yellow", rects: [] },
];

{
  const { list } = openDrawer(threePages, 2);
  check("抽屉列出全部 3 条", list.children.length === 3, "len=" + list.children.length);
  check("第 2 页打开 → 滚到第 2 页那条", list.children[1] && list.children[1].scrolled !== null, JSON.stringify(list.children.map(c => c.scrolled !== null)));
  check("不是从第一条开始", list.children[0] && list.children[0].scrolled === null);
  check("按页排序", list.children[0].dataset.page === "1" && list.children[2].dataset.page === "3");
}

{
  const { list } = openDrawer(threePages, 3);
  check("第 3 页打开 → 滚到第 3 页那条", list.children[2] && list.children[2].scrolled !== null);
}

{
  // 当前页没有标注 → 落在下一页的第一条
  const { list } = openDrawer([threePages[0], threePages[2]], 2);
  check("当前页无标注 → 落在下一页第一条", list.children[1] && list.children[1].scrolled !== null,
    JSON.stringify(list.children.map(c => c.dataset.page + ":" + (c.scrolled !== null))));
  check("不是回退到第一条", list.children[0] && list.children[0].scrolled === null);
}

{
  // 当前页超出范围 → 回退到第一条
  const { list } = openDrawer(threePages, 99);
  check("超出范围 → 回退到第一条", list.children[0] && list.children[0].scrolled !== null);
}

{
  // 失效条目：虚线边框 + 提示文案，笔记照旧显示
  const items = [
    { id: "o1", page: 1, quote: "还在的引文", note: "正常笔记", color: "yellow", rects: [] },
    { id: "o2", page: 1, quote: "失效的引文", note: "失效也要留笔记", color: "yellow", rects: [], orphan: true },
  ];
  const { list } = openDrawer(items, 1);
  const o1 = list.children[0], o2 = list.children[1];
  check("失效条目带 anno-item-orphan 样式", o2.classList.contains("anno-item-orphan"), o2.className);
  check("正常条目不带 orphan 样式", !o1.classList.contains("anno-item-orphan"));
  check("失效条目有提示文案", o2.innerHTML.includes("位置失效") && o2.innerHTML.includes("笔记已保留"), o2.innerHTML.slice(0, 200));
  check("失效条目的笔记仍然渲染", o2.innerHTML.includes("失效也要留笔记"), o2.innerHTML.slice(0, 260));
  check("正常条目不显示失效提示", !o1.innerHTML.includes("位置失效"));
}

{
  // XSS：引文里的尖括号必须被转义
  const { list } = openDrawer([{ id: "x1", page: 1, quote: "<img src=x onerror=alert(1)>", note: "", color: "yellow", rects: [] }], 1);
  check("引文里的 HTML 被转义", !list.children[0].innerHTML.includes("<img src=x"), list.children[0].innerHTML.slice(0, 160));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
