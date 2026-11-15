# -*- coding: utf-8 -*-
"""merge_changes 的 watch 守护循环：**单个包合并失败不许让进程退出**。

2026-11-15 审计发现（交接文档 `HANDOFF_2026-11-15-MERGE-CLOUD-AUDIT.md` §2.3）：
同型缺陷 `export_offline.py` 在 2026-11-09 那轮已经修了
（单轮 try/except + 失败不标记 → 下一轮重试），**`merge_changes.py` 漏了** → 修一半。

触发路径极常见，不是理论风险：

    `_atomic_write_text` 里的 `os.replace(tmp, path)` 在 Windows 上遇到
    Obsidian / 杀软 / 坚果云同步客户端**占用了那篇笔记**
    → 抛 `PermissionError`（OSError）
    → 原来一路冒到 `while True` 外面 → 只被 `except KeyboardInterrupt` 兜着
    → **守护进程当场退出**，后面的变更包再也合并不进来（要人工重启才恢复）。

⚠ 这三条护栏都是**行为级**的：真跑 `main()` / `_watch_once()`，
   不去读源码文本确认「有没有 try/except」——
   那类断言改坏行为也照样绿（把 except 挪个位置就假绿）。
"""
from __future__ import annotations

import json
import sys
import time as _time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _pack(notes):
    return {"format": "microbench-tablet-pack", "notes": notes}


def _write_pack(directory: Path, name: str, rel: str) -> Path:
    p = directory / name
    p.write_text(json.dumps(
        _pack([{"id": rel, "body": "平板改的内容"}]), ensure_ascii=False),
        encoding="utf-8")
    return p


class _FakeTime:
    """只换掉 `sleep`：让 watch 循环跑够 N 轮就以 KeyboardInterrupt 收尾。

    ⚠ 只替换 `microbench.merge_changes` 模块里的那个引用 ——
      直接 patch 全局 `time.sleep` 会连 pytest 自己的等待一起改掉。
    """

    def __init__(self, real, on_sleep):
        self._real = real
        self._on_sleep = on_sleep

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, seconds):
        self._on_sleep(seconds)


# ---------------------------------------------------------------------------
# ① 进程级：守护循环不许被一个包的 OSError 打死
# ---------------------------------------------------------------------------

def test_watch_loop_survives_a_pack_that_fails_to_merge(tmp_path, monkeypatch):
    """一个包合并失败 → 循环继续、进程不退出、失败的包下一轮自动重试。"""
    from microbench import merge_changes as mc

    src = tmp_path / "packs"
    src.mkdir()
    _write_pack(src, "a_bad.json", "01_Literature/a.md")
    _write_pack(src, "b_good.json", "01_Literature/b.md")
    vault = tmp_path / "vault"
    vault.mkdir()

    calls: list[str] = []

    def fake_merge(path, vault_arg, dry_run=False):
        calls.append(path.name)
        if path.name == "a_bad.json":
            # 真机上就是这种：目标笔记被 Obsidian / 杀软占着
            raise PermissionError(32, "另一个程序正在使用此文件")
        return True

    monkeypatch.setattr(mc, "merge_path", fake_merge)

    rounds = {"n": 0}

    def on_sleep(_seconds):
        rounds["n"] += 1
        if rounds["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(mc, "time", _FakeTime(_time, on_sleep))
    monkeypatch.setattr(sys, "argv", [
        "merge_changes", str(src), "--vault", str(vault), "--watch", "1"])

    rc = mc.main()

    assert rc == 0, "watch 守护循环被单个包的 OSError 打断了 —— 进程退出了"
    assert calls.count("b_good.json") == 1, \
        "合并成功的包不该被反复合并（seen 记账坏了）"
    assert calls.count("a_bad.json") >= 2, \
        "失败的包必须下一轮自动重试 —— 否则这个变更包永远合并不进来"


# ---------------------------------------------------------------------------
# ② 轮次级：失败不写 seen（否则永远不重试）
# ---------------------------------------------------------------------------

def test_failed_pack_is_not_marked_as_seen(tmp_path, monkeypatch, capsys):
    """失败的包**不算「处理过」** → 下一轮还会再试。"""
    from microbench import merge_changes as mc

    src = tmp_path / "packs"
    src.mkdir()
    bad = _write_pack(src, "a_bad.json", "01_Literature/a.md")
    good = _write_pack(src, "b_good.json", "01_Literature/b.md")
    vault = tmp_path / "vault"
    vault.mkdir()

    def fake_merge(path, vault_arg, dry_run=False):
        if path.name == "a_bad.json":
            raise PermissionError(32, "另一个程序正在使用此文件")
        return True

    monkeypatch.setattr(mc, "merge_path", fake_merge)

    seen: set = set()
    mc._watch_once(src, vault, seen, False)

    seen_paths = {k[0] for k in seen}
    assert str(good) in seen_paths, "合并成功的包要记进 seen"
    assert str(bad) not in seen_paths, \
        "失败的包被记成「处理过」→ 下一轮就跳过了，这个变更包永远合并不进来"

    err = capsys.readouterr().err
    assert "失败" in err, "失败必须说给用户听（stderr），不能静默：\n" + err


# ---------------------------------------------------------------------------
# ③ 目录级：包目录本身读不出来也不许退出
# ---------------------------------------------------------------------------

def test_watch_survives_when_the_pack_directory_cannot_be_listed(tmp_path, monkeypatch):
    """坚果云同步时目录被换掉 → `glob` 抛 OSError → 本轮跳过，进程继续。"""
    from microbench import merge_changes as mc

    real_dir = tmp_path / "packs"
    real_dir.mkdir()

    class _BrokenDir:
        """路径本身存在，但列目录会抛 OSError（同步客户端正在替换它）。"""

        def __init__(self, *a, **k):
            self._real = Path(*a, **k)

        def glob(self, *a, **k):
            raise PermissionError(32, "另一个程序正在使用此文件")

        def __getattr__(self, name):
            return getattr(self._real, name)

    seen: set = set()
    mc._watch_once(_BrokenDir(real_dir), tmp_path, seen, False)   # 不该抛
    assert not seen


# ---------------------------------------------------------------------------
# ④ 反向：真能合并的包，watch 一轮就该合进去（别把功能改坏了）
# ---------------------------------------------------------------------------

def test_watch_still_merges_a_good_pack(tmp_path, monkeypatch):
    """反向用例：修「不许退出」不能把「正常合并」也修没了。"""
    from microbench import merge_changes as mc

    src = tmp_path / "packs"
    src.mkdir()
    _write_pack(src, "good.json", "01_Literature/note.md")
    vault = tmp_path / "vault"
    vault.mkdir()

    seen: set = set()
    mc._watch_once(src, vault, seen, False)

    note = vault / "01_Literature" / "note.md"
    assert note.exists(), "watch 一轮没把变更包合进 vault"
    assert "平板改的内容" in note.read_text(encoding="utf-8")
    assert seen, "合并成功后要记账（下一轮才不会再合一次）"


# ---------------------------------------------------------------------------
# ⑤ 进程级：真起一个 watch 守护进程，撞上**真的** OSError，看它死不死
# ---------------------------------------------------------------------------

def test_watch_process_survives_a_real_oserror(tmp_path):
    """真起进程 + 真 OSError（**不 monkeypatch**）。

    怎么造一个**真**的 OSError：把 vault 里的 `01_Literature` 做成一个**文件**
    → `_atomic_write_text` 里的 `path.parent.mkdir()` 撞上同名文件
    → `FileExistsError`（OSError）—— 跟真机上「笔记被 Obsidian / 杀软占用」
    是同一族错误（都是写路径上的 OSError）。

    判据是**进程还活着**（`poll() is None`），不是「打印了某句话」。
    """
    import subprocess
    import time

    from microbench import merge_changes as mc

    src = tmp_path / "packs"
    src.mkdir()
    _write_pack(src, "pack.json", "01_Literature/note.md")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "01_Literature").write_text("我不是目录", encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "microbench.merge_changes", str(src),
         "--vault", str(vault), "--watch", "1"],
        cwd=str(REPO),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace")
    try:
        time.sleep(5)
        assert proc.poll() is None, (
            "watch 守护进程被一个真实的 OSError 打断了 —— 退出码 %s。"
            "后面的变更包从此再也合并不进来（要人工重启才恢复）。" % proc.poll())
    finally:
        proc.terminate()
        try:
            _out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
    assert "失败" in (err or ""), (
        "失败要说给用户听（stderr），不能静默吞掉：\n" + (err or ""))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
