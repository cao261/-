# -*- coding: utf-8 -*-
"""
2026-10-26 代码审计的回归护栏。

这一组测试锁住本轮 P0 五处修复。每条都能**破坏性验证**：
把对应的修复改回去，这组测试立刻红。

对应审计报告：docs/AUDIT_2026-10-26.md

| 护栏 | 挡住的回归 |
|---|---|
| test_sync_root_* | H-1 测试写进用户真实的坚果云同步目录 |
| test_vault_* | M-1 /api/vault/file 作用域 = 整个仓库（含 LAN 口令） |
| test_probe_error_* | H-3 probe_primary 异常不过脱敏 → 凭据进浏览器 |
| test_safe_base_url_* | H-4 _safe_base_url 畸形 URL 兜底返回原样 |
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# H-1 —— 同步根必须被沙箱化
# ---------------------------------------------------------------------------

def test_sync_root_never_points_at_real_cloud_data():
    """测试进程里的同步根绝不能是真实的坚果云目录。

    背景（审计 2026-10-26 H-1，已实测复现）：
    `microbench.llm` import 时 `load_dotenv(override=False)` 会把 .env 里的
    ANNOTATIONS_DIR 灌进 os.environ → `_sync_root()` 命中第一个分支 →
    解析到真实云端目录 → pytest 会往里写东西。
    """
    from microbench import app as app_mod

    root = str(app_mod._sync_root())
    for marker in ("Nutstore", "我的坚果云", "坚果云"):
        assert marker not in root, f"同步根指向了真实云端目录: {root}"


def test_sync_root_is_inside_tmp(monkeypatch, tmp_path):
    """同步根必须落在 pytest 的临时目录里（而不是 vault 或云盘）。"""
    from microbench import app as app_mod

    root = Path(str(app_mod._sync_root())).resolve()
    assert str(root).startswith(str(Path(tmp_path).resolve())), \
        f"同步根不在临时目录内: {root}"


def test_sync_root_ignores_annotations_dir_env(monkeypatch, tmp_path):
    """就算环境变量指向真实云端目录，也不能被 _sync_root() 采纳。

    这条守的是「只钉环境变量不够」：`_sync_root()` 还有坚果云云端沙箱
    自动探测那条分支（app.py:3954），必须钉函数本身。
    """
    from microbench import app as app_mod

    monkeypatch.setenv("ANNOTATIONS_DIR", r"C:\Users\someone\Nutstore\1\我的坚果云\MicroBench同步")
    root = str(app_mod._sync_root())
    assert "Nutstore" not in root, f"环境变量仍然生效了: {root}"


def test_sync_root_ignores_nutstore_autodetect(monkeypatch):
    """ANNOTATIONS_DIR 没配时，_sync_root() 会去**自动探测坚果云云端沙箱**。

    那条分支（app.py:3954）指向的同样是真实的云端数据 ——
    所以「只把 ANNOTATIONS_DIR 设成临时目录」是**挡不住**的，
    必须把 `_sync_root` 函数本身钉死。

    这条测试专门守这一层：把环境变量拿掉，它仍然不许指向真实云端。
    """
    from microbench import app as app_mod

    monkeypatch.delenv("ANNOTATIONS_DIR", raising=False)
    root = str(app_mod._sync_root())
    for marker in ("Nutstore", "我的坚果云", "坚果云"):
        assert marker not in root, f"坚果云自动探测分支命中了真实目录: {root}"


def test_write_file_meta_does_not_touch_real_cloud_dir():
    """直接调 _write_file_meta 也必须落在临时目录里。

    这是 H-1 真正的元凶路径（test_sync_regressions_2026_10_25.py 里
    漏了 patch 的那个用例走的就是它）。
    """
    from microbench import app as app_mod

    app_mod._write_file_meta("demo", {"paper_id": "demo"})
    written = app_mod._file_meta_path("demo")
    assert written.exists()
    assert "Nutstore" not in str(written), f"清单写进了真实云端目录: {written}"


# ---------------------------------------------------------------------------
# M-1 —— /api/vault/file 的作用域必须是 vault 的内容目录
# ---------------------------------------------------------------------------

def test_vault_file_cannot_read_lan_token(client, sandbox):
    """logs/lan_token.txt 存着局域网共享口令 —— 绝不能经 API 读出来。

    审计 2026-10-26 M-1 实测：旧写法 GET 这个路径返回 200 + 明文口令。
    """
    (sandbox / "logs").mkdir(parents=True, exist_ok=True)
    (sandbox / "logs" / "lan_token.txt").write_text("SUPER-SECRET-TOKEN", encoding="utf-8")

    r = client.get("/api/vault/file", params={"relative_path": "logs/lan_token.txt"})
    assert r.status_code == 400, f"LAN 口令可以被读出来: {r.status_code} {r.text[:200]}"
    assert "SUPER-SECRET-TOKEN" not in r.text


def test_vault_file_cannot_read_repo_docs(client, sandbox):
    """仓库根的 .md（AGENTS.md / README.md）不属于 vault 内容。"""
    (sandbox / "AGENTS.md").write_text("# repo doc", encoding="utf-8")
    r = client.get("/api/vault/file", params={"relative_path": "AGENTS.md"})
    assert r.status_code == 400, f"仓库文档可以被读出来: {r.status_code}"


@pytest.mark.parametrize("rel", [
    "logs/x.md",
    "docs/x.md",
    "microbench/app.py",
    "tests/test_security.py",
    "x.md",
])
def test_vault_file_rejects_non_content_paths(client, sandbox, rel):
    r = client.get("/api/vault/file", params={"relative_path": rel})
    assert r.status_code == 400, f"{rel} 不该被允许: {r.status_code}"


@pytest.mark.parametrize("rel", [
    "logs/x.md",
    "docs/x.md",
    "AGENTS.md",
    "logs/lan_token.txt",
])
def test_vault_delete_rejects_non_content_paths(client, sandbox, rel):
    """删掉 logs/lan_token.txt → LAN 访问会被静默关闭（fail-closed）。

    审计 2026-10-26 M-1 实测：旧写法 DELETE 也能删到它。
    """
    r = client.request("DELETE", "/api/vault/file", params={"relative_path": rel})
    assert r.status_code == 400, f"{rel} 不该被允许删除: {r.status_code}"


def test_vault_reveal_rejects_non_content_paths(client, sandbox):
    """reveal 也不能被用来在文件管理器里打开仓库里的任意文件。"""
    r = client.post("/api/vault/reveal", params={"relative_path": "logs/lan_token.txt"})
    assert r.status_code == 400, f"reveal 越权: {r.status_code}"


def test_vault_file_still_serves_notes_in_content_dirs(client, sandbox):
    """收窄作用域不能把正常功能一起收掉 —— vault 里的笔记必须照常能读。"""
    note = sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" / "note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("# 我的精读笔记\n内容", encoding="utf-8")

    r = client.get("/api/vault/file",
                   params={"relative_path": "01_Literature/01_Device_TCAD_器件仿真/note.md"})
    assert r.status_code == 200, r.text
    assert "我的精读笔记" in r.text


def test_vault_delete_still_works_in_content_dirs(client, sandbox):
    note = sandbox / "01_Literature" / "note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("x", encoding="utf-8")

    r = client.request("DELETE", "/api/vault/file",
                       params={"relative_path": "01_Literature/note.md"})
    assert r.status_code == 200, r.text
    assert not note.exists()


def test_vault_delete_still_protects_shared_dirs(client, sandbox):
    """受保护目录的 403 语义不能被 400 顶掉。"""
    (sandbox / "03_Knowledge").mkdir(parents=True, exist_ok=True)
    (sandbox / "03_Knowledge" / "important.md").write_text("x", encoding="utf-8")
    r = client.request("DELETE", "/api/vault/file",
                       params={"relative_path": "03_Knowledge/important.md"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# H-3 —— probe_primary 的异常必须脱敏
# ---------------------------------------------------------------------------

def test_probe_error_does_not_leak_credentials(monkeypatch):
    """requests 的 InvalidURL 消息里含完整 URL（含 userinfo 凭据）。

    审计 2026-10-26 H-3 实测：旧写法 `f"探针网络请求异常: {e}"` 会把
    "https://user:sk-SECRET@host/v1" 原样返回给 /api/llm/probe 的浏览器。
    """
    import requests

    from microbench import llm

    class _StubLimiter:
        def acquire(self):
            return None

    def _boom(*_a, **_kw):
        raise requests.exceptions.InvalidURL(
            "Failed to parse: https://user:sk-SECRET@gw.internal:notaport/v1/chat/completions"
        )

    monkeypatch.setattr(llm, "primary_limiter", _StubLimiter())
    monkeypatch.setattr(llm, "requests", type("R", (), {"post": staticmethod(_boom)}))
    monkeypatch.setattr(llm.model_manager, "get_primary_config", lambda: {
        "api_key": "sk-SECRET",
        "base_url": "https://user:sk-SECRET@gw.internal:notaport/v1",
        "model": "test-model",
        "configured": True,
        "is_fallback": False,
    })

    res = llm.model_manager.probe_primary()
    assert res.get("success") is False
    msg = str(res.get("message", ""))
    assert "sk-SECRET" not in msg, f"探针把凭据漏出去了: {msg}"
    assert "SECRET" not in msg, f"探针把凭据漏出去了: {msg}"
    assert "探针网络请求异常" in msg, msg


# ---------------------------------------------------------------------------
# H-4 —— _safe_base_url 的兜底分支必须脱敏
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://user:sk-SECRET@gw.internal:notaport/v1",   # 端口不是数字 → urlsplit().port 抛
    "https://user:sk-SECRET@gw.internal:99999/v1",        # 端口超范围
    "https://user:sk-SECRET@[::1/v1",                    # IPv6 括号不配对
    "https://user:sk-SECRET@gw.internal/v1",              # 正常形态（回归基线）
])
def test_safe_base_url_redacts_credentials(url):
    """畸形 URL 的兜底分支曾经 `return url` —— 那等于把秘密原样吐回去。

    审计 2026-10-26 H-4 实测：3/4 变体泄露进 /api/llm/status。
    """
    from microbench.llm import _safe_base_url

    got = _safe_base_url(url)
    assert "sk-SECRET" not in got, f"凭据泄露: {got}"
    assert "SECRET" not in got, f"凭据泄露: {got}"


def test_safe_base_url_keeps_usable_url():
    """脱敏之后 URL 仍然要能用（host / path 不能被抹掉）。"""
    from microbench.llm import _safe_base_url

    got = _safe_base_url("https://api.minimaxi.com/v1")
    assert got == "https://api.minimaxi.com/v1"
