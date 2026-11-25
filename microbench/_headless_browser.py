# -*- coding: utf-8 -*-
"""headless 浏览器的**回退链**：系统 Edge/Chrome → Playwright 自带的 Chromium。

**为什么需要（2026-11-17 实测）**

本机（设备 30461）的系统 Edge 157 **headless 模式直接不输出任何东西**：

    msedge.exe --headless=new --disable-gpu --no-sandbox --dump-dom 平凡.html
    → stdout 长度 0，连 --user-data-dir 目录都建不出来（进程根本没启动）

（连版本目录里的 `157.0.4341.60\\msedge.exe` 也一样。）

后果很严重：**5 个渲染护栏集体失效** ——
`_test_desktop_bilingual_render.py` / `_test_desktop_ink_render.py` /
`_test_tablet_bilingual_render.py` / `_test_tablet_region_render.py` /
`_test_tablet_pdf_layout.py`
全都报「没拿到探针结果（DOM 长度 0）」→ 它们保护的
「对照模式真的左右分栏」「手写笔迹真的画出来」「圈选区域真的换算成坐标」…
**全都测不了了**（护栏失效 = 改坏了也没人知道，AGENTS §4.16 同型复发）。

而 **Playwright 自带的 Chromium 是好的**（`%LOCALAPPDATA%\\ms-playwright\\chromium-*`）。

→ 所以这里的策略是：**系统浏览器优先**（对方设备 25353 上它一直好用，
  行为完全不变），**拿不到输出就回退**到 Playwright 的 Chromium。
  两个都没有 → 抛错（护栏必须红，不许静默跳过）。

⚠ 回退**不依赖 playwright 这个 pip 包**（只用它的浏览器缓存目录）：
   对方设备上没装 playwright 时，回退自动失效 → 行为跟今天完全一样。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# 系统浏览器候选（跟各探针脚本里原有的 EDGE_CANDIDATES 一致）
SYSTEM_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_system_browser() -> str | None:
    """第一个存在的系统 Edge/Chrome（找不到就 None）。"""
    for p in SYSTEM_BROWSERS:
        if Path(p).is_file():
            return p
    for name in ("msedge", "chrome"):
        from shutil import which
        got = which(name)
        if got:
            return got
    return None


def find_playwright_chromium() -> str | None:
    """Playwright 下载的 Chromium（不依赖 playwright 这个 pip 包）。

    只扫浏览器缓存目录：
      %PLAYWRIGHT_BROWSERS_PATH% / %LOCALAPPDATA%\\ms-playwright /
      ~/AppData/Local/ms-playwright
    """
    roots: list[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        roots.append(Path(env))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")
    roots.append(Path.home() / "AppData" / "Local" / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")

    rels = (
        "chrome-win64/chrome.exe",
        "chrome-win/chrome.exe",
        "chrome-headless-shell-win64/chrome-headless-shell.exe",
        "chrome-linux/chrome",
        "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    )
    for root in roots:
        if not root.is_dir():
            continue
        # 版本号大的优先（chromium-1228 > chromium-1223）
        for d in sorted(root.glob("chromium*"), key=lambda p: p.name, reverse=True):
            for rel in rels:
                p = d / rel
                if p.is_file():
                    return str(p)
    return None


_WORKING_PAIR: tuple[str, str] | None = None


def dump_dom(html_path: Path, *, browser: str | None = None,
             window_size: str = "1400,1000", virtual_time_budget: int = 3000,
             timeout: int = 120) -> str:
    """把一个本地 HTML 用 headless 浏览器渲染，取回最终 DOM 文本。

    优先使用缓存的可用 `(exe, flag)` 组合；若无缓存，依次尝试：
    Playwright Chromium（专为 headless 优化，无系统 Edge 16s 卡顿）→ 系统浏览器。
    **返回空字符串视为失败**，继续试下一个；全都不行才抛错。
    """
    global _WORKING_PAIR
    html_path = Path(html_path)
    uri = html_path.resolve().as_uri()
    candidates: list[str] = []
    if browser:
        candidates.append(browser)
    pw = find_playwright_chromium()
    if pw and pw not in candidates:
        candidates.append(pw)
    sys_b = find_system_browser()
    if sys_b and sys_b not in candidates:
        candidates.append(sys_b)

    if not candidates:
        raise RuntimeError(
            "找不到可用的 headless 浏览器（系统 Edge/Chrome 与 Playwright Chromium 都没有）")

    pairs: list[tuple[str, str]] = []
    if _WORKING_PAIR is not None and _WORKING_PAIR[0] in candidates:
        pairs.append(_WORKING_PAIR)
    for exe in candidates:
        # ⚠ `--headless=new` 与 `--headless` 都试：
        #   新版 Edge/Chrome 与 Playwright Chromium 上 `--headless=new` 更稳更快，
        #   旧版 Chromium <112 才需要 `--headless`。
        for headless_flag in ("--headless=new", "--headless"):
            pair = (exe, headless_flag)
            if pair not in pairs:
                pairs.append(pair)

    tried: list[str] = []
    for exe, headless_flag in pairs:
        cmd = [
            exe, headless_flag, "--disable-gpu", "--no-sandbox",
            f"--window-size={window_size}",
            f"--virtual-time-budget={virtual_time_budget}",
            "--dump-dom", uri,
        ]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            tried.append(f"{exe} {headless_flag}: 超时")
            continue
        except Exception as e:                      # 启动失败等
            tried.append(f"{exe} {headless_flag}: {e}")
            continue
        dom = p.stdout.decode("utf-8", errors="replace")
        if dom.strip():
            _WORKING_PAIR = (exe, headless_flag)
            return dom
        tried.append(f"{exe} {headless_flag}: 无输出")
    raise RuntimeError("headless 浏览器都没吐出 DOM：" + "；".join(tried))


def find_browser() -> str | None:
    """优先 Playwright Chromium（稳且快）；缺失时回退系统 Edge/Chrome。

    ⚠ 对方设备 25353 没装 playwright 时 `find_playwright_chromium()` 返回 None，
      自动落回系统浏览器，两边都能用且不静默跳过护栏。
    """
    return find_playwright_chromium() or find_system_browser()
