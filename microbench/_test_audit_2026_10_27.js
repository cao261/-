/*
 * 2026-10-27 审计的行为级护栏：批注重放的自激循环（CRITICAL-1）。
 *
 * 背景（独立审查 Playwright 实测复现）：
 *   initAnnotationFeatures() 在译文栏上挂 MutationObserver，
 *   回调里是 `setTimeout(reapplyAnnotations, 80)` —— **没有 clearTimeout**，
 *   而 reapplyAnnotations 自己会改 DOM（清标记 + 重新包 <mark>），
 *   于是：改 DOM → 触发 observer → 再排一次 setTimeout → 再改 DOM …
 *   只要这篇论文有**一条能匹配上的批注**，就会以 ~10.7 次/秒 无限重放，
 *   并且每 80ms 洗一次译文栏的文本选区 → 用户的划词选区被清空
 *   → 划词答疑读不到 getSelection() → 核心工作流断掉。
 *
 * 这里用 vm 沙箱跑 index.html 里的**真实源码**，断言两件事：
 *   ① 连续 N 次 DOM 变更 → 只排 **1 个**定时器（真防抖），不是 N 个；
 *   ② 用户正在划词（selection 非折叠）→ 这一轮**不动 DOM**。
 *
 * 旧代码（没有 clearTimeout）会让 ① 直接失败 —— 护栏是真的会红的。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const INDEX = path.join(__dirname, 'static', 'index.html');
const src = fs.readFileSync(INDEX, 'utf8');

let failures = 0;
function check(ok, msg) {
  if (ok) { console.log('  [OK] ' + msg); }
  else { console.log('  [FAIL] ' + msg); failures++; }
}

/** 从 index.html 里抠出某个函数的完整源码（按花括号配平）。 */
function extractFunction(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const m = re.exec(src);
  if (!m) throw new Error('index.html 里找不到函数 ' + name);
  let i = m.index, depth = 0, started = false;
  for (; i < src.length; i++) {
    const c = src[i];
    if (c === '{') { depth++; started = true; }
    else if (c === '}') {
      depth--;
      if (started && depth === 0) { i++; break; }
    }
  }
  return src.slice(m.index, i);
}

/**
 * 造一个最小沙箱：只提供 initAnnotationFeatures 用到的东西。
 * @param {object} selection window.getSelection() 的返回值
 */
function makeSandbox(selection) {
  const state = {
    observerCb: null,
    timers: new Map(),
    timerId: 0,
    replayCalls: 0,
    list: { dataset: {} },
  };

  const sandbox = {
    window: {
      getSelection: () => selection,
    },
    document: {
      getElementById: (id) => (id === 'pdf-translations-list' ? state.list : null),
      addEventListener: () => {},
    },
    MutationObserver: class {
      constructor(cb) { state.observerCb = cb; }
      observe() {}
    },
    setTimeout: (fn) => { const id = ++state.timerId; state.timers.set(id, fn); return id; },
    clearTimeout: (id) => { state.timers.delete(id); },
    reapplyAnnotations: () => { state.replayCalls++; },
    _annoState: { items: [{ id: 'a1', page: 1, quote: 'x', color: 'yellow' }] },
    openAnnotationEditor: () => {},
    setInterval: () => {},
    console: console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(extractFunction('initAnnotationFeatures') + '\ninitAnnotationFeatures();', sandbox);
  state.sandbox = sandbox;
  return state;
}

function flushTimers(state) {
  const fns = Array.from(state.timers.values());
  state.timers.clear();
  fns.forEach((fn) => fn());
}

// ---------------------------------------------------------------------------
console.log('\n[1] 连续 DOM 变更必须被**真防抖**（只排 1 个定时器）');
// ---------------------------------------------------------------------------
{
  const state = makeSandbox(null);
  check(typeof state.observerCb === 'function', 'MutationObserver 回调已注册');

  for (let i = 0; i < 5; i++) state.observerCb();
  check(state.timers.size === 1,
    `5 次变更后只剩 1 个待触发定时器（实际 ${state.timers.size} 个）—— 没有 clearTimeout 就会是 5 个`);

  flushTimers(state);
  check(state.replayCalls === 1,
    `定时器只触发 1 次重放（实际 ${state.replayCalls} 次）`);

  // 再来一轮：仍然是 1 个
  for (let i = 0; i < 5; i++) state.observerCb();
  check(state.timers.size === 1,
    `第二轮 5 次变更后仍然只有 1 个定时器（实际 ${state.timers.size}）`);
}

// ---------------------------------------------------------------------------
console.log('\n[2] 用户正在划词时**不许动 DOM**（否则选区被清空）');
// ---------------------------------------------------------------------------
{
  const selection = { isCollapsed: false, rangeCount: 1 };
  const state = makeSandbox(selection);
  state.observerCb();
  flushTimers(state);
  check(state.replayCalls === 0,
    `划词中不重放（实际重放了 ${state.replayCalls} 次）—— 重放会清掉用户的文本选区`);
}

// ---------------------------------------------------------------------------
console.log('\n[3] 没有划词时照常重放（不能把功能改没了）');
// ---------------------------------------------------------------------------
{
  const selection = { isCollapsed: true, rangeCount: 1 };
  const state = makeSandbox(selection);
  state.observerCb();
  flushTimers(state);
  check(state.replayCalls === 1,
    `无划词时重放 1 次（实际 ${state.replayCalls} 次）`);
}

// ---------------------------------------------------------------------------
console.log('\n[4] 没有批注时不重放（白跑）');
// ---------------------------------------------------------------------------
{
  const state = makeSandbox(null);
  state.observerCb();
  flushTimers(state);
  check(state.replayCalls === 1,
    `有批注时重放 1 次（实际 ${state.replayCalls} 次）`);

  // 清空批注后再触发 → 不该再排定时器
  state.sandbox._annoState.items = [];
  state.observerCb();
  check(state.timers.size === 0,
    `没有批注时不排定时器（实际排了 ${state.timers.size} 个）`);
}

console.log('');
if (failures > 0) {
  console.error(`✗ ${failures} 条护栏未通过`);
  process.exit(1);
}
console.log('✓ 全部护栏通过');
