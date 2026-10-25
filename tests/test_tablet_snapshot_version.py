# -*- coding: utf-8 -*-
"""护栏：平板端与桌面端的「快照有效性」规则必须一致。

2026-10-25 合并两端改动时挖出来的三个跨端不一致：

1. 桌面端给快照加了 `extractor_version` —— 抽取算法一改，旧快照里的译文
   就是**坏的**（按旧算法翻的，栏序可能整个反过来），读取时整份作废重翻。
2. 但**磁盘上的旧页还在**，而按 page_num 合并的逻辑会把它们合回来
   ⇒ 过期译文被复活（读取时的作废逻辑只看最终那一份，管不到它）。
3. 平板端当时**没有**这个判断 → 会把旧译文照搬给用户看（正是最早报的那个
   问题：「译文和原文对不上」）。

⚠ 判据必须**逐字一致**（desktop ↔ tablet ↔ 导出器）：

    saved_extractor_version != 当前版本  →  整份作废

「**没有** extractor_version」也算对不上 —— 那是旧算法翻出来的。
（2026-10-25 实测：本机现存 3 份快照全都没有版本号。）

跑:  python -m pytest tests/test_tablet_snapshot_version.py -q
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _cur_ver() -> int:
    from microbench.paper_text import EXTRACTOR_VERSION
    return EXTRACTOR_VERSION


def _snap(paper_id: str, *, ver, pages, sig: str = "") -> dict:
    """造一份快照（ver=None 表示**没有** extractor_version 字段）。"""
    d: dict = {
        "paper_id": paper_id,
        "paper_title": "测试论文",
        "relative_path": f"01_Literature/{paper_id}.pdf",
        "paper_mtime": 0,
        "paper_text_signature": sig,
        "pages": pages,
    }
    if ver is not None:
        d["extractor_version"] = ver
    return d


# ---------------------------------------------------------------------------
# ① 导出器：只烤当前版本的译文
# ---------------------------------------------------------------------------

def test_exporter_only_bakes_current_version_translations(tmp_path, monkeypatch):
    """导出器只烤当前抽取算法翻出来的译文。"""
    from microbench import export_offline

    monkeypatch.setattr(export_offline, "REPO", tmp_path)
    snap_dir = tmp_path / "logs" / "_pdf_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    cur = _cur_ver()
    good = _snap("good", ver=cur, pages=[{"page_num": 1, "translation": "好译文"}])
    old = _snap("old", ver=cur - 1, pages=[{"page_num": 1, "translation": "旧译文"}])
    legacy = _snap("legacy", ver=None, pages=[{"page_num": 1, "translation": "无版本译文"}])
    for name, d in (("good", good), ("old", old), ("legacy", legacy)):
        (snap_dir / f"{name}.json").write_text(
            json.dumps(d, ensure_ascii=False), encoding="utf-8")

    baked = export_offline.collect_translations()
    assert "good" in baked, "当前版本的译文没被烤进去"
    assert baked["good"].get("1") == "好译文"
    assert "old" not in baked, "旧抽取算法翻的译文被烤进了平板单文件"
    assert "legacy" not in baked, "没有 extractor_version 的旧译文被烤进了平板单文件"


def test_exporter_publishes_current_version_to_tablet():
    """平板要知道「当前版本」才能判断云盘上的译文是不是旧的。"""
    from microbench import export_offline

    assert export_offline.current_extractor_version() == _cur_ver(), \
        "导出器报给平板的版本跟实际抽取算法版本对不上"
    src = (REPO / "microbench" / "export_offline.py").read_text(encoding="utf-8")
    assert '"trans_ver"' in src, "种子数据里没有 trans_ver"


def _export_as_script(out: Path) -> subprocess.CompletedProcess:
    """像 build_apk.ps1 那样，以**脚本模式**跑导出器。"""
    return subprocess.run(
        [sys.executable, str(REPO / "microbench" / "export_offline.py"),
         "--tablet", str(out), "--quiet"],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )


def test_exporter_as_script_can_resolve_extractor_version():
    """⚠ 以**脚本模式**跑时，导出器必须能取到抽取算法版本。

    这是个真实踩过的坑：`python microbench/export_offline.py` 时
    `sys.path[0]` 是 `microbench/`，仓库根不在里面 →
    `from microbench.paper_text import ...` ImportError
    （paper_text.py 内部是相对导入，只能以包形式导入）→
    被 `except` 吞掉 → **译文版本过滤整个不生效** →
    旧算法翻的坏译文照样烤进平板单文件（正是「译文和原文对不上」那个老问题）。

    失败得很安静，所以必须有这条护栏。
    """
    code = (
        "import sys; sys.path.insert(0, %r); "
        "import export_offline; "
        "print('VER=', export_offline.current_extractor_version())"
    ) % str(REPO / "microbench")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "取不到抽取算法版本" not in (proc.stderr or ""), \
        f"脚本模式下取不到抽取算法版本:\n{proc.stderr}"
    assert proc.stdout.strip() == f"VER= {_cur_ver()}", \
        f"脚本模式取到的版本不对: {proc.stdout.strip()!r}"


def test_exporter_as_script_bakes_only_current_translations(tmp_path):
    """端到端：脚本模式跑出来的单文件，trans_ver 要有值，且不含旧译文。"""
    out = tmp_path / "tablet.html"
    proc = _export_as_script(out)
    assert proc.returncode == 0, f"导出失败:\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}"
    assert out.exists(), "导出器没有产出文件"

    html = out.read_text(encoding="utf-8")
    m = re.search(r'<script type="application/json" id="mb-data">(.*?)</script>', html, re.S)
    assert m, "单文件里找不到种子数据"
    data = json.loads(m.group(1).replace("<\\/", "</"))

    assert data.get("trans_ver") == _cur_ver(), (
        "单文件里没有 trans_ver（平板无法判断译文是不是旧的）—— "
        f"实际是 {data.get('trans_ver')!r}")
    assert "取不到抽取算法版本" not in (proc.stderr or ""), \
        f"导出时没能取到抽取算法版本:\n{proc.stderr[-500:]}"

    # 烤进去的每一份译文，都必须来自当前版本的快照
    snap_dir = REPO / "logs" / "_pdf_snapshots"
    baked = data.get("trans") or {}
    if not snap_dir.is_dir():
        return
    for stem in baked:
        fp = snap_dir / f"{stem}.json"
        if not fp.exists():
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        assert d.get("extractor_version") == _cur_ver(), \
            f"{stem} 是旧算法翻的，却被烤进了平板单文件"


# ---------------------------------------------------------------------------
# ② 桌面端：读取时整份作废
# ---------------------------------------------------------------------------

def test_desktop_load_invalidates_stale_snapshot(client, sandbox):
    """版本对不上的快照，桌面端读取时整份作废。"""
    cur = _cur_ver()
    snap_dir = sandbox / "logs" / "_pdf_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    stale = _snap("stale_paper", ver=cur - 1,
                  pages=[{"page_num": 1, "translation": "旧算法翻的"}])
    (snap_dir / "stale_paper.json").write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    r = client.get("/api/pdf/snapshot/load", params={"paper_id": "stale_paper"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("stale_extractor") is True, "版本不符的快照没有被标记为作废"
    assert not data.get("pages"), "版本不符的快照还在返回页内容（过期译文会被复活）"


def test_desktop_load_invalidates_legacy_snapshot_without_version(client, sandbox):
    """**没有** extractor_version 的快照也算旧 —— 那是旧算法翻的。"""
    snap_dir = sandbox / "logs" / "_pdf_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    legacy = _snap("legacy_paper", ver=None,
                   pages=[{"page_num": 1, "translation": "无版本的译文"}])
    (snap_dir / "legacy_paper.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    r = client.get("/api/pdf/snapshot/load", params={"paper_id": "legacy_paper"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data.get("stale_extractor") is True, \
        "没有 extractor_version 的旧快照没有被作废（用户会看到错栏序的译文）"
    assert not data.get("pages")


def test_desktop_load_keeps_current_snapshot(client, sandbox):
    """当前版本的快照照常返回（别把好的也作废了）。"""
    cur = _cur_ver()
    snap_dir = sandbox / "logs" / "_pdf_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    good = _snap("good_paper", ver=cur, pages=[{"page_num": 1, "translation": "好译文"}])
    (snap_dir / "good_paper.json").write_text(
        json.dumps(good, ensure_ascii=False), encoding="utf-8")

    r = client.get("/api/pdf/snapshot/load", params={"paper_id": "good_paper"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert not data.get("stale_extractor"), "当前版本的快照被误判成作废"
    assert len(data.get("pages") or []) == 1


# ---------------------------------------------------------------------------
# ③ 合并逻辑：不能把过期译文合回来
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, sig: str = "sig-1"):
        self.paper_text_signature = sig


def test_merge_pages_does_not_revive_stale_translation():
    """磁盘上已有的是旧算法翻的 → 不能合进来（否则过期译文被复活）。"""
    from microbench.app import _merge_snapshot_pages

    cur = _cur_ver()
    existing = _snap("p", ver=cur - 1, pages=[{"page_num": 1, "translation": "旧的"}])
    incoming = [{"page_num": 2, "translation": "新的"}]
    out = _merge_snapshot_pages(existing, _Req(), incoming)
    assert [p["page_num"] for p in out] == [2], \
        f"旧译文被合回来了: {out}"


def test_merge_pages_drops_legacy_existing_snapshot():
    """**没有** extractor_version 的旧快照也不能合进来。"""
    from microbench.app import _merge_snapshot_pages

    existing = _snap("p", ver=None, pages=[{"page_num": 1, "translation": "旧的"}])
    incoming = [{"page_num": 2, "translation": "新的"}]
    out = _merge_snapshot_pages(existing, _Req(), incoming)
    assert [p["page_num"] for p in out] == [2], f"旧译文被合回来了: {out}"


def test_merge_pages_keeps_both_when_versions_match():
    """两边都是当前版本 → 该合的还是要合（别把并集功能改没了）。"""
    from microbench.app import _merge_snapshot_pages

    cur = _cur_ver()
    existing = _snap("p", ver=cur, pages=[{"page_num": 1, "translation": "第一页"}])
    incoming = [{"page_num": 2, "translation": "第二页"}]
    out = _merge_snapshot_pages(existing, _Req(), incoming)
    assert [p["page_num"] for p in out] == [1, 2], f"当前版本的译文没被合并: {out}"


def test_merge_files_respects_version(tmp_path):
    """同步通道的文件级合并也要有版本闸门。"""
    from microbench.app import _merge_snapshot_files

    cur = _cur_ver()
    dst = tmp_path / "dst.json"
    src = tmp_path / "src.json"

    # dst 是旧版本 → 结果应当以 src 为准，不把 dst 的旧页合回来
    dst.write_text(json.dumps(
        _snap("p", ver=cur - 1, pages=[{"page_num": 1, "translation": "旧的"}]),
        ensure_ascii=False), encoding="utf-8")
    src.write_text(json.dumps(
        _snap("p", ver=cur, pages=[{"page_num": 2, "translation": "新的"}]),
        ensure_ascii=False), encoding="utf-8")
    text = _merge_snapshot_files(dst, src)
    assert text is not None
    merged = json.loads(text)
    assert merged.get("extractor_version") == cur, "合并结果带着旧版本号"
    assert [p["page_num"] for p in merged["pages"]] == [2], \
        f"旧译文明明不该被合回来: {merged['pages']}"

    # 两边都是当前版本 → 该合的还是要合
    dst.write_text(json.dumps(
        _snap("p", ver=cur, pages=[{"page_num": 1, "translation": "第一页"}]),
        ensure_ascii=False), encoding="utf-8")
    text = _merge_snapshot_files(dst, src)
    assert text is not None
    merged = json.loads(text)
    assert [p["page_num"] for p in merged["pages"]] == [1, 2], \
        f"当前版本的译文没被合并: {merged['pages']}"


def test_merge_files_keeps_content_when_both_sides_are_stale(tmp_path):
    """两边都是旧版本时，不能把内容弄丢。

    ⚠ 曾经的写法：`merged = dict(new) if ok(new) else dict(cur)` ——
    两边都旧时永远选 `cur`。而「本机刚建立、还没有这份快照」时
    `cur` 是空的 ⇒ 拉一次同步就把本机快照写成 **0 页**，
    看着像「这篇没翻过」，其实是被合并逻辑吃掉了。
    """
    from microbench.app import _merge_snapshot_files

    dst = tmp_path / "dst.json"      # 本机这边：还没有内容
    src = tmp_path / "src.json"      # 云端来的一份旧快照
    dst.write_text(json.dumps({"pages": []}, ensure_ascii=False), encoding="utf-8")
    src.write_text(json.dumps(
        _snap("p", ver=None, pages=[{"page_num": 1, "translation": "旧的"}]),
        ensure_ascii=False), encoding="utf-8")

    text = _merge_snapshot_files(dst, src)
    assert text is not None, "旧快照被整个丢掉了"
    merged = json.loads(text)
    assert len(merged.get("pages") or []) == 1, \
        "旧快照的内容被合并逻辑吃掉了（看着像「没翻过」）"


# ---------------------------------------------------------------------------
# ④ 平板端：同样的判据（行为级测试在 JS 套件里，见下）
# ---------------------------------------------------------------------------

def test_tablet_uses_strict_version_check():
    """平板端必须是**严格**比较：`!==` 当前版本就作废。

    ⚠ 曾经的写法是「有 extractor_version 且不等于当前版本」才作废 ——
      那等于把「没有版本号」的旧快照（= 旧算法翻的）当成好的放行。
      桌面端 `api_pdf_snapshot_load` 用的是严格判据，两端必须一致。
    """
    src = (REPO / "microbench" / "static" / "tablet" / "app.js").read_text(encoding="utf-8")
    assert "data.extractor_version !== cur" in src, \
        "平板端没有用严格版本比较"
    assert "data.extractor_version\n" not in src, \
        "平板端还在用宽松写法（没有版本号的旧快照会被放行）"


def test_tablet_cache_is_version_scoped():
    """本地缓存键必须带版本号 —— 否则 APK 升级后旧译文会从缓存直接返回。"""
    src = (REPO / "microbench" / "static" / "tablet" / "app.js").read_text(encoding="utf-8")
    assert "function transCacheKey" in src, "没有按版本分区的译文缓存键"
    assert 'trans_v' in src, "缓存键里没有版本号"


def test_tablet_version_gate_has_behaviour_test():
    """版本闸门必须有**行为级**测试（行为级才有保护力，grep 没有）。"""
    js = (REPO / "microbench" / "_test_tablet_hardening.js").read_text(encoding="utf-8")
    assert "loadTranslation" in js, "JS 套件没有驱动 loadTranslation"
    assert "extractor_version" in js, "JS 套件没有检查 extractor_version"
    # 这套 JS 套件必须挂在 pytest 上，否则「验收全绿」对页面代码没有保护力
    wiring = (REPO / "tests" / "test_tablet_frontend.py").read_text(encoding="utf-8")
    assert "_test_tablet_hardening.js" in wiring, \
        "平板 JS 套件没挂进 pytest（页面代码回退了也照样绿）"
