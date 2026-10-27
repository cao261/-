# -*- coding: utf-8 -*-
"""`paper_text._is_mathish_line` 的判据护栏（纯函数，直接单测）。

**为什么要有这份文件**

这个函数的判据是 §4.12「正文优先」的核心：判错 = **正文整段从译文里消失**，
而且用户只会看到"译文少了一大截"，日志里什么都没有。

历史上栽过三次（AGENTS.md §4.12）：
- 「有关系符号就是公式」→ 把 `f(ω) = … Substituting the inverse` 整行吞成公式图
- 「≥4 字母词就算正文」→ 把 `eq (3)` 判成正文
- 「≥2 个长词」→ 把 `are assumed: α = 0.05 …` 判成公式

这些判据全是**纯函数**（入参一行字符串，出参 bool），却只能靠真 PDF 端到端
才能测到 → 每次调整判据都只能靠肉眼比对全库。
这里把它变成可以直接单测的东西。

⚠ 这些用例是**判据的规格说明**。改 `_is_mathish_line` 的行为时，
   要么让这里保持绿，要么**同时**改这里并说明理由。
"""
from __future__ import annotations

import pytest

from microbench.paper_text import _is_mathish_line


# ---------------------------------------------------------------------------
# 必须是「公式」的
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    # 标定数据（生产渲染代码实测的碎片形态）
    "eeﬀ ¼ 33 33 (3)",                    # 1 个词状 token → 公式
    "E;31 nsp ð1 (cid:4) …",               # 同上
    # 纯符号行：公式的积分上下限、乘号残留
    "− ×",
    "Z−∞",
    "∞",
    "× −",
    # 带未解出符号
    "α = [?]",
    "nsp [?]",
    # §4.12 明确：eq (3) **必须**判成公式（公式符号本身也会是长词，
    # 按"有没有长词"判会把它错判成正文）
    "eq (3)",
])
def test_mathish_lines_are_recognised(line):
    assert _is_mathish_line(line) is True, f"没认出公式行: {line!r}"


# ---------------------------------------------------------------------------
# 必须是「正文」的（判错 = 正文消失）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    # §4.12 的两个原始事故
    "f(ω) = −∞ f(t) exp ( − iωt) dt. Substituting the inverse",
    "are assumed: α = 0.05, d = 2 nm, and λ = 1.2 μm",
    # 正常正文
    "The hysteresis loop was measured at room temperature.",
    "Figure 4 shows the normalized magnetization as a function of field.",
])
def test_prose_lines_are_recognised(line):
    assert _is_mathish_line(line) is False, f"正文被误判成公式: {line!r}"


# ---------------------------------------------------------------------------
# 2026-10-27 新增：孤立的短词**不是**公式
# ---------------------------------------------------------------------------
#
# 背景：`_is_mathish_line` 原来有这一条
#
#     letters = re.findall(r"[A-Za-z]", s)
#     if len(letters) <= 2 and len(s) <= 12:
#         return True
#
# → "in" / "of" / "as" / "to" 这些被栏缝裁剩下的残行、或者本来就只有
#   两个字母的词，全部满足「≤2 个字母 且 ≤12 字符」→ 判成公式行。
#
# 后果：`_mark_broken_formulas` 按「连续的公式碎片行」成段判定 —— 一个
# 孤立的 "in" 紧挨着真正带 [?] 的公式行时，会被并进那个段 → 整段替换成
# ⟦FORMULA⟧ → **这句正文从译文里消失**。
#
# 与 §4.12「正文优先」同源的漏网。

@pytest.mark.parametrize("word", ["in", "of", "as", "to", "is", "at", "on", "we"])
def test_isolated_short_words_are_not_mathish(word):
    assert _is_mathish_line(word) is False, (
        f"孤立的短词 {word!r} 被判成公式行 → 紧挨公式行时会被并进 ⟦FORMULA⟧ 段，"
        "正文整句消失"
    )


def test_two_letter_word_with_punctuation_is_not_mathish():
    """带标点的短词行（裁剪残行常见形态）也不是公式。"""
    assert _is_mathish_line("in.") is False
    assert _is_mathish_line("of,") is False


def test_short_words_line_is_not_mathish():
    """一整行都是短词（"in of as"）—— 这是正文残行，不是公式。"""
    assert _is_mathish_line("in of as") is False


def test_genuine_two_symbol_line_is_still_mathish():
    """别把真公式也排除掉：纯符号行仍然要判成公式。"""
    assert _is_mathish_line("− ×") is True
    assert _is_mathish_line("∞") is True


def test_formula_fragments_are_still_mathish():
    """⚠ 这条是**反向**护栏：别把公式碎片判成正文。

    "ndm gp" 是 JACERS 第 2 页公式 (2) 的分子分母碎片（PDF 数学字体
    抽出来的）。如果它被判成正文，公式就散架、碎片直接进 prompt ——
    模型会照抄或瞎编（AGENTS.md §4.6 红线）。

    曾经真的栽过：把"有字母就不算公式"当通用规则，结果
    `tests/test_reader_formula_table.py::test_formula_fragments_are_not_left_as_garbage`
    变红 —— 碎片 "ndm gp" 留在正文里了。
    """
    for frag in ("ndm gp", "x", "y", "11 31 33", "eeﬀ ¼ 33 33 (3)"):
        assert _is_mathish_line(frag) is True, (
            f"公式碎片 {frag!r} 被判成正文 → 公式会散架、碎片进 prompt"
        )


def test_empty_and_overlong_lines():
    assert _is_mathish_line("") is False
    assert _is_mathish_line("   ") is False
    # 超长行直接判 False（避免把整段正文当公式）
    assert _is_mathish_line("x" * 300) is False
