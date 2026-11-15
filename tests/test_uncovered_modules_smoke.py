# -*- coding: utf-8 -*-
"""第 2 批：给「只做过结构扫描、没有专属测试」的模块补最小**行为级**冒烟测试。

AUDIT_2026-11-09 §3 点名的 6 个文件里，第 1 批已经在
`test_audit_2026_11_09.py` 覆盖了 export_offline / merge_changes /
move_vault_to_cloud / plot_compare / test_bench；这里补上剩下的
`generator`，以及 `netutil` / `lan` / `desktop` 四个零覆盖模块。

原则：全部用 tmp_path / sandbox，**不读真实 vault、不联网、不开窗口**。
"""
from __future__ import annotations

import socket
import time

import pytest


# ---------------------------------------------------------------------------
# generator.py
# ---------------------------------------------------------------------------

def test_safe_path_accepts_normal_and_rejects_traversal(tmp_path):
    from microbench import generator as gen

    assert gen._safe_path(tmp_path, "01_Literature", "note.md") == \
        tmp_path / "01_Literature" / "note.md"
    for bad in ("..", "C:\\Windows\\win.ini", "/etc/passwd", "...", "  "):
        with pytest.raises(ValueError, match="非法路径"):
            gen._safe_path(tmp_path, "01_Literature", bad)
    # 归一化后**仍在 base 内**的 .. 是允许的（AGENTS §4.4：用 abspath
    # 而不是 resolve，遍历攻击靠包含性检查拦，不靠一刀切禁 ".."）
    assert gen._safe_path(tmp_path, "01_Literature", "../ok.md") == \
        tmp_path / "ok.md"


def test_sanitize_filename_strips_illegal_chars_and_dot_only_names():
    from microbench import generator as gen

    cleaned = gen.sanitize_filename("2026 GaAs/HZO: v1.2*.md")
    assert cleaned == "2026_GaAsHZO_v1.2.md"
    for ch in '\\/*?:"<>|':
        assert ch not in cleaned
    assert " " not in cleaned
    assert gen.sanitize_filename("..") == ""
    assert gen.sanitize_filename("paper.") == "paper"


def test_vault_stats_counts_files_in_the_sandbox(sandbox):
    """看板统计必须真的数文件，且缓存失效后能刷新（不能是常量 0）。"""
    from microbench import generator as gen

    gen._invalidate_vault_stats_cache()
    assert gen.get_vault_stats()["literature_total"] == 0
    folder = sandbox / "01_Literature" / "cat"
    folder.mkdir(parents=True)
    (folder / "note.md").write_text("# 一篇笔记\n", encoding="utf-8")
    gen._invalidate_vault_stats_cache()
    assert gen.get_vault_stats()["literature_total"] == 1


# ---------------------------------------------------------------------------
# netutil.py：代理兜底状态机
# ---------------------------------------------------------------------------

def test_proxy_bypass_switches_to_direct_and_expires(monkeypatch):
    from microbench import netutil

    monkeypatch.setitem(netutil._state, "bypassed_until", 0.0)
    assert netutil.proxy_bypassed() is False
    assert netutil.direct_kwargs() == {}

    netutil.mark_proxy_dead(Exception("Unable to connect to proxy"))
    assert netutil.proxy_bypassed() is True
    assert netutil.direct_kwargs() == {"proxies": {"http": None, "https": None}}

    # TTL 到期后必须自动恢复走代理（用户把代理客户端开回来后无需重启服务）
    monkeypatch.setitem(netutil._state, "bypassed_until", time.time() - 1)
    assert netutil.proxy_bypassed() is False
    assert netutil.direct_kwargs() == {}


# ---------------------------------------------------------------------------
# lan.py：口令持久化与优先级
# ---------------------------------------------------------------------------

def test_token_is_persistent_and_does_not_rotate(tmp_path):
    from microbench import lan

    assert lan.read_token(tmp_path) == ""
    token = lan.ensure_token(tmp_path)
    assert len(token) >= 12
    assert lan.token_path(tmp_path).read_text(encoding="utf-8").strip() == token
    assert lan.ensure_token(tmp_path) == token, "每次启动都换口令会让平板反复配对"


def test_resolve_token_env_override_wins(tmp_path, monkeypatch):
    from microbench import lan

    persisted = lan.ensure_token(tmp_path)
    monkeypatch.delenv("MB_LAN_TOKEN", raising=False)
    assert lan.resolve_token(tmp_path) == persisted
    monkeypatch.setenv("MB_LAN_TOKEN", "explicit-override")
    assert lan.resolve_token(tmp_path) == "explicit-override"


def test_pairing_url_and_loopback_hosts():
    from microbench import lan

    assert lan.pairing_url("192.168.1.5", 5000, "abc") == \
        "http://192.168.1.5:5000/?k=abc"
    assert "127.0.0.1" in lan.lan_hosts()
    assert "::1" in lan.lan_hosts()


# ---------------------------------------------------------------------------
# desktop.py：只测纯函数，绝不开窗口
# ---------------------------------------------------------------------------

def test_desktop_port_probe_distinguishes_open_from_closed():
    import desktop

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        assert desktop._port_open(port) is True, "有监听都没探到"

    # 监听关掉后必须探不到（AGENTS §4.19：不能把 50000 之类相邻端口
    # 当成自己的服务）
    assert desktop._port_open(port) is False, "端口已关闭却仍被判为已启动"


def test_desktop_module_import_has_no_gui_side_effects():
    """desktop.py 顶层不能起窗口/服务 —— 导入必须是纯定义。"""
    import desktop

    assert callable(desktop.main)
    assert callable(desktop._probe)
    assert isinstance(desktop.DEFAULT_PORT, int)
