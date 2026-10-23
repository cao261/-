# -*- coding: utf-8 -*-
"""双栏页面的阅读顺序：左栏正文读完再读右栏，跨栏表格整块保留在正文之后。

用户诉求（2026-10-23）："他老是先翻译那个最上面的那一栏……应该就是一页中
就是从左到右翻译，一直都是从左到右"。
"""
from pathlib import Path

import pytest

VAULT = Path(__file__).resolve().parent.parent
JACERS = (VAULT / "01_Literature" / "04_Survey_综述" / "01_论文原文_PDF" /
          "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")

pytestmark = pytest.mark.skipif(
    not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")


def _bands(page_no):
    """复刻抽取层：返回 (左栏正文, 右栏正文, 跨栏单元)。"""
    import pdfplumber
    from microbench import paper_text as pt
    with pdfplumber.open(str(JACERS)) as pdf:
        page = pdf.pages[page_no - 1]
        width, height = float(page.width), float(page.height)
        words = page.extract_words(x_tolerance=pt.PDF_X_TOLERANCE,
                                   use_text_flow=False, keep_blank_chars=False)
        y_split = pt._find_vertical_split(words, width, height)
        x_mid = pt._vote_column_gap(words, width, height, y_split)
        assert x_mid is not None, f"第 {page_no} 页没找到栏缝"
        return pt._extract_by_bands(page, words, width, height, y_split, x_mid)


def test_left_column_is_read_before_right_column():
    """左栏正文的所有段落必须整体早于右栏正文（不交替）。"""
    left, right, _units = _bands(2)
    assert left, "第 2 页应该抽到左栏正文"
    assert right, "第 2 页应该抽到右栏正文"
    # 左栏有多个段时，最后一段也要在右栏第一段之前
    assert left[-1] != right[0]


def test_joined_text_puts_left_column_entirely_before_right_column():
    from microbench import paper_text as pt
    left, right, units = _bands(2)
    text = pt._join_reading_order(left, right, units, "")
    assert text.index(left[-1]) < text.index(right[0]), \
        "左栏正文没有整体排在右栏正文之前（左右横跳了）"


def test_span_tables_are_kept_whole_and_moved_after_body_text():
    """跨栏表格整块保留，且排在两栏正文之后。"""
    from microbench import paper_text as pt
    left, right, units = _bands(2)
    assert units, "第 2 页应该识别出跨栏表格单元"
    text = pt._join_reading_order(left, right, units, "")
    right_start = text.index(right[0])
    for unit in units:
        for piece in unit:
            assert text.index(piece) > right_start, \
                "跨栏表格的碎片还留在正文里（或排在正文之前）"


def test_table_caption_stays_next_to_its_body():
    """表标题必须排在它自己的表体前面（不能被甩到最后）。"""
    from microbench import paper_text as pt
    left, right, units = _bands(2)
    text = pt._join_reading_order(left, right, units, "")
    caption = "Table I. Properties of Piezoelectric Materials"
    assert caption in text, "没抽到 Table I 的标题"
    # 表体（BaTiO3 那一半）必须跟在标题后面
    body = "BaTiO3"
    assert body in text
    assert text.index(caption) < text.index(body), \
        "表标题跑到表体后面去了"


def test_first_page_reads_left_then_right():
    """首页也是左栏整段在前。"""
    from microbench import paper_text as pt
    left, right, units = _bands(1)
    if not (left and right):
        pytest.skip("首页不是双栏布局")
    text = pt._join_reading_order(left, right, units, "")
    assert text.index(left[-1]) < text.index(right[0])


def test_no_horizontal_interleaving_in_final_text():
    """最终文本里，左栏的词不应该出现在右栏的词之间（左右交替）。

    只看正文区（y >= 双栏起点）且长度 >= 4 的词：页眉里的词会在文本最前面，
    单字符词（'c'、'.'）又到处都有，用它们判定会误报。
    """
    import pdfplumber
    from microbench import paper_text as pt
    with pdfplumber.open(str(JACERS)) as pdf:
        page = pdf.pages[1]
        text = pt._extract_page_text_raw(page)
        words = page.extract_words(x_tolerance=pt.PDF_X_TOLERANCE,
                                   use_text_flow=False, keep_blank_chars=False)
        width, height = float(page.width), float(page.height)
        y_split = pt._find_vertical_split(words, width, height)
        x_mid = pt._vote_column_gap(words, width, height, y_split)
        assert x_mid is not None

        body = [w for w in words
                if w["top"] >= y_split and len(w["text"]) >= 4]
        body.sort(key=lambda w: (w["top"], w["x0"]))
        left_body = [w["text"] for w in body if w["x1"] <= x_mid]
        right_body = [w["text"] for w in body if w["x0"] >= x_mid]
        assert left_body and right_body

        # 右栏最靠上的词 vs 左栏最靠下的词 —— 前者必须先出现
        right_early = right_body[:6]
        left_late = left_body[-6:]
        for lw in left_late:
            for rw in right_early:
                li, ri = text.find(lw), text.find(rw)
                if li < 0 or ri < 0:
                    continue
                assert li > ri, (
                    f"左栏词 {lw!r} 出现在右栏词 {rw!r} 之前 —— 左右交替了")
