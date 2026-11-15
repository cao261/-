# -*- coding: utf-8 -*-
"""2026-11-15 独立审查 P1-1：隔离读必须区分「内容损坏」与「瞬时占用」。

问题（独立审查实测确认）：
  `_read_json_or_quarantine()` 的 except 混着 `OSError` ——
  Windows 上文件被坚果云客户端 / 杀软 / 别的进程短暂占用（PermissionError）
  时，会把**内容完全健康**的快照改名成 `<id>.json.corrupt-<时间戳>`：
    * 译文从界面上消失（读者按 `<id>.json` 找，找不到）；
    * 用户以为自己的译文没了 → 重翻 → 白烧 token；
    * 而文件其实是好的，只是当时被占用了一下。

现在分三类：
  * 内容坏（解码失败 / JSON 坏 / 顶层不是对象）→ 改名隔离 + 报错；
  * 瞬时 I/O 故障 → **一个字节都不动**，只报错让用户重试；
  * 文件刚好不存在 → 当成「没有旧值」。

这些断言全是**行为级**：不读源码文本，只看真实文件在磁盘上的状态。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _snap_payload(paper: str, page: int = 1, translation: str = "新译文"):
    return {
        "paper_id": paper,
        "paper_title": "测试论文",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 1.0,
        "pages": [{"page_num": page, "text_hash": "abcdef0123456789",
                   "translation": translation, "model": "m",
                   "translated_at": "2026-11-15T12:00:00"}],
    }


@pytest.fixture()
def locked_read(monkeypatch):
    """让**指定路径**的 read_text 抛 PermissionError（模拟文件被占用）。"""
    real_read = Path.read_text
    target: dict = {"path": None}

    def _fake(self, *args, **kwargs):
        if target["path"] is not None and self == target["path"]:
            raise PermissionError(32, "另一个程序正在使用此文件")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _fake)
    return target


# ---------------------------------------------------------------------------
# 1) 瞬时占用：文件一个字节都不能动
# ---------------------------------------------------------------------------

def test_transient_read_failure_does_not_rename_a_healthy_file(tmp_path, locked_read):
    from microbench import app

    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"pages": [{"page_num": 1}]}), encoding="utf-8")
    locked_read["path"] = p

    with pytest.raises(ValueError) as ei:
        app._read_json_or_quarantine(p)

    # 报错里必须说清楚「不是内容损坏」，否则用户会去手工恢复一个没坏的文件
    assert "不是内容损坏" in str(ei.value), str(ei.value)
    # 文件还在原名、原处、原内容（先解除模拟占用，再回读）
    locked_read["path"] = None
    assert p.exists(), "瞬时占用把用户的快照改名了（译文会从界面上消失）"
    assert json.loads(p.read_text(encoding="utf-8"))["pages"][0]["page_num"] == 1
    # 没有任何隔离副本被制造出来
    assert not list(tmp_path.glob("snap.json.corrupt-*")), \
        "瞬时占用不该产生 .corrupt- 隔离文件"


def test_transient_error_is_distinguishable_from_corruption():
    """调用点要能区分两者：既兼容既有的 `except ValueError`，又能分辨。"""
    from microbench import app

    assert issubclass(app._TransientReadError, ValueError), \
        "必须仍是 ValueError —— 既有调用点靠它转成 HTTP 500"
    assert not issubclass(ValueError, app._TransientReadError)


def test_snapshot_save_keeps_the_snapshot_when_read_fails_transiently(client, sandbox,
                                                                      locked_read):
    """端到端：保存时读被占用 → 报错，但**磁盘上原有的译文一页都不能少**。"""
    from microbench import app

    paper = "transient_lock_snapshot_paper"
    assert client.post("/api/pdf/snapshot/save",
                       json=_snap_payload(paper, 1)).status_code == 200

    target = app._snapshot_path(paper)
    before = target.read_text(encoding="utf-8")
    locked_read["path"] = target

    r = client.post("/api/pdf/snapshot/save", json=_snap_payload(paper, 2))

    assert r.status_code >= 400, f"读不到旧值却报成功（会静默丢页）: {r.text}"
    # 关键断言：原文件仍在原名，且第 1 页还在（先解除模拟占用，再回读）
    locked_read["path"] = None
    assert target.exists(), "快照被改名了 —— 用户的译文会凭空消失"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [p["page_num"] for p in data["pages"]] == [1], \
        f"旧译文被覆盖了: {data['pages']}"
    assert target.read_text(encoding="utf-8") == before
    assert not list(target.parent.glob(target.name + ".corrupt-*")), \
        "瞬时占用不该产生 .corrupt- 隔离文件"


# ---------------------------------------------------------------------------
# 2) 反向护栏：真·损坏仍然必须被隔离（别把保护改没了）
# ---------------------------------------------------------------------------

def test_corrupt_json_is_still_quarantined(tmp_path):
    from microbench import app

    p = tmp_path / "corrupt.json"
    p.write_text('{"pages": [{"page_num": 1', encoding="utf-8")

    with pytest.raises(ValueError):
        app._read_json_or_quarantine(p)

    kept = list(tmp_path.glob("corrupt.json.corrupt-*"))
    assert kept, "真损坏的文件必须被隔离保留（供人工恢复）"
    assert kept[0].read_text(encoding="utf-8").startswith('{"pages":')


def test_non_dict_top_level_is_still_quarantined(tmp_path):
    from microbench import app

    p = tmp_path / "array.json"
    p.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError):
        app._read_json_or_quarantine(p)

    assert list(tmp_path.glob("array.json.corrupt-*")), "顶层不是对象也要隔离"


def test_invalid_utf8_is_still_quarantined(tmp_path):
    from microbench import app

    p = tmp_path / "bin.json"
    p.write_bytes(b"\xff\xfe\x00bad")

    with pytest.raises(ValueError):
        app._read_json_or_quarantine(p)

    assert list(tmp_path.glob("bin.json.corrupt-*")), "非 UTF-8 也要隔离"


# ---------------------------------------------------------------------------
# 3) 文件不存在 = 「没有旧值」，不是损坏
# ---------------------------------------------------------------------------

def test_missing_file_is_not_treated_as_corruption(tmp_path):
    from microbench import app

    p = tmp_path / "never-existed.json"
    assert app._read_json_or_quarantine(p) is None
    assert not list(tmp_path.glob("*.corrupt-*"))
