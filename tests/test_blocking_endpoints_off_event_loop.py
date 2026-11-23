# -*- coding: utf-8 -*-
"""**阻塞 I/O 的端点不许跑在事件循环线程上**（2026-11-22 独立审查实测复现）。

## 机制（FastAPI / Starlette 的派发规则）

    · `async def` 端点 → 直接在**事件循环线程**上跑
    · `def`（同步）端点 → 被 `run_in_threadpool` 丢进**工作线程**

所以端点里只要有同步阻塞 I/O（读坚果云目录、写盘、`subprocess`、`unlink`），
写成 `async def` 就会**冻结整条事件循环** —— 无关请求从 15ms 变成秒级，
看板轮询 / SSE 翻译流 / 平板同步全停（AGENTS §4.58）。

本项目自己的先例就在旁边：`api_pdf_snapshot_save` 与 `api_sync_files_release`
本来就是**同步 `def`**，注释里写明「删几百 MB 的文件是同步阻塞 IO，
不该占着事件循环」。这轮把其余 6 个同类端点统一过来。

## 这条护栏怎么判（**行为级**，不是读源码）

真的把请求打进去，在**阻塞那一层**装探针，记录它跑在哪个线程：

    · 探针跑在**事件循环线程** → 端点是 async → 回归了
    · 探针跑在**别的线程**    → FastAPI 把它丢进了线程池 → 正确

判据是**线程身份**，不是「源码里有没有 async def」——
后者换个写法就假绿（AGENTS §4.62「字符串断言型护栏是格式的敌人」）。

## 为什么不能用 TestClient 量延迟

`TestClient` 把整个应用跑在**另一个线程**的 portal 里，
事件循环线程不是测试线程 → 「跑在哪条线程」这件事量不出来，
只能量延迟，而量延迟会因机器负载假红。这里改用
`httpx.ASGITransport` + `asyncio.run`：应用就跑在**测试自己这条**事件循环上，
线程身份一眼可辨。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import threading
from pathlib import Path

import httpx
import pytest

from microbench import app as app_mod


# ---------------------------------------------------------------------------
# 探针：在「会阻塞的原语」上包一层，记录 (标签, 线程 id)
# ---------------------------------------------------------------------------
class _Spy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def wrap(self, label: str, fn):
        def inner(*args, **kwargs):
            self.calls.append((label, threading.get_ident()))
            return fn(*args, **kwargs)
        return inner

    @property
    def fired(self) -> bool:
        return bool(self.calls)


def _install_spies(monkeypatch: pytest.MonkeyPatch) -> _Spy:
    """把探针装到所有可能承载「阻塞」的原语上。

    哪个先被调用就说明这个端点卡在哪 —— 不需要为每个端点单独设计。
    """
    spy = _Spy()
    orig_read_text = pathlib.Path.read_text
    orig_unlink = pathlib.Path.unlink
    orig_write_bytes = pathlib.Path.write_bytes
    orig_popen = subprocess.Popen
    orig_atomic = app_mod._atomic_write_text

    monkeypatch.setattr(pathlib.Path, "read_text",
                        spy.wrap("Path.read_text", orig_read_text))
    monkeypatch.setattr(pathlib.Path, "unlink",
                        spy.wrap("Path.unlink", orig_unlink))
    monkeypatch.setattr(pathlib.Path, "write_bytes",
                        spy.wrap("Path.write_bytes", orig_write_bytes))
    monkeypatch.setattr(subprocess, "Popen",
                        spy.wrap("subprocess.Popen", orig_popen))
    monkeypatch.setattr(app_mod, "_atomic_write_text",
                        spy.wrap("_atomic_write_text", orig_atomic))
    try:
        import send2trash
        monkeypatch.setattr(send2trash, "send2trash",
                            spy.wrap("send2trash", send2trash.send2trash))
    except ImportError:
        pass
    return spy


async def _dispatch(method: str, path: str, *, json=None, params=None,
                    query: dict | None = None, files=None) -> int:
    """真的打一次请求，返回状态码。应用跑在**本测试这条**事件循环上。"""
    transport = httpx.ASGITransport(app=app_mod.app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://testserver") as ac:
        resp = await ac.request(method, path, json=json, params=query, files=files)
        return resp.status_code


def _run_and_judge(monkeypatch, method, path, *, json=None, query=None,
                   expect_blocking=True, files=None):
    """打请求 → 返回 (是否在事件循环线程上阻塞, 详情)。"""
    spy = _install_spies(monkeypatch)
    loop_ident = {}

    async def scenario():
        loop_ident["id"] = threading.get_ident()
        return await _dispatch(method, path, json=json, query=query, files=files)

    status = asyncio.run(scenario())
    loop_id = loop_ident["id"]
    on_loop = [(label, tid) for label, tid in spy.calls if tid == loop_id]
    return status, spy.calls, on_loop, loop_id


# ---------------------------------------------------------------------------
# ① 批注保存 —— 写进**坚果云共享目录**，最典型的阻塞写路径
# ---------------------------------------------------------------------------
def test_annotation_save_does_not_block_the_event_loop(sandbox, monkeypatch):
    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "POST", "/api/pdf/annotations/save",
        json={"paper_id": "evloop_annotation_paper",
              "annotations": [{"id": "a1", "text": "探针", "page": 1,
                               "quote": "探针", "color": "yellow",
                               "created_at": "2026-11-22T00:00:00",
                               "updated_at": "2026-11-22T00:00:00"}]},
    )
    assert status == 200, f"批注保存应当成功，实际 {status}"
    assert calls, "探针一个都没触发 —— 说明这个端点没做它该做的写盘，" \
                   "这条护栏在空跑（判据失效）"
    assert not on_loop, (
        "批注保存的阻塞写盘跑在**事件循环线程**上："
        f"{on_loop}（事件循环线程 id={loop_id}）。"
        "修复：把 api_pdf_annotations_save 改成 **def**（同步端点），"
        "让 FastAPI 把它丢进线程池。")


# ---------------------------------------------------------------------------
# ② 批注删除
# ---------------------------------------------------------------------------
def test_annotation_delete_does_not_block_the_event_loop(sandbox, monkeypatch):
    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "POST", "/api/pdf/annotations/save",
        json={"paper_id": "evloop_annotation_del",
              "annotations": [{"id": "a1", "text": "探针", "page": 1,
                               "quote": "探针", "color": "yellow",
                               "created_at": "2026-11-22T00:00:00",
                               "updated_at": "2026-11-22T00:00:00"}]},
    )
    assert status == 200, status

    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "DELETE", "/api/pdf/annotations/delete",
        query={"paper_id": "evloop_annotation_del", "annotation_id": "a1"},
    )
    assert status == 200, f"批注删除应当成功，实际 {status}"
    assert calls, "探针一个都没触发 —— 这条护栏在空跑"
    assert not on_loop, (
        "批注删除的阻塞写盘跑在**事件循环线程**上："
        f"{on_loop}（事件循环线程 id={loop_id}）。"
        "修复：把 api_pdf_annotations_delete 改成 **def**。")


# ---------------------------------------------------------------------------
# ③ 读 vault 里的笔记（同步读盘）
# ---------------------------------------------------------------------------
def test_vault_file_read_does_not_block_the_event_loop(sandbox, monkeypatch):
    target = Path(sandbox) / "01_Literature" / "evloop_probe.md"
    target.write_text("# 探针\n\n阻塞读盘测试。\n", encoding="utf-8")

    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "GET", "/api/vault/file",
        query={"relative_path": "01_Literature/evloop_probe.md"},
    )
    assert status == 200, f"读 vault 文件应当成功，实际 {status}"
    assert calls, "探针一个都没触发 —— 这条护栏在空跑"
    assert not on_loop, (
        "读 vault 文件的同步读盘跑在**事件循环线程**上："
        f"{on_loop}（事件循环线程 id={loop_id}）。"
        "修复：把 api_vault_file 改成 **def**。")


# ---------------------------------------------------------------------------
# ④ 删除快照（unlink）
# ---------------------------------------------------------------------------
def test_snapshot_delete_does_not_block_the_event_loop(sandbox, monkeypatch):
    # ⚠ 删快照是**幂等**的：文件不存在就直接返回，不会 unlink →
    #   探针永远不触发 → 护栏在空跑。所以先真的造一个快照文件出来。
    snap = app_mod._snapshot_path("evloop_snapshot_paper")
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text('{"paper_id": "evloop_snapshot_paper", "pages": []}',
                    encoding="utf-8")
    assert snap.exists(), "快照文件没造出来，这条护栏没法测"

    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "DELETE", "/api/pdf/snapshot/delete",
        query={"paper_id": "evloop_snapshot_paper"},
    )
    assert status == 200, f"删快照应当成功（幂等），实际 {status}"
    assert calls, "探针一个都没触发 —— 这条护栏在空跑"
    assert not on_loop, (
        "删快照的同步 unlink 跑在**事件循环线程**上："
        f"{on_loop}（事件循环线程 id={loop_id}）。"
        "修复：把 api_pdf_snapshot_delete 改成 **def**。")


# ---------------------------------------------------------------------------
# ⑤ 在文件管理器里显示（subprocess）
# ---------------------------------------------------------------------------
def test_vault_reveal_does_not_block_the_event_loop(sandbox, monkeypatch):
    target = Path(sandbox) / "01_Literature" / "evloop_reveal.md"
    target.write_text("# 探针\n", encoding="utf-8")

    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "POST", "/api/vault/reveal",
        query={"relative_path": "01_Literature/evloop_reveal.md"},
    )
    assert status == 200, f"reveal 应当成功，实际 {status}"
    assert calls, "探针一个都没触发 —— 这条护栏在空跑"
    assert not on_loop, (
        "reveal 的 subprocess 跑在**事件循环线程**上："
        f"{on_loop}（事件循环线程 id={loop_id}）。"
        "修复：把 api_vault_reveal 改成 **def**。")


# ---------------------------------------------------------------------------
# ⑥ 删除 vault 文件（send2trash / unlink）
# ---------------------------------------------------------------------------
def test_vault_delete_does_not_block_the_event_loop(sandbox, monkeypatch):
    target = Path(sandbox) / "01_Literature" / "evloop_delete.md"
    target.write_text("# 探针\n", encoding="utf-8")

    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "DELETE", "/api/vault/file",
        query={"relative_path": "01_Literature/evloop_delete.md"},
    )
    assert status == 200, f"删 vault 文件应当成功，实际 {status}"
    assert calls, "探针一个都没触发 —— 这条护栏在空跑"
    assert not on_loop, (
        "删 vault 文件的阻塞操作跑在**事件循环线程**上："
        f"{on_loop}（事件循环线程 id={loop_id}）。"
        "修复：把 api_vault_delete_file 改成 **def**。")


# ---------------------------------------------------------------------------
# ⑦ 上传 PDF —— 40MB 写盘（落点可能在坚果云 junction 里）
# ---------------------------------------------------------------------------
def test_pdf_upload_does_not_block_the_event_loop(sandbox, monkeypatch):
    """上传 PDF 的写盘不许占着事件循环。

    一次最多 40MB，而落点 `01_Literature/...` **可能在坚果云 junction 里**
    （云同步目录写几十 MB 要好几秒）→ 占着事件循环 = 看板 / SSE 翻译流
    / 平板同步全停（§4.58 同型）。
    """
    # 把重活换成假货：只测「写盘那一步跑在哪条线程」
    # ⚠ extract_text_from_pdf 是被 `asyncio.to_thread(...)` 调用的 →
    #   这里必须给**同步**函数（给 async 的话 to_thread 会返回一个协程对象，
    #   后面的 len(pages) 就炸了）。
    def fake_extract(pdf_bytes):
        return [{"text": "探针正文。", "page": 1}]

    async def fake_glyphs(pages, pdf_bytes):
        return None

    monkeypatch.setattr(app_mod, "extract_text_from_pdf", fake_extract)
    monkeypatch.setattr(app_mod, "_resolve_document_glyphs", fake_glyphs)
    monkeypatch.setattr(app_mod, "_schedule_glyph_backfill", lambda b: None)

    status, calls, on_loop, loop_id = _run_and_judge(
        monkeypatch, "POST", "/api/paper/upload_pdf",
        files={"file": ("probe.pdf", b"%PDF-1.4\nprobe body\n",
                        "application/pdf")},
    )
    assert status == 200, f"上传应当成功，实际 {status}"
    assert calls, "探针一个都没触发 —— 这条护栏在空跑"
    assert not on_loop, (
        "上传 PDF 的写盘跑在**事件循环线程**上："
        f"{on_loop}（事件循环线程 id={loop_id}）。"
        "修复：把写盘丢进 `await asyncio.to_thread(...)`。")
