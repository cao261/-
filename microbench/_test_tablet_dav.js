/*
 * 平板端「坚果云 WebDAV 路径」回归测试。
 *
 * 故障现场（2026-10-25 用户真机截图）：
 *   - 界面显示「已连接坚果云」「坚果云 WebDAV（真双向）」
 *   - 同步结果是「已连接 0 篇 · 新增 0 · 更新 0」
 *   - 24 篇原文 PDF 全部 404（"云盘里没找到这个文件"）
 *   - 文献库还写着「⚠ 还没连接文件夹」
 *
 * 根因：vault 前缀（「我的坚果云/MicroBench工作台」，**两段**）是空的。
 * ⚠⚠ 最阴的地方：前缀错时 PROPFIND **不报 404** ——
 *   服务器对「不存在的目录」回的是 **207 + 0 个条目**。
 *   → 页面看到的是「目录是空的」→ 界面显示「已连接 0 篇」，
 *     **一点错都看不出来**，用户只会以为「云盘没同步完」。
 *
 * 这三条护栏就是为它立的：
 *   ① 列目录 0 条目 = 路径不对 → 必须报错，不能静默；
 *   ② 界面不能说谎（「已连接」时要亮出 vault 的真实位置）；
 *   ③ 文献库不能谎报「还没连接文件夹」（DAV 模式下 dirHandle 永远是 null）。
 *
 * Run:  node microbench/_test_tablet_dav.js
 */
const path = require("path");

// ---- DOM shim -------------------------------------------------------------
function mkEl(tag) {
  return {
    tagName: String(tag || "").toUpperCase(),
    className: "", _text: "", _html: "", children: [], style: {},
    hidden: false, value: "", scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    offsetWidth: 0, listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => c !== x); },
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
// ⚠⚠ 必须在 require app.js **之前**装好原生桥 ——
//   app.js 在模块加载时就把它读进常量 nativeBridge 了。
global.MicroBench = {
  davConfigured: () => true,
  davInfo: () => JSON.stringify({
    configured: true,
    vaultRoot: "我的坚果云/MicroBench工作台",
    syncRoot: "我的坚果云/MicroBench同步",
  }),
  openSettings: () => {},
};
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
    return `<span class="katex${opts && opts.displayMode ? " katex-display" : ""}"></span>`;
  },
};

// ---- fetch 桩 --------------------------------------------------------------
// davListings: "/dav/list/xxx" -> 条目数组
let davListings = {};
const fetchCalls = [];
global.fetch = async (url, opts) => {
  fetchCalls.push(String(url));
  const u = String(url);
  if (u.indexOf("/dav/list/") === 0 || u.indexOf("/dav/sync/list/") === 0) {
    const key = u.replace(/\/$/, "");
    const items = davListings[key] || davListings[u] || [];
    return { ok: true, status: 200, json: async () => items, text: async () => JSON.stringify(items) };
  }
  if (u.indexOf("/dav/file/") === 0 || u.indexOf("/dav/sync/file/") === 0) {
    // readText 要 text()，readBlob 要 blob() —— 两个都得给
    return {
      ok: true, status: 200,
      blob: async () => ({ size: 10 }),
      text: async () => "# 笔记\n\n正文\n",
    };
  }
  return { ok: false, status: 404, text: async () => "not found" };
};

require(path.join(__dirname, "static", "tablet", "app.js"));
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

function reset() {
  byId.clear();
  fetchCalls.length = 0;
  davListings = {};
  T.state.notes.clear();
  T.state.aiHistory.clear();
  T.state.pdfs = {};
  T.state.dirtyCount = 0;
  T.state.currentId = null;
  T.state.dirHandle = null;
  T.state.syncMode = "dav";
}

/** 正常的 vault 目录树（相对 vault 根） */
const GOOD_TREE = {
  "/dav/list/": [
    { path: "00_Dashboard", name: "00_Dashboard", isDir: true, size: 0, mtime: 1 },
    { path: "01_Literature", name: "01_Literature", isDir: true, size: 0, mtime: 1 },
  ],
  "/dav/list/00_Dashboard": [
    { path: "00_Dashboard/看板.md", name: "看板.md", isDir: false, size: 12, mtime: 2 },
  ],
  "/dav/list/01_Literature": [
    { path: "01_Literature/a.pdf", name: "a.pdf", isDir: false, size: 7311, mtime: 3 },
  ],
};

(async function main() {
  console.log("\n[1] 列目录 0 条目 = 路径不对，必须报错（不能静默显示 0 篇）");
  reset();
  // 模拟「前缀没配」：PROPFIND 打到不存在的目录 → 服务器回 207 + 0 个条目
  davListings = { "/dav/list/": [] };
  const backend = T.currentBackend();
  check(backend && backend.mode === "dav", "当前后端是 DAV", backend && backend.mode);
  let threw = null;
  try {
    await T.scanRemote(backend, true);
  } catch (e) {
    threw = e;
  }
  check(threw !== null, "scanRemote 抛错了（不是静默返回空）");
  check(threw && /vault/.test(String(threw.message || threw)),
    "错误信息点明是 vault 路径的问题 -> " + (threw ? String(threw.message || threw) : ""));

  console.log("\n[2] 路径对的时候照常扫出笔记和 PDF");
  reset();
  davListings = GOOD_TREE;
  const scan = await T.scanRemote(T.currentBackend(), true);
  check(scan && scan.notes.length === 1, "扫到 1 篇笔记（实际 " +
    (scan ? scan.notes.length : "-") + "）");
  check(scan && scan.notes[0].path === "00_Dashboard/看板.md",
    "笔记路径是 vault 相对路径 -> " + (scan && scan.notes[0] ? scan.notes[0].path : ""));
  check(scan && Object.keys(scan.pdfs).length === 1, "扫到 1 篇 PDF");
  check(scan && scan.pdfs["01_Literature/a.pdf"], "PDF 清单带上了相对路径");

  console.log("\n[3] 同步失败要弹错，不能安静地报「0 篇」");
  reset();
  davListings = { "/dav/list/": [] };
  await T.pullFromDir(false);
  const tip = getEl("tb-toast")._text || "";
  check(tip.indexOf("同步失败") >= 0 || tip.indexOf("vault") >= 0,
    "同步出错时弹了提示 -> " + JSON.stringify(tip));

  console.log("\n[4] 界面不能说谎：设置页要亮出 vault 的真实位置");
  reset();
  T.refreshSyncUi();
  const detail = getEl("sync-detail")._text || "";
  check(detail.indexOf("我的坚果云/MicroBench工作台") >= 0,
    "设置页显示了 vault 的真实位置 -> " + JSON.stringify(detail));
  check(detail.indexOf("已连接") >= 0, "同时说明了已连接（不是二选一）");

  console.log("\n[5] 文献库不能再谎报「还没连接文件夹」");
  reset();
  T.state.syncMode = "dav";
  T.state.pdfs = { "01_Literature/a.pdf": { id: "01_Literature/a.pdf", name: "a.pdf", size: 1, stem: "a" } };
  T.refreshSyncUi();
  // openPdfLib 是内部函数，但设置页的说明文字走的是同一个判断 ——
  // 这里用「文献库那句警告不会在 DAV 模式下出现」来钉：
  // 只要代码里还在用 state.dirHandle 判断，这句就会露出来。
  const src = require("fs").readFileSync(
    path.join(__dirname, "static", "tablet", "app.js"), "utf8");
  const libIdx = src.indexOf("function openPdfLib");
  const libBody = src.slice(libIdx, libIdx + 1400);
  check(libBody.indexOf("state.dirHandle\n") < 0 &&
        !/note\.textContent\s*=\s*state\.dirHandle/.test(libBody),
    "文献库不再用 state.dirHandle 判断（DAV 模式下它永远是 null）");
  check(libBody.indexOf("currentBackend()") >= 0, "文献库改用 currentBackend() 判断");

  console.log("\n[6] davVaultRootLabel 拿得到原生的真实状态");
  reset();
  check(T.davVaultRootLabel() === "我的坚果云/MicroBench工作台",
    "读到了 vault 位置 -> " + T.davVaultRootLabel());

  console.log("\n" + "=".repeat(60));
  console.log("PASS " + pass + "  FAIL " + fail);
  process.exit(fail === 0 ? 0 : 1);
})();
