# -*- coding: utf-8 -*-
"""论文 PDF 实体通道（files/）的测试：增量、墓碑、还原、释放、冲突。"""
import json
from pathlib import Path

import pytest

PDF_DIR = "01_Literature/04_Survey_综述/01_论文原文_PDF"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from microbench import app
    vault = tmp_path / "vault"
    (vault / PDF_DIR).mkdir(parents=True)
    sync = tmp_path / "sync"
    sync.mkdir()
    monkeypatch.setattr(app, "BASE_DIR", vault)
    monkeypatch.setattr(app, "_sync_root", lambda: sync)
    monkeypatch.setenv("ANNOTATIONS_DIR", str(sync))
    return app, vault, sync


def _make_pdf(vault: Path, name: str, payload: bytes = b"%PDF-1.4 fake") -> Path:
    p = vault / PDF_DIR / name
    p.write_bytes(payload)
    return p


def test_paper_id_matches_frontend_algorithm():
    """后端复刻的 paper_id 必须和前端 index.html 算出来的一模一样。"""
    from microbench import app
    rel = (f"{PDF_DIR}/2008-JACERS-综述-Magnetoelectric laminate composites "
           "An overview.pdf")
    assert app._paper_id_for(rel) == (
        "2008-JACERS-综述-Magnetoelectric laminate composites An overview_pgghcp")
    # 完整相对路径参与 hash → 换个目录 hash 就变
    other = "01_Literature/99_Other/01_论文原文_PDF/x.pdf"
    assert app._paper_id_for(other) != app._paper_id_for(rel)


def test_push_publishes_then_is_incremental(env):
    app, vault, sync = env
    _make_pdf(vault, "a.pdf", b"a" * 100)
    _make_pdf(vault, "b.pdf", b"b" * 200)

    rep = app._sync_push_files()
    assert len(rep["published"]) == 2
    assert rep["unchanged"] == 0
    assert (sync / "files").is_dir()
    blobs = list((sync / "files").glob("*.pdf"))
    assert len(blobs) == 2
    metas = list((sync / "files").glob("*.json"))
    assert len(metas) == 2
    meta = json.loads(metas[0].read_text(encoding="utf-8"))
    assert meta["blob"] is True
    assert meta["deleted"] is False
    assert meta["relative_path"].startswith(PDF_DIR)
    assert len(meta["sha256"]) == 64

    # 第二次：什么都没变 → 零传输
    rep2 = app._sync_push_files()
    assert rep2["published"] == []
    assert rep2["updated"] == []
    assert rep2["unchanged"] == 2


def test_push_detects_content_change(env):
    app, vault, sync = env
    p = _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    p.write_bytes(b"a" * 150)          # 内容和大小都变了
    rep = app._sync_push_files()
    assert len(rep["updated"]) == 1
    assert rep["updated"][0].endswith("a.pdf")


def test_does_not_tombstone_papers_published_by_other_device(env):
    """对方发布、本机没有的论文 → 不能被误判成「我删了」。

    误标墓碑的后果：对方下次同步时把自己那篇 PDF 删掉（数据丢失）。
    """
    app, vault, sync = env
    files = sync / "files"
    files.mkdir(parents=True, exist_ok=True)
    rel = f"{PDF_DIR}/theirs.pdf"
    (files / "theirs.json").write_text(json.dumps({
        "paper_id": "theirs_xxx", "relative_path": rel, "size": 10,
        "mtime": 1.0, "sha256": "x" * 64, "blob": True, "deleted": False,
        "propagate": False, "owner": "OTHERDEVICE",
    }), encoding="utf-8")

    rep = app._sync_push_files()
    assert rep["deleted"] == []
    meta = json.loads((files / "theirs.json").read_text(encoding="utf-8"))
    assert meta["deleted"] is False


def test_pulled_paper_from_other_device_is_not_tombstoned(env):
    """对方推的论文，本机拉下来后再次同步 —— 不能变成墓碑。"""
    app, vault, sync = env
    files = sync / "files"
    files.mkdir(parents=True, exist_ok=True)
    rel = f"{PDF_DIR}/theirs.pdf"
    safe = "theirs_xxx"
    (files / f"{safe}.pdf").write_bytes(b"theirs" * 50)
    (files / f"{safe}.json").write_text(json.dumps({
        "paper_id": safe, "relative_path": rel, "size": 300,
        "mtime": 1.0, "sha256": "y" * 64, "blob": True, "deleted": False,
        "propagate": False, "owner": "OTHERDEVICE",
    }), encoding="utf-8")

    pull = app._sync_pull_files()
    assert pull["restored"] == [rel]
    assert (vault / rel).exists()

    rep = app._sync_push_files()
    assert rep["deleted"] == []
    meta = json.loads((files / f"{safe}.json").read_text(encoding="utf-8"))
    assert meta["deleted"] is False


def test_released_blob_is_not_silently_re_uploaded(env):
    """用户释放了云端 PDF → 同步不该偷偷把它传回去（除非内容真的变了）。"""
    app, vault, sync = env
    _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()

    blob = next((sync / "files").glob("*.pdf"))
    blob.unlink()
    meta_file = next((sync / "files").glob("*.json"))
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta["blob"] = False
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    rep = app._sync_push_files()
    assert rep["published"] == []
    assert rep["updated"] == []
    assert rep["unchanged"] == 1
    assert not list((sync / "files").glob("*.pdf"))


def test_push_marks_tombstone_when_local_removed(env):
    app, vault, sync = env
    p = _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    p.unlink()
    rep = app._sync_push_files()
    assert len(rep["deleted"]) == 1
    meta = json.loads(next((sync / "files").glob("*.json")).read_text(encoding="utf-8"))
    assert meta["deleted"] is True
    assert meta["propagate"] is False      # 默认不联动删对方本地


def test_pull_restores_missing_pdf_to_original_relative_path(env):
    app, vault, sync = env
    _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    # 模拟"另一台设备推过来的 PDF"：本机还没有
    (vault / PDF_DIR / "a.pdf").unlink()
    rep = app._sync_pull_files()
    assert len(rep["restored"]) == 1
    assert (vault / PDF_DIR / "a.pdf").exists()
    assert (vault / PDF_DIR / "a.pdf").read_bytes() == b"a" * 100


def test_pull_does_not_overwrite_conflicting_local_file(env):
    app, vault, sync = env
    _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    local = vault / PDF_DIR / "a.pdf"
    local.write_bytes(b"different-content")
    rep = app._sync_pull_files()
    assert len(rep["conflicts"]) == 1
    assert local.read_bytes() == b"different-content"     # 本机版本没被覆盖


def test_tombstone_without_propagate_keeps_local_file(env):
    app, vault, sync = env
    p = _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    p.unlink()
    app._sync_push_files()               # → 墓碑
    # 另一台设备（这里用同一份 meta 模拟）拉取：默认保留本地
    (vault / PDF_DIR / "a.pdf").write_bytes(b"a" * 100)
    rep = app._sync_pull_files()
    assert len(rep["removed"]) == 0
    assert len(rep["retained"]) == 1
    assert (vault / PDF_DIR / "a.pdf").exists()


def test_tombstone_with_propagate_removes_local_file(env):
    app, vault, sync = env
    p = _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    p.unlink()
    app._sync_push_files()               # → 墓碑
    # 另一台设备本地有这篇
    (vault / PDF_DIR / "a.pdf").write_bytes(b"a" * 100)
    # 墓碑改成传播
    meta_file = next((sync / "files").glob("*.json"))
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    meta["propagate"] = True
    meta_file.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    rep = app._sync_pull_files()
    assert len(rep["removed"]) == 1
    assert not (vault / PDF_DIR / "a.pdf").exists()


def test_release_frees_blob_but_keeps_meta_and_annotations(env):
    app, vault, sync = env
    _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    ann = sync / "annotations"
    ann.mkdir(exist_ok=True)
    (ann / "note.json").write_text("{}", encoding="utf-8")

    from fastapi.testclient import TestClient
    with TestClient(app.app) as client:
        r = client.post("/api/sync/files/release", json={"all": True})
        assert r.status_code == 200
        data = r.json()
        assert data["released"] == 1
        assert data["freed_bytes"] == 100

    assert not list((sync / "files").glob("*.pdf"))     # 实体没了
    assert list((sync / "files").glob("*.json"))        # 清单还在
    assert (ann / "note.json").exists()                 # 批注没动
    meta = json.loads(next((sync / "files").glob("*.json")).read_text(encoding="utf-8"))
    assert meta["blob"] is False


def test_files_status_reports_cloud_and_local(env):
    app, vault, sync = env
    _make_pdf(vault, "a.pdf", b"a" * 100)
    app._sync_push_files()
    st = app._sync_files_status()
    assert st["count"] == 1
    assert st["total_bytes"] == 100
    assert st["items"][0]["local"] is True
    assert st["items"][0]["blob"] is True
