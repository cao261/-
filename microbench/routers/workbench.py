# -*- coding: utf-8 -*-
"""工作台模块化路由：健康检查、SQLite 全库检索、文献元数据、批量后台翻译任务。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..core.schemas import BatchTranslateRequest, VaultPaperMetaRequest
from ..core.security import assert_vault_pdf_scope
from ..generator import _safe_path
from .. import llm
from ..services import vault_index

router = APIRouter(tags=["workbench"])


def _app_mod():
    """动态解析 `microbench.app`，确保测试替身（sandbox / mock）始终生效。"""
    mod = sys.modules.get("microbench.app")
    if mod is None:
        from .. import app as mod  # pragma: no cover
    return mod


def _safe_err(exc: BaseException) -> str:
    return llm.mask_paths(llm.redact_secrets(str(exc)))[:200]


@router.get("/api/health")
def api_health():
    """轻量存活与就绪探针，不触发重型磁盘扫描或模型请求。"""
    app_mod = _app_mod()
    return {
        "status": "ok",
        "service": "microbench",
        "llm_configured": llm.is_configured(),
        "vault_base": Path(app_mod.BASE_DIR).name,
        "sync_configured": bool(app_mod._sync_root()),
    }


@router.get("/api/vault/search")
def api_vault_search(
    q: str = Query(default="", max_length=200),
    kind: str = Query(default="all", pattern="^(all|pdf|note)$"),
    category: str = Query(default="", max_length=100),
    status: str = Query(default="", max_length=40),
    limit: int = Query(default=100, ge=1, le=300),
    refresh: bool = Query(default=False),
):
    """基于 SQLite WAL 索引的跨文献与笔记快速检索。"""
    app_mod = _app_mod()
    return vault_index.search_index(
        app_mod.BASE_DIR,
        q=q,
        kind=kind,
        category=category,
        status=status,
        limit=limit,
        refresh=refresh,
    )


@router.post("/api/vault/paper_meta")
def api_vault_paper_meta(req: VaultPaperMetaRequest):
    """编辑笔记 YAML frontmatter 中的精读状态或标签，并同步刷新索引。"""
    app_mod = _app_mod()
    try:
        return vault_index.update_note_meta(
            app_mod.BASE_DIR,
            req.relative_path,
            status=req.status,
            tags=req.tags,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=_safe_err(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=_safe_err(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新元数据失败: {_safe_err(e)}")


@router.post("/api/paper/translate_batch")
async def api_paper_translate_batch(req: BatchTranslateRequest):
    """启动后台多页批量翻译任务，支持段落对照模式与整页模式。"""
    if not llm.is_configured():
        raise HTTPException(status_code=503, detail="批量翻译需要配置 LLM_API_KEY。")

    app_mod = _app_mod()
    base_dir = Path(app_mod.BASE_DIR)
    try:
        pdf_path = _safe_path(base_dir, req.relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
    assert_vault_pdf_scope(base_dir, pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail=f"PDF 不存在: {req.relative_path}")

    async def _runner(page_no: int) -> None:
        if req.mode == "blocks":
            body = app_mod.TranslateBlocksRequest(
                paper_id=req.paper_id,
                page=page_no,
                relative_path=req.relative_path,
                force=req.force,
                model=req.model,
            )
            await app_mod.api_translate_blocks(body)
        else:
            extracted, _ = await asyncio.to_thread(
                app_mod._extract_page_cached, pdf_path, page_no
            )
            seed_text = (
                extracted
                if extracted and len(extracted.strip()) >= 10
                else "Batch page translation placeholder"
            )
            body = app_mod.TranslatePageRequest(
                paper_id=req.paper_id,
                page=page_no,
                text=seed_text[:15000],
                force=req.force,
                relative_path=req.relative_path,
                model=req.model,
            )
            await app_mod.api_translate_page(body)

    try:
        job = vault_index.BATCH_MANAGER.start_job(
            base_dir,
            req.paper_id,
            req.relative_path,
            req.pages,
            req.mode,
            _runner,
        )
        return {"status": "success", "job": job}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=_safe_err(e))


@router.get("/api/paper/translate_batch/{job_id}")
def api_paper_translate_batch_status(job_id: str):
    """查询批量翻译任务进度与页级状态。"""
    app_mod = _app_mod()
    job = vault_index.BATCH_MANAGER.get_job(app_mod.BASE_DIR, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"批量任务不存在: {job_id}")
    return {"status": "success", "job": job}


@router.delete("/api/paper/translate_batch/{job_id}")
def api_paper_translate_batch_cancel(job_id: str):
    """取消正在进行的批量翻译任务。"""
    app_mod = _app_mod()
    job = vault_index.BATCH_MANAGER.cancel_job(app_mod.BASE_DIR, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"批量任务不存在: {job_id}")
    return {"status": "success", "job": job}
