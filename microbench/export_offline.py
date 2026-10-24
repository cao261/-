#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Export the vault as ONE self-contained HTML file for offline reading on a
tablet.

Why a single file
-----------------
The tablet gets the file by AirDrop / WeChat / a USB stick and opens it
from the Files app with file://. A directory tree of assets does not survive
that trip (iOS Safari will not follow relative paths from a file:// page
reliably, and a folder of notes means the user has to copy the whole
structure). One .html file just works.

Why the formulas are rendered here
----------------------------------
KaTeX needs ~254 KB of woff2 fonts and 275 KB of JavaScript. Shipping the
runtime means the tablet needs either the network or a second file next to
the HTML. Instead we render every formula to static HTML at export time and
inline only the stylesheet (with the fonts as base64 data URIs). The result
renders identically, offline, with no JavaScript involved.

The math extraction rules mirror formatMarkdownLite() in the live app, so a
note looks the same in both places:
  * `$$...$$` is display math, except that a single-line formula of <= 60
    characters without a \\begin/\\end environment is treated as inline
  * `$...$` is inline math and may not span a newline

Usage:
    python microbench/export_offline.py [output.html]
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Folders that hold user content. logs/, microbench/, tests/ are excluded.
VAULT_DIRS = [
    "00_Dashboard",
    "01_Literature",
    "02_Reproduction",
    "03_Knowledge",
    "04_Weekly_Reports",
]

KATEX_DIST = HERE / "_vendor" / "node_modules" / "katex" / "dist"

# Placeholders for extracted math. These have to survive Python-Markdown
# untouched, so they use only word characters and @.
MATH_TOKEN = "@@MBK{idx}@@"
MATH_TOKEN_RE = re.compile(r"@@MBK(\d+)@@")

# Fenced code blocks and inline code are protected from math extraction:
# `$x$` inside a shell snippet is a dollar sign, not a formula.
CODE_SPLIT_RE = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)")


@dataclass
class Note:
    idx: int
    rel: str
    title: str
    folder: str
    subfolder: str
    body: str
    html_body: str = ""
    formulas: list = field(default_factory=list)
    search_text: str = ""


# ---------------------------------------------------------------------------
# 1. Collect
# ---------------------------------------------------------------------------

def collect_notes() -> list[Note]:
    notes: list[Note] = []
    idx = 0
    for folder in VAULT_DIRS:
        base = REPO / folder
        if not base.is_dir():
            continue
        for md in sorted(base.rglob("*.md")):
            rel = md.relative_to(REPO).as_posix()
            if rel.startswith(("logs/", "microbench/")):
                continue
            try:
                body = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            body, fm = split_frontmatter(body)
            title = (fm.get("title") or "").strip() or _first_heading(body) or md.stem
            sub = md.parent.relative_to(base).as_posix()
            notes.append(
                Note(
                    idx=idx,
                    rel=rel,
                    title=title,
                    folder=folder,
                    subfolder="" if sub == "." else sub,
                    body=body,
                )
            )
            idx += 1
    return notes


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
        if line.startswith("## "):
            return line[3:].strip()
    return ""


# ---------------------------------------------------------------------------
# PDF 清单 + 笔记↔PDF 关联
# ---------------------------------------------------------------------------

# 卡片里引用原文 PDF 的几种写法。
# 「本地路径」多数在**表格**里（| **本地路径** | `path` |），不是在正文，
# 所以分隔符不能只认冒号。
_PDF_REF_RES = [
    re.compile(r"\[\[([^\]|]+\.pdf)\]"),                       # [[2024_xxx.pdf]]
    re.compile(r"\*\*本地路径\*\*\s*[|｜:：]\s*`([^`]*\.pdf)`"),
    re.compile(r"\*\*PDF(?: 路径|路径)?\*\*\s*[|｜:：]\s*`([^`]*\.pdf)`"),
    re.compile(r"(?m)^\s*[-*]\s*\*{0,2}本地路径\*{0,2}\s*[|｜:：]\s*`([^`]*\.pdf)`"),
]


def collect_pdfs() -> dict:
    """扫描 vault 里所有 PDF。

    返回 {pdf_id: {"id", "name", "size"}}，pdf_id 是相对 vault 根的正斜杠路径。

    这份清单是平板端「打开原文 PDF」的前提：平板不能猜哪个 PDF 对应哪篇
    笔记，必须由 PC 导出时把真实路径写进去。
    """
    pdfs = {}
    for folder in VAULT_DIRS:
        base = REPO / folder
        if not base.is_dir():
            continue
        for pdf in base.rglob("*.pdf"):
            try:
                size = pdf.stat().st_size
            except OSError:
                size = 0
            rel = pdf.relative_to(REPO).as_posix()
            pdfs[rel] = {
                "id": rel,
                "name": pdf.name,
                "size": size,
                "stem": pdf.stem,
            }
    return pdfs


def _norm_text(s: str) -> str:
    """归一化：去掉非字母数字与中文，转小写。"""
    return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", s).lower()


def match_pdf_by_title(title: str, pdfs: dict, threshold: int = 70) -> list:
    """按标题模糊匹配 PDF —— 与 app.py 的 /api/paper/find_pdf 同一套算法。

    卡片里的 [[xxx.pdf]] 引用多半是模板占位（文件名被截断/是示例），
    真正可靠的线索是论文标题。两条路径都试：
      1. 正文里写的路径（表格里的「本地路径」列最常见）
      2. 标题模糊匹配

    阈值比 PC 端严（70 vs 45）：PC 上是用户手动搜、能看到一堆候选再挑；
    平板上是自动挂「原文 PDF」按钮，误挂比不挂更糟 —— 宁可少挂，
    用户从「文献库」里自己点开也不费事。
    """
    if not title:
        return []
    title_clean = _norm_text(title)
    title_words = [_norm_text(w) for w in re.split(r"\W+", title)
                    if len(_norm_text(w)) >= 3][:8]
    if not title_clean:
        return []

    best = None
    best_score = 0
    for pid, meta in pdfs.items():
        stem_clean = _norm_text(meta["stem"])
        if not stem_clean:
            continue
        score = 0
        if title_clean[:18] in stem_clean or stem_clean in title_clean:
            score += 70
        elif title_clean[:10] in stem_clean:
            score += 45
        word_hits = sum(1 for w in title_words if w and w in stem_clean)
        if word_hits >= 3:
            score += 25 + word_hits * 10
        elif word_hits == 2:
            # 两个词重合很容易撞车 ("magnetic sensor" 满地都是),
            # 只能给很低的分, 不足以越过阈值
            score += 20
        if score > best_score:
            best_score = score
            best = pid
    if best and best_score >= threshold:
        return [best]
    return []


def link_pdfs_for_note(body: str, pdfs: dict) -> list:
    """从笔记正文里找出它引用的 PDF（按文件名匹配，容忍路径写法差异）。"""
    by_name = {}
    for pid, meta in pdfs.items():
        by_name.setdefault(meta["name"], pid)
        by_name.setdefault(meta["stem"], pid)

    found = []
    for rx in _PDF_REF_RES:
        for m in rx.finditer(body):
            raw = m.group(1).strip().replace("\\", "/")
            if not raw:
                continue
            # 完整相对路径直接命中
            if raw in pdfs:
                if raw not in found:
                    found.append(raw)
                continue
            # 只给了文件名 -> 按文件名/主干名匹配
            name = raw.rsplit("/", 1)[-1]
            for key in (name, name[:-4] if name.lower().endswith(".pdf") else name):
                pid = by_name.get(key)
                if pid and pid not in found:
                    found.append(pid)
                    break
    return found



FRONTMATTER_RE = re.compile(r"\A﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def split_frontmatter(body: str) -> tuple[str, dict]:
    """Strip the YAML frontmatter block Obsidian puts at the top of a note.

    Without this the reader shows `title: "..." last_updated: 2026-09-20` as
    if it were body text, followed by a stray horizontal rule. The front
    matter's `title` is more reliable than the first heading, so it is
    returned separately.
    """
    m = FRONTMATTER_RE.match(body)
    if not m:
        return body, {}
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip('"').strip("'")
        meta[key.strip()] = val
    return body[m.end():], meta


# ---------------------------------------------------------------------------
# 2. Math extraction (mirrors formatMarkdownLite in the live app)
# ---------------------------------------------------------------------------

INLINE_MAX_CHARS = 60


def extract_math(text: str) -> tuple[str, list[tuple[str, bool]]]:
    """Replace formulas with placeholders.

    Returns (text_with_placeholders, [(tex, is_display), ...]).
    """
    formulas: list[tuple[str, bool]] = []

    def do_display(m: re.Match) -> str:
        body = m.group(1)
        trimmed = body.strip()
        is_multiline = "\n" in trimmed
        has_env = re.search(r"\\(begin|end)\s*\{[^}]+\}", trimmed)
        too_long = len(trimmed) > INLINE_MAX_CHARS
        display = bool(is_multiline or has_env or too_long)
        formulas.append((body, display))
        return MATH_TOKEN.format(idx=len(formulas) - 1)

    def do_inline(m: re.Match) -> str:
        formulas.append((m.group(1), False))
        return MATH_TOKEN.format(idx=len(formulas) - 1)

    # Only the parts of the document that are NOT code may contain formulas.
    parts = CODE_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            continue  # code segment — leave it alone
        part = re.sub(r"\$\$([\s\S]+?)\$\$", do_display, part)
        part = re.sub(r"(?<!\$)\$([^\$\n]+?)\$(?!\$)", do_inline, part)
        parts[i] = part
    return "".join(parts), formulas


# ---------------------------------------------------------------------------
# 3. KaTeX
# ---------------------------------------------------------------------------

def render_formulas(all_formulas: list[tuple[str, bool]]) -> dict:
    """Render every unique formula in one Node pass.

    Returns {"_per_occurrence": [...]} — one rendered entry per *occurrence*
    (not per unique formula), so the caller can slice it per note.

    NOTE: the empty case must still return the "_per_occurrence" key.
    Returning a bare {} made every formula-free vault die with
    KeyError: '_per_occurrence' — which is exactly the state of a brand-new
    vault, i.e. the first export anyone ever runs.
    """
    if not all_formulas:
        return {"_per_occurrence": []}
    unique: dict[tuple[str, bool], int] = {}
    payload = []
    for tex, display in all_formulas:
        key = (tex, display)
        if key not in unique:
            unique[key] = len(payload)
            payload.append({"id": len(payload), "tex": tex, "display": display})
    proc = subprocess.run(
        ["node", str(HERE / "_katex_batch.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"katex render failed: {proc.stderr[:800]}")
    results = json.loads(proc.stdout)

    # Map every occurrence back to the shared rendered result.
    out: dict[int, dict] = {}
    for (tex, display), uid in unique.items():
        res = results[uid]
        out[uid] = res
    # Rebuild a per-occurrence lookup: occurrence i -> rendered html
    per_occurrence = []
    for tex, display in all_formulas:
        per_occurrence.append(out[unique[(tex, display)]])
    return {"_per_occurrence": per_occurrence}


def build_katex_css() -> str:
    """katex.min.css with the woff2 fonts inlined as data URIs."""
    css_path = KATEX_DIST / "katex.min.css"
    fonts_dir = KATEX_DIST / "fonts"
    if not css_path.exists():
        return ""
    css = css_path.read_text(encoding="utf-8")

    cache: dict[str, str] = {}

    def repl(m: re.Match) -> str:
        fname = m.group(1)
        if fname not in cache:
            fpath = fonts_dir / fname
            if not fpath.exists():
                cache[fname] = ""
                return m.group(0)
            cache[fname] = base64.b64encode(fpath.read_bytes()).decode("ascii")
        return f'url(data:font/woff2;base64,{cache[fname]}) format("woff2")'

    css = re.sub(r'url\(fonts/([A-Za-z0-9_.-]+\.woff2)\)\s*format\("woff2"\)', repl, css)
    # 清掉所有非 woff2 的字体回退 (.woff / .ttf)。留着它们浏览器会
    # 去请求 fonts/KaTeX_xxx.ttf —— 单文件产物里没有这个文件，离线时
    # 会留下 404 噪音，字体也可能回退到系统衬线体。
    css = re.sub(r',\s*url\(fonts/[A-Za-z0-9_.-]+\.(?:woff|ttf)\)\s*format\("(?:woff|truetype)"\)',
                 "", css)
    return css


# ---------------------------------------------------------------------------
# 4. Markdown -> HTML
# ---------------------------------------------------------------------------

def markdown_to_html(text: str) -> str:
    import markdown

    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
        output_format="html",
    )


def resolve_wiki_links(text: str, titles: dict[str, str]) -> str:
    """Turn Obsidian [[links]] into markdown links when we know the target."""

    def repl(m: re.Match) -> str:
        raw = m.group(1)
        if "|" in raw:
            target, alias = raw.split("|", 1)
        else:
            target, alias = raw, raw
        target = target.strip()
        alias = alias.strip() or target
        key = target.lower()
        if key in titles:
            return f"[{alias}](#/{titles[key]})"
        return f'<span class="wikilink-missing">{alias}</span>'

    return re.sub(r"\[\[([^\]]+)\]\]", repl, text)


def build_note_html(note: Note, rendered: list[dict]) -> str:
    """Full pipeline for one note -> HTML string."""
    # 1. Math out
    with_math, formulas = extract_math(note.body)
    note.formulas = formulas

    # 2. Wiki links
    titles = getattr(note, "_titles", {})
    with_links = resolve_wiki_links(with_math, titles)

    # 3. Markdown
    body_html = markdown_to_html(with_links)

    # 4. Math back in
    def put(m: re.Match) -> str:
        i = int(m.group(1))
        if i >= len(rendered):
            return m.group(0)
        res = rendered[i]
        if res.get("error") or not res.get("html"):
            # KaTeX could not parse it — show the raw source instead of
            # silently dropping the formula.
            tex = formulas[i][0].strip()
            return f'<code class="math-error">{html.escape(tex)}</code>'
        return res["html"]

    body_html = MATH_TOKEN_RE.sub(put, body_html)
    return body_html


# ---------------------------------------------------------------------------
# 5. The reader shell
# ---------------------------------------------------------------------------

CSS = """
:root{
  --n-0:#FFFFFF; --n-25:#FDFCF9; --n-50:#F7F4ED; --n-100:#F1EDE3; --n-150:#EAE4D6;
  --n-200:#E1DACA; --n-300:#CFC5B1; --n-400:#A79C88; --n-500:#8A8071; --n-600:#6C6459;
  --n-700:#4E483F; --n-800:#332E28; --n-900:#1C1A17; --n-950:#121110;
  --surface-canvas:var(--n-50); --surface-raised:var(--n-0); --surface-sunken:var(--n-100);
  --surface-quiet:var(--n-25);
  --line-hairline:rgba(51,46,40,.08); --line-soft:#E5DFD3; --line-strong:#CFC5B1;
  --fg-strong:var(--n-900); --fg-base:var(--n-700); --fg-muted:var(--n-600); --fg-subtle:var(--n-500);
  --accent:#A8482C; --accent-soft:#F7EDE8; --accent-line:rgba(168,72,44,.28);
  --font-display:'Cormorant Garamond',Garamond,'Songti SC',serif;
  --font-ui:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  --font-cn:"PingFang SC","Microsoft YaHei","Hiragino Sans GB","Noto Sans CJK SC",sans-serif;
  --font-mono:'JetBrains Mono',ui-monospace,"Cascadia Code",Consolas,monospace;
  --sidebar-w:272px;
  --sa-top:env(safe-area-inset-top,0px); --sa-bottom:env(safe-area-inset-bottom,0px);
  --sa-left:env(safe-area-inset-left,0px); --sa-right:env(safe-area-inset-right,0px);
}
[data-theme="night"]{
  --surface-canvas:var(--n-950); --surface-raised:#1C1A17; --surface-sunken:#232019;
  --surface-quiet:#201D18;
  --line-hairline:rgba(239,236,230,.10); --line-soft:#2E2A24; --line-strong:#4E483F;
  --fg-strong:#EFECE6; --fg-base:#D8D2C6; --fg-muted:#A8A094; --fg-subtle:#8A8071;
  --accent:#C96A4A; --accent-soft:#2A1D17; --accent-line:rgba(201,106,74,.32);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;padding:0;height:100%}
body{
  background:var(--surface-canvas); color:var(--fg-base);
  font-family:var(--font-cn); font-size:16px; line-height:1.75;
  -webkit-text-size-adjust:100%; -webkit-font-smoothing:antialiased;
}
button{font-family:inherit}

/* ---------- top bar ---------- */
.topbar{
  position:sticky; top:0; z-index:50;
  display:flex; align-items:center; gap:12px;
  padding:calc(10px + var(--sa-top)) calc(16px + var(--sa-right)) 10px calc(16px + var(--sa-left));
  background:color-mix(in srgb, var(--surface-canvas) 90%, transparent);
  backdrop-filter:saturate(1.4) blur(14px); -webkit-backdrop-filter:saturate(1.4) blur(14px);
  border-bottom:1px solid var(--line-hairline);
}
.brand{font-family:var(--font-display); font-size:22px; color:var(--fg-strong); white-space:nowrap}
.brand em{color:var(--accent); font-style:italic}
.brand-sub{font-family:var(--font-ui); font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--fg-subtle)}
.spacer{flex:1}
.search{
  flex:0 1 320px; min-width:0; height:40px; padding:0 14px;
  font-family:var(--font-ui); font-size:16px; color:var(--fg-strong);
  background:var(--surface-raised); border:1px solid var(--line-soft); border-radius:999px; outline:none;
}
.search:focus{border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-soft)}
.icon-btn{
  display:inline-flex; align-items:center; justify-content:center;
  width:40px; height:40px; flex-shrink:0;
  border:1px solid var(--line-hairline); border-radius:999px;
  background:var(--surface-raised); color:var(--fg-muted); cursor:pointer;
  font-size:16px;
}
.menu-btn{display:none}

/* ---------- layout ---------- */
.shell{display:flex; align-items:flex-start; min-height:calc(100vh - 60px)}
.sidebar{
  position:sticky; top:60px; flex:0 0 var(--sidebar-w); width:var(--sidebar-w);
  height:calc(100vh - 60px); overflow-y:auto; overscroll-behavior:contain;
  padding:18px 12px calc(40px + var(--sa-bottom)) calc(12px + var(--sa-left));
  border-right:1px solid var(--line-hairline); background:var(--surface-canvas);
}
.group-title{
  margin:16px 0 6px; padding:0 10px;
  font-family:var(--font-ui); font-size:10px; font-weight:600; letter-spacing:.12em;
  text-transform:uppercase; color:var(--fg-subtle);
}
.group-title:first-child{margin-top:0}
.nav-item{
  display:block; width:100%; text-align:left;
  padding:9px 12px; border:0; border-radius:9px; background:transparent;
  color:var(--fg-base); font-family:var(--font-cn); font-size:14px; line-height:1.45;
  cursor:pointer; word-break:break-word;
}
.nav-item:hover{background:var(--surface-sunken)}
.nav-item.active{background:var(--accent-soft); color:var(--accent); font-weight:600}
.nav-sub{font-family:var(--font-ui); font-size:11px; color:var(--fg-subtle); padding:0 12px 4px}

.content{
  flex:1 1 auto; min-width:0;
  max-width:900px; margin:0 auto;
  padding:28px calc(24px + var(--sa-right)) calc(80px + var(--sa-bottom)) 28px;
}

/* ---------- article ---------- */
.doc{font-size:16.5px; line-height:1.85}
.doc h1{font-family:var(--font-display); font-size:2.05rem; font-weight:500; line-height:1.25; color:var(--fg-strong); margin:.2em 0 .5em}
.doc h2{font-family:var(--font-cn); font-size:1.35rem; font-weight:600; color:var(--fg-strong); margin:1.7em 0 .6em; padding-bottom:.28em; border-bottom:1px solid var(--line-soft)}
.doc h3{font-family:var(--font-cn); font-size:1.12rem; font-weight:600; color:var(--fg-strong); margin:1.5em 0 .5em}
.doc h4{font-family:var(--font-cn); font-size:1rem; font-weight:600; color:var(--fg-strong); margin:1.3em 0 .4em}
.doc p{margin:.85em 0}
.doc ul,.doc ol{margin:.85em 0; padding-left:1.4em}
.doc li{margin:.35em 0}
.doc strong{color:var(--fg-strong); font-weight:600}
.doc a{color:var(--accent); text-decoration:none; border-bottom:1px solid var(--accent-line)}
.doc hr{border:0; border-top:1px solid var(--line-soft); margin:2em 0}
.doc code{
  font-family:var(--font-mono); font-size:.86em;
  background:var(--surface-sunken); border:1px solid var(--line-hairline);
  border-radius:5px; padding:1px 5px; color:var(--fg-strong);
}
.doc pre{
  background:var(--surface-sunken); border:1px solid var(--line-hairline);
  border-radius:10px; padding:14px 16px; overflow-x:auto; -webkit-overflow-scrolling:touch;
}
.doc pre code{background:transparent; border:0; padding:0; font-size:13px; line-height:1.6}
.doc table{
  width:100%; border-collapse:collapse; margin:1.1em 0; font-size:.92em;
  display:block; overflow-x:auto; -webkit-overflow-scrolling:touch;
}
.doc th,.doc td{border:1px solid var(--line-soft); padding:8px 11px; text-align:left; vertical-align:top}
.doc th{background:var(--surface-sunken); font-weight:600; color:var(--fg-strong)}
.doc blockquote{
  margin:1.1em 0; padding:.2em 0 .2em 1em;
  border-left:3px solid var(--accent-line); color:var(--fg-muted);
}
.wikilink-missing{color:var(--fg-subtle); border-bottom:1px dotted var(--line-strong)}
.math-error{color:var(--accent); background:var(--accent-soft); border-color:var(--accent-line)}
.katex{font-size:1.04em}
.katex-display{margin:1.1em 0; overflow-x:auto; overflow-y:hidden; padding:2px 0}

/* ---------- meta / footer ---------- */
.doc-meta{
  margin-bottom:22px; padding-bottom:14px; border-bottom:1px solid var(--line-hairline);
  font-family:var(--font-ui); font-size:12px; color:var(--fg-subtle);
}
.empty{padding:60px 0; text-align:center; color:var(--fg-subtle); font-size:15px}
.no-result{padding:26px 14px; color:var(--fg-muted); font-size:14px}

/* ---------- tablet / touch ---------- */
@media (pointer: coarse){
  .icon-btn{width:44px;height:44px}
  .search{height:44px}
  .nav-item{padding:12px 12px; font-size:15px}
  .doc{font-size:17px}
}
@media (max-width:860px){
  .menu-btn{display:inline-flex}
  .search{flex:1 1 auto}
  .brand-sub{display:none}
  .sidebar{
    position:fixed; top:0; left:0; bottom:0; z-index:60;
    width:min(84vw, var(--sidebar-w)); height:100%;
    padding-top:calc(16px + var(--sa-top));
    transform:translateX(-102%); transition:transform .22s ease;
    box-shadow:0 0 40px rgba(0,0,0,.18);
  }
  .sidebar.open{transform:none}
  .scrim{
    position:fixed; inset:0; z-index:55; background:rgba(0,0,0,.42);
    display:none;
  }
  .scrim.show{display:block}
  .content{padding-left:calc(20px + var(--sa-left)); padding-right:calc(20px + var(--sa-right))}
}
@media (min-width:861px){ .scrim{display:none !important} }
@media print{
  .topbar,.sidebar,.scrim{display:none}
  .content{max-width:none;padding:0}
}
"""

JS = r"""
(function(){
  var DATA = JSON.parse(document.getElementById('mb-data').textContent);
  var byId = {}, byTitle = {};
  DATA.notes.forEach(function(n){ byId[n.id] = n; byTitle[n.title.toLowerCase()] = n.id; });
  var order = DATA.notes.map(function(n){ return n.id; });

  var elContent = document.getElementById('content');
  var elSidebar = document.getElementById('sidebar');
  var elSearch = document.getElementById('search');
  var elScrim = document.getElementById('scrim');

  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
    return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]; }); }

  function renderNav(filter){
    var groups = {}, order2 = [];
    DATA.notes.forEach(function(n){
      if(filter && !n.search.toLowerCase().includes(filter)) return;
      var g = n.group;
      if(!groups[g]){ groups[g] = []; order2.push(g); }
      groups[g].push(n);
    });
    var html = '';
    if(!order2.length){
      html = '<div class="no-result">没有匹配的笔记</div>';
    } else {
      order2.forEach(function(g){
        html += '<div class="group-title">' + esc(g) + '</div>';
        groups[g].forEach(function(n){
          html += '<button class="nav-item" data-id="' + n.id + '">' + esc(n.title) + '</button>';
        });
      });
    }
    elSidebar.innerHTML = html;
    highlightActive();
  }

  function highlightActive(){
    var id = currentId();
    var items = elSidebar.querySelectorAll('.nav-item');
    for(var i=0;i<items.length;i++){
      items[i].classList.toggle('active', items[i].getAttribute('data-id') === String(id));
    }
  }

  function currentId(){
    var m = location.hash.match(/^#\/note\/(\d+)/);
    if(m && byId[m[1]]) return parseInt(m[1], 10);
    return DATA.notes.length ? DATA.notes[0].id : 0;
  }

  function show(id){
    var n = byId[id];
    if(!n) return;
    elContent.innerHTML =
      '<div class="doc-meta">' + esc(n.rel) + '</div>' +
      '<article class="doc">' + n.html + '</article>';
    window.scrollTo(0, 0);
    highlightActive();
    elSidebar.classList.remove('open');
    elScrim.classList.remove('show');
  }

  function showList(ids){
    var html = '';
    ids.forEach(function(id){
      var n = byId[id];
      html += '<button class="nav-item" data-id="' + n.id + '">' + esc(n.title) +
              '<div class="nav-sub">' + esc(n.rel) + '</div></button>';
    });
    elContent.innerHTML = html || '<div class="empty">没有匹配的笔记</div>';
  }

  elSidebar.addEventListener('click', function(e){
    var b = e.target.closest('.nav-item');
    if(!b) return;
    location.hash = '#/note/' + b.getAttribute('data-id');
  });

  elContent.addEventListener('click', function(e){
    var a = e.target.closest('a[href^="#/note/"]');
    if(!a) return;
    e.preventDefault();
    location.hash = a.getAttribute('href');
  });

  var searchTimer = null;
  elSearch.addEventListener('input', function(){
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function(){
      var q = elSearch.value.trim().toLowerCase();
      if(!q){ renderNav(''); location.hash = '#/note/' + currentId(); return; }
      var hits = order.filter(function(id){
        var n = byId[id];
        return n.title.toLowerCase().indexOf(q) >= 0 ||
               n.search.toLowerCase().indexOf(q) >= 0;
      });
      renderNav(q);
      showList(hits);
    }, 160);
  });

  document.getElementById('menu-btn').addEventListener('click', function(){
    elSidebar.classList.add('open');
    elScrim.classList.add('show');
  });
  elScrim.addEventListener('click', function(){
    elSidebar.classList.remove('open');
    elScrim.classList.remove('show');
  });

  var themeBtn = document.getElementById('theme-btn');
  themeBtn.addEventListener('click', function(){
    var cur = document.documentElement.getAttribute('data-theme') === 'night' ? 'light' : 'night';
    document.documentElement.setAttribute('data-theme', cur);
    try{ localStorage.setItem('mb_theme', cur); }catch(e){}
    themeBtn.textContent = cur === 'night' ? '☀' : '☾';
  });

  window.addEventListener('hashchange', function(){ show(currentId()); });

  renderNav('');
  show(currentId());
  var t = document.documentElement.getAttribute('data-theme');
  themeBtn.textContent = t === 'night' ? '☀' : '☾';
})();
"""


def build_html(notes: list[Note], katex_css: str) -> str:
    payload = []
    for n in notes:
        payload.append({
            "id": n.idx,
            "title": n.title,
            "rel": n.rel,
            "group": n.folder,
            "html": n.html_body,
            "search": n.search_text[:20000],
        })

    groups = []
    for folder in VAULT_DIRS:
        members = [n for n in notes if n.folder == folder]
        if members:
            groups.append({
                "name": folder,
                "count": len(members),
            })

    data_json = json.dumps({"notes": payload, "groups": groups}, ensure_ascii=False)
    # The JSON lives in a <script type="application/json">; the only sequence
    # that could break out of it is a literal "</script>".
    data_json = data_json.replace("</", "<\\/")

    generated = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>MicroBench · 离线阅读版</title>
<meta name="theme-color" content="#F7F4ED">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="MicroBench">
<meta name="generator" content="MicroBench export_offline.py">
<meta name="exported" content="{generated}">
<style>
{CSS}
</style>
<style>
{katex_css}
</style>
</head>
<body>
<header class="topbar">
  <button class="icon-btn menu-btn" id="menu-btn" aria-label="目录">☰</button>
  <div>
    <div class="brand">Micro<em>Bench</em></div>
    <div class="brand-sub">离线阅读版 · {generated}</div>
  </div>
  <div class="spacer"></div>
  <input class="search" id="search" type="search" placeholder="搜索笔记…" autocomplete="off"
         autocapitalize="off" autocorrect="off" spellcheck="false">
  <button class="icon-btn" id="theme-btn" aria-label="切换主题">☾</button>
</header>

<div class="shell">
  <nav class="sidebar" id="sidebar"></nav>
  <main class="content" id="content"></main>
</div>
<div class="scrim" id="scrim"></div>

<script type="application/json" id="mb-data">{data_json}</script>
<script>
{JS}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------

def vault_fingerprint() -> tuple:
    """Cheap change detector: (path, mtime, size) for every note.

    Used by --watch to decide whether a re-export is worth doing. The
    offline bundle takes a couple of seconds to build, so a naive "always
    rebuild" loop would burn CPU for nothing.
    """
    items = []
    for folder in VAULT_DIRS:
        base = REPO / folder
        if not base.is_dir():
            continue
        for md in base.rglob("*.md"):
            try:
                st = md.stat()
            except OSError:
                continue
            items.append((str(md), int(st.st_mtime), st.st_size))
    items.sort()
    return tuple(items)


def export_once(out_path: Path, quiet: bool = False) -> bool:
    """Build the offline bundle. Returns True on success."""
    def say(*a):
        if not quiet:
            print(*a)

    say(f"[1/4] 扫描 vault ...")
    notes = collect_notes()
    if not notes:
        print("没有找到任何笔记", file=sys.stderr)
        return False
    say(f"      {len(notes)} 篇笔记")

    # Titles by lowercase name, for wiki-link resolution.
    titles = {}
    for n in notes:
        titles[n.title.lower()] = n.idx
        titles[Path(n.rel).stem.lower()] = n.idx

    say("[2/4] 提取公式 ...")
    all_formulas: list[tuple[str, bool]] = []
    per_note: list[list[tuple[str, bool]]] = []
    for n in notes:
        n._titles = titles  # type: ignore[attr-defined]
        _, forms = extract_math(n.body)
        per_note.append(forms)
        all_formulas.extend(forms)
    say(f"      {len(all_formulas)} 处公式")

    say("[3/4] 用 KaTeX 渲染公式 ...")
    rendered = render_formulas(all_formulas)["_per_occurrence"]

    say("[4/4] 生成 HTML ...")
    offset = 0
    for i, n in enumerate(notes):
        n.html_body = build_note_html(n, rendered[offset:offset + len(per_note[i])])
        offset += len(per_note[i])
        n.search_text = re.sub(r"[#*`$|>\[\]()]", " ", n.body)

    katex_css = build_katex_css()
    doc = build_html(notes, katex_css)
    out_path.write_text(doc, encoding="utf-8")

    size_kb = out_path.stat().st_size / 1024
    say(f"\n  完成: {out_path}")
    say(f"  大小: {size_kb:.0f} KB")
    say(f"  笔记: {len(notes)} 篇")
    return True


# ---------------------------------------------------------------------------
# 6. 平板工作台（可写 + 可同步 + 可用 AI）
# ---------------------------------------------------------------------------

TABLET_DIR = HERE / "static" / "tablet"


def detect_cloud_dir() -> Path | None:
    """找出一个「写进去就会上云」的目录。

    坚果云客户端的同步根不是 %USERPROFILE%\\Nutstore，而是
    <镜像根>\\<mirrorId>，镜像 id 从

        %APPDATA%\\Nutstore\\config\\UsersMap.json

    里读。而且 **在镜像根下新建顶层文件夹不会上云** —— 必须放进坚果云
    自带的「我的坚果云」里面（那是真正的云端沙箱），否则客户端会记
    sync_errors(reason=7)，上传量一直是 0。

    找不到就返回 None，让调用方退回工作台根目录。
    """
    import json as _json

    appdata = os.environ.get("APPDATA")
    if appdata:
        cfg = Path(appdata) / "Nutstore" / "config" / "UsersMap.json"
        try:
            data = _json.loads(cfg.read_text(encoding="utf-8"))
            cache = Path(data.get("cachePath", ""))
            mirror = (data.get("users") or {}).get(data.get("activeUser", ""), "")
            if cache and mirror:
                root = cache / str(mirror)
                for name in ("我的坚果云", "Nutstore"):
                    cand = root / name
                    if cand.is_dir():
                        return cand
                if root.is_dir():
                    return root
        except (OSError, ValueError, _json.JSONDecodeError):
            pass

    # 其它常见云盘
    home = Path.home()
    for cand in (home / "OneDrive", home / "BaiduNetdiskDownload",
                 home / "iCloudDrive"):
        if cand.is_dir():
            return cand
    return None


def build_tablet_html(notes: list[Note], katex_css: str, pdfjs: dict | None = None) -> str:
    """把平板工作台打包成一个自包含的 HTML 文件。

    与只读版的关键差别：

      * 携带 **Markdown 源码**而不是烘焙好的 HTML —— 平板上编辑后要能
        立刻重新渲染（含公式），所以渲染器必须跟着走。
      * 内联 KaTeX 的 JS 运行时 —— 只读版把公式烘焙进 HTML 就不需要它；
        可写版不行：用户在平板上敲的公式没人替它渲染。
      * 内联 app.js / styles.css —— 平板工作台的逻辑有上千行，藏在
        Python 的字符串模板里没法维护。
      * 内联 PDF.js —— 平板上要能直接翻论文原文。
    """
    index = (TABLET_DIR / "index.html").read_text(encoding="utf-8")
    app_css = (TABLET_DIR / "styles.css").read_text(encoding="utf-8")
    app_js = (TABLET_DIR / "app.js").read_text(encoding="utf-8")

    pdfjs = pdfjs or {}
    katex_js_path = KATEX_DIST / "katex.min.js"
    katex_js = katex_js_path.read_text(encoding="utf-8") if katex_js_path.exists() else ""

    # 图标内联，保持单文件
    icon_path = HERE / "static" / "apple-touch-icon.png"
    icon_uri = ""
    if icon_path.exists():
        icon_uri = "data:image/png;base64," + base64.b64encode(
            icon_path.read_bytes()).decode("ascii")

    payload = []
    for n in notes:
        payload.append({
            "id": n.rel,
            "title": n.title,
            "rel": n.rel,
            "group": n.folder,
            "body": n.body,
            "mtime": 0,
            "pdfs": getattr(n, "_pdfs", []),
        })
    pdfs = getattr(notes[0], "_pdfs_all", {}) if notes else {}
    data_json = json.dumps({"notes": payload, "pdfs": pdfs}, ensure_ascii=False)
    data_json = data_json.replace("</", "<\\/")

    out = index
    # ⚠ 占位符必须**先**替换, 再内联 CSS/JS。
    #   顺序反了的话, 内联的 app.js 里只要出现 "__DATA__" 这个字面量
    #   (哪怕只是注释里提了一句), 就会被整份数据 JSON 顶掉 ——
    #   实测踩过: app.js 的注释里多了句 "(__DATA__.pdfs)",
    #   导出的 HTML 直接从 72KB 涨到 332KB, 内联的 JS 全废。
    out = out.replace("__ICON__", icon_uri)
    out = out.replace("__DATA__", data_json)
    out = out.replace("<!--INLINE_KATEX_CSS-->", "<style>\n" + katex_css + "\n</style>")
    out = out.replace("<!--INLINE_APP_CSS-->", "<style>\n" + app_css + "\n</style>")
    if pdfjs.get("css"):
        out = out.replace("<!--INLINE_PDFJS_CSS-->",
                          "<style>\n" + pdfjs["css"] + "\n</style>")
    if pdfjs.get("worker"):
        # worker 源码放 text/plain 里，运行时取出来做成 Blob URL
        out = out.replace("<!--INLINE_PDFJS_WORKER-->",
                          '<script type="text/plain" id="mb-pdf-worker-src">\n'
                          + pdfjs["worker"] + "\n</script>")
    if pdfjs.get("js"):
        out = out.replace("<!--INLINE_PDFJS_JS-->",
                          "<script>\n" + pdfjs["js"] + "\n</script>")
    out = out.replace("<!--INLINE_KATEX_JS-->",
                      "<script>\n" + katex_js + "\n</script>")
    out = out.replace("<!--INLINE_APP_JS-->",
                      "<script>\n" + app_js + "\n</script>")
    return out


def export_tablet(out_path: Path, quiet: bool = False) -> bool:
    def say(*a):
        if not quiet:
            print(*a)

    say("[tablet] 扫描 vault ...")
    notes = collect_notes()
    if not notes:
        print("没有找到任何笔记", file=sys.stderr)
        return False
    say(f"[tablet] {len(notes)} 篇笔记")

    say("[tablet] 建立 PDF 清单与笔记关联 ...")
    pdfs = collect_pdfs()
    linked = 0
    for n in notes:
        refs = link_pdfs_for_note(n.body, pdfs)
        # 路径引用没命中时,退到标题模糊匹配 (与 PC 的 find_pdf 同一套算法)
        if not refs:
            refs = match_pdf_by_title(n.title, pdfs)
        n._pdfs = refs          # type: ignore[attr-defined]
        if refs:
            linked += 1
    if notes:
        notes[0]._pdfs_all = pdfs   # type: ignore[attr-defined]
    say(f"[tablet] {len(pdfs)} 个 PDF, {linked} 篇笔记关联到原文")

    say("[tablet] 内联 KaTeX 运行时与字体 ...")
    katex_css = build_katex_css()

    say("[tablet] 内联 PDF.js 运行时 ...")
    pdfjs = build_pdfjs_assets()
    if not pdfjs.get("js"):
        print("  [警告] 找不到 PDF.js vendor, 平板将无法打开原文 PDF")
    else:
        say(f"         pdf.js {len(pdfjs['js'])//1024} KB + worker "
            f"{len(pdfjs['worker'])//1024} KB + css {len(pdfjs['css'])//1024} KB")

    say("[tablet] 组装单文件 ...")
    doc = build_tablet_html(notes, katex_css, pdfjs)
    out_path.write_text(doc, encoding="utf-8")
    say(f"[tablet] 完成: {out_path}  ({out_path.stat().st_size/1024:.0f} KB)")
    return True


def build_pdfjs_assets() -> dict:
    """读取 vendor 里的 PDF.js 运行时。

    worker 必须单独一个 URL（它是个 Worker 脚本），但单文件产物不能有
    第二个文件 —— 所以把 worker 源码塞进 <script type="text/plain">，
    运行时用 Blob URL 交给 pdf.js。
    """
    base = HERE / "static" / "vendor" / "pdfjs"

    def read(name: str) -> str:
        p = base / name
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    js = read("pdf.min.js")
    worker = read("pdf.worker.min.js")
    css = read("pdf_viewer.css")
    # 内联进 HTML 时，源码里出现 </script 会提前关闭 script 标签。
    for name, text in (("pdf.min.js", js), ("pdf.worker.min.js", worker)):
        if "</script" in text.lower():
            raise RuntimeError(f"{name} 含 </script，不能直接内联")

    # PDF.js 的 CSS 会引用 images/ 下的图标。单文件产物里没有这些文件，
    # 浏览���会去请求然后 404。能内联的就内联（gif 只有 2 KB），
    # 仓库里本来就不存在的 svg 直接把引用删掉（可选的无障碍功能图标）。
    img_dir = base / "images"
    gif = img_dir / "loading-icon.gif"
    if gif.exists():
        b64 = base64.b64encode(gif.read_bytes()).decode("ascii")
        # CSS 里可能是 url(images/x.gif) 也可能是 url("images/x.gif")
        css = re.sub(r'url\(\s*["\']?images/loading-icon\.gif["\']?\s*\)',
                      f'url(data:image/gif;base64,{b64})', css)
    css = re.sub(r'url\(\s*["\']?images/[^)]+["\']?\s*\)', 'none', css)

    return {"js": js, "worker": worker, "css": css}


def main() -> int:
    import argparse
    import time

    ap = argparse.ArgumentParser(
        description="把 vault 导出成单文件离线版 (平板可用, 零外部依赖)",
    )
    ap.add_argument("output", nargs="?", default=None,
                    help="输出 HTML 路径 (默认: 工作台根目录/MicroBench_离线阅读版.html)")
    ap.add_argument("--watch", nargs="?", const=60, type=int, metavar="SECONDS",
                    help="常驻监视 vault, 有改动就重新导出 (默认每 60 秒检查一次)")
    ap.add_argument("--tablet", action="store_true",
                    help="导出「平板工作台」: 可编辑 + 可同步 + 可用 AI "
                         "(默认输出 MicroBench_平板工作台.html)")
    ap.add_argument("--quiet", action="store_true", help="不打印进度")
    args = ap.parse_args()

    # Without this, a redirected stdout (the sync script, a scheduler, a
    # pipe) buffers everything and the user sees no progress at all until
    # the process ends. A redirected stream also has to be told to use
    # UTF-8 — the console codepage may well be GBK, and the note titles are
    # full of characters that mangle there.
    try:
        if sys.stdout.isatty():
            sys.stdout.reconfigure(line_buffering=True)
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if args.tablet:
        default_out = REPO / "MicroBench_平板工作台.html"
    else:
        default_out = REPO / "MicroBench_离线阅读版.html"
    out_path = Path(args.output) if args.output else default_out
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    export = export_tablet if args.tablet else export_once

    if not args.watch:
        ok = export(out_path, quiet=args.quiet)
        if ok and not args.quiet:
            if args.tablet:
                print("\n  用法: 把这个文件放到平板上打开 (AirDrop / 微信 / 拷进云盘目录)。")
                print("        打开后在设置里「连接文件夹」,选中云盘里的 vault 目录,")
                print("        平板就能直接读写那个文件夹 (安卓 Chrome 支持真双向)。")
                print("        再在设置里填上 MiniMax API Key,AI 解读/翻译就能用了。")
            else:
                print("\n  用法: 把这个文件拷到平板 (AirDrop / 微信文件传输 / U 盘),")
                print("        在 iPad 上用 Safari 打开,或存到 '文件' App 后点开。")
                print("  想让平板永远拿到最新内容: 把输出目录设成云盘目录,")
                print("        平板直接用云盘 App 打开这个 HTML 即可。")
        return 0 if ok else 1

    # ---- watch mode --------------------------------------------------
    print(f"[watch] 监视 {REPO} ,每 {args.watch} 秒检查一次")
    print(f"[watch] 输出: {out_path}")
    print("[watch] Ctrl+C 退出")
    last = None
    try:
        while True:
            fp = vault_fingerprint()
            if fp != last:
                last = fp
                ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
                print(f"[watch {ts}] 检测到变化,重新导出 ...")
                export(out_path, quiet=True)
                print(f"[watch {ts}] 完成 "
                      f"({out_path.stat().st_size/1024:.0f} KB)")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\n[watch] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
