/*
 * Unit tests for the reader's Markdown renderer (table support + formula marker).
 *
 * formatMarkdownLite() lives inside index.html, so we slice the two functions
 * out of the file and eval them in node with a tiny escapeHtml stub. That
 * keeps the test honest: it exercises the *shipped* source, not a copy.
 *
 * Run:  node microbench/_test_reader_markdown.js
 */
const fs = require("fs");
const path = require("path");

const { loadDesktopSource } = require("./_test_frontend_loader");

const SRC = loadDesktopSource(path.join(__dirname, "static", "index.html"));

function sliceFnFrom(src, name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数 " + name);
  const end = src.indexOf("\n    }\n", start);
  if (end < 0) throw new Error("找不到 " + name + " 的结尾");
  return src.slice(start, end + "\n    }\n".length);
}

function sliceFn(name) {
  return sliceFnFrom(SRC, name);
}

const escapeHtml = (s) => String(s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

function buildFactory(src) {
  return new Function("escapeHtml",
    sliceFnFrom(src, "formatMarkdownLite") + "\n" +
    sliceFnFrom(src, "_matchMarkdownTable") +
    "\nreturn {formatMarkdownLite, _matchMarkdownTable};");
}

const factory = buildFactory(SRC);
const { formatMarkdownLite } = factory(escapeHtml);

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log("  ok   " + name); }
  else { fail++; console.log("  FAIL " + name + (extra ? "  → " + extra : "")); }
}

console.log("== Markdown 表格 ==");
const table = [
  "| 性能 | BaTiO3 | PZT-5 | PVDF |",
  "|---|---|---|---|",
  "| d31 (pC/N) | 90 | 175 | 20 |",
  "| Tc (°C) | 152 | 360 | 125 |",
].join("\n");
const html = formatMarkdownLite(table);
check("渲染成 <table>", html.includes("<table"), html.slice(0, 160));
check("有表头", html.includes("<th>性能</th>"), html.slice(0, 200));
check("表体 2 行", (html.match(/<tr>/g) || []).length === 3, html);
check("单元格值保留", html.includes("<td>175</td>") && html.includes("<td>Tc (°C)</td>"));
check("没有残留竖线文本", !html.includes("|---"), html.slice(0, 200));
check("表格不套 <p>", !/<p>\s*<div class="md-table-wrap"/.test(html), html.slice(0, 120));
check("thead/tbody 齐全", html.includes("<thead>") && html.includes("<tbody>"));

const mixed = "Table I. 压电材料的性能\n\n" + table;
const html2 = formatMarkdownLite(mixed);
check("表格与正文并存", html2.includes("<p>Table I. 压电材料的性能</p>") && html2.includes("<table"), html2.slice(0, 200));

const withMath = "| 量 | 值 |\n|---|---|\n| $d_{31}$ | 90 |";
const html3 = formatMarkdownLite(withMath);
check("表格单元格里的公式仍被 KaTeX 占位", html3.includes('class="math-placeholder math-inline"'), html3);

const notTable = "这个句子里有个 | 竖线，但不是表格。";
check("非表格不误判", !formatMarkdownLite(notTable).includes("<table"), formatMarkdownLite(notTable));

// ---------------------------------------------------------------------------
// 2026-11-25 审计 P1：含 `|` 的行内公式会把表格单元格**切碎**
//
// 根因：行内公式在更早的步骤已经还原成
// `<span class="math-placeholder math-inline">P(A|B)</span>`，
// 而切单元格用的 `split("|")` 不认这个占位 → 把公式里的竖线
// 当成了单元格分隔符 → 单元格被拆成两半、公式本身也被劈开。
// ---------------------------------------------------------------------------
console.log("== 公式里的竖线（2026-11-25）==");
const pipeMath = [
  "| 量 | 值 |",
  "|---|---|",
  "| 联合概率 | $P(A|B)$ |",
  "| 模长 | $|\\alpha|$ |",
].join("\n");
const htmlPM = formatMarkdownLite(pipeMath);
check("公式里的竖线不拆单元格（2 行 × 2 格 = 4 个 <td>）",
      (htmlPM.match(/<td/g) || []).length === 4, htmlPM);
check("公式里的竖线原样保留",
      htmlPM.includes("P(A|B)") && htmlPM.includes("|\\alpha|"), htmlPM);
check("公式仍是 KaTeX 占位（没被劈成两段）",
      (htmlPM.match(/math-placeholder math-inline/g) || []).length === 2, htmlPM);

const pipeCode = [
  "| 命令 | 说明 |",
  "|---|---|",
  "| `a | b` | 管道 |",
].join("\n");
const htmlPC = formatMarkdownLite(pipeCode);
check("行内代码里的竖线也不拆单元格",
      (htmlPC.match(/<td/g) || []).length === 2, htmlPC);

// ---- 变异自检：把「保护公式占位」这一步改掉，断言必须变红 ----
console.log("== 变异自检 ==");
const MUT_TARGET = "s = s.replace(_ATOM_PH_RE, (m) => {";
if (SRC.indexOf(MUT_TARGET) < 0 || SRC.indexOf(MUT_TARGET) !== SRC.lastIndexOf(MUT_TARGET)) {
  check("变异目标串在源码里唯一（护栏本身没坏）", false,
        "找不到或找到多处: " + MUT_TARGET);
} else {
  const mutatedSrc = SRC.replace(MUT_TARGET,
                                 "s = s.replace(/NEVER_MATCHES_ZZZ/, (m) => {");
  const mutatedFormat = buildFactory(mutatedSrc)(escapeHtml).formatMarkdownLite;
  const mutatedHtml = mutatedFormat(pipeMath);
  // ⚠ 判据是「公式内容还在不在」，不是「单元格个数」：
  //   多出来的那一格会被表头列数裁掉 → 单元格数看着没变，
  //   但公式已经被劈成 `$P(A` —— **内容被静默丢弃**，比多一格更伤。
  check("变异被抓住（不再保护公式占位 → 公式被劈开、内容被丢弃）",
        !mutatedHtml.includes("P(A|B)"), "变异后仍含 P(A|B)");
}

console.log("== 公式标记 ==");
const marker = "其中，α 的值由下式给出：\n\n⟦FORMULA:45.1,138.3,272.3,169.5⟧\n\n后文继续。";
const html4 = formatMarkdownLite(marker);
check("标记原样保留（前端再换图）", html4.includes("⟦FORMULA:45.1,138.3,272.3,169.5⟧"), html4);
check("标记不被转义破坏", !html4.includes("&lt;") || !html4.includes("⟦") === false, html4);

// 导出成 .md 时不能残留坐标标记（坐标换个 PDF 就失效了）
const un = factory(escapeHtml);
// ⚠️ 必须用上面已归一化行尾的 SRC，不能再 readFileSync 一遍：
//    原始文件可能是 CRLF，下面按 "\n    }\n" 找函数结尾就找不到 →
//    切出空串 → new Function 里 "_unrenderMathPlaceholders is not defined"。
const unrender = new Function("escapeHtml",
  SRC.slice(SRC.indexOf("function _unrenderMathPlaceholders("),
            SRC.indexOf("\n    }\n", SRC.indexOf("function _unrenderMathPlaceholders(")) + 7)
  + "\nreturn _unrenderMathPlaceholders;")(escapeHtml);
const outMd = unrender("<p>x ⟦FORMULA:45.1,138.3,272.3,169.5⟧ y</p>");
check("导出 .md 时公式标记被换成人话", !outMd.includes("⟦FORMULA") && outMd.includes("见 PDF 原文"), outMd);

console.log("== 其他 ==");
const plain = "**粗体** 和 `代码`\n\n第二段";
const html5 = formatMarkdownLite(plain);
check("粗体仍生效", html5.includes("<strong>粗体</strong>"), html5);
check("段落仍是 <p>", (html5.match(/<p>/g) || []).length === 2, html5);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
