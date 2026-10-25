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
import shutil
import time
import threading
from pathlib import Path

import jinja2

BASE_DIR = Path(__file__).resolve().parent.parent

# 模板的「随代码走」副本。_Templates/ 属于用户的 Obsidian vault，被 .gitignore
# 排除在外，所以全新克隆的仓库里根本没有它 —— 直接读模板会 500
# ("模板不存在")，保存文献卡片 / 复现工程 / 周报全线失败。
# 因此包内必须自带一份兜底副本，vault 里没有就回退到它。
_BUNDLED_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# vault 里的模板目录（用户可自由改写；缺失时自动从包内副本补齐）
VAULT_TEMPLATES_DIRNAME = "_Templates"

# Serialises writes to the *shared* vault files (the troubleshooting diary
# and the weekly report folder). The server runs its sync endpoints in a
# thread pool, so two requests can genuinely be inside the same file at the
# same time; without a lock, a read-modify-write loses one of the two
# entries. Card writes go to distinct files and do not need it.
_VAULT_WRITE_LOCK = threading.Lock()

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
    r"""
    把 *parts 拼到 base_dir 下，并确保结果仍在 base_dir 内。
    Raises ValueError on path-traversal attempts (e.g. parts containing '..' or absolute paths).

    ⚠️ 包含性检查用 **逻辑路径**（os.path.abspath），**不**用 resolve()：
    vault 的数据目录（01_Literature 等）现在是 junction，指向坚果云的
    「我的坚果云\MicroBench工作台」。resolve() 会穿透 junction，把结果变成
    C 盘的真实路径 → 相对 E 盘的 base 就成了"越界" → 所有 PDF 报 400、
    vault 列表 500。junction 对用户是透明的，不该被判成越界。

    abspath 同样会把 `..` 归一化，所以遍历攻击照样被拦；绝对路径/换盘符
    也拦得住。唯一放宽的是"指向 vault 外的符号链接/junction"能被读到 ——
    但建 junction 需要对 vault 目录有写权限，那已在可信边界内。
    """
    base_logical = os.path.abspath(str(base_dir))
    # Reject dot-only components outright. Containment alone is NOT enough:
    # base/"02_Reproduction"/".." 归一化之后就是 base 的父目录，会悄悄
    # 指到 vault 外面去。
    for p in parts:
        if p is None:
            continue
        text = str(p)
        if text.strip(". ") == "":
            raise ValueError(
                f"非法路径分量: '{text!r}' 解析为目录本身/父目录 (拒绝遍历攻击)"
            )
        if os.path.isabs(text) or (len(text) > 1 and text[1] == ":"):
            raise ValueError(f"非法路径分量: '{text!r}' 是绝对路径 (拒绝越界)")
    # 逻辑路径检查（不 resolve）—— abspath 会把 .. 归一化
    joined = os.path.abspath(str(base_dir.joinpath(*parts)))
    if not (joined == base_logical or joined.startswith(base_logical + os.sep)):
        raise ValueError(
            f"非法路径: '{parts}' 解析后位于 base 目录之外 (拒绝遍历攻击)"
        )
    return Path(joined)


# vault 的「内容目录」—— 只有这些子树是用户真正的产出（笔记卡片 / 复现工程 /
# 知识库 / 周报 / 看板 / 模板）。
#
# 为什么需要它：BASE_DIR 既是仓库根、又是 vault 根，但仓库里还住着代码、
# 文档、tests/，以及 logs/ —— **logs/lan_token.txt 存着局域网共享口令**。
# `_safe_path` 只保证「不出 BASE_DIR」，挡不住「读仓库里的任何 .md/.txt」。
# /api/vault/file 的语义是「读一篇笔记」，那就必须把范围收回这些子树。
VAULT_CONTENT_DIRS = (
    "01_Literature",
    "02_Reproduction",
    "03_Knowledge",
    "04_Weekly_Reports",
    "00_Dashboard",
    "_Templates",
)


def _rel_to_base(base_dir: Path, path: Path) -> str:
    """绝对路径 → 相对 base 的 POSIX 风格路径；不在 base 下则返回 ""。"""
    try:
        return str(path.relative_to(base_dir)).replace("\\", "/").strip("/")
    except ValueError:
        return ""


def is_vault_content_path(base_dir: Path, path: Path) -> bool:
    """True 表示 `path` 落在 vault 的内容子树里（见 VAULT_CONTENT_DIRS）。

    审计 2026-10-26 M-1：/api/vault/file 的作用域曾经是「整个仓库」，
    实测 `GET /api/vault/file?relative_path=logs/lan_token.txt` 会
    **明文吐出局域网共享口令**，DELETE 同样放行（删掉口令 → LAN 访问
    静默关闭）。根因就是缺这一层「是不是 vault 内容」的判定。
    """
    rel = _rel_to_base(base_dir, path)
    if not rel:
        return False
    return rel.split("/", 1)[0] in VAULT_CONTENT_DIRS

def sanitize_filename(name: str) -> str:
    """Make string safe for Windows filesystem while preserving meaning."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name).strip("_")
    # Windows silently strips trailing dots and spaces from a file name, so
    # "paper." and "paper " both land as "paper" — and a title ending in
    # "." produced "2026_Title..md" (two dots) because the ".md" suffix
    # was appended afterwards. Strip them so the name on disk matches the
    # name the app reports.
    name = name.rstrip(". ")
    # A component made only of dots ('.', '..', '...') is not a filename — it
    # resolves to a directory. Returning "" makes callers reject it, which is
    # what stops `project_name=".."` from walking out of 02_Reproduction and
    # overwriting the vault-root README.md. Mid-name dots are still allowed
    # ("v1.2_results" must keep working).
    if name.strip(".") == "":
        return ""
    return name[:80]  # Avoid excessively long path names

# ---------------------------------------------------------------------------
# Jinja2 template engine
# ---------------------------------------------------------------------------

# One Environment for the whole process. Previously a fresh
# jinja2.Environment was constructed on every _render_template() call —
# i.e. on every card / report the user saves. Building an Environment
# parses the Jinja lexer and sets up the template machinery, so this was
# pure per-call overhead for an object whose configuration never varies.
_JINJA_ENV = jinja2.Environment(  # nosec B701
    loader=jinja2.BaseLoader(),
    undefined=jinja2.StrictUndefined,
    autoescape=False,           # nosec B701 — Markdown output (not HTML); see docstring
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)


def _resolve_template(name: str) -> Path:
    """定位一个模板文件：vault 里的优先，缺失时回退到包内自带副本。

    vault 的 `_Templates/` 是用户的地盘（可以随手改成自己的模板），但它被
    .gitignore 排除，克隆下来的仓库里没有；此时必须能用包内副本顶上，否则
    "保存文献卡片" 这类最基本的动作会直接 500。
    """
    user_tpl = BASE_DIR / VAULT_TEMPLATES_DIRNAME / name
    if user_tpl.exists():
        return user_tpl
    bundled = _BUNDLED_TEMPLATES_DIR / name
    if bundled.exists():
        return bundled
    raise FileNotFoundError(
        f"模板不存在: {name}（vault: {user_tpl} / 包内: {bundled}）"
    )


def ensure_vault_templates() -> list[str]:
    """把包内模板补进 vault 的 `_Templates/`（已存在的绝不覆盖）。

    用户的 vault 里应该有一份可自由改写的模板；全新克隆时它是空的，
    这里用包内副本补齐，让用户一眼就能找到并改成自己的格式。
    返回本次新写入的文件名列表。
    """
    written: list[str] = []
    if not _BUNDLED_TEMPLATES_DIR.is_dir():
        return written
    target_dir = BASE_DIR / VAULT_TEMPLATES_DIRNAME
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return written
    for src in sorted(_BUNDLED_TEMPLATES_DIR.glob("*.md")):
        dst = target_dir / src.name
        if dst.exists():
            continue
        try:
            shutil.copyfile(src, dst)
            written.append(src.name)
        except OSError:
            continue
    return written


def _render_template(tpl_path: Path, **vars) -> str:
    """
    Read a markdown template file and render with Jinja2.
    Uses StrictUndefined so that any {{var}} typo or missing variable is caught
    at write-time rather than silently rendering as empty string.

    autoescape=False is INTENTIONAL: the templates produce Obsidian-flavoured
    Markdown / LaTeX which is rendered by Obsidian, not a browser. HTML
    auto-escape would corrupt LaTeX subscripts like alpha_{b}. Output is
    written to disk and consumed only by Obsidian — not served as HTML.
    """
    if not tpl_path.exists():
        # Caller is responsible for handling missing template; raise loudly.
        raise FileNotFoundError(f"模板不存在: {tpl_path}")
    src = tpl_path.read_text(encoding="utf-8")
    tmpl = _JINJA_ENV.from_string(src)
    # Re-escape the YAML frontmatter after rendering (see _safe_frontmatter).
    return _safe_frontmatter(tmpl.render(**vars))

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

            # The heading list was recomputed for EVERY keyword — O(keywords
            # x body) with a regex over the whole document each time. It
            # does not depend on the keyword, so hoist it out.
            headings = _re.findall(r"^#{1,3}\s+(.+)$", body, _re.MULTILINE)

            for pat in patterns:
                if pat.search(title_str):
                    score += 10
                if pat.search(tags_str):
                    score += 5
                if pat.search(category_str):
                    score += 3
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

# 30s in-memory cache for get_vault_stats. The dashboard's loadStats() fires
# on every page mount, and walking 01_Literature/**/*.md every time is the
# single biggest cold-start cost in the recent-feed path. A 30s TTL means:
#   - Dashboard load: cache miss → real scan (acceptable, ~100ms)
#   - Within 30s of any other load: cache hit → ~0ms
#   - After save_*: writes invalidate the cache (see save_* functions below).
_VAULT_STATS_CACHE: dict | None = None
_VAULT_STATS_TS: float = 0.0
_VAULT_STATS_TTL = 30.0  # seconds


def _invalidate_vault_stats_cache() -> None:
    """Drop the cached vault-stats dict (called by all save_* functions)."""
    global _VAULT_STATS_CACHE, _VAULT_STATS_TS
    _VAULT_STATS_CACHE = None
    _VAULT_STATS_TS = 0.0


def _copy_stats(stats: dict) -> dict:
    """Shallow copy of the cached stats, with the two list fields copied too.

    Enough to stop a caller from mutating the shared cache (the entries
    themselves are never modified in place).
    """
    out = dict(stats)
    for key in ("literature_list", "reproduction_list"):
        if isinstance(out.get(key), list):
            out[key] = list(out[key])
    return out


def get_vault_stats() -> dict:
    """Analyze current literature and reproduction status across the vault.

    Result is cached in-process for 30s. Writes to the vault invalidate the
    cache so the next read sees fresh numbers.
    """
    # Cache hit: return a COPY. The cached object was handed out by
    # reference, so any caller that mutated it poisoned the cache for the
    # next 30 seconds (e.g. setting `literature_total = 99999`).
    global _VAULT_STATS_CACHE, _VAULT_STATS_TS
    now = time.monotonic()
    if _VAULT_STATS_CACHE is not None and (now - _VAULT_STATS_TS) < _VAULT_STATS_TTL:
        return _copy_stats(_VAULT_STATS_CACHE)

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
        # The counter used to match a LEGACY heading format ("### 坑 01：…")
        # that the writer no longer produces — it writes "### 坑记录：[…]
        # (date)". On the real vault this reported 4 instead of 10.
        #
        # The whitespace classes MUST be horizontal-only ([ \t]) and not
        # \s: \s matches newlines, so `^###\s+坑(?:\s*\d+|\s*记录)` happily
        # spanned several lines and counted the same heading many times
        # over — it reported 76 for a file that holds 10 entries.
        trouble_count = len(
            re.findall(
                r"^###[ \t]+坑(?:[ \t]*\d+|[ \t]*记录)[：:]",
                content,
                re.MULTILINE,
            )
        )

    weekly_count = len(list(weekly_dir.glob("*.md"))) if weekly_dir.exists() else 0

    result = {
        "literature_total": len(lit_files),
        # Full sorted list — NOT truncated. /api/vault/literature paginates on
        # top of this and reports `filtered_total` from it; a [:10] cap here
        # silently capped the feed too, made `has_more` permanently False, and
        # made this endpoint's total disagree with `literature_total` above.
        "literature_list": sorted(lit_files, key=lambda x: x["mtime"], reverse=True),
        "reproduction_total": len(repro_projects),
        "reproduction_list": repro_projects,
        "troubleshoot_count": trouble_count,
        "weekly_count": weekly_count,
    }
    _VAULT_STATS_CACHE = result
    _VAULT_STATS_TS = time.monotonic()
    return result

# ---------------------------------------------------------------------------
# YAML frontmatter helpers
# ---------------------------------------------------------------------------

def _yaml_quoted(value) -> str:
    """Escape a value for use INSIDE a YAML double-quoted scalar.

    The templates render `title: "{{title}}"`, so the value ends up inside
    double quotes. A title such as `A study of "band-to-band tunneling" in
    CFET` therefore produced `title: "A study of "band-to-band tunneling" in
    CFET"` — invalid YAML. Obsidian then renders the properties block as
    broken text and the note loses its tags / date / status. An embedded
    newline could even inject extra frontmatter keys.

    Escapes used are the ones YAML's double-quoted style defines:
    \\\\ \\" \\r \\n \\t
    """
    s = "" if value is None else str(value)
    s = s.replace("\\", "\\\\")
    s = s.replace('"', '\\"')
    s = s.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return s


def _yaml_bare(value) -> str:
    """Escape a value used as a BARE YAML scalar (no surrounding quotes)."""
    s = "" if value is None else str(value)
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = s.replace("#", "")            # a bare '#' starts a comment
    s = s.replace("[", "").replace("]", "")
    s = s.replace("{", "").replace("}", "")
    s = s.replace(",", "")            # a bare ',' would start a new list item
    s = s.replace(":", " ")           # a bare ':' is a mapping separator
    return s.strip()


def _one_line(value) -> str:
    """Collapse a user-supplied string to a single line.

    A newline inside a frontmatter value breaks out of the YAML scalar
    before any escaping can help: the template renders `title: "{{title}}"`,
    so a title containing a newline produced

        title: "Evil "quoted"
        status: 已精读
        injected: yes"

    — i.e. the attacker injected real frontmatter keys. Collapsing newlines
    at the data level (before rendering) removes the vector entirely.
    """
    s = "" if value is None else str(value)
    return s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


# A YAML inline list item that needs no quoting.
_YAML_SAFE_ITEM = re.compile(r"^[A-Za-z0-9_一-鿿][A-Za-z0-9_.一-鿿 \-]*$")
_FM_QUOTED_LINE = re.compile(r'^(\s*)([A-Za-z_][\w\-]*)\s*:\s*"(.*)"\s*$')
_FM_LIST_LINE = re.compile(r'^(\s*)([A-Za-z_][\w\-]*)\s*:\s*\[(.*)\]\s*$')


def _safe_frontmatter(text: str) -> str:
    """Re-escape the YAML frontmatter of a rendered note.

    The templates render `title: "{{title}}"`, so a value containing a
    double quote (e.g. `A study of "band-to-band tunneling" in CFET`)
    produced `title: "A study of "band-to-band tunneling" in CFET"` —
    invalid YAML. Obsidian then rendered the properties block as broken
    text and the note lost its tags / date / status. An embedded newline
    could even inject extra frontmatter keys.

    This walks only the frontmatter block and re-escapes the values that
    are already written as double-quoted scalars (plus inline lists),
    leaving every other line — comments, bare numbers, nested list items
    — exactly as the template produced it. Doing it here (rather than
    editing each template) fixes the literature, bilingual, QA and
    reproduction templates in one place, and keeps the body text free of
    escape characters.
    """
    if not text.startswith("---"):
        return text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return text

    out = list(lines)
    for i in range(1, end):
        line = out[i]
        m = _FM_QUOTED_LINE.match(line)
        if m:
            indent, key, value = m.groups()
            out[i] = f'{indent}{key}: "{_yaml_quoted(value)}"'
            continue
        m = _FM_LIST_LINE.match(line)
        if m:
            indent, key, items = m.groups()
            parts = [p.strip() for p in items.split(",") if p.strip()]
            quoted = []
            for p in parts:
                if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
                    quoted.append('"' + _yaml_quoted(p[1:-1]) + '"')
                elif _YAML_SAFE_ITEM.match(p):
                    quoted.append(p)
                else:
                    quoted.append('"' + _yaml_quoted(p) + '"')
            out[i] = f"{indent}{key}: [{', '.join(quoted)}]"
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Literature card
# ---------------------------------------------------------------------------

def save_literature_card(data: dict) -> dict:
    """Generate and write a structured literature note from template."""
    # `or ""` instead of `get(key, "")`: the Pydantic models declare these
    # fields Optional, so a client may legitimately send `"venue": null`.
    # `dict.get(key, default)` returns None (the key EXISTS) and the
    # following .strip() raised AttributeError -> HTTP 500. With `year`
    # it was worse: it silently produced the file name "None_Title.md".
    title = _one_line((data.get("title") or "Untitled").strip())
    authors = _one_line((data.get("authors") or "").strip())
    year = str(data.get("year") or datetime.datetime.now().year)
    venue = _one_line((data.get("venue") or "").strip())
    doi = _one_line((data.get("doi") or "").strip())
    arxiv = _one_line((data.get("arxiv") or "").strip())
    topic_category = data.get("topic_category") or "01_Device_TCAD_器件仿真"
    tags = _one_line(data.get("tags") or "TCAD, NanoDevice")
    abstract = _one_line((data.get("abstract") or "").strip())
    status = _one_line(data.get("status") or "待精读")

    # Category whitelist
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        raise ValueError(
            f"非法 topic_category: '{topic_category}'. "
            f"允许值: {ALLOWED_TOPIC_CATEGORIES}"
        )

    tpl_path = _resolve_template("01_Literature_Note_Template.md")
    today = datetime.date.today().isoformat()
    safe_title = sanitize_filename(title)
    pdf_name = f"{year}_{safe_title[:30]}.pdf"

    # Parse all tags (comma-separated) and format as YAML list for the frontmatter.
    # The template uses {{tags_csv}} which is comma-separated (YAML inline list syntax).
    # Each tag is emitted as a quoted YAML scalar so a tag containing a comma,
    # a bracket or a colon cannot break the properties block.
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    if not tag_list:
        tag_list = ["Research"]
    tags_csv = ", ".join(f'"{_yaml_quoted(t)}"' for t in tag_list)
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
        # Reject any traversal hints BEFORE sanitization. sanitize_filename()
        # only removes Windows-illegal characters and slashes; it doesn't
        # detect "..", so "../../etc/passwd" becomes "..etcpasswd.md" —
        # which is technically inside BASE_DIR (so not a real escape), but
        # is clearly user error / attack intent and should be rejected.
        if ".." in filename_override:
            raise ValueError(
                f"非法 filename_override: '{filename_override}' 含 '..', 请输入单层文件名"
            )
        safe_base = sanitize_filename(filename_override)
        if not safe_base:
            raise ValueError(f"filename 清理后为空: '{filename_override}'")
        filename = f"{safe_base}.md"
    else:
        filename = f"{year}_{safe_title}.md"
    file_path = _safe_path(target_dir, filename)

    # Make sure the destination folder exists. It normally does (the vault
    # ships with the standard sub-folders), but if the user removed or
    # renamed one, the write used to fail with a raw
    # "[Errno 2] No such file or directory: 'C:\\Users\\...\\vault\\...'" —
    # a 500 that also leaked the user's absolute filesystem path to the
    # browser.
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(rendered, encoding="utf-8")
    _invalidate_vault_stats_cache()
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

    target_paper = _one_line(data.get("target_paper") or "待关联论文").strip()
    project_type = (data.get("project_type") or "TCAD").upper()
    toolchain = _one_line(data.get("toolchain") or "Sentaurus TCAD 2023").strip()
    description = _one_line(data.get("description") or "").strip()

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
        tpl_path = _resolve_template("02_TCAD_Reproduction_Template.md")
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
        tpl_path = _resolve_template("03_EDA_Circuit_Reproduction_Template.md")
        rendered = _render_template(
            tpl_path,
            project_name=project_name,
            target_paper_link=target_paper,
            type=description or "EDA Placement/Routing/STA",
            toolchain=toolchain,
            repo_url=_one_line(data.get("repo_url") or "https://github.com/"),
            commit_hash="main",
            date=today,
        )

    readme_path = _safe_path(proj_dir, "README.md")
    if readme_path.exists():
        # Re-running the wizard with the same project name used to
        # overwrite the existing README with a freshly rendered template,
        # silently destroying whatever the user had written there (notes,
        # checklists, parameters). The subfolders survived, so the project
        # still looked intact while its README had been replaced.
        raise ValueError(
            f"复现工程 '{project_name}' 已存在, 请换一个名字, "
            f"或直接在 {readme_path} 上继续编辑。"
        )
    readme_path.write_text(rendered, encoding="utf-8")
    _invalidate_vault_stats_cache()

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
````text
{raw_error}
````
- **根本原因**：
{root_cause}
- **解决方案**：
````bash
{solution}
````
"""
    if trouble_file.exists():
        # Append under a lock instead of read-modify-write.
        #
        # The old code did read_text() -> concat -> write_text(). With two
        # browser tabs open, both threads read the same "before" state and
        # the second write clobbered the first entry — a silent loss of a
        # hard-won debugging note. Appending is atomic enough for our
        # purposes (single process, entries well under the pipe buffer),
        # and the lock serialises concurrent requests in this process.
        with _VAULT_WRITE_LOCK:
            with open(trouble_file, "a", encoding="utf-8") as f:
                f.write(entry)
    else:
        with _VAULT_WRITE_LOCK:
            with open(trouble_file, "a", encoding="utf-8") as f:
                f.write(f"# 踩坑与排错日记\n{entry}")
    _invalidate_vault_stats_cache()
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

    tpl_path = _resolve_template("04_Weekly_Report_Template.md")
    stats = get_vault_stats()

    # Filter to the last 7 days. The old code took the 3 most recently
    # *modified* notes with no date filter, so a report labelled "本周"
    # happily listed notes from three weeks earlier — and then claimed a
    # hardcoded "本周推进了核心器件仿真网格收敛与最新文献调研" while the
    # real weekly activity was zero.
    cutoff = (today - datetime.timedelta(days=7)).isoformat()
    week_lits = [
        l for l in stats.get("literature_list", [])
        if str(l.get("mtime", ""))[:10] >= cutoff
    ]
    recent_lits = week_lits[:3]
    week_repro = [
        p for p in stats.get("reproduction_list", [])
        if str(p.get("mtime", "")) >= cutoff
    ]

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

    # The one-line summary used to be a hardcoded sentence that was always
    # printed, even when the week had no activity at all. Derive it from
    # what actually happened in the window.
    if week_lits or week_repro:
        summary = (
            f"本周共更新 {len(week_lits)} 篇文献笔记、"
            f"{len(week_repro)} 个复现工程目录。"
        )
    else:
        summary = "本周暂无新的文献录入或复现工程更新, 待补充进展。"

    # Link the most recently touched reproduction project instead of a
    # hard-coded one. The old value pointed at "repro_cfet_sub3nm", so the
    # moment the user deleted or renamed that project, every weekly report
    # shipped a dead Obsidian link. Falls back to a neutral placeholder
    # when the vault has no reproduction project yet.
    repro_link = "02_Reproduction/|复现工程 (待创建)"
    repro_list = stats.get("reproduction_list") or []
    if repro_list:
        latest = max(repro_list, key=lambda r: (r.get("mtime") or "", r.get("name") or ""))
        repro_link = f"{latest.get('path', '')}/README|{latest.get('name', '复现工程')}"

    rendered = _render_template(
        tpl_path,
        week_id=week_id,
        start_date=start_date,
        end_date=end_date,
        one_line_summary=summary,
        paper_1_link=lit_lines,
        reproduction_link=repro_link,
    )

    weekly_dir = _safe_path(BASE_DIR, "04_Weekly_Reports")
    weekly_dir.mkdir(parents=True, exist_ok=True)
    filename = f"Weekly_{today.strftime('%Y%m%d')}_{week_id}.md"
    file_path = _safe_path(weekly_dir, filename)
    # Write via a temp file + atomic replace so a concurrent request (or a
    # crash) can never leave a half-written report in the vault.
    tmp_path = file_path.with_suffix(".md.tmp")
    with _VAULT_WRITE_LOCK:
        tmp_path.write_text(rendered, encoding="utf-8")
        os.replace(tmp_path, file_path)
    _invalidate_vault_stats_cache()

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
    title = _one_line(data.get("title") or "").strip() or "Untitled_Paper"
    authors = _one_line(data.get("authors") or "").strip() or "Unknown"
    year = int(data.get("year") or 2024)
    topic_category = data.get("topic_category") or "01_Device_TCAD_器件仿真"
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
        f"title: \"{_yaml_quoted(title)}\"",
        f"authors: \"{_yaml_quoted(authors)}\"",
        f"year: {year}",
        f"date: {today}",
        f"category: \"{_yaml_quoted(topic_category)}\"",
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
        trans_html = (sec.get("translation_html") or "").strip()
        trans = (sec.get("translation") or "").strip()
        orig_html = (sec.get("orig_html") or "").strip()
        orig = (sec.get("text") or "").strip()
        lines.append(f"### {sec_title}\n")
        # Translation: prefer pre-rendered HTML (already KaTeX-processed) over
        # raw markdown. The HTML embeds inline <span class="katex"> + <svg>
        # blocks for every formula, so Obsidian / Typora / GitHub preview all
        # show fully-typeset math without any community plugin.
        if trans_html:
            lines.append("> [!NOTE] 中文精译 (已 KaTeX 排版)\n")
            lines.append('<div class="bilingual-trans-cn">\n')
            lines.append(trans_html)
            lines.append("\n</div>\n")
        elif trans:
            # Fallback when client didn't pre-render (older client / partial load)
            trans_block = "\n".join(f"> {line}" if line.strip() else ">" for line in trans.splitlines())
            lines.append(f"> [!NOTE] 中文精译 (LaTeX 源码 — 需 Obsidian 公式插件)\n{trans_block}\n")
        # English original: same dual-mode treatment. Useful when the source
        # PDF contains LaTeX (common in microelectronics / physics papers).
        if orig_html:
            page_hint = f"(第 {sec.get('page_start', 1)}-{sec.get('page_end', 1)} 页 · KaTeX 已排版)"
            lines.append(f"<details>\n<summary><b>展开对应英文原文 {page_hint}</b></summary>\n\n<div class=\"bilingual-trans-en\">\n\n{orig_html}\n\n</div>\n\n</details>\n")
        elif orig:
            page_hint = f"(第 {sec.get('page_start', 1)}-{sec.get('page_end', 1)} 页)"
            lines.append(f"<details>\n<summary><b>展开对应英文原文 {page_hint}</b></summary>\n\n{orig}\n\n</details>\n")
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
    # The destination folder may have been removed by the user; create it
    # instead of failing with a raw "[Errno 2]" 500 that also leaks the
    # absolute filesystem path to the browser.
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    _invalidate_vault_stats_cache()

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
    title = _one_line(data.get("title") or "Untitled_Paper").strip()
    question = _one_line(data.get("question") or "未命名疑问").strip()
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
title: "答疑: {_yaml_quoted(question[:60])}"
paper: "{_yaml_quoted(title)}"
category: "{_yaml_quoted(topic_category)}"
date: {today_str}
mode: "{_yaml_quoted(mode)}"
tags: [Literature, QnA, AI划词答疑, {_yaml_quoted(mode)}]
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
    # The destination folder may have been removed by the user; create it
    # instead of failing with a raw "[Errno 2]" 500 that also leaks the
    # absolute filesystem path to the browser.
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    _invalidate_vault_stats_cache()

    return {
        "status": "success",
        "file_name": filename,
        "folder": str(file_path.parent.relative_to(BASE_DIR)).replace("\\", "/"),
        "relative_path": str(file_path.relative_to(BASE_DIR)).replace("\\", "/"),
    }