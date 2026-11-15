# -*- coding: utf-8 -*-
"""临时诊断：渲染 PDF 某一页整页 PNG（核对版面用）。"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PDF = (REPO / "01_Literature/04_Survey_综述/01_论文原文_PDF/"
       "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")
page_no = int(sys.argv[1]) if len(sys.argv) > 1 else 2
out = REPO / "logs" / "blockdiag" / ("page-full-p%d.png" % page_no)
out.parent.mkdir(parents=True, exist_ok=True)

import fitz  # noqa: E402

doc = fitz.open(str(PDF))
page = doc[page_no - 1]
pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6))
pix.save(str(out))
print("%s  %dx%d  page=%dx%d pt" % (out, pix.width, pix.height, page.rect.width, page.rect.height))
