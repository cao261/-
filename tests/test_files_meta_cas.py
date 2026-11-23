# -*- coding: utf-8 -*-
"""files/ 共享清单的**乐观 CAS + 字段合并**（2026-11-22 独立审查实测复现）。

## 坑是怎么来的

`files/<paper_id>.json` 这份清单在**坚果云同步根**里，**两台设备都写**。
原来的写法是「读 → 整份覆盖写回」：

    A 设备：用户删了那篇 PDF → 点「联动删除」
             → 清单上写入 deleted=True / propagate=True / propagate_by=A
    B 设备：同一时刻在同步发布这篇
             → **整份覆盖** → A 写的三个字段被抹成 deleted=False / propagate=False

后果：A 的联动删除请求**永远传不过去**（B 那台设备不会删），
而 A、B 两边**都返 200**「已标记 / 已同步」—— 静默失效，最难查。

「联动删除」是**三个字段合起来表达的一件事**
（读侧 `_sync_pull_files` 先看 `deleted` 才看 `propagate`），
所以这三个字段只能**整组**保留或整组作废。

## 这条护栏盯什么（行为级）

真的造出「对方设备写的联动删除三件套」，再跑一遍**发布路径**，
断言那三个字段**一个都没少**。

第二条更狠：在「我们读完 → 我们写」的窗口里，让**另一台设备**写一次
（跟 tests/test_sync_cas_2026_11_17.py 同一个手法），
断言对方写的内容**没有被整份盖回去**。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

OTHER_DEVICE = "OTHER_DEVICE_ID_0001"


def _seed_linked_delete(app_mod, rel: str, paper_id: str) -> str:
    """在 files 通道里造一份「对方设备发起的联动删除」清单。"""
    safe = app_mod._safe_paper_key(app_mod._paper_id_for(rel))
    files_dir = app_mod._files_dir()
    files_dir.mkdir(parents=True, exist_ok=True)
    app_mod._write_file_meta(safe, {
        "paper_id": paper_id,
        "relative_path": rel,
        "size": 11,
        "mtime": 0.0,
        "sha256": "a" * 64,
        "blob": True,
        "deleted": True,
        "propagate": True,
        "propagate_by": OTHER_DEVICE,
        "owner": OTHER_DEVICE,
        "published_at": "2026-11-22T00:00:00",
    })
    return safe


def _read_meta(app_mod, safe: str) -> dict:
    return json.loads(app_mod._file_meta_path(safe).read_text(encoding="utf-8"))


def test_publish_never_wipes_the_other_devices_linked_delete(sandbox):
    """本机发布不得抹掉**对方设备**发起的「联动删除」三件套。"""
    from microbench import app as app_mod

    rel = "01_Literature/cas_publish_paper.pdf"
    pdf = Path(sandbox) / rel
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 32)

    safe = _seed_linked_delete(app_mod, rel, "cas_publish_paper")

    app_mod._sync_push_files()

    meta = _read_meta(app_mod, safe)
    assert meta.get("deleted") is True, (
        f"发布把对方的 deleted 抹掉了：{meta}")
    assert meta.get("propagate") is True, (
        f"发布把对方的 propagate 抹掉了：{meta}")
    assert meta.get("propagate_by") == OTHER_DEVICE, (
        f"发布把对方的 propagate_by 抹掉了：{meta}")


def test_publish_keeps_the_other_devices_concurrent_write(sandbox, monkeypatch):
    """发布时，另一台设备在「我们读完 → 我们写」之间写的内容必须还在。

    跟 tests/test_sync_cas_2026_11_17.py 同一个手法：把 reader 换成一个
    「读完就把对方写的版本落盘」的版本。
    """
    from microbench import app as app_mod

    rel = "01_Literature/cas_publish_race.pdf"
    pdf = Path(sandbox) / rel
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 32)

    safe = app_mod._safe_paper_key(app_mod._paper_id_for(rel))
    files_dir = app_mod._files_dir()
    files_dir.mkdir(parents=True, exist_ok=True)
    target = app_mod._file_meta_path(safe)

    original = app_mod._read_json_or_quarantine
    state = {"done": False}

    def racing(path):
        data = original(path)
        if not state["done"] and Path(path) == Path(target):
            state["done"] = True
            payload = {
                "paper_id": "cas_publish_race",
                "relative_path": rel,
                "size": 11,
                "mtime": 0.0,
                "sha256": "b" * 64,
                "blob": True,
                "deleted": True,
                "propagate": True,
                "propagate_by": OTHER_DEVICE,
                "owner": OTHER_DEVICE,
                "published_at": "2026-11-22T00:00:00",
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False),
                              encoding="utf-8")
        return data

    monkeypatch.setattr(app_mod, "_read_json_or_quarantine", racing)

    app_mod._sync_push_files()

    meta = _read_meta(app_mod, safe)
    assert meta.get("propagate_by") == OTHER_DEVICE, (
        "另一台设备在窗口里写的联动删除被整份盖回去了（CAS 没生效）: " f"{meta}")
    assert meta.get("deleted") is True, f"对方写的 deleted 被盖回去了: {meta}"


def test_file_meta_write_never_uses_the_plain_overwrite(sandbox, monkeypatch):
    """**生产写路径**不许再走「整份覆盖」的 `_write_file_meta`。

    判据是行为级的：把 `_write_file_meta` 换成会抛异常的版本，
    然后跑发布路径 —— 它仍然成功 → 说明发布走的是 CAS 合并，不是整份覆盖。
    """
    from microbench import app as app_mod

    rel = "01_Literature/cas_publish_nowrite.pdf"
    pdf = Path(sandbox) / rel
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n" + b"0" * 32)

    def boom(*args, **kwargs):
        raise AssertionError(
            "生产写路径又用了整份覆盖的 _write_file_meta —— "
            "共享清单会被另一台设备的写静默覆盖")

    monkeypatch.setattr(app_mod, "_write_file_meta", boom)

    app_mod._sync_push_files()      # 不抛异常 = 走的是 CAS 合并路径
