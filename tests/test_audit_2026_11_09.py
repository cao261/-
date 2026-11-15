# -*- coding: utf-8 -*-
"""第四轮审计（2026-11-09）第 1 批修复的**行为级**回归护栏。

这个文件刻意不写 `assert "xxx" in 源码` —— 那类护栏改坏行为也照样绿
（见 AUDIT_2026-10-27 §6.6 / AGENTS §4.16）。每条都跑真实函数、断言结果，
并且尽可能带一条「改回错误实现就会红」的反向用例：

  * 固定 .tmp 名  → test_atomic_write_concurrent_writes 会红
  * `or {}`  读坏快照 → test_snapshot_save_* 会红
  * `return True` fail-open → test_extractor_gate_* 会红
  * 吞掉同步异常   → test_sync_reports_partial_failures 会红
  * float() 放行 nan → test_csv_* 会红
  * 坏变更包写到一半崩 → test_bad_pack_* 会红
  * 空操作报成功   → test_do_move_* 会红
  * 缺 KaTeX 照常覆盖 → test_export_tablet_* 会红
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. app.py：原子写 / 隔离读 / 版本闸门 / 同步错误可见
# ---------------------------------------------------------------------------

def test_atomic_write_concurrent_writes_never_interleave(tmp_path):
    """12 个并发写同一目标：结果必须是**某一整份**内容，不能有半截或残留 tmp。

    原来的 `path.name + ".tmp"` 固定名会让并发写互相 os.replace，
    这里必然报错或写出交错内容。
    """
    from microbench import app

    target = tmp_path / "state.json"
    errors: list[Exception] = []

    def worker(i: int):
        try:
            app._atomic_write_text(
                target, json.dumps({"i": i, "pad": "x" * 4000}))
        except Exception as e:      # noqa: BLE001 - 这正是要抓的竞态
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写抛异常了: {errors[:3]}"
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["i"] in range(12)
    assert data["pad"] == "x" * 4000, "写出的内容不完整（半截文件）"
    assert not list(tmp_path.glob(".*.tmp")), "临时文件没清干净"


def test_atomic_write_failure_keeps_the_old_file(tmp_path, monkeypatch):
    """写盘失败时旧文件必须完好，且不留临时文件。"""
    from microbench import app

    target = tmp_path / "state.json"
    target.write_text("OLD", encoding="utf-8")

    def boom(_src, _dst):
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(app.os, "replace", boom)
    with pytest.raises(OSError):
        app._atomic_write_text(target, "NEW")
    assert target.read_text(encoding="utf-8") == "OLD"
    assert not list(tmp_path.glob(".*.tmp"))


def test_read_json_or_quarantine_rejects_non_object(tmp_path):
    """合法 JSON 但顶层是 [] 也必须隔离 —— 否则调用方 `.get()` 会崩。"""
    from microbench import app

    p = tmp_path / "x.json"
    p.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        app._read_json_or_quarantine(p)
    assert not p.exists(), "坏文件应当被改名隔离"
    kept = list(tmp_path.glob("x.json.corrupt-*"))
    assert kept and kept[0].read_text(encoding="utf-8") == "[1, 2]"


def _snap_payload(paper: str, page: int = 1, translation: str = "新译文"):
    return {
        "paper_id": paper,
        "paper_title": "测试论文",
        "relative_path": "01_Literature/x.pdf",
        "paper_mtime": 1.0,
        "pages": [{"page_num": page, "text_hash": "abcdef0123456789",
                   "translation": translation, "model": "m",
                   "translated_at": "2026-11-09T12:00:00"}],
    }


def test_snapshot_save_never_silently_clears_a_corrupt_snapshot(client, sandbox):
    """半截快照 + 一次保存 → 不能返回 success，也不能把旧内容抹掉。"""
    from microbench import app

    paper = "corrupt_snapshot_paper"
    path = app._snapshot_path(paper)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"pages": [{"page_num": 1', encoding="utf-8")

    r = client.post("/api/pdf/snapshot/save", json=_snap_payload(paper))
    assert r.status_code >= 400, f"损坏快照被当成空值静默合并了: {r.text}"
    kept = list(path.parent.glob(path.name + ".corrupt-*"))
    assert kept, "损坏文件必须被隔离保留（供人工恢复）"
    assert kept[0].read_text(encoding="utf-8").startswith('{"pages":')


def test_snapshot_save_still_merges_healthy_snapshot(client, sandbox):
    """反向护栏：正常快照不受隔离逻辑影响，仍按页合并。"""
    from microbench import app

    paper = "healthy_snapshot_paper"
    assert client.post("/api/pdf/snapshot/save",
                       json=_snap_payload(paper, 1)).status_code == 200
    assert client.post("/api/pdf/snapshot/save",
                       json=_snap_payload(paper, 2)).status_code == 200
    data = app._safe_read_json(app._snapshot_path(paper))
    assert sorted(p["page_num"] for p in data["pages"]) == [1, 2]


def test_extractor_gate_fails_closed_when_version_unavailable(monkeypatch):
    """取不到 EXTRACTOR_VERSION 时必须拒绝合并，不能放行旧译文。"""
    from microbench import app

    def boom():
        raise ImportError("paper_text 不可用")

    monkeypatch.setattr(app, "_current_extractor_version", boom)
    assert app._snapshot_extractor_ok({"extractor_version": 5}) is False


def test_extractor_gate_accepts_current_version():
    from microbench import app

    cur = app._current_extractor_version()
    assert app._snapshot_extractor_ok({"extractor_version": cur}) is True
    assert app._snapshot_extractor_ok({"extractor_version": cur - 1}) is False


def test_sync_reports_partial_failures(client, sandbox, monkeypatch):
    """单项写盘失败必须出现在 errors 里，不能再报「全绿」。"""
    from microbench import app

    snap_dir = sandbox / "logs" / "_pdf_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    (snap_dir / "a.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(app, "_merge_snapshot_files",
                        lambda *_a, **_k: '{"pages": []}')

    def boom(*_a, **_k):
        raise OSError("模拟写盘失败")

    monkeypatch.setattr(app, "_atomic_write_text", boom)
    r = client.post("/api/sync", json={"push_only": True, "include_files": False})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["error_count"] >= 1
    assert d["errors"][0]["kind"] == "snapshot_push"
    assert d["errors"][0]["name"] == "a.json"


def test_sync_clean_run_has_no_errors(client, sandbox):
    """反向护栏：没有失败项时 error_count 必须是 0。"""
    r = client.post("/api/sync", json={"push_only": True, "include_files": False})
    assert r.status_code == 200, r.text
    assert r.json()["error_count"] == 0
    assert r.json()["errors"] == []


def test_public_ipv6_origin_is_not_reflected(client):
    """公网 IPv6 字面量不该拿到 CORS 回显，也不能跨站读到 API。"""
    r = client.get("/api/stats", headers={"Origin": "http://[2001:db8::1]"})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


# ---------------------------------------------------------------------------
# 2. plot_compare.py：非有限值 / 千位分隔 / 半残 tolerance
# ---------------------------------------------------------------------------

def test_csv_parser_rejects_nan_inf_and_extra_columns(capsys):
    from microbench.plot_compare import _parse_csv_points

    text = "0,1\nnan,2\n3,inf\n1,234.5,6.7\n4,5\n"
    pts = _parse_csv_points(text)
    assert pts == [[0.0, 1.0], [4.0, 5.0]], f"脏数据混进坐标了: {pts}"
    assert "跳过" in capsys.readouterr().err


def test_compute_metrics_rejects_non_finite_input():
    from microbench.plot_compare import compute_metrics

    with pytest.raises(ValueError, match="非有限"):
        compute_metrics([[0.0, float("nan")], [1.0, 2.0]],
                        [[0.0, 1.0], [1.0, 2.0]])


def test_compute_metrics_tolerates_partial_tolerance_dict():
    """只给 pass_pct 时不能 KeyError（warn_pct 要有默认值）。"""
    from microbench.plot_compare import compute_metrics

    paper = [[0.0, 1.0], [1.0, 2.0], [2.0, 3.0]]
    repro = [[0.0, 1.1], [1.0, 2.1], [2.0, 3.1]]
    d = compute_metrics(paper, repro, tolerance={"pass_pct": 5.0})
    assert d["verdict"] in ("pass", "warn", "fail", "inconclusive")


# ---------------------------------------------------------------------------
# 3. merge_changes.py：坏包整包拒绝 / 冲突稿唯一 / 读失败不当空
# ---------------------------------------------------------------------------

def _pack(notes):
    return {"format": "microbench-tablet-pack", "notes": notes}


def test_bad_pack_is_rejected_before_any_write(tmp_path):
    from microbench import merge_changes as mc

    p = tmp_path / "pack.json"
    p.write_text(json.dumps(_pack([
        {"id": "01_Literature/a.md", "body": "正常"},
        "我是坏元素",
    ]), ensure_ascii=False), encoding="utf-8")
    assert mc.load_pack(p) is None, "坏包必须整包拒绝（不能写一半再崩）"


def test_read_text_failure_returns_none_not_empty(tmp_path):
    from microbench import merge_changes as mc

    p = tmp_path / "bad.md"
    p.write_bytes(b"\xff\xfe not utf-8")
    assert mc.read_text(p) is None


def test_conflict_drafts_are_unique_and_original_untouched(tmp_path):
    from microbench import merge_changes as mc

    vault = tmp_path / "vault"
    lit = vault / "01_Literature"
    lit.mkdir(parents=True)
    target = lit / "note.md"
    target.write_text("ORIGINAL", encoding="utf-8")
    pack = _pack([{"id": "01_Literature/note.md",
                   "body": "平板版本", "baseHash": "对不上的哈希"}])

    s = mc.merge_pack(pack, vault)
    assert s["conflicts"] == ["01_Literature/note.md"]
    assert target.read_text(encoding="utf-8") == "ORIGINAL"
    mc.merge_pack(pack, vault)   # 同秒再来一次
    drafts = sorted(lit.glob("note_冲突合并_*.md"))
    assert len(drafts) == 2, f"同秒冲突稿互相覆盖了: {drafts}"
    for d in drafts:
        body = d.read_text(encoding="utf-8")
        assert "平板版本" in body and "ORIGINAL" in body


# ---------------------------------------------------------------------------
# 4. export_offline.py：缺资源拒绝覆盖 / 原子写 / 坏快照不崩
# ---------------------------------------------------------------------------

def test_export_tablet_refuses_missing_katex_and_keeps_previous(tmp_path, monkeypatch,
                                                                capsys):
    """缺 KaTeX 时必须拒绝导出，且**不能覆盖**已有产物。

    ⚠ 这条护栏原来的写法有个坑（2026-11-10 审计 F2）：
    它只断言 `export_tablet(...) is False`，而 export_tablet 在
    「vault 里一篇笔记都没有」时**也**返回 False —— 断言被另一条路径满足，
    于是把 KaTeX 检查整个删掉它照样绿（干净 clone / CI / 新设备上
    正是这种状态，护栏等于不存在）。实测：空 vault 副本里撤掉检查 → 绿；
    补一篇笔记 → 红。

    现在三件事一起断言：
      ① 造一篇真笔记，让导出「本该成功」；
      ② stderr 里必须出现「缺少 KaTeX」—— 这是**可区分**的拒绝原因，
         不会被「没有找到任何笔记」那条路径满足；
      ③ 已有产物不能被覆盖。
    """
    from microbench import export_offline as eo

    vault = tmp_path / "vault"
    (vault / "01_Literature").mkdir(parents=True)
    (vault / "01_Literature" / "probe.md").write_text(
        "# 探针笔记\n\n正文。\n", encoding="utf-8")
    monkeypatch.setattr(eo, "REPO", vault)
    monkeypatch.setattr(eo, "KATEX_DIST", tmp_path / "empty_katex")

    out = tmp_path / "tablet.html"
    out.write_text("PREVIOUS GOOD", encoding="utf-8")
    assert eo.export_tablet(out, quiet=True) is False
    err = capsys.readouterr().err
    assert "缺少 KaTeX" in err, (
        "不是因缺 KaTeX 拒绝的 —— 这条护栏被另一条返回路径满足了：\n" + err)
    assert out.read_text(encoding="utf-8") == "PREVIOUS GOOD", "已有产物被覆盖了"


def test_export_atomic_write_replaces_whole_file(tmp_path):
    from microbench import export_offline as eo

    out = tmp_path / "reader.html"
    out.write_text("OLD", encoding="utf-8")
    eo._atomic_write_text(out, "NEW" * 5000)
    assert out.read_text(encoding="utf-8") == "NEW" * 5000
    assert not list(tmp_path.glob(".*.tmp"))


def test_collect_translations_survives_malformed_snapshots(tmp_path, monkeypatch, capsys):
    from microbench import export_offline as eo

    monkeypatch.setattr(eo, "REPO", tmp_path)
    snap = tmp_path / "logs" / "_pdf_snapshots"
    snap.mkdir(parents=True)
    (snap / "truncated.json").write_text("{broken", encoding="utf-8")
    (snap / "array.json").write_text("[]", encoding="utf-8")
    out = eo.collect_translations()
    assert isinstance(out, dict)
    assert "跳过 2 份" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 5. move_vault_to_cloud.py：空操作不报成功 / 失败能回滚
# ---------------------------------------------------------------------------

def test_do_move_does_not_report_success_when_nothing_moved(tmp_path, monkeypatch):
    from microbench import move_vault_to_cloud as mv

    repo = tmp_path / "repo"
    cloud = tmp_path / "cloud"
    target = cloud / mv.CLOUD_SUBDIR
    repo.mkdir()
    target.mkdir(parents=True)
    for name in mv.VAULT_ITEMS:
        (repo / name).mkdir()
        (target / name).mkdir()      # 云端已有同名 → 全部 [跳过]

    monkeypatch.setattr(mv, "REPO", repo)
    monkeypatch.setattr(mv, "detect_cloud_dir", lambda: cloud)
    monkeypatch.setattr(mv, "is_junction", lambda _p: False)

    assert mv.do_move(True) == 1, "一个 junction 都没建，不能返回 0"
    assert all((repo / n).exists() for n in mv.VAULT_ITEMS)


def test_rollback_moved_restores_files(tmp_path, monkeypatch):
    from microbench import move_vault_to_cloud as mv

    repo = tmp_path / "repo"
    target = tmp_path / "cloud" / mv.CLOUD_SUBDIR
    (target / "01_Literature").mkdir(parents=True)
    (target / "01_Literature" / "note.md").write_text("用户笔记", encoding="utf-8")
    repo.mkdir()
    monkeypatch.setattr(mv, "REPO", repo)

    assert mv.rollback_moved(["01_Literature"], target) is True
    assert (repo / "01_Literature" / "note.md").read_text(encoding="utf-8") == "用户笔记"


# ---------------------------------------------------------------------------
# 6. test_bench.py：standalone 测试台绝不指向真实 vault
# ---------------------------------------------------------------------------

def test_testbench_runs_in_a_temporary_vault():
    """用子进程导入，避免它 patch 掉的模块常量污染本 pytest 进程。"""
    code = ("import microbench.test_bench as t; "
            "print('SANDBOX=' + str(t.BASE_DIR))")
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    line = [x for x in r.stdout.splitlines() if x.startswith("SANDBOX=")][-1]
    sandbox = Path(line.split("=", 1)[1]).resolve()
    assert "mb_testbench_" in sandbox.name
    assert sandbox != REPO.resolve()
