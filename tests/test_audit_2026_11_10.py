# -*- coding: utf-8 -*-
"""第五轮审计（2026-11-10）第 1 批修复的**行为级**回归护栏。

这一批修的是上一轮自己留下的三个欠账（见 docs/AUDIT_2026-11-10.md）：

  * F4 快照保存的「读旧值 → 合并 → 写回」没有锁 → 两个写者读到同一个
    旧基线，后写的把先写的页整份盖掉，而两个接口都返回 success。
    上一轮 P1-1 的修法写的是「隔离读 + 按 paper_id 分桶加锁」，
    只做了前半截。

  * F2 「缺 KaTeX 拒绝导出」那条护栏是**环境依赖**的：它只断言返回值
    False，而 export_tablet 在「vault 里一篇笔记都没有」时也返回 False
    → 断言被另一条路径满足，把检查整个删掉它照样绿。

  * F3 桌面离线导出 export_once 的 KaTeX 拒绝**完全没有护栏**，
    而且那个检查还排在 render_formulas() 之后（用 KaTeX 渲染公式那一步
    已经跑过了才拒绝，是马后枪）。

本文件同样不写 `assert "xxx" in 源码` —— 那类护栏改坏行为也照样绿。
每条都跑真实函数、断言可区分的副作用，并尽量带反向用例：

  * 去掉快照锁        → test_concurrent_snapshot_save_and_sync_keep_all_pages 会红
  * 锁只包写盘不包合并 → test_snapshot_save_holds_lock_across_merge 会红
  * 去掉 KaTeX 检查   → test_export_once_* 会红
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. F4：快照的「读-合-写」必须按 paper_id 串行
# ---------------------------------------------------------------------------

def _snap_payload(paper: str, page: int = 1, translation: str = "译文"):
    """一份合法的快照保存请求体。"""
    return {
        "paper_id": paper,
        "paper_title": "并发测试论文",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 1.0,
        "pages": [{
            "page_num": page,
            "text_hash": f"{page:064d}",
            "translation": translation,
            "model": "m",
            "translated_at": "2026-11-10T12:00:00",
        }],
    }


def _snapshot_file(paper: str, pages: dict[int, str], version: int = 5) -> dict:
    return {
        "schema_version": 2,
        "extractor_version": version,
        "paper_id": paper,
        "paper_title": "并发测试论文",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 1.0,
        "paper_text_signature": "",
        "pages": [{
            "page_num": pn,
            "text_hash": f"{pn:064d}",
            "translation": txt,
            "model": "m",
            "translated_at": "2026-11-10T12:00:00",
        } for pn, txt in sorted(pages.items())],
        "saved_at": "2026-11-10T12:00:00",
    }


def test_snapshot_save_holds_lock_across_merge(client, sandbox, monkeypatch):
    """锁必须覆盖「读旧值 → 合并 → 写回」整段，不能只锁最后的写盘。

    怎么测才不吃概率：`_snapshot_lock` 是**非重入**锁 —— 如果当前线程
    已经持有它，再 acquire(blocking=False) 一定失败。所以只要在「合并」
    这一步探一下，就能确定锁当时到底被谁拿着。
    """
    from microbench import app

    paper = "lock_scope_paper"
    real_merge = app._merge_snapshot_pages
    seen: dict = {}

    def probe_merge(existing, req, incoming):
        lock = app._snapshot_lock(paper)
        got = lock.acquire(blocking=False)
        if got:
            # 没被持有 —— 探针自己拿到了，立刻还回去，别污染状态
            lock.release()
        seen["held_during_merge"] = not got
        return real_merge(existing, req, incoming)

    monkeypatch.setattr(app, "_merge_snapshot_pages", probe_merge)
    r = client.post("/api/pdf/snapshot/save", json=_snap_payload(paper, 1))
    assert r.status_code == 200, r.text
    assert seen.get("held_during_merge") is True, (
        "合并发生时锁没被当前线程持有 —— 锁只包了写盘，"
        "「读旧值 → 合并」之间仍有竞态窗口")


def test_concurrent_snapshot_save_and_sync_keep_all_pages(client, sandbox, monkeypatch):
    """快照保存端点与同步端点并发时，**两边的页都必须活下来**。

    这是 F4 的正题：两个写者写的是同一个文件
    （logs/_pdf_snapshots/<paper_id>.json）：
      * 保存端点是 async → 跑在事件循环线程；
      * /api/sync 是 def  → 被 FastAPI 丢到线程池线程。
    没有锁时，两者会读到同一个旧基线，后写的把先写的页整份盖掉，
    而两个接口都返回 success —— 用户只看到「保存成功」，翻过的页没了。

    ⚠ 怎么让重叠**确定**发生（第一版这条护栏是假护栏，撤掉锁照样绿）：
      两个请求都进同一个事件循环，async 端点的临界区又没有 await，
      于是谁先被事件循环 picked up，谁就整个跑完 —— 另一个根本没插进来。
    现在用事件把顺序钉死：
      ① 同步请求先发；它的合并函数**读完 dst 之后**置位事件；
      ② 保存请求等到事件置位才发 → 保证保存端的「读旧值」发生在
         同步端的「写回」之前 → 两边读到的是同一个旧基线；
      ③ 合并里各睡 0.3 秒，把「读完了但还没写」的窗口撑开。

    有锁时：同步端点先持锁 → 保存端点阻塞在锁上 → 等同步写完再读 →
    两页都在。这条**永远绿**，不存在 flaky。
    """
    from microbench import app
    from microbench.paper_text import EXTRACTOR_VERSION

    paper = "concurrent_snapshot_paper"

    # 共享目录里放一份「另一台设备推过来的」快照（第 2 页）
    shared_snap_dir = app._sync_dir("snapshots")
    (shared_snap_dir / f"{paper}.json").write_text(
        json.dumps(_snapshot_file(paper, {2: "对方的译文"}, EXTRACTOR_VERSION),
                   ensure_ascii=False),
        encoding="utf-8")

    real_pages = app._merge_snapshot_pages
    real_files = app._merge_snapshot_files
    sync_has_read = threading.Event()

    def slow_merge_pages(existing, req, incoming):
        # 调用方已经读完旧值了，这里撑开「读完了但还没写」的窗口
        time.sleep(0.30)
        return real_pages(existing, req, incoming)

    def slow_merge_files(dst, src):
        # real_merge_files 内部已经读完 dst/src —— 读完这一刻通知保存端点
        out = real_files(dst, src)
        sync_has_read.set()
        time.sleep(0.30)
        return out

    monkeypatch.setattr(app, "_merge_snapshot_pages", slow_merge_pages)
    monkeypatch.setattr(app, "_merge_snapshot_files", slow_merge_files)

    def pull_from_shared():
        return client.post("/api/sync",
                           json={"pull_only": True, "include_files": False})

    def save_page_1():
        assert sync_has_read.wait(timeout=15), "同步线程没走到读那一步"
        return client.post("/api/pdf/snapshot/save",
                           json=_snap_payload(paper, 1))

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_sync = ex.submit(pull_from_shared)      # 先发
        f_save = ex.submit(save_page_1)           # 等同步读完再发
        r_sync, r_save = f_sync.result(), f_save.result()

    assert r_sync.status_code == 200, r_sync.text
    assert r_save.status_code == 200, r_save.text

    data = app._safe_read_json(app._snapshot_path(paper))
    assert data, "快照文件不见了"
    pages = sorted(p["page_num"] for p in (data.get("pages") or []))
    assert pages == [1, 2], (
        f"并发写丢了页 —— 现在只有 {pages}。"
        "「读旧值 → 合并 → 写回」必须按 paper_id 串行（F4）")


# ---------------------------------------------------------------------------
# 2. F3：桌面离线导出 export_once 的 KaTeX 拒绝
# ---------------------------------------------------------------------------

def _tiny_vault(tmp_path: Path) -> Path:
    """造一个最小 vault：有一篇笔记，导出「本该成功」。"""
    vault = tmp_path / "vault"
    (vault / "01_Literature").mkdir(parents=True)
    (vault / "01_Literature" / "probe.md").write_text(
        "# 探针笔记\n\n正文。\n", encoding="utf-8")
    return vault


def test_export_once_refuses_missing_katex_and_keeps_previous(tmp_path, monkeypatch,
                                                               capsys):
    """export_once 缺 KaTeX 时必须拒绝导出，且不能覆盖已有产物。

    上一轮给 export_once 也加了拒绝逻辑，但**一条测试都没有**（F3）。
    """
    from microbench import export_offline as eo

    monkeypatch.setattr(eo, "REPO", _tiny_vault(tmp_path))
    monkeypatch.setattr(eo, "KATEX_DIST", tmp_path / "empty_katex")

    out = tmp_path / "reader.html"
    out.write_text("PREVIOUS GOOD", encoding="utf-8")
    assert eo.export_once(out, quiet=True) is False
    err = capsys.readouterr().err
    assert "缺少 KaTeX" in err, (
        "不是因缺 KaTeX 拒绝的 —— 断言被别的返回路径满足了：\n" + err)
    assert out.read_text(encoding="utf-8") == "PREVIOUS GOOD", "已有产物被覆盖了"


def test_export_once_checks_katex_before_rendering_formulas(tmp_path, monkeypatch):
    """资源检查必须排在 render_formulas() **之前**。

    原来的检查排在最后 —— 而 render_formulas 正是「用 KaTeX 渲染公式」
    那一步，缺资源时它已经跑过了（要么失败、要么留下未渲染的残骸），
    再拒绝导出已经是马后枪。
    """
    from microbench import export_offline as eo

    monkeypatch.setattr(eo, "REPO", _tiny_vault(tmp_path))
    monkeypatch.setattr(eo, "KATEX_DIST", tmp_path / "empty_katex")

    called: list = []
    monkeypatch.setattr(eo, "render_formulas",
                        lambda *a, **k: called.append(1) or {"_per_occurrence": []})

    out = tmp_path / "reader.html"
    out.write_text("PREVIOUS GOOD", encoding="utf-8")
    assert eo.export_once(out, quiet=True) is False
    assert not called, "缺 KaTeX 时竟然已经跑去渲染公式了 —— 检查位置不对"


# ---------------------------------------------------------------------------
# 3. F6：报错提示必须指向仓库里真实存在的、可复现的东西
# ---------------------------------------------------------------------------

def test_katex_missing_hint_points_at_a_reproducible_source():
    """提示里的 KaTeX 来源必须**在仓库里**，且装完就能用。

    原来写的是「见根目录 _vendor_assets.py」—— 那个脚本被 .gitignore 的
    `/_*.py` 忽略，根本不在仓库里；而且它管的是 static/vendor/（已入库），
    与这里要的 microbench/_vendor/node_modules/katex 是两回事。
    """
    from microbench import export_offline as eo

    hint = eo.KATEX_MISSING_HINT
    assert "npm install" in hint, "提示没给出可执行的安装命令"
    assert "microbench/_vendor" in hint, "提示没指出装到哪"

    # 依赖声明必须入库，否则干净 clone 不知道该装哪个版本
    pkg = REPO / "microbench" / "_vendor" / "package.json"
    assert pkg.exists(), "microbench/_vendor/package.json 没入库 —— 干净 clone 无法复现"
    lock = REPO / "microbench" / "_vendor" / "package-lock.json"
    assert lock.exists(), "package-lock.json 没入库 —— 版本没锁死"
    data = json.loads(pkg.read_text(encoding="utf-8"))
    assert "katex" in (data.get("dependencies") or {}), "package.json 里没有 katex 依赖"


def test_katex_vendor_dir_is_not_fully_gitignored():
    """`microbench/_vendor/` 不能再整个被忽略 —— 否则依赖声明进不了仓库。"""
    gi = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "microbench/_vendor/node_modules/" in gi, (
        ".gitignore 应该只忽略 node_modules，package.json 要入库")
    assert "\nmicrobench/_vendor/\n" not in gi, (
        "microbench/_vendor/ 整个目录还在被忽略 —— package.json 进不了仓库")
