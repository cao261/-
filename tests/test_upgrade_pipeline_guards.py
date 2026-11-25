# -*- coding: utf-8 -*-
"""Regression and behavioral guards for Step 2 pipeline upgrades."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# 1. translation.py: ⟦TABLE_n⟧ survival, chunk_section_text hard_max, overlap
# ---------------------------------------------------------------------------
def test_sanitize_keeps_neutral_table_token_and_apply_restores_markdown():
    from microbench import translation as tr

    raw = "导言\n\n⟦TABLE_0⟧\n\n结尾 ⟦TABLE:1,2,3,4⟧ 坏块 ⟦TABLE_99⟧"
    sanitized = tr.sanitize_translation(raw)
    assert "⟦TABLE_0⟧" in sanitized, "中性表格占位符被 sanitize_translation 误杀"
    assert "⟦TABLE:1,2,3,4⟧" not in sanitized, "坐标标记应被兜底清理"

    restored = tr.apply_table_map(sanitized, {"⟦TABLE_0⟧": "| A | B |\n|---|---|\n| 1 | 2 |"})
    assert "| A | B |" in restored
    assert "⟦TABLE_0⟧" not in restored
    assert "⟦TABLE_99⟧" not in restored
    assert tr.TABLE_TEXT_FALLBACK in restored


def test_translate_page_endpoint_puts_the_multimodal_table_back(client, sandbox, monkeypatch):
    """⚠ **端点级**护栏（2026-11-25 审计 P0）。

    旧顺序是 `sanitize_translation(...)` 在前、`table_map` 回填在后，
    而清洗阶段就把 `⟦TABLE_n⟧` 销毁了 → 多模态抄好的表格**永远贴不回原位**。

    ⚠ 为什么要端点级：函数级（`sanitize_translation` / `apply_table_map` 各测一遍）
      只能证明**这两个函数**是对的，证明不了**端点把它们按对的顺序接在一起**。
      本轮在另一处就栽了同型：判据写在了用户不走的路径上（§4.146）。
    """
    fitz = pytest.importorskip("fitz")
    from microbench import app as app_mod
    from microbench import llm as llm_mod

    # 造一个**短页** PDF：抽出来的正文 < 100 字符 → 端点保留前端传来的 text
    # （这样 ⟦TABLE:...⟧ 坐标标记一定在 page_text 里，表格分支才会被走到）
    rel = "01_Literature/表格拼回样本.pdf"
    pdf_path = sandbox / rel
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "Short.", fontsize=11, fontname="helv")
    doc.save(str(pdf_path))
    doc.close()

    seen = {}

    async def fake_prepare_tables(page_text, pdf_path_, page_no, model=""):
        # 真实 _prepare_tables 会把 ⟦TABLE:...⟧ 换成中性占位符 ⟦TABLE_n⟧，
        # 并把多模态抄出的 Markdown 放进 table_map
        seen["called"] = True
        return ("表格前文 ⟦TABLE_0⟧ 表格后文",
                {"⟦TABLE_0⟧": "| A | B |\n|---|---|\n| 1 | 2 |"})

    monkeypatch.setattr(app_mod, "_prepare_tables", fake_prepare_tables)
    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat", lambda **kw: "译文前 ⟦TABLE_0⟧ 译文后")

    res = client.post("/api/paper/translate_page", json={
        "paper_id": "tableback_xyz789",
        "page": 1,
        "text": "表格前文 ⟦TABLE:10,20,100,50⟧ 表格后文",
        "relative_path": rel,
        "force": True,
    })
    assert res.status_code == 200, res.text
    assert seen.get("called"), "表格读图根本没被调用 —— 这条护栏就守不到东西"

    translation = res.json()["translation"]
    assert "| A | B |" in translation, \
        f"抄好的表格没贴回译文里（占位符在清洗阶段就被销毁了）: {translation!r}"

    # ⚠ 只断言「表在译文里」**不够** —— `apply_table_map` 有个兜底分支：
    #   占位符不见了就把表格**追加到译文末尾**，表确实在译文里，但**没贴回原位**
    #   （这正是 P0 的症状：表格永远回不到它该在的地方）。
    # → 必须断言**位置**：表在「译文前」与「译文后」之间。
    assert translation.index("译文前") < translation.index("| A | B |") \
        < translation.index("译文后"), \
        f"表格没贴回原位（被追加到末尾了）: {translation!r}"
    assert "未能读出" not in translation, \
        f"占位符被清洗阶段销毁了 → 表格只能兜底追加到末尾: {translation!r}"
    assert "⟦TABLE_0⟧" not in translation, \
        f"占位符漏给用户了: {translation!r}"


def test_chunk_section_text_respects_hard_max_with_overlap():
    from microbench.translation import chunk_section_text

    # Single paragraph close to hard_max followed by more text
    p1 = "A" * 400
    p2 = "B" * 480
    p3 = "C" * 480
    chunks = chunk_section_text(f"{p1}\n\n{p2}\n\n{p3}", target=500, hard_max=520, overlap=250)
    assert len(chunks) >= 2
    for ch in chunks:
        assert len(ch) <= 520, f"分块超出 hard_max: {len(ch)} > 520"


def test_merge_translation_parts_does_not_swallow_short_new_paragraph():
    from microbench.translation import merge_translation_parts

    p_overlap = "这是一段相当长的重叠段落内容用于跨分块衔接，包含足够多的汉字以确保字符比例超过阈值。" * 3
    new_short = "实验成立。"
    part1 = f"第一部分开头。\n\n{p_overlap}"
    part2 = f"{p_overlap}\n\n{new_short}"
    merged = merge_translation_parts([part1, part2])
    assert new_short in merged, f"短新段被重叠去重吞掉了: {merged}"
    assert merged.count(p_overlap) == 1


# ---------------------------------------------------------------------------
# 2. replaced_pdf.py: CJK-Latin gap wrap/positions & kinsoku punctuation
# ---------------------------------------------------------------------------
def test_replaced_pdf_cjk_latin_wrap_never_overflows_line_width():
    from microbench import replaced_pdf as rp

    font = rp._resolve_font()
    ts = rp._CjkTypesetter(font)
    text = "在BaTiO3和PZT材料中研究了CoFe2O4界面耦合"
    width = 90.0
    fs = 10.0
    lines = ts.wrap(rp._split_atoms(text), width, fs)
    assert len(lines) >= 2
    for ln in lines:
        w = ts.line_width(ln, fs)
        assert w <= width + 0.05, f"CJK-Latin 混排行宽溢出 {w:.2f} > {width}: {ln}"
        pos = ts.positions(ln, fs, 0.0, width, False)
        last_atom, last_x = pos[-1]
        end_x = last_x + ts.measure(last_atom[1], fs)
        assert end_x <= width + 0.05, f"positions 实际右缘溢出 {end_x:.2f} > {width}"


def test_replaced_pdf_kinsoku_prevents_open_end_and_double_close_start():
    from microbench import replaced_pdf as rp

    font = rp._resolve_font()
    ts = rp._CjkTypesetter(font)
    sample = "测试一二「引用内容测试」。？！最后一句。"
    lines = ts.wrap(rp._split_atoms(sample), 50.0, 10.0)
    for ln in lines:
        joined = "".join(c for _k, c in ln)
        assert joined[0] not in rp._NO_LINE_START, f"行首出现禁则标点: {joined!r}"
        assert joined[-1] not in rp._NO_LINE_END, f"行尾出现开括号: {joined!r}"


def test_fix_missing_glyphs_strips_zero_width_fallbacks():
    from microbench.replaced_pdf import CjkFont, fix_missing_glyphs

    class _FakeNoBom(CjkFont):
        def has_glyph(self, ch, bold=False):
            return ch not in ("\ufeff", "\u200b")

    out = fix_missing_glyphs("A\ufeffB\u200bC", _FakeNoBom(name="probe"))
    assert out == "ABC", f"空回退映射未清除零宽字符: {out!r}"


# ---------------------------------------------------------------------------
# 3. glyphs.py & plot_compare.py & qa_context.py
# ---------------------------------------------------------------------------
def test_glyph_parse_recognition_dot_numbering_no_index_shift():
    from microbench import glyphs as gl

    assert gl._parse_recognition("1. ×, 2. ≈", 5) == {1: "×", 2: "≈"}
    assert gl._parse_recognition("1) ≥\n2) ≠", 3) == {1: "≥", 2: "≠"}


def test_glyph_dictionary_save_preserves_disk_entries(tmp_path):
    from microbench.glyphs import GlyphDictionary

    p = tmp_path / "g.json"
    d1 = GlyphDictionary(p).load()
    d2 = GlyphDictionary(p).load()
    d1.put("s:F1|1", "×", "a" * 64)
    d1.save()
    d2.put("s:F2|2", "≈", "b" * 64)
    d2.save()
    reloaded = GlyphDictionary(p).load()
    assert reloaded.get("s:F1|1") == "×"
    assert reloaded.get("s:F2|2") == "≈"


def test_plot_compare_restricts_to_overlap_and_constant_r2():
    from microbench.plot_compare import compute_metrics

    # Outlier point at x=10 outside overlap [0, 5] must NOT corrupt RMSE
    m = compute_metrics([[0.0, 0.0], [5.0, 5.0]], [[0.0, 0.0], [5.0, 5.0], [10.0, 10.0]])
    assert m["x_overlap"] == [0.0, 5.0]
    assert m["n_points"] == 2
    assert m["rmse"] == 0.0
    assert m["verdict"] == "pass"

    # Exact constant curve should report R^2 == 1.0
    c = compute_metrics([[0.0, 3.0], [1.0, 3.0], [2.0, 3.0]], [[0.0, 3.0], [1.0, 3.0], [2.0, 3.0]])
    assert c["r_squared"] == 1.0


def test_qa_context_budget_is_strict_and_supports_hundreds():
    from microbench.qa_context import extract_ordinals, pick_context

    assert extract_ordinals("请看第一百零五节") == {105}
    secs = [
        {"section": f"s{i}", "title": f"{i}. Title {i}", "number": str(i), "text": "正文。" * 400}
        for i in range(1, 6)
    ]
    ctx, meta = pick_context(
        secs, question="总结", focus_text="焦点正文内容。" * 200, focus_title="当前节", budget=900
    )
    assert len(ctx) <= 900, f"上下文超出严格预算: {len(ctx)} > 900"
    assert meta["chars"] <= 900


# ---------------------------------------------------------------------------
# 4. paper_text.py & llm.py
# ---------------------------------------------------------------------------
def test_mark_line_glyphs_does_not_swallow_operator_between_cids():
    from microbench import paper_text as pt

    line = {"text": "(cid:3)=(cid:4)", "x0": 0, "x1": 100, "top": 10, "bottom": 20}
    glyphs = [
        {"font": "F", "cid": "3", "x0": 10, "x1": 18, "top": 11, "bottom": 19},
        {"font": "F", "cid": "4", "x0": 24, "x1": 32, "top": 11, "bottom": 19},
    ]
    out = pt._mark_line_glyphs(line, glyphs)
    assert "=" in out, f"运算符 '=' 被误吞掉了: {out!r}"
    assert out == "⟦G:0⟧=⟦G:1⟧"


def test_extract_outline_strips_glyph_markers_and_caches(tmp_path):
    fitz = pytest.importorskip("fitz")
    from microbench import paper_text as pt

    pdf = tmp_path / "outline_test.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "I. Introduction", fontsize=12, fontname="helv")
    page.insert_text((72, 120), "This composite exhibits strong ME coupling across interfaces.", fontsize=11, fontname="helv")
    doc.save(str(pdf))
    doc.close()

    res1 = pt.extract_outline(pdf)
    res2 = pt.extract_outline(pdf)
    assert res1 == res2
    assert res1["sections"]
    assert res1 is not res2, "缓存命中的返回值应当深拷贝，防止调用方原地修改污染缓存"


def test_extract_page_blocks_handles_short_single_column_page(tmp_path):
    fitz = pytest.importorskip("fitz")
    from microbench import paper_text as pt

    pdf = tmp_path / "short_page.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "Figure 1. Short caption only.", fontsize=11, fontname="helv")
    doc.save(str(pdf))
    doc.close()

    blocks = pt.extract_page_blocks(pdf, 1)
    assert blocks, "短单栏页（词数 < _MIN_WORDS_FOR_COLUMNS）不应再返回空块列表"
    assert "Short caption" in blocks[0]["text"]


def test_llm_thinking_400_separates_param_rejection_from_forced_thinking(monkeypatch):
    from microbench import llm

    monkeypatch.setattr(llm, "_THINKING_DISABLE_UNSUPPORTED", set())
    monkeypatch.setattr(llm, "_THINKING_FORCED_MODELS", {"MiniMax-M3.1-Flash"})

    llm._record_thinking_unsupported("custom-openai-model", "Unknown parameter: 'thinking'")
    assert llm.model_forces_thinking("custom-openai-model") is False
    assert llm.cfg_accepts_thinking_param({"model": "custom-openai-model", "base_url": "https://x"}) is False

    llm._record_thinking_unsupported("next-gen-adaptive", "requires adaptive thinking")
    assert llm.model_forces_thinking("next-gen-adaptive") is True


def test_llm_chat_isolated_uses_custom_provider_endpoint(monkeypatch):
    from microbench import llm

    seen = {}

    def fake_invoke(cfg, messages, *a, **kw):
        seen["url"] = cfg.get("base_url")
        seen["api_key"] = cfg.get("api_key")
        seen["model"] = cfg.get("model")
        return True, "ok", 200

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    monkeypatch.setattr(llm, "_invoke_model", fake_invoke)
    out = llm.chat_isolated([{"role": "user", "content": "hi"}], model_name="deepseek-flash")
    assert out == "ok"
    assert "deepseek.com" in (seen["url"] or "")
    assert seen["api_key"] == "sk-deepseek-test"
    assert seen["model"] == "deepseek-flash"


def test_llm_stream_model_strips_split_think_tags(monkeypatch):
    from microbench import llm

    chunks = ["<thi", "nk>internal", " secret</th", "ink>Visible ", "answer!"]
    sse_lines = [f'data: {json.dumps({"choices": [{"delta": {"content": c}}]})}' for c in chunks]
    sse_lines.append("data: [DONE]")

    class _FakeResp:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            for ln in sse_lines:
                yield ln

    class _FakeClient:
        def stream(self, *a, **kw):
            return _FakeResp()

    async def _no_limit(*a, **kw):
        return 0.0

    monkeypatch.setattr(llm, "_get_async_client", lambda timeout=60: _FakeClient())
    monkeypatch.setattr(llm, "_acquire_limiter_async", _no_limit)

    async def _run():
        out = []
        cfg = {"model": "m", "base_url": "https://example.invalid/v1", "api_key": "k"}
        async for part in llm._stream_model(cfg, [{"role": "user", "content": "q"}], 1000, 0.2, 30):
            out.append(part)
        return "".join(out)

    got = asyncio.run(_run())
    assert "internal" not in got
    assert "secret" not in got
    assert "<think" not in got
    assert got == "Visible answer!"


def test_llm_summarize_paper_parses_fenced_and_think_json(monkeypatch):
    from microbench import llm

    monkeypatch.setattr(
        llm,
        "chat",
        lambda **kw: '<think>planning</think>\n```json\n{"tldr": "摘要", "key_points": ["要点1"], "takeaway": "结论"}\n```',
    )
    res = llm.summarize_paper({"title": "Paper"})
    assert res["tldr"] == "摘要"
    assert res["key_points"] == ["要点1"]
    assert res["takeaway"] == "结论"
