"""裁图防邻居 + 认字清单与白名单一致（2026-10-26 的护栏）。

背景：裁字形时的 padding 太大 → 窄字形（2~3pt 宽）会把**相邻字符**裁进来
→ 模型看到的是"ω𝑓"这种混合图 → 认错或认不出。
实测（2008-JAP 综述 46 个语义键）：pad=1.0 时 14/46 混入邻居。

这些测试把「标定出来的结论」钉死，防止有人把 padding 改回去。
"""
import re
import sys
from pathlib import Path

import pytest

from microbench import glyphs as gl
from microbench import paper_text as pt


# ---------------------------------------------------------------------------
# 1) padding：必须小，且有上限（防切掉字形）
# ---------------------------------------------------------------------------

def test_glyph_pad_is_small():
    """裁图外扩量必须小 —— 太大就把相邻字符裁进来了。

    实测标定（46 个语义键，判据=别的字形有多少面积落进裁剪框）：
      pad=1.0  → 14/46 混入邻居
      pad=0.25 → 9/46
      pad=0.10 → 7/46
    """
    assert gl._GLYPH_PAD_PT <= 0.3, \
        f"padding={gl._GLYPH_PAD_PT} 太大，窄字形会把邻居裁进来（实测 ≥1.0 时 14/46 混入）"
    assert gl._GLYPH_PAD_PT >= 0.1, \
        f"padding={gl._GLYPH_PAD_PT} 太小，可能把字形切掉（实测 pad=0 时墨迹左溢 ≤0.75pt 是抗锯齿边缘）"


def test_render_glyph_uses_the_calibrated_pad():
    """_render_glyph 必须真的用这个 padding（别出现"常量改了但没用"）。"""
    import inspect
    src = inspect.getsource(gl._render_glyph)
    assert "pad: float = _GLYPH_PAD_PT" in src, "签名没用 _GLYPH_PAD_PT"
    assert "x0 - pad" in src and "x1 + pad" in src, "裁剪框没用 pad"
    assert "x0 - 1.0" not in src, "还残留着写死的 ±1.0"


# ---------------------------------------------------------------------------
# 2) 认字清单里的符号必须全在白名单里（否则模型的一次有效输出被整个丢掉）
# ---------------------------------------------------------------------------

def test_hint_prompt_symbols_are_all_whitelisted():
    """提示词清单里的每个符号都必须在 ALLOWED_SYMBOLS 里。

    不在白名单 → 模型选它 → 词典 put() 丢弃 → 白白浪费一次调用。
    """
    seg = gl._RECOGNIZE_PROMPT_WITH_HINTS
    # 只看数学/希腊/箭头区的字符（跳过中文说明和标点）
    chars = set(re.findall(
        r"[\u2200-\u22FF\u2190-\u21FF\u0370-\u03FF\u00B0\u2032\u2033"
        r"\u00B7\u2219\u2225\u2308-\u230B\u2267]", seg))
    bad = sorted(c for c in chars if c not in gl.ALLOWED_SYMBOLS)
    assert not bad, f"提示词里有不在白名单的符号（模型选了会被丢弃）: {bad}"


def test_bracket_symbols_are_whitelisted():
    """括号类必须在白名单里。

    实测：JAP 综述里有一大批语义键是**大括号/圆括号/方括号/竖线**
    （TeX 数学字体拿来分组、取整、绝对值），
    它们原本**不在白名单** → 模型认对了也会被丢 → 译文里还是 [?]。
    """
    for ch in "()[]{}|":
        assert ch in gl.ALLOWED_SYMBOLS, f"括号 {ch!r} 不在白名单里"


def test_variant_relation_symbols_are_whitelisted():
    """模型爱给"变体"（≧ 而不是 ≥）—— 不收就等于白认一次。"""
    for ch in "≧≪≫":
        assert ch in gl.ALLOWED_SYMBOLS


# ---------------------------------------------------------------------------
# 3) 词典：白名单收紧后仍能正确过滤
# ---------------------------------------------------------------------------

def test_dictionary_rejects_non_whitelisted(tmp_path):
    """白名单是最后一道闸 —— 不在里面的字符一律不收。"""
    d = gl.GlyphDictionary(tmp_path / "g.json")
    for bad in ("文", "字", "a", "1"):
        d.put("s:F|1", bad, "abc")
        assert d.get("s:F|1") is None, f"不该收下 {bad!r}"


def test_dictionary_accepts_brackets(tmp_path):
    d = gl.GlyphDictionary(tmp_path / "g.json")
    d.put("s:F|1", "}", "abc")
    assert d.get("s:F|1") == "}"


# ---------------------------------------------------------------------------
# 4) 真实产物：窄字形的裁图不能带邻居（真回归，不靠合成样本）
# ---------------------------------------------------------------------------

def _jap_pdf():
    root = Path(__file__).resolve().parents[1]
    hits = list((root / "01_Literature").rglob("2008-JAP-multiferroelectrics-review.pdf"))
    if not hits:
        pytest.skip("vault 里没有 JAP 综述，跳过真实字形测试")
    return hits[0]


def test_real_narrow_glyphs_do_not_swallow_neighbours():
    """真实论文里的窄字形（2~3pt 宽）裁图**不能**带进相邻字符。

    判据（用 PDF 自己的信息，不靠肉眼）：
    页面上每个字形有自己的 x 区间；裁剪框若与**别的**字形有面积重叠
    （≥30% 的面积落进框内），就是带邻居了。
    """
    import io as _io
    import pdfplumber
    from PIL import Image

    pdf = _jap_pdf()
    data = pdf.read_bytes()
    pages = pt.extract_text_from_pdf(data)
    with pdfplumber.open(_io.BytesIO(data)) as pl:
        page_chars = {i + 1: getattr(p, "chars", []) for i, p in enumerate(pl.pages)}

    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        seen = set()
        contaminated = 0
        checked = 0
        for p in pages:
            for g in p.get("glyphs") or []:
                sk = gl.semantic_key(g.get("font", ""), g.get("cid"))
                if sk in seen:
                    continue
                seen.add(sk)
                gx0, gx1 = float(g["x0"]), float(g["x1"])
                gt, gb = float(g["top"]), float(g["bottom"])
                # 只看窄字形（这类最容易被邻居污染）
                if gx1 - gx0 > 6.0:
                    continue
                pad = gl._GLYPH_PAD_PT
                cx0, cx1 = gx0 - pad, gx1 + pad
                ct0, ct1 = gt - pad, gb + pad
                png = gl._render_glyph(doc, p["page"], g)
                if not png:
                    continue
                checked += 1
                img = Image.open(_io.BytesIO(png)).convert("L")
                inv = img.point(lambda q: 255 - q)
                w, h = img.size
                cols = [x for x in range(w)
                        if any(inv.getpixel((x, y)) > 128 for y in range(h))]
                if not cols:
                    continue
                # 墨迹在页面上的 x 范围
                mx0 = gx0 - pad + cols[0] / 12.0
                mx1 = gx0 - pad + cols[-1] / 12.0
                for c in page_chars.get(p["page"], []):
                    ox0, ox1 = float(c["x0"]), float(c["x1"])
                    ot0, ot1 = float(c["top"]), float(c["bottom"])
                    ix = min(cx1, ox1) - max(cx0, ox0)
                    iy = min(ct1, ot1) - max(ct0, ot0)
                    if ix <= 0 or iy <= 0:
                        continue
                    own = max(1e-6, (ox1 - ox0) * (ot1 - ot0))
                    if ix * iy / own >= 0.3:
                        is_self = (abs(ox0 - gx0) < 0.6 and abs(ox1 - gx1) < 0.6)
                        if not is_self:
                            contaminated += 1
                            break
    finally:
        doc.close()

    assert checked > 0, "没找到窄字形，测试没打到点上"
    # 标定结论：pad=0.25 时窄字形里"严重混入"的应该很少
    assert contaminated * 1.0 / max(1, checked) < 0.35, \
        f"窄字形混入邻居的比例 {contaminated}/{checked} 太高（padding={gl._GLYPH_PAD_PT}）"
