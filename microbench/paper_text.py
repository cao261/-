"""
Paper full-text fetcher + section splitter.

Goal: given an arXiv ID or DOI (or URL), download the PDF, extract
plain text, and split into recognizable sections (Abstract, Introduction,
Methods, Results, Discussion, Conclusion, References).

Why this matters: a硕博生 needs to read English papers efficiently.
Split-screen reading (original left, Chinese translation right) only
works if we have the full text, not just the abstract.

Strategy:
  arXiv:       direct PDF download (always available, ~5MB typical)
  DOI:         try Semantic Scholar's openAccessPdf field;
               if missing, fall back to the publisher landing page URL
               so the user can manually download
  URL:         direct download if Content-Type is application/pdf

Text extraction: pdfplumber (preserves reading order, handles tables
better than pypdf). Falls back to raw decode if pdfplumber fails.

Two extraction quirks that matter a lot for LLM translation (found on
real JACerS/IEEE-style PDFs, e.g. "J.Am.Ceram.Soc.,91[2]351-358(2008)"):

1. Missing spaces. Many publisher PDFs use subsetted fonts that no longer
   encode the space glyph. pdfplumber's default x_tolerance=3 then treats
   the small inter-word gaps as intra-word spacing and emits glued text
   like "compositestructureshavebeeninvestigated". A reasoning model
   (MiniMax-M3.1-Flash) tries to re-parse such text, falls into an
   unbounded thinking loop, burns the whole max_tokens budget on
   reasoning and returns an EMPTY body. Lowering x_tolerance to 1.5
   restores correct word splitting.
2. Two-column layout. pdfplumber's plain extract_text() interleaves the
   left and right column line by line. We detect the column gutter and
   extract left column then right column, keeping the full-width header
   (title/abstract) intact.

Section splitting: regex over numbered/Roman-numeral headings plus
common keywords. Returns list of {title, text, page_start, page_end}.
"""

import io
import re
import sys
from typing import Optional
from pathlib import Path

# Module-level import. The SSRF-hardening patch introduced module-level
# helpers that call requests.get(), but `requests` was only ever imported
# as a function-local name inside fetch_full_text() — so the helpers raised
# `NameError: name 'requests' is not defined` and every DOI → openAccessPdf
# full-text fetch died with a 502.
import requests

from .paper_fetcher import (
    _normalize_arxiv_id,
    _normalize_doi,
    HEADERS,
    PDF_MAX_SIZE_MB,
    # The SSRF guard lives in paper_fetcher (it is needed there too, for the
    # doi.org redirect chain and the IEEE landing-page scrape). Re-exported
    # here so the existing `from .paper_text import _assert_public_http_url`
    # call sites keep working.
    _assert_public_http_url,
)


# ---------------------------------------------------------------------------
# SSRF guard for the "user pasted a direct PDF URL" path
# ---------------------------------------------------------------------------
# Implementation lives in paper_fetcher._assert_public_http_url — see the
# comment there for why it must not be duplicated in this module.
#
# The implementation now lives in paper_fetcher._assert_public_http_url and
# is imported above — it is needed there as well (doi.org redirect chain,
# IEEE landing-page scrape), and duplicating it would let the two copies
# drift apart.


def _download_pdf_bytes(url: str, max_bytes: int, allow_redirects: bool = False) -> bytes:
    """GET `url` and return the body, refusing to buffer more than `max_bytes`.

    The previous code used `requests.get(..., stream=True)` followed by
    `.content`, which reads the *entire* body into memory before the size
    check happens at the call site. A hostile or misconfigured URL could
    therefore make the server allocate an arbitrary amount of RAM.

    Streaming with a running counter keeps the peak allocation bounded by
    `max_bytes`, and the response is always closed (the old code never
    closed the connection, leaving the socket to be reclaimed by the GC).
    """
    resp = requests.get(
        url, headers=HEADERS, timeout=60, stream=True, allow_redirects=allow_redirects
    )
    try:
        resp.raise_for_status()
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    f"PDF 超过 {max_bytes // (1024 * 1024)}MB 上限, 已中止下载"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        resp.close()


# Common section keywords (case-insensitive). Order matters for matching priority.
# Each canonical maps to a list of substrings; a title matches if it equals one
# of these OR starts with one followed by space/colon/dot (handles "Methods:" / "Methods.")
# or simply starts with the substring (handles "Methods" / "Methodology" etc.).
SECTION_KEYWORDS = [
    ("abstract", ["abstract", "summary"]),
    ("introduction", ["introduction", "background", "overview"]),
    ("methods", ["methods", "method", "methodology", "approach", "experimental setup", "experimental section", "model", "framework", "experimental methods"]),
    ("results", ["results", "result", "experimental result", "evaluation", "experiments", "performance"]),
    ("discussion", ["discussion", "discussions"]),
    ("conclusion", ["conclusion", "conclusions", "summary and outlook", "concluding remarks"]),
    ("references", ["references", "reference", "bibliography"]),
    ("acknowledgments", ["acknowledgments", "acknowledgement", "acknowledgements"]),
]


# Regex for section headings. Captures:
#   Group 1: numbering (e.g. "1", "1.", "I", "I.", "I.2")
#   Group 2: title (e.g. "Introduction")
#   Two flavors: numbered (1, 2, 3...) and Roman (I, II, III...)
HEADING_NUMERIC_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\.?\s+([A-Z][A-Za-z][A-Za-z\s\-]{1,80})\s*$",
    re.MULTILINE,
)
HEADING_ROMAN_RE = re.compile(
    r"^\s*([IVX]+)\.?\s+([A-Z][A-Z]+(?:\s+[A-Z]+){0,8})\s*$",
    re.MULTILINE,
)


def _classify_title_to_section(title: str) -> Optional[str]:
    """
    Given a section title (e.g. "Introduction", "II. METHODOLOGY"),
    return our canonical section key (e.g. "introduction", "methods")
    or None if it doesn't match any known category.
    """
    t = title.lower().strip().rstrip(".").strip()
    # Strip leading numbering — only if followed by a period, to avoid eating
    # leading letters of words like "introduction" (which starts with 'i',
    # a Roman numeral letter).
    t = re.sub(r"^(?:\d+\.|[ivx]+\.)\s*", "", t).strip()

    # Build a flat set of all keywords for fast lookup
    all_keywords = {}  # keyword -> canonical
    for canonical, keywords in SECTION_KEYWORDS:
        for kw in keywords:
            all_keywords[kw] = canonical

    # 1) Direct equality
    if t in all_keywords:
        return all_keywords[t]

    # 2) Word-boundary substring match (handles "Experimental Results" → "results",
    #    "Methodology Overview" → "methods", "Background and Overview" → "introduction")
    #    For each keyword, check if it appears in t with word boundaries on both sides
    #    OR matches the full t.
    for kw, canonical in all_keywords.items():
        # Find all occurrences of kw in t, check word boundaries
        idx = 0
        while True:
            pos = t.find(kw, idx)
            if pos < 0:
                break
            # Word boundary before
            left_ok = (pos == 0) or (t[pos - 1] in " \t-:.,;")
            # Word boundary after
            end = pos + len(kw)
            right_ok = (end == len(t)) or (t[end] in " \t-:.,;")
            if left_ok and right_ok:
                return canonical
            idx = pos + 1

    # 3) Prefix match (handles "Methods and Materials" → "methods")
    for kw, canonical in all_keywords.items():
        if t.startswith(kw + " ") or t.startswith(kw + ":") or t.startswith(kw + "."):
            return canonical

    return None


def extract_text_from_pdf(pdf_bytes: bytes) -> list[dict]:
    """
    Extract per-page text from a PDF using pdfplumber.
    Returns list of {page: int, text: str}.
    """
    pages = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = _extract_page_text(page)
                pages.append({"page": i, "text": text})
    except ImportError:
        # pdfplumber not installed — fall back to pypdf
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for i, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                pages.append({"page": i, "text": text})
        except ImportError:
            raise RuntimeError(
                "PDF 文本提取需要 pdfplumber 或 pypdf。请运行 "
                "`pip install pdfplumber` 安装。"
            )
    except Exception as e:
        raise RuntimeError(f"PDF 文本提取失败: {e}")
    return pages


def extract_page_text_from_path(pdf_path, page_no: int) -> str:
    """
    Extract the text of a single page (1-based) from a PDF on disk.

    Used by the PDF dual-column reader's per-page translation endpoint:
    the server re-extracts the page with the correct word spacing and
    column order instead of trusting the text the browser scraped via
    PDF.js (which loses spaces on fonts without space glyphs).

    The tail is trimmed: a page normally ends with a hyphenated word that
    continues on the next page ("...in a highly mag-") plus a lone page
    number. Feeding that to the model makes the translation end on a
    half-word ("在高度磁——"), which reads as "已翻译但内容是乱的".
    """
    import pdfplumber
    path = Path(pdf_path)
    with pdfplumber.open(str(path)) as pdf:
        if page_no < 1 or page_no > len(pdf.pages):
            return ""
        return _trim_page_tail(_extract_page_text(pdf.pages[page_no - 1]))


_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?。！？]")
# 末尾残片最多砍掉这么多字符，防止整页没有句号时把大半页都砍掉。
_MAX_TAIL_TRIM_CHARS = 1500


def _trim_page_tail(text: str) -> str:
    """去掉页尾的页码行和没写完的末句。

    只用于「按页翻译」这条路径（单页独立成文）。整篇章节翻译不能这么切：
    章节里各页是拼起来的，页尾的 "mag-" 正是下一页 "netic" 的前半截，
    切掉反而会拼出一个新词。

    为什么必须切：一页的最后一句往往跨页续写，页尾还常带一个单独的页码
    行。直接丢给模型翻译，译文就会以半截词收尾（"在高度磁——"），读起来
    像翻译坏了。按「最后一个句末标点」一刀切掉尾巴，可以同时解决三件事：
    跨页断词、页码行、没写完的末句。
    """
    if not text:
        return text
    lines = text.split("\n")

    # 1) 末尾孤立的页码行（单独一个数字）
    while lines and _PAGE_NUMBER_RE.match(lines[-1]):
        lines.pop()
    text = "\n".join(lines).rstrip()

    # 2) 砍掉最后一个句末标点之后的残片
    ends = list(_SENTENCE_END_RE.finditer(text))
    if ends:
        cut = ends[-1].end()
        if cut < len(text) and (len(text) - cut) <= _MAX_TAIL_TRIM_CHARS:
            text = text[:cut]

    return text.strip()


# ---------------------------------------------------------------------------
# Page text extraction internals
# ---------------------------------------------------------------------------

# PDF 字体子集化后空格字形常常缺失，pdfplumber 默认 x_tolerance=3 会把相邻
# 单词粘在一起（compositestructureshavebeeninvestigated）。1.5 能按真实字
# 间距正确切词，实测可完全恢复空格。
PDF_X_TOLERANCE = 1.5

_X_BINS = 120       # x 方向直方图分箱数
_Y_BANDS = 60       # y 方向分带数（用于定位双栏起始位置）

# pdfplumber 无法解码的字符会输出成 "(cid:12)" 这种占位符。
_CID_RE = re.compile(r"\(cid:\d+\)")
# 连续多个 cid 通常是一整段公式/符号（一次能抽出十几个），
# 逐个替换会让译文里出现 "[?][?][?][?][?][?][?][?]" 这种刷屏。
_CID_RUN_RE = re.compile(r"(?:\(cid:\d+\)[ \t]*){2,}")


def _clean_cid_markers(text: str) -> str:
    """
    Replace pdfplumber's undecodable-character markers with "[?]".

    Publisher PDFs often use subsetted fonts without a ToUnicode table, so
    pdfplumber can only emit "(cid:2)" for a symbol it cannot map. Handing
    those to the model invites it to *invent* a symbol — in testing it turned
    "(cid:2)" into "\\approx" and "(cid:3)" into "\\times", producing
    formulas that were never in the paper. With "[?]" plus a prompt note,
    the model skips the symbol instead of fabricating one.

    A whole formula that failed to decode comes out as a long run of cid
    markers; those are collapsed into a single "[?]" so the translation
    doesn't end up with a wall of placeholders.
    """
    if not text:
        return text
    text = _CID_RUN_RE.sub("[?]", text)
    return _CID_RE.sub("[?]", text)


def _x_cover(words, page_width: float, nb: int) -> list:
    """每个 x 分箱被文字覆盖的宽度。"""
    cover = [0.0] * nb
    for w in words:
        a = max(0, min(nb - 1, int(w["x0"] / page_width * nb)))
        b = max(0, min(nb - 1, int(w["x1"] / page_width * nb)))
        for i in range(a, b + 1):
            cover[i] += (w["x1"] - w["x0"])
    return cover


def _longest_gap(cover, lo: int, hi: int):
    """返回 [lo, hi) 内最长零覆盖段的 (起始 bin, 长度)。"""
    best_a, best_len, run_a = None, 0, None
    for i in range(lo, hi):
        if cover[i] <= 0.5:
            if run_a is None:
                run_a = i
        else:
            if run_a is not None and i - run_a > best_len:
                best_a, best_len = run_a, i - run_a
            run_a = None
    if run_a is not None and hi - run_a > best_len:
        best_a, best_len = run_a, hi - run_a
    return best_a, best_len


def _extract_page_text(page) -> str:
    """
    抽取单页文本，按阅读顺序排列：整宽区域(标题/摘要) -> 左栏 -> 右栏。
    任何一步检测不可靠都退回 pdfplumber 的普通抽取（只是空格仍然修好）。
    """
    return _mark_broken_formulas(_clean_cid_markers(_extract_page_text_raw(page)))


# 公式碎片的标记。送给模型前先把碎公式整段替换成它，模型照抄即可。
FORMULA_MARKER = "⟦FORMULA⟧"

# 关系符号：= ≈ ≤ ≥ 以及 PDF 里常见的乱码等价物 ¼
_RELATION_RE = re.compile(r"[=≈≤≥¼]")
# 数学残片常见符号：括号(乱码) / 分数 / 上下标数字
_FRAGMENT_CHARS = "ðÞ[]()"


def _is_formulaish_line(line: str) -> bool:
    """判断一行是不是「公式或公式碎片」而不是正文句子。"""
    s = line.strip()
    if not s:
        return False
    if _RELATION_RE.search(s):
        # 有关系符号 → 是公式（长度上限放宽，公式行可以很长）
        return len(s) <= 160
    # 有正常英文单词（>=4 字母）→ 一定是正文
    if re.search(r"[A-Za-z]{4,}", s):
        return False
    if len(s) > 60:
        return False
    has_digit = any(ch.isdigit() for ch in s)
    has_special = any(ch in _FRAGMENT_CHARS for ch in s)
    short_words_only = bool(re.fullmatch(r"[A-Za-z]{1,3}(\s+[A-Za-z]{1,3})*", s))
    return has_digit or has_special or short_words_only


def _mark_broken_formulas(text: str) -> str:
    """把「PDF 排版导致无法完整提取的公式」整段标成 ⟦FORMULA⟧。

    为什么必须标：这类区域的文本是乱序碎片（分子、分母、上下标各占一行），
    送给模型后它会"顺手补全"出一个看起来很像样的公式 —— 实测把
        α_E,31 = (n d₃₁ᵐ g₃₁) / (n s₁₁ᴱ (1-(k₃₁ᴱ)²) - (s₃₃ᵐ)(1-n))
    编成了
        λ = a_ME/(d₃₁ E₃₁) ≈ nsp(1-n)(k_p+3/2 k_m)(s_m+1/n s_p)
    原文里根本没有 λ。**错公式比没公式危险得多** —— 用户会照着它去算。

    所以这里把碎公式整段换成标记，提示词要求模型原样输出这个标记，
    用户看到「公式未能完整提取，请对照 PDF 原文」，而不是一个假公式。

    注意：公式在抽取结果里是**好几行**（分子一行、分母一行、上下标各一行），
    "=" 和 "[?]" 往往不在同一行 —— 所以按「连续的公式碎片行」成段判定：
    段里同时出现 [?] 和关系符号，才整段替换。
    """
    if not text or "[?]" not in text:
        return text
    lines = text.split("\n")

    # 1) 找连续的公式碎片行
    runs: list[list[int]] = []
    cur: list[int] = []
    for idx, line in enumerate(lines):
        if _is_formulaish_line(line):
            cur.append(idx)
        else:
            if cur:
                runs.append(cur)
                cur = []
    if cur:
        runs.append(cur)

    # 2) 段里同时有 [?] 和关系符号 → 整段是坏掉的公式
    drop: set[int] = set()
    for run in runs:
        if len(run) > 10:
            continue
        seg = "\n".join(lines[i] for i in run)
        if "[?]" in seg and _RELATION_RE.search(seg):
            drop.update(run)

    if not drop:
        return text

    out: list[str] = []
    in_formula = False
    for idx, line in enumerate(lines):
        if idx in drop:
            if not in_formula:
                out.append(FORMULA_MARKER)
                in_formula = True
            continue
        in_formula = False
        out.append(line)
    return "\n".join(out)


def _find_vertical_split(words, width, height):
    """找到双栏正文开始的 y（整宽标题区结束的位置）；找不到返回 0。

    抽成独立函数，生产代码和测试共用同一套判定，免得两边算出不同的
    y_split，测试就成了自说自话。
    """
    for band in range(_Y_BANDS):
        y0 = height * band / _Y_BANDS
        y1 = height * (band + 1) / _Y_BANDS
        band_words = [w for w in words if y0 <= w["top"] < y1]
        if len(band_words) < 6:
            continue
        cover = _x_cover(band_words, width, _X_BINS)
        a, ln = _longest_gap(cover, int(_X_BINS * 0.15), int(_X_BINS * 0.85))
        if a is None or ln < 2:
            continue
        left_n = sum(1 for w in band_words if w["x1"] <= width * a / _X_BINS)
        right_n = sum(1 for w in band_words if w["x0"] >= width * (a + ln) / _X_BINS)
        if left_n >= 4 and right_n >= 4:
            return y0
    return 0.0


def _extract_page_text_raw(page) -> str:
    plain = ""
    try:
        plain = page.extract_text(x_tolerance=PDF_X_TOLERANCE) or ""
    except Exception:
        plain = ""

    try:
        width, height = float(page.width), float(page.height)
        words = page.extract_words(
            x_tolerance=PDF_X_TOLERANCE, use_text_flow=False, keep_blank_chars=False
        )
    except Exception:
        return plain

    if len(words) < 40:
        return plain

    # 1) 垂直方向：找到双栏开始的 y（整宽标题区结束的位置）
    y_split = _find_vertical_split(words, width, height)

    below = [w for w in words if w["top"] >= y_split]
    if len(below) < 30:
        return plain

    # 2) 水平方向：在双栏区域找栏间空白
    cover = _x_cover(below, width, _X_BINS)
    a, ln = _longest_gap(cover, int(_X_BINS * 0.15), int(_X_BINS * 0.85))
    x_gap0 = x_gap1 = None
    if a is not None and ln >= 2:
        g0 = width * a / _X_BINS
        g1 = width * (a + ln) / _X_BINS
        if g1 - g0 >= width * 0.01:
            left_n = sum(1 for w in below if w["x1"] <= g0 + 1)
            right_n = sum(1 for w in below if w["x0"] >= g1 - 1)
            if left_n >= 8 and right_n >= 8:
                x_gap0, x_gap1 = g0, g1

    if x_gap0 is None:
        # 2b) 整宽元素（跨栏的表格/图注）会把 x 覆盖直方图整段填满，
        #     全局最长零覆盖段就找不到栏缝了 —— 于是退回 plain，
        #     两栏正文互相交错，公式被拆散（实测 JACERS 综述第 2 页）。
        #     这里改成「逐带投票」：每个水平带单独找栏缝，对中点投票，
        #     取众数当栏缝。
        x_mid = _vote_column_gap(words, width, height, y_split)
        if x_mid is None:
            return plain
    else:
        x_mid = (x_gap0 + x_gap1) / 2.0

    # 3) 按带分组切页 → 左栏正文 / 右栏正文 / 整宽块
    top_txt = ""
    if y_split > 0:
        try:
            top_txt = page.crop((0, 0, width, y_split)).extract_text(
                x_tolerance=PDF_X_TOLERANCE
            ) or ""
        except Exception:
            top_txt = ""
    left_parts, right_parts, span_units = _extract_by_bands(
        page, words, width, height, y_split, x_mid)
    if not left_parts and not right_parts and not span_units:
        return plain
    return _join_reading_order(left_parts, right_parts, span_units, top_txt)


def _join_reading_order(left_parts, right_parts, span_units, top_txt="") -> str:
    """页内阅读顺序：左栏正文 → 右栏正文 → 跨栏块（页眉/图注/整宽表格）。

    用户诉求："一页中就是从左到右翻译，一直都是从左到右"。

    之前的两种做法都会碍事：
    - 整栏裁剪：跨栏表格被栏缝劈成两半，一半塞进左栏、一半塞进右栏，
      表格碎片夹在正文中间，译文读起来完全对不上；
    - 逐带交替拼接：左1→右1→左2→右2，译文左右横跳。

    现在：两栏正文各自成段（先左后右），跨栏的表格/图注**整块保留**
    并排在正文之后 —— 放最前面会让读者一进页面就撞上一页眉+表格，
    正文得先滚过去才看得到（用户原话："老是先翻译最上面的那一栏，
    给我的阅读造成阻碍"）。
    """
    parts = []
    if top_txt and top_txt.strip():
        parts.append(top_txt.strip())
    parts.extend(p for p in left_parts if p.strip())
    parts.extend(p for p in right_parts if p.strip())
    for unit in span_units or []:
        parts.extend(p for p in unit if p.strip())
    return "\n\n".join(parts)


def _vote_column_gap(words, width, height, y_split):
    """逐带投票找栏缝，返回栏缝中点（页面坐标）；找不到返回 None。"""
    band_h = max(8.0, height / 40.0)
    bands = []
    y = y_split
    while y < height:
        bw = [w for w in words if y <= w["top"] < y + band_h]
        bands.append((y, y + band_h, bw))
        y += band_h

    # 投票：只有"能找出栏缝"的带才参与
    votes: dict[int, int] = {}
    for _, _, bw in bands:
        mid = _band_column_gap(bw, width)
        if mid is None:
            continue
        key = int(round(mid / width * _X_BINS))
        votes[key] = votes.get(key, 0) + 1
    if not votes:
        return None
    x_bin = max(votes.items(), key=lambda kv: kv[1])[0]
    if votes[x_bin] < 3:
        return None
    x_mid = width * x_bin / _X_BINS

    left_n = sum(1 for w in words if w["x1"] <= x_mid + 1)
    right_n = sum(1 for w in words if w["x0"] >= x_mid - 1)
    if left_n < 8 or right_n < 8:
        return None
    return x_mid


def _extract_by_bands(page, words, width, height, y_split, x_mid):
    """按水平带分组切页。

    返回 (正文左栏段, 正文右栏段, 整宽单元列表)。

    整宽单元 = 跨栏的表格/图注 + 它紧邻的表体，按「标题 → 左半 → 右半」
    整块保留。跨栏表格被栏缝劈成两半、碎片夹在正文中间，读起来完全对不上
    （实测 JACERS 第 2 页：Table I 的标题被甩到最后，表体却散在两栏里）。

    分组判据是"栏缝位置有没有空隙"，不是"最长空隙在哪" —— 后者会被图注、
    缩进段落带偏，导致整页又按整宽裁回去（两栏正文重新交错）。
    """
    band_h = max(8.0, height / 40.0)
    x_bin = int(round(x_mid / width * _X_BINS))
    groups = []   # [kind, y0, y1]
    y = y_split
    while y < height:
        bw = [w for w in words if y <= w["top"] < y + band_h]
        y1 = y + band_h
        if bw:
            cover = _x_cover(bw, width, _X_BINS)
            kind = "split" if _has_gap_near(cover, x_bin) else "span"
            if groups and groups[-1][0] == kind:
                groups[-1][2] = y1
            else:
                groups.append([kind, y, y1])
        y = y1

    left_parts: list[str] = []
    right_parts: list[str] = []
    units: list[list[str]] = []
    pending: list[str] = []
    try:
        for kind, gy0, gy1 in groups:
            if kind == "span":
                st = page.crop((0, gy0, width, gy1)).extract_text(
                    x_tolerance=PDF_X_TOLERANCE) or ""
                if st.strip():
                    pending.append(st.strip())
                continue
            lt = page.crop((0, gy0, x_mid, gy1)).extract_text(
                x_tolerance=PDF_X_TOLERANCE) or ""
            rt = page.crop((x_mid, gy0, width, gy1)).extract_text(
                x_tolerance=PDF_X_TOLERANCE) or ""
            if pending:
                # 上面紧挨着跨栏块 → 这一组是表格的表体，整块保留
                unit = list(pending)
                pending = []
                if lt.strip():
                    unit.append(lt.strip())
                if rt.strip():
                    unit.append(rt.strip())
                units.append(unit)
            else:
                # 纯正文：两栏各自成段（后面再按「左栏读完再右栏」合并）
                if lt.strip():
                    left_parts.append(lt.strip())
                if rt.strip():
                    right_parts.append(rt.strip())
    except Exception:
        return [], [], []
    if pending:
        units.append(pending)
    return left_parts, right_parts, units


def _band_column_gap(band_words, width):
    """在一个水平带里找栏缝，返回栏缝中点（页面坐标）或 None。"""
    if len(band_words) < 6:
        return None
    cover = _x_cover(band_words, width, _X_BINS)
    a, ln = _longest_gap(cover, int(_X_BINS * 0.15), int(_X_BINS * 0.85))
    if a is None or ln < 2:
        return None
    mid = width * (a + ln / 2.0) / _X_BINS
    left_n = sum(1 for w in band_words if w["x1"] <= mid)
    right_n = sum(1 for w in band_words if w["x0"] >= mid)
    if left_n < 3 or right_n < 3:
        return None
    return mid


def _has_gap_near(cover, center_bin, tol_bins=3):
    """cover 在 center_bin 附近是否有空隙（这一带是不是双栏排版）。"""
    lo = max(0, center_bin - tol_bins)
    hi = min(len(cover), center_bin + tol_bins + 1)
    for i in range(lo, hi):
        if cover[i] <= 0.5:
            return True
    return False


def split_into_sections(pages: list[dict]) -> list[dict]:
    """
    Split full-text pages into canonical sections.

    Algorithm:
      1. Concatenate all pages into one text (keep page boundaries via markers)
      2. Find all heading candidates via HEADING_NUMERIC_RE + HEADING_ROMAN_RE
      3. Filter to headings whose title maps to a known SECTION_KEYWORDS
      4. Slice text between consecutive recognized headings

    Returns list of {title, text, page_start, page_end} where the first
    item may be a "preamble" (text before the first recognized heading,
    usually the Abstract on its own page).
    """
    if not pages:
        return []

    # Build full text with page markers (page_num|sentinel|text)
    full_text_parts = []
    page_boundaries = []  # (char_offset, page_num)
    # Running offset instead of len("".join(full_text_parts)) per page.
    # The old form was O(n^2): every page re-joined the whole accumulated
    # text, so a 1000-page paper spent ~740ms just computing boundaries
    # (5.4M characters copied 1000 times).
    running_len = 0
    for p in pages:
        page_boundaries.append((running_len, p["page"]))
        running_len += len(p["text"]) + 1  # +1 for the "\n" marker below
        full_text_parts.append(p["text"])
        full_text_parts.append("\n")
    full_text = "".join(full_text_parts)

    # Find candidate headings
    candidates = []
    for m in HEADING_NUMERIC_RE.finditer(full_text):
        candidates.append({
            "start": m.start(),
            "end": m.end(),
            "number": m.group(1),
            "title": m.group(2).strip(),
        })
    for m in HEADING_ROMAN_RE.finditer(full_text):
        candidates.append({
            "start": m.start(),
            "end": m.end(),
            "number": m.group(1),
            "title": m.group(2).strip(),
        })

    # Map to canonical sections + dedupe (a heading might match both regexes)
    seen = set()
    recognized = []
    for c in candidates:
        key = (c["start"], c["title"])
        if key in seen:
            continue
        seen.add(key)
        section = _classify_title_to_section(c["title"])
        if section:
            recognized.append({**c, "section": section})

    # Sort by start position; remove very-close duplicates (different formats of same heading)
    recognized.sort(key=lambda x: x["start"])

    if not recognized:
        # No recognizable sections — return the whole text as a single "preamble"
        return [{
            "section": "full_text",
            "title": "Full Text",
            "text": full_text.strip(),
            "page_start": pages[0]["page"],
            "page_end": pages[-1]["page"],
        }]

    # Build sections by slicing text between recognized headings
    sections = []
    # Pre-section: text before the first recognized heading
    if recognized[0]["start"] > 0:
        pre_text = full_text[:recognized[0]["start"]].strip()
        if pre_text and len(pre_text) > 50:
            sections.append({
                "section": "preamble",
                "title": "Preamble",
                "text": pre_text,
                "page_start": pages[0]["page"],
                "page_end": _find_page(recognized[0]["start"], page_boundaries),
            })

    for i, h in enumerate(recognized):
        start = h["end"]
        end = recognized[i + 1]["start"] if i + 1 < len(recognized) else len(full_text)
        text = full_text[start:end].strip()

        # Skip sections that are essentially empty (just whitespace)
        if len(text) < 30:
            continue

        sections.append({
            "section": h["section"],
            "title": h["title"],
            "number": h["number"],
            "text": text,
            "page_start": _find_page(start, page_boundaries),
            "page_end": _find_page(end, page_boundaries),
        })

    return sections


def _find_page(char_offset: int, page_boundaries: list[tuple[int, int]]) -> int:
    """Return the page number that contains the given char offset."""
    page_num = page_boundaries[-1][1]
    for offset, pnum in page_boundaries:
        if offset <= char_offset:
            page_num = pnum
        else:
            break
    return page_num


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def fetch_full_text(identifier: str, save_pdf_to: Optional[Path] = None) -> dict:
    """
    Fetch the full text of a paper given an arXiv ID, DOI, or URL.

    Returns dict with:
      - identifier: original input
      - source: 'arXiv' | 'OpenAccess' | 'URL'
      - title / authors / year / venue (best-effort metadata)
      - pdf_path: where the PDF was saved (if save_pdf_to was provided or
                  default Downloads dir)
      - pdf_bytes: bytes (caller can persist)
      - sections: list of {section, title, text, page_start, page_end}
      - total_pages: int
      - char_count: int

    Raises ValueError if no full-text source is available.
    """
    import requests
    from .paper_fetcher import (
        fetch_arxiv_metadata,
        fetch_crossref_doi_metadata,
    )

    pdf_bytes = None
    source = None
    meta = {"title": "", "authors": "", "year": "", "venue": ""}

    # --- Try arXiv first if it looks like one ---
    # IMPORTANT: use strict fullmatch to avoid matching year.month inside DOIs
    # like "10.1109/TIE.2020.3007097" (which contains "2020.3007").
    arxiv_id = None
    ident_stripped = identifier.strip()
    arxiv_fullmatch = re.fullmatch(r"(\d{4}\.\d{4,5})(v\d+)?", ident_stripped)
    arxiv_url_match = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", ident_stripped)
    if arxiv_fullmatch or arxiv_url_match:
        try:
            arxiv_id = arxiv_fullmatch.group(1) if arxiv_fullmatch else arxiv_url_match.group(1)
        except (AttributeError, ValueError):
            arxiv_id = None
    # Also accept arXiv IDs prefixed with "arXiv:"
    if arxiv_id is None and ident_stripped.lower().startswith("arxiv:"):
        try:
            arxiv_id = _normalize_arxiv_id(ident_stripped)
        except ValueError:
            arxiv_id = None

    if arxiv_id:
        # We already have download_arxiv_pdf; reuse it
        # Wrap in try/except so arXiv 404 falls through to DOI path
        tmp_path = None
        try:
            from .paper_fetcher import download_arxiv_pdf
            import tempfile as _tempfile
            tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.close()
            tmp_path = Path(tmp.name)
            info = download_arxiv_pdf(arxiv_id, str(tmp_path))
            with open(tmp_path, "rb") as f:
                pdf_bytes = f.read()
            meta = fetch_arxiv_metadata(arxiv_id)
            source = "arXiv"
        except Exception:
            # arXiv path failed (404, network error, etc.) — fall through to DOI
            arxiv_id = None
            pdf_bytes = None
            source = None
        finally:
            # The temp file used to be unlinked *inside* the try block, so
            # any failure (network error, 404, disk error) leaked a .pdf
            # into %TEMP% on every attempt. Repeated failed fetches slowly
            # filled the temp directory with multi-MB leftovers.
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    # --- Otherwise try DOI ---
    if pdf_bytes is None:
        doi = None
        try:
            doi = _normalize_doi(identifier)
        except ValueError:
            pass

        if doi:
            # Try Semantic Scholar's openAccessPdf field
            data = {}
            try:
                with requests.get(
                    f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                    headers=HEADERS,
                    params={"fields": "title,authors,year,venue,openAccessPdf"},
                    timeout=20,
                ) as resp:
                    if resp.status_code == 200:
                        data = resp.json()
                    else:
                        # The unauthenticated Graph API rate-limits hard
                        # (HTTP 429 is routine). Falling through with an
                        # empty dict keeps the Crossref metadata path alive
                        # instead of blowing up with an UnboundLocalError.
                        print(
                            f"[MicroBench] Semantic Scholar OA 查询返回 HTTP "
                            f"{resp.status_code} (DOI: {doi})",
                            file=sys.stderr,
                        )
                oa = data.get("openAccessPdf") or {}
                pdf_url = oa.get("url") if isinstance(oa, dict) else None
                if pdf_url:
                    # The OA URL comes from an upstream API, not from the
                    # user — but it is still an arbitrary URL, so it gets the
                    # same SSRF treatment as a user-supplied one. Without
                    # this, a crafted DOI whose metadata points at
                    # 127.0.0.1 would make the server probe the LAN.
                    _assert_public_http_url(pdf_url)
                    pdf_bytes = _download_pdf_bytes(
                        pdf_url, PDF_MAX_SIZE_MB * 1024 * 1024
                    )
                    if pdf_bytes:
                        source = "OpenAccess"
                        meta = {
                            "title": data.get("title", ""),
                            "authors": ", ".join(
                                a.get("name", "") for a in (data.get("authors") or [])
                            ),
                            "year": data.get("year", ""),
                            "venue": data.get("venue", ""),
                        }
            except requests.RequestException:
                pass
            except ValueError:
                # Blocked by the SSRF guard, or over the size cap.
                pass

            # Fall back to Crossref for metadata even if no PDF
            if not meta.get("title"):
                try:
                    cmeta = fetch_crossref_doi_metadata(doi)
                    meta = {**meta, **cmeta}
                except Exception:
                    pass

    # --- Otherwise try direct URL ---
    # SSRF guard: this path fetches an operator-supplied URL with no host
    # allowlist and with redirects followed, so anything the machine can
    # reach (localhost services, 169.254.169.254 metadata, LAN routers) is
    # reachable too. MicroBench is a local single-user app, so this is not a
    # remote attack surface, but a mistyped/hostile link (or a redirect from
    # an otherwise-legitimate publisher link) should not silently probe the
    # local network. Reject loopback / link-local / private / non-http(s)
    # targets, and re-check after redirects by disabling redirect-following.
    if pdf_bytes is None and identifier.startswith("http"):
        try:
            _assert_public_http_url(identifier)
            current = identifier
            hops = 0
            body = None
            # Follow up to 3 redirects manually, re-validating each hop.
            while True:
                resp = requests.get(
                    current, headers=HEADERS, timeout=60, stream=True,
                    allow_redirects=False,
                )
                try:
                    if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                        if hops >= 3:
                            break
                        nxt = requests.compat.urljoin(
                            current, resp.headers.get("Location", "")
                        )
                        if not nxt:
                            break
                        _assert_public_http_url(nxt)
                        current = nxt
                        hops += 1
                        continue
                    resp.raise_for_status()
                    ctype = resp.headers.get("Content-Type", "").lower()
                    if "pdf" in ctype:
                        # Stream with a running cap instead of `.content`,
                        # which would buffer an unbounded body before the
                        # size limit could be enforced.
                        chunks = []
                        total = 0
                        limit = PDF_MAX_SIZE_MB * 1024 * 1024
                        for chunk in resp.iter_content(chunk_size=64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > limit:
                                raise ValueError(
                                    f"PDF 超过 {PDF_MAX_SIZE_MB}MB 上限, 已中止下载"
                                )
                            chunks.append(chunk)
                        body = b"".join(chunks)
                    break
                finally:
                    resp.close()
            if body:
                pdf_bytes = body
                source = "URL"
        except requests.RequestException:
            pass
        except ValueError as e:
            # Blocked by the SSRF guard — surface it instead of silently
            # falling through to the generic "could not fetch" advice, which
            # would send the user hunting for a permissions problem that
            # isn't the cause.
            raise ValueError(f"出于安全考虑拒绝抓取该 URL: {e}")

    if pdf_bytes is None:
        raise ValueError(
            f"无法获取该论文的全文: '{identifier}'。\n"
            "原因: arXiv 未命中, 且出版商(IEEE/Elsevier/Wiley 等)未提供开放 PDF。\n"
            "建议:\n"
            "  1) 在校园网内登录 IEEE Xplore, 复制 PDF 直接 URL (右键 'Save as' 后粘贴 PDF 链接)\n"
            "  2) 找作者主页 / arXiv 预印本版 (Google Scholar → 'All versions' → 找 arXiv 链接)\n"
            "  3) 文献传递 / Sci-Hub (合规自查)\n"
            "  4) 如果已有 PDF 在本地, 把 PDF 直接拖入浏览器后, 复制 file:// URL 粘贴\n"
            "  5) 用 PDF 编辑器导出文本后直接粘贴文本到 LLM 问答区"
        )

    if len(pdf_bytes) > PDF_MAX_SIZE_MB * 1024 * 1024:
        raise ValueError(f"PDF 超过 {PDF_MAX_SIZE_MB}MB 上限, 已中止")

    # Extract text
    pages = extract_text_from_pdf(pdf_bytes)
    sections = split_into_sections(pages)

    return {
        "identifier": identifier,
        "source": source,
        "title": meta.get("title", ""),
        "authors": meta.get("authors", ""),
        "year": meta.get("year", ""),
        "venue": meta.get("venue", ""),
        "pdf_bytes": pdf_bytes,
        "pdf_size": len(pdf_bytes),
        "total_pages": len(pages),
        "char_count": sum(len(p["text"]) for p in pages),
        "sections": sections,
    }


__all__ = [
    "extract_text_from_pdf",
    "split_into_sections",
    "fetch_full_text",
]