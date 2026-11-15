# -*- coding: utf-8 -*-
"""「中译替换版 PDF」（原文页 + 替换页交替）的护栏。

用户诉求（2026-11-15）：
  「直接就是原文上直接把翻译打上去……这篇论文一共十页，
    然后我翻译完之后就变成二十页。第一页就是原来的那个文章，
    第二页就是原来的那个字儿直接削去……然后第三页又是原来的第二页……
    然后原本的图表还都保持不变。」

盯住的红线：
  1. **图表/公式/矢量图必须原样保留** —— redaction 只能删文字。
  2. **替换页里不能有英文残留**（被涂掉的字不能还能搜到）。
  3. **放不下的块不许截断** —— 宁可保留英文，也不许写半截译文。
  4. **段落坐标的版本闸门** —— 切分算法变了就不能贴旧译文。
  5. 输出**不能覆盖源 PDF**。

⚠️ 这里**一律不读源码文本当断言**（`assert "xxx" in src`）。
   元护栏 tests/test_guard_efficacy.py 把那一类钉死不许再涨：
   那种断言改坏行为也照样绿（把 onclick 删了、把版本闸门短路了，
   源码里该有的字符串还在 → 全绿）。
   → 后端走**真 import + 真打端点**，前端走 **node 切片跑真实函数**
     （microbench/_test_replaced_pdf_frontend.js）。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS_TEST = REPO / "microbench" / "_test_replaced_pdf_frontend.js"

sys.path.insert(0, str(REPO))

fitz = pytest.importorskip("fitz")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _make_paper(path: Path) -> None:
    """造一篇 2 页的「论文」：英文正文 + 一张矢量图。"""
    doc = fitz.open()
    for pno in (1, 2):
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 90), "Magnetostrictive composites are studied here.",
                         fontsize=11, fontname="helv")
        page.insert_text((72, 110), "The second line of the paragraph goes on.",
                         fontsize=11, fontname="helv")
        # 一张「图」：矢量矩形 + 文字标签
        page.draw_rect(fitz.Rect(72, 200, 300, 360), color=(0, 0, 1), width=2)
        page.insert_text((80, 215), "Figure 1. A caption for the figure.",
                         fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


def _make_paper_with_bitmap(path: Path) -> None:
    """造一篇带**位图**的论文（图 + 矢量图 + 正文都要活着）。"""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
    pix.set_rect(pix.irect, (200, 40, 40))
    png = pix.tobytes("png")

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 90), "Magnetostrictive composites are studied here.",
                     fontsize=11, fontname="helv")
    # 位图（放在文字框之外，涂改范围碰不到它）
    page.insert_image(fitz.Rect(320, 60, 460, 160), stream=png)
    # 矢量图
    page.draw_rect(fitz.Rect(72, 200, 300, 360), color=(0, 0, 1), width=2)
    page.insert_text((80, 215), "Figure 1. A caption for the figure.",
                     fontsize=9, fontname="helv")
    doc.save(str(path))
    doc.close()


def _blocks_for_page(page_no: int):
    from microbench.replaced_pdf import Block
    return [
        Block(index=0, bbox=(70.0, 76.0, 500.0, 96.0),
              translation="磁致伸缩复合材料在此研究。", kind="body", col="S"),
        Block(index=1, bbox=(70.0, 98.0, 500.0, 116.0),
              translation="这是段落的第二行，继续往下写。", kind="body", col="S"),
    ]


# ---------------------------------------------------------------------------
# 模块契约（真 import，不用「读源码文本」）
# ---------------------------------------------------------------------------
def test_module_exposes_the_builder_api():
    """构建器、块数据结构、统计结构都必须在（且可调用）。"""
    from microbench import replaced_pdf as rp

    assert callable(rp.build_replaced_pdf), "没有可调用的 build_replaced_pdf()"
    blk = rp.Block(index=0, bbox=(1.0, 2.0, 3.0, 4.0), translation="译文")
    assert blk.index == 0 and blk.translation == "译文"
    assert blk.kind == "body", "块类型默认值应是 body"

    stats = rp.BuildStats()
    d = stats.as_dict()
    for key in ("pages_total", "pages_replaced", "blocks_written",
                "blocks_skipped", "blocks_unfit", "blocks_overflow"):
        assert key in d, "统计里少了 " + key
    assert d["pages_total"] == 0


def test_formula_and_table_blocks_are_not_replaced():
    """公式块与表格块**不参与替换**（公式硬拼 LaTeX 极易装反分子分母）。"""
    from microbench.replaced_pdf import REPLACEABLE_KINDS
    assert "formula" not in REPLACEABLE_KINDS, "公式块不能被替换"
    assert "table" not in REPLACEABLE_KINDS, "表格块不能被替换（用户要图表原样）"
    assert "body" in REPLACEABLE_KINDS
    assert "heading" in REPLACEABLE_KINDS


# ---------------------------------------------------------------------------
# 核心红线：涂改只能删文字，图表必须活着
# ---------------------------------------------------------------------------
def test_redaction_only_removes_text_keeps_figures(tmp_path):
    """⚠ 核心红线：**只删文字**，图片与矢量图必须原样保留。

    用户原话：「原本的图表还都保持不变」。
    忘了加 images=NONE / graphics=NONE → 图片会被涂成白块。

    判据用**结果**而不是源码：造一篇带位图 + 矢量图 + 正文的 PDF，
    构建替换版后逐项比对图片数、矢量图数与文字。
    """
    from microbench.replaced_pdf import build_replaced_pdf
    src = tmp_path / "paper.pdf"
    out = tmp_path / "out.pdf"
    _make_paper_with_bitmap(src)

    before = fitz.open(str(src))
    try:
        n_img = len(before.load_page(0).get_images())
        n_vec = len(before.load_page(0).get_drawings())
        assert n_img >= 1, "测试样本里没有位图 —— 这条护栏等于没测"
        assert n_vec >= 1, "测试样本里没有矢量图 —— 这条护栏等于没测"
    finally:
        before.close()

    build_replaced_pdf(src, out, {1: _blocks_for_page(1)})

    doc = fitz.open(str(out))
    try:
        replaced = doc.load_page(1)          # 第 2 页 = 第 1 页的替换页
        assert len(replaced.get_images()) == n_img, \
            "替换页里的位图少了 —— 图片被涂成白块了"
        assert len(replaced.get_drawings()) >= n_vec, \
            "替换页里的矢量图少了 —— 图被抹掉了"
        txt = replaced.get_text()
        assert "Magnetostrictive" not in txt, "替换页还留着英文（涂改没生效）"
        assert "磁致伸缩复合材料" in txt, "替换页没有中文"
        assert "Figure 1" in txt, "图注被误涂了（用户要图表原样）"
    finally:
        doc.close()


def test_build_produces_interleaved_pages_and_keeps_figures(tmp_path):
    from microbench.replaced_pdf import build_replaced_pdf
    src = tmp_path / "paper.pdf"
    out = tmp_path / "out.pdf"
    _make_paper(src)
    pages = {1: _blocks_for_page(1), 2: _blocks_for_page(2)}
    stats = build_replaced_pdf(src, out, pages)
    assert out.exists()

    doc = fitz.open(str(out))
    try:
        assert doc.page_count == 4, "2 页原文 → 4 页（原文+替换交替），实际 %d" % doc.page_count

        # 原文页（0/2）：英文还在
        p0 = doc.load_page(0).get_text()
        assert "Magnetostrictive" in p0, "原文页的英文不见了"
        assert "磁致伸缩" not in p0, "原文页不该有中文"

        # 替换页（1/3）：中文在、英文被涂掉
        p1 = doc.load_page(1).get_text()
        assert "磁致伸缩复合材料" in p1, "替换页没有中文"
        assert "Magnetostrictive" not in p1, "替换页还留着英文（涂改没生效）"
        assert "Figure 1" in p1, "图注被误涂了（用户要图表原样）"

        # 图（矢量矩形）还在
        assert len(doc.load_page(1).get_drawings()) >= len(doc.load_page(0).get_drawings()), \
            "替换页的矢量图比原文页还少 —— 图被涂掉了"
    finally:
        doc.close()

    assert stats.pages_total == 2
    assert stats.pages_replaced == 2
    assert stats.blocks_written == 4
    assert stats.blocks_overflow == 0, "不该有写入被拒的块：%s" % stats.warnings


def test_untranslatable_block_keeps_english_instead_of_truncating(tmp_path):
    """放不下的块 → **保留英文**，绝不写半截译文。

    截断的译文 = 信息丢了；保留英文至少还能读。

    ⚠ 译文要**真的**放不下：块的可用宽度会被扩到「原页真实的行宽」
       （见 _line_group），所以这里用足够长的译文来逼出放不下。
    """
    from microbench.replaced_pdf import Block, build_replaced_pdf
    src = tmp_path / "paper.pdf"
    out = tmp_path / "out.pdf"
    # 页面做矮一点 → 「往下借空隙」的空间有限，才逼得出放不下
    doc = fitz.open()
    page = doc.new_page(width=612, height=300)
    page.insert_text((72, 90), "Magnetostrictive composites are studied here.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 110), "The second line of the paragraph goes on.",
                     fontsize=11, fontname="helv")
    doc.save(str(src))
    doc.close()
    pages = {1: [Block(index=0, bbox=(70.0, 76.0, 90.0, 200.0),
                       translation="很长的中文译文" * 500, kind="body", col="S")]}
    stats = build_replaced_pdf(src, out, pages)
    doc = fitz.open(str(out))
    try:
        p1 = doc.load_page(1).get_text()
        assert "Magnetostrictive" in p1, "放不下的块应该保留英文原文"
        assert "很长的中文译文" not in p1, "不该写进放不下的译文"
    finally:
        doc.close()
    assert stats.blocks_unfit >= 1, "放不下的块要如实计数"
    assert stats.blocks_overflow == 0, "不该有写入被拒的块"


def test_no_leftover_english_sliver_when_the_first_line_is_indented(tmp_path):
    """⚠ 回归：块的 bbox 取的是**首行**起点，后续行更靠左。

    涂改范围若只按块 bbox → 每行最左边几个字母会**留在原地**
    （实测在 JACERS 综述第 2 页右栏残留成一条竖排的 'ert' 'in' 'ot'）。
    → 涂改范围必须跟着**原页真实的行**收紧。
    """
    from microbench.replaced_pdf import Block, build_replaced_pdf
    src = tmp_path / "paper.pdf"
    out = tmp_path / "out.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # 首行缩进 20pt，第二行顶格 —— 正是论文 PDF 的常态
    page.insert_text((90, 90), "Magnetostrictive composites are studied here.",
                     fontsize=11, fontname="helv")
    page.insert_text((70, 110), "The second line of the paragraph goes on.",
                     fontsize=11, fontname="helv")
    doc.save(str(src))
    doc.close()

    pages = {1: [Block(index=0, bbox=(88.0, 76.0, 500.0, 116.0),
                       translation="磁致伸缩复合材料在此研究，这是第二行。",
                       kind="body", col="S")]}
    build_replaced_pdf(src, out, pages)
    doc = fitz.open(str(out))
    try:
        p1 = doc.load_page(1).get_text()
        assert "Magnetostrictive" not in p1, "首行的英文没涂掉"
        assert "The second line" not in p1, \
            "第二行还留着英文 —— 涂改范围没跟着原页的行收紧"
        assert "磁致伸缩复合材料" in p1, "中文没写进去"
    finally:
        doc.close()


def test_chinese_is_searchable_in_the_output(tmp_path):
    """写进去的是**真正的 PDF 文字**（可选中、可检索），不是一张图。"""
    from microbench.replaced_pdf import build_replaced_pdf
    src = tmp_path / "paper.pdf"
    out = tmp_path / "out.pdf"
    _make_paper(src)
    build_replaced_pdf(src, out, {1: _blocks_for_page(1)})
    doc = fitz.open(str(out))
    try:
        txt = doc.load_page(1).get_text()
        assert "磁致伸缩复合材料" in txt, "中文检索不到 —— 写进去的可能不是文字"
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------
def test_clean_translation_strips_markdown_and_latex():
    """Markdown 味道要去掉，LaTeX 里的**信息不能丢**。

    ⚠ 旧实现把 `\\alpha` 直接删掉 → 译文里只剩「效应  很好」，
      符号没了。现在换成**真正的 Unicode 符号**（α），
      读者看到的就是论文里那个符号。
    """
    from microbench.replaced_pdf import clean_translation
    out = clean_translation("**磁电**效应 $\\alpha_{ME}$ 很好\n第二行")
    assert "**" not in out
    assert "$" not in out
    assert "\\" not in out, "还剩 LaTeX 命令：" + out
    assert "\n" not in out, "换行必须变成空格（见 test_newlines_become_spaces_before_layout）"
    assert "磁电" in out and "α" in out, out
    assert "alpha" not in out, "LaTeX 命令名不该留在正文里：" + out


def test_clean_translation_keeps_numbers_and_subscripts_readable():
    """数字下标要变成**真下标**，别留一堆下划线。"""
    from microbench.replaced_pdf import clean_translation
    out = clean_translation("Cr$_2$O$_3$ 晶体的数值")
    assert "Cr" in out and "O" in out
    assert "₂" in out and "₃" in out, "数字下标没转成真下标：" + out
    assert "_" not in out, "还留着下划线写法：" + out


def test_broken_marker_residue_is_removed():
    """抽取层偶尔吐**半截**标记（实测「为避免混淆�/T18⟧」）→ 残骸不许进 PDF。"""
    from microbench.replaced_pdf import clean_translation
    out = clean_translation("为避免混淆�/T18⟧")
    assert "⟧" not in out, out
    assert "/T18" not in out, "坏标记的残骸没清掉：" + out
    assert "为避免混淆" in out, "正文被误删：" + out


def test_missing_glyphs_are_replaced_not_tofu():
    """字体没有的字形要换掉 —— 豆腐块（□）比缺字难看。"""
    from microbench.replaced_pdf import CjkFont, fix_missing_glyphs
    font = CjkFont(name="probe")      # 没有 path → 查不了字形 → 全部放行
    assert fix_missing_glyphs("abc", font) == "abc"

    class _Fake(CjkFont):
        def has_glyph(self, ch, bold=False):
            return ch not in ("˒", "□")

    fake = _Fake(name="probe")
    out = fix_missing_glyphs("引用3˒4与□", fake)
    assert "˒" not in out, "上标逗号没被替换"
    assert "□" not in out, "豆腐块字符没被清掉"
    assert "3" in out and "4" in out


# ---------------------------------------------------------------------------
# 字体：必须「写进去能原样取回来」，否则中文搜不到
# ---------------------------------------------------------------------------
def test_selected_font_passes_the_roundtrip_check(monkeypatch):
    """**选中的字体必须能原样取回** —— 否则用户在阅读器里搜不到中文。

    踩过的坑（2026-11-15）：Noto Sans SC（可变字体）写「料/理/墨」时，
    PDF 的 ToUnicode 里记成 U+F9BE（兼容表意文字）——
    渲染出来一模一样，**但搜「材料」「理论」都搜不到**。
    """
    from microbench import replaced_pdf as rp
    monkeypatch.setattr(rp, "_FONT_CACHE", None)
    font = rp._resolve_font()
    ok, why = rp._font_roundtrips(font, False)
    assert ok, "选中的字体没通过往返校验：%s" % why
    if font.bold_path:
        bok, bwhy = rp._font_roundtrips(font, True)
        assert bok, "选中的粗体没通过往返校验：%s" % bwhy


def test_resolver_skips_a_font_that_fails_the_roundtrip(monkeypatch):
    """**变异改被调方**：把校验判成不合格 → 必须跳过这个字体继续找下一套。"""
    from microbench import replaced_pdf as rp
    real = rp._font_roundtrips

    def fake(font, bold=False):
        if font.path and "msyh" in str(font.path):
            return False, "假装不合格"
        return real(font, bold)

    monkeypatch.setattr(rp, "_font_roundtrips", fake)
    monkeypatch.setattr(rp, "_FONT_CACHE", None)
    monkeypatch.setattr(rp, "_FONT_REJECTED", [])
    font = rp._resolve_font()
    assert font.path is None or "msyh" not in str(font.path), \
        "往返校验不合格的字体还是被选上了"
    assert any("msyh" in str(p) for p, _ in rp._FONT_REJECTED), \
        "拒用记录里没有它 —— 排查时会看不懂为什么没用雅黑"


# ---------------------------------------------------------------------------
# 布局：译文里的换行必须变成空格
# ---------------------------------------------------------------------------
def test_newlines_become_spaces_before_layout():
    """译文里的换行必须变成空格。

    ⚠ `insert_textbox` 把 "\\n" 当**强制换行** → 实际行数比按宽度折行算出来的多
    → MuPDF 直接拒写（实测 7 块「算着放得下、实际被拒」全是这个原因）。
    """
    from microbench.replaced_pdf import clean_translation
    out = clean_translation("第一行\n第二行\n\n第三行")
    assert "\n" not in out, "译文里还留着换行 → insert_textbox 会多折一行： " + out
    assert "第一行" in out and "第二行" in out and "第三行" in out, out


def test_output_path_uses_a_separate_folder():
    """输出不能放进 `01_论文原文_PDF/` —— 那个目录名说的是「原文」。"""
    from microbench.app import _default_replaced_out
    out = _default_replaced_out(
        "01_Literature/04_Survey_综述/01_论文原文_PDF/某论文.pdf")
    assert "01_论文原文_PDF" not in out, "输出又落回原文目录了：" + out
    assert out.endswith("_中译替换版.pdf"), out
    assert "05_译文替换版_PDF" in out, out
    # 非标准目录也要落到某个地方，不能抛异常
    assert _default_replaced_out("别处/某论文.pdf").endswith("_中译替换版.pdf")


# ---------------------------------------------------------------------------
# 后端端点：真打 HTTP（不读源码文本）
# ---------------------------------------------------------------------------
PAPER_ID = "testpaper_abc123"
SRC_REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/测试论文.pdf")


def _write_snapshot(base_dir: Path, blocks_version: int) -> None:
    """按真实快照格式落一份（含段落译文 + 坐标）。"""
    from microbench.app import _snapshot_path
    path = _snapshot_path(PAPER_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "paper_id": PAPER_ID,
        "relative_path": SRC_REL,
        "saved_at": "2026-11-15T09:00:00",
        "pages": [{
            "page_num": 1,
            "blocks_version": blocks_version,
            "blocks": [{
                "i": 0,
                "bbox": [70.0, 76.0, 500.0, 96.0],
                "translation": "磁致伸缩复合材料在此研究。",
                "kind": "body",
                "col": "S",
            }],
        }],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_vault_pdf(base_dir: Path) -> None:
    rel = base_dir / SRC_REL
    rel.parent.mkdir(parents=True, exist_ok=True)
    _make_paper(rel)


def test_api_builds_the_replaced_pdf_end_to_end(client, sandbox):
    """端到端：造 vault → 造快照 → 真打端点 → 拿到替换版 PDF。"""
    from microbench.bilingual import BLOCK_TRANSLATION_VERSION
    _make_vault_pdf(sandbox)
    _write_snapshot(sandbox, BLOCK_TRANSLATION_VERSION)

    res = client.post("/api/paper/build_replaced_pdf", json={
        "paper_id": PAPER_ID,
        "relative_path": SRC_REL,
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data.get("ok") is True
    assert data.get("pages_total") == 2, data
    assert data.get("pages_replaced") == 1, data
    assert data.get("blocks_written") == 1, data
    assert data.get("out_size_bytes", 0) > 0

    out_rel = data["out_relative_path"]
    assert "05_译文替换版_PDF" in out_rel, out_rel
    assert "01_论文原文_PDF" not in out_rel, out_rel
    out_path = sandbox / out_rel
    assert out_path.exists(), "替换版没落盘：" + out_rel
    doc = fitz.open(str(out_path))
    try:
        assert doc.page_count == 3, "2 页原文 → 原文 + 1 张替换页 = 3 页"
        # 交错顺序：原文1 / 替换1 / 原文2（第 2 页没译文 → 不生成替换页）
        assert "磁致伸缩" in doc.load_page(1).get_text(), "替换页里没有中文"
        assert "Magnetostrictive" not in doc.load_page(1).get_text(), \
            "替换页里还留着英文"
    finally:
        doc.close()


def test_api_refuses_a_snapshot_from_an_old_block_version(client, sandbox):
    """⚠ 版本闸门：段落切分算法变了 → 旧译文的坐标会错位 → 必须拒绝。

    判据是**响应码**，不是源码里有没有那行比较。
    """
    from microbench.bilingual import BLOCK_TRANSLATION_VERSION
    _make_vault_pdf(sandbox)
    _write_snapshot(sandbox, BLOCK_TRANSLATION_VERSION + 1)

    res = client.post("/api/paper/build_replaced_pdf", json={
        "paper_id": PAPER_ID,
        "relative_path": SRC_REL,
    })
    assert res.status_code == 409, (
        "旧版本的段落译文被贴上去了（坐标会错位）：" + res.text)


def test_api_refuses_a_snapshot_without_blocks(client, sandbox):
    """快照里没有段落译文 → 409（而不是硬贴一页空白）。"""
    from microbench.app import _snapshot_path
    _make_vault_pdf(sandbox)
    path = _snapshot_path(PAPER_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "paper_id": PAPER_ID,
        "pages": [{"page_num": 1, "translation": "整页译文"}],
    }, ensure_ascii=False), encoding="utf-8")

    res = client.post("/api/paper/build_replaced_pdf", json={
        "paper_id": PAPER_ID,
        "relative_path": SRC_REL,
    })
    assert res.status_code == 409, res.text


def test_api_reports_missing_snapshot_and_missing_pdf(client, sandbox):
    """没有快照 / 源 PDF 不在 → 404（分清是哪一种）。"""
    _make_vault_pdf(sandbox)
    res = client.post("/api/paper/build_replaced_pdf", json={
        "paper_id": PAPER_ID,
        "relative_path": SRC_REL,
    })
    assert res.status_code == 404, res.text
    assert "快照" in res.text, res.text

    res = client.post("/api/paper/build_replaced_pdf", json={
        "paper_id": PAPER_ID,
        "relative_path": "01_Literature/不存在的论文.pdf",
    })
    assert res.status_code == 404, res.text


def test_api_never_overwrites_the_source_pdf(client, sandbox):
    """输出路径 == 源路径 → 400，且源文件一个字节都不能动。"""
    from microbench.bilingual import BLOCK_TRANSLATION_VERSION
    _make_vault_pdf(sandbox)
    _write_snapshot(sandbox, BLOCK_TRANSLATION_VERSION)
    src_path = sandbox / SRC_REL
    before = src_path.read_bytes()

    res = client.post("/api/paper/build_replaced_pdf", json={
        "paper_id": PAPER_ID,
        "relative_path": SRC_REL,
        "out_relative_path": SRC_REL,
    })
    assert res.status_code == 400, res.text
    assert src_path.exists(), "源 PDF 被删了"
    assert src_path.read_bytes() == before, "源 PDF 被改了"


# ---------------------------------------------------------------------------
# 前端：node 切片跑 index.html 里的**真实函数**
# ---------------------------------------------------------------------------
def test_replaced_pdf_frontend_node_guardrail_passes():
    """前端行为级护栏必须通过（node 跑真实函数，含变异自检）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 中译替换版前端行为护栏跳过")
    proc = subprocess.run([node, str(JS_TEST)], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, (
        "中译替换版前端护栏失败:\n" + proc.stdout + "\n" + proc.stderr)
    assert "FAIL 0" in proc.stdout, proc.stdout
    # 变异自检必须真的抓到东西 —— 否则这套断言是恒真的
    m = re.search(r"变异捕获\s+(\d+)/(\d+)", proc.stdout)
    assert m, "护栏输出里没有变异自检结果 —— 断言可能是恒真的：\n" + proc.stdout
    caught, total = int(m.group(1)), int(m.group(2))
    assert total > 0, "变异自检一个都没配 —— 等于没写：\n" + proc.stdout
    assert caught == total, \
        f"变异只抓到 {caught}/{total} —— 有一套断言是恒真的：\n" + proc.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
