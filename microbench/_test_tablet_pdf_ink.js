/*
 * 平板工作台 · PDF 缩放 + 手写层 回归测试（2026-10-28）
 *
 * 用户反馈的两个真问题：
 *   ① 手写「没有任何作用」—— 画了不显示 / 存了取不回 / 电脑上看不见
 *   ② 缩放放大后「只能显示一部分，动不了了」
 *
 * ⚠ 这份是**静态断言**（快、不依赖浏览器）；
 *   配套的**真实渲染**护栏在 microbench/_test_tablet_pdf_layout.py
 *   （headless Edge 实测版面）—— 两份都要绿。
 *
 * Run:  node microbench/_test_tablet_pdf_ink.js
 */
const path = require("path");
const fs = require("fs");

const APP = path.join(__dirname, "static", "tablet", "app.js");
const CSS = path.join(__dirname, "static", "tablet", "styles.css");
const src = fs.readFileSync(APP, "utf8");
const css = fs.readFileSync(CSS, "utf8");

let pass = 0, fail = 0;
const failures = [];
function ok(name, cond, detail) {
  if (cond) { pass++; console.log("  PASS  " + name); }
  else { fail++; failures.push(name + (detail ? " — " + detail : "")); console.log("  FAIL  " + name + (detail ? " — " + detail : "")); }
}
function section(t) { console.log("\n=== " + t + " ==="); }

// 取 .tb-pdf-page 的规则块（只取第一层，不含 media 块里的）
//
// ⚠⚠ 必须**剥掉注释**再判断：规则里带着大段中文注释，
//   注释里会提到 `max-width:100%` / `overflow:hidden` 这些**被禁用的写法**
//   （正是在解释为什么不能用）→ 不剥注释就会误判成「还在用」。
function stripComments(s) {
  return s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}
function rule(sel) {
  const re = new RegExp("^" + sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{[^}]*\\}", "m");
  const m = css.match(re);
  return m ? stripComments(m[0]) : "";
}

// ---------------------------------------------------------------------------
// ① 缩放：放大后内容超出容器 ⇒ 必须能滚动，绝不能被裁掉
// ---------------------------------------------------------------------------
section("缩放放大后不能被裁掉（用户说的「变大了就动不了」）");

const pageRule = rule(".tb-pdf-page");
ok("页卡不能用 max-width:100% + overflow:hidden 夹住放大的内容",
  !(/max-width\s*:\s*100%/.test(pageRule) && /overflow\s*:\s*hidden/.test(pageRule)),
  "页卡规则 = " + pageRule.replace(/\s+/g, " ").trim());

ok("页卡宽度跟着内容走（width:max-content），放大后自然超宽",
  /width\s*:\s*max-content/.test(pageRule),
  "页卡规则 = " + pageRule.replace(/\s+/g, " ").trim());

const bodyRule = rule(".tb-pdf-body");
ok("滚动容器能横向滚动（overflow:auto，且没有 overflow-x:hidden）",
  /overflow\s*:\s*auto/.test(bodyRule) && !/overflow-x\s*:\s*hidden/.test(bodyRule),
  "body 规则 = " + bodyRule.replace(/\s+/g, " ").trim());

ok("缩放上限存在且 ≤ 4", /Math\.min\(\s*4\s*,/.test(src), "没找到 zoom 上限");

// 缩放变化后必须重画（否则还是旧尺寸）
// ⚠ 窗口要够宽：两个按钮的 onclick 写法长度不同，窗口太窄会误报
const zoomHandlers = src.match(/\$?\("pdf-zoom-(?:in|out)"\)\.onclick[\s\S]{0,400}?\};/g) || [];
ok("缩放按钮会触发重画", zoomHandlers.length >= 2 && zoomHandlers.every(h => /redrawPdf\(\)/.test(h)),
  "handlers = " + zoomHandlers.length + " -> " + JSON.stringify(zoomHandlers));

// ---------------------------------------------------------------------------
// ② 手写：能画、能存、能取回、电脑上也能看见
// ---------------------------------------------------------------------------
section("手写层：能画、能存、能取回");

const inkOnRule = rule(".tb-pdf-ink.is-on");
ok("手写层 touch-action:none（手指拖动不会被浏览器当滚动手势）",
  /touch-action\s*:\s*none/.test(inkOnRule),
  "规则 = " + inkOnRule.replace(/\s+/g, " ").trim());
ok("手写层 pointer-events:auto（开着手写时能收到事件）",
  /pointer-events\s*:\s*auto/.test(inkOnRule), "规则 = " + inkOnRule.replace(/\s+/g, " ").trim());

const setupInk = (src.match(/function setupInkLayer\([\s\S]*?\n  \}/) || [""])[0];
// ⚠ 剥掉注释再判断：注释里解释「为什么删掉 pointerleave」也算命中
const setupCode = stripComments(setupInk);
const fitBody = (setupInk.match(/function fit\(\)\s*\{[\s\S]*?\n    \}/) || [""])[0];
const fitCode = stripComments(fitBody);

ok("手写层建了 canvas", /el\(["']canvas["']\)/.test(setupInk));
ok("画布尺寸为 0 时跳过（不落到 1×1 = 画了等于没画）",
  /!r\.width \|\| !r\.height/.test(fitCode), "fit() 里没看到判空");
ok("画布位图尺寸没有 `|| 1` 兜底",
  !/\|\| 1\)/.test(fitCode), "fit() 里还有 || 1 兜底");
ok("手写层尺寸会在布局后重测（ResizeObserver 或 rAF）",
  /ResizeObserver/.test(setupInk) || /requestAnimationFrame/.test(setupInk));
ok("手写层不监听 pointerleave（否则半笔被丢掉）",
  !/pointerleave/.test(setupCode), "setupInkLayer 里还有 pointerleave");
ok("手写用 pointer capture 兜住划出边界的手势",
  /setPointerCapture/.test(setupCode));

ok("笔迹用归一化坐标（0~1）", /Math\.max\(0,\s*Math\.min\(1,/.test(src));
ok("打开论文时会把笔迹装回来", /inkStrokes\(pg\)\.push\(pts\)/.test(src));
ok("保存的批注带 strokes（电脑端要能读到）", /strokes:\s*strokes/.test(src));
ok("笔迹带自己的页码（strokes[].page，一条手写批注可跨多页）",
  /strokes\.push\(\{\s*page:/.test(src), "strokes 里没带 page");

const drawInk = (src.match(/function drawInkPage\([\s\S]*?\n  \}/) || [""])[0];
ok("drawInkPage 会真的描线（beginPath/stroke）",
  /beginPath\(\)/.test(drawInk) && /\.stroke\(\)/.test(drawInk));

// ---------------------------------------------------------------------------
// ③ 桌面端也要能看见平板画的笔迹
// ---------------------------------------------------------------------------
section("桌面端渲染手写笔迹（平板画的字，电脑上要能看见）");

const DESKTOP = path.join(__dirname, "static", "index.html");
const desktop = fs.readFileSync(DESKTOP, "utf8");
ok("桌面端会渲染 strokes（原来只画 rects → 同步过来一个字都看不见）",
  /a\.strokes/.test(desktop));
ok("桌面端按 strokes[].page 判断页码（手写批注的 page 是 0）",
  /s\.page/.test(desktop));
ok("桌面端笔迹用 SVG polyline 画（不干扰原文 canvas）",
  /createElementNS/.test(desktop) && /polyline/.test(desktop));
ok("桌面端有 .anno-ink-svg 的 CSS（SVG 要铺满批注层）",
  /\.anno-ink-svg/.test(desktop));

// ---------------------------------------------------------------------------
console.log("\n" + "=".repeat(60));
console.log(`PASS ${pass}  FAIL ${fail}`);
if (fail) {
  console.log("\n失败明细：");
  failures.forEach(f => console.log("  - " + f));
}
process.exit(fail ? 1 : 0);
