# -*- coding: utf-8 -*-
"""问答上下文装配：按需取节，而不是按顺序取前 N 节（2026-11-15）。

## 为什么要有这个模块

三个入口原本都按「**论文顺序取前 N**」拼上下文：

| 位置 | 原来的写法 | 后果 |
|---|---|---|
| `app.py` `api_explain_stream` | `if total + len(chunk) > 24000: break` | 取到超预算就停 → 后面全部丢掉 |
| `index.html` `askPaperQuestion` | `_readerState.sections.slice(0, 20)` | 20 节之外**静默**丢弃 |
| `index.html` `_buildScopeContextRaw` full 模式 | `sections.slice(0, 8)` | 划词答疑的 full 模式只取前 8 节 |

**实测（长论文 20 节 / 每节 3000 字 / 问第 18 节）**：发出去的 prompt 里
**只有前 6 节**，第 18 节根本不在 → 模型只能回答「我的上下文里没有这一节，
我只能猜」。把同一节挪到第 3 位，它就出现了。

根因不是模型笨，是**截断策略错了**：应该「按用户问的那一节取」，
而不是「按论文顺序取前 N」。位置决定生死 —— 这正是用户报的现象。

## 装配策略（`pick_context`）

1. **焦点节永远保留** —— 用户当前所在 / 正在问的那一节（前端显式给）。
   指代型提问（「总结**这一节**」「这一节讲了什么」）没有别的线索可依，
   焦点节是唯一可靠的锚。
2. **问题命中的节优先** —— 问题里出现「第 N 节 / Chapter N / Section IV」
   或标题关键词时，命中它。
3. **其余按原顺序补** —— 预算还有就继续放。
4. **单节超长 → 头尾各取一半** —— 保住开头（背景/问题设定）与结尾（结论）。
   原来的 `[:6000]` 是**头截断**，长节的结论会被切掉。
"""
from __future__ import annotations

import re

# 总字符预算（上下文窗口够用；M3 系列的输入窗口远大于此）
QA_CONTEXT_BUDGET = 32000
# 单节最多给多少字（超出则头尾各取一半）
QA_SECTION_CHAR_LIMIT = 8000
# 焦点节最多给多少字（用户明确要的那一节，值得多给）
QA_FOCUS_CHAR_LIMIT = 16000


# ---------------------------------------------------------------------------
# 序号识别：阿拉伯 / 中文 / 罗马
# ---------------------------------------------------------------------------

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

# 明确指代「第几节」的说法 —— 只在这些语境里取数字，避免把「3 个实验」误判成节号
_ORDINAL_PATTERNS = (
    re.compile(r"第\s*([0-9]{1,3}|[一二三四五六七八九十百]{1,4})\s*[节章节章篇部]"),
    re.compile(r"\b(?:section|sec\.?|chapter|part)\s*([0-9]{1,3}|[IVXLC]{1,6})\b", re.I),
)

_STOPWORDS = {
    "这", "那", "这个", "那个", "这一", "那一", "该", "本", "其", "它", "他",
    "是", "的", "了", "和", "与", "或", "在", "对", "把", "被", "给", "从",
    "有", "会", "能", "要", "说", "讲", "问", "请", "帮", "我", "你", "们",
    "什么", "怎么", "如何", "哪些", "哪个", "为什么", "可以", "需要", "一下",
    "the", "a", "an", "of", "in", "on", "for", "to", "is", "are", "and", "or",
    "this", "that", "it", "please", "summarize", "section",
}


def _cn_to_int(token: str) -> int | None:
    """中文数字 → int（支持 一 ~ 九十九）。"""
    token = token.strip()
    if not token:
        return None
    if all(c in _CN_DIGITS for c in token):
        val = 0
        for c in token:
            val = val * 10 + _CN_DIGITS[c]
        return val
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return None


def _roman_to_int(token: str) -> int | None:
    """罗马数字 → int（I / II / III / IV / V ...）。"""
    token = token.strip().upper()
    if not token or any(c not in _ROMAN_VALUES for c in token):
        return None
    total = 0
    prev = 0
    for c in reversed(token):
        val = _ROMAN_VALUES[c]
        total = total - val if val < prev else total + val
        prev = max(prev, val)
    return total or None


def _token_to_ordinal(token: str) -> int | None:
    """把一个序号 token 转成 int（阿拉伯 / 中文 / 罗马都试）。"""
    token = token.strip().strip(".,;:)")
    if not token:
        return None
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None
    return _cn_to_int(token) or _roman_to_int(token)


def extract_ordinals(question: str) -> set[int]:
    """从问题里抽「用户指的是第几节」（只认明确的节号指代）。"""
    found: set[int] = set()
    if not question:
        return found
    for pat in _ORDINAL_PATTERNS:
        for m in pat.finditer(question):
            val = _token_to_ordinal(m.group(1))
            if val and val > 0:
                found.add(val)
    return found


def _title_keywords(title: str) -> list[str]:
    """把标题切成可与问题比对的小写词（中文按 2-gram，英文按 word）。"""
    title = (title or "").lower()
    words = set(re.findall(r"[a-z][a-z\-]{2,}", title))
    for seg in re.findall(r"[一-鿿]+", title):
        if len(seg) == 1:
            words.add(seg)
        else:
            for i in range(len(seg) - 1):
                words.add(seg[i:i + 2])
    return [w for w in words if w not in _STOPWORDS]


def _question_keywords(question: str) -> list[str]:
    """问题里的实词（去停用词）。"""
    q = (question or "").lower()
    words = set(re.findall(r"[a-z][a-z\-]{2,}", q))
    for seg in re.findall(r"[一-鿿]+", q):
        if len(seg) == 1:
            words.add(seg)
        else:
            for i in range(len(seg) - 1):
                words.add(seg[i:i + 2])
    return [w for w in words if w not in _STOPWORDS]


def _section_ordinal(section: dict) -> int | None:
    """章节自身的序号：优先 `number` 字段，其次标题里的罗马/阿拉伯/中文序号。"""
    raw = str(section.get("number") or "").strip()
    val = _token_to_ordinal(raw)
    if val:
        return val
    title = str(section.get("title") or "")
    # 兼容三种写法："IV. RESULTS" / "4. Method" / "第18节 标题"
    m = re.match(
        r"^\s*(?:第\s*)?([IVXLC]{1,6}|\d{1,3}|[一二三四五六七八九十]{1,4})\s*[.、章节节\s]",
        title, re.I)
    if m:
        return _token_to_ordinal(m.group(1))
    return None


def score_sections(sections: list[dict], question: str) -> list[float]:
    """给每个章节打「与问题的相关度」得分。"""
    ordinals = extract_ordinals(question)
    qwords = _question_keywords(question)
    scores: list[float] = []
    for pos, sec in enumerate(sections):
        score = 0.0
        sec_ord = _section_ordinal(sec)
        if sec_ord and sec_ord in ordinals:
            score += 100.0
        elif (pos + 1) in ordinals:
            # 兜底：用户说的「第 N 节」也可能就是**界面上第 N 个章节**
            # （论文自带的编号常常和列表顺序对不上，比如前面多一段 abstract）
            score += 60.0
        title = str(sec.get("title") or "")
        for w in _title_keywords(title):
            if w in qwords:
                score += 10.0
        body = str(sec.get("text") or "")[:500].lower()
        for w in qwords:
            if w in body:
                score += 1.0
        scores.append(score)
    return scores


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------

def _head_tail(text: str, limit: int) -> str:
    """超长文本 → 头尾各取一半（保住开头与结论），中间用省略号连接。"""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + "\n\n……（中间内容已省略）……\n\n" + text[-tail:]


def _section_block(sec: dict, text: str) -> str:
    title = str(sec.get("title") or sec.get("section") or "?")
    key = str(sec.get("section") or "")
    head = f"[{key}] {title}" if key else f"[{title}]"
    return f"{head}\n{text}"


def _section_label(sec: dict, idx: int) -> str:
    """章节的显示名（给「未装入的节」提示用，比下标好认）。"""
    title = str(sec.get("title") or "").strip()
    num = str(sec.get("number") or "").strip()
    if num and title:
        return f"{num} {title}"[:60]
    return (title or f"第 {idx + 1} 节")[:60]


def pick_context(
    sections: list[dict],
    question: str = "",
    focus_index: int | None = None,
    focus_text: str = "",
    focus_title: str = "",
    budget: int = QA_CONTEXT_BUDGET,
) -> tuple[str, dict]:
    """按需装配问答上下文。

    返回 `(context_text, meta)`。`meta` 里带 `included` / `dropped` /
    `focus_hit` / `labels`，方便调用方在界面上说清楚「给了哪些节、漏了哪些节」。
    """
    sections = [s for s in (sections or []) if isinstance(s, dict)]
    if not sections:
        return "", {"included": [], "dropped": [], "focus_hit": False,
                    "labels": {}, "chars": 0, "budget": budget}

    scores = score_sections(sections, question)
    picked: list[tuple[int, str]] = []   # (原始下标, 正文)
    used = 0
    included: list[int] = []
    dropped: list[int] = []
    labels = {i: _section_label(s, i) for i, s in enumerate(sections)}

    def _take(idx: int, limit: int) -> bool:
        nonlocal used
        sec = sections[idx]
        text = str(sec.get("text") or "")
        block = _section_block(sec, _head_tail(text, limit))
        if used + len(block) > budget:
            # 这一节塞不下了 → 给一个"塞得下"的份额（至少 800 字），
            # 塞不下就整节跳过（宁可少一节，也不要半节糊弄）
            room = budget - used
            if room < 800:
                return False
            block = _section_block(sec, _head_tail(text, room))
        picked.append((idx, block))
        used += len(block)
        included.append(idx)
        return True

    # --- 1) 焦点节：用户当前所在 / 正在问的那一节，永远优先 -------------
    focus_hit = False
    if focus_index is not None and 0 <= focus_index < len(sections):
        if _take(focus_index, QA_FOCUS_CHAR_LIMIT):
            focus_hit = True
    elif focus_text.strip():
        # 划词答疑：前端直接把焦点节正文给了（独立字段，不受上下文截断影响）
        block = f"[{focus_title or '当前小节'}]\n{_head_tail(focus_text.strip(), QA_FOCUS_CHAR_LIMIT)}"
        picked.append((-1, block))
        used += len(block)
        focus_hit = True

    # --- 2) 问题命中的节（按得分降序） -----------------------------------
    order = sorted(range(len(sections)), key=lambda i: (-scores[i], i))
    for idx in order:
        if idx in included:
            continue
        if scores[idx] <= 0:
            break
        if not _take(idx, QA_SECTION_CHAR_LIMIT):
            break

    # --- 3) 其余按原顺序补 ----------------------------------------------
    for idx in range(len(sections)):
        if idx in included:
            continue
        if not _take(idx, QA_SECTION_CHAR_LIMIT):
            break

    dropped = [i for i in range(len(sections)) if i not in included]
    ctx = "\n\n---\n\n".join(block for _, block in picked)
    return ctx, {
        "included": included,
        "dropped": dropped,
        "focus_hit": focus_hit,
        "labels": labels,
        "chars": len(ctx),
        "budget": budget,
    }
