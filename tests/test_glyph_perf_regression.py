"""「打开/上传论文 半天没反应」的回归护栏。

用户反馈（2026-10-25）："上传本地pdf下面只是显示正在抓取并解析，然后半天没反应，
从论文库里打开也是半天没反应"。

根因（实测）：
  1. upload_pdf / open_vault_pdf **逐页**调符号认字 → 36 页的论文发
     35 次**串行**多模态请求（29~48s/次）→ 8 分钟以上。
  2. 符号词典的形状指纹**永远命不中**（同符号组内差中位 61 vs 异符号
     组间差中位 74 → 重叠），所以每页都重新问模型，还可能把 ≈ 认成 ≥。

修法：
  - 语义键（字体基名|字符码）当主键 → 跨论文复用，实测 6184 个实例
    只对应 101 个不同符号。
  - 整篇一次解析（resolve_document_glyphs）→ 全篇 1~3 次批量调用。

这里测的是**不依赖网络**的那部分：语义键、词典双层结构、批量去重、
降级路径（认不出来必须是 "[?]"，且内部标记绝不外泄）。
"""
import json
from pathlib import Path

import pytest

from microbench import glyphs as gl
from microbench import paper_text as pt


# ---------------------------------------------------------------------------
# 1) 语义键：跨文档稳定（去子集前缀）
# ---------------------------------------------------------------------------

def test_semantic_key_strips_subset_prefix():
    """同一原始字体、不同子集前缀 → 同一个语义键（跨论文复用的前提）。"""
    a = gl.semantic_key("JNKPHE+MathematicalPi-Three", "2")
    b = gl.semantic_key("WEIJUK+MathematicalPi-Three", "2")
    assert a == b == "MathematicalPi-Three|2"


def test_semantic_key_distinguishes_different_cid_and_font():
    assert gl.semantic_key("CMEX10", "18") != gl.semantic_key("CMEX10", "19")
    assert gl.semantic_key("CMEX10", "18") != gl.semantic_key("CMSY10", "18")


def test_semantic_key_handles_missing_font():
    assert gl.semantic_key("", "3") == "|3"


# ---------------------------------------------------------------------------
# 2) 词典：双层结构（语义键 + 形状二级校验）
# ---------------------------------------------------------------------------

def test_dictionary_semantic_key_roundtrip(tmp_path):
    path = tmp_path / "g.json"
    d = gl.GlyphDictionary(path)
    d.put("s:CMEX10|18", "×", "abc123")
    d.save()
    again = gl.GlyphDictionary(path).load()
    assert again.get("s:CMEX10|18") == "×"


def test_dictionary_shape_mismatch_is_rejected(tmp_path):
    """语义键命中但形状**极端不像** → 不采信（宁可 [?] 也不指错符号）。

    场景：两篇论文里同名基名的字体其实是不同字形。

    注意阈值：形状只是**二级校验**（语义键才是主键），
    所以容差放宽到 _SHAPE_GUARD_MAX_DIFF —— 实测同语义键跨文档的
    形状差最大 81 位，异语义键最小 0 位，形状本来就区分不开。
    """
    d = gl.GlyphDictionary(tmp_path / "g.json")
    base = "0" * 64
    d.put("s:CMEX10|18", "×", base)
    # 128 位全反 → 极端不像 → 拒绝
    assert d.get_verified("s:CMEX10|18", "f" * 64) is None
    # 完全相同 → 采信
    assert d.get_verified("s:CMEX10|18", base) == "×"


def test_shape_guard_threshold_matches_calibration():
    """二级校验阈值必须落在实测标定的安全区间内。

    标定（全库 24 篇 / 6184 实例，生产渲染代码）：
      同语义键·跨文档 形状差 max = 81
      → 阈值必须 ≥ 81，否则会误杀"同一个符号"
    """
    assert gl._SHAPE_GUARD_MAX_DIFF >= 81, \
        f"阈值 {gl._SHAPE_GUARD_MAX_DIFF} < 实测最大同键差 81，会误杀同符号"


def test_stable_shape_is_more_size_invariant_than_stretch():
    """等比补白版比拉伸版更抗字号变化（这是它当二级校验的前提）。

    ⚠️ 必须用**真实 PDF 字形**来验（合成图会把抗锯齿/密度差异放大成假信号）——
    AGENTS.md 教训：阈值要在真实产物上校准。
    """
    root = Path(__file__).resolve().parents[1]
    hits = list((root / "01_Literature").rglob("2008-JAP-multiferroelectrics-review.pdf"))
    if not hits:
        pytest.skip("vault 里没有 JAP 综述，跳过真实字形测试")
    pdf = hits[0]

    from microbench import paper_text as pt
    _text, glyphs = pt.extract_page_text_with_glyphs(pdf, 1)
    if not glyphs:
        pytest.skip("第 1 页没有解不出的符号")
    doc, _ = gl._open_doc(pdf)
    try:
        g = glyphs[0]
        a_stretch = gl._shape_bits(gl._render_glyph(doc, 1, g, zoom=12.0))
        b_stretch = gl._shape_bits(gl._render_glyph(doc, 1, g, zoom=20.0))
        a_stable = gl._shape_bits_stable(gl._render_glyph(doc, 1, g, zoom=12.0))
        b_stable = gl._shape_bits_stable(gl._render_glyph(doc, 1, g, zoom=20.0))
    finally:
        doc.close()

    assert a_stable and b_stable, "稳定指纹不该是空的"
    d_stretch = gl._hamming(a_stretch, b_stretch)
    d_stable = gl._hamming(a_stable, b_stable)
    assert d_stable <= d_stretch, \
        f"等比补白版应更稳：stretch={d_stretch} stable={d_stable}"


def test_dictionary_legacy_shape_entries_still_readable(tmp_path):
    """旧格式（纯 hex 形状键 → 符号）必须仍能读（两台设备共用同一个词典文件）。"""
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"deadbeef": "×"}), encoding="utf-8")
    d = gl.GlyphDictionary(path).load()
    assert d.get("deadbeef") == "×"


def test_dictionary_refuses_symbol_outside_whitelist(tmp_path):
    d = gl.GlyphDictionary(tmp_path / "g.json")
    d.put("s:CMEX10|18", "文", "abc")
    assert d.get("s:CMEX10|18") is None


# ---------------------------------------------------------------------------
# 3) 整篇批量解析：一次模型调用处理全篇（不是每页一次）
# ---------------------------------------------------------------------------

def _fake_page(page_no, texts, glyphs):
    return {"page": page_no, "text": texts, "glyphs": glyphs}


def _glyph(font, cid, x=10.0):
    return {"font": font, "cid": cid, "x0": x, "x1": x + 8,
            "top": 10.0, "bottom": 18.0}


class _FakeDoc:
    """够用的假 fitz.Document（只提供 close）。"""

    def close(self):
        pass


def _real_png(w: int = 48, h: int = 48) -> bytes:
    """一张**合法**的 PNG（有墨迹），让形状指纹算得出非空值。"""
    from PIL import Image
    import io
    img = Image.new("L", (w, h), 255)
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = 0 if 8 <= x < w - 8 and 8 <= y < h - 8 else 255
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _patch_render(monkeypatch, png=None):
    """把字形渲染固定成同一张图 → 形状指纹恒定，测试聚焦在调用次数上。"""
    png = png if png is not None else _real_png()
    monkeypatch.setattr(gl, "_render_glyph",
                        lambda doc, page, bbox, zoom=12.0: png)
    monkeypatch.setattr(gl, "_open_doc", lambda src: (_FakeDoc(), False))


def test_document_resolution_makes_one_model_call_for_all_pages(monkeypatch, tmp_path):
    """3 页、每页同一个符号 → **只调 1 次**模型（旧实现是 3 次）。

    这条是本次回归的核心护栏：把整篇解析改回逐页，它必须变红。
    """
    calls = []

    def fake_recognize(strip, count, model_name="", force_fallback=False, deadline=None, isolated=False):
        calls.append(count)
        # 认 1 个符号 = ×
        return "1=×"

    monkeypatch.setattr(gl, "_recognize_batch", fake_recognize)
    _patch_render(monkeypatch)

    pages = [
        _fake_page(1, "a⟦G:0⟧b", [_glyph("CMEX10", "18")]),
        _fake_page(2, "c⟦G:0⟧d", [_glyph("CMEX10", "18")]),
        _fake_page(3, "e⟦G:0⟧f", [_glyph("CMEX10", "18")]),
    ]
    stats = gl.resolve_document_glyphs(pages, b"pdfbytes", dict_path=tmp_path / "g.json")

    assert len(calls) == 1, f"整篇只该调 1 次模型，实际 {len(calls)} 次"
    assert stats["model_calls"] == 1
    # 所有页都被替换成真符号
    assert "⟦G:" not in "".join(p["text"] for p in pages)
    assert all("×" in p["text"] for p in pages)


def test_document_resolution_uses_dictionary_without_model_call(monkeypatch, tmp_path):
    """词典命中 → 零模型调用（跨论文复用的收益）。"""
    calls = []

    def fake_recognize(strip, count, model_name="", force_fallback=False, deadline=None, isolated=False):
        calls.append(count)
        return "1=×"

    monkeypatch.setattr(gl, "_recognize_batch", fake_recognize)
    _patch_render(monkeypatch)

    d = gl.GlyphDictionary(tmp_path / "g.json")
    d.put("s:CMEX10|18", "×", gl._shape_bits_stable(b"PNGDATA"))
    d.save()

    pages = [_fake_page(1, "a⟦G:0⟧b", [_glyph("CMEX10", "18")])]
    stats = gl.resolve_document_glyphs(pages, b"pdfbytes", dict_path=tmp_path / "g.json")

    assert calls == [], "词典命中不该再调模型"
    assert stats["model_calls"] == 0
    assert "×" in pages[0]["text"]


def test_document_resolution_falls_back_to_placeholder(monkeypatch, tmp_path):
    """模型失败 → 退回 "[?]"，且内部标记 ⟦G:n⟧ 绝不外泄。"""
    def boom(strip, count, model_name="", force_fallback=False, deadline=None):
        raise RuntimeError("HTTP 429 限流")

    monkeypatch.setattr(gl, "_recognize_batch", boom)
    _patch_render(monkeypatch)

    pages = [_fake_page(1, "a⟦G:0⟧b", [_glyph("CMEX10", "18")])]
    stats = gl.resolve_document_glyphs(pages, b"pdfbytes", dict_path=tmp_path / "g.json")

    assert stats["failed"] is True
    assert "⟦G:" not in pages[0]["text"], "内部标记漏出去了"
    assert "[?]" in pages[0]["text"]


def test_document_resolution_survives_broken_pdf(monkeypatch, tmp_path):
    """**PDF 打不开**时也不能漏标记（这是本次测试发现的真实缺陷）。

    旧写法把回填放在 try 里 → 打开 PDF 失败就整段跳过 →
    ⟦G:n⟧ 原样进 prompt → 模型照抄/瞎编（AGENTS.md §4.6 红线）。
    """
    def boom_open(src):
        raise RuntimeError("Failed to open stream")

    monkeypatch.setattr(gl, "_open_doc", boom_open)

    pages = [_fake_page(1, "a⟦G:0⟧b", [_glyph("CMEX10", "18")])]
    stats = gl.resolve_document_glyphs(pages, b"pdfbytes", dict_path=tmp_path / "g.json")

    assert stats["failed"] is True
    assert "⟦G:" not in pages[0]["text"], "内部标记漏出去了（PDF 打不开时）"
    assert "[?]" in pages[0]["text"]


def test_document_resolution_respects_deadline(monkeypatch, tmp_path):
    """超出时间预算 → 不再发新的模型调用（不能因为降格而无限等）。"""
    import time

    calls = []

    def fake_recognize(strip, count, model_name="", force_fallback=False, deadline=None, isolated=False):
        calls.append(count)
        return "1=×"

    monkeypatch.setattr(gl, "_recognize_batch", fake_recognize)
    _patch_render(monkeypatch)

    pages = [
        _fake_page(1, "a⟦G:0⟧b", [_glyph("CMEX10", str(i)) for i in range(25)]),
    ]
    # 预算已经过了 → 不该发任何调用
    stats = gl.resolve_document_glyphs(pages, b"pdfbytes",
                                      dict_path=tmp_path / "g.json",
                                      deadline=time.monotonic() - 1)
    assert calls == [], "预算用尽后不该再调模型"
    assert stats["failed"] is True
    assert "⟦G:" not in pages[0]["text"]


def test_recognize_false_does_not_call_model(monkeypatch, tmp_path):
    """recognize=False → 只查词典，**零模型调用**（打开论文走这条）。

    为什么重要：打开论文必须亚秒级返回；而且认字失败会触发全局降格，
    连累用户后续的翻译（AGENTS.md：一次任务的毛病不该拖累别的请求）。
    """
    calls = []

    def fake_recognize(strip, count, model_name="", force_fallback=False, deadline=None, isolated=False):
        calls.append(count)
        return "1=×"

    monkeypatch.setattr(gl, "_recognize_batch", fake_recognize)
    _patch_render(monkeypatch)

    pages = [_fake_page(1, "a⟦G:0⟧b", [_glyph("CMEX10", "18")])]
    stats = gl.resolve_document_glyphs(pages, b"pdfbytes",
                                      dict_path=tmp_path / "g.json",
                                      recognize=False)
    assert calls == [], "recognize=False 不该调模型"
    assert stats["model_calls"] == 0
    # 没认出来 → "[?]"，但标记不能外泄
    assert "⟦G:" not in pages[0]["text"]
    assert "[?]" in pages[0]["text"]


def test_recognize_false_uses_dictionary(monkeypatch, tmp_path):
    """recognize=False 时，词典命中的符号**照常替换**（不用等模型）。"""
    calls = []

    def fake_recognize(strip, count, model_name="", force_fallback=False, deadline=None, isolated=False):
        calls.append(count)
        return "1=×"

    monkeypatch.setattr(gl, "_recognize_batch", fake_recognize)
    _patch_render(monkeypatch)

    d = gl.GlyphDictionary(tmp_path / "g.json")
    d.put("s:CMEX10|18", "×", gl._shape_bits_stable(_real_png()))
    d.save()

    pages = [_fake_page(1, "a⟦G:0⟧b", [_glyph("CMEX10", "18")])]
    gl.resolve_document_glyphs(pages, b"pdfbytes",
                              dict_path=tmp_path / "g.json", recognize=False)
    assert calls == []
    assert "×" in pages[0]["text"]


# ---------------------------------------------------------------------------
# 4) 真实 PDF 回归：整篇解析后不留内部标记
# ---------------------------------------------------------------------------

def _jap_pdf():
    root = Path(__file__).resolve().parents[1]
    hits = list((root / "01_Literature").rglob("2008-JAP-multiferroelectrics-review.pdf"))
    if not hits:
        pytest.skip("vault 里没有 JAP 综述，跳过真实 PDF 测试")
    return hits[0]


def test_real_document_has_far_fewer_unique_symbols_than_instances():
    """真实长论文：字形实例数 ≫ 不同符号数。

    这正是"整篇一次"能把 35 次模型调用压到 1~3 次的原因。
    """
    pdf = _jap_pdf()
    pages = pt.extract_text_from_pdf(pdf.read_bytes())
    instances = sum(len(p.get("glyphs") or []) for p in pages)
    if instances == 0:
        pytest.skip("这篇没有解不出的符号")
    unique = set()
    for p in pages:
        for g in p.get("glyphs") or []:
            unique.add(gl.semantic_key(g.get("font", ""), g.get("cid")))
    assert instances > 100, f"实例数应很多，实际 {instances}"
    assert len(unique) < instances / 10, \
        f"不同符号数应远小于实例数，实际 {len(unique)} vs {instances}"


def test_real_document_resolution_never_leaks_markers(tmp_path):
    """整篇解析（不调模型：词典没命中就退回 [?]）后，章节文本里没有内部标记。"""
    pdf = _jap_pdf()
    pdf_bytes = pdf.read_bytes()
    pages = pt.extract_text_from_pdf(pdf_bytes)
    if not any(p.get("glyphs") for p in pages):
        pytest.skip("这篇没有解不出的符号")

    gl.resolve_document_glyphs(pages, pdf_bytes, dict_path=tmp_path / "g.json")
    blob = "".join(p["text"] for p in pages)
    assert "⟦G:" not in blob, "内部标记 ⟦G:n⟧ 漏出去了（会原样进 prompt）"


# ---------------------------------------------------------------------------
# 5) 端点级护栏：上传/打开接口必须走**整篇一次**（不是逐页）
# ---------------------------------------------------------------------------

def test_upload_endpoint_uses_document_level_glyph_resolution(monkeypatch):
    """上传接口 → 整篇解析只调 1 次（改成逐页必须变红）。

    这条是本次问题的**端点级**护栏：单元测试只测 resolve_document_glyphs 本身，
    拦不住"接口又改回逐页调用"。

    ⚠️ 关键：必须**强制**两条路径都被触发到 —— 否则测试会因为
    "这份最小 PDF 里没有解不出的符号 → 逐页循环压根没进"而**假绿**
    （AGENTS.md：恒真的断言等于没写）。
    """
    from microbench import app as app_mod

    calls = []

    async def fake_doc_resolve(pages, pdf_source):
        calls.append(("document", len(pages)))
        for p in pages:
            if p.get("glyphs"):
                p["text"] = gl.strip_markers(p.get("text") or "")
        return {"model_calls": 0}

    monkeypatch.setattr(app_mod, "_resolve_document_glyphs", fake_doc_resolve)

    # 逐页那条老路：一旦接口改回它，这里就会被调用 → 断言失败
    async def fake_page_resolve(text, glyphs, pdf_source, page_no, model=""):
        calls.append(("page", page_no))
        return gl.strip_markers(text)

    monkeypatch.setattr(app_mod, "_resolve_page_glyphs", fake_page_resolve)

    # 造一页**带字形**的 PDF：让抽取层真的产出 glyphs（否则两条路径都不会进）
    real_extract = pt.extract_text_from_pdf

    def fake_extract(pdf_bytes):
        pages = real_extract(pdf_bytes)
        for p in pages:
            p["glyphs"] = [{"font": "CMEX10", "cid": "18",
                             "x0": 10.0, "x1": 18.0,
                             "top": 10.0, "bottom": 18.0}]
            p["text"] = (p.get("text") or "") + " ⟦G:0⟧"
        return pages

    monkeypatch.setattr(app_mod, "extract_text_from_pdf", fake_extract)

    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post(
        "/api/paper/upload_pdf?topic_category=01_Device_TCAD_器件仿真&title=Revert Check",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200, r.text

    doc_calls = [c for c in calls if c[0] == "document"]
    page_calls = [c for c in calls if c[0] == "page"]
    assert doc_calls, "整篇解析压根没被调用（测试没打到点上）"
    assert not page_calls, \
        f"接口又改回逐页解析了：{len(page_calls)} 次逐页调用（每页一次多模态请求 → 打开 8 分钟）"


def test_open_endpoint_does_not_call_model(monkeypatch):
    """打开论文**不许调模型** —— 这是"半天没反应"的直接保证。

    打开必须亚秒级返回：符号认不出来显示 [?] 完全可以接受，
    而认字失败还会触发全局降格，连累用户后续的翻译。
    """
    from microbench import app as app_mod

    calls = []

    def fake_recognize(strip, count, model_name="", force_fallback=False, deadline=None, isolated=False):
        calls.append(count)
        return "1=×"

    monkeypatch.setattr(gl, "_recognize_batch", fake_recognize)
    monkeypatch.setattr(app_mod.glyphs_mod, "_recognize_batch", fake_recognize)

    # 后台补齐也不能在测试里真的发请求
    monkeypatch.setattr(app_mod, "_schedule_glyph_backfill", lambda b: None)

    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post(
        "/api/paper/upload_pdf?topic_category=01_Device_TCAD_器件仿真&title=NoModel",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    assert calls == [], f"打开论文不该调模型，实际调了 {len(calls)} 次"


def _pdf_bytes() -> bytes:
    """一份最小的、带可抽取文本的 PDF。"""
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]"
        b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length 62>>stream\n"
        b"BT /F1 18 Tf 40 140 Td (Hello MicroBench) Tj ET\n"
        b"endstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
        b"startxref 0\n%%EOF\n"
    )
    return pdf
