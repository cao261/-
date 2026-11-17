# -*- coding: utf-8 -*-
"""跨设备共享文件的并发写缺陷（2026-11-17 全面功能审查）。

**同一类缺陷在四个地方重复出现**：共享文件（坚果云同步根）上的
「读旧值 → 改 → 整份写回」，跨设备时对方的更新会被**静默覆盖**。

① 批注保存（`api_pdf_annotations_save`）
   乐观 CAS 的指纹取在**读之后** → 另一台设备若在「读」与「取指纹」之间写入，
   我们拿到的是**新**指纹 → 写前比对必然通过 → 用**旧基线**盖回去 →
   对方刚写的批注**静默消失**，而两个接口都返回 200。
② 批注删除（`api/pdf/annotations/delete`）
   **完全没有 CAS** → 整文件回写 → 对方新加的批注被一起抹掉。
③ 译文快照（`api_pdf_snapshot_save`）
   只有**进程内**的 `threading.Lock` → 挡不住另一台设备经坚果云写同一个文件
   → 对方新翻的页被整份盖掉 → 用户要重翻 = **重烧 token**。
④ files 通道「拉取还原」直接 `shutil.copyfile` → 目标文件在拷贝过程中是**半截**的
   → 坚果云同步客户端可能把半截 PDF 上行给对方。
⑤ 联动删除（`propagate_delete`）不记录**发起方** → 发起端自己那份也被删掉，
   且云端实体同时被释放 → **不可恢复**。

⚠ 这些用例全部是**行为级**：真的让「另一台设备」在读写窗口里写一次，
然后断言**可观察的后果**（对方的数据还在 / 接口返回 409 / 没有半截文件）。
不是「读源码文本断言里有某个字符串」那种恒真的假护栏。
"""
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _anno(path: Path, aid: str, quote: str = "引文") -> dict:
    return {"id": aid, "quote": quote, "page": 1, "color": "yellow",
            "updated_at": "2026-11-17T10:00:00"}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ids(path: Path) -> set:
    return {a.get("id") for a in (_read(path).get("annotations") or [])}


def _make_files_entry(app_mod, paper_id: str, rel: str, *, size: int,
                      owner: str = "A") -> str:
    """在 files 通道里造一份「清单 + 云端实体」。"""
    safe = app_mod._safe_paper_key(paper_id)
    files_dir = app_mod._files_dir()
    files_dir.mkdir(parents=True, exist_ok=True)
    (files_dir / f"{safe}.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * size)
    app_mod._write_file_meta(safe, {
        "paper_id": paper_id,
        "relative_path": rel,
        "size": size,
        "mtime": 0,
        "sha256": "x" * 64,
        "blob": True,
        "deleted": False,
        "propagate": False,
        "owner": owner,
        "published_at": "2026-11-17T00:00:00",
    })
    return safe


def _race_reader(app_mod, target: Path, payload: dict):
    """造一个「读完就把对方写的版本落盘」的读函数。

    模拟的是**另一台设备**在我们读完之后、写之前写了一次 ——
    这正是跨设备丢数据的那个窗口。
    """
    original = app_mod._read_json_or_quarantine
    state = {"done": False}

    def racing(path):
        data = original(path)
        if not state["done"] and Path(path) == Path(target):
            state["done"] = True
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False),
                              encoding="utf-8")
        return data

    return racing


# ---------------------------------------------------------------------------
# ① 批注保存：并发写不能被静默覆盖
# ---------------------------------------------------------------------------
def test_annotation_save_keeps_the_other_devices_concurrent_write(client, monkeypatch):
    """另一台设备在「我们读完 → 我们写」之间写的批注，必须还在。"""
    from microbench import app as app_mod

    paper = "cas_anno_paper_1"
    target = app_mod._annotation_path(paper)
    monkeypatch.setattr(
        app_mod, "_read_json_or_quarantine",
        _race_reader(app_mod, target, {
            "paper_id": paper,
            "saved_at": "2026-11-17T10:00:00",
            "updated_at": "2026-11-17T10:00:00",
            "schema_version": 2,
            "annotations": [_anno(target, "b1", "对方写的")],
        }),
    )

    r = client.post("/api/pdf/annotations/save", json={
        "paper_id": paper,
        "annotations": [_anno(target, "a1", "我写的")],
    })
    assert r.status_code == 200, r.text
    ids = _ids(target)
    assert "a1" in ids, f"自己的批注丢了: {ids}"
    assert "b1" in ids, (
        "另一台设备刚写的批注被静默覆盖了（CAS 的指纹取在读之后 → "
        "写前比对必然通过 → 用旧基线盖回去）: " + str(ids))


def test_annotation_save_gives_up_instead_of_overwriting_on_repeated_conflict(
        client, monkeypatch):
    """CAS 撞满 3 轮 → 409（放弃），**不能**强写覆盖对方的数据。"""
    from microbench import app as app_mod

    paper = "cas_anno_paper_2"
    target = app_mod._annotation_path(paper)
    assert client.post("/api/pdf/annotations/save", json={
        "paper_id": paper,
        "annotations": [_anno(target, "a1")],
    }).status_code == 200
    before = target.read_text(encoding="utf-8")

    counter = {"n": 0}

    def moving_fingerprint(path):
        counter["n"] += 1
        return (counter["n"], counter["n"])       # 每次都变 → 永远冲突

    monkeypatch.setattr(app_mod, "_file_fingerprint", moving_fingerprint)
    r = client.post("/api/pdf/annotations/save", json={
        "paper_id": paper,
        "annotations": [_anno(target, "a2")],
    })
    assert r.status_code == 409, (
        "连续冲突时应该放弃（409）并让客户端重试，而不是强写: " + r.text)
    assert target.read_text(encoding="utf-8") == before, (
        "冲突时文件被覆盖了 —— 强写 = 把「可能冲突」变成「确定丢数据」")


# ---------------------------------------------------------------------------
# ② 批注删除：也要走 CAS
# ---------------------------------------------------------------------------
def test_annotation_delete_keeps_the_other_devices_concurrent_write(client, monkeypatch):
    """删除一条批注时，对方同时新加的批注不能被整文件覆盖抹掉。"""
    from microbench import app as app_mod

    paper = "cas_anno_paper_3"
    target = app_mod._annotation_path(paper)
    assert client.post("/api/pdf/annotations/save", json={
        "paper_id": paper,
        "annotations": [_anno(target, "a1", "要删的")],
    }).status_code == 200

    monkeypatch.setattr(
        app_mod, "_read_json_or_quarantine",
        _race_reader(app_mod, target, {
            "paper_id": paper,
            "saved_at": "2026-11-17T11:00:00",
            "updated_at": "2026-11-17T11:00:00",
            "schema_version": 2,
            "annotations": [_anno(target, "a1", "要删的"),
                            _anno(target, "b1", "对方新加的")],
        }),
    )

    r = client.delete("/api/pdf/annotations/delete",
                      params={"paper_id": paper, "annotation_id": "a1"})
    assert r.status_code == 200, r.text
    assert r.json().get("deleted") == 1, (
        "deleted 应该是**真的被删掉的那几条**（不是删除前的总数）: "
        + r.text)
    ids = _ids(target)
    assert "a1" not in ids, f"要删的没删掉: {ids}"
    assert "b1" in ids, (
        "删除时把对方刚加的批注一起抹掉了（删除端点原来完全没有 CAS）: "
        + str(ids))


# ---------------------------------------------------------------------------
# ③ 译文快照：跨设备的 CAS（进程内锁挡不住另一台设备）
# ---------------------------------------------------------------------------
def test_snapshot_save_keeps_pages_translated_by_the_other_device(client, monkeypatch):
    """对方新翻的那一页，必须还在（否则用户要重翻 = 重烧 token）。"""
    from microbench import app as app_mod
    from microbench.paper_text import EXTRACTOR_VERSION

    paper = "cas_snapshot_paper_1"
    target = app_mod._snapshot_path(paper)
    monkeypatch.setattr(
        app_mod, "_read_json_or_quarantine",
        _race_reader(app_mod, target, {
            "schema_version": app_mod.SNAPSHOT_SCHEMA_VERSION,
            "extractor_version": EXTRACTOR_VERSION,
            "paper_id": paper,
            "pages": [{"page_num": 7, "text_hash": "b" * 16,
                       "translation": "对方翻的第 7 页"}],
        }),
    )

    r = client.post("/api/pdf/snapshot/save", json={
        "paper_id": paper,
        "paper_title": "并发测试",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 1.0,
        "pages": [{"page_num": 1, "text_hash": "a" * 16,
                   "translation": "我翻的第 1 页"}],
    })
    assert r.status_code == 200, r.text
    pages = {p["page_num"] for p in (_read(target).get("pages") or [])}
    assert 1 in pages, f"自己翻的页丢了: {pages}"
    assert 7 in pages, (
        "另一台设备新翻的页被整份盖掉了（快照原来只有进程内锁，"
        "挡不住跨设备写）: " + str(pages))


# ---------------------------------------------------------------------------
# ④ files 通道「拉取还原」必须原子
# ---------------------------------------------------------------------------
def test_files_restore_never_leaves_a_half_written_pdf(client, monkeypatch, sandbox):
    """还原中途失败 → 目标文件不能是半截的，也不能留下临时文件。"""
    from microbench import app as app_mod

    rel = "01_Literature/原子还原测试.pdf"
    payload = b"%PDF-1.4\n" + b"0" * 64
    _make_files_entry(app_mod, "atomic_restore_paper", rel, size=len(payload))

    def boom_copyfile(src, dst, *a, **kw):
        with open(dst, "wb") as fh:
            fh.write(b"PARTIAL")
        raise OSError("磁盘满了（模拟）")

    def boom_copyfileobj(src, dst, *a, **kw):
        dst.write(b"PARTIAL")
        raise OSError("磁盘满了（模拟）")

    monkeypatch.setattr(app_mod.shutil, "copyfile", boom_copyfile)
    monkeypatch.setattr(app_mod.shutil, "copyfileobj", boom_copyfileobj)

    app_mod._sync_pull_files()

    local = Path(sandbox) / rel
    assert not local.exists(), (
        "还原失败时留下了半截文件 —— 坚果云可能把它当新版本上传给对方")
    leftovers = [p.name for p in local.parent.iterdir()
                 if p.name != local.name]
    assert not leftovers, f"留下了临时文件: {leftovers}"


# ---------------------------------------------------------------------------
# ⑤ 联动删除：发起端自己那份不能被删
# ---------------------------------------------------------------------------
def test_propagate_delete_keeps_the_requesters_own_copy(client, monkeypatch, sandbox):
    """A 发起联动删除 → A 自己那份保留（要删由用户走正常的 vault 删除）。"""
    from microbench import app as app_mod

    rel = "01_Literature/联动删除测试.pdf"
    local = Path(sandbox) / rel
    local.parent.mkdir(parents=True, exist_ok=True)
    payload = b"%PDF-1.4\n" + b"0" * 32
    local.write_bytes(payload)
    _make_files_entry(app_mod, "propagate_paper", rel, size=len(payload),
                      owner="A")

    monkeypatch.setattr(app_mod, "_device_id", lambda: "A")
    r = client.post("/api/sync/files/propagate_delete",
                    params={"paper_id": "propagate_paper"})
    assert r.status_code == 200, r.text

    # A 自己同步 → 自己的本地副本**不能**被删
    app_mod._sync_pull_files()
    assert local.exists(), (
        "发起端自己那份被联动删除流程删掉了，而且云端实体同时被释放 → "
        "不可恢复")

    # 对方（B）同步 → 才删
    monkeypatch.setattr(app_mod, "_device_id", lambda: "B")
    app_mod._sync_pull_files()
    assert not local.exists(), "对方没有按联动删除删掉本地副本"


def test_propagate_delete_keeps_a_locally_modified_pdf(client, monkeypatch, sandbox):
    """本机那份已经被用户改过（大小对不上）→ 不该被一次同步删掉。"""
    from microbench import app as app_mod

    rel = "01_Literature/联动删除改版.pdf"
    local = Path(sandbox) / rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"%PDF-1.4\n" + b"0" * 32)
    _make_files_entry(app_mod, "propagate_paper_2", rel, size=16, owner="A")

    monkeypatch.setattr(app_mod, "_device_id", lambda: "A")
    assert client.post("/api/sync/files/propagate_delete",
                       params={"paper_id": "propagate_paper_2"}).status_code == 200
    monkeypatch.setattr(app_mod, "_device_id", lambda: "B")
    app_mod._sync_pull_files()
    assert local.exists(), (
        "本机这份已经和发布的不一样了（用户自己改过），却仍被联动删除删掉")
