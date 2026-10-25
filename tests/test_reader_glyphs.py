"""「解不出的数学符号」的护栏。

用户反馈（2026-10-25）："只要涉及 × 的，它都显示的是个？" ——
论文里的 ×、≈、≥ 在译文里全变成 "[?]"。

修法：抽取层给**孤立**的解不出符号打上锚点标记 ⟦G:n⟧ → 服务端裁图让多模态
认字 → 存进符号词典（按形状指纹）→ 之后查表即可。

这里测的是**不依赖网络**的那部分：形状指纹、回答解析、白名单、
词典读写、抽取层的标记规则。
"""
import json

import pytest

from microbench import glyphs as gl
from microbench import paper_text as pt


# ---------------------------------------------------------------------------
# 1) 形状指纹：同一形状 → 同一把 key
# ---------------------------------------------------------------------------

def _png_from_gray(blocks):
    """用 PIL 拼一张"像素块"图（模拟字形位图）。"""
    from PIL import Image
    img = Image.new("L", (len(blocks[0]) * 4, len(blocks) * 4), 255)
    px = img.load()
    for r, row in enumerate(blocks):
        for c, v in enumerate(row):
            for dy in range(4):
                for dx in range(4):
                    px[c * 4 + dx, r * 4 + dy] = v
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_shape_bits_is_stable_for_same_shape():
    a = _png_from_gray([[0, 255, 0], [255, 0, 255]])
    b = _png_from_gray([[0, 255, 0], [255, 0, 255]])
    assert gl._shape_bits(a) == gl._shape_bits(b)
    assert gl._shape_bits(a)


def test_shape_bits_differs_for_different_shape():
    a = _png_from_gray([[0, 255, 0], [255, 0, 255]])
    b = _png_from_gray([[0, 0, 0], [0, 0, 0]])
    assert gl._hamming(gl._shape_bits(a), gl._shape_bits(b)) > gl._MATCH_MAX_DIFF


def _jacers_pdf():
    """vault 里那篇 JACERS 综述（里面有 ≈ ≥ × 三个真符号）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    hits = list((root / "01_Literature").rglob("2008-JACERS-*.pdf"))
    if not hits:
        pytest.skip("vault 里没有 JACERS 综述，跳过真实字形测试")
    return hits[0]


def _real_glyphs():
    """JACERS 综述第 1 页那三个真符号（≈ ≥ ×）。"""
    from microbench import glyphs as gl
    from microbench import paper_text as pt
    pdf = _jacers_pdf()
    _text, glyphs = pt.extract_page_text_with_glyphs(pdf, 1)
    if len(glyphs) < 3:
        pytest.skip("这一页的符号数不足，跳过")
    doc, _ = gl._open_doc(pdf)
    try:
        out = []
        for g in glyphs[:3]:
            out.append(gl._render_glyph(doc, 1, g, zoom=12.0))
    finally:
        doc.close()
    return out


def test_real_glyph_matches_itself_at_different_zoom():
    """同一个真字形渲染在不同倍率 → 指纹必须落在容差内（词典要能命中）。"""
    from microbench import glyphs as gl
    from microbench import paper_text as pt
    pdf = _jacers_pdf()
    _text, glyphs = pt.extract_page_text_with_glyphs(pdf, 1)
    if len(glyphs) < 3:
        pytest.skip("这一页的符号数不足，跳过")
    doc, _ = gl._open_doc(pdf)
    try:
        for g in glyphs[:3]:
            a = gl._shape_bits(gl._render_glyph(doc, 1, g, zoom=12.0))
            b = gl._shape_bits(gl._render_glyph(doc, 1, g, zoom=20.0))
            d = gl._hamming(a, b)
            assert d <= gl._MATCH_MAX_DIFF, f"同一字形不同倍率差异 {d} > {gl._MATCH_MAX_DIFF}"
    finally:
        doc.close()


def test_real_glyphs_are_mutually_distinct():
    """三个不同的真符号（≈ ≥ ×）必须彼此区分得开（不能互相认错）。"""
    pings = [p for p in _real_glyphs() if p]
    if len(pings) < 3:
        pytest.skip("没裁出三个字形，跳过")
    from microbench import glyphs as gl
    bits = [gl._shape_bits(p) for p in pings]
    for i in range(len(bits)):
        for j in range(i + 1, len(bits)):
            d = gl._hamming(bits[i], bits[j])
            assert d > gl._MATCH_MAX_DIFF, f"两个不同符号只差 {d} 位（会认错）"


def test_shape_bits_of_blank_is_empty():
    """空白图没有形状 → 空（不能进词典，否则所有空白都变成同一个符号）。"""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("L", (16, 16), 255).save(buf, format="PNG")
    assert gl._shape_bits(buf.getvalue()) == ""


def test_dictionary_matches_near_shape(tmp_path):
    """词典要能认出"很接近"的形状（字号不同也认得出）。"""
    d = gl.GlyphDictionary(tmp_path / "g.json")
    bits = gl._shape_bits(_png_from_gray([[0, 255, 0], [255, 0, 255]]))
    d.put(bits, "×")
    near = gl._shape_bits(_png_from_gray([[0, 255, 0], [255, 0, 250]]))
    assert d.get(near) == "×"
    far = gl._shape_bits(_png_from_gray([[0, 0, 0], [0, 0, 0]]))
    assert d.get(far) is None


# ---------------------------------------------------------------------------
# 2) 模型回答的解析 + 白名单
# ---------------------------------------------------------------------------

def test_parse_numbered_answer():
    got = gl._parse_recognition("1=×, 2=≈, 3=≥", 3)
    assert got == {1: "×", 2: "≈", 3: "≥"}


def test_parse_plain_list_answer():
    got = gl._parse_recognition("×, ≈, ≥", 3)
    assert got == {1: "×", 2: "≈", 3: "≥"}


def test_parse_rejects_symbols_outside_whitelist():
    """模型说了一个不在白名单里的字符 → 必须丢弃（防它胡编）。"""
    got = gl._parse_recognition("1=文, 2=字, 3=符", 3)
    assert got == {}


def test_parse_rejects_multi_char_and_garbage():
    assert gl._parse_recognition("1=\\approx, 2=??", 2) == {}


def test_parse_handles_question_mark_as_unknown():
    got = gl._parse_recognition("1=×, 2=?", 2)
    assert got == {1: "×"}


# ---------------------------------------------------------------------------
# 3) 词典：只收白名单单字符，可持久化
# ---------------------------------------------------------------------------

def test_dictionary_roundtrip(tmp_path):
    path = tmp_path / "glyph_dict.json"
    d = gl.GlyphDictionary(path)
    d.put("k1", "×")
    d.put("k2", "≈")
    d.save()
    again = gl.GlyphDictionary(path).load()
    assert again.get("k1") == "×"
    assert again.get("k2") == "≈"
    assert again.get("nope") is None


def test_dictionary_refuses_bad_entries(tmp_path):
    d = gl.GlyphDictionary(tmp_path / "g.json")
    d.put("k", "文字")          # 多字符
    d.put("k2", "文")           # 不在白名单
    assert d.get("k") is None
    assert d.get("k2") is None


def test_dictionary_survives_corrupt_file(tmp_path):
    path = tmp_path / "glyph_dict.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    d = gl.GlyphDictionary(path).load()
    assert d.data == {}


# ---------------------------------------------------------------------------
# 4) 未识别的标记必须退回 "[?]"，模型绝不能看见内部标记
# ---------------------------------------------------------------------------

def test_strip_markers():
    assert gl.strip_markers("a⟦G:3⟧b") == "a[?]b"
    assert gl.strip_markers("没有标记") == "没有标记"


# ---------------------------------------------------------------------------
# 5) 抽取层：孤立符号给锚点，连续一串保持整体占位
# ---------------------------------------------------------------------------

def test_isolated_cid_gets_anchor():
    line = {"text": "up to 500(cid:1) larger", "x0": 0, "x1": 100,
            "top": 10, "bottom": 20}
    glyphs = [{"font": "F", "cid": "1", "x0": 40, "x1": 50, "top": 11, "bottom": 19}]
    out = pt._mark_line_glyphs(line, glyphs)
    assert "⟦G:0⟧" in out
    assert "(cid:" not in out


def test_consecutive_cids_stay_one_placeholder():
    """连续一串（公式碎片 / 引用编号）→ 整体一个 "[?]"，不给模型灌一堆标记。"""
    line = {"text": "Ref (cid:1) (cid:2) (cid:3) done", "x0": 0, "x1": 100,
            "top": 10, "bottom": 20}
    glyphs = [{"font": "F", "cid": str(i), "x0": 10 + i * 8, "x1": 16 + i * 8,
               "top": 11, "bottom": 19} for i in range(3)]
    out = pt._mark_line_glyphs(line, glyphs)
    assert out.count("[?]") == 1
    assert "⟦G:" not in out


def test_unmatched_cid_falls_back_to_placeholder():
    """对不上字形 → "[?]"，宁可空着也不能指错。"""
    line = {"text": "x (cid:9) y", "x0": 0, "x1": 100, "top": 10, "bottom": 20}
    out = pt._mark_line_glyphs(line, [])
    assert "[?]" in out
    assert "⟦G:" not in out


def test_marker_regex_and_version():
    assert pt.GLYPH_MARKER_RE.search("⟦G:12⟧")
    assert pt.EXTRACTOR_VERSION >= 5, "抽取算法改了就必须 +1（否则旧快照不作废）"
