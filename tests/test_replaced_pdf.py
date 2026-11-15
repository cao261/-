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
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "microbench" / "replaced_pdf.py"
APP = REPO / "microbench" / "app.py"
INDEX = REPO / "microbench" / "static" / "index.html"

sys.path.insert(0, str(REPO))

fitz = pytest.importorskip("fitz")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _src():
    return MODULE.read_text(encoding="utf-8")


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


def _blocks_for_page(page_no: int):
    from microbench.replaced_pdf import Block
    return [
        Block(index=0, bbox=(70.0, 76.0, 500.0, 96.0),
              translation="磁致伸缩复合材料在此研究。", kind="body", col="S"),
        Block(index=1, bbox=(70.0, 98.0, 500.0, 116.0),
              translation="这是段落的第二行，继续往下写。", kind="body", col="S"),
    ]


# ---------------------------------------------------------------------------
# 结构契约
# ---------------------------------------------------------------------------
def test_module_exists_and_exposes_builder():
    assert MODULE.exists(), "缺少 microbench/replaced_pdf.py"
    src = _src()
    assert "def build_replaced_pdf(" in src, "没有 build_replaced_pdf()"
    assert "class Block" in src, "没有 Block 数据类"
    assert "class BuildStats" in src, "没有 BuildStats"


def test_redaction_only_removes_text_keeps_figures():
    """⚠ 核心红线：**只删文字**，图片与矢量图必须原样保留。

    用户原话：「原本的图表还都保持不变」。
    忘了加 images=NONE / graphics=NONE → 图片会被涂成白块。
    """
    src = _src()
    assert "images=fitz.PDF_REDACT_IMAGE_NONE" in src, \
        "涂改时没保住图片 —— 原文的插图会被涂成白块"
    assert "graphics=fitz.PDF_REDACT_LINE_ART_NONE" in src, \
        "涂改时没保住矢量图 —— 原文的曲线图会被抹掉"


def test_formula_and_table_blocks_are_not_replaced():
    """公式块与表格块**不参与替换**（公式硬拼 LaTeX 极易装反分子分母）。"""
    from microbench.replaced_pdf import REPLACEABLE_KINDS
    assert "formula" not in REPLACEABLE_KINDS, "公式块不能被替换"
    assert "table" not in REPLACEABLE_KINDS, "表格块不能被替换（用户要图表原样）"
    assert "body" in REPLACEABLE_KINDS
    assert "heading" in REPLACEABLE_KINDS


def test_newlines_become_spaces_before_layout():
    """译文里的换行必须变成空格。

    ⚠ `insert_textbox` 把 "\\n" 当**强制换行** → 实际行数比按宽度折行算出来的多
    → MuPDF 直接拒写（实测 7 块「算着放得下、实际被拒」全是这个原因）。
    """
    src = _src()
    assert re.search(r'out = re\.sub\(r"\\s\*\\n\\s\*", " ", out\)', src), \
        "clean_translation 没把换行变成空格"


def test_api_endpoint_exists_with_version_gate():
    src = APP.read_text(encoding="utf-8")
    assert '@app.post("/api/paper/build_replaced_pdf")' in src, \
        "缺少 /api/paper/build_replaced_pdf 端点"
    # 版本闸门：段落切分算法变了 → 旧段落译文的坐标会错位 → 必须拒绝
    assert "BLOCK_TRANSLATION_VERSION" in src, "端点没有做段落坐标的版本闸门"
    assert "saved_ver != BLOCK_TRANSLATION_VERSION" in src, \
        "端点没有比对 blocks_version —— 旧译文会被贴到错位的段落上"


def test_api_never_overwrites_the_source_pdf():
    src = APP.read_text(encoding="utf-8")
    assert "输出路径不能和源 PDF 相同" in src, "端点没防止覆盖源 PDF"


def test_frontend_button_is_wired():
    src = INDEX.read_text(encoding="utf-8")
    assert 'id="pdf-replaced-toggle"' in src, "PDF 工具条里没有「中译替换版」按钮"
    assert "onclick=\"buildReplacedPdf()\"" in src, "按钮没接上 buildReplacedPdf()"
    assert "async function buildReplacedPdf(" in src, "没有 buildReplacedPdf()"
    assert "/api/paper/build_replaced_pdf" in src, "前端没调新端点"


def test_frontend_replaced_view_does_not_retranslate_and_fills_width():
    """替换版视图：① 不再翻一遍（白花钱）② 右栏收起 ③ PDF 独占整栏。

    ⚠ 「藏掉右栏」**不等于**「PDF 变宽」：`.pdf-split` 是两列 grid
    （`repeat(2, minmax(0,1fr))`），藏掉第二列后第一列仍只占一半宽
    （实测 1600px 视口里容器还是 785px）→ 必须改列数。
    """
    src = INDEX.read_text(encoding="utf-8")
    assert "_pdfReplacedView" in src, "没有「替换版视图」标志"
    assert "if (!_pdfReplacedView) startPdfTranslationQueue();" in src, \
        "替换版仍然会启动翻译队列 → 12 页白翻一遍"
    assert "if (_pdfReplacedView) return;" in src, \
        "翻译队列没在替换版视图里刹车"
    assert "pdf-split-single" in src, \
        "没把 .pdf-split 改成单列 → PDF 只占半屏"
    assert 'classList.toggle("pdf-split-single"' in src, \
        "单列类没被挂上"
    assert "_中译替换版" in src and "replaced" in src, \
        "替换版判定没跟着文件走 → 缩放重排后标志会丢"


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


def test_broken_marker_residue_is_removed():
    """抽取层偶尔吐**半截**标记（实测「为避免混淆�/T18⟧」）→ 残骸不许进 PDF。"""
    from microbench.replaced_pdf import clean_translation
    out = clean_translation("为避免混淆�/T18⟧")
    assert "⟧" not in out, out
    assert "/T18" not in out, "坏标记的残骸没清掉：" + out
    assert "为避免混淆" in out, "正文被误删：" + out


def test_clean_translation_strips_markdown_and_latex_guard_source():
    """源码里必须真的做「换行 → 空格」，而不是碰巧。"""
    src = _src()
    assert re.search(r'out = re\.sub\(r"\\s\*\\n\\s\*", " ", out\)', src), \
        "clean_translation 没把换行变成空格"


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
# 端到端：真造一篇 PDF，真构建，真核对
# ---------------------------------------------------------------------------
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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
