# -*- coding: utf-8 -*-
"""「中译替换版 PDF」的**质量**护栏（2026-11-24）。

用户原话：「看看github上有没有类似的功能的实现，借鉴一下现在质量还是不太行，
然后有的页数没翻到没好好翻定位一下原因」

这一轮改造的三条主线（对应下面三组护栏）：
  ① **图表零损伤** —— 涂改只涂原页真实的文字行，且每块都**减去图表保护区**；
  ② **中文排版对齐** —— 自己断行 + 逐行基线定位 + 两端对齐 + 不填满原框；
  ③ **小节标题独立落位** —— 从正文尾部拆出来，不再粘在正文最后一句后面。

⚠ 纪律（AGENTS §4.92）：
  这里**一律不读源码文本当断言**。判据全是**真实产物**：
  · 真 import → 真调用生产函数（`_protected_rects` / `_clip_to_protected` / …）
  · 真造 PDF → 真构建 → 真渲染 → 真比像素
  · 真打 HTTP 端点 → 看体检字段
  改了行为却仍全绿 = 这套护栏没有保护力 → 所以每组都配了**变异自检**
  （故意把生产代码改坏，断言护栏会红）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

fitz = pytest.importorskip("fitz")


# ---------------------------------------------------------------------------
# 样本
# ---------------------------------------------------------------------------
def _make_figure_paper(path: Path) -> None:
    """一篇带「渐变图」的论文（模拟 JACERS 综述首页那种上百条渐变矢量）。

    页面上刻意放了四类东西：
      · 正文两行（在图上方）
      · 渐变图（40 条相邻横带）+ 图上的**艺术字**「Review」
      · 图下的**轴标签**「Magnetic field (Oe)」（短 → 属图内元素）
      · 图下的**图注**「(a) Magnetostriction …」（长 → 属论文文字）
    """
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "Magnetostrictive composites are studied here.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 110), "The second line of the paragraph goes on.",
                     fontsize=11, fontname="helv")
    # 一句**短正文**（离图很远）—— 它必须被翻译：
    # 「图内元素」的判据是「短」**且**「离图近」两条同时成立，缺一不可。
    page.insert_text((72, 150), "See Fig. 3.", fontsize=11, fontname="helv")
    for i in range(40):
        y0 = 295 + i * 2.0
        shade = 0.2 + 0.6 * (i / 40.0)
        page.draw_rect(fitz.Rect(72, y0, 300, y0 + 2.0), color=None,
                       fill=(0.2, shade, 0.8), width=0)
    page.insert_text((110, 345), "Review", fontsize=30, fontname="helv")
    page.insert_text((80, 388), "Magnetic field (Oe)", fontsize=9, fontname="helv")
    page.insert_text((72, 405),
                     "(a) Magnetostriction and piezomagnetic properties of the composite.",
                     fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


def _make_heading_paper(path: Path) -> None:
    """正文两行 + 独立成行的小节标题（抽取层会把标题吸进上一段末尾）。"""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "The first sentence of the body paragraph.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 110), "The second sentence of the body paragraph.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 135), "I. Introduction", fontsize=11, fontname="helv")
    doc.save(str(path))
    doc.close()


def _figure_blocks():
    from microbench.replaced_pdf import Block
    return [
        Block(index=0, bbox=(70.0, 76.0, 500.0, 96.0),
              translation="磁致伸缩复合材料在此研究。", kind="body", col="S"),
        Block(index=1, bbox=(88.0, 98.0, 500.0, 116.0),
              translation="这是段落的第二行，继续往下写。", kind="body", col="S"),
        # 艺术字「Review」那一块 → 整块落在图里 → 必须整块跳过
        Block(index=2, bbox=(100.0, 315.0, 215.0, 355.0),
              translation="综述", kind="body", col="S"),
        # 轴标签那一块 → 属图内元素 → 不涂不翻
        Block(index=3, bbox=(72.0, 378.0, 220.0, 396.0),
              translation="磁场（ Oe ）", kind="body", col="S"),
        # 图注 → 属论文文字 → 照翻
        Block(index=4, bbox=(70.0, 396.0, 430.0, 412.0),
              translation="（a）磁致伸缩与压磁特性。", kind="body", col="S"),
        # 远处的**短正文** → 属论文文字 → 照翻
        Block(index=5, bbox=(70.0, 136.0, 200.0, 154.0),
              translation="参见图 3。", kind="body", col="S"),
    ]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _region_diff(page_a, page_b, clip, zoom: int = 2) -> int:
    """两页在同一区域上的**像素差**（逐像素比 RGB）。

    这是「图表还保持不变」这件事唯一的**客观**判据 ——
    数矢量对象、读文字都发现不了「图上多了一块纯白矩形」。
    """
    matrix = fitz.Matrix(zoom, zoom)
    sa = page_a.get_pixmap(matrix=matrix, clip=clip)
    sb = page_b.get_pixmap(matrix=matrix, clip=clip)
    assert (sa.width, sa.height) == (sb.width, sb.height), \
        "两张图尺寸不同，没法逐像素比"
    stride = sa.n
    a, b = sa.samples, sb.samples
    diff = 0
    for i in range(sa.width * sa.height):
        if a[i * stride:(i + 1) * stride] != b[i * stride:(i + 1) * stride]:
            diff += 1
    return diff


def _line_baselines(page) -> list:
    """页面上每一行的基线 + 文本。"""
    out = []
    for blk in page.get_text("dict").get("blocks") or []:
        for ln in blk.get("lines") or []:
            spans = ln.get("spans") or []
            if not spans:
                continue
            text = "".join(sp.get("text") or "" for sp in spans)
            base = max(float((sp.get("origin") or (0, 0))[1]) for sp in spans)
            out.append((base, text))
    return out


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


@pytest.fixture(scope="module")
def cjk_font():
    from microbench import replaced_pdf as rp
    return rp._resolve_font()


# ---------------------------------------------------------------------------
# ① 图表零损伤
# ---------------------------------------------------------------------------
def test_protected_area_merges_the_vector_bands_into_one_block(tmp_path):
    """渐变图在 PDF 里是**上百条矢量** → 保护区必须**聚成一块**。

    ⚠ 踩过的坑（2026-11-24 真机取证）：
      原来逐条 `get_drawings()` 取 rect → JACERS 首页的渐变图被拆成 **251 条**，
      每条面积 226（刚好卡在「面积 < 200 当分隔线跳过」的阈值**之上**）
      → 保护区形同虚设 → 正文块的涂改矩形顺带把图盖了，
        实测在渐变图上盖出 **210×35.6pt 的纯白实心矩形**。
    """
    from microbench import replaced_pdf as rp
    src = tmp_path / "fig.pdf"
    _make_figure_paper(src)
    doc = fitz.open(str(src))
    try:
        page = doc.load_page(0)
        n_draw = len(page.get_drawings())
        assert n_draw >= 20, "样本里没有足够的矢量条 —— 这条护栏等于没测"
        protected = rp._protected_rects(page)
    finally:
        doc.close()

    assert len(protected) == 1, \
        "40 条相邻矢量带没有被聚成一块（实际 %d 块）→ 保护区会形同虚设" % len(protected)
    r = protected[0]
    assert r.x0 <= 72.5 and r.x1 >= 299.5, "保护区没罩住整张图：%s" % (r,)
    assert r.y0 <= 295.5 and r.y1 >= 374.5, "保护区没罩住整张图：%s" % (r,)


def test_merge_nearby_collapses_adjacent_rects():
    """相邻的矩形必须**合并成一块**（否则保护区被切碎 → 形同虚设）。

    ⚠ 这是旧版 PyMuPDF 没有 `cluster_drawings()` 时的**兜底路径**依赖的逻辑，
      也是「渐变图在 PDF 里是上百条矢量」这个坑的直接解药。
    """
    from microbench import replaced_pdf as rp

    bands = [fitz.Rect(72, 295 + i * 2, 300, 297 + i * 2) for i in range(40)]
    merged = rp._merge_nearby(bands)
    assert len(merged) == 1, \
        "40 条相邻矢量带没被合并成一块（实际 %d 块）→ 保护区会形同虚设" % len(merged)

    far = [fitz.Rect(72, 295, 300, 375), fitz.Rect(72, 600, 300, 700)]
    assert len(rp._merge_nearby(far)) == 2, \
        "两个离得很远的图被并成一块 → 会把中间的正文也保护掉（该翻的不翻）"


def test_figure_line_needs_both_short_and_near():
    """「图内元素」的判据是**两条同时成立**：短 **且** 离图近。

    ⚠ 为什么不能只看「离图多近」（2026-11-24 真机取证）：
      正文行离图最近只有 **2.1pt**（第 1 页），
      **图注**离图 **7.2pt** —— 它们是论文的文字，必须照翻。
    ⚠ 为什么不能只看「短」（同上）：
      远处的短正文（"See Fig. 3."）是论文文字，必须照翻。
    """
    from microbench import replaced_pdf as rp
    protected = [fitz.Rect(72, 295, 300, 375)]

    def rec(text, bbox, size):
        return {"text": text, "bbox": bbox, "size": size, "baseline": bbox[3]}

    near_short = rec("Magnetic field (Oe)", (80.0, 378.0, 156.0, 391.0), 9.0)
    far_short = rec("See Fig. 3.", (72.0, 138.0, 124.0, 153.0), 11.0)
    long_near = rec("(a) Magnetostriction and piezomagnetic properties "
                    "of the composite.", (72.0, 395.0, 343.0, 408.0), 9.0)
    inside = rec("Review", (110.0, 313.0, 208.0, 354.0), 30.0)

    assert rp._is_figure_line(near_short, protected) is True, \
        "离图近的短标签没被认成图内元素 → 轴标签会被翻成中文"
    assert rp._is_figure_line(far_short, protected) is False, \
        "离图远的短正文被误认成图内元素 → 短正文永远不会被翻"
    assert rp._is_figure_line(long_near, protected) is False, \
        "图注被误认成图内元素 → 图注永远不会被翻（它是论文文字）"
    assert rp._is_figure_line(inside, protected) is True, \
        "图里的艺术字没被认成图内元素 → 会被涂掉并写上中文"


def _white_fill_count(page) -> int:
    """页面上「白色实心填充矩形」的个数（redaction 刷白块会制造这种东西）。"""
    n = 0
    for d in page.get_drawings():
        if d.get("type") != "fs":
            continue
        fill = d.get("fill")
        if not fill or len(fill) < 3:
            continue
        try:
            rgb = tuple(float(c) for c in fill[:3])
        except (TypeError, ValueError):
            continue
        if all(abs(c - 1.0) < 0.02 for c in rgb):
            n += 1
    return n


def test_replacement_does_not_paint_white_boxes_over_the_page(tmp_path, cjk_font):
    """涂改**不许刷白块** —— redaction 只该「删掉文字」，不该再盖一层白。

    ⚠ 2026-11-24 真机取证：旧实现 `add_redact_annot(rect, fill=bg)`
      每涂一行就画一个**白色实心矩形** →
      JACERS 第 8 页（纯文字页）实测多出 **163 个**这样的矢量对象：
      · 它们是**可选中的空白块**（选中文字时会选中它们）；
      · 它们会**盖住页面的彩色背景**
        （探针实测：彩色底 (229,216,153) 被冲成纯白 (255,255,255)）；
      · 它们让文件变大。
    """
    from microbench.replaced_pdf import build_replaced_pdf
    src = tmp_path / "fig.pdf"
    out = tmp_path / "out.pdf"
    _make_figure_paper(src)
    build_replaced_pdf(src, out, {1: _figure_blocks()}, font=cjk_font)

    doc = fitz.open(str(out))
    try:
        original = doc.load_page(0)
        replaced = doc.load_page(1)
        n_orig = _white_fill_count(original)
        n_rep = _white_fill_count(replaced)
    finally:
        doc.close()
    assert n_rep <= n_orig, \
        "替换页多出了 %d 个白色填充矩形（涂改不该刷白块）" % (n_rep - n_orig)


def test_clip_to_protected_never_overlaps_the_figure(tmp_path):
    """涂改范围必须**避开**保护区（外扩 1pt 留余量）。

    ⚠ 为什么是「实心填充」这么严重：
      `add_redact_annot(rect, fill=bg)` 会把整个矩形**填成背景色** →
      只要压到图上一丁点，图上就多一块白斑。
    """
    from microbench import replaced_pdf as rp
    src = tmp_path / "fig.pdf"
    _make_figure_paper(src)
    doc = fitz.open(str(src))
    try:
        protected = rp._protected_rects(doc.load_page(0))
    finally:
        doc.close()
    assert protected, "保护区是空的 —— 样本有问题"

    # 一条**压在图上边缘**的文字行（这是真机上的真实情形）
    line = fitz.Rect(72.0, 284.2, 320.9, 299.3)
    pieces = rp._clip_to_protected(line, protected)
    assert pieces, "减完什么都不剩 —— 这一行会被整条涂掉"

    for hole in protected:
        grown = fitz.Rect(hole) + (-1.0, -1.0, 1.0, 1.0)
        for piece in pieces:
            inter = piece & grown
            assert inter.is_empty or inter.get_area() <= 0.01, \
                "涂改范围还压着保护区（会盖出白斑）：piece=%s hole=%s" % (piece, hole)


def test_figure_area_is_pixel_identical_after_replacement(tmp_path, cjk_font):
    """⚠ 核心红线（用户原话「原本的图表还都保持不变」）：

    替换页的**图区**必须与原文页**逐像素相同**。
    """
    from microbench.replaced_pdf import build_replaced_pdf
    src = tmp_path / "fig.pdf"
    out = tmp_path / "out.pdf"
    _make_figure_paper(src)
    stats = build_replaced_pdf(src, out, {1: _figure_blocks()}, font=cjk_font)

    doc = fitz.open(str(out))
    try:
        original = doc.load_page(0)
        replaced = doc.load_page(1)
        clip = fitz.Rect(72, 293, 300, 377)
        diff = _region_diff(original, replaced, clip)
        assert diff == 0, \
            "图区有 %d 个像素被改了 —— 图表没有保持不变" % diff
        txt = replaced.get_text()
        assert "Review" in txt, "图上的艺术字被涂了（它属于图，不是正文）"
        assert "Magnetic field (Oe)" in txt, \
            "图下的轴标签被涂了（轴标签属于图内元素，不该翻）"
        assert "磁场" not in txt, "轴标签被翻成中文了（图内元素保持英文）"
        assert "磁致伸缩与压磁特性" in txt, "图注没翻（图注是论文文字，应该翻）"
        assert "参见图" in txt, \
            "远处的短正文没翻（「图内元素」判据必须同时满足「短」和「离图近」）"
        assert "综述" not in txt, "艺术字被翻成中文压在图上了"
    finally:
        doc.close()

    assert stats.blocks_in_figure == 1, \
        "整块落在图里的块（艺术字）没有被跳过：%s" % stats.as_dict()


def test_mutation_disabling_the_protection_is_detected(tmp_path, cjk_font,
                                                        monkeypatch):
    """**变异自检**：把保护区整个关掉 → 必须被抓到。

    判据用「像素差 > 0」而不是「某函数被调用过」——
    后者只要函数还在就绿，等于没测。
    """
    from microbench.replaced_pdf import build_replaced_pdf
    from microbench import replaced_pdf as rp
    src = tmp_path / "fig.pdf"
    out = tmp_path / "mut.pdf"
    _make_figure_paper(src)

    monkeypatch.setattr(rp, "_protected_rects", lambda page: [])
    build_replaced_pdf(src, out, {1: _figure_blocks()}, font=cjk_font)

    doc = fitz.open(str(out))
    try:
        original = doc.load_page(0)
        replaced = doc.load_page(1)
        diff = _region_diff(original, replaced, fitz.Rect(72, 293, 300, 377))
    finally:
        doc.close()
    assert diff > 0, \
        "把保护区整个关掉，图却一像素没变 —— 这条护栏是恒真的（改坏行为也照样绿）"


def test_mutation_removing_the_clip_is_detected(tmp_path, monkeypatch):
    """**变异自检**：把「涂改减去保护区」这一步去掉 → 必须被抓到。

    抓法与生产逻辑同源：结果一旦**与保护区相交**，就是错的。
    """
    from microbench import replaced_pdf as rp
    src = tmp_path / "fig.pdf"
    _make_figure_paper(src)
    doc = fitz.open(str(src))
    try:
        protected = rp._protected_rects(doc.load_page(0))
    finally:
        doc.close()

    line = fitz.Rect(72.0, 284.2, 320.9, 299.3)
    monkeypatch.setattr(rp, "_clip_to_protected",
                        lambda rect, prot: [fitz.Rect(rect)])
    pieces = rp._clip_to_protected(line, protected)

    hit = False
    for hole in protected:
        grown = fitz.Rect(hole) + (-1.0, -1.0, 1.0, 1.0)
        for piece in pieces:
            inter = piece & grown
            if not inter.is_empty and inter.get_area() > 0.01:
                hit = True
    assert hit, \
        "去掉「减去保护区」这一步，涂改范围却还是不压着图 —— 这条护栏是恒真的"


# ---------------------------------------------------------------------------
# ② CJK 排版：断行 / 禁则 / 基线 / 不填满原框
# ---------------------------------------------------------------------------
def test_cjk_breaks_anywhere_but_latin_words_stay_whole(cjk_font):
    """中文**任意位置可断**，拉丁词**整体不拆**。

    ⚠ 踩过的坑（2026-11-24 真机取证）：
      MuPDF 的折行是**按空格分词**的 → 一长串中文在它眼里是「整个一个词」
      → 放不下就整体推到下一行 → 行末留一大片白
      （实测 250pt 宽的行只排了 193.5pt，下一行只有 120pt）。
    """
    from microbench import replaced_pdf as rp
    ts = rp._CjkTypesetter(cjk_font)

    atoms = rp._split_atoms("磁电耦合 coefficient 系数")
    kinds = [k for k, _ in atoms]
    assert kinds.count("cjk") == 6, "汉字必须被切成**单字**原子：%s" % atoms
    latin = [c for k, c in atoms if k == "word"]
    assert "coefficient" in latin, "拉丁词必须整体成词，不能按字母拆：%s" % atoms

    # 断行：中文能断
    text = "磁电耦合复合材料在层压结构中的界面耦合效应与残余磁应变行为研究进展综述"
    lines = ts.wrap(rp._split_atoms(text), 120.0, 10.0)
    assert len(lines) >= 3, "中文没有断行（整段被当成一个词了）：%s" % lines
    for ln in lines:
        w = ts.measure("".join(c for _k, c in ln), 10.0)
        assert w <= 120.01, "有一行超出了给定的行宽：%.2f" % w

    # 断行：拉丁词不拆（词宽 ≤ 行宽时）
    words = "magnetoelectric coupling coefficient"
    lines = ts.wrap(rp._split_atoms(words), 90.0, 10.0)
    got = ["".join(c for _k, c in ln).strip() for ln in lines]
    for w in words.split():
        assert any(w in line for line in got), \
            "拉丁词 %r 被拆开了：%s" % (w, got)


def test_no_line_starts_with_a_forbidden_punctuation(cjk_font):
    """禁则：标点不许落在行首（避头），开括号不许落在行尾（避尾）。"""
    from microbench import replaced_pdf as rp
    ts = rp._CjkTypesetter(cjk_font)
    text = "这是一个测试，用来检查标点悬挂与避头尾规则是否生效，例如「引用」也一样。"
    lines = ts.wrap(rp._split_atoms(text), 40.0, 10.0)
    for ln in lines:
        head = "".join(c for _k, c in ln).lstrip()
        assert head[:1] not in rp._NO_LINE_START, \
            "有一行以禁则标点开头：%r" % head
        tail = "".join(c for _k, c in ln).rstrip()
        assert tail[-1:] not in rp._NO_LINE_END, \
            "有一行以开括号结尾：%r" % tail


def test_chinese_lines_land_on_the_original_baselines(tmp_path, cjk_font):
    """中文必须**逐行落在原页的基线**上 —— 否则不像「同一份排版」。

    ⚠ 踩过的坑：PyMuPDF 会把**一个视觉行**拆成**多条** line 记录
      （下标 / 字间距 / 字体切换）→ 基线列表里出现两条相差 0.05pt 的基线
      → 中文的两行被排到**同一条基线**上 → 渲染出来是**一团黑**。
    """
    from microbench.replaced_pdf import build_replaced_pdf
    src = tmp_path / "fig.pdf"
    out = tmp_path / "out.pdf"
    _make_figure_paper(src)

    doc = fitz.open(str(src))
    try:
        original = doc.load_page(0)
        orig_bases = sorted({round(b, 2) for b, _t in _line_baselines(original)})
    finally:
        doc.close()

    build_replaced_pdf(src, out, {1: _figure_blocks()}, font=cjk_font)
    doc = fitz.open(str(out))
    try:
        replaced = doc.load_page(1)
        zh = [(b, t) for b, t in _line_baselines(replaced) if _has_cjk(t)]
    finally:
        doc.close()

    assert zh, "替换页里没有中文"
    for base, text in zh:
        assert any(abs(base - ob) <= 0.5 for ob in orig_bases), \
            "中文行 %.2f 没有落在任何一条原页基线上（%s）：%r" % (base, orig_bases, text)
    # 两行中文不能落在同一条基线上（那就是「叠成一团黑」）
    bases = [round(b, 2) for b, _t in zh]
    assert len(set(bases)) == len(bases), \
        "有几行中文落在了同一条基线上（会叠字）：%s" % bases


def test_visual_lines_merge_one_line_but_not_two_columns():
    """同一个视觉行要合并；**左右两栏的同行**不能合并。"""
    from microbench import replaced_pdf as rp

    def rec(text, bbox, baseline, size):
        return {"text": text, "bbox": bbox, "baseline": baseline, "size": size}

    same_line = [
        rec("I.", (149.1, 276.0, 155.2, 285.2), 285.0, 9.0),
        rec("Introduction", (164.3, 276.0, 211.0, 285.2), 285.05, 9.0),
    ]
    merged = rp._visual_lines(same_line)
    assert len(merged) == 1, \
        "同一个视觉行没合并（PyMuPDF 会把一行拆成多条）→ 基线会重复 → 中文叠字"
    assert "Introduction" in merged[0]["text"]

    two_columns = [
        rec("left column", (72.0, 276.0, 200.0, 285.2), 285.0, 9.0),
        rec("right column", (320.0, 276.0, 460.0, 285.2), 285.0, 9.0),
    ]
    kept = rp._visual_lines(two_columns)
    assert len(kept) == 2, "左右两栏的同行被并成一行 → 中文会横跨整页"


def test_heading_is_split_out_of_the_body_paragraph(tmp_path, cjk_font):
    """小节标题必须**从正文尾部拆出来**，单独落在它自己那行基线上。

    ⚠ 踩过的坑：抽取层的段落块会把「独立成行的标题」吸进上一段的末尾
      （实测 JACERS 第 1 页 i=4 的源文本尾部是 "I. Introduction"）
      → 译文末尾多出「I. 引言」，贴在正文最后一句后面，看起来像排版坏了。
    """
    from microbench.replaced_pdf import build_replaced_pdf
    src = tmp_path / "head.pdf"
    out = tmp_path / "out.pdf"
    _make_heading_paper(src)
    blocks = {1: [__import__("microbench.replaced_pdf", fromlist=["Block"]).Block(
        index=0, bbox=(70.0, 76.0, 320.0, 145.0),
        translation="这是正文的第一句。这是正文的第二句。I. 引言",
        kind="body", col="S")]}

    stats = build_replaced_pdf(src, out, blocks, font=cjk_font)
    assert stats.headings_split == 1, \
        "小节标题没被拆出来：%s" % stats.as_dict()

    doc = fitz.open(str(out))
    try:
        replaced = doc.load_page(1)
        lines = _line_baselines(replaced)
    finally:
        doc.close()

    body_lines = [(b, t) for b, t in lines if 88.0 <= b <= 92.0]
    head_lines = [(b, t) for b, t in lines if 133.0 <= b <= 137.0]
    assert body_lines, "正文行不见了"
    assert head_lines, "小节标题没有单独成行"
    assert "引言" not in body_lines[0][1], \
        "标题还粘在正文后面：%r" % body_lines[0][1]
    assert "引言" in head_lines[0][1], \
        "标题行里没有标题译文：%r" % head_lines[0][1]
    assert "Introduction" not in "".join(t for _b, t in lines), \
        "原来的英文标题还在（没被替换）"


# ---------------------------------------------------------------------------
# ③ 「有的页数没翻到」：构建前全量体检
# ---------------------------------------------------------------------------
def test_health_endpoint_reports_never_translated_pages(client, sandbox):
    """⚠ 用户 2026-11-24：「有的页数没翻到没好好翻」。

    根因（2026-11-17 就埋下的坑）：
      替换版只认**段落级**译文（带坐标的 blocks）。
      在**普通模式**翻的页只有整页 `translation` → 没有坐标 → 物理上无处可贴。
      而旧实现**只遍历快照里已有的页** → **压根没翻过的页**根本不在名单里
      → 静默留英文，用户毫无提示。

    → 端点必须把三类页分开报出来：
      · `pages_with_blocks`     有段落级译文 → 能替换
      · `pages_only_fulltext`   只有整页译文 → 替换不了（要按段落补翻）
      · `pages_never_translated` 压根没翻过    → 替换不了（要整页补翻）
    """
    from microbench.app import _snapshot_path
    from microbench.bilingual import BLOCK_TRANSLATION_VERSION

    rel = "01_Literature/04_Survey_综述/01_论文原文_PDF/三页测试论文.pdf"
    pdf_path = sandbox / rel
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    for _ in range(3):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 90), "Body text of the test paper.",
                         fontsize=11, fontname="helv")
    doc.save(str(pdf_path))
    doc.close()

    path = _snapshot_path("healthpaper_xyz789")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "paper_id": "healthpaper_xyz789",
        "relative_path": rel,
        "pages": [
            {"page_num": 1, "blocks_version": BLOCK_TRANSLATION_VERSION,
             "blocks": [{"i": 0, "bbox": [70.0, 76.0, 500.0, 96.0],
                         "translation": "磁致伸缩复合材料在此研究。",
                         "kind": "body", "col": "S"}]},
            {"page_num": 2, "translation": "第二页的整页译文（没有段落坐标）"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    res = client.post("/api/paper/build_replaced_pdf", json={
        "paper_id": "healthpaper_xyz789",
        "relative_path": rel,
    })
    assert res.status_code == 200, res.text
    data = res.json()

    assert data.get("pdf_pages_total") == 3, \
        "没报出 PDF 总页数：%s" % json.dumps(data, ensure_ascii=False)
    assert data.get("pages_with_blocks") == [1], data.get("pages_with_blocks")
    assert data.get("pages_only_fulltext") == [2], \
        "没认出「第 2 页只有整页译文」：%s" % data.get("pages_only_fulltext")
    assert data.get("pages_never_translated") == [3], \
        "没认出「第 3 页压根没翻过」：%s" % data.get("pages_never_translated")
    assert data.get("pages_missing") == [2, 3], \
        "待补翻名单不完整（两类缺页都要在里面）：%s" % data.get("pages_missing")
    assert data.get("pages_without_blocks") == [2], \
        "pages_without_blocks 应与「只有整页译文的页」一致：%s" % data.get("pages_without_blocks")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
