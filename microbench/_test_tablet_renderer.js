/*
 * Unit tests for the tablet app's Markdown renderer.
 *
 * The renderer runs in the browser, but it is plain JS — so it can be driven
 * from node with a tiny DOM shim. That matters: a renderer bug shows up as
 * "the formula shows as raw LaTeX on my tablet", and the only place that is
 * cheap to catch is a test.
 *
 * Run:  node microbench/_test_tablet_renderer.js
 */
const path = require("path");
const fs = require("fs");

// ---- minimal DOM shim -------------------------------------------------
global.window = global;
global.document = {
  addEventListener: () => {},
  getElementById: () => null,
  createElement: () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} },
                          appendChild() {}, setAttribute() {}, addEventListener() {} }),
  documentElement: { setAttribute() {}, getAttribute: () => null },
};
global.indexedDB = { open: () => ({}) };
global.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
global.matchMedia = () => ({ matches: false });
global.katex = {
  renderToString(tex, opts) {
    // 模拟真实 KaTeX 的最小输出：够测试判断"有没有走 KaTeX 这条路"
    return `<span class="katex${opts && opts.displayMode ? " katex-display" : ""}" data-tex="${tex}"></span>`;
  },
};

require(path.join(__dirname, "static", "tablet", "app.js"));
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

// ---- 1. 标题 / 段落 / 列表 -------------------------------------------
console.log("\n[1] 基本块级结构");
let h = T.renderMarkdown("# 一级标题\n\n正文段落。\n");
check(h.includes("<h1>一级标题</h1>"), "一级标题");
check(h.includes("<p>正文段落。</p>"), "段落");
h = T.renderMarkdown("## 二级\n\n### 三级\n");
check(h.includes("<h2>二级</h2>"), "二级标题");
check(h.includes("<h3>三级</h3>"), "三级标题");

console.log("\n[2] 列表 / 任务列表");
h = T.renderMarkdown("- 甲\n- 乙\n");
check(h.includes("<ul>") && h.includes("<li>甲</li>"), "无序列表");
h = T.renderMarkdown("1. 甲\n2. 乙\n");
check(h.includes("<ol>") && h.includes("<li>甲</li>"), "有序列表");
h = T.renderMarkdown("- [ ] 未完成\n- [x] 已完成\n");
check(h.includes('class="task-list"'), "任务列表 class");
check(h.includes('class="done"'), "已完成项带 done");

console.log("\n[3] 表格 / 代码 / 引用 / 分隔线");
h = T.renderMarkdown("| a | b |\n|---|---|\n| 1 | 2 |\n");
check(h.includes("<table>") && h.includes("<th>a</th>") && h.includes("<td>2</td>"), "表格");
h = T.renderMarkdown("```python\nprint(1)\n```\n");
check(h.includes("<pre><code") && h.includes("print(1)"), "代码块");
h = T.renderMarkdown("行内 `code` 文本\n");
check(h.includes("<code>code</code>"), "行内代码");
h = T.renderMarkdown("> 引用一句话\n");
check(h.includes("<blockquote>"), "引用");
h = T.renderMarkdown("---\n");
check(h.includes("<hr>"), "分隔线");

console.log("\n[4] 行内格式");
h = T.renderMarkdown("**粗体** 与 *斜体* 与 `代码`\n");
check(h.includes("<strong>粗体</strong>"), "粗体");
check(h.includes("<em>斜体</em>"), "斜体");
h = T.renderMarkdown("[链接](https://example.com)\n");
check(h.includes('<a href="https://example.com"') && h.includes(">链接</a>"), "链接");
check(h.includes('rel="noopener noreferrer"'), "外链带 rel=noopener");
h = T.renderMarkdown("![[图片.png]]\n");
check(h.includes("wikilink-missing"), "Obsidian 双链降级处理");

console.log("\n[5] 公式");
h = T.renderMarkdown("值 $E = mc^2$ 结束\n");
check(h.includes('class="katex"') && !h.includes("$E = mc"), "行内公式走 KaTeX");
// 带环境的块级公式
h = T.renderMarkdown("$$\n\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}\n$$\n");
check(h.includes("katex-display"), "带 align 环境的块级公式走 KaTeX");
// 超长的单行 $$ 应当保持块级
const longTex = "\\frac{a}{b} + \\frac{c}{d} + \\frac{e}{f} + \\frac{g}{h} + \\frac{i}{j} + \\frac{k}{l}";
h = T.renderMarkdown("$$" + longTex + "$$\n");
check(h.includes("katex-display"), "超过 60 字符的单行 $$ 保持块级");
// 短的单行 $$..$$ 应当降级成行内（与桌面端 formatMarkdownLite 同一套规则）
h = T.renderMarkdown("$$x=1$$\n");
check(!h.includes("katex-display"), "短的单行 $$ 降级为行内");
// 单个不成对的 $ 不是公式 —— 笔记里 "($33/篇)" 这种写法很常见。
const ex1 = T.extractMath("一篇 ($33/篇)");
check(ex1.formulas.length === 0, "不成对的 $ 不当公式");
// 成对的 $ 会被当公式 —— 与桌面端 formatMarkdownLite 是同一套规则，
// 两边行为必须一致，否则同一个公式在网页上和在平板上长得不一样。
const ex2 = T.extractMath("价格 $5 和 $6 都不是公式");
check(ex2.formulas.length === 1 && ex2.formulas[0].tex === "5 和 ",
      "成对的 $ 按桌面版同规则当公式");

console.log("\n[6] frontmatter");
const fm = "---\ntitle: \"测试\"\nlast_updated: 2026-01-01\n---\n\n# 正文标题\n";
check(T.stripFrontmatter(fm).indexOf("---") !== 0, "frontmatter 被剥离");
h = T.renderMarkdown(fm);
check(!h.includes("last_updated"), "渲染结果里不出现 frontmatter 字段");
check(h.includes("正文标题"), "frontmatter 之后的正文正常渲染");
check(T.firstHeading(fm) === "正文标题", "firstHeading 跳过 frontmatter");

console.log("\n[7] HTML 消毒: 外来 HTML 不能执行脚本");
h = T.renderMarkdown("<script>alert(1)<\/script>\n");
check(!/<script/i.test(h), "script 标签被丢掉");
check(!h.includes("alert(1)"), "script 内容也一起丢掉");
h = T.renderMarkdown('<img src="x" onerror="alert(1)">\n');
check(!/onerror/i.test(h), "onerror 事件属性被清掉");
h = T.renderMarkdown("[x](javascript:alert(1))\n");
check(!/href="javascript:/i.test(h), "javascript: 链接被拦下");
h = T.renderMarkdown('<a href="javascript:alert(1)">点</a>\n');
check(!/href="javascript:/i.test(h), "原始 HTML 里的 javascript: 链接被拦下");
h = T.renderMarkdown('<iframe src="http://evil"></iframe>\n');
check(!/<iframe/i.test(h), "iframe 被丢掉");
// 白名单内的排版标签必须保留 —— 01_Literature 的双语精读笔记里存着
// <h2 class="md-h2">这种原始 HTML，转义了就等于毁掉那些笔记。
h = T.renderMarkdown('<h2 class="md-h2">翻译</h2>\n');
check(h.includes("<h2 class=\"md-h2\">翻译</h2>"), "白名单内的 HTML 标签原样保留");

console.log("\n[8] hash 稳定性");
check(T.hash("abc") === T.hash("abc"), "同样内容 hash 相同");
check(T.hash("abc") !== T.hash("abd"), "不同内容 hash 不同");
check(T.hash("") === "45h", "空串 hash 与 JS 基线一致 (45h)");

console.log("\n" + "=".repeat(56));
console.log("PASS " + pass + "  FAIL " + fail);
process.exit(fail ? 1 : 0);
