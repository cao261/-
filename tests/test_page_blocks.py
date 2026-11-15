# -*- coding: utf-8 -*-
"""段落块抽取（2026-11-12）—— 「译文按原文排版」的地基。

用户原话："就像那种翻译插件一样，他们和原文的排版是一模一样的，
没什么区别……原文是双栏，他们也是双栏"。

要让译文能**放回原文的位置**，抽取层必须给出段落级的坐标。
这部分测试守两件事：

  ① 判据本身对不对（合成行 → 段落切分，不依赖 PDF）
  ② 在真实双栏论文上，左右栏的块不能串（依赖 vault 里的样本，缺失就跳过）

⚠ **页级文本一个字都不能变**（EXTRACTOR_VERSION 不用 +1 的前提）——
   那条由 `test_page_text_is_unchanged_by_blocks` 用 git 里的旧版模块做 A/B。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

VAULT = REPO / "01_Literature" / "04_Survey_综述" / "01_论文原文_PDF"
JACERS = VAULT / "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf"

from microbench import paper_text as pt  # noqa: E402


def _line(top, bottom, x0, x1, text):
    return {"text": text, "x0": x0, "x1": x1, "top": top, "bottom": bottom}


# ---------------------------------------------------------------------------
# ① 判据：合成行 → 段落切分（不依赖 PDF，永远能跑）
# ---------------------------------------------------------------------------
def test_continuation_line_stays_in_same_paragraph():
    """普通续行（无缩进、行距正常）不该另起一段。"""
    lines = [
        _line(100, 110, 60, 300, "This is the first line of a paragraph"),
        _line(111, 121, 60, 300, "and this is just its continuation"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert len(blocks) == 1, "续行被切成了新段落"
    assert "continuation" in blocks[0]["text"]


def test_block_text_joins_lines_with_space():
    """段落块的正文必须**一行一句**（行与行之间是空格，不是换行）。

    为什么：PDF 的行折会原样进到模型输入 → 模型照着回车 →
    译文里下标/上标被拆成单独一行（实测 JACERS 第 2 页：
    "其中，a 为横向 ME 电压系数，v 为整体比\\nE,31\\nE 表示整个…"）。
    """
    lines = [
        _line(100, 110, 60, 300, "where a is transverse ME voltage"),
        _line(111, 121, 60, 300, "coefficient, v is the volume E,31 ratio"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert len(blocks) == 1
    assert "\n" not in blocks[0]["text"], "块文本里还有换行"
    assert "E,31 ratio" in blocks[0]["text"]


def test_heading_does_not_swallow_first_body_line():
    """⚠ 标题**不能**把正文的**第一行**吞进去（2026-11-12 实测）。

    实测 JACERS 第 2 页：
      标题行 "(1) Individual Phases in ME Composites"（6 词，真标题）
      下一行 "The first ME composites reported by Philips3,4 used BaTiO3 as"
      （10 词）→ 曾被当成"标题折行"并进标题块
      → 对照视图里标题的译文后面粘着一句正文。
    """
    lines = [
        _line(100, 110, 60, 300, "(1) Individual Phases in ME Composites"),
        _line(111, 121, 60, 300,
              "The first ME composites reported by Philips3,4 used BaTiO3 as"),
        _line(122, 132, 60, 300, "the piezoelectric phase, which has a"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert blocks, "没抽出块"
    heading = [b for b in blocks if b.get("kind") == "heading"]
    assert heading, "真标题没被切出来"
    assert "Individual Phases" in heading[0]["text"]
    assert "composites reported by" not in heading[0]["text"], \
        "正文的第一行被吞进标题块了"
    # 正文第一行必须还在正文里
    body = [b for b in blocks if b.get("kind") != "heading"]
    assert any("composites reported by" in b["text"] for b in body), \
        "正文的第一行丢了"


def test_heading_keeps_short_continuation_line():
    """真标题的折行（短）仍然要并进标题块（不能切碎了）。"""
    lines = [
        _line(100, 110, 60, 300, "(3) Ceramic and Terfenol-D (or Galfenol) Alloy ME"),
        _line(111, 121, 60, 300, "Composites"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    heading = [b for b in blocks if b.get("kind") == "heading"]
    assert heading, "标题没切出来"
    assert "Composites" in heading[0]["text"]


def test_indent_starts_new_paragraph():
    """首行缩进 ≥ 3pt → 新段落（IEEE 风格：靠缩进分段，不靠空行）。"""
    lines = [
        _line(100, 110, 60, 300, "First paragraph starts here"),
        _line(111, 121, 60, 300, "and continues on the next line"),
        _line(130, 140, 72, 300, "Second paragraph starts indented"),
        _line(141, 151, 60, 300, "and continues without indent"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert len(blocks) == 2, f"缩进分段没生效：{[b['text'][:20] for b in blocks]}"
    assert blocks[1]["split"] == "indent"
    assert "Second paragraph" in blocks[1]["text"]
    # 缩进段落的**续行**（没缩进）必须还在同一块里
    assert "continues without indent" in blocks[1]["text"]


def test_line_gap_starts_new_paragraph():
    """行间距 ≥ 5pt → 新段落（Elsevier 风格：靠空行分段，不缩进）。"""
    lines = [
        _line(100, 110, 60, 300, "First paragraph of the column"),
        _line(111, 121, 60, 300, "second line of the same paragraph"),
        _line(135, 145, 60, 300, "A separate paragraph after a gap"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert len(blocks) == 2
    assert blocks[1]["split"] == "gap"


def test_trailing_superscript_is_not_a_new_paragraph():
    """行尾的上标碎片（缩进特别大）不能被当成"新段落"。

    实测（JACERS 第 3 页）：一行的上标 "2 4" 被单独抽成一行，
    它的 x0 在行尾（缩进 225pt）→ 不处理的话一段话被切成两半，
    而且两个块在 y 上重叠（上标把上一行的 bottom 撑大了）。
    """
    lines = [
        _line(100, 110, 60, 300, "The ME voltage coefficient for the sintered"),
        _line(111, 116, 286, 300, "2 4"),
        _line(112, 122, 60, 300, "laminate shown in the figure has been reported"),
    ]
    blocks = pt._group_lines_into_blocks(lines, "L")
    assert len(blocks) == 1, f"行尾上标把段落切开了：{[b['text'][:24] for b in blocks]}"


def test_block_bbox_covers_its_lines():
    """块的坐标要罩住它自己的每一行。"""
    lines = [
        _line(100, 110, 60, 250, "Hello world"),
        _line(111, 121, 60, 310, "second line"),
    ]
    b = pt._group_lines_into_blocks(lines, "L")[0]
    x0, top, x1, bottom = b["bbox"]
    assert x0 <= 60 and x1 >= 310
    assert top <= 100 and bottom >= 121


def test_block_columns_are_tagged():
    """栏归属必须标出来（左 L / 右 R / 整宽 S）—— 前端靠它把译文放回原栏。"""
    lines = [_line(100, 110, 60, 300, "some text")]
    assert pt._group_lines_into_blocks(lines, "L")[0]["col"] == "L"
    assert pt._group_lines_into_blocks(lines, "R")[0]["col"] == "R"
    assert pt._group_lines_into_blocks(lines, "S")[0]["col"] == "S"


# ---------------------------------------------------------------------------
# ② 真实双栏论文：左右栏不能串
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")
def test_blocks_split_into_left_and_right_columns():
    blocks = pt.extract_page_blocks(JACERS, 3)
    assert blocks, "第 3 页应该抽出段落块"
    cols = {b["col"] for b in blocks}
    assert "L" in cols and "R" in cols, f"没有分出左右两栏：{cols}"

    import pdfplumber
    with pdfplumber.open(str(JACERS)) as doc:
        page = doc.pages[2]
        width, height = float(page.width), float(page.height)
        words = page.extract_words(x_tolerance=pt.PDF_X_TOLERANCE,
                                   use_text_flow=False, keep_blank_chars=False)
        y_split = pt._find_vertical_split(words, width, height)
        x_mid = pt._vote_column_gap(words, width, height, y_split)
    assert x_mid, "这一页应该探得到栏缝"

    for b in blocks:
        x0, top, x1, bottom = b["bbox"]
        assert 0 <= x0 <= width and 0 <= x1 <= width, f"块的 x 超出页面：{b['bbox']}"
        assert 0 <= top <= height and 0 <= bottom <= height, f"块的 y 超出页面：{b['bbox']}"
        if b["col"] == "L":
            assert x1 <= x_mid + 2, f"左栏块跑到右栏去了：{b['bbox']} (x_mid={x_mid})"
        if b["col"] == "R":
            assert x0 >= x_mid - 2, f"右栏块跑到左栏去了：{b['bbox']} (x_mid={x_mid})"


@pytest.mark.skipif(not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")
def test_blocks_follow_reading_order_left_then_right():
    """阅读顺序：左栏的块整体排在右栏的块之前（跟现有页级文本一致）。"""
    blocks = pt.extract_page_blocks(JACERS, 3)
    seq = [b["col"] for b in blocks]
    if "L" not in seq or "R" not in seq:
        pytest.skip("这一页没有分出两栏")
    last_left = max(i for i, c in enumerate(seq) if c == "L")
    first_right = min(i for i, c in enumerate(seq) if c == "R")
    assert last_left < first_right, f"左右栏在阅读顺序里交替了：{seq}"


@pytest.mark.skipif(not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")
def test_formula_block_is_detected():
    """公式块要能被单独认出来（前端要按它的坐标裁图贴译文）。"""
    blocks = pt.extract_page_blocks(JACERS, 3)
    kinds = {b["kind"] for b in blocks}
    assert "formula" in kinds, f"没认出公式块：{kinds}"


# ---------------------------------------------------------------------------
# ④ API：前端要能拿到这些块
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")
def test_page_blocks_endpoint(vault_client):
    """GET /api/paper/page_blocks 要能返回这一页的段落块。"""
    rel = str(JACERS.relative_to(REPO)).replace("\\", "/")
    r = vault_client.get("/api/paper/page_blocks",
                         params={"relative_path": rel, "page": 3})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["page"] == 3
    blocks = data["blocks"]
    assert blocks, "这一页应该抽出段落块"
    for b in blocks:
        assert set(["i", "kind", "col", "bbox", "text"]) <= set(b.keys())
        assert len(b["bbox"]) == 4
        assert b["col"] in ("L", "R", "S")


@pytest.mark.skipif(not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")
def test_page_blocks_endpoint_rejects_illegal_path(vault_client):
    """路径闸门必须还在（这是个能读任意文件的口子，要挡住）。"""
    r = vault_client.get("/api/paper/page_blocks",
                         params={"relative_path": "../../etc/passwd", "page": 1})
    assert r.status_code in (400, 404), r.text


# ---------------------------------------------------------------------------
# ③ 底线：页级文本一个字都不能变
# ---------------------------------------------------------------------------
def test_page_text_is_unchanged_by_blocks():
    """加了段落块之后，页级文本必须与 git HEAD 的旧版**逐字**相同。

    这条是 EXTRACTOR_VERSION 不用 +1 的前提 —— 一旦不成立，
    用户全部译文会作废重翻（跨设备同步）。
    """
    import importlib
    # ⚠ 旧模块有相对导入（from .paper_fetcher import ...），
    #   必须在 microbench 包里加载 → 临时放一份进包目录，用完即删。
    old_path = REPO / "microbench" / "_paper_text_old_tmp.py"
    if not old_path.exists():
        old_path.write_bytes(subprocess.run(
            ["git", "-C", str(REPO), "show", "HEAD:microbench/paper_text.py"],
            capture_output=True, check=True).stdout)
    try:
        spec = importlib.util.spec_from_file_location(
            "microbench._paper_text_old_tmp", old_path)
        old = importlib.util.module_from_spec(spec)
        sys.modules["microbench._paper_text_old_tmp"] = old
        spec.loader.exec_module(old)

        pdfs = sorted(VAULT.rglob("*.pdf")) if VAULT.is_dir() else []
        if not pdfs:
            pytest.skip("vault 里没有 PDF 样本，跳过")
        import pdfplumber
        checked = 0
        for pdf in pdfs[:3]:
            try:
                with pdfplumber.open(str(pdf)) as doc:
                    n = min(len(doc.pages), 2)
            except Exception:
                continue
            for page_no in range(1, n + 1):
                a = old.extract_page_text_from_path(pdf, page_no)
                b = pt.extract_page_text_from_path(pdf, page_no)
                assert a == b, f"{pdf.name} 第 {page_no} 页的文本变了"
                checked += 1
        assert checked, "一篇都没比对上"
    finally:
        try:
            old_path.unlink()
        except OSError:
            pass
