"use strict";
/**
 * 将模块化桌面前端（index.html + desktop/styles + desktop/js）装配成单一文本，
 * 供 Node 行为级护栏抽取真实函数/CSS 并做变异自检。
 */
const fs = require("fs");
const path = require("path");

const DEFAULT_INDEX = path.join(__dirname, "static", "index.html");

const JS_MODULE_ORDER = [
  "core/utils.js",
  "core/overlay-esc.js",
  "state/store.js",
  "state/readpos.js",
  "reader/canvas-manager.js",
  "reader/ai-activity.js",
  "reader/annotations.js",
  "reader/qa-copilot.js",
  "reader/pdf-reader.js",
  "reader/shortcuts.js",
  "views/dashboard.js",
  "views/library.js",
  "views/labs.js",
  "main.js",
];

const CSS_ORDER = [
  "00-tokens.css",
  "01-layout.css",
  "02-library.css",
  "03-labs.css",
  "04-reader.css",
];

function stripEsmSyntax(jsText) {
  return jsText
    .replace(/^[ \t]*import\s+(?:[\s\S]*?\s+from\s+)?["'][^"']+["'];?[ \t]*\r?\n?/gm, "")
    .replace(/^[ \t]*export\s*\{[\s\S]*?\};?[ \t]*\r?\n?/gm, "")
    .replace(/^[ \t]*export\s+default\s+/gm, "");
}

function walkJsFiles(dir, prefix = "") {
  if (!fs.existsSync(dir)) return [];
  const out = [];
  for (const ent of fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const rel = prefix ? `${prefix}/${ent.name}` : ent.name;
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) out.push(...walkJsFiles(full, rel));
    else if (ent.isFile() && ent.name.endsWith(".js")) out.push(rel);
  }
  return out;
}

function loadDesktopSource(indexPath) {
  const idx = indexPath || DEFAULT_INDEX;
  let html = fs.readFileSync(idx, "utf8").replace(/\r\n/g, "\n");
  const desktopRoot = path.join(path.dirname(idx), "desktop");
  const stylesDir = path.join(desktopRoot, "styles");
  const jsDir = path.join(desktopRoot, "js");

  if (!fs.existsSync(stylesDir) || !fs.existsSync(jsDir)) {
    return html;
  }

  const cssParts = [];
  const seenCss = new Set();
  for (const name of CSS_ORDER) {
    const fp = path.join(stylesDir, name);
    if (fs.existsSync(fp)) {
      cssParts.push(fs.readFileSync(fp, "utf8").replace(/\r\n/g, "\n"));
      seenCss.add(name);
    }
  }
  for (const name of fs.readdirSync(stylesDir).sort()) {
    if (name.endsWith(".css") && !seenCss.has(name)) {
      cssParts.push(fs.readFileSync(path.join(stylesDir, name), "utf8").replace(/\r\n/g, "\n"));
    }
  }
  const cssBundle = "\n  <style>\n" + cssParts.join("\n") + "\n  </style>";

  let firstCss = true;
  html = html.replace(/[ \t]*<link[^>]+href=["']\/static\/desktop\/styles\/[^"']+["'][^>]*>\r?\n?/g, () => {
    if (firstCss) {
      firstCss = false;
      return cssBundle + "\n";
    }
    return "";
  });

  const jsParts = [];
  const seenJs = new Set();
  for (const rel of JS_MODULE_ORDER) {
    const fp = path.join(jsDir, rel);
    if (fs.existsSync(fp)) {
      jsParts.push(stripEsmSyntax(fs.readFileSync(fp, "utf8").replace(/\r\n/g, "\n")));
      seenJs.add(rel);
    }
  }
  for (const rel of walkJsFiles(jsDir)) {
    if (!seenJs.has(rel)) {
      jsParts.push(stripEsmSyntax(fs.readFileSync(path.join(jsDir, rel), "utf8").replace(/\r\n/g, "\n")));
    }
  }
  const jsBundle = "<script>\n" + jsParts.join("\n") + "\n</script>";
  html = html.replace(/<script[^>]+src=["']\/static\/desktop\/js\/[^"']+["'][^>]*>\s*<\/script>/, () => jsBundle);
  return html;
}

/** 从汇编源码中按平衡花括号抽取顶层函数声明，跳过字符串与注释。 */
function extractTopFunction(src, name) {
  const esc = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp("(?:async\\s+)?function\\s+" + esc + "\\s*\\(");
  const m = re.exec(src);
  if (!m) throw new Error("找不到函数: " + name);
  const openIdx = src.indexOf("{", m.index);
  if (openIdx < 0) throw new Error("函数没有 {: " + name);
  let i = openIdx, depth = 0, prev = "";
  const n = src.length;
  while (i < n) {
    const ch = src[i];
    if (ch === "/" && src[i + 1] === "/") {
      const nl = src.indexOf("\n", i);
      i = nl < 0 ? n : nl + 1;
      continue;
    }
    if (ch === "/" && src[i + 1] === "*") {
      const cl = src.indexOf("*/", i + 2);
      i = cl < 0 ? n : cl + 2;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === "`") {
      const q = ch;
      i += 1;
      while (i < n) {
        if (src[i] === "\\") { i += 2; continue; }
        if (src[i] === q) { i += 1; break; }
        i += 1;
      }
      prev = "x";
      continue;
    }
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(m.index, i + 1);
    }
    if (!/\s/.test(ch)) prev = ch;
    i += 1;
  }
  throw new Error("函数括号未配平: " + name);
}

module.exports = {
  DEFAULT_INDEX,
  JS_MODULE_ORDER,
  CSS_ORDER,
  stripEsmSyntax,
  loadDesktopSource,
  extractTopFunction,
};
