# -*- coding: utf-8 -*-
"""抽取层「公式块定位」与「分栏阈值」的护栏。

**背景（本轮实测挖出来的）**

批次 B 一开始把两个阈值合并成一个：
    _MAX_FORMULA_RUN_LINES = 10   （同时管 _mark_broken_formulas 和 _formula_spans）

理由看起来很美：「同样的 11~12 行碎片，一条路径判'整段作废'、另一条判'定位公式块'，
行为不一致」。**但这个理由是错的** —— 两个阈值管的是两件不同的事：

  _mark_broken_formulas  「这段是坏掉的公式」→ 整段替换成 ⟦FORMULA⟧ 占位符
                         （没有坐标，前端裁不出图）→ 阈值小一点更保守
  _formula_spans         「这段是一整条公式」→ 定位 bbox，交给前端从 PDF
                         原页裁图贴进译文 → 阈值必须够大

合并到 10 的直接后果（真实案例）：
    `2024_1f_noise_TMR_sensor_bath_corre.pdf` 第 6 页的式 (A1) 整整 **11 行**
    （积分上下限 + 求和号 + 分式被拆成多行）。
    阈值压到 10 → 公式块定位失败 → 11 行碎片直接进 prompt
    → 模型会照着编出一条不存在的公式。

**AGENTS.md §4.6：错公式比没公式危险得多（用户会照着它去算）。**
用 fitz 裁图比对确认过：阈值 12 裁出来的是完整的式 (A1)，阈值 10 裁不出图。
"""
from __future__ import annotations

import pytest

from microbench import paper_text as pt
from microbench.paper_text import (
    _MAX_BROKEN_FORMULA_RUN_LINES,
    _MAX_FORMULA_SPAN_LINES,
    _MIN_BELOW_WORDS_FOR_COLUMNS,
    _MIN_WORDS_FOR_COLUMNS,
    _formula_spans,
)


def _mk_lines(texts: list[str], line_h: float = 4.0, step: float = 5.0) -> list[dict]:
    """造一批「行」：坐标是假的，但相对关系（上下、跨度）是真的。"""
    out = []
    y = 10.0
    for t in texts:
        out.append({
            "text": t,
            "top": y,
            "bottom": y + line_h,
            "x0": 50.0,
            "x1": 300.0,
        })
        y += step
    return out


# 真实案例的 11 行（2024_1f_noise_TMR_sensor_bath_corre 第 6 页的式 (A1)）：
# 积分上下限、求和号、分式被 PDF 拆成多行。
ELEVEN_LINE_FORMULA = [
    "ν(t",
    "−",
    "t′)m˙ y′(t′)dt′ = (",
    "−",
    "τ",
    "c",
    ")n−1",
    "dtn",
    "m y′(t). (A1)",
    "Z−∞ n=1",
    "X",
]


def test_eleven_line_formula_is_still_locatable():
    """⚠ 这条是**反向**护栏：11 行的长公式必须还能被定位成公式块。

    定位失败 → 碎片直接进 prompt → 模型会照着编公式（§4.6）。
    """
    lines = _mk_lines(ELEVEN_LINE_FORMULA)
    spans = _formula_spans(lines, [False] * len(lines))
    assert spans, (
        "11 行的长公式没能定位成公式块 → 碎片会直接进 prompt，"
        "模型会照着编一条不存在的公式（§4.6）"
    )
    assert spans[0] == (0, len(ELEVEN_LINE_FORMULA) - 1)


def test_formula_span_threshold_is_twelve():
    """阈值必须是 12 —— 11 行公式的实测下限。

    改成 10 → test_eleven_line_formula_is_still_locatable 变红。
    """
    assert _MAX_FORMULA_SPAN_LINES == 12, (
        "_MAX_FORMULA_SPAN_LINES 被改动了：11 行的长公式（式 A1）会定位失败，"
        "碎片直接进 prompt → 模型照着编公式（§4.6）"
    )


def test_broken_formula_threshold_stays_at_ten():
    """坏公式占位符的阈值保持 10（更保守：宁可少判也别毁掉整块表格）。"""
    assert _MAX_BROKEN_FORMULA_RUN_LINES == 10


def test_very_long_run_is_not_a_formula():
    """13 行的连续数学行**不**当成一条公式 —— 那多半是表格。

    这条保证「公式块定位」不会把整张表吞成一张图（§4.13 的教训）。
    """
    texts = ELEVEN_LINE_FORMULA + ["Z−∞ n=2", "Y"]
    lines = _mk_lines(texts)
    spans = _formula_spans(lines, [False] * len(lines))
    assert not spans, "13 行的连续数学行被当成了公式块（多半是表格被吞）"


# ---------------------------------------------------------------------------
# 分栏阈值：40 / 30 —— 别为了"宽松"把它降下去
# ---------------------------------------------------------------------------

def test_column_thresholds_stay_conservative():
    """分栏词数阈值必须保持 40 / 30。

    降成 12 / 10 的那版把
      `2024_Fe_MgO_Fe_DMFT_TMR_first_princ.pdf` 第 20 页
    （双栏论文的最后一页，整页只有一条**横跨两栏**的参考文献 + 右上角页码）
    送进了分栏路径，而栏缝恰好落在连字符上（"Ap-" | "plied"）
    → 参考文献被从中间劈开、页码"20"楔进两半之间。

    fitz 渲染原页确认过：那条参考文献本来是跨栏整行。
    词数少 ≠ 不可能分栏 —— 保守阈值是对的，词数不够就别猜栏。
    """
    assert _MIN_WORDS_FOR_COLUMNS == 40, (
        "分栏词数下限被改小了：横跨两栏的参考文献页会被误判成双栏，"
        "从连字符处被劈开（实测 Fe_MgO 第 20 页）"
    )
    assert _MIN_BELOW_WORDS_FOR_COLUMNS == 30


def test_column_thresholds_are_not_shared_with_formula_thresholds():
    """两条阈值本来就是两件事，别再合并（合并过一次，坑了 11 行长公式）。"""
    assert _MAX_FORMULA_SPAN_LINES != _MAX_BROKEN_FORMULA_RUN_LINES
    assert pt._RELATION_RE.search("=") is not None
