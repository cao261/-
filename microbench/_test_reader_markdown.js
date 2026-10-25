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

// 归一化行尾：仓库约定 LF，但 Windows 上的脚本可能写成 CRLF，
// 而下面按 "\n    }\n" 找函数结尾 —— CRLF 会让它找不到。
const SRC = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8")
  .replace(/\r\n/g, "\n");

// Slice a top-level function out of index.html. The file uses 4-space
// indentation for functions declared at script-block level, so the function
// ends at the next "\n    }\n". (A brace counter would trip over regex
// quantifiers like /\n{2,}/ and template literals.)
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

const factory = new Function("escapeHtml",
  sliceFn("formatMarkdownLite") + "\n" + sliceFn("_matchMarkdownTable") +
  "\nreturn {formatMarkdownLite, _matchMarkdownTable};");
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

console.log("== 公式标记 ==");
const marker = "其中，α 的值由下式给出：\n\n⟦FORMULA:45.1,138.3,272.3,169.5⟧\n\n后文继续。";
const html4 = formatMarkdownLite(marker);
check("标记原样保留（前端再换图）", html4.includes("⟦FORMULA:45.1,138.3,272.3,169.5⟧"), html4);
check("标记不被转义破坏", !html4.includes("&lt;") || !html4.includes("⟦") === false, html4);

// 导出成 .md 时不能残留坐标标记（坐标换个 PDF 就失效了）
const un = factory(escapeHtml);
const src = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
const unrender = new Function("escapeHtml",
  src.slice(src.indexOf("function _unrenderMathPlaceholders("),
            src.indexOf("\n    }\n", src.indexOf("function _unrenderMathPlaceholders(")) + 7)
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
