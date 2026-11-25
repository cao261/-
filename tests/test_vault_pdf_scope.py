# -*- coding: utf-8 -*-
"""vault 里的 PDF 清单：只收「论文原文」，不收生成物（2026-11-25 审计 P0）。

背景：「中译替换版」是**生成物**（`<主题>/05_译文替换版_PDF/*_中译替换版.pdf`，
版面是「原文页 + 中文替换页」交替），不是论文原文。

⚠⚠ **这个文件记的是一次「护栏没护住用户路径」的教训**（2026-11-25）：

  第一版只守了 `_vault_pdfs()`（同步通道用的那个清单）→
  护栏全绿，但用户点的「📂 从仓库论文库打开…」下拉里**照样**有 2 篇替换版。

  根因不是判据写错，而是**判据写了两遍**：
  `_vault_pdfs()` 排除了生成物，而前端真正用的
  `/api/paper/list_vault_pdfs`（自己 rglob 一遍）**根本没调那个排除**。
  → 护栏守的是一个**用户不走的路径**。

  现在：
    ① 判据收敛成单一函数 `app._is_replaced_output_pdf()`；
    ② 三个扫描点（同步清单 / 论文库列表 / 按标题找本地 PDF）全部走它；
    ③ 本文件**真打端点**断言用户看到的那两个列表，
       并带**变异自检**（把判据改成永远失效 → 生成物必须立刻出现）。

⚠ 红线：**不改变任何既有文件的路径与文件名**（paper_id 哈希口径不变）——
   这里只是不再把生成物/仓库文件算进「论文原文」清单。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from microbench import app
from microbench.generator import VAULT_CONTENT_DIRS

# 生成物文件名里带一段**只有它有**的词 —— 用来证明「按标题找本地 PDF」
# 找的也不是生成物（不然两个文件同名，打分打平，谁被选中全看 rglob 顺序）。
GENERATED_ONLY_TOKEN = "ZZZ生成物专属"
GENERATED_REL = ("01_Literature/铁电传感器/05_译文替换版_PDF/"
                 "ZZZ生成物专属_中译替换版.pdf")
ORIGINAL_REL = "01_Literature/铁电传感器/01_论文原文_PDF/JAP综述.pdf"


def _make_tree(root: Path) -> None:
    """造一个像样的 vault：原文 PDF + 生成物 + 仓库里的杂 PDF。"""
    lit = root / "01_Literature" / "铁电传感器"
    orig = lit / "01_论文原文_PDF"
    replaced = lit / "05_译文替换版_PDF"
    know = root / "03_Knowledge"
    for d in (orig, replaced, know):
        d.mkdir(parents=True, exist_ok=True)
    (orig / "JAP综述.pdf").write_bytes(b"%PDF-1.4 original")
    (replaced / "ZZZ生成物专属_中译替换版.pdf").write_bytes(b"%PDF-1.4 replaced")
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


@pytest.fixture()
def vault_client(sandbox):
    """真打端点用的 client（BASE_DIR 已被 sandbox 钉到临时 vault）。"""
    from fastapi.testclient import TestClient

    with TestClient(app.app) as c:
        yield c


def _rels() -> list[str]:
    return [rel for rel, _pdf in app._vault_pdfs()]


# ---------------------------------------------------------------------------
# ① 同步通道用的清单（_vault_pdfs）
# ---------------------------------------------------------------------------
def test_vault_pdfs_keeps_original_pdfs(vault):
    rels = _rels()
    assert ORIGINAL_REL in rels
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


# ---------------------------------------------------------------------------
# ② ⚠ 用户真正走的那条路：「📂 从仓库论文库打开…」
#    （第一版护栏漏了这里 —— 判据写在了用户不走的路径上）
# ---------------------------------------------------------------------------
def test_library_dropdown_excludes_generated_replaced_pdfs(vault_client, sandbox):
    """前端「从仓库论文库打开」的下拉数据源 = /api/paper/list_vault_pdfs。

    生成物混进来 = 同一篇文章出现两份（原文 + 中译替换版），
    用户点错一份打开的完全是另一个版面。
    """
    _make_tree(sandbox)
    res = vault_client.get("/api/paper/list_vault_pdfs")
    assert res.status_code == 200, res.text
    rels = [p["relative_path"] for p in res.json()["pdfs"]]

    assert ORIGINAL_REL in rels, "论文原文应该在论文库里"
    bad = [r for r in rels if "05_译文替换版_PDF" in r]
    assert not bad, f"中译替换版生成物出现在「仓库论文库」下拉里了: {bad}"


def test_find_pdf_never_matches_a_generated_replaced_pdf(vault_client, sandbox):
    """按标题找本地 PDF 时，生成物也不能被匹配上。

    生成物的文件名里带一段只有它有的词 → 一旦判据失效，它就会被匹配出来。
    """
    _make_tree(sandbox)
    res = vault_client.get("/api/paper/find_pdf",
                           params={"title": GENERATED_ONLY_TOKEN})
    assert res.status_code == 200, res.text
    data = res.json()
    assert not data.get("found"), \
        "按标题找本地 PDF 找到的是中译替换版生成物：" + str(data)


# ---------------------------------------------------------------------------
# ③ 变异自检：证明上面两条断言**是承重的**（不是恒真）
# ---------------------------------------------------------------------------
def test_exclusion_is_load_bearing_mutation_check(vault_client, sandbox, monkeypatch):
    """把判据改成「永远不算生成物」→ 生成物必须立刻出现在两个列表里。

    这条断言保证：
      ① 夹具里**真的有一份**生成物文件（否则上面两条是恒真）；
      ② 是**这个判据**把它挡住的（不是「文件不存在」「端点没扫到」）。
    """
    _make_tree(sandbox)

    # 变异：判据彻底失效
    monkeypatch.setattr(app, "_is_replaced_output_pdf", lambda parts: False)

    res = vault_client.get("/api/paper/list_vault_pdfs")
    assert res.status_code == 200, res.text
    rels = [p["relative_path"] for p in res.json()["pdfs"]]
    assert GENERATED_REL in rels, \
        "判据失效后生成物也没出现 —— 说明夹具里根本没有这份文件，" \
        "上面两条断言就是恒真的"

    res2 = vault_client.get("/api/paper/find_pdf",
                            params={"title": GENERATED_ONLY_TOKEN})
    assert res2.json().get("found"), \
        "判据失效后按标题仍然找不到生成物 —— 说明这条断言守的不是同一件事"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
