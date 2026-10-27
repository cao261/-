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
import traceback
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
    Returns list of {page: int, text: str, glyphs: list}.

    glyphs：本页「解不出的字符」清单（解出来的那些不在里面）。
    文本里对应的位置是 ⟦G:<下标>⟧ 标记 —— 服务端拿 glyphs 去裁图，
    交给多模态模型认字（× ≈ ≥ 这些符号文本层解不出来，字形却是画出来的）。
    """
    pages = []
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                glyphs: list = []
                text = _extract_page_text(page, glyphs)
                pages.append({"page": i, "text": text, "glyphs": glyphs})
    except ImportError:
        # pdfplumber not installed — fall back to pypdf
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_bytes))
            for i, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                pages.append({"page": i, "text": text, "glyphs": []})
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


def extract_page_text_with_glyphs(pdf_path, page_no: int) -> tuple[str, list]:
    """抽一页文本，并一并返回本页「解不出的字符」清单。

    返回 (text, glyphs)：
      - text 里解不出的字符是 ⟦G:<下标>⟧ 标记（下标 = glyphs 的下标）
      - glyphs 每项含 font / cid / 坐标，服务端据此裁图让多模态认字

    给「按页翻译」和「章节翻译」用：翻译前先把标记换成真实符号，
    模型看到的才是 "500 × larger" 而不是 "500 [?] larger"。
    """
    glyphs: list = []
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as doc:
            if page_no < 1 or page_no > len(doc.pages):
                return "", glyphs
            text = _extract_page_text(doc.pages[page_no - 1], glyphs)
    except Exception:
        return "", glyphs
    return text, glyphs


_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?。！？]")
# 抽取层插入的标记（公式/表格）。页尾截断必须避开它们内部的小数点。
_MARKER_RE = re.compile(r"⟦(?:FORMULA|TABLE):[^⟧\n]*⟧|⟦G:\d+⟧")
# 末尾残片最多砍掉这么多字符，防止整页没有句号时把大半页都砍掉。
_MAX_TAIL_TRIM_CHARS = 1500


def render_page_png(pdf_path, page_no: int, zoom: float = 2.0,
                    max_pixels: int = 1_600_000) -> bytes:
    """把 PDF 的一页渲染成 PNG（多模态答疑用）。

    为什么需要它：表格和曲线图「不好文字化」—— pdfplumber 抽出来的只是
    碎片（"2 4 3" 这种上下标），模型看到的是一堆没有意义的字符。
    MiniMax-M3 是多模态模型，直接把这一页的截图喂给它，它就能自己看图
    读出数值和趋势（实测 image_url 输入 HTTP 200 且模型能正确读出图里的
    数值）。

    zoom=2.0 → 约 144 DPI，够看清坐标轴刻度。
    max_pixels 限制总像素数，避免超大页面把请求撑爆。
    返回 PNG 字节；渲染失败时抛异常由调用方兜底。
    """
    import io as _io
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - env dependent
        raise RuntimeError(
            "PDF 页面截图需要 PyMuPDF (fitz), 请先 pip install pymupdf"
        ) from exc

    path = Path(pdf_path)
    with fitz.open(str(path)) as doc:
        if page_no < 1 or page_no > doc.page_count:
            raise ValueError(f"页码越界: {page_no} (共 {doc.page_count} 页)")
        page = doc.load_page(page_no - 1)
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        # 像素总量保护：超出就按比例降一档 zoom
        if pix.width * pix.height > max_pixels:
            scale = (max_pixels / float(pix.width * pix.height)) ** 0.5
            mat = fitz.Matrix(zoom * scale, zoom * scale)
            pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")


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
    #
    #    标记（⟦FORMULA:45.1,138.3,…⟧ / ⟦TABLE:28.2,494.6,…⟧）内部的小数点
    #    也是"句末标点"，按标点切会把标记切成半截 —— 半个标记既匹配不上
    #    也不会被多模态读图处理，整张表就废了（实测页尾的表格标记被切掉
    #    "1⟧"，译文里留下半截标记 + 重复的表格）。
    #    → 切点必须避开标记内部；标记自己的结束处 "⟧" 才是合法切点。
    spans = [(m.start(), m.end()) for m in _MARKER_RE.finditer(text)]

    def _in_marker(pos: int) -> bool:
        return any(a < pos < b for a, b in spans)

    cuts = [e.end() for e in _SENTENCE_END_RE.finditer(text) if not _in_marker(e.end())]
    cuts.extend(b for _a, b in spans)      # 标记结束处也算切点
    cuts = sorted(c for c in cuts if c > 0)
    if cuts:
        cut = cuts[-1]
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

# ---------------------------------------------------------------------------
# 抽取算法版本号
# ---------------------------------------------------------------------------
# 每次改动「页内阅读顺序 / 公式处理 / 表格抽取」的算法都必须 +1。
#
# 翻译快照里记着生成它时的版本号。抽取算法一改，旧快照里的译文就是
# 「按老算法翻出来的」——栏序是错的、公式是占位符——继续拿来用只会让用户
# 以为修复没生效（2026-10-24 实测：10-23 14:17 的快照被 10-23 16:36 的
# 栏序修复甩在后面，读者照旧恢复旧快照，用户看到的还是"先翻译右边那栏"）。
#
# 版本对不上 → 快照里的页一律作废重翻。
#
# 2026-10-24: 2（公式改用坐标 + 边界吸附）
# 2026-10-25: 3（表格整块改成"多模态读图"标记）
# 2026-10-25: 4（裁剪框夹到页内 —— 越界会让整组正文消失）
# 2026-10-25: 5（解不出的数学符号带锚点标记 → 服务端"看图认字"）
EXTRACTOR_VERSION = 5

# pdfplumber 无法解码的字符会输出成 "(cid:12)" 这种占位符。
# （有捕获组：符号认字要拿到字符号；.sub() 的用法不受影响）
_CID_RE = re.compile(r"\(cid:(\d+)\)")
# 连续多个 cid 通常是一整段公式/符号（一次能抽出十几个），
# 逐个替换会让译文里出现 "[?][?][?][?][?][?][?][?]" 这种刷屏。
_CID_RUN_RE = re.compile(r"(?:\(cid:\d+\)[ \t]*){2,}")

# 解不出的字符在文本里的占位标记：⟦G:<序号>⟧
# 序号 = 该字符在「本页未解出字符表」里的下标（表按 page.chars 的内容流顺序排，
# 同一份 PDF 任何时候算出来都一样）。
#
# 为什么不用 "[?]": 那是**放弃**的标记 —— 用户看到 "500 [?] larger" 根本读不出
# 那是乘号。而这些符号（× ≈ ≥ ≤ …）在 PDF 里是**有字形的**，只是 TeX 数学字体
# 缺 ToUnicode 映射、pdfplumber 解不出来。标记带下标后，服务端可以：
#   ① 按坐标裁出这个字形 → ② 交给多模态模型认字 → ③ 存进「符号词典」缓存
# 之后同一形状的符号直接查表，零成本、零模型调用。
#
# 注意：**绝不能把 "(cid:N)" 原样交给文本模型** —— 它会把 cid 猜成
# \approx / \times，编出论文里根本没有的公式（2026-10-25 实测事故）。
GLYPH_MARKER_RE = re.compile(r"⟦G:(\d+)⟧")


def _unknown_glyphs(page) -> list[dict]:
    """本页里 pdfplumber 解码失败的字符。

    顺序 = page.chars 的内容流顺序（不排序！）—— 因为 page.chars 与
    page.extract_text() 的字符顺序一致，排序会破坏这种对应。

    每条记录：
        font   字体名（含子集标签，如 "KOGOBA+AdvP4C4E74"）
        cid    pdfplumber 给的字符号
        x0/x1/top/bottom  页面坐标（用来裁图 + 认字）
    """
    out: list[dict] = []
    try:
        chars = page.chars
    except Exception:
        return out
    for ch in chars:
        t = ch.get("text") or ""
        m = _CID_RE.match(t)
        if not m:
            continue
        out.append({
            "font": ch.get("fontname") or "",
            "cid": m.group(1),
            "x0": float(ch.get("x0", 0.0)),
            "x1": float(ch.get("x1", 0.0)),
            "top": float(ch.get("top", 0.0)),
            "bottom": float(ch.get("bottom", 0.0)),
        })
    return out


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
    # 畸形页 / page.width 取不到时会传 0 进来。原来直接除 → ZeroDivisionError
    # 一路冒到 extract_text_from_pdf，**整篇**抽取失败；而正确行为是这一页
    # 退回普通整页文本（返回全零覆盖 = "找不到栏缝" 的自然降级）。
    if not page_width or page_width <= 0:
        return cover
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


def _extract_page_text(page, out_glyphs: Optional[list] = None) -> str:
    """
    抽取单页文本，按阅读顺序排列：整宽区域(标题/摘要) -> 左栏 -> 右栏。
    任何一步检测不可靠都退回 pdfplumber 的普通抽取（只是空格仍然修好）。

    out_glyphs：出参。传入一个 list，本页解不出的字符会按顺序 append 进去
    （含坐标），服务端拿它把 ⟦G:n⟧ 认成真实符号。
    """
    return _mark_broken_formulas(
        _clean_cid_markers(_extract_page_text_raw(page, out_glyphs)))


# 公式碎片的标记。送给模型前先把碎公式整段替换成它，模型照抄即可。
FORMULA_MARKER = "⟦FORMULA⟧"

# 关系符号：= ≈ ≤ ≥ 以及 PDF 里常见的乱码等价物 ¼
_RELATION_RE = re.compile(r"[=≈≤≥¼]")
# 数学残片常见符号：括号(乱码) / 分数 / 上下标数字
_FRAGMENT_CHARS = "ðÞ[]()"

# 连续公式碎片行的**最大长度**（行数）。
#
# 超过这个数就不当成"一段坏公式"处理 —— 因为这么多行连续都是"数学行"，
# 更可能是一整块表格或一整段正文被误判，而不是一条被拆碎的公式。
#
# ⚠⚠ 这里有**两个**阈值，**故意不一样**（2026-10-27 取证后拆回来的）：
#
#   _MAX_BROKEN_FORMULA_RUN_LINES = 10  → _mark_broken_formulas
#       「这段是坏掉的公式」→ 整段替换成 ⟦FORMULA⟧ 占位符（没有坐标，
#       前端裁不出图）。阈值小一点更保守：宁可少判也别把一整块表格毁掉。
#
#   _MAX_FORMULA_SPAN_LINES = 12      → _formula_spans
#       「这段是一整条公式」→ 定位它的 bbox，交给前端从 PDF 原页裁图。
#       阈值必须够大：**实测 2024_1f_noise_TMR_sensor_bath_corre 第 6 页
#       的式 (A1) 整整 11 行**（积分上下限 + 求和号 + 分式被拆成多行）。
#       把它压到 10 → 公式块定位失败 → 11 行碎片直接进 prompt
#       → 模型会照着编出一条不存在的公式（§4.6：错公式比没公式危险得多）。
#       用 fitz 裁图比对确认过：旧阈值（12）裁出来的是完整的式 (A1)，
#       新阈值（10）裁不出图、只剩乱码文本。
#
# 所以这两个数**不是重复，是两条不同的判据**：一个管"要不要作废"，
# 一个管"能不能定位"。合并成一个会静悄悄弄坏 11 行的长公式。
_MAX_BROKEN_FORMULA_RUN_LINES = 10
_MAX_FORMULA_SPAN_LINES = 12

# 英文常用短词表（用于区分「正文残行」和「公式碎片」）。
#
# 为什么要这张表：`_is_mathish_line` 原来有一条
#     if len(letters) <= 2 and len(s) <= 12: return True
# 把 "in" / "of" / "as" 这类**栏缝裁下来的英文残行**判成公式行。
# 后果是正文整句消失（见 _is_mathish_line 的说明）。
#
# 但不能一刀切"有字母就不算公式" —— 公式碎片本身就长这样：
#     "ndm gp"   ← PDF 数学字体抽出来的分子分母碎片
#     "x"        ← 孤立的变量
# 一旦把它们判成正文，公式就会散架、碎片直接进 prompt（模型会瞎编）。
#
# 所以只排除「整行都是这些常用英文词」的情况。
# 词表刻意保守：只收学术论文里高频、且**不可能**是数学符号的词。
_ENGLISH_SHORT_WORDS = frozenset({
    # 介词 / 连词 / 冠词 / 常用动词
    "a", "an", "the", "and", "or", "but", "nor", "for", "yet", "so",
    "in", "on", "at", "to", "of", "by", "with", "from", "into", "onto",
    "over", "under", "above", "below", "near", "than", "then", "thus",
    "as", "is", "are", "was", "were", "be", "been", "being", "am",
    "it", "its", "we", "us", "our", "they", "them", "their", "he", "she",
    "this", "that", "these", "those", "there", "here", "not", "no",
    "if", "do", "does", "did", "can", "could", "may", "might", "must",
    "shall", "should", "will", "would", "has", "have", "had", "also",
    "which", "who", "whom", "whose", "when", "where", "why", "how",
    "all", "any", "each", "both", "few", "more", "most", "other",
    "some", "such", "only", "own", "same", "so", "too", "very",
    "can", "one", "two", "via", "per", "etc", "vs",
})


def _is_english_short_words(s: str) -> bool:
    """这一行是不是「整行都是英文常用短词」（→ 正文残行，不是公式）。

    判据：
      - 字母串全部是 1~3 个字母的**词**
      - 每个词都在 `_ENGLISH_SHORT_WORDS` 里
      - 行里没有数字、公式符号（"ðÞ[]()"）

    对照：
      "in of as"  → in/of/as 都在表里 → True  → 正文残行 ✓
      "ndm gp"    → ndm/gp 不在表里 → False → 公式碎片 ✓（保持原判定）
      "x"         → x 不在表里     → False → 公式碎片 ✓
      "Z−∞"       → 有公式符号     → False → 公式 ✓
    """
    if any(ch.isdigit() for ch in s):
        return False
    if any(ch in _FRAGMENT_CHARS for ch in s):
        return False
    words = re.findall(r"[A-Za-z]+", s)
    if not words:
        return False
    if not all(len(w) <= 3 for w in words):
        return False
    return all(w.lower() in _ENGLISH_SHORT_WORDS for w in words)

# 分栏所需的最小词数。
#
# ⚠ 2026-10-27：这里**曾经**被从 40（全页）/ 30（标题区以下）降到 12/10，
#   理由是"原阈值没挡住任何真问题"。**这个理由是错的，已回退。**
#
# 取证（全库 27 篇 / 429 页）：
#   - words < 40 的页有 3 个，其中 **1 个不是"一页一张图的短页"**：
#     `2024_Fe_MgO_Fe_DMFT_TMR_first_princ.pdf` 第 20 页 —— 那是
#     **双栏论文的最后一页**，整页只有一条横跨两栏的参考文献
#     （"A. Cebollada, ... Applied Physics Letters 79, ..."）
#     加右上角的页码。
#   - 降阈值后这一页进了分栏路径，而栏缝恰好落在连字符上
#     （"Ap-" | "plied"）→ 参考文献被从中间劈开、页码"20"楔进两半之间。
#     用 fitz 渲染原页确认过：这条参考文献本来是**跨栏整行**。
#
#   → 词数少 ≠ 不可能分栏。**保守阈值是对的**：词数不够就别猜栏。
#   真正的双栏判断交给栏缝探测（`_longest_gap` + `_vote_column_gap`），
#   探不到就退回 plain。
_MIN_WORDS_FOR_COLUMNS = 40
_MIN_BELOW_WORDS_FOR_COLUMNS = 30


def _is_mathish_line(line: str) -> bool:
    """判断一行是不是「公式或公式碎片」而不是正文句子。

    **正文优先**（2026-10-24 修）：早先的判据是「有关系符号就是公式」，
    结果把这种行整行吞成了公式图：

        f(ω) = −∞ f(t) exp ( − iωt) dt. Substituting the inverse

    —— 行尾的正文（Substituting the inverse）跟着进了图，译文里这句话
    直接消失。实测 IEEE 论文 2024_1f_noise 第 3 页：'substituting'、
    'inverse' 两个词在译文里整个不见。

    所以现在：**先数"词状 token"**（≥3 个字母、不含数字/符号、且不是
    (cid:N) 这种占位符）。出现 ≥2 个 → 是正文，不碰。

    为什么不是"有没有长词"（≥4 字母）：公式符号本身也会是长词
    （ε_eff 抽成 "eeff"），按"有没有"会把 eq (3) 判成正文；
    为什么不是"≥2 个长词"：正文行 "are assumed: α = 0.05, d = 2 nm, ..."
    只有 "assumed" 一个长词，按那个判据它会变成公式图 →
    整句话（含 0.05 这些数值）从译文里消失（实测 1f_noise 第 5 页）。

    词状 token（≥3 字母、纯字母）：
        are assumed: α = 0.05 …   → are / assumed / and  = 3 个 → 正文 ✓
        eeﬀ ¼ 33 33 (3)           → eeff               = 1 个 → 公式 ✓
        E;31 nsp ð1 (cid:4) …     → nsp                = 1 个 → 公式 ✓
        f(ω) = … Substituting the inverse → exp/Substituting/the/inverse = 4 → 正文 ✓
    """
    s = (line or "").strip()
    if not s:
        return False
    if len(s) > 200:
        return False
    # (cid:N) / ⟦G:n⟧ 是 PDF 无法解码的符号占位符，不是"词"
    probe = re.sub(r"\(cid:\d+\)", " ", s)
    probe = GLYPH_MARKER_RE.sub(" ", probe)
    wordish = re.findall(r"(?<![A-Za-z])[A-Za-z]{3,}(?![A-Za-z])", probe)
    if len(wordish) >= 2:
        return False          # 正文，不碰
    if _RELATION_RE.search(s):
        # 有关系符号 → 是公式（长度上限放宽，公式行可以很长）
        return len(s) <= 160
    if "(cid:" in s or "[?]" in s or GLYPH_MARKER_RE.search(s):
        return True
    # 纯符号行："− ×"、"Z−∞"、"∞"、"× −" —— 公式的积分上下限、乘号残留。
    # 这类行不含 4 字母词，但也不含数字/括号，很容易被当成正文，
    # 一断行公式就被切成好几段（实测 eq (12) 被切成 3 块）。
    #
    # ⚠ 2026-10-27 修正：原来第一条是
    #     letters = re.findall(r"[A-Za-z]", s)
    #     if len(letters) <= 2 and len(s) <= 12:
    #         return True
    # → "in" / "of" / "as" / "to" 这些**孤立的英文短词**全部满足
    #   「≤2 个字母 且 ≤12 字符」→ 被判成公式行。
    #
    #   后果不是"多认了一个公式"，而是**正文整句消失**：
    #   _mark_broken_formulas 按「连续的公式碎片行」成段判定 —— 一个孤立的
    #   "in" 紧挨着真正带 [?] 的公式行时会被并进那一段 → 整段替换成
    #   ⟦FORMULA⟧ → 这句正文从译文里不见了。
    #
    # 修法（**刻意保守**）：只把「整行都是真实英文短词」的行排除掉，
    # 其余一律保持原判定。
    #
    #   为什么不能一刀切"有字母就不算公式"：公式碎片本身就长这样 ——
    #     "ndm gp"    ← PDF 数学字体抽出来的分子分母碎片
    #     "x"         ← 孤立的变量
    #   把它们判成正文，公式就会散架、碎片直接进 prompt（模型会瞎猜）。
    #   实测：JACERS 第 2 页的公式 (2) 就是这么散开的。
    #
    #   所以用一张**英文常用短词表**来区分：
    #     "in of as"  → 全是常用词 → 正文残行 → 不是公式
    #     "ndm gp"    → 不在词表 → 公式碎片 → 照旧
    letters = re.findall(r"[A-Za-z]", s)
    if len(letters) <= 2 and len(s) <= 12:
        # 整行都是英文常用短词 → 是正文残行（栏缝裁下来的、原本的连接词）
        if _is_english_short_words(s):
            return False
        return True
    has_digit = any(ch.isdigit() for ch in s)
    has_special = any(ch in _FRAGMENT_CHARS for ch in s)
    short_words_only = bool(re.fullmatch(r"[A-Za-z]{1,3}(\s+[A-Za-z]{1,3})*", s))
    if short_words_only and _is_english_short_words(s):
        # "in of as" 这种整行都是常用英文词 → 正文残行，不是公式
        return False
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
    if not text or ("[?]" not in text and not GLYPH_MARKER_RE.search(text)):
        return text
    lines = text.split("\n")

    # 1) 找连续的公式碎片行
    runs: list[list[int]] = []
    cur: list[int] = []
    for idx, line in enumerate(lines):
        if _is_mathish_line(line):
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
        if len(run) > _MAX_BROKEN_FORMULA_RUN_LINES:
            continue
        seg = "\n".join(lines[i] for i in run)
        has_unk = ("[?]" in seg) or bool(GLYPH_MARKER_RE.search(seg))
        # 关系符号可能自己也是解不出的字符（数学字体里 "=" 就是），
        # 所以除了"有字面关系符号"，还要看**这行像不像公式**
        # （_is_mathish_line：没有 ≥2 个长词 + 带未解出符号 → 公式行）。
        # 必须**逐行**判：_is_mathish_line 对超过 200 字符的整段直接返回 False，
        # 而公式块往往就是一大段（实测某数学页一整段公式 600+ 字符）。
        # 正文里的行内符号（"up to 500× larger than any other ME materials"
        # 有很多长词）不会被误判成公式。
        any_mathish = any(_is_mathish_line(lines[i]) for i in run)
        if has_unk and (_RELATION_RE.search(seg) or any_mathish):
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


# ---------------------------------------------------------------------------
# 公式区域：带 bbox 的标记
# ---------------------------------------------------------------------------
# 公式在 PDF 里是矢量排版：分子一行、分母一行、上下标各一行，文本层抽出来
# 是乱序碎片（实测 α_E,31 = n·d₃₁ᵐ·g₃₁ /(n·s₁₁ᴱ(1-(k₃₁ᴱ)²) - s₃₃ᵐ(1-n))
# 被抽成 6 行碎片，模型看着只会瞎猜）。
#
# 两条路：
#   a) 照碎片拼 LaTeX —— 极容易把分子分母装反（用户原话："分子分母分不清
#      谁是谁"）。而错公式比没公式危险得多：用户会照着它去算。
#   b) 不猜：**把公式在 PDF 上的位置记下来**，前端按这个坐标从原页裁一张图
#      贴进译文 —— 和论文里长得一模一样，绝不会错。
#
# 走 b。标记格式：⟦FORMULA:x0,y0,x1,y1⟧（PDF 页坐标，左上角原点，单位 pt）。
# 标记要原样穿过模型的翻译（提示词要求逐字照抄），前端再拿它去裁图。
FORMULA_MARKER_BBOX_RE = re.compile(
    r"⟦FORMULA:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),"
    r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)⟧"
)

# ---------------------------------------------------------------------------
# 表格标记：⟦TABLE:x0,y0,x1,y1⟧（PDF 页坐标，pt）
# ---------------------------------------------------------------------------
# 表格在 PDF 文本层是碎的："d (pC/N) (cid:4)90 (cid:4)175 (cid:4)109 B700 16.5"
# —— 连负号都认不出，模型只能靠上下文猜对齐，结果是**整行数值错位**
# （实测 JACERS Table I：d31 行的 16.5 跑到了 d33 行，7200 变成 B700）。
#
# 所以表格不交给文本模型猜：抽取层只记下表格在页面上的位置，
# 由服务端把这一块**裁成图交给多模态模型抄**（app.py 的 _read_table_vision）。
# 图上是什么样，表格就是什么样 —— 顺序和数值都不会错。
TABLE_MARKER_RE = re.compile(
    r"⟦TABLE:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),"
    r"(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)⟧"
)

# 公式块的竖直高度上限（pt）。超过这个高度就不是"一行公式"，多半是表格。
_FORMULA_MAX_HEIGHT = 90.0


def _crop_lines(page, bbox, margin: float = 16.0) -> list[dict]:
    """裁一块区域，返回该区域的文本行（每行带 bbox）。

    为什么不用 extract_text()：它只给字符串。我们要的是「哪一行在哪」——
    公式要按位置裁图（⟦FORMULA:x0,y0,x1,y1⟧），行要能定位。

    实测：把 extract_text_lines() 的行文本用 "\\n" 拼起来，与 extract_text()
    的输出**逐字相同**（JACERS 综述第 2 页左栏 823 字符完全一致），所以换用
    行级抽取不会改变正文内容，只是多带了坐标。

    **为什么要向外扩 margin 再按行顶过滤**（2026-10-24 实测）：
    pdfplumber 裁剪时会把压在裁剪边上的字形**切掉一半**，剩下的半个字形
    的 top 被裁到边界值 → 它跟下一行的字混成同一行 → 两行被**逐字交错**：

        cannot be coprocessed with oxide ferroelectrics, as sintering
        would oxidize the iron and dramatically reduce the alloys supe-
        → cwaonunlodtobxiediczoeptrhoeciersosnedanwditdhraomxiadteicaflelryroree

    这行乱码直接进了翻译。扩边裁剪 → 行完整落在裁剪框里，不再被切；
    再按「行的 top 属于哪一带」过滤 → 每行只归属一带，不重不漏。
    """
    x0, y0, x1, y1 = bbox
    # 裁剪框必须**完全落在页面内** —— pdfplumber 的 crop() 越界会直接抛
    # ValueError（"Bounding box ... is not fully within parent page"）。
    # 而"向外扩 margin"很容易把框推出页面下沿（实测 JACERS 第 4~6 页：
    # 最后一组扩到 784 > 页高 782.986 → 整组文字全丢，那三页正文少了
    # 83~148 个词，页面上却看不出任何报错）。
    ph = float(getattr(page, "height", 0) or 0)
    pw = float(getattr(page, "width", 0) or 0)
    cy0 = max(0.0, y0 - margin)
    cy1 = (y1 + margin) if not ph else min(ph, y1 + margin)
    cx0 = max(0.0, x0)
    cx1 = (x1) if not pw else min(pw, x1)
    if cy1 <= cy0 or cx1 <= cx0:
        return []
    box = (cx0, cy0, cx1, cy1)
    try:
        cropped = page.crop(box)
    except Exception:
        return []
    try:
        raw = cropped.extract_text_lines(
            x_tolerance=PDF_X_TOLERANCE, return_chars=False)
    except Exception:
        raw = []
    out: list[dict] = []
    for ln in raw or []:
        text = (ln.get("text") or "").strip()
        if not text:
            continue
        try:
            top = float(ln.get("top", 0.0))
            bottom = float(ln.get("bottom", 0.0))
            lx0 = float(ln.get("x0", 0.0))
            lx1 = float(ln.get("x1", 0.0))
        except (TypeError, ValueError):
            continue
        # 只保留「行顶属于这一带」的行 —— 相邻带的行在这里出现，但归它们
        if not (y0 - 0.01 <= top < y1):
            continue
        out.append({
            "text": text,
            "x0": lx0,
            "x1": lx1,
            "top": top,
            "bottom": bottom,
        })
    return out


def _line_intervals(words) -> list[tuple[float, float]]:
    """所有文字行的 y 区间（按 top 聚类，重叠的合并）。

    用来判断「某个裁剪边界是不是切在字上」。
    """
    if not words:
        return []
    spans: list[list[float]] = []
    for w in sorted(words, key=lambda w: float(w["top"])):
        try:
            t = float(w["top"])
            b = float(w.get("bottom", t))
        except (TypeError, ValueError):
            continue
        if spans and t <= spans[-1][1] + 0.5:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([t, b])
    return [(a, b) for a, b in spans]


def _snap_to_line_edge(y: float, intervals) -> float:
    """裁剪边界切在字上时，把边界上移到那一行的顶边。

    为什么必须修：pdfplumber 裁到半行时，那一行会被截断/错位，实测
    （JACERS 综述第 2 页右栏）相邻两行被逐字交错成一团乱码：

        erties of magnetostrictive materials previously used in ME lam-
        inate composites. Ferrites have large resistances (relative to
        → einrtaitees ocof mmpaognsietteos.strFicetrirvietems ahtaevrie...

    这行乱码直接进了翻译（模型只能靠上下文硬猜）。上移到行顶边 → 整行完整
    地落在同一块里，既不截断也不丢字。
    """
    for t, b in intervals:
        if t < y < b:
            return max(0.0, t - 0.01)
    return y


def _mark_attached_lines(lines: list[dict]) -> list[bool]:
    """标出「其实是上一行上标/下标」的短行。

    判据：短行 + y 区间和上一条**正文行**重叠 ≥ 60% + x 区间被它包住。

    为什么必须做：论文里的上标（引用序号 3）、下标（d₃₁ 的 31）、α_ME 的
    ME，在文本层里都是**独立的一行**。不排除它们，"a" + "ME" 会被当成
    两行公式碎片，公式块的位置也就算不准了。

    只对正文行生效：公式行下面挂着的小字（分母的下标 11/31/33）是公式
    的一部分，不能摘出去。
    """
    attached = [False] * len(lines)
    for i in range(1, len(lines)):
        j = i - 1
        while j >= 0 and attached[j]:
            j -= 1
        if j < 0:
            continue
        prev = lines[j]
        cur = lines[i]
        if _is_mathish_line(prev["text"]):
            continue          # 上一行是公式 → 挂在它下面的小字属于公式
        pw = prev["bottom"] - prev["top"]
        cw = cur["bottom"] - cur["top"]
        if pw <= 0 or cw <= 0:
            continue
        overlap = min(prev["bottom"], cur["bottom"]) - max(prev["top"], cur["top"])
        if overlap <= 0 or overlap / cw < 0.6:
            continue
        if cur["x0"] >= prev["x0"] - 0.5 and cur["x1"] <= prev["x1"] + 0.5:
            attached[i] = True
    return attached


def _formula_spans(lines: list[dict], attached: list[bool]) -> list[tuple[int, int]]:
    """找出公式块，返回 [(起始行号, 结束行号), ...]。

    判据（比「看着像公式」严格，因为误判的代价不对称）：
      - 连续若干行都是"数学行"（没有 ≥2 个 4 字母以上的英文词）；
      - 块里含关系符号（= ≈ ≤ ≥，PDF 里常被抽成 ¼）。
        **必须有等号**：表格行也含大量 (cid: 碎片（"d 31(pC/N) (cid:4)90 ..."），
        但表格行没有等号 —— 没有这一条，整张表会变成一张图，
        用户就没法翻译表里的内容了（实测 JACERS Table I 整表被吞）。
      - 块的高度不超过 90pt（超高的多半是表格，不是公式）。
    """
    runs: list[list[int]] = []
    cur: list[int] = []
    for i, ln in enumerate(lines):
        if attached[i] or not _is_mathish_line(ln["text"]):
            if cur:
                runs.append(cur)
                cur = []
            continue
        cur.append(i)
    if cur:
        runs.append(cur)

    out: list[tuple[int, int]] = []
    for run in runs:
        if len(run) > _MAX_FORMULA_SPAN_LINES:
            continue
        seg = "\n".join(lines[i]["text"] for i in run)
        height = (lines[run[-1]]["bottom"] - lines[run[0]]["top"])
        if height > _FORMULA_MAX_HEIGHT:
            continue
        if _RELATION_RE.search(seg):
            out.append((run[0], run[-1]))
    return out


def _mark_line_glyphs(line: dict, glyphs: list) -> str:
    """把一行文本里的 "(cid:N)" 换成带锚点的标记 ⟦G:<下标>⟧。

    怎么对回具体字形：字形的 x 区间落在这一行里、且垂直方向落在**行的带内**
    （含上下标）→ 唯一匹配。

    注意：**不能拿字形的 top 直接跟行的 top 比**（容差 4pt 就够）——
    数学公式里的上下标、分数线高度差很多，按 top 比会一个都匹配不上
    （实测某数学页 632 个 cid 一个都没标上）。判据改成"落在行的垂直带内"。

    **只有孤立符号才给锚点**：连续的 cid（中间只隔空格）是一整块东西
    （公式碎片 / 参考文献编号 "[12]"），旧行为把它们折叠成**一个** "[?]"——
    文本不膨胀、模型也不会被灌几百个标记。折叠成整块的那些保持旧行为。

    对不上就留 "[?]"，**宁可空着也不能指错**（指错等于编造）。
    """
    text = line.get("text") or ""
    if "(cid:" not in text:
        return text
    lx0 = float(line.get("x0", 0.0))
    lx1 = float(line.get("x1", 0.0))
    ltop = float(line.get("top", 0.0))
    lbottom = float(line.get("bottom", ltop))
    cand = [i for i, g in enumerate(glyphs)
            if g["x0"] >= lx0 - 4.0 and g["x1"] <= lx1 + 4.0
            and g["top"] >= ltop - 8.0 and g["bottom"] <= lbottom + 8.0]
    cand.sort(key=lambda i: glyphs[i]["x0"])

    occ = list(_CID_RE.finditer(text))
    if not occ:
        return text
    # 把连续的 cid 归成组（中间只隔空格/制表符）
    groups: list[list[int]] = []
    k = 0
    while k < len(occ):
        j = k
        while (j + 1 < len(occ)
               and occ[j + 1].start() - occ[j].end() <= 3):
            j += 1
        groups.append(list(range(k, j + 1)))
        k = j + 1

    out: list[str] = []
    pos = 0
    ci = 0            # 候选字形的游标
    for grp in groups:
        m0 = occ[grp[0]]
        out.append(text[pos:m0.start()])
        picked = cand[ci:ci + len(grp)]
        ci += len(grp)
        if len(grp) == 1 and picked:
            # 孤立符号 → 带锚点，交给服务端认字
            out.append(f"⟦G:{picked[0]}⟧")
        else:
            # 连续一整块 → 整体占位（跟旧行为一致，不给模型灌标记）
            out.append("[?]")
        pos = occ[grp[-1]].end()
    out.append(text[pos:])
    return "".join(out)


def _plain_lines_with_glyphs(page, glyphs: list) -> str:
    """把整页文本按行抽出来、标上字形锚点，再拼回去（兜底路径用）。

    为什么不用 extract_text()：它只给字符串，没有每行的几何信息，
    cid 就无法与页面上的具体字形对应（→ 全部退回 "[?]"，还被当成坏公式丢掉）。
    行级抽取（extract_text_lines）带 bbox，与 extract_text 的输出**逐字相同**
    （AGENTS.md §4.12 实测过这个等价性），所以换它不改正文，只多带坐标。

    抽不出来就返回空串 → 调用方继续用原来的 plain。
    """
    if not glyphs:
        return ""
    try:
        lines = page.extract_text_lines(
            x_tolerance=PDF_X_TOLERANCE, return_chars=False)
    except Exception:
        return ""
    if not lines:
        return ""
    out: list[str] = []
    for ln in lines:
        try:
            t = (ln.get("text") or "").strip()
        except Exception:
            continue
        if not t:
            continue
        out.append(_mark_line_glyphs(ln, glyphs))
    return "\n".join(out)


def _lines_to_text(lines: list[dict], mark_formulas: bool = True,
                   glyphs: Optional[list] = None) -> str:
    """把文本行拼成整段文本，并把公式块换成带坐标的标记。

    glyphs：本页「解不出的字符」表。给了就把行里的 "(cid:N)" 换成
    带锚点的标记 ⟦G:<下标>⟧，服务端据此裁图让多模态认字。
    """
    if not lines:
        return ""
    spans = _formula_spans(lines, _mark_attached_lines(lines)) if mark_formulas else []
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        hit = None
        for a, b in spans:
            if a == i:
                hit = (a, b)
                break
        if hit:
            a, b = hit
            x0 = min(lines[k]["x0"] for k in range(a, b + 1))
            x1 = max(lines[k]["x1"] for k in range(a, b + 1))
            top = min(lines[k]["top"] for k in range(a, b + 1))
            bottom = max(lines[k]["bottom"] for k in range(a, b + 1))
            # 往外留一点边距，别把上下标/分数线切掉
            pad = 2.0
            out.append(
                "⟦FORMULA:{:.1f},{:.1f},{:.1f},{:.1f}⟧".format(
                    max(0.0, x0 - pad), max(0.0, top - pad),
                    x1 + pad, bottom + pad)
            )
            i = b + 1
            continue
        out.append(_mark_line_glyphs(lines[i], glyphs) if glyphs else lines[i]["text"])
        i += 1
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


def _plain_text(page) -> str:
    """pdfplumber 的整页文本（双栏会被逐行交错，只在兜底路径用）。"""
    try:
        return page.extract_text(x_tolerance=PDF_X_TOLERANCE) or ""
    except Exception as e:
        # ⚠ 必须留痕：静默返回空串 = 这一页正文凭空消失。
        #   用户只会看到"译文少了一大段"，日志里却什么都没有（§4.13 的教训）。
        print(f"[paper_text] 页面文本抽取失败，当作空页: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return ""


def _extract_page_text_raw(page, out_glyphs: Optional[list] = None) -> str:
    # ⚠ 顺序有意义：先算 glyphs，再决定要不要跑 extract_text。
    #
    #   有未解出的数学符号时，下面的 _plain_lines_with_glyphs() 会重新切行、
    #   输出**带 ⟦G:n⟧ 标记**的文本，把 extract_text() 的结果整个覆盖掉 ——
    #   那先前那次调用就是白跑一遍 pdfminer layout（单页几十~几百毫秒，
    #   391 页的库累积起来很可观）。
    #   输出与原来逐字一致（覆盖关系没变）→ 不需要动 EXTRACTOR_VERSION。
    glyphs = _unknown_glyphs(page)
    if out_glyphs is not None:
        out_glyphs.extend(glyphs)

    plain = ""
    if glyphs:
        marked_plain = _plain_lines_with_glyphs(page, glyphs)
        if marked_plain:
            plain = marked_plain
    if not plain:
        plain = _plain_text(page)

    try:
        width, height = float(page.width), float(page.height)
        words = page.extract_words(
            x_tolerance=PDF_X_TOLERANCE, use_text_flow=False, keep_blank_chars=False
        )
    except Exception:
        return plain

    # 词数下限：**原来**是 40 / 30（硬编码的魔法数字）。
    #
    # ⚠ 2026-10-27 取证（全库 27 篇 / 429 页）：
    #   words < 40 的页只有 3 个（21 词 / 2 词 / 2 词）—— 全是真正的短页
    #   （一整页只有一张图或一段图注），退回 plain 是**对的**。
    #   below < 30 但 words >= 40 的页只有 1 个，而那一页的栏缝检测返回 None
    #   （整宽内容，本来也测不出双栏）→ 降阈值救不了它。
    #
    # 所以：**不靠词数决定是否双栏**，而是先探栏缝，探到了才走双栏路径。
    # 词数只作为"太少就不可能分栏"的兜底（真正分栏至少要每栏几个词）。
    if len(words) < _MIN_WORDS_FOR_COLUMNS:
        return plain

    # 1) 垂直方向：找到双栏开始的 y（整宽标题区结束的位置）
    y_split = _find_vertical_split(words, width, height)

    below = [w for w in words if w["top"] >= y_split]
    if len(below) < _MIN_BELOW_WORDS_FOR_COLUMNS:
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
    left_parts, right_parts, span_units = _extract_by_bands(
        page, words, width, height, y_split, x_mid, glyphs=glyphs)

    # 3b) 双栏正文起点以上的区域也要分栏 —— 见 _extract_top_region 的说明。
    #     这一步是「先翻译右边的了」那个 bug 的真正修复点。
    front, top_left, top_right = _extract_top_region(
        page, words, width, height, y_split, x_mid, glyphs=glyphs)
    left_parts = top_left + left_parts
    right_parts = top_right + right_parts

    if not left_parts and not right_parts and not span_units and not front:
        return plain
    return _join_reading_order(left_parts, right_parts, span_units, "\n\n".join(front))


def _band_kind(bw, width: float, x_mid: float, x_bin: int) -> str:
    """判断这一带是「双栏」（split）还是「整宽」（span）。

    判据：**栏缝位置有没有空隙**。

    为什么不能再看「两边各有多少词」（2026-10-24 实测踩坑）：
    左栏只剩几个词的带（公式的下标行 "11 31 33"）会被判成「整宽」→
    整宽裁剪把左右两栏的文字合进同一行 → 变成
    "11 31 33 rior magnetostriction. Also, Terfenol-D has ..." 这种
    逐字交错的乱码，直接进了译文。

    现在只看栏缝：中间空 → 双栏（各裁各的）；中间被填满 → 整宽
    （跨栏表格、整宽图注）。

    容差用 **2 个分箱**（≈10pt）而不是 3 个：
    跨栏表格的两半之间也有缝（实测 JACERS Table I：左右两半的缝只有 ~7pt），
    容差 3 时表格行被判成「双栏」→ **行被栏缝劈成两半** → 模型对不上列 →
    表格数值整体错位一行（实测 d31 那行的 16.5 跑到了 d33 行）。
    容差 2 时表格行判成「整宽」→ 整行抽出来 → 数值顺序正确。
    """
    cover = _x_cover(bw, width, _X_BINS)
    return "split" if _has_gap_near(cover, x_bin, tol_bins=2) else "span"


def _extract_top_region(page, words, width, height, y_split, x_mid,
                          glyphs: Optional[list] = None):
    """把「双栏正文起点以上」的区域按栏拆开。

    返回 (整宽块, 左栏段, 右栏段)。

    为什么必须拆（这是用户反馈「先翻译右边的了」的根因）:
      旧做法把 y_split 以上的区域**整宽裁一刀**当页眉，然后排在最前面。
      很多期刊页顶部并不是「整宽标题区」，而是**左右两栏各自从不同高度
      开始**（页顶被一张大图占住，只有另一栏先有文字）。实测 JACERS 综述
      第 3 页：右栏图注从 y=227 就开始，左栏正文要到 y=450 才开始 ——
      整宽裁一刀就把右栏正文整块抢到了最前面，左栏正文反而排在后面，
      译文读起来完全是乱的。

      所以这里用同一套「按带找栏缝」的判据把顶部区域也分栏:
        - 真的双栏的带 → 左右分开，��左归左、右归右；
        - 整宽的带（页眉/标题/整宽图注）→ 单独成块，仍排最前；
        - 只有一栏有字的带 → 归那一栏。
    """
    front: list[str] = []
    left_parts: list[str] = []
    right_parts: list[str] = []
    if y_split <= 0:
        return front, left_parts, right_parts

    band_h = max(8.0, height / 40.0)
    x_bin = int(round(x_mid / width * _X_BINS))
    intervals = _line_intervals(words)
    # 注意：顶部区域**不能**把相邻的同类带合并成一块。
    # 试过（2026-10-24）：合并后一个块的裁剪范围横跨了整页顶部，
    # 左栏那一侧把公式碎片和右栏正文混进了同一行 —— 阅读顺序直接乱掉
    #（右栏第一段里插进了左栏的分母碎片 "nsp11ð1..."）。
    # 代价只是每带之间多一个段落断裂（译文里那句话会被拆成两段），
    # 远比顺序错乱好。
    y = 0.0
    while y < y_split - 0.01:
        y1 = min(y + band_h, y_split)
        y1 = _snap_to_line_edge(y1, intervals)
        if y1 <= y + 0.01:
            y1 = min(y + band_h, y_split)
        bw = [w for w in words if y <= w["top"] < y1]
        if bw:
            kind = _band_kind(bw, width, x_mid, x_bin)
            try:
                if kind == "split":
                    lt = _lines_to_text(_crop_lines(page, (0, y, x_mid, y1)),
                                        glyphs=glyphs)
                    rt = _lines_to_text(_crop_lines(page, (x_mid, y, width, y1)),
                                        glyphs=glyphs)
                    if lt.strip():
                        left_parts.append(lt.strip())
                    if rt.strip():
                        right_parts.append(rt.strip())
                else:
                    st = _lines_to_text(_crop_lines(page, (0, y, width, y1)),
                                        glyphs=glyphs)
                    if st.strip():
                        front.append(st.strip())
            except Exception:
                # 原来这里是 except: pass —— 一带抽取失败就静默跳过，
                # 结果整页的图注/正文凭空消失（2026-10-25 实测 JACERS 第 4~6 页：
                # 顶部区域的每一带都失败 → 整页正文少了 83~148 个词，
                # 而且没有任何报错）。静默兜底必须留痕。
                import traceback
                print(f"[MicroBench] 顶部区域一带抽取失败: "
                      f"{traceback.format_exc()}", file=sys.stderr)
        y = y1
    return front, left_parts, right_parts


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
        # 单元内部用单换行连着（表格的标题和它的表体必须紧挨着），
        # 单元之间空一行（它们是独立的一块内容）。
        block = "\n".join(p for p in unit if p.strip())
        if block:
            parts.append(block)
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


def _looks_like_body_text(bw, x_mid: float) -> bool:
    """判断一带里的文字「像正文」而不是「像表格行」。

    正文：两栏都有不少词，而且有像英文单词的东西（≥4 字母）。
    表格行：词多但都很短（数字、单位、下标），没有英文单词。
    """
    def bodyish(ws) -> bool:
        if len(ws) < 8:
            return False
        txt = " ".join(w["text"] for w in ws)
        return len(re.findall(r"[A-Za-z]{4,}", txt)) >= 4
    left = [w for w in bw if w["x1"] <= x_mid]
    right = [w for w in bw if w["x0"] >= x_mid]
    return bodyish(left) and bodyish(right)


def _band_is_table_like(bw, x_mid: float) -> bool:
    """这一带像不像表格行（可以并进表格区域）。

    表格区域**只能由这样的带延续**（2026-10-25 独立审查发现 13 处
    表格区域把正文也吞了进去）：表题后面如果跟着一段"短"正文
    （左右栏各不到 8 个词 → _looks_like_body_text 判不出来），
    区域就会一直延伸，把那几段正文从正文里删掉，还当成表格让多模态
    "逐格抄" —— 正文就这么消失了。
    → 区域收尾看的是「像不像表格行」，不是「像不像正文」。

    表格行的样子：**词很短、含数字、没有像英文单词的东西**
    （"BaTiO3 PZT-5 PZNPT 2605"）。正文段落的词是长单词
    （"the following parameters"），表格行里没有。

    判"像英文单词"不能只看字母数：**材料名也是长词**
    （BaTiO3 / Terfenol-D / PZNPT），所以再看**是否以小写为主**
    （真英文单词大多是小写，材料名是大小写混排）。
    """
    if _looks_like_body_text(bw, x_mid):
        return False
    txt = " ".join(w["text"] for w in bw)
    prose_words = 0
    for w in re.findall(r"[A-Za-z]{4,}", txt):
        low = sum(1 for c in w if c.islower())
        if low >= len(w) * 0.6:
            prose_words += 1
    if prose_words >= 4:
        return False          # 有像英文单词的东西 → 是正文
    digits = sum(1 for w in bw if any(ch.isdigit() for ch in w["text"]))
    return digits >= 1


# 论文里的表格必有表题（"Table I. Properties of ..." / "TABLE 2" / "表 1"）。
# **用表题定位表格**，而不是用"有很多数字"—— 公式行也全是数字
# （"a ¼ 33 31 (2)"），按数字判会把公式当表格（实测 eq (2)/(3) 被误判）。

def _numeric_rows(bw) -> int:
    """这一带里有几行是"数字行"（含 3 个以上数字的短词）。"""
    n = 0
    for w in bw:
        if sum(1 for c in w["text"] if c.isdigit()) >= 3:
            n += 1
    return n


def _find_table_regions(bands, x_mid: float, max_gap: float = 16.0,
                        min_bands: int = 2, min_numeric: int = 2):
    """找出页面上的表格区域。

    返回 [[(y0, y1, bw), ...], ...]（每个区域 = 连续的若干"表状带"）。

    判据：**连续的表状带**（词短、含数字、没有像英文单词的东西），
    至少 2 带、其中至少 2 带是数字行。

    为什么不用"表题"定位（2026-10-25 独立审查后改）：
      - 表题常常不在带首（带高 ~20pt，一条带塞两行）→ 覆盖率只有 2/43；
      - 改成"表状行成段"后，表的**行**本身（"d 31(pC/N) 90 175 ..."）
        才是可靠的信号。
      - 表题所在的带不算"表状带"（表题是长单词）→ 表题**留在正文里**，
        由主模型翻译；图片里只有表体，不会重复。
    """
    regions: list[list] = []
    cur: list = []
    prev_y1 = None
    prev_band = None      # 上一个带（用来把表题并进区域）

    def _numeric_count(reg):
        return sum(1 for (_, _, bw) in reg if _numeric_rows(bw) >= 1)

    def _close(cur_reg):
        """收尾一个区域。

        表题（"Table I. Properties of ..."）**不进区域** —— 它留在正文里，
        由主模型翻译；图片里只有表体。两者不重复。
        """
        if not cur_reg:
            return None
        if len(cur_reg) >= min_bands and _numeric_count(cur_reg) >= min_numeric:
            return cur_reg
        return None

    for kind, gy0, gy1, bw in bands:
        table_like = _band_is_table_like(bw, x_mid)
        if table_like and cur and prev_y1 is not None and gy0 - prev_y1 <= max_gap:
            cur.append((gy0, gy1, bw))
        else:
            closed = _close(cur)
            if closed:
                regions.append(closed)
            cur = [(gy0, gy1, bw)] if table_like else []
        prev_y1 = gy1
        prev_band = (kind, gy0, gy1, bw)
    closed = _close(cur)
    if closed:
        regions.append(closed)
    return regions


def _extract_by_bands(page, words, width, height, y_split, x_mid,
                       glyphs: Optional[list] = None):
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
    intervals = _line_intervals(words)
    # 先算逐带（不合并）—— 表格区域要在**未合并**的分带上认，
    # 合并后的组会把表题、表头、表体搅在一起，表格区域就认不出来了。
    bands = []
    y = y_split
    while y < height:
        y1 = min(y + band_h, height)
        y1 = _snap_to_line_edge(y1, intervals)
        if y1 <= y + 0.01:
            y1 = min(y + band_h, height)
        bw = [w for w in words if y <= w["top"] < y1]
        if bw:
            bands.append((_band_kind(bw, width, x_mid, x_bin), y, y1, bw))
        y = y1

    # 表格区域 → 整块换成"多模态读图"标记
    table_regions = _find_table_regions(bands, x_mid)
    table_bands = set()
    for reg in table_regions:
        for gy0, gy1, _bw in reg:
            table_bands.add((round(gy0, 2), round(gy1, 2)))

    groups = []   # [kind, y0, y1]
    for kind, gy0, gy1, bw in bands:
        if (round(gy0, 2), round(gy1, 2)) in table_bands:
            # ⚠️ 必须**打断合并链**：否则前后两个同类型的带会被并成一个大组，
            # 整宽裁剪又把中间的表格行捞回正文（表格既在图里又在文字里，
            # 重复且模型照样会乱对齐）。
            groups.append(["__table__", gy0, gy1])
            continue
        if groups and groups[-1][0] == kind:
            groups[-1][2] = gy1
        else:
            groups.append([kind, gy0, gy1])

    left_parts: list[str] = []
    right_parts: list[str] = []
    units: list[list[str]] = []
    pending: list[str] = []
    table_rows: list[str] = []     # 表格正文（整块换成"多模态读图"标记）
    table_words: list = []         # 表格区域的词（用来算 bbox）
    table_mode = False
    try:
        for kind, gy0, gy1 in groups:
            if kind == "__table__":
                continue          # 表格区域：整块换成"多模态读图"标记，不走正文
            bw = [w for w in words if gy0 <= w["top"] < gy1]
            if table_mode and _band_is_table_like(bw, x_mid):
                # 表格的表体：整宽抽，行不被栏缝劈开。
                # 为什么必须整宽：跨栏表格的行被栏缝切成两半后，
                # 压在栏缝上的数字还会被切碎（"1350" → "13" + "350"），
                # 模型对不上列 → 表格数值整体错位一行
                # （实测 JACERS Table I：d31 行的 16.5 跑到了 d33 行）。
                st = _lines_to_text(
                    _crop_lines(page, (0, gy0, width, gy1)), mark_formulas=False,
                    glyphs=glyphs)
                if st.strip():
                    table_rows.append(st.strip())
                table_words.extend(bw)
                continue
            if table_rows:
                # 没有表题的表格（少见）：保留整宽文本，让模型自己排
                pending.extend(table_rows)
                table_rows, table_words = [], []
            table_mode = False
            if kind == "span":
                # 跨栏块多半是表格：表格行也长得像公式碎片（"d 31(pC/N) 90 ..."），
                # 但表格必须保持文本形态交给模型排版，所以这里不标公式。
                st = _lines_to_text(
                    _crop_lines(page, (0, gy0, width, gy1)), mark_formulas=False,
                    glyphs=glyphs)
                if st.strip():
                    pending.append(st.strip())
                table_mode = True
                continue
            lt = _lines_to_text(_crop_lines(page, (0, gy0, x_mid, gy1)),
                                glyphs=glyphs)
            rt = _lines_to_text(_crop_lines(page, (x_mid, gy0, width, gy1)),
                                glyphs=glyphs)
            if pending:
                # 跨栏块（表题之类）自己成一块，不把后面的正文拖进来 ——
                # 正文必须留在它自己的栏位里（阅读顺序不能乱）。
                unit = list(pending)
                pending = []
                units.append(unit)
            # 纯正文：两栏各自成段（后面再按「左栏读完再右栏」合并）
            if lt.strip():
                left_parts.append(lt.strip())
            if rt.strip():
                right_parts.append(rt.strip())
    except Exception:
        # 静默兜底会把"整页正文丢失"藏起来（实测 2026-10-25：某个异常导致
        # 整页退回 plain 文本，正文少了 83~148 个词，却没有任何报错）。
        # 这里把堆栈打出来，别再"看不见"。
        import traceback
        print(f"[MicroBench] 抽取分带失败（退回 plain 文本）: "
              f"{traceback.format_exc()}", file=sys.stderr)
        return [], [], []
    if table_rows:
        pending.extend(table_rows)
    if pending:
        units.append(pending)
    # 表格区域（跨栏表格）→ 整块换成"多模态读图"标记
    for reg in table_regions:
        tw = [w for (_, _, bw) in reg for w in bw]
        if not tw:
            continue
        units.append([_table_marker(tw, width, height)])
    return left_parts, right_parts, units


def _table_marker(words: list, width: float, height: float) -> str:
    """表格区域 → 「多模态读图」标记（带坐标）。"""
    x0 = min(w["x0"] for w in words)
    x1 = max(w["x1"] for w in words)
    y0 = min(w["top"] for w in words)
    y1 = max(w["bottom"] for w in words)
    pad = 3.0
    return "⟦TABLE:{:.1f},{:.1f},{:.1f},{:.1f}⟧".format(
        max(0.0, x0 - pad), max(0.0, y0 - pad),
        min(width, x1 + pad), min(height, y1 + pad))



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

def fetch_full_text(identifier: str, save_pdf_to: Optional[Path] = None,
                    glyph_resolver=None) -> dict:
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
    # 解不出的数学符号（× ≈ ≥ …）必须在**切章节之前**解析掉：
    # 标记里的下标是"本页"的，章节是跨页拼的，切完就再也对不回具体字形了；
    # 而且 ⟦G:n⟧ 是内部标记，**绝不能**原样进 prompt（模型会照抄或瞎编）。
    #
    # ⚠️ 这里只查词典、**不调模型**（recognize=False）：
    # 拉取全文要等外网下载 + PDF 解析，再叠一次多模态认字（29~90s）
    # 会让用户以为卡死。符号由服务端的后台补齐任务慢慢填进词典。
    # glyph_resolver 是旧的单页接口，保留兼容（不再走它）。
    try:
        from .glyphs import resolve_document_glyphs
        resolve_document_glyphs(pages, pdf_bytes, recognize=False)
    except Exception:
        # 解析不了也要保证内部标记不外泄
        from .glyphs import strip_markers
        for _p in pages:
            if _p.get("glyphs"):
                _p["text"] = strip_markers(_p.get("text") or "")
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