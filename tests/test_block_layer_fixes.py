# -*- coding: utf-8 -*-
"""块层的三个修复（2026-11-13）的护栏。

三件事都只动**块层**（段落块），页级文本必须逐字不变
→ `EXTRACTOR_VERSION` 不动 → 用户的译文一句都不作废。

① 表格脚注「没有空格字符」→ 词粘成一坨 → 按位置间隙把词切回来
   （`_heal_line_spaces`）
② 单栏页底部藏着一段两栏正文 → 被并成一个宽块 → 块层兜底拆栏
   （`_find_bottom_column_split`）
③ ②的护栏：单栏论文底部的**宽表格**也有缝（那是表格的列分隔线），
   不能当成页面栏缝 → 每一带都必须「像正文」
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

VAULT = REPO / "01_Literature" / "04_Survey_综述" / "01_论文原文_PDF"
JACERS = VAULT / "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf"
MEC = VAULT / "Magnetoelectric Composites.pdf"

from microbench import paper_text as pt  # noqa: E402


# ---------------------------------------------------------------------------
# ① 没有空格字符的行 → 按间隙把词切回来
# ---------------------------------------------------------------------------
def _char(text, x0, x1, size=7.0):
    """造一个 pdfplumber 能用的字符（它要 upright/doctop 等字段）。"""
    return {"text": text, "x0": x0, "x1": x1, "size": size,
            "top": 617.0, "bottom": 625.0, "doctop": 617.0,
            "upright": True, "fontname": "TestFont"}


def _glued_line():
    """造一行「词粘在一起」的文本（模拟 JACERS 综述的表格脚注）。

    真实情况：字符是**逐个精确摆放**的（字母之间间隙 = 0），
    只有词与词之间有 1.33~1.48pt 的间隙；pdfplumber 用固定
    x_tolerance=1.5 切词 → 那些词边界被判成「字母间距」→ 粘成一坨。

    ⚠ 所以这里造出来的「文本」里**一个空格都没有**，
      词边界只体现在字符的位置间隙上 —— 这才是要修的那种行。
    """
    words = ("Where d 31 and d 33 are piezoelectric constants; e, dielectric "
             "constant; T c, curie temperature; r, density; Q m, mechanical "
             "quality factor; k 33, electromechanical coupling factor.").split()
    text = "".join(words)          # ← 一个空格都没有
    chars = []
    x = 40.0
    for i, w in enumerate(words):
        if i:
            x += 1.4                # 词边界：1.4pt 的间隙
        for ch in w:
            chars.append(_char(ch, x, x + 3.0))
            x += 3.0                # 字母之间：精确摆放，间隙 0
    return {"text": text, "x0": 40.0, "x1": x, "top": 617.0, "bottom": 625.0,
            "chars": chars}


def test_glued_line_gets_spaces_back():
    """粘在一起的行 → 词边界按位置间隙切回来。"""
    line = _glued_line()
    healed = pt._heal_line_spaces(line)
    assert "piezoelectric constants" in healed, healed
    assert "33arepiezoelectric" not in healed, healed
    assert "electromechanical coupling factor" in healed, healed
    # 字符一个都不能少（只是把空格插回去）
    assert healed.replace(" ", "") == line["text"].replace(" ", "")


def test_normal_line_is_not_touched():
    """正常的一行（本来就有空格）**不能**被乱切。"""
    line = {
        "text": "The magnetoencephalography signal was recorded at 30 Hz",
        "x0": 60.0, "x1": 400.0, "top": 100.0, "bottom": 110.0,
        "chars": [_char(c, 60.0 + i * 6.0, 63.0 + i * 6.0) for i, c in enumerate(
            "The magnetoencephalography signal was recorded at 30 Hz")],
    }
    assert pt._heal_line_spaces(line) == line["text"]


def test_cjk_line_is_not_touched():
    """中文行没有空格是**正常**的 —— 绝不能去动它。"""
    line = {
        "text": "磁电复合材料的压电系数随厚度比变化明显这一结论来自实验验证",
        "x0": 60.0, "x1": 400.0, "top": 100.0, "bottom": 110.0,
        "chars": [_char(c, 60.0 + i * 10.0, 70.0 + i * 10.0) for i, c in enumerate(
            "磁电复合材料的压电系数随厚度比变化明显这一结论来自实验验证")],
    }
    assert pt._heal_line_spaces(line) == line["text"]


# ---------------------------------------------------------------------------
# ①bis 连字（U+FB01 ﬁ）：pdfplumber 会把连字字符在 chars 里**重复登记一次**
# ---------------------------------------------------------------------------
def _glued_line_with_ligature():
    """造一行：词粘在一起，且含一个**被重复登记**的连字字符。

    实测（Magnetoelectric Composites 第 9 页）：
      page.chars 里 'ﬁ' 有 113 个，line['chars'] 里变成 132 个，
      重复项的 x0/x1 与原项**完全相同**；
      而 line['text'] 只有一份 →
      拿这份 chars 重新切词就会切出 "coeffificients" 这种**多一个 fi** 的坏词。

    错词比不修危险得多（模型会照着坏词翻译）→ 必须去重 + 硬不变式兜底。
    """
    words = ("The electric-field tuning of the composite is rapid and the "
             "magnetostrictive response is measured at room temperature").split()
    text = "".join(words)          # ← 一个空格都没有
    chars = []
    x = 40.0
    for i, w in enumerate(words):
        if i:
            x += 1.4                # 词边界：1.4pt 的间隙
        for ch in w:
            if ch == "f" and i + 1 < len(words) and words[i + 1].startswith("i"):
                # 这一对 f+i 在 PDF 里是**一个连字字符** U+FB01
                lig = _char("\ufb01", x, x + 5.0)
                chars.append(lig)
                chars.append(dict(lig))     # ← pdfplumber 的重复登记
                x += 5.0
                continue
            chars.append(_char(ch, x, x + 3.0))
            x += 3.0
    return {"text": text, "x0": 40.0, "x1": x, "top": 617.0, "bottom": 625.0,
            "chars": chars}


def test_duplicated_ligature_char_does_not_create_a_fake_word():
    """连字被重复登记 → 绝不能切出 "fifield" 这种多一个 fi 的坏词。"""
    line = _glued_line_with_ligature()
    healed = pt._heal_line_spaces(line)
    assert "fifield" not in healed, healed
    assert "electric-field" in healed, healed
    # 硬不变式：只加空格，不改字符
    assert healed.replace(" ", "") == line["text"].replace(" ", "")


def test_heal_never_changes_characters_on_real_sample():
    """真实样本（Magnetoelectric Composites）：修复只能加空格，不能改字符。

    这一条是**兜底的不变式**：不管阈值/去重逻辑怎么变，
    只要出现「切出来的词和原文对不上」，就必须整行弃修。
    """
    if not MEC.exists():
        pytest.skip("vault 里没有 Magnetoelectric Composites 样本，跳过")
    for pno in (9, 15, 23, 26):
        blocks = pt.extract_page_blocks(MEC, pno)
        for b in blocks:
            text = b.get("text") or ""
            for bad in ("fifield", "coeffificients", "Inflfluence", "fifilms"):
                assert bad not in text, "第 %s 页出现坏词 %r：%r" % (pno, bad, text[:200])


# ---------------------------------------------------------------------------
# ② 单栏页底部藏两栏正文 → 块层拆栏；③ 表格列分隔线不能误判
# ---------------------------------------------------------------------------
def _word(text, x0, x1, top):
    return {"text": text, "x0": x0, "x1": x1, "top": top,
            "bottom": top + 8.0}


def _two_column_words(y_start, n_lines, page_w=612.0, x_mid=306.0):
    """造一段「左栏正文 + 右栏正文」。

    ⚠ 每行每边要放**足够多**的词（≥8）：`_band_words_both_sides` 要求
      「投票的带」左右各有 ≥8 个词 —— 这正是把「宽表格的列分隔线」
      和「页面栏缝」区分开的第一道护栏（宽表格的词是摊在整页上的，
      某一头往往只有几个词）。
    """
    left = ["alpha", "beta", "gamma", "delta", "epsilon",
            "zeta", "eta", "theta", "iota"]
    right = ["kappa", "lambda", "sigma", "omega", "kappa2",
             "delta2", "sigma2", "theta2", "omega2"]
    words = []
    for i in range(n_lines):
        top = y_start + i * 9.0
        x = 60.0
        for w in left:
            words.append(_word(w, x, x + 24.0, top))
            x += 27.0
        x = 320.0
        for w in right:
            words.append(_word(w, x, x + 24.0, top))
            x += 27.0
    return words


def test_bottom_two_column_text_is_split():
    """单栏页底部的一段两栏正文 → 认得出栏缝（返回 y_top + x_mid）。"""
    height = 780.0
    words = _two_column_words(height * 0.60, 8)
    got = pt._find_bottom_column_split(words, 612.0, height)
    assert got is not None, "底部的两栏正文没认出来"
    y_top, x_mid = got
    assert abs(x_mid - 306.0) < 30.0, x_mid
    assert y_top >= height * 0.45


def test_table_column_separator_is_not_mistaken_for_page_columns():
    """⚠ 单栏论文底部的**宽表格**也有缝（那是表格的列分隔线）。

    把它当成页面栏缝 → 表格行被从中间劈开 → 原文与译文全错位
    （实测 3 页被误伤）。所以表格样的带必须**一票否决**。
    """
    height = 780.0
    words = []
    for i in range(6):
        top = height * 0.60 + i * 12.0
        # 表格行：好几段短文字 + 数字，没有像英文单词的东西
        words.append(_word("BaTiO3", 60.0, 100.0, top))
        words.append(_word("2605", 110.0, 140.0, top))
        words.append(_word("PZT-5", 160.0, 200.0, top))
        words.append(_word("16.5", 215.0, 245.0, top))
        words.append(_word("0.72", 260.0, 290.0, top))
        words.append(_word("1200", 320.0, 350.0, top))
        words.append(_word("0.94", 360.0, 390.0, top))
        words.append(_word("14", 410.0, 425.0, top))
    assert pt._find_bottom_column_split(words, 612.0, height) is None, \
        "表格的列分隔线被当成了页面栏缝"


# ---------------------------------------------------------------------------
# 真实样本回归：三个被误伤过的页面（表格列分隔线被当成页面栏缝）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,pno", [
    ("Review of Magnetoelectric Sensors.pdf", 11),
    ("Magnetoelectric Magnetic Field Sensors A Review.pdf", 16),
    ("Current Status of Magnetoelectric Composite Thin Thick Films.pdf", 6),
])
def test_real_pages_with_wide_tables_are_not_split(name, pno):
    """⚠ 真实回归样本：三页都被误伤过（表格行被从中间劈开）。

    根因是「宽表格的列分隔线」和「页面栏缝」在几何上很像：
    每一行都有一条空隙，但位置随机，最多偶然撞上 2 个带。
    → 现在必须同时满足三条（两头够词 / 跨 ≥3 个带 / 像正文）才认。
    """
    pdf = VAULT / name
    if not pdf.exists():
        pytest.skip("vault 里没有 %s，跳过" % name)
    import pdfplumber
    with pdfplumber.open(str(pdf)) as doc:
        page = doc.pages[pno - 1]
        words = page.extract_words(x_tolerance=pt.PDF_X_TOLERANCE,
                                   use_text_flow=False, keep_blank_chars=False)
        got = pt._find_bottom_column_split(words, float(page.width),
                                            float(page.height))
    assert got is None, "%s 第 %s 页又被当成两栏正文了：%s" % (name, pno, got)


# ---------------------------------------------------------------------------
# 真实样本：JACERS 综述第 2 页的表格脚注
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")
def test_jacers_table_footnote_is_readable():
    """实测样本：表格脚注的原文必须**可读**（不能是粘成一坨的）。"""
    blocks = pt.extract_page_blocks(JACERS, 2)
    texts = [b.get("text") or "" for b in blocks]
    foot = [t for t in texts if "curie" in t and "dielectric" in t]
    assert foot, "没找到表格脚注那一块"
    text = foot[0]
    assert "33arepiezoelectric" not in text, text
    assert "are piezoelectric" in text, text
    assert "dielectric constant" in text, text
    assert "electromechanical coupling factor" in text, text
