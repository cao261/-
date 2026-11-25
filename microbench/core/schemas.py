# -*- coding: utf-8 -*-
"""后端共享 Pydantic 模型（供路由与 app.py 复用）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class VaultPaperMetaRequest(BaseModel):
    """更新笔记 Markdown frontmatter 中的精读状态或标签。"""

    relative_path: str = Field(..., min_length=3, max_length=500)
    status: Optional[str] = Field(default=None, max_length=40)
    tags: Optional[list[str]] = Field(default=None, max_length=24)


class BatchTranslateRequest(BaseModel):
    """后台批量翻译多页 PDF 的任务请求。"""

    paper_id: str = Field(..., min_length=3, max_length=300)
    relative_path: str = Field(..., min_length=3, max_length=500)
    pages: list[int] = Field(..., min_length=1, max_length=200)
    mode: Literal["page", "blocks"] = Field(default="page")
    model: str = Field(default="", max_length=64)
    force: bool = Field(default=False)
