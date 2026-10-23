#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键对齐：拉最新代码 → 重启服务 → 同步共享目录。

两台设备各跑一份 MicroBench 时，代码走 GitHub、数据走坚果云同步目录。
这个脚本把"手动对齐"的三个动作压成一次双击：

    1) git pull --ff-only        （代码）
    2) 重启 uvicorn 服务          （后端改动要重启才生效）
    3) POST /api/sync            （数据：PDF/批注/快照/进度）

用法：
    python sync_align.py            # 正常对齐
    python sync_align.py --no-pull  # 只重启服务 + 同步（本地有未提交改动时）

Windows / macOS / Linux 通用。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "http://127.0.0.1:5000"
TOTAL = 3


def step(msg: str) -> None:
    print(f"[{step.n}/{TOTAL}] {msg}", flush=True)
    step.n += 1


step.n = 1


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def git_pull() -> bool:
    """先直连（GitHub 直连常比走代理稳），失败再让 git 自己按配置走。"""
    for extra in ([], ["-c", "http.proxy=", "-c", "https.proxy="]):
        p = run(["git", *extra, "pull", "--ff-only", "origin", "main"])
        if p.returncode == 0:
            head = run(["git", "log", "-1", "--oneline"]).stdout.strip()
            print(f"      代码已对齐 → {head}")
            return True
        err = (p.stdout or "") + (p.stderr or "")
    print("      拉取失败：\n" + err.strip()[:500])
    return False


def _kill_old_service() -> None:
    """停掉旧的 uvicorn 进程（Windows 用 CIM 查命令行）。"""
    if sys.platform.startswith("win"):
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Where-Object { $_.CommandLine -match 'uvicorn microbench' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=60)
    else:
        subprocess.run(["pkill", "-f", "uvicorn microbench"],
                       capture_output=True, timeout=60)


def restart_service() -> bool:
    _kill_old_service()
    time.sleep(2)
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "microbench.app:app",
         "--host", "127.0.0.1", "--port", "5000", "--log-level", "warning"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs,
    )
    for _ in range(40):
        time.sleep(1)
        try:
            urllib.request.urlopen(API + "/api/stats", timeout=3).read()
            print("      服务已就绪 → http://127.0.0.1:5000")
            return True
        except Exception:
            continue
    print("      服务启动超时（60 秒内没起来），请看 logs/ 或手动启动")
    return False


def sync() -> dict | None:
    req = urllib.request.Request(
        API + "/api/sync", data=b"{}",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"      同步失败：{e}")
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-pull", action="store_true",
                    help="跳过 git pull（本地有未提交改动时用）")
    args = ap.parse_args()

    print("=" * 62)
    print("  MicroBench 一键对齐")
    print("=" * 62)

    if args.no_pull:
        step("跳过拉取（--no-pull）")
    else:
        step("拉取最新代码")
        if not git_pull():
            print("      ⚠ 拉取失败，继续做服务重启与同步")

    step("重启服务")
    if not restart_service():
        return 1

    step("同步共享目录")
    d = sync()
    if not d:
        return 1

    pushed = {k: len(v) for k, v in (d.get("pushed") or {}).items()}
    pulled = {k: len(v) for k, v in (d.get("pulled") or {}).items()}
    print(f"      推送 {pushed}")
    print(f"      拉取 {pulled}")
    fp = (d.get("files") or {}).get("push") or {}
    fl = (d.get("files") or {}).get("pull") or {}
    print(f"      PDF：发布 {len(fp.get('published', []))} 篇 / "
          f"更新 {len(fp.get('updated', []))} 篇 / "
          f"未变 {fp.get('unchanged', 0)} 篇 / "
          f"补齐 {len(fl.get('restored', []))} 篇")
    print(f"      耗时 {d.get('elapsed_ms', 0)} ms")
    print()
    print("✅ 对齐完成")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
