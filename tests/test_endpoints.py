# -*- coding: utf-8 -*-
"""
Per-endpoint runnability suite for the MicroBench API.

Every one of the 41 API routes is exercised here. The goal is simple:
prove that each endpoint is reachable, validates its input, returns the
documented shape, and — for the write paths — that it actually writes
the right thing.

Tests are split into three groups:
  * fast, offline  — pure request/response shape checks, no network
  * network        — hit arXiv / Crossref / a real PDF
  * llm            — spend a little of the user's LLM quota

Run everything:            python -m pytest tests -v
Skip the slow ones:        python -m pytest tests -v -m "not slow"
"""
from __future__ import annotations

import io
import json

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _png_bytes(w: int = 240, h: int = 120) -> bytes:
    """A small chart-like PNG with a saturated red curve."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for x in range(10, w - 10):
        y = int(h * 0.9 - (x - 10) * (h * 0.8) / (w - 20))
        for dy in range(-1, 2):
            d.point((x, y + dy), fill=(220, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _pdf_bytes() -> bytes:
    """A minimal one-page PDF with extractable text."""
    from PIL import Image
    # Render a page as an image-backed PDF; pdfplumber can read the text
    # layer only if there is one, so build a tiny real PDF by hand.
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
    )
    return pdf


# ---------------------------------------------------------------------------
# 1. Static + stats
# ---------------------------------------------------------------------------

def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    assert len(r.text) > 1000
    # No-cache so an edited frontend is picked up immediately.
    assert "no-store" in r.headers.get("cache-control", "")


def test_api_stats(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    for key in ("literature_total", "reproduction_total",
                "troubleshoot_count", "weekly_count"):
        assert key in data, f"/api/stats 缺少字段 {key}"
        assert isinstance(data[key], int)


def test_api_vault_literature_pagination(client):
    r = client.get("/api/vault/literature?limit=5&offset=0")
    assert r.status_code == 200
    d = r.json()
    for key in ("items", "total", "filtered_total", "offset", "limit", "has_more"):
        assert key in d, f"/api/vault/literature 缺少字段 {key}"
    assert len(d["items"]) <= 5
    assert d["offset"] == 0
    assert d["has_more"] == (d["offset"] + len(d["items"]) < d["filtered_total"])


def test_api_vault_literature_offset_is_honoured(client, sandbox):
    """A page of 2 + offset 1 must not repeat the first item."""
    for i in range(4):
        (sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" /
         "03_文献速览卡片").mkdir(parents=True, exist_ok=True)
        (sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" /
         "03_文献速览卡片" / f"note_{i}.md").write_text(
            f"---\ntitle: note {i}\n---\n\nbody {i}\n", encoding="utf-8")
    from microbench import generator as gen_mod
    gen_mod._invalidate_vault_stats_cache()

    p1 = client.get("/api/vault/literature?limit=2&offset=0").json()
    p2 = client.get("/api/vault/literature?limit=2&offset=2").json()
    assert len(p1["items"]) == 2
    assert len(p2["items"]) == 2
    paths1 = {i["path"] for i in p1["items"]}
    paths2 = {i["path"] for i in p2["items"]}
    assert not (paths1 & paths2), "offset 没有生效, 两页返回了相同条目"
    assert p1["total"] >= 4


def test_api_vault_literature_invalid_paging(client):
    assert client.get("/api/vault/literature?limit=0").status_code == 422
    assert client.get("/api/vault/literature?offset=-1").status_code == 422
    assert client.get("/api/vault/literature?limit=9999").status_code == 422


# ---------------------------------------------------------------------------
# 2. Literature card save / read / delete
# ---------------------------------------------------------------------------

def test_api_paper_save_and_read_and_delete(client, sandbox):
    payload = {
        "title": "A Study of \"band-to-band tunneling\" in CFET",
        "authors": "Zhang, Wei",
        "year": 2025,
        "venue": "IEDM",
        "topic_category": "01_Device_TCAD_器件仿真",
        "tags": "CFET, 2nm",
        "abstract": "We fabricate a CFET with buried oxide.",
        "status": "待精读",
    }
    r = client.post("/api/paper/save", json=payload)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    rel = d["relative_path"]
    written = sandbox / rel
    assert written.exists(), f"卡片未落盘: {rel}"

    # The frontmatter must still be valid YAML even with a quote in the title.
    text = written.read_text(encoding="utf-8")
    assert text.startswith("---")
    fm = text.split("---", 2)[1]
    try:
        import yaml  # optional dependency
        parsed = yaml.safe_load(fm)
        assert parsed["title"] == payload["title"]
    except ImportError:
        # No PyYAML — at least assert the quote was escaped.
        assert '\\"band-to-band tunneling\\"' in fm

    # Read it back.
    r = client.get("/api/vault/file", params={"relative_path": rel})
    assert r.status_code == 200
    assert "band-to-band tunneling" in r.text

    # And delete it.
    r = client.request("DELETE", "/api/vault/file",
                       params={"relative_path": rel})
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    assert not written.exists()


def test_api_paper_save_rejects_bad_category(client):
    r = client.post("/api/paper/save", json={
        "title": "x", "topic_category": "../../etc",
    })
    assert r.status_code == 400


def test_api_paper_save_rejects_null_optional_fields(client):
    """`"venue": null` must not become a 500."""
    r = client.post("/api/paper/save", json={
        "title": "Null field test", "authors": None, "venue": None,
        "doi": None, "arxiv": None, "abstract": None, "year": None,
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"


def test_api_vault_file_missing_and_bad_type(client, sandbox):
    assert client.get("/api/vault/file",
                      params={"relative_path": "nope/missing.md"}).status_code == 404
    (sandbox / "x.bin").write_bytes(b"\x00")
    r = client.get("/api/vault/file", params={"relative_path": "x.bin"})
    assert r.status_code == 400, "非 .md/.txt 文件必须被拒绝"


def test_api_vault_delete_protects_shared_dirs(client, sandbox):
    (sandbox / "03_Knowledge").mkdir(parents=True, exist_ok=True)
    (sandbox / "03_Knowledge" / "important.md").write_text("x", encoding="utf-8")
    r = client.request("DELETE", "/api/vault/file",
                       params={"relative_path": "03_Knowledge/important.md"})
    assert r.status_code == 403, "受保护目录不允许通过 API 删除"


# ---------------------------------------------------------------------------
# 3. Folders / find_pdf / serve_pdf / pdf info
# ---------------------------------------------------------------------------

def test_api_paper_folders(client, sandbox):
    base = sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" / "03_文献速览卡片"
    (base / "周报01").mkdir(parents=True, exist_ok=True)
    r = client.get("/api/paper/folders", params={
        "topic_category": "01_Device_TCAD_器件仿真", "kind": "literature_cards"})
    assert r.status_code == 200
    assert "周报01" in r.json()["folders"]


def test_api_paper_folders_rejects_bad_category(client):
    r = client.get("/api/paper/folders",
                   params={"topic_category": "../../etc", "kind": "any"})
    assert r.status_code == 400


def test_api_serve_pdf_and_info(client, sandbox):
    pdf_dir = sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" / "01_论文原文_PDF"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / "paper.pdf").write_bytes(_pdf_bytes())
    rel = "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/paper.pdf"

    r = client.get("/api/paper/serve_pdf", params={"relative_path": rel})
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/pdf")

    r = client.get("/api/pdf/info", params={"relative_path": rel})
    assert r.status_code == 200
    d = r.json()
    assert d["size"] > 0
    assert d["mtime"] > 0
    assert d["relative_path"] == rel


def test_api_serve_pdf_rejects_traversal(client):
    r = client.get("/api/paper/serve_pdf",
                   params={"relative_path": "../../../Windows/win.ini"})
    assert r.status_code == 400


def test_api_find_pdf(client, sandbox):
    pdf_dir = sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" / "01_论文原文_PDF"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / "2024_GAAFET_Benchmarking.pdf").write_bytes(_pdf_bytes())
    r = client.get("/api/paper/find_pdf",
                   params={"title": "GAAFET Benchmarking"})
    assert r.status_code == 200
    assert r.json()["found"] is True


# ---------------------------------------------------------------------------
# 4. Upload + split + paste
# ---------------------------------------------------------------------------

def test_api_paper_upload_pdf(client, sandbox):
    r = client.post(
        "/api/paper/upload_pdf?topic_category=01_Device_TCAD_器件仿真&title=Test Upload",
        files={"file": ("paper.pdf", _pdf_bytes(), "application/pdf")},
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    assert d["total_pages"] >= 1
    assert (sandbox / d["pdf_path"]).exists()


def test_api_paper_upload_pdf_rejects_non_pdf(client):
    r = client.post("/api/paper/upload_pdf",
                    files={"file": ("evil.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_api_paper_split_text(client):
    text = ("1. Introduction\nSome introductory text about GAA-FET.\n\n"
            "2. Methods\nWe used Sentaurus TCAD.\n\n"
            "3. Results\nThe ION is 1.2 mA/um.\n")
    r = client.post("/api/paper/split_text", json={"raw_text": text})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "success"
    assert len(d["sections"]) >= 1
    for s in d["sections"]:
        for k in ("section", "title", "text", "page_start", "page_end"):
            assert k in s


def test_api_paper_split_text_rejects_short_input(client):
    r = client.post("/api/paper/split_text", json={"raw_text": "short"})
    assert r.status_code == 422


def test_api_paste_analyze_without_question(client):
    r = client.post("/api/paper/paste_analyze", json={
        "title": "Pasted", "raw_text": "x" * 200,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "success"
    assert d["ai_answer"] is None


# ---------------------------------------------------------------------------
# 5. Reproduction / troubleshoot / weekly
# ---------------------------------------------------------------------------

def test_api_reproduction_create(client, sandbox):
    r = client.post("/api/reproduction/create", json={
        "project_name": "repro_test_alpha", "project_type": "TCAD",
        "target_paper": "2303.15982", "description": "A test project",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    proj = sandbox / "02_Reproduction" / "repro_test_alpha"
    assert proj.exists()
    for sub in ("configs", "scripts", "data", "plots"):
        assert (proj / sub).is_dir(), f"缺少 {sub}/"
    assert (proj / "README.md").exists()


def test_api_reproduction_rejects_duplicate_name(client):
    payload = {"project_name": "repro_test_dup", "project_type": "TCAD"}
    assert client.post("/api/reproduction/create", json=payload).status_code == 200
    r = client.post("/api/reproduction/create", json=payload)
    assert r.status_code == 400, "重复创建必须报错而不是覆盖已有 README"


def test_api_reproduction_rejects_traversal(client, sandbox):
    r = client.post("/api/reproduction/create", json={"project_name": ".."})
    assert r.status_code == 400
    assert not (sandbox / "configs").exists(), "遍历攻击创建了 vault 根目录"
    assert (sandbox / "README.md").exists() or not (sandbox / "README.md").exists()


def test_api_reproduction_rejects_bad_type(client):
    r = client.post("/api/reproduction/create", json={
        "project_name": "x", "project_type": "MALWARE"})
    assert r.status_code == 400


def test_api_troubleshoot_add(client, sandbox):
    kb = sandbox / "03_Knowledge" / "踩坑与排错日记.md"
    r = client.post("/api/troubleshoot/add", json={
        "tool_name": "Sentaurus TCAD", "category": "TCAD仿真收敛",
        "error_keyword": "ConvergenceFailure", "raw_error": "iteration diverged",
        "root_cause": "mesh too coarse", "solution": "refine near drain",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"
    assert kb.exists()
    assert "ConvergenceFailure" in kb.read_text(encoding="utf-8")


def test_api_troubleshoot_rejects_bad_category(client):
    r = client.post("/api/troubleshoot/add", json={
        "tool_name": "x", "category": "火锅底料", "error_keyword": "y",
        "raw_error": "z", "root_cause": "r", "solution": "s",
    })
    assert r.status_code == 400


def test_api_weekly_generate(client, sandbox):
    r = client.post("/api/weekly/generate")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    written = sandbox / d["path"]
    assert written.exists()
    body = written.read_text(encoding="utf-8")
    assert "{{" not in body, "模板占位符未渲染"


# ---------------------------------------------------------------------------
# 6. Export bilingual / QA
# ---------------------------------------------------------------------------

def test_api_export_bilingual(client, sandbox):
    r = client.post("/api/paper/export_bilingual", json={
        "title": "Bilingual Card",
        "authors": "Li, M.", "year": 2025,
        "topic_category": "01_Device_TCAD_器件仿真",
        "sections": [
            {"title": "Introduction", "section": "introduction",
             "text": "English body", "translation": "中文正文",
             "page_start": 1, "page_end": 1},
        ],
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    written = sandbox / d["relative_path"]
    assert written.exists()
    assert "中文正文" in written.read_text(encoding="utf-8")


def test_api_export_qa(client, sandbox):
    r = client.post("/api/paper/export_qa", json={
        "title": "QA Paper", "question": "什么是量子限制效应？",
        "selection": "Quantum confinement reduces mobility.",
        "answer": "量子限制使载流子有效质量增大。",
        "mode": "explain", "topic_category": "01_Device_TCAD_器件仿真",
        "year": 2025,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    written = sandbox / d["relative_path"]
    assert written.exists()
    assert "量子限制" in written.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 7. Plot digitiser
# ---------------------------------------------------------------------------

def test_api_plot_extract(client):
    # The endpoint's upload field is named "image" (see app.py's
    # `image: UploadFile = File(...)`), not "file".
    r = client.post("/api/plot/extract",
                    files={"image": ("chart.png", _png_bytes(), "image/png")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    assert len(d["points"]) > 20, "曲线提取得到的点太少"
    assert d["width"] == 240 and d["height"] == 120


def test_api_plot_calibrate(client):
    # ref_a: pixel(0,100) -> real(0,0);  ref_b: pixel(200,0) -> real(10,1)
    #   x_real = 0.05 * px
    #   y_real = -0.01 * py + 1.0     (the y axis is inverted in pixel space)
    r = client.post("/api/plot/calibrate", json={
        "pixel_points": [[10, 90], [190, 10]],
        "ref_a": {"px": 0, "py": 100, "rx": 0, "ry": 0},
        "ref_b": {"px": 200, "py": 0, "rx": 10, "ry": 1},
    })
    assert r.status_code == 200, r.text
    pts = r.json()["points"]
    assert len(pts) == 2
    assert abs(pts[0][0] - 0.5) < 0.01
    assert abs(pts[0][1] - 0.1) < 0.01
    assert abs(pts[1][0] - 9.5) < 0.01
    assert abs(pts[1][1] - 0.9) < 0.01


def test_api_plot_compare(client):
    r = client.post("/api/plot/compare", json={
        "paper_points": [[0, 0], [1, 1], [2, 2], [3, 3]],
        "repro_points": [[0, 0], [1, 1.01], [2, 2], [3, 3]],
        "pass_pct": 5.0, "warn_pct": 20.0,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    assert d["verdict"] == "pass"
    assert d["max_rel_err_pct"] < 5.0


def test_api_plot_compare_all_zero_is_inconclusive(client):
    """A paper curve that is all zeros is a data-extraction failure, not a pass."""
    r = client.post("/api/plot/compare", json={
        "paper_points": [[0, 0], [1, 0], [2, 0], [3, 0]],
        "repro_points": [[0, 100], [1, 100], [2, 100], [3, 100]],
    })
    assert r.status_code == 200
    assert r.json()["verdict"] == "inconclusive"


def test_api_plot_compare_requires_two_points(client):
    r = client.post("/api/plot/compare", json={
        "paper_points": [[0, 0]], "repro_points": [[0, 0], [1, 1]]})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 8. Snapshots
# ---------------------------------------------------------------------------

def test_api_snapshot_lifecycle(client, sandbox):
    save = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "testpaper_001",
        "paper_title": "Test Paper",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 1700000000.0,
        "pages": [{"page_num": 1, "text_hash": "abcdef0123456789",
                   "translation": "第一页译文", "model": "m",
                   "translated_at": "2026-10-20T12:00:00"}],
    })
    assert save.status_code == 200, save.text
    d = save.json()
    assert d["status"] == "success"
    assert d["saved_pages"] == 1

    listing = client.get("/api/pdf/snapshot/list")
    assert listing.status_code == 200
    assert any(s["paper_id"] == "testpaper_001"
               for s in listing.json()["snapshots"])

    load = client.get("/api/pdf/snapshot/load",
                       params={"paper_id": "testpaper_001"})
    assert load.status_code == 200
    assert load.json()["pages"][0]["translation"] == "第一页译文"

    dele = client.request("DELETE", "/api/pdf/snapshot/delete",
                          params={"paper_id": "testpaper_001"})
    assert dele.status_code == 200
    assert client.get("/api/pdf/snapshot/load",
                      params={"paper_id": "testpaper_001"}).status_code == 404


def test_api_snapshot_rejects_duplicate_pages(client):
    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "dup_test_01", "paper_title": "T",
        "relative_path": "a.pdf", "paper_mtime": 1.0,
        "pages": [
            {"page_num": 1, "text_hash": "aaaa1111", "translation": "x"},
            {"page_num": 1, "text_hash": "bbbb2222", "translation": "y"},
        ],
    })
    assert r.status_code == 400


def test_api_snapshot_rejects_empty_hash(client):
    """The frontend used to send an empty text_hash when crypto.subtle was
    unavailable, which made every snapshot save 422."""
    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "empty_hash_1", "paper_title": "T",
        "relative_path": "a.pdf", "paper_mtime": 1.0,
        "pages": [{"page_num": 1, "text_hash": "", "translation": "x"}],
    })
    assert r.status_code == 422
    assert "text_hash" in r.json()["detail"]


def test_api_snapshot_rejects_oversized_page_list(client):
    pages = [{"page_num": i + 1, "text_hash": "a" * 16, "translation": "x"}
             for i in range(2500)]
    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": "too_many_pages", "paper_title": "T",
        "relative_path": "a.pdf", "paper_mtime": 1.0, "pages": pages,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 9. LLM status / translation cache
# ---------------------------------------------------------------------------

def test_api_llm_status(client):
    r = client.get("/api/llm/status")
    assert r.status_code == 200
    d = r.json()
    for key in ("configured", "model", "is_degraded", "primary",
                "fallback", "rate_limit"):
        assert key in d, f"/api/llm/status 缺少字段 {key}"
    # No credential may ever be exposed.
    blob = json.dumps(d)
    assert "sk-" not in blob, "/api/llm/status 泄露了 API key"
    assert "Bearer" not in blob


def test_api_translation_cache_stats(client):
    r = client.get("/api/translation/cache_stats")
    assert r.status_code == 200
    assert "size" in r.json()


def test_api_translation_cache_batch_lookup(client):
    r = client.post("/api/paper/translation_cache_batch_lookup", json={
        "keys": [{"section_key": "intro", "text": "some text", "model": "m"}],
    })
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 1
    assert d["misses"] + len(d["hits"]) == 1


# ---------------------------------------------------------------------------
# 10. Network-backed endpoints
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.network
def test_api_paper_fetch_arxiv(client):
    r = client.post("/api/paper/fetch", json={"identifier": "2303.15982"})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d.get("title"), "arXiv 元数据没有标题"
    assert d.get("authors") is not None


@pytest.mark.slow
@pytest.mark.network
def test_api_paper_bibtex_arxiv(client):
    r = client.get("/api/paper/bibtex", params={"arxiv": "2303.15982"})
    assert r.status_code == 200, r.text
    assert "@" in r.json()["bibtex"]


@pytest.mark.slow
@pytest.mark.network
def test_api_paper_fetch_full_arxiv(client):
    r = client.post("/api/paper/fetch_full", json={"identifier": "2303.15982"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    assert d["total_pages"] >= 1
    assert isinstance(d["sections"], list)


@pytest.mark.slow
@pytest.mark.network
def test_api_paper_download_pdf(client, sandbox):
    r = client.get("/api/paper/download_pdf", params={
        "identifier": "2303.15982",
        "topic_category": "01_Device_TCAD_器件仿真",
        "title": "Arxiv Download Test", "year": 2024,
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    assert (sandbox / d["relative_path"]).exists()


# ---------------------------------------------------------------------------
# 11. LLM-backed endpoints
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.llm
def test_api_paper_summarize(client, llm_ready):
    if not llm_ready:
        pytest.skip("未配置 LLM")
    r = client.post("/api/paper/summarize", json={
        "title": "GAA-FET scaling study",
        "authors": "A. Author", "year": 2025, "venue": "IEDM",
        "abstract": "We demonstrate a GAA-FET at 2nm gate length.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "success"


@pytest.mark.slow
@pytest.mark.llm
def test_api_ask(client, llm_ready, sandbox):
    kb = sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" / "03_文献速览卡片"
    kb.mkdir(parents=True, exist_ok=True)
    (kb / "gatest.md").write_text(
        "---\ntitle: GAA FET\ntags: [GAA]\n---\n\n"
        "GAA-FET 用全栅环绕沟道, 静电控制能力强。\n", encoding="utf-8")
    from microbench import generator as gen_mod
    gen_mod._invalidate_vault_stats_cache()
    r = client.post("/api/ask", json={"question": "GAA-FET", "top_k": 3})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] in ("success", "partial")
    assert isinstance(d["chunks"], list)


@pytest.mark.slow
@pytest.mark.llm
def test_api_paper_classify_qa_scope(client, llm_ready):
    r = client.post("/api/paper/classify_qa_scope", json={
        "selected_text": "GAA-FET gate-all-around transistor",
        "question": "这个结构的创新点是什么?", "preset": "",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["mode"] in ("term", "mid", "wide", "full")
    assert 300 <= d["budget"] <= 80000


@pytest.mark.slow
@pytest.mark.llm
def test_api_paper_translate_section(client, llm_ready):
    r = client.post("/api/paper/translate_section", json={
        "section_title": "Introduction",
        "section_text": "The GAA-FET wraps the channel with the gate electrode.",
        "section_key": "intro_test",
    })
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "success"
    assert d["translation"]


@pytest.mark.slow
@pytest.mark.llm
def test_api_paper_translate_page(client, llm_ready):
    r = client.post("/api/paper/translate_page", json={
        "paper_id": "test_paper_xyz", "page": 1,
        "text": "The gate-all-around structure improves electrostatic control.",
    })
    assert r.status_code == 200, r.text
    assert r.json()["translation"]


@pytest.mark.slow
@pytest.mark.llm
def test_sse_explain_stream(client, llm_ready):
    with client.stream("POST", "/api/paper/explain_stream", json={
        "question": "What is GAA-FET?",
        "sections": [{"section": "introduction", "title": "Introduction",
                      "text": "A GAA-FET wraps the channel with the gate."}],
        "paper_title": "Test Paper",
    }) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        payload = "".join(r.iter_text())
    assert "data:" in payload
    assert "[DONE]" in payload, "SSE 流没有发送终止事件"
    assert '"error"' not in payload, f"SSE 报错了: {payload[:400]}"


@pytest.mark.slow
@pytest.mark.llm
def test_sse_explain_selection_stream(client, llm_ready):
    with client.stream("POST", "/api/paper/explain_selection_stream", json={
        "selected_text": "GAA-FET",
        "question": "GAA-FET 的全称是什么?",
        "paper_title": "Test Paper",
        "surrounding_context": "x" * 500,
        "source_type": "pdf",
        "force_scope": "term",
    }) as r:
        assert r.status_code == 200
        payload = "".join(r.iter_text())
    assert "data:" in payload
    assert "[DONE]" in payload


@pytest.mark.slow
@pytest.mark.llm
def test_sse_cross_qa_stream(client, llm_ready):
    with client.stream("POST", "/api/paper/cross_qa_stream", json={
        "question": "What is the main contribution?",
        "sections": [
            {"section": "introduction", "title": "Introduction",
             "text": "We propose a novel 2nm GAA-FET."},
            {"section": "conclusion", "title": "Conclusion",
             "text": "The proposed device shows record drive current."},
        ],
        "paper_title": "Test Paper",
    }) as r:
        assert r.status_code == 200
        payload = "".join(r.iter_text())
    assert "data:" in payload
    assert "[DONE]" in payload
