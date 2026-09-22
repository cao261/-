"""
Obsidian Vault Markdown Generator & Manager for MicroBench.

V1.1 hardening:
- Path-traversal protection via _safe_path() (resolves symlinks, rejects anything outside BASE_DIR).
- Jinja2-based template rendering with StrictUndefined (catches missing/typo'd variables early).
- Strict category whitelist (paper topic_category, troubleshoot category).
- All file writes go through Path.write_text(encoding="utf-8") with explicit encoding.
"""

import datetime
import os
import re
from pathlib import Path

import jinja2

BASE_DIR = Path(__file__).resolve().parent.parent

# Whitelist of topic categories (kept in sync with frontend <select> options).
# Empty tuple entries (=those in the frontend dropdown) intentionally excluded here
# because that list is the source of truth for the dropdown — no need to duplicate.
ALLOWED_TOPIC_CATEGORIES = (
    "01_Device_TCAD_器件仿真",
    "02_Circuit_EDA_电路与算法",
    "03_Materials_Physics_材料物性",
    "04_Survey_综述",
)

ALLOWED_TROUBLESHOOT_CATEGORIES = (
    "TCAD仿真收敛",
    "EDA环境与授权",
    "Linux与服务器",
    "Python与CUDA",
)

ALLOWED_PROJECT_TYPES = ("TCAD", "EDA")

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _safe_path(base_dir: Path, *parts) -> Path:
    """
    Resolve *parts under base_dir and ensure the resulting absolute path is inside base_dir.
    Raises ValueError on path-traversal attempts (e.g. parts containing '..' or absolute paths).
    """
    base_resolved = base_dir.resolve()
    # First join as-is to detect early traversal in raw parts
    joined = base_dir.joinpath(*parts)
    resolved = joined.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        raise ValueError(
            f"非法路径: '{parts}' 解析后位于 base 目录之外 (拒绝遍历攻击)"
        )
    return resolved

def sanitize_filename(name: str) -> str:
    """Make string safe for Windows filesystem while preserving meaning."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name).strip("_")
    return name[:80]  # Avoid excessively long path names

# ---------------------------------------------------------------------------
# Jinja2 template engine
# ---------------------------------------------------------------------------

def _render_template(tpl_path: Path, **vars) -> str:
    """
    Read a markdown template file and render with Jinja2.
    Uses StrictUndefined so that any {{var}} typo or missing variable is caught
    at write-time rather than silently rendering as empty string.
    """
    if not tpl_path.exists():
        # Caller is responsible for handling missing template; raise loudly.
        raise FileNotFoundError(f"模板不存在: {tpl_path}")
    src = tpl_path.read_text(encoding="utf-8")
    env = jinja2.Environment(
        loader=jinja2.BaseLoader(),
        undefined=jinja2.StrictUndefined,
        autoescape=False,           # markdown / LaTeX, no HTML escaping
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    tmpl = env.from_string(src)
    return tmpl.render(**vars)

def search_vault(query: str, top_k: int = 5, snippet_chars: int = 400) -> list[dict]:
    """
    Simple keyword-based retrieval over all markdown files in the vault.
    Scoring (per file):
      - frontmatter `title` keyword hit: +10
      - frontmatter `tags` keyword hit: +5
      - frontmatter `category` keyword hit: +3
      - body keyword occurrence: +2 per occurrence (capped at +20)
      - heading (## ...) keyword hit: +4
    Returns top_k chunks sorted by score (descending).
    """
    import re as _re
    query = (query or "").strip()
    if not query:
        return []

    keywords = [w.lower() for w in _re.split(r"\s+", query) if w.strip()]
    if not keywords:
        return []

    patterns = [_re.compile(_re.escape(k), _re.IGNORECASE) for k in keywords]

    vault_roots = [
        BASE_DIR / "01_Literature",
        BASE_DIR / "02_Reproduction",
        BASE_DIR / "03_Knowledge",
    ]

    results = []
    for root in vault_roots:
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            fm = {}
            body = text
            fm_match = _re.match(r"^---\s*\n(.*?)\n---\s*\n", text, _re.DOTALL)
            if fm_match:
                fm_text = fm_match.group(1)
                body = text[fm_match.end():]
                for line in fm_text.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        fm[k.strip().lower()] = v.strip().strip('"').strip("'")

            score = 0
            title_str = fm.get("title", "")
            tags_str = fm.get("tags", "")
            category_str = fm.get("category", "")

            for pat in patterns:
                if pat.search(title_str):
                    score += 10
                if pat.search(tags_str):
                    score += 5
                if pat.search(category_str):
                    score += 3
                headings = _re.findall(r"^#{1,3}\s+(.+)$", body, _re.MULTILINE)
                for h in headings:
                    if pat.search(h):
                        score += 4
                body_hits = sum(1 for _ in pat.finditer(body))
                score += min(body_hits, 10) * 2

            if score == 0:
                continue

            snippet = _build_snippet(body, patterns, max_chars=snippet_chars)
            rel_path = str(p.relative_to(BASE_DIR)).replace("\\", "/")
            results.append({
                "path": rel_path,
                "title": title_str or p.stem,
                "tags": tags_str,
                "score": score,
                "snippet": snippet,
            })

    results.sort(key=lambda x: (x["score"], x["path"]), reverse=True)
    return results[:top_k]


def _build_snippet(body: str, patterns: list, max_chars: int = 400) -> str:
    """
    Extract a snippet around the first keyword match.
    Falls back to the first non-empty paragraph if no match.
    """
    import re as _re
    first_pos = None
    for pat in patterns:
        m = pat.search(body)
        if m and (first_pos is None or m.start() < first_pos):
            first_pos = m.start()
    if first_pos is None:
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:max_chars]
        return ""
    half = max_chars // 2
    start = max(0, first_pos - half)
    end = min(len(body), first_pos + half)
    snippet = body[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(body):
        snippet = snippet + "..."
    snippet = _re.sub(r"\n{2,}", "\n", snippet)
    return snippet[:max_chars]


# ---------------------------------------------------------------------------
# Vault stats
# ---------------------------------------------------------------------------

def get_vault_stats() -> dict:
    """Analyze current literature and reproduction status across the vault."""
    lit_dir = BASE_DIR / "01_Literature"
    repro_dir = BASE_DIR / "02_Reproduction"
    trouble_file = BASE_DIR / "03_Knowledge" / "踩坑与排错日记.md"
    weekly_dir = BASE_DIR / "04_Weekly_Reports"

    lit_files = []
    if lit_dir.exists():
        for p in lit_dir.glob("**/*.md"):
            if p.is_file() and not p.name.startswith("."):
                lit_files.append({
                    "title": p.stem,
                    "category": p.parent.name,
                    "path": str(p.relative_to(BASE_DIR)).replace("\\", "/"),
                    "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                })

    repro_projects = []
    if repro_dir.exists():
        for p in repro_dir.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                readme = p / "README.md"
                mtime = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
                repro_projects.append({
                    "name": p.name,
                    "has_readme": readme.exists(),
                    "path": str(p.relative_to(BASE_DIR)).replace("\\", "/"),
                    "mtime": mtime
                })

    trouble_count = 0
    if trouble_file.exists():
        content = trouble_file.read_text(encoding="utf-8")
        trouble_count = len(re.findall(r"###\s+坑\s+\d+", content))

    weekly_count = len(list(weekly_dir.glob("*.md"))) if weekly_dir.exists() else 0

    return {
        "literature_total": len(lit_files),
        "literature_list": sorted(lit_files, key=lambda x: x["mtime"], reverse=True)[:10],
        "reproduction_total": len(repro_projects),
        "reproduction_list": repro_projects,
        "troubleshoot_count": trouble_count,
        "weekly_count": weekly_count,
    }

# ---------------------------------------------------------------------------
# Literature card
# ---------------------------------------------------------------------------

def save_literature_card(data: dict) -> dict:
    """Generate and write a structured literature note from template."""
    title = data.get("title", "Untitled").strip()
    authors = data.get("authors", "").strip()
    year = str(data.get("year", datetime.datetime.now().year))
    venue = data.get("venue", "").strip()
    doi = data.get("doi", "").strip()
    arxiv = data.get("arxiv", "").strip()
    topic_category = data.get("topic_category", "01_Device_TCAD_器件仿真")
    tags = data.get("tags", "TCAD, NanoDevice")
    abstract = data.get("abstract", "").strip()
    status = data.get("status", "待精读")

    # Category whitelist
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        raise ValueError(
            f"非法 topic_category: '{topic_category}'. "
            f"允许值: {ALLOWED_TOPIC_CATEGORIES}"
        )

    tpl_path = BASE_DIR / "_Templates" / "01_Literature_Note_Template.md"
    today = datetime.date.today().isoformat()
    safe_title = sanitize_filename(title)
    pdf_name = f"{year}_{safe_title[:30]}.pdf"

    # Parse all tags (comma-separated) and format as YAML list for the frontmatter.
    # The template uses {{tags_csv}} which is comma-separated (YAML inline list syntax).
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if not tag_list:
        tag_list = ["Research"]
    tags_csv = ", ".join(tag_list)
    topic_tag = tag_list[0]  # First tag is the "primary" used in template body

    rendered = _render_template(
        tpl_path,
        title=title,
        authors=authors,
        year=year,
        venue=venue,
        doi=doi,
        arxiv=arxiv,
        topic_tag=topic_tag,
        tags_csv=tags_csv,
        date=today,
        pdf_name=pdf_name,
        status=status,
    )

    # Append abstract section if provided (templates don't have a placeholder for it,
    # and Jinja2's StrictUndefined would reject inserting it after a known heading).
    if abstract:
        abstract_section = f"\n\n## 0. 论文摘要 (Abstract)\n> {abstract}\n"
        rendered = rendered.replace(
            "## 1. 核心科学/工程问题",
            f"{abstract_section}\n## 1. 核心科学/工程问题",
            1,
        )

    # Resolve safe path under BASE_DIR in dedicated subfolder
    target_dir = _safe_path(BASE_DIR, "01_Literature", topic_category, "03_文献速览卡片")
    # Optional user-chosen subfolder (single component, no traversal).
    # Empty string = default (03_文献速览卡片 directly).
    subfolder_raw = (data.get("subfolder") or "").strip()
    if subfolder_raw:
        if "/" in subfolder_raw or "\\" in subfolder_raw or subfolder_raw in (".", ".."):
            raise ValueError(
                f"非法 subfolder: '{subfolder_raw}'. 必须是单层目录名,不能含 / 或 .."
            )
        safe_sub = sanitize_filename(subfolder_raw)
        if not safe_sub:
            raise ValueError(f"subfolder 清理后为空: '{subfolder_raw}'")
        target_dir = _safe_path(target_dir, safe_sub)
        target_dir.mkdir(parents=True, exist_ok=True)

    # Filename: honour user override, else default year + safe_title.
    filename_override = (data.get("filename_override") or "").strip()
    if filename_override:
        # Strip .md if user added it; we re-attach the canonical extension.
        if filename_override.lower().endswith(".md"):
            filename_override = filename_override[:-3]
        safe_base = sanitize_filename(filename_override)
        if not safe_base:
            raise ValueError(f"filename 清理后为空: '{filename_override}'")
        filename = f"{safe_base}.md"
    else:
        filename = f"{year}_{safe_title}.md"
    file_path = _safe_path(target_dir, filename)

    file_path.write_text(rendered, encoding="utf-8")
    return {
        "status": "success",
        "file_name": filename,
        "folder": str(file_path.parent.relative_to(BASE_DIR)).replace("\\", "/"),
        "relative_path": str(file_path.relative_to(BASE_DIR)).replace("\\", "/"),
        "pdf_name": pdf_name,
        "pdf_path": str((BASE_DIR / "01_Literature" / topic_category / "01_论文原文_PDF" / pdf_name).relative_to(BASE_DIR)).replace("\\", "/"),
    }

# ---------------------------------------------------------------------------
# Reproduction scaffold
# ---------------------------------------------------------------------------

def create_reproduction_workspace(data: dict) -> dict:
    """Create a reproduction project scaffold with checklists and subfolders."""
    raw_name = data.get("project_name", "").strip()
    if not raw_name:
        raw_name = f"repro_{datetime.date.today().strftime('%Y%m%d')}"
    project_name = sanitize_filename(raw_name)
    if not project_name:
        raise ValueError("project_name 净化后为空, 请使用合法字符 (字母/数字/下划线/短横线)")

    target_paper = data.get("target_paper", "待关联论文").strip()
    project_type = data.get("project_type", "TCAD").upper()
    toolchain = data.get("toolchain", "Sentaurus TCAD 2023").strip()
    description = data.get("description", "").strip()

    if project_type not in ALLOWED_PROJECT_TYPES:
        raise ValueError(
            f"非法 project_type: '{project_type}'. 允许值: {ALLOWED_PROJECT_TYPES}"
        )

    proj_dir = _safe_path(BASE_DIR, "02_Reproduction", project_name)
    proj_dir.mkdir(parents=True, exist_ok=True)

    for sub in ["configs", "scripts", "data", "plots"]:
        (proj_dir / sub).mkdir(exist_ok=True)

    today = datetime.date.today().isoformat()
    if project_type == "TCAD":
        tpl_path = BASE_DIR / "_Templates" / "02_TCAD_Reproduction_Template.md"
        rendered = _render_template(
            tpl_path,
            project_name=project_name,
            target_paper_link=target_paper,
            device_type=description or "Advanced NanoDevice",
            tcad_tool=toolchain,
            project_folder=project_name,
            date=today,
        )
    else:  # EDA
        tpl_path = BASE_DIR / "_Templates" / "03_EDA_Circuit_Reproduction_Template.md"
        rendered = _render_template(
            tpl_path,
            project_name=project_name,
            target_paper_link=target_paper,
            type=description or "EDA Placement/Routing/STA",
            toolchain=toolchain,
            repo_url=data.get("repo_url", "https://github.com/"),
            commit_hash="main",
            date=today,
        )

    readme_path = _safe_path(proj_dir, "README.md")
    readme_path.write_text(rendered, encoding="utf-8")

    return {
        "status": "success",
        "project_name": project_name,
        "path": str(proj_dir.relative_to(BASE_DIR)).replace("\\", "/"),
    }

# ---------------------------------------------------------------------------
# Troubleshoot log
# ---------------------------------------------------------------------------

def record_troubleshoot(data: dict) -> dict:
    """Record a troubleshooting entry into the knowledge base."""
    tool_name = data.get("tool_name", "TCAD/EDA").strip()
    category = data.get("category", "仿真收敛").strip()
    error_keyword = data.get("error_keyword", "未知错误").strip()
    raw_error = data.get("raw_error", "").strip()
    root_cause = data.get("root_cause", "").strip()
    solution = data.get("solution", "").strip()
    today = datetime.date.today().isoformat()

    if category not in ALLOWED_TROUBLESHOOT_CATEGORIES:
        raise ValueError(
            f"非法 category: '{category}'. "
            f"允许值: {ALLOWED_TROUBLESHOOT_CATEGORIES}"
        )

    trouble_file = BASE_DIR / "03_Knowledge" / "踩坑与排错日记.md"
    trouble_file = _safe_path(BASE_DIR, "03_Knowledge", "踩坑与排错日记.md")
    trouble_file.parent.mkdir(parents=True, exist_ok=True)

    entry = f"""

---

### 坑记录：[{category}] {error_keyword} ({today})
- **工具/环境**：{tool_name}
- **错误现场**：
```text
{raw_error}
```
- **根本原因**：
{root_cause}
- **解决方案**：
```bash
{solution}
```
"""
    if trouble_file.exists():
        existing = trouble_file.read_text(encoding="utf-8")
        trouble_file.write_text(existing + entry, encoding="utf-8")
    else:
        trouble_file.write_text(f"# 踩坑与排错日记\n{entry}", encoding="utf-8")

    return {"status": "success", "keyword": error_keyword}

# ---------------------------------------------------------------------------
# Weekly report
# ---------------------------------------------------------------------------

def generate_weekly_report() -> dict:
    """Automatically aggregate week's activity into a weekly report draft."""
    today = datetime.date.today()
    start_date = (today - datetime.timedelta(days=7)).isoformat()
    end_date = today.isoformat()
    week_num = today.strftime("%W")
    week_id = f"{today.year}_Week{week_num}"

    tpl_path = BASE_DIR / "_Templates" / "04_Weekly_Report_Template.md"
    stats = get_vault_stats()
    recent_lits = stats.get("literature_list", [])[:3]

    # Build the bullet list of literature progress for the template.
    # Template has a single {{paper_1_link}} placeholder; we pass a single string
    # of newline-joined lines (instead of multiple vars to keep the template simple).
    if recent_lits:
        lit_lines = "\n".join(
            f"- [x] 文献进展：[[{lit['path']}|{lit['title']}]]"
            for lit in recent_lits
        )
    else:
        lit_lines = "- [ ] 本周待记录新精读文献"

    rendered = _render_template(
        tpl_path,
        week_id=week_id,
        start_date=start_date,
        end_date=end_date,
        one_line_summary="本周推进了核心器件仿真网格收敛与最新文献调研。",
        paper_1_link=lit_lines,
        reproduction_link="02_Reproduction/repro_cfet_sub3nm/README|CFET仿真复现",
    )

    weekly_dir = _safe_path(BASE_DIR, "04_Weekly_Reports")
    weekly_dir.mkdir(parents=True, exist_ok=True)
    filename = f"Weekly_{today.strftime('%Y%m%d')}_{week_id}.md"
    file_path = _safe_path(weekly_dir, filename)
    file_path.write_text(rendered, encoding="utf-8")

    return {
        "status": "success",
        "filename": filename,
        "path": str(file_path.relative_to(BASE_DIR)).replace("\\", "/"),
    }


def save_bilingual_reading_card(data: dict) -> dict:
    """
    Save a bilingual reading card (paired English + Chinese sections) into 01_Literature/{category}/02_双语精读笔记.
    Optionally bundles QA interaction records collected during reading.
    """
    title = data.get("title", "").strip() or "Untitled_Paper"
    authors = data.get("authors", "").strip() or "Unknown"
    year = int(data.get("year") or 2024)
    topic_category = data.get("topic_category", "01_Device_TCAD_器件仿真")
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        topic_category = "01_Device_TCAD_器件仿真"

    sections = data.get("sections", [])
    qa_records = data.get("qa_records") or []
    safe_title = sanitize_filename(title)

    # Filename override (single component, no .md suffix)
    fn_override = (data.get("filename_override") or "").strip()
    if fn_override:
        if fn_override.lower().endswith(".md"):
            fn_override = fn_override[:-3]
        safe_base = sanitize_filename(fn_override)
        if not safe_base:
            raise ValueError(f"filename 清理后为空: '{fn_override}'")
        filename = f"{safe_base}.md"
    else:
        filename = f"{year}_{safe_title[:35]}_双语精读.md"

    today = datetime.date.today().isoformat()
    lines = [
        "---",
        f"title: \"{title}\"",
        f"authors: \"{authors}\"",
        f"year: {year}",
        f"date: {today}",
        f"category: \"{topic_category}\"",
        "tags: [Literature, Bilingual, 双语精读笔记]",
        "status: 精读中",
        "---",
        "",
        f"# {title}",
        "",
        f"> **作者**: {authors} ({year})  ",
        f"> **归档分类**: [[01_Literature/{topic_category}/02_双语精读笔记]]  ",
        f"> **所属专题**: `{topic_category}`",
        "",
        "---",
        "",
        "## 📑 双语精读对照",
        "",
    ]

    for sec in sections:
        sec_title = sec.get("title", "未命名章节")
        trans = (sec.get("translation") or "").strip()
        orig = (sec.get("text") or "").strip()
        lines.append(f"### {sec_title}\n")
        if trans:
            trans_block = "\n".join(f"> {line}" if line.strip() else ">" for line in trans.splitlines())
            lines.append(f"> [!NOTE] 中文精译\n{trans_block}\n")
        if orig:
            lines.append(f"<details>\n<summary><b>展开对应英文原文 (第 {sec.get('page_start', 1)}-{sec.get('page_end', 1)} 页)</b></summary>\n\n{orig}\n\n</details>\n")
        lines.append("---\n")

    if qa_records:
        lines.append("## 🤖 划词精读答疑与物理推导 (AI Q&A Records)\n")
        for i, qa in enumerate(qa_records, 1):
            q_text = (qa.get("question") or "").strip()
            sel_text = (qa.get("selection") or "").strip()
            ans_text = (qa.get("answer") or "").strip()
            mode = qa.get("mode", "explain")
            mode_desc = {"explain": "💡 概念与物理推导", "math": "📐 公式与数学拆解", "experiment": "🧪 仿真复现与实验验证"}.get(mode, "💡 概念答疑")
            lines.append(f"### 疑问 {i}：{q_text} ({mode_desc})\n")
            if sel_text:
                sel_quote = "\n".join(f"> {l}" if l.strip() else ">" for l in sel_text.splitlines())
                lines.append(f"**选中上下文**:\n{sel_quote}\n")
            if ans_text:
                lines.append(f"**AI 解答**:\n\n{ans_text}\n")
            lines.append("---\n")

    content = "\n".join(lines)
    target_dir = _safe_path(BASE_DIR, "01_Literature", topic_category, "02_双语精读笔记")
    # Optional user-chosen subfolder
    subfolder_raw = (data.get("subfolder") or "").strip()
    if subfolder_raw:
        if "/" in subfolder_raw or "\\" in subfolder_raw or subfolder_raw in (".", ".."):
            raise ValueError(
                f"非法 subfolder: '{subfolder_raw}'. 必须是单层目录名,不能含 / 或 .."
            )
        safe_sub = sanitize_filename(subfolder_raw)
        if not safe_sub:
            raise ValueError(f"subfolder 清理后为空: '{subfolder_raw}'")
        target_dir = _safe_path(target_dir, safe_sub)
        target_dir.mkdir(parents=True, exist_ok=True)
    file_path = _safe_path(target_dir, filename)
    file_path.write_text(content, encoding="utf-8")

    return {
        "status": "success",
        "file_name": filename,
        "folder": str(file_path.parent.relative_to(BASE_DIR)).replace("\\", "/"),
        "relative_path": str(file_path.relative_to(BASE_DIR)).replace("\\", "/"),
    }


def save_qa_card(data: dict) -> dict:
    """
    Save an individual text selection Q&A note into 01_Literature/{topic_category}/04_AI划词答疑.
    """
    title = (data.get("title") or "Untitled_Paper").strip()
    question = (data.get("question") or "未命名疑问").strip()
    selection = (data.get("selection") or "").strip()
    answer = (data.get("answer") or "").strip()
    mode = data.get("mode", "explain")
    mode_labels = {
        "explain": "💡 概念与物理推导",
        "math": "📐 公式与数学拆解",
        "experiment": "🧪 仿真复现与实验验证",
    }
    mode_desc = mode_labels.get(mode, "💡 概念解析")

    topic_category = data.get("topic_category", "01_Device_TCAD_器件仿真")
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        topic_category = "01_Device_TCAD_器件仿真"

    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%Y-%m-%d %H:%M:%S")

    safe_q = sanitize_filename(question)[:30].rstrip("_")
    safe_title = sanitize_filename(title)[:30].rstrip("_")

    # Filename override (single component, no .md suffix)
    fn_override = (data.get("filename_override") or "").strip()
    if fn_override:
        if fn_override.lower().endswith(".md"):
            fn_override = fn_override[:-3]
        safe_base = sanitize_filename(fn_override)
        if not safe_base:
            raise ValueError(f"filename 清理后为空: '{fn_override}'")
        filename = f"{safe_base}.md"
    else:
        filename = f"QA_{now.strftime('%Y%m%d_%H%M%S')}_{safe_q or safe_title}.md"

    # Format Markdown
    sel_block = ""
    if selection:
        sel_lines = "\n".join(f"> {l}" if l.strip() else ">" for l in selection.splitlines())
        sel_block = f"## 📌 选中原文 / 上下文\n{sel_lines}\n\n---\n\n"

    content = f"""---
title: "答疑: {question[:60]}"
paper: "{title}"
category: "{topic_category}"
date: {today_str}
mode: "{mode}"
tags: [Literature, QnA, AI划词答疑, {mode}]
status: 已完成
---

# 🤖 AI 划词精读答疑：{question}

> **关联文献**: [[{title}]]  
> **归档位置**: `01_Literature/{topic_category}/04_AI划词答疑/`  
> **答疑类型**: {mode_desc}  
> **记录时间**: {time_str}  

---

{sel_block}## 💡 AI 深度解答与物理推导

{answer}

---
*由 MicroBench AI 划词答疑助手自动生成并归档至 Obsidian 知识库*
"""

    target_dir = _safe_path(BASE_DIR, "01_Literature", topic_category, "04_AI划词答疑")
    # Optional user-chosen subfolder
    subfolder_raw = (data.get("subfolder") or "").strip()
    if subfolder_raw:
        if "/" in subfolder_raw or "\\" in subfolder_raw or subfolder_raw in (".", ".."):
            raise ValueError(
                f"非法 subfolder: '{subfolder_raw}'. 必须是单层目录名,不能含 / 或 .."
            )
        safe_sub = sanitize_filename(subfolder_raw)
        if not safe_sub:
            raise ValueError(f"subfolder 清理后为空: '{subfolder_raw}'")
        target_dir = _safe_path(target_dir, safe_sub)
        target_dir.mkdir(parents=True, exist_ok=True)
    file_path = _safe_path(target_dir, filename)
    file_path.write_text(content, encoding="utf-8")

    return {
        "status": "success",
        "file_name": filename,
        "folder": str(file_path.parent.relative_to(BASE_DIR)).replace("\\", "/"),
        "relative_path": str(file_path.relative_to(BASE_DIR)).replace("\\", "/"),
    }