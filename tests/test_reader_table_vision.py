# -*- coding: utf-8 -*-
"""表格走多模态读图（2026-10-25 用户要求）。

用户原话："对于表格的话个别单元他这个都抽错的话你也给我直接上这个多模态
读图吧……我要的是这个阅读的……至少顺序要是很正常的"。

背景：表格在 PDF 文本层是碎的
    "d (pC/N) (cid:4)90 (cid:4)175 (cid:4)109 B700 16.5"
文本模型只能靠上下文猜对齐 → 整行数值错位（d31 行的 16.5 跑到 d33 行、
7200 变成 B700）。改成：抽取层只记坐标，服务端裁图交给多模态模型逐格抄。

本文件锁死三件事：
  1. 抽取层把表格整块变成带坐标的标记（表格碎片不再进正文）；
  2. 标记的坐标落在表格真实位置（裁图能裁到）；
  3. 标记**不会被页尾截断切坏**（切坏 → 多模态读不到 → 表格全丢）。
"""
import json
import re
from pathlib import Path

import pytest

VAULT = Path(__file__).resolve().parent.parent
JACERS = (VAULT / "01_Literature" / "04_Survey_综述" / "01_论文原文_PDF" /
          "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")
REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
       "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")

pytestmark = pytest.mark.skipif(
    not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")


def _page_text(page_no: int) -> str:
    from microbench import paper_text as pt
    return pt.extract_page_text_from_path(str(JACERS), page_no)


def test_table_markers_carry_coordinates():
    text = _page_text(2)
    from microbench import paper_text as pt
    marks = pt.TABLE_MARKER_RE.findall(text)
    assert len(marks) == 2, f"第 2 页应有两张表（Table I / Table II），实际 {len(marks)}"
    for m in marks:
        x0, y0, x1, y1 = (float(v) for v in m)
        assert 0 < x0 < x1, f"x 范围无效: {m}"
        assert 0 < y0 < y1, f"y 范围无效: {m}"


def test_table_fragments_are_not_in_the_body_text():
    """表格的碎片行不能留在正文里（模型会拿它瞎猜对齐）。"""
    text = _page_text(2)
    assert "BaTiO3 PZT-5" not in text, "表头还留在正文里"
    assert "d (pC/N) (cid:4)90 (cid:4)175" not in text, "表格数值碎片还在正文里"


def test_markers_survive_the_page_tail_trim():
    """页尾截断不能把标记切坏。

    标记里有小数点（"28.2,494.6,536.3,627.9"），而截尾是按"最后一个句末
    标点"切的 → 标记会被切成半截，半截匹配不上，多模态就找不到那张表了。
    """
    text = _page_text(2)
    from microbench import paper_text as pt
    # 页面末尾的标记必须完整
    assert "⟦TABLE:28.2,639.4,536.3,754.1⟧" in text, "页尾的表格标记被截断了"
    # 标记内部的小数点不能被当成切点
    trimmed = pt._trim_page_tail("正文。\n\n⟦TABLE:1.5,2.5,3.5,4.5⟧")
    assert trimmed.endswith("⟧"), f"标记被页尾截断切坏了: {trimmed!r}"


def test_trim_keeps_markers_intact():
    from microbench import paper_text as pt
    for marker in ("⟦FORMULA:45.1,138.3,272.3,169.5⟧",
                   "⟦TABLE:28.2,494.6,536.3,627.9⟧"):
        out = pt._trim_page_tail("前文没有句号\n" + marker)
        assert marker in out, f"{marker} 被截尾切坏了: {out!r}"


def test_read_table_endpoint_validates_input(client, sandbox):
    """裁表接口的入参校验（路径越界 / 坐标非法）。"""
    r = client.post("/api/paper/read_table", json={
        "relative_path": "../../etc/passwd", "page": 1, "rect": "1,2,3,4"})
    assert r.status_code in (400, 404), f"越权路径没被拦: {r.status_code}"
    r2 = client.post("/api/paper/read_table", json={
        "relative_path": REL, "page": 1, "rect": "abc"})
    assert r2.status_code in (400, 422), r2.text


def test_prompt_and_rules_mention_table_marker():
    """提示词要教模型"原样输出 ⟦TABLE_n⟧ 标记"。"""
    from microbench.app import TRANSLATION_SYSTEM_PROMPT
    assert "TABLE_0" in TRANSLATION_SYSTEM_PROMPT
    assert "⟦FORMULA" in TRANSLATION_SYSTEM_PROMPT


def test_translation_prompt_has_vision_table_rules():
    from microbench.app import TABLE_VISION_PROMPT
    for kw in ("Markdown", "不要推算", "上下标"):
        assert kw in TABLE_VISION_PROMPT, f"抄表提示词缺少约束: {kw}"
