# -*- coding: utf-8 -*-
"""2026-10-27 全量审计的回归护栏（核心层：app.py + 桌面前端 + 依赖）。

这一轮审计（第四轮，前三轮见 docs/AUDIT_2026-10-25.md / AUDIT_2026-10-26.md）
的修复在这里各留一条护栏。**每条护栏都要能真的红** —— 见文件末尾的
「破坏性验证」清单（把修复改回去 → 对应护栏必须失败 → 复原 → 全绿）。

分组：
  A 局域网口令护栏（含 /docs /openapi.json 这条新增的）
  B 鉴权 fail-closed
  C 绝对路径 / 凭据回显
  D 事件循环阻塞（性能）
  E GET 端点不再建目录
  F 缓存加锁 / 跨设备读-改-写 CAS
  G 模型名白名单
  H 桌面前端（专注模式 / 弹窗 Esc / 批注重放）
  I 依赖 CVE 版本锁
"""
from __future__ import annotations

import inspect
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / "microbench" / "static" / "index.html"
APP_PY = REPO / "microbench" / "app.py"
JS_TEST = REPO / "microbench" / "_test_audit_2026_10_27.js"
REQ = REPO / "requirements.txt"

TOKEN = "audit-token-2026"


def _index_src() -> str:
    return INDEX.read_text(encoding="utf-8")


def _app_src() -> str:
    return APP_PY.read_text(encoding="utf-8")


def _as_remote_peer(monkeypatch):
    """把调用方伪装成「局域网上的另一台机器」。"""
    from microbench import app as app_mod

    monkeypatch.setattr(app_mod, "_LAN_TOKEN", TOKEN)
    monkeypatch.setattr(app_mod, "_is_remote_peer", lambda host: True)
    return app_mod


# ===========================================================================
# A 局域网口令护栏
# ===========================================================================

def test_lan_openapi_and_docs_require_token(monkeypatch):
    """/docs /redoc /openapi.json 在局域网上**必须**要口令。

    实测（2026-10-27）：从 192.168.1.100 无口令访问 /openapi.json 拿到
    71386 字节的**完整 API 目录**（200），而同一时刻所有 /api/* 都正确 401。
    交互式文档等于把「60 条路由 + 全部请求模型」的地图免费送给局域网anyone。
    """
    from fastapi.testclient import TestClient
    from microbench.app import app

    _as_remote_peer(monkeypatch)
    with TestClient(app) as c:
        for p in ("/openapi.json", "/docs", "/redoc"):
            r = c.get(p)
            assert r.status_code == 401, f"{p} 远端无口令可访问（{r.status_code}）"
        assert c.get("/api/stats").status_code == 401
        # /api/auth/* 仍然豁免 —— 平板要先问「要不要口令」再把口令交上来
        assert c.get("/api/auth/status").status_code == 200
        # 带对口令 → 放行
        r = c.get("/openapi.json", headers={"X-MB-Token": TOKEN})
        assert r.status_code == 200, r.text


def test_lan_openapi_still_open_for_local_users(monkeypatch):
    """本机（回环）用不能被这道护栏挡住 —— 桌面端要能看 API 文档。"""
    from fastapi.testclient import TestClient
    from microbench.app import app

    with TestClient(app) as c:
        assert c.get("/openapi.json").status_code == 200


# ===========================================================================
# B 鉴权 fail-closed
# ===========================================================================

def test_client_is_local_fails_closed_when_peer_is_missing():
    """拿不到对端地址时**不能**当成「本机」（fail-open = 鉴权整个失效）。"""
    from microbench import app as app_mod

    class _NoPeer:
        client = None

    assert app_mod._client_is_local(_NoPeer()) is False


# ===========================================================================
# C 绝对路径 / 凭据回显
# ===========================================================================

def test_sync_status_root_is_masked_for_remote_clients(monkeypatch):
    """远端拿到的同步根只能是云端末段，不能带本机用户名和坚果云镜像 id。"""
    from fastapi.testclient import TestClient
    from microbench.app import app

    _as_remote_peer(monkeypatch)
    with TestClient(app) as c:
        r = c.get("/api/sync/status", headers={"X-MB-Token": TOKEN})
        assert r.status_code == 200, r.text
        root = r.json()["root"]
        assert not re.match(r"^[A-Za-z]:", root), f"远端拿到了完整绝对路径: {root}"
        assert len(Path(root).parts) <= 2, f"路径段太多: {root}"


def test_sync_status_root_is_full_path_for_local_users(client):
    """本机用户仍然看得到完整路径（他要用它确认两端云端路径一致）。"""
    r = client.get("/api/sync/status")
    assert r.status_code == 200, r.text
    assert re.match(r"^[A-Za-z]:", r.json()["root"]), r.json()["root"]


def test_safe_err_masks_windows_absolute_paths():
    """异常回显里不能带本机绝对路径（含 Windows 用户名）。"""
    from microbench import app as app_mod

    out = app_mod._safe_err(OSError(r"cannot open C:\Users\30461\Nutstore\1\x.json"))
    assert "30461" not in out, out
    assert "<本机路径>" in out, out


def test_safe_err_redacts_credentials():
    from microbench import app as app_mod

    out = app_mod._safe_err(RuntimeError("boom sk-abcdefghijklmnopqrstuvwxyz012345"))
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out, out


def test_no_raw_exception_leaks_in_http_details():
    """所有 HTTPException 的 detail 都必须过 _safe_err。"""
    src = _app_src()
    assert "detail=str(e)" not in src, "还有裸 str(e) 回显"
    assert '"llm_error": str(e)' not in src, "问答接口还在回显原始异常"
    assert 'f"⚠️ 问答过程出错: {str(e)' not in src


def test_ask_question_error_is_redacted(client, monkeypatch):
    """问答兜底分支回给浏览器的内容里不能带本机绝对路径。"""
    from microbench import app as app_mod

    monkeypatch.setattr(app_mod, "search_vault", lambda *a, **k: [{"note": "x", "path": "a.md"}])
    monkeypatch.setattr(app_mod.llm, "is_configured", lambda: True)

    def _boom(*a, **k):
        raise OSError(r"cannot read D:\Users\30461\Nutstore\1\snapshots\x.json")

    monkeypatch.setattr(app_mod.llm, "answer_with_context", _boom)
    r = client.post("/api/ask", json={"question": "hello"})
    assert r.status_code == 200, r.text
    body = r.text
    assert "30461" not in body, body[:400]
    assert "llm_error" in body


# ===========================================================================
# D 事件循环阻塞（性能）
# ===========================================================================

def test_find_pdf_endpoint_is_not_async():
    """全量 rglob 的端点必须是同步 def（丢进 anyio 线程池），不能卡事件循环。"""
    from microbench import app as app_mod

    assert not inspect.iscoroutinefunction(app_mod.api_find_pdf)


def test_formula_image_clip_runs_in_thread():
    """裁图是同步 CPU 工作，必须 to_thread。

    ⚠ 断言必须**限定在 api_formula_image 这个函数里**：
      `_pdf_clip_png` 还被 _read_table_vision（抄表）用着，
      全文件 grep 会两边都命中 → 护栏对该修复零覆盖。

    ⚠ 2026-10-28 合并修正：原来断言的是字面量
      `"asyncio.to_thread(_pdf_clip_png"`（**单行**），
      对方设备把同一个调用换行写成
      `await asyncio.to_thread(\n    _pdf_clip_png, ...)` →
      **语义完全相同**但字符串不匹配 → 护栏误报（假红）。
      → 改成断言「`_pdf_clip_png` 的调用出现在 `to_thread(` 之后」
      （忽略换行/空白），并**排除**裸同步调用。
    """
    import inspect
    from microbench import app as app_mod

    src = inspect.getsource(app_mod.api_formula_image)
    # 去掉所有空白后再比对 → 换行写法不再骗过护栏
    flat = re.sub(r"\s+", "", src)
    assert "asyncio.to_thread(_pdf_clip_png" in flat, src[-800:]
    # 反向护栏：不能有绕过 to_thread 的裸调用
    #   （"to_thread(" 后面紧跟 _pdf_clip_png 已由上面覆盖；
    #    这里额外确认函数体里没有 "png = _pdf_clip_png" 这种裸调）
    assert "png=_pdf_clip_png(" not in flat, src[-800:]


def test_translate_page_extraction_runs_in_thread():
    """按页翻译的 PDF 抽取必须 to_thread（实测 64~332 ms/页）。

    ⚠ 同上：断言限定在 api_translate_page 函数体里
      （_extract_page_cached 的 docstring 里也写着 to_thread，
       全文件 grep 会误判成"已修"）。
    """
    import inspect
    from microbench import app as app_mod

    src = inspect.getsource(app_mod.api_translate_page)
    assert "asyncio.to_thread(" in src, src[-800:]
    assert "_extract_page_cached" in src, src[-800:]


def test_sync_files_channel_runs_in_thread():
    """同步的 files 通道（sha256 + 79MB 拷贝）不能堵事件循环。

    ⚠ 2026-10-28 合并修正：对方设备把 `/api/sync` 整个改成**同步 def**
      （函数体全是同步磁盘 IO → FastAPI 自动丢进 anyio 线程池）。
      原来的断言是字面量 `"asyncio.to_thread(_sync_push_files)"` →
      端点改成同步 def 之后 `to_thread` 反而**多余**（线程池里再开线程）
      → 护栏要求的是一个**更差**的实现形态。

    → 改成断言**行为语义**：
      ① 端点**不是** async def（否则它会独占事件循环）；
      ② files 通道的重活调用仍被调用到（没被误删）。
    """
    import inspect
    from microbench import app as app_mod

    # ① 必须是同步端点 → FastAPI 自动丢线程池 → 不冻结事件循环
    assert not inspect.iscoroutinefunction(app_mod.api_sync), \
        "api_sync 又变回 async def 了 → 整个同步磁盘 IO 会独占事件循环"

    src = inspect.getsource(app_mod.api_sync)
    flat = re.sub(r"\s+", "", src)
    # ② files 通道的重活还在被调用
    assert "_sync_push_files(" in flat, src[-800:]
    assert "_sync_pull_files(" in flat, src[-800:]


def test_extract_page_cached_uses_disk_cache(monkeypatch):
    """命中磁盘缓存时不重复抽 PDF（缓存里的页结构与单页抽取完全一致）。"""
    from microbench import app as app_mod

    monkeypatch.setattr(
        app_mod.pdf_cache, "read_cache",
        lambda *a, **k: [{"page": 2, "text": "CACHED", "glyphs": [{"font": "F"}]}],
    )
    called = []
    monkeypatch.setattr(app_mod, "extract_page_text_with_glyphs",
                        lambda p, n: (called.append(n) or ("LIVE", [])))
    text, glyphs = app_mod._extract_page_cached(Path("x.pdf"), 2)
    assert text == "CACHED"
    assert glyphs == [{"font": "F"}]
    assert called == [], "命中缓存时不该再抽一次"


def test_extract_page_cached_falls_back_to_live_extraction(monkeypatch):
    from microbench import app as app_mod

    monkeypatch.setattr(app_mod.pdf_cache, "read_cache", lambda *a, **k: None)
    called = []
    monkeypatch.setattr(app_mod, "extract_page_text_with_glyphs",
                        lambda p, n: (called.append(n) or ("LIVE", [])))
    text, glyphs = app_mod._extract_page_cached(Path("x.pdf"), 3)
    assert text == "LIVE"
    assert called == [3]


# ===========================================================================
# E GET 端点不再建目录
# ===========================================================================

def test_get_endpoints_do_not_create_directories(sandbox, monkeypatch, tmp_path):
    """GET 端点不得在磁盘（乃至坚果云同步根）里建目录。

    GET 属安全方法 → **不受 csrf_origin_guard 约束** → 跨站一个 <img>
    就能让服务端建目录。原来 `_annotations_dir()` 会在 GET 路径上
    mkdir → 在用户的云端共享目录里建目录（还会被坚果云上传）。
    """
    from fastapi.testclient import TestClient
    from microbench import app as app_mod

    shared = tmp_path / "syncroot"
    shared.mkdir()
    monkeypatch.setattr(app_mod, "_sync_root", lambda: shared)

    with TestClient(app_mod.app) as c:
        snap_dir = app_mod._snapshot_dir()
        ann_dir = shared / "annotations"
        # lifespan 启动时建过一次；删掉，再验证 GET 不会重建
        shutil.rmtree(snap_dir, ignore_errors=True)
        shutil.rmtree(ann_dir, ignore_errors=True)

        assert c.get("/api/pdf/annotations/load?paper_id=probepaper").status_code == 200
        assert not ann_dir.exists(), "GET /api/pdf/annotations/load 建了目录"

        assert c.get("/api/pdf/snapshot/list").status_code == 200
        assert not snap_dir.exists(), "GET /api/pdf/snapshot/list 建了目录"


def test_write_paths_still_create_the_directories(sandbox, monkeypatch, tmp_path):
    """写路径必须照旧能建目录（不能把功能改没了）。"""
    from fastapi.testclient import TestClient
    from microbench import app as app_mod

    shared = tmp_path / "syncroot"
    shared.mkdir()
    monkeypatch.setattr(app_mod, "_sync_root", lambda: shared)

    with TestClient(app_mod.app) as c:
        r = c.post("/api/pdf/annotations/save", json={
            "paper_id": "probepaper",
            "annotations": [{"id": "a1", "quote": "x", "page": 1}],
        })
        assert r.status_code == 200, r.text
        assert (shared / "annotations").is_dir(), "写路径没有建目录"


# ===========================================================================
# F 缓存加锁 / 跨设备读-改-写 CAS
# ===========================================================================

def test_real_stats_cache_is_lock_protected():
    """模块级缓存现在被 anyio 线程池并发读写 → 必须有**真的**锁。

    ⚠ 断言要检查它是个 Lock 实例，不能只查 hasattr ——
      把 `_REAL_STATS_LOCK = None` 也算"有锁"的话，护栏就是零覆盖。
    """
    import threading
    from microbench import app as app_mod

    lock = getattr(app_mod, "_REAL_STATS_LOCK", None)
    assert isinstance(lock, type(threading.Lock())), f"没有真的锁: {lock!r}"


def test_annotation_save_merges_and_keeps_both_sides(client):
    """CAS 改造不能弄坏跨设备合并：两次保存的批注都要在。"""
    r = client.post("/api/pdf/annotations/save", json={
        "paper_id": "probepaper",
        "annotations": [{"id": "a1", "quote": "x", "page": 1, "color": "yellow"}],
    })
    assert r.status_code == 200, r.text
    r = client.post("/api/pdf/annotations/save", json={
        "paper_id": "probepaper",
        "annotations": [{"id": "a2", "quote": "y", "page": 1, "color": "yellow"}],
    })
    assert r.status_code == 200, r.text
    r = client.get("/api/pdf/annotations/load?paper_id=probepaper")
    ids = {a["id"] for a in r.json()["annotations"]}
    assert ids == {"a1", "a2"}, ids


def test_annotation_save_still_applies_delete_tombstones(client):
    """删除墓碑仍然生效（桌面端删掉的批注不能被复活）。"""
    client.post("/api/pdf/annotations/save", json={
        "paper_id": "probepaper",
        "annotations": [{"id": "a1", "quote": "x", "page": 1}],
    })
    r = client.post("/api/pdf/annotations/save", json={
        "paper_id": "probepaper",
        "annotations": [],
        "deleted": ["a1"],
    })
    assert r.status_code == 200, r.text
    r = client.get("/api/pdf/annotations/load?paper_id=probepaper")
    ids = {a["id"] for a in r.json()["annotations"]}
    assert "a1" not in ids, ids


# ===========================================================================
# G 模型名白名单
# ===========================================================================

def test_unknown_model_name_is_not_passed_through():
    """客户端传任意模型名 → 不能原样透传（它会被回显到前端）。"""
    from microbench import app as app_mod

    payload = "<img src=x onerror=alert(1)>"
    name, fallback, cache_key = app_mod._resolve_translation_model(payload)
    assert payload not in name
    assert payload not in cache_key
    assert name == "" or name in app_mod._known_model_ids()


def test_known_models_still_resolve():
    """白名单不能把正常选项挡掉。"""
    from microbench import app as app_mod

    _name, fallback, _ = app_mod._resolve_translation_model("agnes")
    assert fallback is True

    name, fallback, ck = app_mod._resolve_translation_model("MiniMax-M3")
    assert name == "MiniMax-M3" and fallback is False and ck == "MiniMax-M3"


# ===========================================================================
# H 桌面前端
# ===========================================================================

def test_zen_topbar_is_removed_from_focus_order():
    """专注模式藏起来的工具条不能还在 Tab 序里（否则键盘用户掉进看不见的工具条）。"""
    src = _index_src()
    m = re.search(r"#reader-modal\.zen \.reader-topbar\s*\{(.*?)\}", src, re.S)
    assert m, "找不到 zen 顶栏规则"
    assert "visibility: hidden" in m.group(1), m.group(1)
    assert "translateY(-101%)" in m.group(1), "滑出动画不能被删"


def test_zen_nav_small_screen_gives_feedback():
    """小屏（<860px）章节导航放不下 → 必须如实提示，不能是死按钮。"""
    src = _index_src()
    assert 'matchMedia("(max-width: 860px)")' in src


def test_modal_esc_handlers_are_not_once_true():
    """`{once:true}` 会让「任意键」吃掉 Esc 监听器 → 弹窗关不掉。

    判据扫的是**所有** `addEventListener("keydown"` 调用点：
    只要有任意一个带 `{ once: true }` 就算不合格。
    （只查 `onEsc, { once: true }` 会被改个变量名绕过去 —— 那也是零覆盖。）
    """
    # ⚠ 注释里会**引用**旧写法来说明坑 → 判据必须只看代码行
    code_lines = [ln for ln in _index_src().splitlines() if not ln.strip().startswith("//")]
    code = "\n".join(code_lines)

    hits = []
    lines = code.splitlines()
    for i, ln in enumerate(lines):
        if 'addEventListener("keydown"' in ln:
            window = "\n".join(lines[i:i + 3])
            if "once: true" in window:
                hits.append(window.strip()[:120])
    assert not hits, f"还有 {{once:true}} 的 keydown 监听器: {hits}"
    assert "_wireModalEsc" in code
    assert "_wireModalEsc(modal, closeSnapshotModal);" in code
    assert "_wireModalEsc(modal, closeDiagnoseModal);" in code


def test_annotation_features_are_initialised_only_once():
    """initAnnotationFeatures 每次开论文都会调用 → document 级监听器不能重复挂。"""
    assert "__mbAnnoFeaturesInited" in _index_src()


def test_model_name_is_escaped_in_section_header():
    """章节视图的「模型名」写进 innerHTML 前必须转义。"""
    assert "escapeHtml(modelName)" in _index_src()


def test_annotation_replay_is_debounced_behaviourally():
    """批注重放的自激循环 —— 行为级护栏（node 跑 index.html 里的真实源码）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 批注重放护栏跳过")
    proc = subprocess.run(
        [node, str(JS_TEST)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


# ===========================================================================
# I 依赖 CVE 版本锁
# ===========================================================================

def test_requirements_pin_patched_pillow_and_requests():
    """Pillow 12.2.0 有 40+ 个已知 CVE（全部 13.0.0 修）；
    requests 2.32.3 有 2 个（2.32.4 / 2.33.0 修）。"""
    req = REQ.read_text(encoding="utf-8")
    assert re.search(r"^Pillow>=13\.0\.0", req, re.M), "Pillow 版本下限没锁到 13.0.0"
    assert re.search(r"^requests>=2\.33\.0", req, re.M), "requests 版本下限没锁到 2.33.0"
