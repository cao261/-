# -*- coding: utf-8 -*-
"""`auto_sync.find_pwsh()` 的护栏。

**为什么要有这份文件**

`auto_sync.py` 要在同步完成后重打平板 APK，重打要调 PowerShell 7。
原来它只查一张写死的绝对路径表，而两台设备都把 pwsh 装在**用户目录**下
（路径里带用户名）→ 表里只有 30461 那台的路径 → **本机 25353 永远找不到**
→ 每轮同步都「跳过重打 APK」，而日志里那行提示看着不像错误，
所以一直没人发现 APK 停在旧版本。

这里钉死三层查找顺序，任何一层被删掉都会红：
  1. PATH 上的 pwsh（最可靠，跨设备）
  2. 程序目录下的绝对路径（系统级安装）
  3. %LOCALAPPDATA%\\Programs\\PowerShell\\<版本>\\pwsh.exe 通配
"""
from __future__ import annotations

from pathlib import Path

import pytest

from microbench import auto_sync


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """把三层查找全部清空，让每个用例自己决定哪一层有东西。"""
    monkeypatch.setattr(auto_sync.shutil, "which", lambda _name: None)
    monkeypatch.setattr(auto_sync, "PWSH_CANDIDATES", [])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-such-localappdata"))
    return tmp_path


def _make_local_install(local_appdata: Path, version: str = "7") -> Path:
    pwsh = local_appdata / "Programs" / "PowerShell" / version / "pwsh.exe"
    pwsh.parent.mkdir(parents=True, exist_ok=True)
    pwsh.write_bytes(b"MZ fake")
    return pwsh


def test_path_lookup_wins(isolated, monkeypatch, tmp_path):
    """PATH 上有 pwsh 时直接用它 —— 不依赖任何写死的路径。"""
    exe = tmp_path / "bin" / "pwsh.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ fake")
    monkeypatch.setattr(auto_sync.shutil, "which", lambda name: str(exe) if name == "pwsh" else None)
    assert auto_sync.find_pwsh() == exe


def test_falls_back_to_localappdata(isolated, monkeypatch, tmp_path):
    """PATH 里没有时，用户级安装（%LOCALAPPDATA%）必须被找到。

    这就是本机（25353）的真实形态：pwsh 装在
    `C:\\Users\\<用户名>\\AppData\\Local\\Programs\\PowerShell\\7\\pwsh.exe`。
    """
    local = tmp_path / "AppData" / "Local"
    pwsh = _make_local_install(local)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert auto_sync.find_pwsh() == pwsh


def test_prefers_newest_version_when_several_exist(isolated, monkeypatch, tmp_path):
    """同一台机器装着多个版本时，取目录名排序靠后的（优先新版）。"""
    local = tmp_path / "AppData" / "Local"
    _make_local_install(local, "7.4")
    newer = _make_local_install(local, "7.6")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert auto_sync.find_pwsh() == newer


def test_returns_none_when_nothing_exists(isolated):
    """三层都没有 → None（调用方据此报告"跳过重打 APK"，不能假装成功）。"""
    assert auto_sync.find_pwsh() is None


def test_build_apk_reports_missing_pwsh(monkeypatch):
    """找不到 pwsh 时 build_apk 必须**明确报失败**，不能静默返回成功。

    这不是吹毛求疵：原来的提示文字是"跳过重打 APK"，看着像正常跳过，
    实际结果是交付的 APK 一直是旧的。
    """
    monkeypatch.setattr(auto_sync, "find_pwsh", lambda: None)
    ok, msg = auto_sync.build_apk()
    assert ok is False
    assert "PowerShell 7" in msg
