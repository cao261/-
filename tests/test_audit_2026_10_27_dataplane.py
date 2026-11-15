# -*- coding: utf-8 -*-
"""2026-10-27 审计 · 数据面护栏（pdf_cache / merge_changes / auto_sync）。

这份文件钉死的是**会真丢用户数据**的三类缺陷：

| 编号 | 缺陷 | 后果 |
|---|---|---|
| C-2 | `pdf_cache` 缓存键缺「词典路径」 | 「没有词典」跟「词典文件不存在」撞键 → 词典维度静默失效 → **译文跟原文对不上** |
| C-1/H-5 | `auto_sync` 快进合并后 `git merge --abort` 无效 | 回退是假的 → 坏代码留在 HEAD → **下一轮直接被 push 给对方** |
| H-1 | `merge_changes` 空 body 清空真笔记 | 平板一次空提交 → **电脑端笔记正文归零**（还经坚果云同步到另一台） |
| H-2 | `pdf_cache` Windows 写入失败 | 缓存写不进去 + 读侧误判未命中 → 用户每次都等 2.7~9.7 秒重抽 |
| H-3 | `pdf_cache` 不校验快照结构 | `pages=[123]` 读出来 → 下游 `p["text"]` 抛 TypeError → 端点 500 |
| L-5 | `pdf_cache` 孤儿文件堆积 | 29 个缓存文件 vs 27 篇 PDF，2.58MB 没人清 |

⚠ **绝不能碰用户的真实 vault / 真实坚果云同步目录** ——
本文件里的一切（PDF、词典、vault、git 仓）都在 `tmp_path` 里造。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from microbench import auto_sync
from microbench import pdf_cache
from microbench.merge_changes import djb2_base36, merge_pack


# ===========================================================================
# ① C-2（2026-10-28 修正）：缓存键必须是**单一真相**
#
# ⚠ 这组护栏的**判据在 2026-10-28 合并对方设备（25353）的第二轮审计时改了**。
#   原判据是「词典的 (mtime,size) + 路径必须进键」，依据是
#   「词典认出了新符号 → 文本里 ⟦G:n⟧ 对应的真实字符会变 → 必须重解」。
#
#   **那个前提被代码事实否掉了**：
#     `paper_text.extract_text_from_pdf(pdf_bytes)` 的签名里**没有字典参数**，
#     整个 paper_text 模块**不 import 也不读**符号词典。
#     文本里的 `⟦G:<下标>⟧` 是**页内 glyphs 列表的下标**，不是词典序号；
#     把标记换成真实字符是后面 `_resolve_document_glyphs()` 干的事，
#     那时缓存**早写完了**。
#   → 缓存里存的永远是**抽取原始输出**，词典维度对它**没有任何影响**。
#
#   而「加词典路径维度来防撞键」是**在给一个不存在的问题打补丁**，代价更严重：
#     不同调用点用不同参数算出**不同的键** → 一份写一份读
#     → 缓存**永远命中不了** → 842x 加速完全失效
#     → 用户每次都等 2.7~9.7 秒重抽（**比撞键更糟**）。
#
#   → 正确解法：**所有调用点用同一个签名**（单一真相），
#     再用 `EXTRACT_IMPL_REV` 覆盖「抽取实现变了」这一真正需要失效的场景。
# ===========================================================================

def test_cache_key_takes_no_dictionary_argument():
    """缓存键**不许**再接受词典参数。

    只要它还能接字典参数，就说明「词典维度」这条错误路线没被彻底拔掉 ——
    调用点一旦有人传了字典，读写两侧的键就可能不一致。
    """
    import inspect

    params = list(inspect.signature(pdf_cache.cache_key).parameters)
    assert params == ["pdf_path"], (
        f"cache_key 的参数是 {params} —— 词典维度必须彻底移除"
        "（抽取层不读词典，词典维度只会让读写两侧算出不同的键 → 缓存永不命中）"
    )


def test_cache_read_and_write_use_the_same_key():
    """**写进去的必须能被读出来** —— 读写两侧必须用同一个键。

    这是「单一真相」的行为级断言：写侧 `cache_key(pdf)`，
    读侧也必须能命中同一份快照。
    之前若读侧多传一个字典参数 → 键不同 → 这条会红。
    """
    import inspect

    from microbench import app as app_mod

    # app.py 里**所有** pdf_cache 调用点都必须是单参数形式
    src = inspect.getsource(app_mod)
    import re as _re
    for m in _re.finditer(r"pdf_cache\.(?:read_cache|write_cache|cache_key)\(([^)]*)\)", src):
        args = m.group(1)
        assert "GLYPH_DICT" not in args and "dict" not in args.lower(), (
            f"app.py 里有个 pdf_cache 调用点传了词典参数：{m.group(0)}"
            " → 读写两侧的键会不一致 → 缓存永远命中不了"
        )


def test_cache_key_ignores_the_glyph_dictionary_entirely(tmp_path):
    """词典怎么变，缓存键都**不该**变。

    这个缓存存的是 `extract_text_from_pdf()` 的**原始输出**，
    而抽取层全程不读词典（见 `tests/test_pdf_cache.py` 里那条
    「抽取层不依赖词典」的前置断言）。

    原来的键里带了词典的 (mtime, size)，后果是：后台认字每认出一个新符号
    就保存一次词典 → 签名变 → **全库 27 篇的缓存一起失效** →
    下次打开每篇都要重抽 2.7 秒（最慢 9.7 秒）。
    「认字越勤，打开越慢」正是这个缓存要解决的问题本身。
    """
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    d = tmp_path / "dict.json"
    d.write_text("{}", encoding="utf-8")
    k1 = pdf_cache.cache_key(pdf)

    d.write_text('{"k": "v"}', encoding="utf-8")   # 词典变了
    k2 = pdf_cache.cache_key(pdf)
    assert k1 == k2, (
        "词典变化让缓存键变了 → 后台每认出一个符号就废掉全库缓存，"
        "用户打开论文会莫名变慢"
    )


# ===========================================================================
# ② H-3：快照结构必须校验（不合格 → 当未命中，绝不返回坏结构）
# ===========================================================================

def _snapshot(cache_dir: Path, key: str, payload: dict) -> Path:
    """写一份快照。

    ⚠ **两种文件名都写**：
      `<key>.json`        —— 旧版（固定文件名）的布局
      `<key>.deadbeef.json` —— 新版（唯一文件名）的布局
    只写新布局的话，旧版根本找不到文件 → 「读出来是 None」→
    护栏在旧版上是绿的（假护栏）。两种都写，旧版才会真的读到那份坏结构。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False)
    for name in ("%s.json" % key, "%s.deadbeef.json" % key):
        (cache_dir / name).write_text(text, encoding="utf-8")
    return cache_dir / ("%s.deadbeef.json" % key)


def test_read_cache_rejects_pages_that_are_not_dicts(tmp_path, monkeypatch):
    """`pages=[123]` → 必须降级为未命中。

    旧版只验「是不是非空 list」→ 读出来 `[123]` → 下游 `p["text"]`
    抛 TypeError → 端点 500，而不是「重抽一次」。
    """
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    key = pdf_cache.cache_key(pdf)
    _snapshot(cache_dir, key, {"key": key, "pages": [123]})
    assert pdf_cache.read_cache(pdf) is None, \
        "结构不合法的快照没有被当成未命中 → 下游会抛 TypeError"


def test_read_cache_accepts_a_well_formed_snapshot(tmp_path, monkeypatch):
    """合法结构必须能正常读出来（别把好缓存也毙了）。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    key = pdf_cache.cache_key(pdf)
    pages = [{"page": 1, "text": "x", "glyphs": []}]
    _snapshot(cache_dir, key, {"key": key, "pages": pages})
    assert pdf_cache.read_cache(pdf) == pages


def test_read_cache_rejects_a_snapshot_whose_text_is_not_a_string(tmp_path, monkeypatch):
    """`text` 不是字符串 → 也是坏结构 → 当未命中。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    key = pdf_cache.cache_key(pdf)
    _snapshot(cache_dir, key, {"key": key, "pages": [{"page": 1, "text": None}]})
    assert pdf_cache.read_cache(pdf) is None


# ===========================================================================
# ③ H-2：Windows 上并发写 / 读中写都不能失败
# ===========================================================================

def test_concurrent_writes_to_the_same_key_never_fail(tmp_path, monkeypatch, capsys):
    """6 个线程同时写同一个 key → 不能有任何一个写失败。

    Windows 的 `os.replace`（ReplaceFile）对**已被打开读**的目标文件返回
    ERROR_ACCESS_DENIED；并发 replace 同一个目标同样撞。
    → 旧版实测 32 次 `PermissionError(13, '拒绝访问。')`，缓存根本写不进去。
    """
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    key = pdf_cache.cache_key(pdf)
    pages = [{"page": 1, "text": "hello", "glyphs": []}]

    errors = []

    def writer():
        try:
            for _ in range(25):
                pdf_cache.write_cache(pdf, pages, key)
        except Exception as exc:      # write_cache 本该自己吞掉异常
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, "并发写抛了异常: %r" % (errors[:3],)
    err = capsys.readouterr().err
    assert "写缓存失败" not in err, "有缓存写失败了（Windows 的 replace 撞了）:\n%s" % err[-800:]
    got = pdf_cache.read_cache(pdf)
    assert got == pages, "并发写之后缓存读不出来"


def test_reading_while_writing_never_raises(tmp_path, monkeypatch, capsys):
    """一线程持续读 + 一线程持续写 → 读侧永远不抛异常，写侧永远不失败。

    旧版实测：2 秒内 **782 次** `PermissionError`；读侧还把写失败
    当成「未命中」→ 用户每次都等 2.7~9.7 秒重新抽。
    """
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    key = pdf_cache.cache_key(pdf)
    pages = [{"page": 1, "text": "hello", "glyphs": []}]
    pdf_cache.write_cache(pdf, pages, key)

    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                pdf_cache.read_cache(pdf)
        except Exception as exc:
            errors.append(exc)

    def writer():
        try:
            for _ in range(60):
                pdf_cache.write_cache(pdf, pages, key)
        except Exception as exc:
            errors.append(exc)

    r = threading.Thread(target=reader)
    w = threading.Thread(target=writer)
    r.start()
    w.start()
    w.join()
    stop.set()
    r.join()

    assert not errors, "读中写抛了异常: %r" % (errors[:3],)
    err = capsys.readouterr().err
    assert "写缓存失败" not in err, "读中写时缓存写失败了:\n%s" % err[-800:]
    assert pdf_cache.read_cache(pdf) == pages


# ===========================================================================
# ④ L-5：孤儿文件必须有人清，而且**不能**在读路径上清
# ===========================================================================

def _old_file(path: Path, days: int = 90) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    t = time.time() - days * 86400
    os.utime(path, (t, t))
    return path


def test_prune_cache_removes_expired_snapshots(tmp_path, monkeypatch):
    """过期（TTL）的快照要被清掉，活的要留下。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    stale = _old_file(cache_dir / ("a" * 64 + ".aaaa.json"))
    fresh = cache_dir / ("b" * 64 + ".bbbb.json")
    fresh.write_text("{}", encoding="utf-8")

    n = pdf_cache.prune_cache(max_files=100, ttl_seconds=14 * 24 * 3600)
    assert n == 1
    assert not stale.exists(), "过期快照没清掉"
    assert fresh.exists(), "还在有效期内的快照被误删了"


def test_prune_cache_keeps_the_directory_bounded(tmp_path, monkeypatch):
    """文件数超上限 → 按 LRU（留最新的）清掉多余的。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for i in range(10):
        f = cache_dir / ("%064x.%02x.json" % (i, i))
        f.write_text("{}", encoding="utf-8")
        t = time.time() - (10 - i)          # i 越大越新
        os.utime(f, (t, t))

    pdf_cache.prune_cache(max_files=3, ttl_seconds=10 ** 9)
    left = sorted(cache_dir.glob("*.json"))
    assert len(left) == 3, "缓存目录没有被限制在上限内: %d" % len(left)
    assert (cache_dir / ("%064x.%02x.json" % (9, 9))).exists(), "最新的那个被误删了"


def test_writing_the_cache_prunes_orphans(tmp_path, monkeypatch):
    """写路径就是 orphan 的清理调用点（读路径不做清理）。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    monkeypatch.setattr(pdf_cache, "_last_prune_at", 0.0)   # 让节流放行
    orphan = _old_file(cache_dir / ("c" * 64 + ".cccc.json"))

    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    key = pdf_cache.cache_key(pdf)
    pdf_cache.write_cache(pdf, [{"page": 1, "text": "x", "glyphs": []}], key)
    assert not orphan.exists(), "孤儿文件没人清（L-5）"


def test_the_read_path_never_prunes(tmp_path, monkeypatch):
    """读路径**不许**做清理（读必须快）。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    orphan = _old_file(cache_dir / ("d" * 64 + ".dddd.json"))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    pdf_cache.read_cache(pdf)
    assert orphan.exists(), "读路径上做了清理 → 读会变慢"


def test_clear_cache_removes_every_snapshot_file(tmp_path, monkeypatch):
    """clear_cache 要把快照（含崩溃残留的 .tmp）全清掉。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / ("e" * 64 + ".eeee.json")).write_text("{}", encoding="utf-8")
    (cache_dir / "leftover.tmp").write_text("{}", encoding="utf-8")
    n = pdf_cache.clear_cache()
    assert n == 2
    assert list(cache_dir.iterdir()) == []


# ===========================================================================
# ⑤ H-1：空正文绝不能清空 vault 里的真笔记
# ===========================================================================

def _vault_with_note(tmp_path: Path) -> tuple[Path, Path, str]:
    vault = tmp_path / "vault"
    (vault / "01_Literature").mkdir(parents=True)
    target = vault / "01_Literature" / "real.md"
    original = "# real note\nimportant\n"
    target.write_text(original, encoding="utf-8")
    return vault, target, original


def test_empty_body_does_not_erase_a_real_note(tmp_path):
    """包里 body="" + baseHash 命中 → **一个字都不能写**。

    旧版：`current != body` → `target.write_text("")` → 原文全丢，
    没有冲突副本、没有备份；该文件在 vault 里 → 经坚果云同步到另一台
    → **两台设备正文一起丢**。
    """
    vault, target, original = _vault_with_note(tmp_path)
    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [{
            "id": "01_Literature/real.md", "title": "real",
            "group": "01_Literature", "body": "",
            "baseHash": djb2_base36(original),
        }],
    }
    s = merge_pack(pack, vault)
    assert target.read_text(encoding="utf-8") == original, "空正文把真笔记清空了！"
    assert "01_Literature/real.md" in s["rejected"], \
        "空正文必须进 rejected，实际: %r" % (s,)
    assert "01_Literature/real.md" not in s["written"]
    assert "01_Literature/real.md" not in s["conflicts"]


def test_whitespace_only_body_is_also_rejected(tmp_path):
    """只有空白的正文同样不许写。"""
    vault, target, original = _vault_with_note(tmp_path)
    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [{
            "id": "01_Literature/real.md", "title": "real",
            "group": "01_Literature", "body": "   \n\t\n",
            "baseHash": djb2_base36(original),
        }],
    }
    s = merge_pack(pack, vault)
    assert target.read_text(encoding="utf-8") == original
    assert "01_Literature/real.md" in s["rejected"]


def test_a_normal_body_still_writes(tmp_path):
    """回归：正常（非空）正文的写入行为**完全不变**。"""
    vault, target, original = _vault_with_note(tmp_path)
    body = "# real note\n平板改过的内容\n"
    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [{
            "id": "01_Literature/real.md", "title": "real",
            "group": "01_Literature", "body": body,
            "baseHash": djb2_base36(original),
        }],
    }
    s = merge_pack(pack, vault)
    assert target.read_text(encoding="utf-8") == body
    assert s["written"] == ["01_Literature/real.md"]


def test_a_conflicting_body_still_saves_a_merged_copy(tmp_path):
    """回归：两边都改 → 仍然另存合并稿（原文件不动）。"""
    vault, target, _original = _vault_with_note(tmp_path)
    pack = {
        "format": "microbench-tablet-pack", "version": 1,
        "notes": [{
            "id": "01_Literature/real.md", "title": "real",
            "group": "01_Literature", "body": "平板版本\n",
            "baseHash": djb2_base36("完全不同的旧内容\n"),
        }],
    }
    s = merge_pack(pack, vault)
    assert s["conflicts"] == ["01_Literature/real.md"]
    assert target.read_text(encoding="utf-8") == "# real note\nimportant\n"
    merged = list((vault / "01_Literature").glob("*_冲突合并_*.md"))
    assert len(merged) == 1


# ===========================================================================
# ⑥ C-1 + H-5：auto_sync 快进合并失败后必须**真的**回退
# ===========================================================================

def _git(repo, *args, check=True):
    p = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120,
    )
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if check and p.returncode != 0:
        raise AssertionError("git %s 失败:\n%s" % (" ".join(args), out))
    return p.returncode, out


@pytest.fixture()
def three_repos(tmp_path):
    """upstream(裸) / work(本机) / other(对方) —— 三个**真** git 仓。

    复现的就是 auto_sync 的真实场景：对方 push 一个坏提交 → 本机快进合并
    → 测试红 → 必须回退到合并前。
    """
    upstream = tmp_path / "upstream.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"
    other = tmp_path / "other"

    _git(tmp_path, "init", "--bare", "-b", "main", str(upstream))
    _git(tmp_path, "init", "-b", "main", str(seed))
    for r in (seed,):
        _git(r, "config", "user.email", "audit@example.com")
        _git(r, "config", "user.name", "audit")
    (seed / ".gitignore").write_text("logs/\n", encoding="utf-8")
    (seed / "app.py").write_text("v1-good\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "v1-good")
    _git(seed, "remote", "add", "origin", str(upstream))
    _git(seed, "push", "-u", "origin", "main")

    _git(tmp_path, "clone", str(upstream), str(work))
    _git(tmp_path, "clone", str(upstream), str(other))
    for r in (work, other):
        _git(r, "config", "user.email", "audit@example.com")
        _git(r, "config", "user.name", "audit")
    return {"upstream": upstream, "seed": seed, "work": work, "other": other}


def _other_pushes_a_broken_commit(other: Path) -> str:
    (other / "app.py").write_text("v2-BROKEN\n", encoding="utf-8")
    _git(other, "add", "app.py")
    _git(other, "commit", "-m", "v2-BROKEN")
    _git(other, "push", "origin", "main")
    return _git(other, "rev-parse", "HEAD")[1]


def _run_auto_sync(work: Path, monkeypatch, tests_fail=True):
    """在 work 仓上跑一轮 auto_sync（测试判红，不推）。"""
    monkeypatch.setattr(auto_sync, "REPO", work)
    if tests_fail:
        monkeypatch.setattr(auto_sync, "run_tests",
                            lambda: (False, ["pytest 失败: 故意红的用例"]))
    monkeypatch.setattr(sys, "argv", ["auto_sync.py"])
    auto_sync.main()
    return auto_sync.LAST_RESULT


def test_fast_forward_merge_is_really_rolled_back_when_tests_fail(three_repos, monkeypatch):
    """快进合并 + 测试红 → 工作区必须**真的**回到合并前的提交。

    旧版：`git merge --abort` 在快进合并下 rc=128（没有 MERGE_HEAD）→
    回退无效 → HEAD 停在坏提交上，但报告却说「已回退合并（没有推）」→
    下一轮 `workspace_clean()` 判定「干净」→ **坏代码被 push 给对方**。
    """
    work = three_repos["work"]
    other = three_repos["other"]

    bad_sha = _other_pushes_a_broken_commit(other)
    pre_sha = _git(work, "rev-parse", "HEAD")[1]
    assert pre_sha != bad_sha

    result = _run_auto_sync(work, monkeypatch)

    # ① 工作区回到合并前的 SHA
    assert _git(work, "rev-parse", "HEAD")[1] == pre_sha, \
        "快进合并后没有回退到合并前的提交"
    # ② 文件内容不是坏版本
    assert (work / "app.py").read_text(encoding="utf-8") == "v1-good\n", \
        "工作区里还留着坏版本的内容"
    # ③ 坏提交不在本机历史里（否则下一轮会被当成「干净」推出去）
    rc, _ = _git(work, "merge-base", "--is-ancestor", bad_sha, "HEAD", check=False)
    assert rc != 0, "坏提交还在本机历史里 → 下一轮会被 push 给对方"
    # ④ 工作区干净
    assert _git(work, "status", "--porcelain")[1] == "", "回退后工作区不干净"
    # ⑤ 报告不许撒谎：说「已回退」就必须给出回退到的提交
    assert result["action"] == "tests_failed"
    msg = "\n".join(result["messages"])
    if "已回退" in msg:
        assert pre_sha[:7] in msg, \
            "报告说「已回退」却没给出回退到的提交（报告在撒谎）: %s" % msg
    assert "没有推" in msg or "没推" in msg


def test_auto_sync_never_claims_a_rollback_it_could_not_perform(three_repos, monkeypatch):
    """回退**失败**时，报告里不许出现「已回退」。"""
    work = three_repos["work"]
    other = three_repos["other"]
    _other_pushes_a_broken_commit(other)

    real_git = auto_sync.git

    def fake_git(*args, **kw):
        if args and (args[0] == "reset"
                     or (args[0] == "merge" and "--abort" in args)):
            return 1, "fatal: 模拟的回退失败"
        return real_git(*args, **kw)

    monkeypatch.setattr(auto_sync, "git", fake_git)
    result = _run_auto_sync(work, monkeypatch)

    msg = "\n".join(result["messages"])
    assert "已回退" not in msg, "回退失败了却报告「已回退」: %s" % msg
    assert "回退失败" in msg, "回退失败必须明确报错: %s" % msg
    assert result.get("rollback_ok") is False


def test_a_merge_conflict_is_also_rolled_back(three_repos, monkeypatch):
    """回归：真冲突时 `git merge --abort` 是对的，回退照样要生效。"""
    work = three_repos["work"]
    other = three_repos["other"]

    # 本机先改一行并提交（本地有提交 → 合并会冲突）
    (work / "app.py").write_text("v1-good\n本机改动\n", encoding="utf-8")
    _git(work, "add", "app.py")
    _git(work, "commit", "-m", "本机改动")
    pre_sha = _git(work, "rev-parse", "HEAD")[1]
    _other_pushes_a_broken_commit(other)

    result = _run_auto_sync(work, monkeypatch)
    assert result["action"] == "conflict", "合并冲突没被识别: %r" % (result,)
    assert _git(work, "rev-parse", "HEAD")[1] == pre_sha, "冲突后没有回退"
    assert _git(work, "status", "--porcelain")[1] == "", "冲突回退后工作区不干净"
