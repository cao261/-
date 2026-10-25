/*
 * Unit tests for the reader's "AI 活动" registry (进度可视化).
 *
 * 用户诉求（2026-10-25）："他有可能等了半天然后什么也没输出" ——
 * 一个不动的转圈 = 用户分不清"在跑"还是"卡了"。所以做了：
 *   · 统一任务登记处（翻译 / 章节翻译 / 划词答疑）
 *   · 每 500ms 刷新"已用时 + 阶段"
 *   · 工具栏徽标 + 可点开的面板，每个任务可取消
 *
 * 这段代码在 index.html 里，用**段落标记**切出来在 node 里跑（测的是发布
 * 出去的那份源码，不是抄一份）。
 *
 * Run:  node microbench/_test_reader_ai_activity.js
 */
const fs = require("fs");
const path = require("path");

// 归一化行尾：仓库约定 LF，但 Windows 上的脚本可能写成 CRLF，
// 而下面按 "\n    }\n" 找函数结尾 —— CRLF 会让它找不到。
const SRC = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8")
  .replace(/\r\n/g, "\n");

// 切出「AI 活动登记处」整段：从横幅注释到 window._aiFormatMs 赋值。
function sliceSection(startMarker, endMarker) {
  const s = SRC.indexOf(startMarker);
  if (s < 0) throw new Error("找不到起始标记: " + startMarker);
  const e = SRC.indexOf(endMarker, s);
  if (e < 0) throw new Error("找不到结束标记: " + endMarker);
  return SRC.slice(s, e + endMarker.length);
}

const SECTION = sliceSection(
  "// AI 活动登记处 —— 让",
  "window._aiFormatMs = _aiFormatMs;"
);

// ---- 极简 DOM 桩：面板渲染只需要 getElementById + querySelector ----
function makeEl() {
  const el = {
    innerHTML: "",
    dataset: {},
    style: {},
    _children: [],
    _classes: new Set(),
    classList: {
      add(c) { el._classes.add(c); },
      remove(c) { el._classes.delete(c); },
      toggle(c) { el._classes.has(c) ? el._classes.delete(c) : el._classes.add(c); },
      contains(c) { return el._classes.has(c); },
    },
    querySelector(sel) { return el._children.find(c => c._sel === sel) || null; },
    appendChild(c) { el._children.push(c); return c; },
  };
  return el;
}

const pillEl = makeEl();
const panelEl = makeEl();
const bodyEl = makeEl();
panelEl.querySelector = (sel) => (sel === ".ai-panel-body" ? bodyEl : null);

const documentStub = {
  getElementById(id) {
    if (id === "ai-activity-pill") return pillEl;
    if (id === "ai-panel") return panelEl;
    return null;
  },
};

let toastCount = 0;
const escapeHtml = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

const factory = new Function(
  "document", "escapeHtml", "showToast", "setInterval", "clearInterval", "setTimeout", "window",
  SECTION + "\nreturn { _aiTaskStart, _aiTaskStage, _aiTaskEnd, _aiTaskCancel, _aiFormatMs, _AI_TASKS, _AI_TASK_HISTORY, _renderAiActivity };"
);
let timerCallback = null;
const api = factory(
  documentStub, escapeHtml,
  () => { toastCount++; },
  (fn, ms) => { timerCallback = fn; return 1; },
  () => { timerCallback = null; },
  (fn) => fn(),
  {}   // window
);

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ok   " + name); }
  else { fail++; console.log("  FAIL " + name + (extra ? "  → " + extra : "")); }
}

console.log("== 耗时格式化 ==");
check("0 秒", api._aiFormatMs(0) === "0s", api._aiFormatMs(0));
check("59 秒", api._aiFormatMs(59000) === "59s", api._aiFormatMs(59000));
check("1 分 00 秒", api._aiFormatMs(60000) === "1m00s", api._aiFormatMs(60000));
check("1 分 18 秒", api._aiFormatMs(78000) === "1m18s", api._aiFormatMs(78000));

console.log("== 任务登记 / 收尾 ==");
{
  const ac = { aborted: false, abort() { this.aborted = true; } };
  const id = api._aiTaskStart({
    kind: "translate", label: "翻译第 3 页", model: "MiniMax-M3", controller: ac,
  });
  check("登记后任务在册", api._AI_TASKS.size === 1, String(api._AI_TASKS.size));
  const t = api._AI_TASKS.get(id);
  check("任务带标签与模型", t && t.label === "翻译第 3 页" && t.model === "MiniMax-M3",
    t ? t.label + "/" + t.model : "null");
  check("初始状态是 running", t && t.state === "running");

  api._aiTaskStage(id, "等待模型响应（含读表）");
  check("阶段可更新", api._AI_TASKS.get(id).stage === "等待模型响应（含读表）",
    api._AI_TASKS.get(id).stage);

  // 徽标应显示"运行中"
  api._renderAiActivity();
  check("徽标进入运行态", pillEl.dataset.active === "1", JSON.stringify(pillEl.dataset));
  check("徽标显示任务数", /AI 运行中 1/.test(pillEl.innerHTML), pillEl.innerHTML);
  check("面板渲染出该任务", /翻译第 3 页/.test(bodyEl.innerHTML), bodyEl.innerHTML.slice(0, 200));
  check("面板里有取消按钮", /_aiTaskCancel/.test(bodyEl.innerHTML));

  // 回归（2026-10-25 浏览器实测）：PDF 双栏模式下 .reader-topbar 是
  // display:none，AI 活动徽标被连带隐藏 → 浮层点不开 → 「✕ 取消」够不着。
  // 修法之一是"有任务就自动展开浮层" → 这里锁住它。
  check("有任务时浮层自动展开", panelEl.classList.contains("open"),
    "classes=" + Array.from(panelEl._classes).join(","));

  // 取消
  api._aiTaskCancel(id);
  check("取消会 abort 上游请求", ac.aborted === true);
  check("取消会弹提示", toastCount === 1, String(toastCount));

  api._aiTaskEnd(id, "done");
  check("结束后从在册列表移除", api._AI_TASKS.size === 0, String(api._AI_TASKS.size));
  check("结束后进历史", api._AI_TASK_HISTORY.length === 1, String(api._AI_TASK_HISTORY.length));
  check("历史记录带耗时基准", api._AI_TASK_HISTORY[0].startedAt > 0);

  api._renderAiActivity();
  check("徽标回到空闲态", pillEl.dataset.active === "0", JSON.stringify(pillEl.dataset));
  check("历史条目仍在面板里", /翻译第 3 页/.test(bodyEl.innerHTML), bodyEl.innerHTML.slice(0, 300));
  // 任务结束后自动展开的记录要清空 —— 下一批任务还能自动弹出来
  // （否则用户手动关过一次面板后，就再也看不到进度和取消按钮了）。
  panelEl.classList.remove("open");
  api._aiTaskStart({ kind: "translate", label: "翻译第 5 页", model: "MiniMax-M3" });
  api._renderAiActivity();
  check("新任务会再次自动展开浮层", panelEl.classList.contains("open"),
    "classes=" + Array.from(panelEl._classes).join(","));
  api._aiTaskEnd(Array.from(api._AI_TASKS.keys())[0], "done");
}

console.log("== 多任务并存 ==");
{
  api._aiTaskStart({ kind: "translate", label: "翻译第 4 页", model: "A" });
  api._aiTaskStart({ kind: "qa", label: "划词答疑", model: "B" });
  check("两个任务都在册", api._AI_TASKS.size === 2, String(api._AI_TASKS.size));
  api._renderAiActivity();
  check("徽标显示 2 个任务", /AI 运行中 2/.test(pillEl.innerHTML), pillEl.innerHTML);
  check("两个任务都渲染出来",
    /翻译第 4 页/.test(bodyEl.innerHTML) && /划词答疑/.test(bodyEl.innerHTML));
  // 结束一个，另一个还在
  const first = Array.from(api._AI_TASKS.keys())[0];
  api._aiTaskEnd(first, "error", "上游 500");
  check("失败的任务从在册移除", api._AI_TASKS.size === 1, String(api._AI_TASKS.size));
  check("失败原因进历史",
    api._AI_TASK_HISTORY.some(h => h.state === "error" && /上游 500/.test(h.error || "")),
    JSON.stringify(api._AI_TASK_HISTORY.map(h => h.state)));
  api._renderAiActivity();
  check("面板显示失败原因", /上游 500/.test(bodyEl.innerHTML), bodyEl.innerHTML.slice(0, 400));
}

console.log("== 收尾不许再动界面 ==");
{
  // 回归：2026-10-25 独立审查抓到的阻断 bug ——
  // _aiTaskEnd 收尾时调 onStage → translateSection 的 onStage 把
  // #reader-translation 整个覆盖成 "done · 0s" → 翻完章节译文消失。
  let stageCalls = [];
  const state = {
    items: [],
    _AI_TASKS: new Map(),
    _AI_TASK_HISTORY: [],
  };
  const id = api._aiTaskStart({
    kind: "section", label: "翻译章节：测试",
    onStage: (stage) => { stageCalls.push(stage); },
  });
  api._aiTaskStage(id, "等待模型响应");
  check("运行中会更新阶段", stageCalls.length === 1, JSON.stringify(stageCalls));
  api._aiTaskEnd(id, "done");
  check("收尾时**不再**调 onStage（否则会覆盖译文）",
    stageCalls.length === 1, JSON.stringify(stageCalls));
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
