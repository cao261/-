"""
Translation utilities for MicroBench.

Provides:
- chunk_section_text(): split long sections into LLM-friendly chunks at
  paragraph boundaries, with overlap to preserve context continuity.
- merge_translation_parts(): stitch chunk translations back together without
  the duplicated sentences that the overlap window would otherwise cause.
- sanitize_translation(): repair LaTeX the model over-escaped, and normalize
  inline/display math delimiters into the $...$ / $$...$$ the frontend renders.
- TranslationCache: in-memory LRU cache for translations, keyed by section
  text hash + section_key + model name. Skips redundant LLM calls when the
  user re-clicks translate or reopens a paper.
"""

import hashlib
import re
from collections import OrderedDict
from difflib import SequenceMatcher
from threading import Lock
from typing import Optional


# ---------------------------------------------------------------------------
# Section chunking
# ---------------------------------------------------------------------------

# Soft target chunk size (chars). Smaller chunks → shorter LLM round-trip
# time → less likely to hit upstream load-balancer timeouts (a 60s+ M2.7
# thinking response is exactly when connections get killed by intermediaries).
CHUNK_TARGET_SIZE = 5000

# Hard ceiling for a single chunk — never go above this even for paragraphs.
CHUNK_HARD_MAX = 7000

# Overlap between consecutive chunks so the LLM has continuity context.
CHUNK_OVERLAP = 300


_PARAGRAPH_RE = re.compile(r"\n\s*\n")


def chunk_section_text(text: str,
                       target: int = CHUNK_TARGET_SIZE,
                       hard_max: int = CHUNK_HARD_MAX,
                       overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split a section into chunks at paragraph boundaries.

    Strategy:
    1. If text fits in `target`, return [text].
    2. Otherwise, split on double-newlines (paragraph boundaries).
    3. Greedily accumulate paragraphs into chunks of ~`target` chars.
    4. Add `overlap` chars from the previous chunk's tail to each chunk so
       the LLM keeps continuity (e.g., pronoun references, equations).
    5. If a single paragraph exceeds `hard_max`, split it on sentence ends.

    Returns a list of non-empty chunks (in order).
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]

    paragraphs = [p.strip() for p in _PARAGRAPH_RE.split(text) if p.strip()]
    if not paragraphs:
        return [text[:hard_max]]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        # If this paragraph alone exceeds hard_max, flush current then split para
        if para_len > hard_max:
            if current:
                chunks.append("\n\n".join(current))
                current, current_len = [], 0
            chunks.extend(_split_oversized(para, hard_max))
            continue

        # Adding this paragraph would exceed target → flush & start new chunk
        if current and current_len + para_len + 2 > target:
            chunks.append("\n\n".join(current))
            # Start next chunk with overlap from the previous one
            overlap_text = _tail_overlap("\n\n".join(current), overlap)
            current = [overlap_text, para] if overlap_text else [para]
            current_len = sum(len(p) for p in current)
        else:
            current.append(para)
            current_len += para_len

    if current:
        chunks.append("\n\n".join(current))

    return [c.strip() for c in chunks if c.strip()]


def _split_oversized(para: str, hard_max: int) -> list[str]:
    """
    Split a single paragraph that exceeds hard_max.

    Order matters: try hard line breaks first (PDF text keeps the paper's
    line layout, so a line break is the most natural cut), then fall back to
    sentence ends. Cutting mid-line makes the model stitch unrelated
    sentences together and produces garbled output.
    """
    # 1) Prefer line breaks — PDF 抽取的文本保留了论文的换行
    lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
    if len(lines) > 1:
        groups: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for ln in lines:
            if cur and cur_len + len(ln) + 1 > hard_max:
                groups.append("\n".join(cur))
                cur, cur_len = [], 0
            cur.append(ln)
            cur_len += len(ln) + 1
        if cur:
            groups.append("\n".join(cur))
        # 2) 仍然超长的组按句子切
        out: list[str] = []
        for g in groups:
            if len(g) <= hard_max:
                out.append(g)
            else:
                out.extend(_split_on_sentences(g, hard_max))
        return out

    return _split_on_sentences(para, hard_max)


def _split_on_sentences(para: str, hard_max: int) -> list[str]:
    """Split a paragraph on sentence ends (no line breaks available)."""
    sentences = re.split(r"(?<=[.!?。！？])\s+", para)
    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        if cur and cur_len + len(s) > hard_max:
            out.append(" ".join(cur))
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += len(s) + 1
    if cur:
        out.append(" ".join(cur))
    return [o for o in out if o.strip()]


def _tail_overlap(text: str, overlap: int) -> str:
    """
    Return the tail of `text` (~`overlap` chars) as complete sentences.

    Cutting the overlap mid-sentence makes the model translate a fragment
    twice, which shows up in the final page as a duplicated half-sentence.
    """
    if len(text) <= overlap:
        return text
    tail = text[-overlap:]
    # Round to a word boundary so we don't cut mid-word
    space = tail.find(" ")
    if space > 0 and space < overlap // 2:
        tail = tail[space + 1:]
    # Prefer starting at a sentence boundary when one is close enough
    m = list(re.finditer(r"(?<=[.!?。！？])\s+", tail))
    if m and m[0].start() < overlap * 0.4:
        tail = tail[m[0].end():]
    return tail.strip()


# ---------------------------------------------------------------------------
# Merging chunk translations (removing overlap duplication)
# ---------------------------------------------------------------------------

# 判定重复的最小字符数。太短容易误判（中文单字重复率很高）。
_DUP_MIN_CHARS = 16
_DUP_WINDOW = 600

# 段落级重复判定：相似度阈值 + 最多比对几段。
_DUP_PARA_RATIO = 0.72
_DUP_PARA_MAX = 4


def _split_paragraphs(text: str) -> list[tuple[str, str]]:
    """Split into (content, separator) pairs so we can drop leading ones."""
    pieces = re.split(r"(\n\s*\n)", text)
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(pieces):
        content = pieces[i]
        sep = pieces[i + 1] if i + 1 < len(pieces) else ""
        if content.strip():
            out.append((content, sep))
        i += 2
    return out


def _duplicate_paragraph_count(prev: str, nxt: str) -> int:
    """
    How many leading paragraphs of `nxt` repeat the tail of `prev`.

    The overlap window makes the model translate the same source region
    twice, but the two renderings differ slightly ("获批" vs "批准通过",
    "D. Johnson" vs "Johnson"), so an exact character match misses it.
    Comparing whole paragraphs with a similarity ratio catches it.
    """
    prev_ps = [p.strip() for p in prev.split("\n\n") if p.strip()]
    next_ps = [p.strip() for p in nxt.split("\n\n") if p.strip()]
    if not prev_ps or not next_ps:
        return 0

    for k in range(min(_DUP_PARA_MAX, len(prev_ps), len(next_ps)), 0, -1):
        a = " ".join(prev_ps[-k:])
        for j in range(min(_DUP_PARA_MAX, len(next_ps)), 0, -1):
            b = " ".join(next_ps[:j])
            if not a or not b:
                continue
            if SequenceMatcher(None, a, b).ratio() >= _DUP_PARA_RATIO:
                return j
    return 0


def _duplicate_prefix_len(prev: str, nxt: str,
                          window: int = _DUP_WINDOW) -> int:
    """
    Length of `nxt`'s prefix that repeats the tail of `prev`.

    Character-level fallback for overlaps that land mid-paragraph.
    """
    if not prev or not nxt:
        return 0
    tail = prev[-window:]
    head = nxt[:window]
    if not tail or not head:
        return 0
    sm = SequenceMatcher(None, tail, head, autojunk=False)
    m = sm.find_longest_match(0, len(tail), 0, len(head))
    if m.size < _DUP_MIN_CHARS:
        return 0
    # 命中必须贴着 head 的开头，且几乎延伸到 tail 的结尾
    if m.b > 40 or m.a + m.size < len(tail) - 60:
        return 0
    return m.b + m.size


def merge_translation_parts(parts: list[str]) -> str:
    """
    Join chunk translations, dropping the text the overlap made redundant.

    Without this, a page split into 2+ chunks shows the overlap paragraph
    twice — the reader sees the same passage appear again out of place,
    which looks exactly like "翻译翻了一半又跳回去".
    """
    merged = ""
    for raw in parts:
        # 先归一化换行符：模型常返回 CRLF，而下面的段落切分用的是 "\n\n"，
        # CRLF 下切不开，段落级去重会完全失效。
        part = (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not part:
            continue
        if not merged:
            merged = part
            continue

        # 先按段落去重（能扛住措辞差异），失败再退回字符级
        dup_paras = _duplicate_paragraph_count(merged, part)
        if dup_paras:
            blocks = _split_paragraphs(part)
            part = "".join(c + s for c, s in blocks[dup_paras:]).strip()
        else:
            dup = _duplicate_prefix_len(merged, part)
            if dup and dup < len(part):
                part = part[dup:].lstrip()

        if not part:
            continue
        merged = f"{merged}\n\n{part}"
    return merged.strip()


# ---------------------------------------------------------------------------
# Translation output sanitizing
# ---------------------------------------------------------------------------

# 模型偶尔把 LaTeX 写成"过度转义"的形式：
#     \alpha_{\mathrm{ME}}      → \/alpha_\{\mathrm{ME}\}
#     \mathrm{V/cm\text{-}Oe}  → \mathrm{V\/cm\text{-}Oe}
# KaTeX 解析不了这种写法，整条公式会被原样用红字显示出来 —— 用户在页面上
# 看到的就是一串 \/ 和 \{ 的乱码。这里把它们还原成合法 LaTeX。
_MATH_SPAN_RE = re.compile(r"\$\$(.+?)\$\$|\$([^\$\n]+?)\$", re.DOTALL)

# 常见的 LaTeX 命令。用来判断"这串东西是公式"以及"被转义坏掉的命令"，
# 而不是去乱猜正文里的反斜杠。
_LATEX_COMMANDS = {
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
    "iota", "kappa", "lambda", "mu", "nu", "xi", "rho", "sigma", "tau",
    "upsilon", "phi", "chi", "psi", "omega", "varphi", "vartheta",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Sigma", "Phi", "Psi", "Omega",
    "mathrm", "mathbf", "mathit", "mathcal", "mathbb", "mathsf", "boldsymbol",
    "text", "textbf", "textit", "operatorname", "overline", "underline",
    "frac", "dfrac", "tfrac", "sqrt", "sum", "prod", "int", "oint", "lim",
    "log", "ln", "exp", "sin", "cos", "tan", "cot", "sec", "csc", "arcsin",
    "cdot", "cdots", "ldots", "times", "div", "approx", "neq", "equiv",
    "leq", "geq", "ll", "gg", "subset", "subseteq", "supset", "supseteq",
    "cup", "cap", "in", "notin", "ni", "pm", "mp", "rightarrow", "leftarrow",
    "leftrightarrow", "Rightarrow", "Leftarrow", "to", "infty", "partial",
    "nabla", "forall", "exists", "propto", "sim", "cong", "doteq",
    "left", "right", "big", "Big", "bigg", "Bigg", "quad", "qquad",
    "begin", "end", "mathrm", "circ", "degree", "prime", "hbar", "ell",
}

# "\/alpha" → "\alpha"：斜杠紧跟一个已知 LaTeX 命令时，它是坏掉的反斜杠。
# 注意不能用 \b 做词边界："alpha_" 后面跟的是下划线（属于单词字符），
# \b 在那里不成立，会导致整条规则永远匹配不上。
_LATEX_CMD_ALT = "|".join(sorted(_LATEX_COMMANDS, key=len, reverse=True))
_MANGLED_CMD_RE = re.compile(r"\\/(?=(?:%s)(?![A-Za-z]))" % _LATEX_CMD_ALT)


def _fix_overescaped_latex(tex: str) -> str:
    """Repair over-escaped LaTeX inside a single math expression."""
    if "\\/" not in tex and "\\{" not in tex and "\\}" not in tex:
        return tex

    # 1) 斜杠：LaTeX 里 "/" 不需要转义，"\/" 是模型过度防御的产物。
    #    但 "\/alpha" 说明本该是反斜杠（命令），"\/cm" 才是普通斜杠。
    tex = _MANGLED_CMD_RE.sub("\\\\", tex)
    tex = tex.replace("\\/", "/")

    # 2) 花括号：只在下标/上标后面的组边界上还原。
    #    "_\{" 一定是"要开一个组"，不是字面量花括号；"{\}" 配套闭合。
    #    其它位置的 "\{" 是合法的字面花括号（\text{\{}），保持不动。
    tex = re.sub(r"([_^])\\\{", r"\1{", tex)
    if re.search(r"([_^])\{", tex) and "\\}" in tex:
        tex = tex.replace("\\}", "}")
    return tex


def _wrap_bare_latex(text: str) -> str:
    """
    Wrap LaTeX fragments that the model left outside $...$ delimiters.

    The model sometimes emits a formula as plain prose — "\\alpha_{ME}" with
    no dollar signs. The frontend's KaTeX pipeline only picks up $...$, so
    the raw LaTeX ends up visible as text. Wrapping those fragments makes the
    formula render instead of leaking "\\alpha_{\\mathrm{ME}}" onto the page.
    """
    if "\\" not in text:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1].isalpha():
            j = i + 1
            while j < n and text[j].isalpha():
                j += 1
            cmd = text[i + 1:j]          # 命令名不含反斜杠
            if cmd not in _LATEX_COMMANDS:
                out.append(ch)
                i += 1
                continue
            # 吃掉紧随其后的 {...} / _{...} / ^{...}
            while True:
                k = j
                while k < n and text[k] in " \t":
                    k += 1
                if k < n and text[k] in "_^":
                    k += 1
                    while k < n and text[k] in " \t":
                        k += 1
                if k < n and text[k] == "{":
                    depth = 0
                    m = k
                    while m < n:
                        if text[m] == "{":
                            depth += 1
                        elif text[m] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        m += 1
                    if depth != 0:
                        break  # 括号没闭合，别吞
                    j = m + 1
                    continue
                break
            out.append("$" + text[i:j] + "$")
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def find_untranslated_runs(text: str, min_words: int = 8) -> list[str]:
    """
    Find stretches of English that the model left untranslated.

    This is the safety net for "有的翻译有的不翻译": the model sometimes
    silently drops a sentence (typically a garbled one from the PDF text
    layer). Prompting alone cannot prevent that — the only reliable fix is
    to CHECK the output and retry when whole passages came back in English.

    A run is only reported when it contains ordinary lower-case words.
    All-capitalised runs (author lists, institution names, citation
    abbreviations) are legitimate English in a Chinese translation and are
    ignored.
    """
    if not text:
        return []
    runs: list[str] = []
    for m in re.finditer(r"[A-Za-z][A-Za-z\-']*(?:[ \t]+[A-Za-z][A-Za-z\-']*){%d,}"
                         % (min_words - 1), text):
        frag = m.group(0)
        words = re.findall(r"[A-Za-z][A-Za-z\-']*", frag)
        if len(words) < min_words:
            continue
        # 全是首字母大写 / 短缩写 → 人名、机构名、引用缩写，保留英文是合理的
        if all(w[0].isupper() or len(w) <= 2 for w in words):
            continue
        # 邮箱 / DOI / 图表引用 / 链接
        if re.search(r"@|Fig\.|Table\s|Eq\.|doi|https?://|arXiv", frag, re.I):
            continue
        runs.append(frag.strip())
    return runs


def _fix_odd_dollars(text: str) -> str:
    """
    Make sure every line has an even number of "$".

    A lone "$" is almost always a price in a publisher's copyright line
    (".../35/$23.00", "1531-7331/10/0804-0153$20.00"). The frontend pairs
    "$...$" with a non-greedy, single-line regex, so a stray "$" is usually
    harmless — but if a second one shows up on the same line the two get
    paired and a chunk of prose is swallowed into a math span.

    Turning the stray half-width "$" into a full-width "＄" keeps the text
    readable and makes the pairing unambiguous.
    """
    if "$" not in text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.count("$") % 2 == 1:
            # 落单的 $：替换最后一个（价格通常在数字后面）
            pos = line.rfind("$")
            lines[i] = line[:pos] + "＄" + line[pos + 1:]
    return "\n".join(lines)


def sanitize_translation(text: str) -> str:
    """
    Clean up raw model output before it reaches the frontend.

    Four jobs:
      1. Drop any leaked <think> reasoning block (defence in depth — llm.py
         already strips them, but a truncated one can slip through).
      2. Normalize inline/display math delimiters into $...$ / $$...$$, because
         the frontend's KaTeX pipeline only understands the dollar form.
         Models switch between the two notations freely.
      3. Repair over-escaped LaTeX (see _fix_overescaped_latex).
      4. Wrap LaTeX the model left as bare prose so KaTeX can render it
         (see _wrap_bare_latex).
    """
    if not text:
        return ""

    # 0) 统一换行符。模型经常返回 CRLF，而前端按 "\n{2,}" 切段落——
    #    "\r\n\r\n" 切不开，整篇译文会挤成一个段落、到处冒出 <br>，
    #    服务端的段落级去重也会跟着失效。必须先归一化。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # 1) 泄漏的思考块
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*\Z", "", text, flags=re.DOTALL)
    text = text.replace("</think>", "").replace("<think>", "")

    # 2) 定界符归一化：\( ... \) → $ ... $，\[ ... \] → $$ ... $$
    text = re.sub(r"\\\[(.+?)\\\]", lambda m: "$$" + m.group(1) + "$$",
                  text, flags=re.DOTALL)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: "$" + m.group(1) + "$",
                  text, flags=re.DOTALL)

    # 3) 修 LaTeX
    def _repair(m: re.Match) -> str:
        body = m.group(1) if m.group(1) is not None else m.group(2)
        fixed = _fix_overescaped_latex(body)
        if m.group(1) is not None:
            return "$$" + fixed + "$$"
        return "$" + fixed + "$"

    pieces: list[str] = []
    last = 0
    for m in _MATH_SPAN_RE.finditer(text):
        between = text[last:m.start()]
        if between:
            pieces.append(_wrap_bare_latex(_fix_overescaped_latex(between)))
        body = m.group(1) if m.group(1) is not None else m.group(2)
        fixed = _fix_overescaped_latex(body)
        pieces.append(("$$" + fixed + "$$") if m.group(1) is not None
                       else ("$" + fixed + "$"))
        last = m.end()
    tail = text[last:]
    if tail:
        pieces.append(_wrap_bare_latex(_fix_overescaped_latex(tail)))

    # 4) 落单的 $ 换成全角 ＄（多半是版权行的页价）
    out = _fix_odd_dollars("".join(pieces)).strip()

    # 5) 公式标记
    #    - 带坐标的 ⟦FORMULA:x0,y0,x1,y1⟧：**保留**。前端拿这四个坐标去 PDF
    #      原页裁一张图，贴进译文里 —— 和论文里长得一模一样，绝不会把分子
    #      分母装反。模型偶尔在坐标里加空格或抄错位数，这里修一次；
    #      修不好（数字不成比例）就退回文字提示，绝不放行坏坐标 ——
    #      那会裁到页面上无关的地方去。
    #    - 裸的 ⟦FORMULA⟧：没有坐标，裁不了图 → 换成文字提示。
    out = _normalize_formula_markers(out)
    return out


# 公式标记（带坐标）—— 前端靠它去 PDF 原页裁图。
# 说明：实际归一化用一个正则一次搞定（⟦FORMULA:...⟧ 里不是 4 个数就退回
# 文字提示），这里只留常量。
FORMULA_TEXT_FALLBACK = (
    "【公式未能完整提取：PDF 排版导致分子/分母与上下标被打散，"
    "请对照左侧 PDF 原文】"
)


def _normalize_formula_markers(text: str) -> str:
    """修好带坐标的公式标记；修不好的换成文字提示。"""

    def repl(m: re.Match) -> str:
        raw = m.group(1)
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 4:
            return FORMULA_TEXT_FALLBACK
        try:
            x0, y0, x1, y1 = (float(p) for p in parts)
        except ValueError:
            return FORMULA_TEXT_FALLBACK
        if x1 <= x0 or y1 <= y0:
            return FORMULA_TEXT_FALLBACK
        return "⟦FORMULA:{:.1f},{:.1f},{:.1f},{:.1f}⟧".format(x0, y0, x1, y1)

    out = re.sub(r"⟦FORMULA:([^⟧]*)⟧", repl, text)
    out = out.replace("⟦FORMULA⟧", FORMULA_TEXT_FALLBACK)
    return out


# ---------------------------------------------------------------------------
# Translation cache
# ---------------------------------------------------------------------------

DEFAULT_CACHE_SIZE = 64


class TranslationCache:
    """
    Simple thread-safe LRU cache for translation results.

    Key: (section_key, text_hash, model_name)
    Value: translation string + metadata (char_count, chunked flag)

    LRU eviction keeps the cache bounded; entries are evicted when full.
    Thread safety via a single Lock — the cache is on a single FastAPI worker
    so contention is negligible.
    """

    def __init__(self, max_size: int = DEFAULT_CACHE_SIZE):
        self._max_size = max_size
        self._store: "OrderedDict[tuple, dict]" = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _key_for(section_key: str, text: str, model: str) -> tuple:
        text_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        return (section_key or "", text_hash, model or "")

    def get(self, section_key: str, text: str, model: str) -> Optional[dict]:
        key = self._key_for(section_key, text, model)
        with self._lock:
            if key not in self._store:
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            # Return a copy so callers can't mutate the cache
            return dict(self._store[key])

    def put(self, section_key: str, text: str, model: str, value: dict) -> None:
        key = self._key_for(section_key, text, model)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = dict(value)
            else:
                self._store[key] = dict(value)
                if len(self._store) > self._max_size:
                    self._store.popitem(last=False)

    def stats(self) -> dict:
        with self._lock:
            return {
                "size": len(self._store),
                "max_size": self._max_size,
                "hits": self.hits,
                "misses": self.misses,
            }

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0


# Module-level singleton — initialized by app.py at startup
_cache: Optional[TranslationCache] = None


def get_cache() -> TranslationCache:
    """Get (or lazily create) the global translation cache."""
    global _cache
    if _cache is None:
        _cache = TranslationCache()
    return _cache


def reset_cache() -> None:
    """Reset the global cache (mainly for tests)."""
    global _cache
    _cache = TranslationCache()


# ---------------------------------------------------------------------------
# Cross-section RAG helper
# ---------------------------------------------------------------------------

# How many chars of each section to include in cross-section context.
# Stay well under model context window even for 8 sections.
PER_SECTION_BUDGET = 6000


def build_cross_section_context(sections: list[dict],
                                question: str,
                                per_section_budget: int = PER_SECTION_BUDGET) -> str:
    """
    Build a multi-section context block for cross-chapter QA.

    Each section is labeled with [section_key] so the LLM can reference it.
    Sections longer than `per_section_budget` are truncated (kept head + tail)
    so the total stays bounded.
    """
    parts = [f"用户问题: {question}\n\n--- 论文章节内容 (按章节标注) ---\n"]
    for s in sections:
        key = (s.get("section") or "?").upper()
        title = s.get("title") or key
        text = (s.get("text") or "").strip()
        if len(text) > per_section_budget:
            half = per_section_budget // 2
            text = text[:half] + "\n\n... [中段省略] ...\n\n" + text[-half:]
        parts.append(f"\n[{key}] {title}\n{text}\n")
    return "".join(parts)


__all__ = [
    "CHUNK_TARGET_SIZE",
    "CHUNK_HARD_MAX",
    "CHUNK_OVERLAP",
    "PER_SECTION_BUDGET",
    "chunk_section_text",
    "merge_translation_parts",
    "sanitize_translation",
    "find_untranslated_runs",
    "TranslationCache",
    "get_cache",
    "reset_cache",
    "build_cross_section_context",
]