# -*- coding: utf-8 -*-
"""公式原图 + 表格渲染 + 快照版本化 —— 2026-10-24 三个用户反馈的护栏。

用户原话：
  - "表格的翻译，他都是很乱非常之乱……原有的表格形式完全看不出来，
     而且占的篇幅非常大"
  - "左右翻译的时候……它还是会先翻译这个先出现字的那一列"
  - "公式渲染还有问题……基本没有和文中的公式一样的……分子分母分不清谁是谁"

对应三条契约：
  1. 公式不再靠猜：抽取层只记坐标（⟦FORMULA:x0,y0,x1,y1⟧），前端拿坐标去 PDF
     原页裁图 —— 和论文里一模一样，绝不会把分子分母装反。
  2. Markdown 表格渲染成真正的 <table>，不再是一堆竖线。
  3. 抽取算法一升级，旧快照整份作废（否则用户看到的还是老算法翻的译文，
     会以为"修复没生效"）。
"""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

VAULT = Path(__file__).resolve().parent.parent
JACERS = (VAULT / "01_Literature" / "04_Survey_综述" / "01_论文原文_PDF" /
          "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")
INDEX_HTML = VAULT / "microbench" / "static" / "index.html"
JS_TEST = VAULT / "microbench" / "_test_reader_markdown.js"

FORMULA_RE = re.compile(r"⟦FORMULA:(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)⟧")

pytestmark = pytest.mark.skipif(
    not JACERS.exists(), reason="vault 里没有 JACERS 样本，跳过")


# ---------------------------------------------------------------------------
# 1) 公式：抽取层给坐标，位置必须落在公式真实所在处
# ---------------------------------------------------------------------------

def _page_text(page_no: int) -> str:
    import pdfplumber
    from microbench import paper_text as pt
    with pdfplumber.open(str(JACERS)) as pdf:
        return pt._extract_page_text_raw(pdf.pages[page_no - 1])


def test_formula_is_marked_with_coordinates():
    """第 2 页的公式 (2) 必须变成带坐标的标记（而不是一堆碎片行）。"""
    text = _page_text(2)
    marks = FORMULA_RE.findall(text)
    assert marks, "第 2 页没有找到带坐标的公式标记"
    x0, y0, x1, y1 = (float(v) for v in marks[0])
    assert x1 > x0 and y1 > y0, f"公式坐标无效: {marks[0]}"
    # 公式 (2) 在左栏顶部：x 落在左栏（< 300），y 在页面上部（< 250）
    assert 0 < x0 < 300, f"公式 x 起点不在左栏: {x0}"
    assert 100 < y0 < 250, f"公式 y 起点不对: {y0}"


def test_formula_fragments_are_not_left_as_garbage():
    """公式碎片不能继续留在正文里（它们会被模型瞎猜）。"""
    text = _page_text(2)
    # 旧实现会把分子分母拆成这几行
    for frag in ("ndm gp", "11 31 33"):
        assert frag not in text, f"公式碎片 {frag!r} 还留在正文里"
    # 标记出现的位置在正文中间（不是整页开头）
    assert text.index(FORMULA_RE.search(text).group(0)) > 100


def test_formula_image_crop_is_the_real_equation():
    """按标记里的坐标裁图，裁出来的必须是论文里那个公式。"""
    fitz = pytest.importorskip("fitz")
    text = _page_text(2)
    m = FORMULA_RE.search(text)
    assert m, "没有公式标记"
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    with fitz.open(str(JACERS)) as doc:
        page = doc.load_page(1)
        pix = page.get_pixmap(clip=fitz.Rect(x0, y0, x1, y1),
                              matrix=fitz.Matrix(4, 4), alpha=False)
        png = pix.tobytes("png")
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "裁出来不是 PNG"
    assert pix.width > 100 and pix.height > 20, "裁出来的图太小，不像公式"


def test_prose_is_not_mistaken_for_formula():
    """正文段落不能被当成公式（否则整页正文变成图）。"""
    text = _page_text(2)
    marks = FORMULA_RE.findall(text)
    assert len(marks) <= 3, f"公式标记过多（{len(marks)} 个），正文被误判成公式了"
    # 明显是正文的内容还在
    assert "Individual Phases in ME Composites" in text
    assert "Correspondingly, Table II provides" in text


# ---------------------------------------------------------------------------
# 2) 表格：不能再是一堆竖线
# ---------------------------------------------------------------------------

def test_markdown_table_is_rendered_as_table():
    """用 node 跑前端真实代码：Markdown 表格必须渲染成 <table>。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 前端渲染护栏跳过")
    proc = subprocess.run([node, str(JS_TEST)], capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, f"前端渲染测试失败:\n{proc.stdout}\n{proc.stderr}"
    assert "0 failed" in proc.stdout, proc.stdout


def test_reader_html_has_table_and_formula_styles():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert ".md-table" in html, "缺少表格样式"
    assert ".formula-shot" in html, "缺少公式原图样式"
    assert "renderFormulaImages" in html, "公式原图渲染函数没接上"


def test_translation_prompt_asks_for_markdown_tables():
    """提示词要明确要求表格用 Markdown 表格输出。"""
    from microbench.app import TRANSLATION_SYSTEM_PROMPT
    assert "Markdown 表格" in TRANSLATION_SYSTEM_PROMPT
    assert "FORMULA" in TRANSLATION_SYSTEM_PROMPT


def test_sanitizer_keeps_formula_marker_but_repairs_broken_one():
    from microbench.translation import (FORMULA_TEXT_FALLBACK,
                                        sanitize_translation)
    good = sanitize_translation("公式如下 ⟦FORMULA:45.1,138.3,272.3,169.5⟧ 后文")
    assert "⟦FORMULA:45.1,138.3,272.3,169.5⟧" in good, "好的公式标记被弄丢了"
    # 模型把坐标抄坏（数量不对）→ 退回文字提示，绝不放行坏坐标
    bad = sanitize_translation("公式如下 ⟦FORMULA:45.1,138.3⟧ 后文")
    assert FORMULA_TEXT_FALLBACK in bad, "坏坐标没有退回文字提示"
    # 坐标前后有空格也要能修好
    spaced = sanitize_translation("⟦FORMULA: 45.1 , 138.3 , 272.3 , 169.5⟧")
    assert "⟦FORMULA:45.1,138.3,272.3,169.5⟧" in spaced


# ---------------------------------------------------------------------------
# 3) 快照版本化：抽取算法一升级，旧快照整份作废
# ---------------------------------------------------------------------------

def test_snapshot_with_old_extractor_version_is_dropped(client, sandbox):
    """旧版快照不能被恢复 —— 否则用户看到的还是老算法翻的译文。"""
    from microbench.paper_text import EXTRACTOR_VERSION
    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "ver_test_01", "paper_title": "T",
        "relative_path": "a.pdf", "paper_mtime": 1.0,
        "pages": [{"page_num": 1, "text_hash": "a" * 16,
                   "translation": "旧译文"}],
    })
    assert r.status_code == 200, r.text
    # 把文件里的版本号改成旧的
    import json
    fp = sandbox / "logs" / "_pdf_snapshots" / "ver_test_01.json"
    data = json.loads(fp.read_text(encoding="utf-8"))
    assert data.get("extractor_version") == EXTRACTOR_VERSION, \
        "保存快照时没有记抽取算法版本"
    data["extractor_version"] = EXTRACTOR_VERSION - 1
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    r2 = client.get("/api/pdf/snapshot/load?paper_id=ver_test_01")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body.get("stale_extractor") is True, "旧快照没有被标记为过期"
    assert body.get("pages") == [], "旧快照的页没有被作废（会被照旧恢复）"


def test_snapshot_with_current_version_is_kept(client):
    """版本对得上的快照照常恢复。"""
    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "ver_test_02", "paper_title": "T",
        "relative_path": "a.pdf", "paper_mtime": 1.0,
        "pages": [{"page_num": 1, "text_hash": "a" * 16,
                   "translation": "新译文"}],
    })
    assert r.status_code == 200, r.text
    r2 = client.get("/api/pdf/snapshot/load?paper_id=ver_test_02")
    body = r2.json()
    assert not body.get("stale_extractor")
    assert len(body.get("pages") or []) == 1


# ---------------------------------------------------------------------------
# 4) 公式裁图接口：路径安全 + 真的返回 PNG
# ---------------------------------------------------------------------------

def test_formula_image_endpoint_rejects_path_traversal(client):
    r = client.get("/api/paper/formula_image",
                   params={"relative_path": "../../etc/passwd", "page": 1,
                           "rect": "10,10,100,100"})
    assert r.status_code in (400, 404), f"越权路径没被拦: {r.status_code}"


def test_formula_image_endpoint_returns_png(client, sandbox):
    """拿 vault 里真的 PDF 裁一张图。"""
    import shutil as _sh
    pdf_dir = sandbox / "01_Literature"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    target = pdf_dir / "sample.pdf"
    _sh.copyfile(JACERS, target)
    r = client.get("/api/paper/formula_image",
                   params={"relative_path": "01_Literature/sample.pdf",
                           "page": 2, "rect": "45.1,138.3,272.3,169.5"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_formula_image_endpoint_rejects_bad_rect(client, sandbox):
    pdf_dir = sandbox / "01_Literature"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    target = pdf_dir / "sample2.pdf"
    shutil.copyfile(JACERS, target)
    # 五个数字 → 坐标格式不对
    r = client.get("/api/paper/formula_image",
                   params={"relative_path": "01_Literature/sample2.pdf",
                           "page": 2, "rect": "1,2,3,4,5"})
    assert r.status_code == 400, r.text
    # 太短的 rect → FastAPI 参数校验拦下（422）
    r2 = client.get("/api/paper/formula_image",
                    params={"relative_path": "01_Literature/sample2.pdf",
                            "page": 2, "rect": "abc"})
    assert r2.status_code in (400, 422), r2.text


def test_formula_image_pixel_cap(client, sandbox):
    """整页 + 最大 scale 也不能把内存打爆（必须有像素上限）。"""
    pdf_dir = sandbox / "01_Literature"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    target = pdf_dir / "sample3.pdf"
    shutil.copyfile(JACERS, target)
    r = client.get("/api/paper/formula_image",
                   params={"relative_path": "01_Literature/sample3.pdf",
                           "page": 2, "rect": "0,0,600,800", "scale": 8})
    assert r.status_code == 200, r.text
    # 返回的 PNG 像素数必须在上限内（4M）
    import struct
    w, h = struct.unpack(">II", r.content[16:24])
    assert w * h <= 4_200_000, f"裁图像素数失控: {w}x{h}"
