/*
 * 平板工作台 · 「圈选区域 → 问 AI / 总结这块」回归测试（2026-11-12）
 *
 * 用户原话："我直接用手写笔圈一部分，然后直接能问 AI"、
 *           "圈一部分，然后直接能问 AI，这个如果能做到的话会很方便"。
 *
 * 改之前的现状：平板上「✍ 带上我的手写问 AI」发的是**整页图**
 *   → 圈的那一小块在整页里又小又糊 → 模型基本看不清 → 等于白圈。
 *   本文件钉住「只发圈的那一块」这条链不许退化。
 *
 * ⚠ 这份是**静态断言**（快、不依赖浏览器）。
 * Run:  node microbench/_test_tablet_region_ask.js
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

/** 取某个函数体的文本（按大括号配平）。取不到返回 ""。 */
function fnBody(name) {
  const i = src.indexOf("function " + name);
  if (i < 0) return "";
  let j = src.indexOf("{", i);
  if (j < 0) return "";
  let depth = 0;
  for (let k = j; k < src.length; k++) {
    const ch = src[k];
    if (ch === "{") depth++;
    else if (ch === "}") { depth--; if (depth === 0) return src.slice(i, k + 1); }
  }
  return src.slice(i, i + 4000);
}

/** 取 CSS 里某条规则（先剥注释，免得注释里的字样被当成真规则）。 */
function rule(sel) {
  const re = new RegExp("^" + sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*\\{[^}]*\\}", "m");
  const m = css.match(re);
  if (!m) return "";
  return m[0].replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
}

// ---------------------------------------------------------------------------
// ① 包围盒：得从笔迹算出「圈的是哪一块」
// ---------------------------------------------------------------------------
section("圈选区域：算得出包围盒");

ok("有 inkBBox（从笔迹算包围盒）", /function inkBBox\s*\(/.test(src));
const bboxBody = fnBody("inkBBox");
ok("inkBBox 认笔迹里的归一化坐标（x0/y0/x1/y1）",
  /x0/.test(bboxBody) && /y1/.test(bboxBody));
ok("inkBBox 遍历每一笔的每个点（i += 2）",
  /i\s*\+\s*1\s*<\s*pts\.length\s*;\s*i\s*\+=\s*2/.test(bboxBody),
  bboxBody.slice(0, 400));

// ---------------------------------------------------------------------------
// ② 裁图：只裁圈的那一块，且按它自己的像素密度放大
// ---------------------------------------------------------------------------
section("圈选区域：只裁那一块");

ok("有 captureInkRegion（裁圈选区域）", /function captureInkRegion\s*\(/.test(src));
const cropBody = fnBody("captureInkRegion");
ok("裁剪用 drawImage 的九参形式（带源矩形，不是整页）",
  /drawImage\(\s*\w+\s*,[\s\S]{0,120}?,\s*0\s*,\s*0\s*,/.test(cropBody),
  cropBody.slice(0, 600));
ok("裁剪会叠上手写层（模型要知道圈在哪）",
  /inkCv/.test(cropBody) && /drawImage\(inkCv/.test(cropBody));
ok("裁剪带高亮框（明确指出「看这一块」）",
  /strokeRect/.test(cropBody));
ok("放大倍数有上限（放大不增加细节，别把糊图放太大）",
  /REGION_MAX_UPSCALE\s*=\s*2\.5/.test(src));
ok("裁出来的小图有最小宽度（太窄字太小模型看不清）",
  /REGION_MIN_WIDTH_PX\s*=\s*\d+/.test(src));

// ---------------------------------------------------------------------------
// ③ 交互：圈完 → 浮出「问这块 / 总结这块 / 清除」
// ---------------------------------------------------------------------------
section("圈选区域：圈完能直接问");

ok("有 showRegionBar（浮出操作条）", /function showRegionBar\s*\(/.test(src));
const barBody = fnBody("showRegionBar");
ok("浮条挂在 .tb-pdf-orig 里（宽屏并排时页卡原点跟原文画布不同）",
  /\.tb-pdf-orig/.test(barBody) && /orig\.appendChild\(bar\)/.test(barBody),
  barBody.slice(0, 500));
ok("浮条有三个动作：问这块 / 总结这块 / 清除",
  /问这块/.test(barBody) && /总结这块/.test(barBody) && /清除/.test(barBody));
ok("浮条上的按钮拦下 pointerdown（不然会被手写层当成又画一笔）",
  /pointerdown/.test(barBody) && /stopPropagation/.test(barBody));

const inkLayer = fnBody("setupInkLayer");
ok("画完一笔就浮出操作条（圈完直接能问，不用再找按钮）",
  /showRegionBar\(\s*page\s*\)/.test(inkLayer),
  inkLayer.slice(-700));

ok("有 askRegion（问这块）", /function askRegion\s*\(/.test(src));
ok("有 summarizeRegion（总结这块）", /function summarizeRegion\s*\(/.test(src));
ok("有 clearRegionInk（清除圈选）", /function clearRegionInk\s*\(/.test(src));

// 问这块 → 打开 AI 面板、带上图、聚焦输入框
const askBody = fnBody("askRegion");
ok("问这块会把裁好的图存成待发图片",
  /pendingRegionImage/.test(askBody));
ok("问这块会打开 AI 面板并聚焦输入框",
  /setPane\(\s*["']ai["']\s*\)/.test(askBody) && /focus\(\)/.test(askBody));

// 总结这块 → 直接发问
const sumBody = fnBody("summarizeRegion");
ok("总结这块会直接把问题发出去（带图）",
  /runAI\(\s*["']ask["']/.test(sumBody) && /imageDataUrl/.test(sumBody));
ok("总结这块的提示词明确是「总结圈出来的这部分」",
  /总结/.test(sumBody));

// ---------------------------------------------------------------------------
// ④ AI 通道：能带图、能自带问题、带图时别塞无关正文
// ---------------------------------------------------------------------------
section("AI 通道：带图提问");

const runBody = fnBody("runAI");
ok("runAI 支持调用方直接给问题（总结这块用）",
  /o\.question/.test(runBody));
ok("runAI 会用「已选中的圈选区域」当图",
  /state\.pendingRegionImage/.test(runBody));
ok("待发的圈选区域图只跟「提问」走（翻译选中不该混进一张图）",
  /kind\s*===\s*["']ask["'][\s\S]{0,200}?state\.pendingRegionImage/.test(runBody),
  runBody.slice(0, 1200));
ok("带图时不再塞 4000 字笔记正文（注意力被拉走 + 白烧 token）",
  /imgAttached/.test(runBody) && /imgAttached \? "" :/.test(runBody),
  runBody.slice(0, 900));
ok("带图的提问仍按 OpenAI 兼容的 image_url 数组格式发",
  /image_url/.test(runBody) && /data:image/.test(runBody) === false || /image_url/.test(runBody));
ok("图用掉就清掉（下一句提问不该又带上同一张图）",
  /state\.pendingRegionImage\s*=\s*""/.test(runBody));

const inkAsk = fnBody("askAiWithInk");
ok("「带上我的手写问 AI」优先发圈选区域，没有才退回整页图",
  /currentInkRegion\(\)/.test(inkAsk) && /captureInkRegion/.test(inkAsk) && /captureInkPage/.test(inkAsk));

// ---------------------------------------------------------------------------
// ⑤ 收尾：浮条不能变成僵尸
// ---------------------------------------------------------------------------
section("浮条生命周期");

ok("关掉手写 → 浮条收起", /function inkToggle[\s\S]{0,900}hideRegionBar\(\)/.test(src));
ok("关掉阅读器 → 浮条收起", /function closePdf[\s\S]{0,900}hideRegionBar\(\)/.test(src));
ok("缩放重画 → 浮条重新挂出来（否则缩放一次按钮就没了）",
  /function redrawPdf[\s\S]{0,2600}showRegionBar\(cur\)/.test(src));

// ---------------------------------------------------------------------------
// ⑥ CSS：浮条要能点到、要在手写层上面
// ---------------------------------------------------------------------------
section("浮条样式");

const regionRule = rule(".tb-pdf-region");
ok("浮条有 pointer-events:auto（默认 none → 点不动）",
  /pointer-events\s*:\s*auto/.test(regionRule),
  "规则 = " + regionRule.replace(/\s+/g, " ").trim());
ok("浮条有 z-index（在手写层上面）",
  /z-index\s*:\s*\d+/.test(regionRule),
  "规则 = " + regionRule.replace(/\s+/g, " ").trim());
ok("浮条是绝对定位（跟着圈走）",
  /position\s*:\s*absolute/.test(regionRule));

// ---------------------------------------------------------------------------
// ⑦ 护栏：不能出现「整页图冒充区域图」
// ---------------------------------------------------------------------------
section("护栏");

ok("圈了几乎整页时不当成「圈了一块」（退回整页图）",
  /REGION_MAX_PAGE_RATIO/.test(src));
ok("currentInkRegion 认得出「当前页的圈」",
  /function currentInkRegion\s*\(/.test(src));

// ---------------------------------------------------------------------------
console.log("\n============================================================");
console.log("PASS " + pass + "  FAIL " + fail);
if (fail) {
  console.log("\n失败项：");
  failures.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
