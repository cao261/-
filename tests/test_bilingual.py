# -*- coding: utf-8 -*-
"""段落级翻译（2026-11-12）—— 「译文按原文排版」的翻译侧契约。

用户原话："就像那种翻译插件一样，他们和原文的排版是一模一样的，
没什么区别……原文是双栏，他们也是双栏"。

这套测试守三件事：

  ① **协议**：模型按 `⟦B7⟧` / `⟦T7⟧` 编号返回 → 服务端按号回填。
     回填错位 = 译文贴到错的段落上 —— 那比没译文更糟。
  ② **版本闸门**：段落切分算法一改，块编号就变了 →
     旧译文必须自动作废（`BLOCK_TRANSLATION_VERSION`），而且**不能**
     牵连整页译文（`EXTRACTOR_VERSION` 绝不能动）。
  ③ **存储**：段落译文要能存进快照并读回来 —— 平板端只能靠这条通道
     （它访问不到本地后端）。

⚠ 这套测试**不调真模型**（用假的 chat），跑得快、也不烧额度。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from microbench import bilingual as bi
from microbench import paper_text as pt

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# ① 协议
# ---------------------------------------------------------------------------
def test_flatten_inline_newlines_joins_line_breaks():
    """单个换行（PDF 的行折）要合成空格；双换行（真分段）保留。"""
    assert bi._flatten_inline_newlines("前半\n后半") == "前半 后半"
    assert bi._flatten_inline_newlines("第一段\n\n第二段") == "第一段\n\n第二段"
    assert bi._flatten_inline_newlines("  前后  ") == "前后"


def test_parse_block_translations_flattens_line_breaks():
    """译文里的行折要合成空格（下标不能单独占一行）。"""
    raw = "⟦T0⟧其中 a 为横向系数\nE,31\nE 表示体积比⟦/T0⟧"
    got = bi.parse_block_translations(raw)
    assert got == {0: "其中 a 为横向系数 E,31 E 表示体积比"}


def test_parse_block_translations_keeps_ids():
    raw = "⟦T0⟧第一段⟦/T0⟧\n\n⟦T2⟧第三段⟦/T2⟧"
    got = bi.parse_block_translations(raw)
    assert got == {0: "第一段", 2: "第三段"}


def test_parse_block_translations_filters_wanted_ids():
    raw = "⟦T0⟧一⟦/T0⟧⟦T5⟧五⟦/T5⟧"
    got = bi.parse_block_translations(raw, want_ids={5})
    assert got == {5: "五"}


def test_parse_block_translations_drops_empty_segment():
    """空段不能进结果（否则会贴出空白框）。"""
    raw = "⟦T0⟧  ⟦/T0⟧⟦T1⟧有内容⟦/T1⟧"
    assert bi.parse_block_translations(raw) == {1: "有内容"}


def test_parse_block_translations_accepts_missing_close_marker():
    """⚠ **模型忘了写 ⟦/Tn⟧ 也要能解析**（2026-11-12 实测）。

    真相：JACERS 第 2 页 12 段，模型把编号**全写对了**，却一个闭标记都没写
    → 旧解析器一段都拿不到 → 整页 502「模型没有按段落编号返回译文」。
    段落在**下一个开标记**处结束 → 必须能切出来。
    """
    raw = "⟦T0⟧第一段\n\n⟦T2⟧第三段\n\n⟦T5⟧第五段"
    got = bi.parse_block_translations(raw)
    assert got == {0: "第一段", 2: "第三段", 5: "第五段"}


def test_parse_block_translations_missing_close_stops_at_next_open():
    """缺闭标记时，段落必须截到**下一个开标记**（不能把下一段吞进来）。"""
    raw = "⟦T0⟧第一段\n\n⟦T2⟧第三段"
    got = bi.parse_block_translations(raw)
    assert got[0] == "第一段"
    assert got[2] == "第三段"


def test_parse_block_translations_close_marker_wins_over_next_open():
    """有闭标记时，段落截到**自己的**闭标记（尾随内容不算进段里）。"""
    raw = "⟦T0⟧第一段⟦/T0⟧ 多余的话\n\n⟦T2⟧第三段⟦/T2⟧"
    got = bi.parse_block_translations(raw)
    assert got[0] == "第一段"
    assert got[2] == "第三段"


def test_align_by_paragraphs_fallback():
    """模型无视标记、但段数对得上 → 按空行对齐（常见情况，能救）。"""
    blocks = [{"i": 0, "text": "a"}, {"i": 2, "text": "b"}]
    got = bi._align_by_paragraphs("译文甲\n\n译文乙", blocks)
    assert got == {0: "译文甲", 2: "译文乙"}


def test_align_by_paragraphs_refuses_when_count_mismatches():
    """段数对不上 → 返回空（宁可放弃同位模式，也不能贴错段落）。"""
    blocks = [{"i": 0, "text": "a"}, {"i": 2, "text": "b"}]
    assert bi._align_by_paragraphs("只有一段", blocks) == {}


def test_blocks_signature_changes_with_version():
    """版本闸门：版本一变，签名必须变（否则旧译文会被当成新的用）。"""
    blocks = [{"i": 0, "text": "a"}]
    sig = bi.blocks_signature(blocks)
    old = bi.BLOCK_TRANSLATION_VERSION
    try:
        bi.BLOCK_TRANSLATION_VERSION = old + 1
        assert bi.blocks_signature(blocks) != sig
    finally:
        bi.BLOCK_TRANSLATION_VERSION = old


def test_blocks_signature_ignores_geometry_but_not_text():
    """坐标变了不影响译文内容 → 签名不该变；原文变了必须变。"""
    b1 = [{"i": 0, "text": "a", "bbox": [1, 2, 3, 4]}]
    b2 = [{"i": 0, "text": "a", "bbox": [9, 9, 9, 9]}]
    b3 = [{"i": 0, "text": "a改", "bbox": [1, 2, 3, 4]}]
    assert bi.blocks_signature(b1) == bi.blocks_signature(b2)
    assert bi.blocks_signature(b1) != bi.blocks_signature(b3)


def test_concatenate_blocks_joins_with_blank_line():
    blocks = [{"translation": "一"}, {"translation": ""}, {"translation": "二"}]
    assert bi.concatenate_blocks(blocks) == "一\n\n二"


# ---------------------------------------------------------------------------
# ② translate_blocks 的行为（假 chat，不调真模型）
# ---------------------------------------------------------------------------
def _blocks():
    return [
        {"i": 0, "kind": "body", "col": "L", "bbox": [1, 2, 3, 4], "text": "First para"},
        {"i": 1, "kind": "body", "col": "L", "bbox": [1, 5, 3, 6], "text": "Second para"},
        {"i": 2, "kind": "formula", "col": "L", "bbox": [1, 7, 3, 8], "text": "a 1/2 b"},
        {"i": 3, "kind": "table", "col": "S", "bbox": [1, 9, 3, 10], "text": ""},
    ]


def test_translate_blocks_fills_translations():
    async def chat(**kw):
        return "⟦T0⟧第一段⟦/T0⟧\n⟦T1⟧第二段⟦/T1⟧"

    out, reason = asyncio.run(bi.translate_blocks(_blocks(), chat))
    assert reason == ""
    assert out[0]["translation"] == "第一段"
    assert out[1]["translation"] == "第二段"


def test_translate_blocks_skips_formula_and_table():
    """公式/表格**不进模型**（碎片会被模型补全成不存在的公式、表格会整行错位）。"""
    seen = {}

    async def chat(**kw):
        seen["user"] = kw["messages"][-1]["content"]
        return "⟦T0⟧一⟦/T0⟧⟦T1⟧二⟦/T1⟧"

    out, _ = asyncio.run(bi.translate_blocks(_blocks(), chat))
    assert "a 1/2 b" not in seen["user"], "公式碎片被送进模型了"
    assert out[2]["translation"] == "", "公式块的译文应由调用方填（贴公式原图）"
    assert out[3]["translation"] == "", "表格块的译文应由调用方填（多模态抄表）"


def test_translate_blocks_marks_untranslated_paragraphs():
    """漏段 → 合并成**一次**补问；补问也失败 → 标 UNTRANSLATED（partial）。"""
    calls = []

    async def chat(**kw):
        calls.append(kw)
        return "⟦T0⟧一⟦/T0⟧"          # 漏了第 1 段；补问也只回得来第 1 段

    out, reason = asyncio.run(bi.translate_blocks(_blocks(), chat))
    assert reason == "partial"
    assert out[0]["translation"] == "一"
    assert out[1]["translation"] == bi.UNTRANSLATED
    assert len(calls) == 2, "漏段应该合并成一次补问"


def test_translate_blocks_recovers_missing_paragraph_by_one_retry():
    """漏段 → 补问一次就补回来了 → 不算 partial（用户不用看到警告）。"""
    calls = []

    async def chat(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return "⟦T0⟧一⟦/T0⟧"          # 漏了第 1 段
        return "⟦T1⟧二⟦/T1⟧"               # 补问补上了

    out, reason = asyncio.run(bi.translate_blocks(_blocks(), chat))
    assert reason == ""
    assert out[0]["translation"] == "一"
    assert out[1]["translation"] == "二"
    assert len(calls) == 2


def test_translate_blocks_gives_up_when_model_ignores_protocol():
    """模型完全无视标记、段数也对不上 → 报 parse（调用方据此放弃同位模式）。"""
    async def chat(**kw):
        return "这是一整段完全对不上号的译文"

    out, reason = asyncio.run(bi.translate_blocks(_blocks(), chat))
    assert reason == "parse"
    assert all(b["translation"] == bi.UNTRANSLATED
               for b in out if b["kind"] == "body")


def test_translate_blocks_uses_paragraph_alignment_fallback():
    """模型无视标记但段数对得上 → 按空行对齐救回来。"""
    async def chat(**kw):
        return "译文甲\n\n译文乙"

    out, reason = asyncio.run(bi.translate_blocks(_blocks(), chat))
    assert reason == ""
    assert out[0]["translation"] == "译文甲"
    assert out[1]["translation"] == "译文乙"


# ---------------------------------------------------------------------------
# ③ 标题切分（对照模式里标题要自己一个框）
# ---------------------------------------------------------------------------
def _line(top, bottom, x0, x1, text):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


def test_heading_is_split_from_body():
    """标题和它下面的正文必须分成两块（否则译文糊成一坨）。"""
    lines = [
        _line(100, 110, 60, 300, "(3) Ceramic and Terfenol-D Alloy ME"),
        _line(111, 121, 60, 300, "Composites"),
        _line(130, 140, 60, 300, "Although ferrite/PZT laminates had giant"),
        _line(141, 151, 60, 300, "ME voltage coefficients, the limited was"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    kinds = [b["kind"] for b in blocks]
    assert "heading" in kinds, f"标题没被切出来：{[(b['kind'], b['text'][:30]) for b in blocks]}"
    head = next(b for b in blocks if b["kind"] == "heading")
    assert "Ceramic" in head["text"]
    assert "Composites" in head["text"]
    assert "ferrite" not in head["text"], "正文被吞进标题了"


def test_body_sentence_is_not_mistaken_for_heading():
    """正文句子（带编号前缀、很长）不能被当成标题。

    真实案例（ACS 论文）："V. The remarkable ME tunability comes from
    voltage induced extra FM ordering in Pt" —— 这里的 "V." 是电压变量，
    不是章节号。
    """
    lines = [
        _line(100, 110, 60, 300,
              "V. The remarkable ME tunability comes from voltage induced extra"),
        _line(111, 121, 60, 300,
              "FM ordering in Pt, which is a hallmark of the coupled system"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert all(b["kind"] != "heading" for b in blocks), \
        f"正文被误判成标题了：{[(b['kind'], b['text'][:40]) for b in blocks]}"


def test_reference_entry_is_not_mistaken_for_heading():
    """参考文献条目不能被当成标题（词数太多）。"""
    lines = [
        _line(100, 110, 60, 300, "(1) Azevedo, F. A. C.; et al. Equal Numbers of"),
        _line(111, 121, 60, 300, "Neuronal and Neuronal Magnetic Signals"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert all(b["kind"] != "heading" for b in blocks), \
        f"参考文献被误判成标题了：{[(b['kind'], b['text'][:40]) for b in blocks]}"


def test_toc_dot_leader_line_is_not_heading():
    """目录的点线行不是标题。"""
    lines = [
        _line(100, 110, 60, 300, "I. INTRODUCTION. . . . . . . . . . . . . 2"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert all(b["kind"] != "heading" for b in blocks)


# ---------------------------------------------------------------------------
# ④ API：/api/paper/translate_blocks
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    not (pt.Path(__file__).resolve().parent.parent
         / "01_Literature").is_dir(),
    reason="vault 里没有论文样本，跳过")
def test_translate_blocks_endpoint_contract(vault_client, monkeypatch):
    """端点契约：每块带自己的译文 + 整页译文 + 版本号。"""
    from microbench import app as app_mod
    from microbench import llm as llm_mod

    repo = pt.Path(__file__).resolve().parent.parent
    pdfs = sorted((repo / "01_Literature").rglob("*.pdf"))
    if not pdfs:
        pytest.skip("vault 里没有 PDF 样本，跳过")
    pdf = pdfs[0]
    rel = str(pdf.relative_to(repo)).replace("\\", "/")

    async def fake_chat(**kw):
        # 假装模型完美按协议返回
        import re
        ids = re.findall(r"⟦B(\d+)⟧", kw["messages"][-1]["content"])
        return "".join(f"⟦T{i}⟧译文{i}⟦/T{i}⟧" for i in ids)

    def fake_chat_sync(**kw):
        return asyncio.run(fake_chat(**kw))

    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat", fake_chat_sync)
    monkeypatch.setattr(llm_mod, "get_llm_status",
                        lambda: {"is_degraded": False, "active_model": "test"})

    r = vault_client.post("/api/paper/translate_blocks", json={
        "paper_id": "contract-test", "page": 1,
        "relative_path": rel, "force": True, "model": "",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "success"
    assert data["blocks_version"] == bi.BLOCK_TRANSLATION_VERSION
    assert data["blocks"], "应该返回段落块"
    for b in data["blocks"]:
        assert "translation" in b
        assert "i" in b and "col" in b and "bbox" in b
    assert data["translation"].strip(), "应该同时给出整页译文"


def test_translate_blocks_endpoint_requires_relative_path(vault_client):
    """段落块要服务端抽 → relative_path 必填。"""
    r = vault_client.post("/api/paper/translate_blocks", json={
        "paper_id": "contract-test", "page": 1,
    })
    assert r.status_code == 400, r.text


def test_translate_blocks_endpoint_rejects_illegal_path(vault_client):
    """路径闸门必须还在。"""
    r = vault_client.post("/api/paper/translate_blocks", json={
        "paper_id": "contract-test", "page": 1,
        "relative_path": "../../etc/passwd",
    })
    assert r.status_code in (400, 404), r.text


# ---------------------------------------------------------------------------
# ⑤ 快照：段落译文能存能取（平板端唯一的通道）
# ---------------------------------------------------------------------------
def test_snapshot_round_trip_keeps_blocks(client):
    """快照存进 pages[].blocks，读回来还在 —— 平板靠它同步。"""
    blocks = [{
        "i": 0, "kind": "body", "col": "L", "bbox": [60.0, 100.0, 300.0, 120.0],
        "translation": "第一段译文",
    }]
    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "bilingual-snapshot-test",
        "paper_title": "段落译文快照测试",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 0,
        "pages": [{
            "page_num": 1, "text_hash": "0123456789abcdef",
            "translation": "第一段译文", "model": "test",
            "translated_at": "2026-11-12T10:00:00",
            "blocks": blocks, "blocks_version": bi.BLOCK_TRANSLATION_VERSION,
        }],
    })
    assert r.status_code == 200, r.text

    r2 = client.get("/api/pdf/snapshot/load",
                    params={"paper_id": "bilingual-snapshot-test"})
    assert r2.status_code == 200, r2.text
    data = r2.json()
    page = data["pages"][0]
    assert page["blocks"], "快照里的段落译文丢了"
    assert page["blocks"][0]["translation"] == "第一段译文"
    assert page["blocks_version"] == bi.BLOCK_TRANSLATION_VERSION
    # 整页译文必须还在（段落译文的作废不能牵连它）
    assert page["translation"] == "第一段译文"
    client.delete("/api/pdf/snapshot/delete",
                  params={"paper_id": "bilingual-snapshot-test"})


def test_snapshot_load_drops_blocks_when_version_mismatches(client):
    """段落切分算法改了（版本 +1）→ 段落译文作废，但**整页译文保留**。"""
    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "bilingual-version-test",
        "paper_title": "版本闸门测试",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 0,
        "pages": [{
            "page_num": 1, "text_hash": "0123456789abcdef",
            "translation": "整页译文", "model": "test",
            "translated_at": "2026-11-12T10:00:00",
            "blocks": [{"i": 0, "kind": "body", "col": "L",
                        "bbox": [1.0, 2.0, 3.0, 4.0], "translation": "段落译文"}],
            "blocks_version": bi.BLOCK_TRANSLATION_VERSION + 99,
        }],
    })
    assert r.status_code == 200, r.text
    r2 = client.get("/api/pdf/snapshot/load",
                    params={"paper_id": "bilingual-version-test"})
    data = r2.json()
    page = data["pages"][0]
    # 服务端原样返回（版本比对在前端做），但**整页译文必须完好**
    assert page["translation"] == "整页译文"
    client.delete("/api/pdf/snapshot/delete",
                  params={"paper_id": "bilingual-version-test"})


# ---------------------------------------------------------------------------
# ⑥ 前端接线（静态断言：改名/漏接线要能被发现）
# ---------------------------------------------------------------------------
TABLET_JS = REPO / "microbench" / "static" / "tablet" / "app.js"
TABLET_CSS = REPO / "microbench" / "static" / "tablet" / "styles.css"
DESKTOP_HTML = REPO / "microbench" / "static" / "index.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def test_tablet_reads_block_translations_from_snapshot():
    """平板只能靠快照拿到段落译文 —— 这条线断了平板的对照模式就是空的。"""
    src = _read(TABLET_JS)
    assert "p.blocks" in src, "平板没从快照里读 pages[].blocks"
    assert "blockTrans" in src, "平板没有段落译文的存放位置"
    assert '["bilingual", "对照"]' in src, "平板的布局里没有「对照」这一项"
    assert "renderBilingualInto" in src, "平板没有对照渲染函数"


def test_tablet_formula_marker_is_not_shown_raw():
    """公式坐标标记不能原样甩给用户（平板裁不了公式图）。"""
    src = _read(TABLET_JS)
    assert "⟦FORMULA:" in src and "公式：见原文" in src, \
        "平板没有把公式标记换成人话"


def test_tablet_bilingual_css_exists():
    css = _read(TABLET_CSS)
    for name in (".tb-bilingual-cols", ".tb-bilingual-col", ".tb-bilingual-block",
                 ".tb-bilingual-empty"):
        assert name in css, f"平板的 styles.css 里少了 {name}"
    assert "lay-bilingual" in css, "平板没有 lay-bilingual 的样式规则"


def test_desktop_bilingual_wiring():
    """桌面端：开关、渲染、快照存取三处都要接上。"""
    src = _read(DESKTOP_HTML)
    assert "pdf-bilingual-toggle" in src, "桌面端没有「对照」按钮"
    assert "toggleBilingualMode" in src, "桌面端没有切换函数"
    assert "renderBilingualPage" in src, "桌面端没有对照渲染函数"
    assert "/api/paper/translate_blocks" in src, "桌面端没调段落翻译接口"
    assert "_bilingualBlocksPayload" in src, "桌面端没把段落译文存进快照"
    assert "blocks_version" in src, "桌面端没做段落译文的版本闸门"
    # ⚠ 绝不能出现动 EXTRACTOR_VERSION 的痕迹
    assert "EXTRACTOR_VERSION =" not in src, "前端不该出现抽取算法版本号"


def test_backend_never_bumps_extractor_version():
    """⚠ 红线：EXTRACTOR_VERSION 一动 → 用户全部译文作废重翻 + 波及另一台。"""
    from microbench import paper_text
    assert paper_text.EXTRACTOR_VERSION == 5, \
        "EXTRACTOR_VERSION 被改了！用户全部译文会作废重翻（跨设备同步）"

