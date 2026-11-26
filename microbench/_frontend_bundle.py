# -*- coding: utf-8 -*-
"""将模块化前端（index.html + static/desktop/styles/*.css + static/desktop/js/**/*.js）
拼装为单份 HTML 源码，供既有 DOM / CSS / JS 切片护栏直接抽取与变异自检。
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_INDEX = STATIC_DIR / "index.html"
DESKTOP_DIR = STATIC_DIR / "desktop"

# 按依赖拓扑排序加载（与 browser ES module 加载语义完全等价）
JS_MODULE_ORDER = (
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
)

CSS_ORDER = (
    "00-tokens.css",
    "01-layout.css",
    "02-library.css",
    "03-labs.css",
    "04-reader.css",
)

_IMPORT_RE = re.compile(
    r"(?ms)^[ \t]*import\s+(?:[\s\S]*?\s+from\s+)?[\"'][^\"']+[\"'];?[ \t]*\r?\n?"
)
_EXPORT_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*export\s*\{[\s\S]*?\};?[ \t]*\r?\n?"
)
_EXPORT_DEFAULT_RE = re.compile(
    r"(?m)^[ \t]*export\s+default\s+"
)


def strip_esm_syntax(js_text: str) -> str:
    """去掉模块头尾的 import / export，保留函数与声明用于单域切片评估。"""
    out = _IMPORT_RE.sub("", js_text)
    out = _EXPORT_BLOCK_RE.sub("", out)
    out = _EXPORT_DEFAULT_RE.sub("", out)
    return out


def load_desktop_source(index_path: Path | str | None = None) -> str:
    """读取 index.html，并将关联的 desktop CSS / JS 模块按原位置内联展开。"""
    idx = Path(index_path) if index_path is not None else DEFAULT_INDEX
    html = idx.read_text(encoding="utf-8").replace("\r\n", "\n")
    static_root = idx.parent
    desktop_root = static_root / "desktop"

    # 若尚未拆分（老式单文件），直接原样返回
    if not (desktop_root / "js").is_dir() or not (desktop_root / "styles").is_dir():
        return html

    css_parts: list[str] = []
    seen_css: set[str] = set()
    for name in CSS_ORDER:
        p = desktop_root / "styles" / name
        if p.is_file():
            css_parts.append(p.read_text(encoding="utf-8").replace("\r\n", "\n"))
            seen_css.add(name)
    for p in sorted((desktop_root / "styles").glob("*.css")):
        if p.name not in seen_css:
            css_parts.append(p.read_text(encoding="utf-8").replace("\r\n", "\n"))
    css_bundle = "\n  <style>\n" + "\n".join(css_parts) + "\n  </style>"

    # 替换第一处 desktop CSS link，移除其余 desktop CSS link
    css_link_re = re.compile(
        r'[ \t]*<link[^>]+href=["\']/static/desktop/styles/[^"\']+["\'][^>]*>\r?\n?'
    )
    first_css = True

    def _repl_css(_m: re.Match) -> str:
        nonlocal first_css
        if first_css:
            first_css = False
            return css_bundle + "\n"
        return ""

    if css_link_re.search(html):
        html = css_link_re.sub(_repl_css, html)

    js_parts: list[str] = []
    seen_js: set[str] = set()
    for rel in JS_MODULE_ORDER:
        p = desktop_root / "js" / rel
        if p.is_file():
            js_parts.append(strip_esm_syntax(p.read_text(encoding="utf-8").replace("\r\n", "\n")))
            seen_js.add(rel.replace("\\", "/"))
    for p in sorted((desktop_root / "js").rglob("*.js")):
        rel = str(p.relative_to(desktop_root / "js")).replace("\\", "/")
        if rel not in seen_js:
            js_parts.append(strip_esm_syntax(p.read_text(encoding="utf-8").replace("\r\n", "\n")))
    js_bundle = "<script>\n" + "\n".join(js_parts) + "\n</script>"

    mod_script_re = re.compile(
        r'<script[^>]+src=["\']/static/desktop/js/[^"\']+["\'][^>]*>\s*</script>'
    )
    if mod_script_re.search(html):
        html = mod_script_re.sub(lambda _m: js_bundle, html, count=1)

    return html


def strip_js_comments(js: str) -> str:
    """剥除 JS 行/块注释，保留字符串、模板串与正则字面量（防止 /\"/g 破坏引号配对）。"""
    out: list[str] = []
    i, n = 0, len(js)
    prev = ""
    while i < n:
        c = js[i]
        if c in "\"'`":
            q = c
            out.append(c)
            i += 1
            while i < n:
                if js[i] == "\\":
                    out.append(js[i:i + 2])
                    i += 2
                    continue
                out.append(js[i])
                if js[i] == q:
                    i += 1
                    break
                i += 1
            prev = "x"
            continue
        if js.startswith("//", i):
            j = js.find("\n", i)
            i = n if j < 0 else j
            prev = "\n"
            continue
        if js.startswith("/*", i):
            j = js.find("*/", i + 2)
            i = n if j < 0 else j + 2
            prev = " "
            continue
        # 正则字面量：/ 前面若不是标识符/数字/)]，则视为正则而非除号
        if c == "/" and prev not in ("x", ")]") and not (prev and (prev.isalnum() or prev in "_$)]")):
            j = i + 1
            in_class = False
            ok = False
            while j < n:
                ch = js[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    break
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    ok = True
                    break
                j += 1
            if ok:
                out.append(js[i:j + 1])
                i = j + 1
                prev = "x"
                continue
        out.append(c)
        if not c.isspace():
            prev = c
        i += 1
    return "".join(out)
