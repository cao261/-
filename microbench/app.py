"""
MicroBench V1.1 - FastAPI Local Backend Server.

V1.1 hardening:
- New POST /api/paper/download_pdf endpoint for arXiv PDF auto-download.
- New GET /api/paper/bibtex?arxiv=... or ?doi=... for BibTeX export.
- CORS: only allow localhost origins (was allow_origins=['*'], which violates
  spec when combined with allow_credentials=True).
- All endpoints wrap generator logic with explicit error mapping.
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Optional

from .paper_fetcher import (
    smart_fetch_paper,
    download_arxiv_pdf,
    fetch_crossref_doi_metadata,
    resolve_doi_landing_url,
    generate_bibtex,
)
from .generator import (
    BASE_DIR,
    get_vault_stats,
    save_literature_card,
    save_bilingual_reading_card,
    save_qa_card,
    create_reproduction_workspace,
    record_troubleshoot,
    generate_weekly_report,
    search_vault,
    ALLOWED_TOPIC_CATEGORIES,
)
from . import llm
from .plot_compare import (
    extract_curve_from_image,
    calibrate as calibrate_points,
    compute_metrics,
    _parse_csv_points,
)
from .paper_text import fetch_full_text, split_into_sections, extract_text_from_pdf

app = FastAPI(
    title="MicroBench - 微电子与半导体科研工作台",
    version="1.4.0",
    description="本地学术工作台 API: 文献抓取/全文/PDF/BibTeX/翻译/AI解读/复现工程/踩坑/周报/RAG",
)

# CORS — only allow localhost (UI is same-origin via FastAPI's static file serving).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5000",
        "http://localhost:5000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class FetchPaperRequest(BaseModel):
    identifier: str = Field(..., min_length=3, max_length=200)

class SavePaperRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    authors: Optional[str] = Field(default="", max_length=2000)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    venue: Optional[str] = Field(default="", max_length=300)
    doi: Optional[str] = Field(default="", max_length=200)
    arxiv: Optional[str] = Field(default="", max_length=50)
    topic_category: Optional[str] = Field(default="01_Device_TCAD_器件仿真", max_length=100)
    tags: Optional[str] = Field(default="TCAD", max_length=500)
    abstract: Optional[str] = Field(default="", max_length=10000)
    status: Optional[str] = Field(default="待精读", max_length=50)

class CreateReproductionRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=80)
    target_paper: Optional[str] = Field(default="", max_length=300)
    project_type: Optional[str] = Field(default="TCAD", max_length=10)
    toolchain: Optional[str] = Field(default="Sentaurus TCAD 2023", max_length=200)
    description: Optional[str] = Field(default="", max_length=2000)
    repo_url: Optional[str] = Field(default="", max_length=500)

class TroubleshootRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=100)
    category: str = Field(..., min_length=1, max_length=50)
    error_keyword: str = Field(..., min_length=1, max_length=200)
    raw_error: str = Field(..., min_length=1, max_length=10000)
    root_cause: str = Field(..., min_length=1, max_length=5000)
    solution: str = Field(..., min_length=1, max_length=5000)

class SummarizeRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    authors: Optional[str] = Field(default="", max_length=2000)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    venue: Optional[str] = Field(default="", max_length=300)
    abstract: Optional[str] = Field(default="", max_length=10000)

class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    top_k: Optional[int] = Field(default=5, ge=1, le=20)

class CalibrateRequest(BaseModel):
    pixel_points: list = Field(..., min_length=1, max_length=10000)
    ref_a: dict  # {px, py, rx, ry}
    ref_b: dict

class CompareRequest(BaseModel):
    # paper/repro can be supplied as either explicit points or CSV strings (or both).
    # The endpoint merges them, so we keep these optional with no min_length.
    paper_points: list = Field(default_factory=list, max_length=10000)
    repro_points: list = Field(default_factory=list, max_length=10000)
    paper_csv: Optional[str] = Field(default=None, max_length=200000)
    repro_csv: Optional[str] = Field(default=None, max_length=200000)
    pass_pct: Optional[float] = Field(default=5.0, ge=0.1, le=100.0)
    warn_pct: Optional[float] = Field(default=20.0, ge=0.1, le=100.0)

class FetchFullRequest(BaseModel):
    identifier: str = Field(..., min_length=3, max_length=300,
                            description="arXiv ID, DOI, 或 PDF 直接 URL")

class TranslateRequest(BaseModel):
    section_title: str = Field(..., min_length=1, max_length=200)
    # Large sections (Intro 30K+ chars) are valid; we cap at 60K on the server
    # before sending to LLM (see translation logic below).
    section_text: str = Field(..., min_length=10, max_length=60000)
    section_key: Optional[str] = Field(default=None, max_length=50,
                                        description="canonical section key e.g. 'introduction'")

class ExplainRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    sections: list = Field(..., min_length=1, max_length=20,
                            description="paper sections [{title, text}, ...]")
    paper_title: Optional[str] = Field(default="", max_length=300)


class ExplainSelectionRequest(BaseModel):
    selected_text: str = Field(..., min_length=2, max_length=15000,
                               description="用户划选的原文或译文片段")
    question: str = Field(..., min_length=1, max_length=1000,
                          description="用户针对该片段提出的问题")
    paper_title: Optional[str] = Field(default="", max_length=300)
    surrounding_context: Optional[str] = Field(default="", max_length=20000,
                                                description="上下文背景 (如所在章节或页面文本)")
    source_type: Optional[str] = Field(default="pdf", max_length=50,
                                       description="pdf / translation / original")


class TranslatePageRequest(BaseModel):
    """Translate one page of a paper (used by PDF dual-column reader).
    `paper_id` is a stable identifier (e.g. arXiv ID or DOI) — used to
    key the page-level translation cache so re-opening a paper is instant.
    """
    paper_id: str = Field(..., min_length=3, max_length=100)
    page: int = Field(..., ge=1, le=200, description="1-based page number")
    text: str = Field(..., min_length=10, max_length=15000,
                      description="Full page text (single chunk from pdfplumber)")


class PasteTextRequest(BaseModel):
    """User pastes raw paper text (e.g. copy from PDF reader) for AI analysis.
    Useful when PDF can't be fetched (paywalled IEEE etc.)."""
    title: str = Field(..., min_length=1, max_length=300)
    raw_text: str = Field(..., min_length=100, max_length=200000)
    question: Optional[str] = Field(default=None, max_length=500,
                                      description="若指定, 直接调用 LLM 回答; 否则返回拆分后的章节")

class SplitTextRequest(BaseModel):
    """Split already-pasted raw text into sections (no PDF download)."""
    raw_text: str = Field(..., min_length=100, max_length=200000)

class ExportBilingualRequest(BaseModel):
    """Export bilingual reading notes directly to Obsidian vault."""
    title: str = Field(..., min_length=1, max_length=500)
    topic_category: Optional[str] = Field(default="01_Device_TCAD_器件仿真", max_length=100)
    sections: list = Field(..., min_length=1, description="List of section dicts with title, text, translation")
    authors: Optional[str] = Field(default="", max_length=2000)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    qa_records: Optional[list] = Field(default=None, description="List of Q&A interaction records to bundle into note")

class ExportQARequest(BaseModel):
    """Export a single Q&A record directly into 04_AI划词答疑 in Obsidian vault."""
    title: str = Field(..., min_length=1, max_length=500, description="论文标题")
    question: str = Field(..., min_length=1, max_length=2000, description="用户提问")
    selection: Optional[str] = Field(default="", max_length=15000, description="选中的原文/译文")
    answer: str = Field(..., min_length=1, max_length=50000, description="AI 的解答")
    mode: Optional[str] = Field(default="explain", max_length=50, description="答疑模式: explain, math, experiment")
    topic_category: Optional[str] = Field(default="01_Device_TCAD_器件仿真", max_length=100)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    source_pdf: Optional[str] = Field(default=None, max_length=300)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>MicroBench static/index.html not found</h1>", status_code=404)
    # No-cache headers so users always get the latest HTML/JS after edits.
    # Without this, browsers may serve a stale cached version that references
    # old endpoints (e.g. export.arxiv.org HTTP path that times out).
    response = FileResponse(index_path, media_type="text/html")
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/api/stats")
async def api_stats():
    try:
        return get_vault_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 vault 统计失败: {str(e)}")

@app.post("/api/paper/fetch")
async def api_fetch_paper(req: FetchPaperRequest):
    try:
        data = smart_fetch_paper(req.identifier)
        # Also try to resolve landing URL for DOI papers (for manual download link)
        if data.get("doi") and not data.get("arxiv"):
            try:
                data["landing_url"] = resolve_doi_landing_url(data["doi"])
            except Exception:
                # Non-fatal — landing URL is best-effort
                data["landing_url"] = None
        return {"status": "success", "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"参数无效: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"抓取文献失败: {str(e)}")

@app.post("/api/paper/save")
async def api_save_paper(req: SavePaperRequest):
    try:
        res = save_literature_card(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存文献卡片失败: {str(e)}")

@app.post("/api/paper/export_bilingual")
async def api_export_bilingual(req: ExportBilingualRequest):
    """
    Export bilingual reading card with English + Chinese sections
    directly into 01_Literature in Obsidian vault.
    """
    try:
        res = save_bilingual_reading_card(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出双语卡片失败: {str(e)}")


@app.post("/api/paper/export_qa")
async def api_export_qa(req: ExportQARequest):
    """
    Export single selection Q&A note into 01_Literature/{category}/04_AI划词答疑/
    """
    try:
        res = save_qa_card(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出答疑笔记失败: {str(e)}")




@app.get("/api/paper/download_pdf")
async def api_download_pdf(
    identifier: str = Query(..., min_length=3, max_length=200, description="arXiv ID 或 DOI"),
    topic_category: str = Query(
        "01_Device_TCAD_器件仿真",
        description="目标分类, 必须在 ALLOWED_TOPIC_CATEGORIES 内",
    ),
    title: str = Query(..., min_length=1, max_length=300, description="用于 PDF 文件命名"),
    year: Optional[int] = Query(default=2024, ge=1900, le=2100),
):
    """
    Download a PDF to the appropriate literature folder.
    Only supports arXiv direct PDF. For DOI papers, returns landing_url
    so the frontend can prompt user to download manually.
    """
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"非法 topic_category: '{topic_category}'",
        )

    is_arxiv = "arxiv" in identifier.lower() or bool(
        __import__("re").search(r"^\d{4}\.\d{4,5}", identifier.strip())
    )
    if not is_arxiv:
        # For DOI, return landing URL instead of trying direct PDF
        try:
            landing = resolve_doi_landing_url(identifier)
            return {
                "status": "manual_required",
                "reason": "DOI 出版商通常需要浏览器渲染/登录, 无法直接下载 PDF",
                "landing_url": landing,
            }
        except ValueError as e:
            # Bad DOI format → user error, not upstream error
            raise HTTPException(status_code=400, detail=f"参数无效: {str(e)}")
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"无法解析 DOI 落地页: {str(e)}",
            )

    # arXiv path: build safe target path inside BASE_DIR
    from .generator import sanitize_filename, _safe_path
    safe_title = sanitize_filename(title)
    target_dir = _safe_path(BASE_DIR, "01_Literature", topic_category, "01_论文原文_PDF")
    target_dir.mkdir(parents=True, exist_ok=True)
    pdf_name = f"{year}_{safe_title[:30]}.pdf"
    save_to = target_dir / pdf_name
    # Re-validate (defense in depth)
    save_to = _safe_path(target_dir, pdf_name)

    try:
        info = download_arxiv_pdf(identifier, save_to)
        return {
            "status": "success",
            "file_name": pdf_name,
            "relative_path": str(save_to.relative_to(BASE_DIR)).replace("\\", "/"),
            "bytes": info["bytes"],
            "source": info["source"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"下载 PDF 失败: {str(e)}")


@app.get("/api/paper/serve_pdf")
async def api_serve_pdf(
    relative_path: str = Query(..., min_length=3, max_length=500,
                                description="相对 BASE_DIR 的 PDF 路径"),
):
    """
    Serve a PDF file from inside BASE_DIR (safe path validation).

    Used by the PDF.js dual-column reader to load the original PDF
    without exposing the filesystem.
    """
    from .generator import _safe_path
    try:
        # Defensive: _safe_path rejects '..' and absolute paths
        pdf_path = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"PDF 不存在: {relative_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail=f"不是 PDF 文件: {relative_path}")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/vault/file")
async def api_vault_file(
    relative_path: str = Query(..., min_length=3, max_length=500,
                                description="相对 BASE_DIR 的 .md / .txt 路径"),
):
    """
    Serve a text file from inside the vault (safe path validation).

    Used by the dashboard feed to open literature / QA / bilingual notes
    in a new browser tab. Only .md / .txt files inside BASE_DIR are
    served; PDFs and other binaries must use /api/paper/serve_pdf.
    """
    from .generator import _safe_path
    try:
        file_path = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {relative_path}")
    suffix = file_path.suffix.lower()
    if suffix not in (".md", ".markdown", ".txt"):
        raise HTTPException(status_code=400,
                            detail=f"不支持的文件类型: {suffix} (仅支持 .md/.txt)")
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = file_path.read_text(encoding="gbk", errors="replace")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/paper/find_pdf")
async def api_find_pdf(
    title: str = Query(..., min_length=1, max_length=300,
                       description="论文标题 (用于模糊匹配本地 PDF)"),
    year: Optional[int] = Query(default=None, ge=1900, le=2100),
):
    """
    Find a local PDF in the vault that matches a paper title.

    Walks 01_Literature/**.pdf and matches by:
      - exact or prefix normalized match
      - substring overlap on alphanumeric normalized stems
      - keyword token overlap (splits on non-alphanumeric chars)
    Returns the relative path of the best match, or null.
    """
    base = BASE_DIR / "01_Literature"
    if not base.exists():
        return {"found": False}

    import re

    def _norm(s: str) -> str:
        return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", s).lower()

    title_clean = _norm(title)
    # Tokenize on non-alphanumeric characters, keeping tokens length >= 3
    title_words = [_norm(w) for w in re.split(r"\W+", title) if len(_norm(w)) >= 3][:8]

    best = None
    best_score = 0
    for cat_dir in base.iterdir():
        if not cat_dir.is_dir():
            continue
        for pdf in cat_dir.rglob("*.pdf"):
            stem_clean = _norm(pdf.stem)
            score = 0

            # Year bonus if matched
            if year and pdf.stem.startswith(f"{year}_"):
                score += 15

            # Prefix or substring match
            if title_clean and stem_clean:
                if title_clean[:18] in stem_clean or stem_clean in title_clean:
                    score += 70
                elif title_clean[:10] in stem_clean:
                    score += 45

            # Word token overlap
            word_hits = sum(1 for w in title_words if w and w in stem_clean)
            if word_hits >= 2:
                score += 25 + word_hits * 10
            elif word_hits == 1 and len(title_words) <= 2:
                score += 35

            if score > best_score:
                best_score = score
                best = pdf

    if best and best_score >= 45:
        return {
            "found": True,
            "relative_path": str(best.relative_to(BASE_DIR)).replace("\\", "/"),
            "score": best_score,
            "file_name": best.name,
            "size_bytes": best.stat().st_size,
        }
    return {"found": False}


@app.post("/api/paper/upload_pdf")
async def api_upload_pdf(
    file: UploadFile = File(..., description="本地论文 PDF 文件"),
    topic_category: str = Query("01_Device_TCAD_器件仿真", description="学术专题归档"),
    title: Optional[str] = Query(default=None, description="论文标题 (可选，留空则自动提取)"),
):
    """
    Upload a local PDF file, extract full text, split into canonical sections,
    and save into 01_Literature for dual-column split-screen reading & AI Q&A.
    """
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        topic_category = "01_Device_TCAD_器件仿真"

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持上传 PDF 文件 (.pdf)")

    pdf_bytes = await file.read()
    if len(pdf_bytes) > 40 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF 文件不能超过 40MB")

    try:
        pages = extract_text_from_pdf(pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF 文本提取失败: {str(e)}")

    if not pages:
        raise HTTPException(status_code=400, detail="未能从 PDF 中识别出有效文字 (可能是扫描图片版 PDF)")

    sections = split_into_sections(pages)
    if not sections:
        full_text = "\n\n".join(p["text"] for p in pages)
        sections = [{
            "section": "full_text",
            "title": "Full Text",
            "text": full_text.strip(),
            "page_start": 1,
            "page_end": len(pages),
        }]

    import datetime
    from .generator import sanitize_filename, _safe_path

    paper_title = (title or "").strip()
    if not paper_title:
        p1_text = pages[0]["text"].strip() if pages else ""
        lines = [l.strip() for l in p1_text.splitlines() if len(l.strip()) > 8]
        if lines and len(lines[0]) < 180:
            paper_title = lines[0]
        else:
            paper_title = Path(file.filename).stem.replace("_", " ").replace("-", " ")

    safe_title = sanitize_filename(paper_title)
    year = datetime.date.today().year
    pdf_name = f"{year}_{safe_title[:50].rstrip('_')}.pdf"

    target_dir = _safe_path(BASE_DIR, "01_Literature", topic_category, "01_论文原文_PDF")
    target_dir.mkdir(parents=True, exist_ok=True)
    save_to = _safe_path(target_dir, pdf_name)
    save_to.write_bytes(pdf_bytes)
    pdf_saved_path = str(save_to.relative_to(BASE_DIR)).replace("\\", "/")

    sections_out = []
    for s in sections:
        sections_out.append({
            "section": s["section"],
            "title": s["title"],
            "number": s.get("number", ""),
            "text": s["text"],
            "page_start": s["page_start"],
            "page_end": s["page_end"],
            "char_count": len(s["text"]),
        })

    return {
        "status": "success",
        "title": paper_title,
        "pdf_path": pdf_saved_path,
        "file_name": pdf_name,
        "total_pages": len(pages),
        "char_count": sum(len(p["text"]) for p in pages),
        "sections": sections_out,
        "topic_category": topic_category,
    }


@app.get("/api/paper/bibtex")
async def api_export_bibtex(
    arxiv: Optional[str] = Query(default=None, max_length=50),
    doi: Optional[str] = Query(default=None, max_length=200),
    bib_key: Optional[str] = Query(default=None, max_length=80),
):
    """Generate a BibTeX entry from arXiv ID or DOI."""
    if not arxiv and not doi:
        raise HTTPException(status_code=400, detail="必须提供 arxiv 或 doi 之一")
    try:
        if arxiv:
            meta = smart_fetch_paper(arxiv)
        else:
            meta = fetch_crossref_doi_metadata(doi)
        bib = generate_bibtex(meta, bib_key=bib_key)
        return {"status": "success", "bibtex": bib}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"参数无效: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"生成 BibTeX 失败: {str(e)}")

@app.post("/api/reproduction/create")
async def api_create_reproduction(req: CreateReproductionRequest):
    try:
        res = create_reproduction_workspace(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建复现工程失败: {str(e)}")

@app.post("/api/troubleshoot/add")
async def api_add_troubleshoot(req: TroubleshootRequest):
    try:
        res = record_troubleshoot(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"记录排错日志失败: {str(e)}")

@app.post("/api/weekly/generate")
async def api_generate_weekly():
    try:
        res = generate_weekly_report()
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成周报失败: {str(e)}")

# ---------------------------------------------------------------------------
# AI / RAG endpoints (require LLM_API_KEY or AGNES_API_KEY env var)
# ---------------------------------------------------------------------------

@app.get("/api/llm/status")
async def api_llm_status():
    """Report full LLM dual-model configuration & failover status."""
    st = llm.get_llm_status()
    # Backward compatible fields + rich failover info
    return {
        "configured": st["configured"],
        "model": st["active_model"],
        "base_url": st["primary"]["base_url"] if not st["is_degraded"] else st["fallback"]["base_url"],
        "active_model": st["active_model"],
        "active_provider": st["active_provider"],
        "is_degraded": st["is_degraded"],
        "degraded_reason": st["degraded_reason"],
        "degraded_at": st["degraded_at"],
        "primary": st["primary"],
        "fallback": st["fallback"],
        "next_probe_in_seconds": st["next_probe_in_seconds"],
        "rate_limit": st["rate_limit"],
    }

@app.post("/api/llm/probe")
async def api_llm_probe():
    """Manually probe MiniMax to test if quota has reset and trigger auto-upgrade."""
    res = llm.probe_primary_model()
    return res


@app.post("/api/paper/summarize")
async def api_summarize_paper(req: SummarizeRequest):
    """
    Generate a Chinese TL;DR + 3-5 key points + 1 research takeaway
    from paper metadata via LLM.

    If LLM is not configured, returns 200 with a fallback skeleton + an
    inline error message in the result (so the UI can still render gracefully).
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="LLM 未配置。请在 .env 设置 LLM_API_KEY 或 AGNES_API_KEY 后重启服务。",
        )
    try:
        result = llm.summarize_paper(req.model_dump())
        return {"status": "success", "summary": result}
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成摘要失败: {str(e)}")

@app.post("/api/ask")
async def api_ask_question(req: AskRequest):
    """
    RAG-style Q&A over the local vault.

    Pipeline:
      1. search_vault() ranks markdown files by keyword score
      2. Top-k chunks + their snippets are injected into the LLM prompt
      3. LLM answers with [[filename]] citations

    Resilience: even if LLM call fails, the matched chunks are returned so
    the user can still browse what their vault contains on the topic.
    """
    chunks = search_vault(req.question, top_k=req.top_k or 5)
    if not chunks:
        return {
            "status": "success",
            "answer": "vault 中未找到与该问题相关的内容。请尝试更换关键词, 或先录入更多文献。",
            "chunks": [],
            "llm_used": False,
            "llm_error": None,
        }
    if not llm.is_configured():
        return {
            "status": "success",
            "answer": (
                f"🔍 找到 {len(chunks)} 条相关文献片段 (LLM 未配置, "
                "未生成综合答案)。配置 LLM_API_KEY 后可启用 AI 综合回答。"
            ),
            "chunks": chunks,
            "llm_used": False,
            "llm_error": None,
        }
    try:
        answer = llm.answer_with_context(req.question, chunks)
        return {
            "status": "success",
            "answer": answer,
            "chunks": chunks,
            "llm_used": True,
            "llm_error": None,
        }
    except llm.LLMError as e:
        # Resilience: return chunks + a clear note about LLM failure.
        # Don't raise 502 — the user still benefits from the RAG hits.
        return {
            "status": "partial",
            "answer": (
                f"⚠️ LLM 调用失败, 但 RAG 检索到 {len(chunks)} 条相关文献片段。"
                f"请检查 .env 中 LLM_BASE_URL / LLM_MODEL_NAME 配置。错误: {str(e)[:200]}"
            ),
            "chunks": chunks,
            "llm_used": False,
            "llm_error": str(e),
        }
    except Exception as e:
        return {
            "status": "partial",
            "answer": f"⚠️ 问答过程出错: {str(e)[:200]}。以下是 vault 检索结果。",
            "chunks": chunks,
            "llm_used": False,
            "llm_error": str(e),
        }


# ---------------------------------------------------------------------------
# Plot digitization + reproduction alignment
# ---------------------------------------------------------------------------

@app.post("/api/plot/extract")
async def api_plot_extract(
    image: UploadFile = File(..., description="PNG/JPG/GIF/BMP chart screenshot"),
    sat_thresh: int = Query(default=60, ge=10, le=200),
    val_thresh: int = Query(default=230, ge=50, le=255),
):
    """
    Extract the dominant colored curve from a chart screenshot.
    Accepts multipart/form-data with field name "image" (PNG/JPG/GIF/BMP).
    Returns pixel-space (x, y) points plus metadata.
    Frontend then calibrates to real coords via /api/plot/calibrate.
    """
    image_bytes = await image.read()
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="图片不能超过 10MB")
    try:
        result = extract_curve_from_image(
            image_bytes, sat_thresh=sat_thresh, val_thresh=val_thresh
        )
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图片解析失败: {str(e)}")

@app.post("/api/plot/calibrate")
async def api_plot_calibrate(req: CalibrateRequest):
    """
    Convert pixel-space points to real data coords using 2 reference points
    (the user clicks on the chart's axes and types in the value at each point).
    """
    try:
        calibrated = calibrate_points(req.pixel_points, req.ref_a, req.ref_b)
        return {"status": "success", "points": calibrated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"标定失败: {str(e)}")

@app.post("/api/plot/compare")
async def api_plot_compare(req: CompareRequest):
    """
    Compare paper-reported data against reproduction data.

    Input can be either:
      - explicit paper_points / repro_points (list of [x, y])
      - paper_csv / repro_csv strings (parsed on the server)
    """
    paper = req.paper_points
    repro = req.repro_points
    if req.paper_csv:
        paper = paper + _parse_csv_points(req.paper_csv)
    if req.repro_csv:
        repro = repro + _parse_csv_points(req.repro_csv)
    if len(paper) < 2:
        raise HTTPException(status_code=400, detail="paper 数据点不足 (需要 >= 2 个点)")
    if len(repro) < 2:
        raise HTTPException(status_code=400, detail="repro 数据点不足 (需要 >= 2 个点)")

    tolerance = {
        "pass_pct": req.pass_pct or 5.0,
        "warn_pct": req.warn_pct or 20.0,
    }
    try:
        result = compute_metrics(paper, repro, tolerance=tolerance)
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"对比计算失败: {str(e)}")


# ---------------------------------------------------------------------------
# Paper full-text reading + AI explain (V1.4)
# ---------------------------------------------------------------------------

@app.post("/api/paper/fetch_full")
async def api_fetch_full_text(req: FetchFullRequest):
    """
    Fetch the full text of a paper given an arXiv ID, DOI, or URL.
    Extracts PDF, splits into canonical sections (Abstract / Introduction /
    Methods / Results / Discussion / Conclusion / References).

    Response includes:
      - title / authors / year / venue (metadata)
      - source ('arXiv' / 'OpenAccess' / 'URL')
      - sections: list of {section, title, text, page_start, page_end}
      - pdf_size / total_pages / char_count

    Caveats: IEEE / Elsevier / Wiley DOIs are usually paywalled —
    we'll return 400 with a clear message asking the user to either
    paste the PDF URL, use an arXiv preprint, or paste the text directly.
    """
    try:
        result = fetch_full_text(req.identifier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"全文获取失败: {str(e)}")

    # Side-effect: also save the PDF to vault so the dual-column reader can find it.
    # Best-effort: failures here don't fail the overall request.
    pdf_saved_path = None
    if result.get("source") == "arXiv":
        try:
            from .generator import sanitize_filename, _safe_path
            safe_title = sanitize_filename(result["title"])
            topic = "01_Device_TCAD_器件仿真"  # default category for now
            target_dir = _safe_path(BASE_DIR, "01_Literature", topic, "01_论文原文_PDF")
            target_dir.mkdir(parents=True, exist_ok=True)
            pdf_name = f"{result['year']}_{safe_title[:30]}.pdf"
            save_to = _safe_path(target_dir, pdf_name)
            # Re-download to vault only if not already there
            if not save_to.exists():
                from .paper_fetcher import download_arxiv_pdf
                download_arxiv_pdf(result["identifier"], save_to)
            pdf_saved_path = str(save_to.relative_to(BASE_DIR)).replace("\\", "/")
        except Exception:
            # Non-fatal — find_pdf will simply not return a match
            pdf_saved_path = None

    # Strip pdf_bytes from response (too large to JSON-ship)
    sections_out = []
    for s in result["sections"]:
        sections_out.append({
            "section": s["section"],
            "title": s["title"],
            "number": s.get("number", ""),
            "text": s["text"],
            "page_start": s["page_start"],
            "page_end": s["page_end"],
            "char_count": len(s["text"]),
        })
    return {
        "status": "success",
        "identifier": result["identifier"],
        "source": result["source"],
        "title": result["title"],
        "authors": result["authors"],
        "year": result["year"],
        "venue": result["venue"],
        "pdf_size": result["pdf_size"],
        "total_pages": result["total_pages"],
        "char_count": result["char_count"],
        "pdf_path": pdf_saved_path,
        "sections": sections_out,
    }


TRANSLATION_SYSTEM_PROMPT = """你是学术论文翻译助手, 帮助硕博生阅读英文学术论文。

规则:
1. 严格忠于原文, 不要添加原文没有的内容
2. 翻译要学术化、平实, 避免文学化或口语化
3. 专业术语保留英文 (如 "GAA-FET", "hydrodynamic model", "DIBL")
4. 数学符号保持 LaTeX 形式 (如 "$I_{on}$", "$\\tau$", "$\\lambda$")
5. 中文输出, Markdown 格式
6. 如果原文含图表引用 (如 "Fig. 3", "Table II"), 保留原样
"""


@app.post("/api/paper/translate_section")
async def api_translate_section(req: TranslateRequest):
    """
    Translate a single section (English → Chinese) via LLM.

    Features (v1.2):
    - Auto-chunks sections >8K chars at paragraph boundaries so the LLM
      sees complete paragraphs (no mid-sentence cutoffs).
    - Concatenates chunk translations in order with blank-line separators.
    - Caches results in-memory by (section_key, text_hash, model) so
      re-clicking translate on an unchanged section returns instantly.

    Falls back gracefully if LLM is not configured: returns 503.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="翻译需要 LLM 配置。请在 .env 中设置 LLM_API_KEY 或 AGNES_API_KEY + LLM_BASE_URL。",
        )

    from .translation import chunk_section_text, get_cache
    cache = get_cache()
    curr_st = llm.get_llm_status()
    model = curr_st.get("active_model") or "default"

    # Cache lookup: same text + same model = same translation
    cached = cache.get(req.section_key or "", req.section_text, model)
    if cached:
        cached["from_cache"] = True
        return cached

    chunks = chunk_section_text(req.section_text)
    was_segmented = len(chunks) > 1
    translated_parts: list[str] = []

    try:
        for idx, chunk in enumerate(chunks):
            if was_segmented:
                user_msg = (
                    f"翻译以下学术论文章节 (标题: {req.section_title}, "
                    f"第 {idx + 1}/{len(chunks)} 段):\n\n{chunk}"
                )
            else:
                user_msg = (
                    f"翻译以下学术论文章节 (标题: {req.section_title}):\n\n{chunk}"
                )
            translated_parts.append(
                llm.chat(
                    messages=[
                        {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    # Shorter cap: smaller chunks need shorter responses, and
                    # faster responses are less likely to be killed mid-stream
                    # by upstream proxies / load balancers.
                    max_tokens=1500,
                    temperature=0.2,
                    # Generous timeout — MiniMax-M2.7 thinking mode can take 60s
                    # per chunk; multi-chunk translations need headroom.
                    timeout=120,
                ).strip()
            )
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM 翻译失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"翻译失败: {str(e)}")

    translation = "\n\n".join(translated_parts).strip()
    curr_st = llm.get_llm_status()
    actual_model = curr_st.get("active_model") or model
    result = {
        "status": "success",
        "translation": translation,
        "was_segmented": was_segmented,
        "chunk_count": len(chunks),
        "from_cache": False,
        "model": actual_model,
        "is_degraded": curr_st["is_degraded"],
    }

    # Store in cache under actual producing model so re-translate is instant
    cache.put(req.section_key or "", req.section_text, actual_model, result)
    return result


EXPLAIN_SYSTEM_PROMPT = """你是学术论文 AI 解读助手, 帮助硕博生深度理解英文学术论文。

回答用户问题时:
1. 基于提供的论文章节内容回答, 不要编造
2. 引用具体章节 (用 [Abstract], [Introduction], [Methods], [Results] 等)
3. 学术化但简洁, 用中文
4. 如果用户问的"创新点"或"方法", 用 bullet 列出
5. 如果章节内容不足以回答, 明确说明
"""


CROSS_QA_SYSTEM_PROMPT = """你是学术论文跨章节综合分析助手, 帮助硕博生综合多个章节信息回答问题。

规则:
1. 综合多个章节回答问题, 不要只复述单一章节
2. 明确标注每个观点来自哪个章节 (用 [Introduction], [Methods], [Results] 等方括号标签)
3. 区分: 论文做了什么 (方法/结果) vs 论文声称了什么 (引言/讨论)
4. 学术化、简洁, 用中文
5. 如果问题涉及对比 (新旧方法对比/预期 vs 实际), 用表格或并列 bullet
6. 如果提供章节不足以回答, 明确指出缺失哪类信息
"""


class CrossQARequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    # Accept either specific section indices OR all sections from the paper
    sections: list = Field(..., min_length=2, max_length=20,
                           description="至少 2 个章节 [{section, title, text}, ...]")
    selected_indices: Optional[list[int]] = Field(default=None,
                                                   description="用户勾选的章节索引列表; 为空则用全部")
    paper_title: Optional[str] = Field(default="", max_length=300)
    per_section_budget: Optional[int] = Field(default=6000, ge=1000, le=12000)


@app.post("/api/paper/explain")
async def api_explain(req: ExplainRequest):
    """
    Answer a user question about a paper using its sections as context.

    Pre-built question examples (前端可一键触发):
      - "这篇论文的核心创新点是什么?"
      - "方法是如何实现的? 关键步骤/公式?"
      - "实验结果如何? 关键指标?"
      - "这篇对我做 GAA-FET/CFET 研究有何启发?"
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="AI 解读需要 LLM 配置。请在 .env 中设置 LLM_API_KEY。",
        )

    # Build context from sections — prefer Abstract + Introduction for context window
    sections = req.sections[:20]
    context_parts = []
    total_chars = 0
    for s in sections:
        title = s.get("title", s.get("section", "?"))
        text = (s.get("text") or "")[:6000]  # cap per-section
        chunk = f"[{s.get('section', '?')}] {title}\n{text}"
        if total_chars + len(chunk) > 24000:  # ~6k tokens
            break
        context_parts.append(chunk)
        total_chars += len(chunk)

    context = "\n\n---\n\n".join(context_parts)
    paper_label = req.paper_title or "未命名论文"

    user_msg = (
        f"论文标题: {paper_label}\n\n"
        f"--- 论文内容 (节选) ---\n{context}\n--- 论文内容结束 ---\n\n"
        f"用户问题: {req.question}"
    )

    try:
        answer = llm.chat(
            messages=[
                {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=1200,
            temperature=0.3,
        )
        return {"status": "success", "answer": answer.strip()}
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 解读失败: {str(e)}")


@app.post("/api/paper/cross_qa")
async def api_cross_qa(req: CrossQARequest):
    """
    Cross-chapter Q&A: synthesize information from 2+ sections to answer a
    question that requires combining content across the paper.

    Use cases:
      - "本文方法在 [Introduction] 提到的不足上如何改进?"
      - "[Results] 中观察到的现象与 [Discussion] 的解释是否一致?"
      - "对比本文 [Methods] 与之前 [Introduction] 提到的 SOTA 方法"

    The endpoint respects `selected_indices` so users can pick which sections
    feed into the context window — useful when papers have many sections.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="跨章节问答需要 LLM 配置。请在 .env 中设置 LLM_API_KEY。",
        )
    if len(req.sections) < 2:
        raise HTTPException(
            status_code=400,
            detail="跨章节问答至少需要 2 个章节。",
        )

    from .translation import build_cross_section_context

    # If user selected specific indices, filter sections; else use all
    if req.selected_indices:
        valid_indices = [i for i in req.selected_indices if 0 <= i < len(req.sections)]
        sections_to_use = [req.sections[i] for i in valid_indices]
        if len(sections_to_use) < 2:
            raise HTTPException(
                status_code=400,
                detail="所选章节不足 2 个, 请至少勾选 2 个章节进行跨章节问答。",
            )
    else:
        sections_to_use = req.sections

    context = build_cross_section_context(
        sections_to_use,
        req.question,
        per_section_budget=req.per_section_budget,
    )
    paper_label = req.paper_title or "未命名论文"
    user_msg = (
        f"论文标题: {paper_label}\n\n"
        f"{context}\n\n"
        f"请综合上述多章节内容回答用户问题, 并用 [SECTION_KEY] 标注信息来源。"
    )

    try:
        answer = llm.chat(
            messages=[
                {"role": "system", "content": CROSS_QA_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=1500,
            temperature=0.3,
        )
        return {
            "status": "success",
            "answer": answer.strip(),
            "sections_used": [
                {
                    "section": s.get("section", "?"),
                    "title": s.get("title", "?"),
                }
                for s in sections_to_use
            ],
            "section_count": len(sections_to_use),
            "context_chars": len(context),
        }
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"跨章节问答失败: {str(e)}")


EXPLAIN_SELECTION_SYSTEM_PROMPT = r"""你是一个微电子与集成电路领域资深学者与论文精读导师。
用户在阅读微电子/半导体器件/TCAD/EDA学术论文时，划选了一段难以理解的文字、公式或技术论述向你提问。

请根据用户划选的内容与论文上下文，提供详实、严谨、易于理解的中文解答：
1. 【直击疑问】：首先明确针对用户提出的问题进行回答，解释重点突出。
2. 【物理背景与器件机制】：如果涉及半导体物理（如载流子输运、能带结构、量子限制、自热效应、短沟道效应、界面缺陷等）或TCAD仿真（SDE几何构建、网格剖分、SDevice物理模型等），请结合专业背景深入透彻剖析。
3. 【术语全称与公式拆解】：对涉及的专业缩写（如 DTCO, GAA-FET, CFET, SDE, SDevice, DIBL, SS）给出中英文全称与物理意义；对公式中的关键变量进行清晰解释，公式使用标准 LaTeX 排版（如 $I_{on}/I_{off}$, $\mu_{eff}$）。
4. 【工程与研究启发】：结合当前先进节点（如 Sub-3nm/2nm）或前沿论文研究范式，指出该段内容对研究者有何启发或实操注意事项。
5. 结构清晰，采用分段 Markdown 格式输出。
"""


@app.post("/api/paper/explain_selection")
async def api_explain_selection(req: ExplainSelectionRequest):
    """
    Explain a specific highlighted/selected snippet from either the PDF text or
    the Chinese translation, using surrounding section/page context and LLM.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="AI 划词答疑需要 LLM 配置。请在 .env 中设置 LLM_API_KEY 或 AGNES_API_KEY。",
        )

    paper_label = req.paper_title or "未知论文"
    source_desc = "PDF 英文原文" if req.source_type == "pdf" else ("中文译文" if req.source_type == "translation" else "论文选段")

    user_msg_parts = [
        f"论文标题: 《{paper_label}》",
        f"【用户划选片段 ({source_desc})】:\n```text\n{req.selected_text.strip()}\n```",
    ]
    if req.surrounding_context:
        ctx_snippet = req.surrounding_context.strip()[:4000]
        user_msg_parts.append(f"【该片段所在的章节/页面上下文背景】:\n{ctx_snippet}")

    user_msg_parts.append(f"【用户的问题】:\n{req.question.strip()}")
    user_msg = "\n\n".join(user_msg_parts)

    try:
        answer = llm.chat(
            messages=[
                {"role": "system", "content": EXPLAIN_SELECTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=1500,
            temperature=0.3,
            timeout=120,
        )
        curr_st = llm.get_llm_status()
        return {
            "status": "success",
            "answer": answer.strip(),
            "selected_text": req.selected_text,
            "question": req.question,
            "model": curr_st["active_model"],
            "is_degraded": curr_st["is_degraded"],
        }
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM 答疑调用失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"答疑服务出错: {str(e)}")


@app.get("/api/translation/cache_stats")
async def api_translation_cache_stats():
    """Return translation cache stats (size/hits/misses) — useful for debug."""
    from .translation import get_cache
    return get_cache().stats()


@app.post("/api/paper/translate_page")
async def api_translate_page(req: TranslatePageRequest):
    """
    Translate a single PDF page (used by the dual-column reader).

    The page text comes from pdfplumber.extract_text() which gives plain text
    per page. Cached per (paper_id, page) so re-opening is instant.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="按页翻译需要 LLM 配置。请在 .env 中设置 LLM_API_KEY。",
        )

    from .translation import chunk_section_text, get_cache
    cache = get_cache()
    cfg = llm._config()
    model = cfg["model"]

    # Cache key includes paper_id + page so each page is cached independently.
    # We DO NOT include text hash (text might come from different extractions
    # of the same page); instead we accept text-as-given. A page extract is
    # deterministic for a given PDF, so this is fine.
    cache_section_key = f"page:{req.paper_id}:{req.page}"

    cached = cache.get(cache_section_key, req.text, model)
    if cached:
        cached["from_cache"] = True
        return cached

    # Translate the whole page in one shot (page text is already < 15K chars)
    # If somehow larger, chunk and translate parts.
    chunks = chunk_section_text(req.text)
    translated_parts = []
    try:
        for idx, chunk in enumerate(chunks):
            if len(chunks) > 1:
                user_msg = (
                    f"翻译以下学术论文第 {req.page} 页 (第 {idx+1}/{len(chunks)} 段):\n\n{chunk}"
                )
            else:
                user_msg = f"翻译以下学术论文第 {req.page} 页:\n\n{chunk}"
            translated_parts.append(
                llm.chat(
                    messages=[
                        {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=1500,
                    temperature=0.2,
                    timeout=90,
                ).strip()
            )
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=f"按页翻译失败: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"按页翻译失败: {e}")

    translation = "\n\n".join(translated_parts).strip()
    curr_st = llm.get_llm_status()
    result = {
        "status": "success",
        "translation": translation,
        "paper_id": req.paper_id,
        "page": req.page,
        "chunk_count": len(chunks),
        "from_cache": False,
        "model": curr_st["active_model"],
        "is_degraded": curr_st["is_degraded"],
    }
    cache.put(cache_section_key, req.text, model, result)
    return result


@app.post("/api/paper/split_text")
async def api_split_text(req: SplitTextRequest):
    """
    Split user-pasted raw text into sections (no PDF download needed).
    Useful for paywalled IEEE papers where the user has copied text from
    their institution's PDF reader.
    """
    pages = [{"page": 1, "text": req.raw_text}]
    sections = split_into_sections(pages)
    if not sections:
        # No recognizable sections — return whole text as full_text
        sections = [{
            "section": "full_text",
            "title": "Full Text",
            "text": req.raw_text.strip(),
            "page_start": 1,
            "page_end": 1,
            "char_count": len(req.raw_text),
        }]
    sections_out = []
    for s in sections:
        sections_out.append({
            "section": s["section"],
            "title": s["title"],
            "number": s.get("number", ""),
            "text": s["text"],
            "page_start": s["page_start"],
            "page_end": s["page_end"],
            "char_count": len(s["text"]),
        })
    return {
        "status": "success",
        "sections": sections_out,
        "char_count": len(req.raw_text),
    }


@app.post("/api/paper/paste_analyze")
async def api_paste_analyze(req: PasteTextRequest):
    """
    One-shot workflow for paywalled papers:
      1. User pastes raw text (copied from PDF reader in browser)
      2. We split into sections
      3. If `question` is provided, ask LLM immediately
      4. Return sections + (optional) AI answer

    Saves the user from running split + ask in two steps.
    """
    pages = [{"page": 1, "text": req.raw_text}]
    sections = split_into_sections(pages)
    if not sections:
        sections = [{
            "section": "full_text",
            "title": "Full Text",
            "text": req.raw_text.strip(),
            "page_start": 1,
            "page_end": 1,
        }]

    sections_out = [{
        "section": s["section"],
        "title": s["title"],
        "number": s.get("number", ""),
        "text": s["text"],
        "page_start": s["page_start"],
        "page_end": s["page_end"],
        "char_count": len(s["text"]),
    } for s in sections]

    response = {
        "status": "success",
        "title": req.title,
        "sections": sections_out,
        "char_count": len(req.raw_text),
        "ai_answer": None,
        "ai_error": None,
    }

    if req.question:
        if not llm.is_configured():
            response["ai_error"] = "LLM 未配置, 无法回答"
            return response
        try:
            # Build ExplainRequest context
            answer = llm.chat(
                messages=[
                    {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"论文标题: {req.title}\n\n"
                        f"--- 用户粘贴的全文 ---\n{req.raw_text[:20000]}\n--- 全文结束 ---\n\n"
                        f"用户问题: {req.question}"
                    )},
                ],
                max_tokens=1200,
                temperature=0.3,
            )
            response["ai_answer"] = answer.strip()
        except llm.LLMError as e:
            response["ai_error"] = f"LLM 调用失败: {str(e)}"
    return response