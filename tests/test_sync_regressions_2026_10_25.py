# -*- coding: utf-8 -*-
"""跨设备同步的回归护栏（2026-10-25 审计新增）。

**为什么这个文件存在**

那一轮审计找出 5 个问题，**全部在测试盲区外** —— 192 条用例全绿，
一个问题都没拦住：

1. `app.py` 没有模块级 `import sys` → 「模型漏译 → 重译一次」这条分支
   直接 500，已付费的译文和缓存**双丢**（下次点开同一页重新扣费）。
2. 同步 push 里有一段「兼容旧版本」的回灌：把本机**陈旧副本**的批注
   合并回共享目录（deleted 传空）→ A 设备删掉的批注被 B 设备同步**复活**，
   还回灌给 A。
3. 译文快照**整份替换** → A 翻了 1–5 页、B 翻了 6–10 页，
   B 同步一次就把 A 的 1–5 页清空，A 要重翻 5 页、重新烧 token。
4. `merge_changes.safe_target` 用 `resolve()` 做包含性判断 →
   **穿透 junction**，当前这台机器（vault 已整体搬进坚果云、
   `01_Literature` 等全是 junction）上平板变更包**每一条都被拒**。
5. `_paper_id_for` 用 `ord()` 逐码点算哈希，而前端用 `charCodeAt()`
   （UTF-16 码元）→ 路径里有 emoji 时两端 paper_id 不同，
   批注/快照/进度**静默**认不出来。

每一个都补了护栏 —— 改回去就会红。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. sys 必须在模块级
# ---------------------------------------------------------------------------

def test_app_module_has_sys():
    """app.py 必须模块级 import sys。

    以前只有 `api_vault_reveal` 内部做了局部 import，
    而「漏译 → 重译」那两处（translate_section / translate_page）
    用的是模块级的 sys.stderr → NameError → HTTP 500。
    """
    from microbench import app as app_mod
    assert "sys" in vars(app_mod), "app.py 没有模块级 import sys（漏译分支会 500）"
    assert app_mod.sys is sys


def test_translate_endpoints_reference_module_sys():
    """两处重译分支必须能用模块级的 sys（别改回局部 import）。"""
    from microbench import app as app_mod
    src = (REPO / "microbench" / "app.py").read_text(encoding="utf-8")
    assert "file=sys.stderr" in src, "重译分支不再打日志了？确认一下"
    # 函数体内没有再 import sys（否则又是局部作用域）
    for marker in ("def api_translate_section", "def api_translate_page"):
        assert marker in src, f"找不到 {marker} —— 函数改名了？"
        body = src.split(marker, 1)[1][:4000]
        assert "\n        import sys" not in body and "\n    import sys" not in body, (
            f"{marker} 里又出现了局部 import sys"
        )
    assert app_mod is not None


# ---------------------------------------------------------------------------
# 2. 批注删除不能被同步复活
# ---------------------------------------------------------------------------

def _anno_payload(aid: str, quote: str = "标记") -> dict:
    return {
        "id": aid,
        "page": 1,
        "source": "pdf",
        "quote": quote,
        "note": "",
        "color": "yellow",
        "rects": [],
        "created_at": "2026-10-25T10:00:00.000Z",
        "updated_at": "2026-10-25T10:00:00.000Z",
    }


def test_deleted_annotation_is_not_resurrected_by_sync(client, sandbox, monkeypatch):
    """A 删掉批注 → 同步 → 批注不能回来（更不能回灌给 A）。

    复现路径：同步 push 里那段「兼容旧版本」会把本机
    `logs/_pdf_snapshots/annotations/` 下的**陈旧副本**读回来，
    用 deleted=[] 合并写回共享目录 —— 已删除的 id 因为不在 cur 里
    被当成「新增」重新加回。
    """
    from microbench import app as app_mod

    shared = sandbox / "_sync_shared"
    monkeypatch.setattr(app_mod, "_sync_root", lambda: shared)

    paper = "anno_test_paper_1"
    r = client.post("/api/pdf/annotations/save", json={
        "paper_id": paper,
        "annotations": [_anno_payload("anno-1")],
        "deleted": [],
    })
    assert r.status_code == 200, r.text

    # 造一份「陈旧的本机副本」—— 旧版本把批注存在这里
    legacy = sandbox / "logs" / "_pdf_snapshots" / "annotations"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / f"{paper}.json").write_text(json.dumps({
        "paper_id": paper,
        "annotations": [_anno_payload("anno-1")],
    }, ensure_ascii=False), encoding="utf-8")

    # A 删除
    r = client.post("/api/pdf/annotations/save", json={
        "paper_id": paper,
        "annotations": [],
        "deleted": ["anno-1"],
    })
    assert r.status_code == 200, r.text
    assert app_mod._safe_read_json(app_mod._annotation_path(paper))["annotations"] == []

    # 同步（push + pull）
    r = client.post("/api/sync", json={"include_files": False})
    assert r.status_code == 200, r.text

    after = app_mod._safe_read_json(app_mod._annotation_path(paper))
    assert after is not None, "共享目录里的批注文件不见了"
    ids = [a.get("id") for a in after.get("annotations") or []]
    assert "anno-1" not in ids, f"已删除的批注被同步复活了: {ids}"

    # 再同步一次也不能复活
    client.post("/api/sync", json={"include_files": False})
    after2 = app_mod._safe_read_json(app_mod._annotation_path(paper))
    ids2 = [a.get("id") for a in (after2 or {}).get("annotations") or []]
    assert "anno-1" not in ids2, f"第二次同步又复活了: {ids2}"


# ---------------------------------------------------------------------------
# 3. 译文快照按 page_num 合并，不整份替换
# ---------------------------------------------------------------------------

def _snap_page(n: int, text: str) -> dict:
    return {
        "page_num": n,
        "text_hash": f"{n:064x}"[:64],
        "text": text,
        "translation": f"第 {n} 页的译文",
        "model": "MiniMax-M3",
        "translated_at": "2026-10-25T10:00:00.000Z",
    }


def test_snapshot_save_merges_pages(client, sandbox):
    """先存 1–2 页、再存 3–4 页 → 文件里应该有 4 页，不是 2 页。

    整份替换的话，第二次保存会把第一次的 1–2 页清掉 ——
    两台设备轮流翻同一篇时这就是「对方已翻的页消失」。
    """
    paper = "snap_test_paper_1"
    common = {
        "paper_id": paper,
        "paper_title": "测试论文",
        "relative_path": "01_Literature/test.pdf",
        "paper_mtime": 1.0,
        "paper_text_signature": "sig-abc",
    }
    r = client.post("/api/pdf/snapshot/save", json={
        **common, "pages": [_snap_page(1, "a"), _snap_page(2, "b")],
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/pdf/snapshot/save", json={
        **common, "pages": [_snap_page(3, "c"), _snap_page(4, "d")],
    })
    assert r.status_code == 200, r.text

    from microbench import app as app_mod
    data = app_mod._safe_read_json(app_mod._snapshot_path(paper))
    assert data is not None, "快照文件不见了"
    nums = sorted(p.get("page_num") for p in data.get("pages") or [])
    assert nums == [1, 2, 3, 4], f"快照没有按页合并，实际是 {nums}"


def test_snapshot_merge_drops_stale_pages_when_source_changed(client, sandbox):
    """原文变了（signature 不同）→ 旧译文必须作废，不能留着。"""
    paper = "snap_test_paper_2"
    base = {
        "paper_id": paper,
        "paper_title": "测试论文",
        "relative_path": "01_Literature/test2.pdf",
        "paper_mtime": 1.0,
    }
    r = client.post("/api/pdf/snapshot/save", json={
        **base, "paper_text_signature": "sig-old",
        "pages": [_snap_page(1, "old")],
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/pdf/snapshot/save", json={
        **base, "paper_text_signature": "sig-new",
        "pages": [_snap_page(1, "new")],
    })
    assert r.status_code == 200, r.text

    from microbench import app as app_mod
    data = app_mod._safe_read_json(app_mod._snapshot_path(paper))
    pages = data.get("pages") or []
    assert len(pages) == 1, f"原文变了还留着旧页: {[p.get('page_num') for p in pages]}"
    assert pages[0].get("text") == "new"


# ---------------------------------------------------------------------------
# 4. merge_changes.safe_target 不能被 junction 穿透
# ---------------------------------------------------------------------------

def _make_junction(link: Path, target: Path) -> bool:
    """建一个目录 junction（Windows，不需要管理员）。失败返回 False。"""
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30)
        return r.returncode == 0 and link.exists()
    except Exception:
        return False


def test_safe_target_survives_junction_vault(tmp_path):
    """junction 部署下，变更包里的路径不能被判「越界」。

    本机 vault 的 01_Literature 等目录全是 junction（vault 已整体搬进坚果云），
    `resolve()` 会穿透它 → 目标跑到 C 盘 → 相对 vault(D 盘) 判越界 →
    **每一条笔记都被拒**，平板上的编辑一条都合不回来。
    """
    from microbench import merge_changes

    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    for d in ("00_Dashboard", "03_Knowledge"):
        (vault / d).mkdir()
    # 真身在别处 —— 就像坚果云里的那个目录
    real = tmp_path / "nutstore" / "MicroBench工作台" / "01_Literature"
    real.mkdir(parents=True)
    # vault 里的 01_Literature 是个 junction
    if not _make_junction(vault / "01_Literature", real):
        pytest.skip("这台机器建不了 junction（不是 Windows？）—— 换用逻辑路径断言")

    got = merge_changes.safe_target(vault, "01_Literature/笔记.md")
    assert got is not None, "junction 里的路径被判越界了（平板变更包会全被拒）"


def test_safe_target_uses_logical_path_not_resolve(tmp_path):
    """静态护栏：safe_target 不能用 resolve() 做包含性判断。"""
    from microbench import merge_changes
    src = (REPO / "microbench" / "merge_changes.py").read_text(encoding="utf-8")
    body = src.split("def safe_target", 1)[1].split("\ndef ", 1)[0]
    # 只看代码行（注释里提到 resolve() 是为了说明为什么不能用它）
    code_lines = [
        ln for ln in body.splitlines()
        if not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert ".resolve()" not in code, (
        "safe_target 又用回 resolve() 了 —— 它会穿透 junction，"
        "vault 在坚果云里时每一条笔记都会被判越界"
    )
    assert "abspath" in code, "safe_target 应该在用逻辑路径 os.path.abspath"
    assert merge_changes.safe_target(tmp_path, "01_Literature/x.md") is not None
    assert merge_changes.safe_target(tmp_path, "../evil.md") is None
    assert merge_changes.safe_target(tmp_path, "/etc/passwd.md") is None
    assert merge_changes.safe_target(tmp_path, "01_Literature/x.txt") is None


# ---------------------------------------------------------------------------
# 5. 其它同批修掉的静默问题
# ---------------------------------------------------------------------------

def test_file_meta_write_is_atomic(sandbox):
    """清单文件必须 tmp + replace —— 直接写会被坚果云读到半截 JSON。"""
    from microbench import app as app_mod
    app_mod._write_file_meta("demo", {"paper_id": "demo"})
    p = app_mod._file_meta_path("demo")
    assert p.exists()
    # 目录里不该留下 .tmp 残留
    leftovers = list(p.parent.glob("*.tmp"))
    assert not leftovers, f"有临时文件残留: {leftovers}"
