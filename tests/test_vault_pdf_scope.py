# -*- coding: utf-8 -*-
"""vault 里的 PDF 清单：只收「论文原文」，不收生成物（2026-11-25 审计 P0）。

背景：`_vault_pdfs()` 原来用 `BASE_DIR.rglob("*.pdf")` 扫**整个仓库**
（BASE_DIR 既是仓库根、又是 vault 根），于是两样东西被算成了「论文原文」：

  ① **「中译替换版」生成物**（`<主题>/05_译文替换版_PDF/*_中译替换版.pdf`）
     → 「从仓库论文库打开」的列表里出现**同名同源的两份 PDF**；
     → files/ 同步通道还把这些生成物哈希同步进坚果云（白占空间，
       两台设备各传一份）。
  ② 仓库里的其它 PDF（文档、测试夹具……）

修法：收窄到 vault 的**内容目录**（generator.VAULT_CONTENT_DIRS），
并且排除「译文替换版」输出目录。

⚠ 红线：**不改变任何既有文件的路径与文件名**（paper_id 哈希口径不变）——
   这里只是不再把生成物/仓库文件算进「论文原文」清单。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from microbench import app
from microbench.generator import VAULT_CONTENT_DIRS


def _make_tree(root: Path) -> None:
    """造一个像样的 vault：原文 PDF + 生成物 + 仓库里的杂 PDF。"""
    lit = root / "01_Literature" / "铁电传感器"
    orig = lit / "01_论文原文_PDF"
    replaced = lit / "05_译文替换版_PDF"
    know = root / "03_Knowledge"
    for d in (orig, replaced, know):
        d.mkdir(parents=True, exist_ok=True)
    (orig / "JAP综述.pdf").write_bytes(b"%PDF-1.4 original")
    (replaced / "JAP综述_中译替换版.pdf").write_bytes(b"%PDF-1.4 replaced")
    (know / "笔记.pdf").write_bytes(b"%PDF-1.4 note")
    # 仓库里的杂 PDF（代码、文档、测试夹具…）
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "paper.pdf").write_bytes(b"%PDF-1.4 docs")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "fixture.pdf").write_bytes(b"%PDF-1.4 fixture")
    (root / "根目录杂.pdf").write_bytes(b"%PDF-1.4 root")


@pytest.fixture()
def vault(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    monkeypatch.setattr(app, "BASE_DIR", tmp_path)
    return tmp_path


def _rels() -> list[str]:
    return [rel for rel, _pdf in app._vault_pdfs()]


def test_vault_pdfs_keeps_original_pdfs(vault):
    rels = _rels()
    assert "01_Literature/铁电传感器/01_论文原文_PDF/JAP综述.pdf" in rels
    assert "03_Knowledge/笔记.pdf" in rels


def test_vault_pdfs_excludes_generated_replaced_pdfs(vault):
    """生成物（中译替换版）**不是**论文原文 —— 绝不能进清单。"""
    rels = _rels()
    bad = [r for r in rels if "05_译文替换版_PDF" in r]
    assert not bad, f"中译替换版生成物混进论文清单了: {bad}"


def test_vault_pdfs_excludes_repo_files_outside_vault(vault):
    """仓库里的 PDF（docs/ tests/ 根目录）也不该进 vault 论文清单。"""
    rels = _rels()
    for rel in rels:
        first = rel.split("/", 1)[0]
        assert first in VAULT_CONTENT_DIRS, f"清单里混进了仓库文件: {rel}"


def test_vault_pdfs_returns_existing_files_only(vault):
    """清单里的每个文件都必须真的存在（避免登记了已被删掉的路径）。"""
    for rel, pdf in app._vault_pdfs():
        assert pdf.is_file(), f"清单里的文件不存在: {rel}"
        assert pdf.exists()
