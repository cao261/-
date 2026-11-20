# -*- coding: utf-8 -*-
"""阅读进度续读 —— 桌面端**真应用**验收脚本（关掉应用 → 重开同一篇 → 必须回到同一行）。

用户 2026-11-20 的原话：
    「我这论文都很长嘛，我不想每次打开都要重新翻好久翻到我目前看的部分，
      需要你能做到像看小说一样，这次看到这里了，关掉应用，下次打开也还是这里」

⚠ 这条脚本验的是**真应用**（`desktop.py` → pywebview → Edge WebView2），
   不是浏览器页面刷新 —— 差别很大：
   · 真应用的 localStorage 在 `logs/webview_profile`（WebView2 用户目录）
   · 真应用每次启动的 URL 带随机 cache-bust（`?v=<hex>`），**没有 open_pdf**
   · 第二次打开必须走用户的真实路径：**从「仓库论文库」下拉框里点开这篇论文**

驱动方式：`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`
→ 用 Playwright 的 `connect_over_cdp` 接进**真窗口**里操作（不另开浏览器）。

⚠ 会拦掉 /api/paper/translate_page（不烧用户 LLM 额度）与 /api/sync*（不写坚果云）。
⚠ 真应用自己起服务、自己停服务 —— 跟你双击快捷方式的流程一致。

跑法：python microbench/_live_readpos_test.py [pdf 相对路径] [页码]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "logs" / "blockdiag"
PROFILE = REPO / "logs" / "webview_profile"
CDP_PORT = 9222
APP_TITLE = "MicroBench"          # 窗口标题前缀
LS_KEY = "microbench_readpos_v1"

DEFAULT_REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
               "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, ("  [%s]" % detail) if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


# ---------------------------------------------------------------------------
# 应用进程的启停
# ---------------------------------------------------------------------------
def _ps(cmd: str) -> str:
    """跑一条 PowerShell，返回 stdout。"""
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          capture_output=True, text=True, errors="replace").stdout


def kill_app_processes() -> None:
    """关掉可能残留的**本应用**进程。

    ⚠ 只杀 `--user-data-dir` 指向本应用 profile 的 WebView2 进程 ——
       这台机器上还有 **Windows 自己的** WebView2 组件（通知中心/小组件），
       一刀切 `taskkill /IM msedgewebview2.exe` 会把系统组件一起打死。
    """
    ps = (
        "$p='%s'; "
        "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | "
        "Where-Object { $_.CommandLine -like ('*' + $p + '*') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    ) % str(PROFILE).replace("'", "''")
    _ps(ps)
    ps2 = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*desktop.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    _ps(ps2)
    time.sleep(1.5)


def launch_app(open_pdf: str | None) -> subprocess.Popen:
    env = dict(os.environ)
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=%d" % CDP_PORT
    args = [sys.executable, "desktop.py"]
    if open_pdf:
        args += ["--open", open_pdf]
    return subprocess.Popen(
        args, cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def wait_cdp(timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def close_app_window() -> bool:
    """按用户点 X 的方式优雅关窗（WM_CLOSE → pywebview 退出 → 服务随之停）。"""
    import win32con
    import win32gui
    hits: list[int] = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd) or ""
            if APP_TITLE in t:
                hits.append(hwnd)
        return True

    win32gui.EnumWindows(cb, None)
    for hwnd in hits:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    return bool(hits)


def app_process_alive(proc: subprocess.Popen) -> bool:
    return proc.poll() is None


def _pick_page(ctx):
    for pg in ctx.pages:
        if "127.0.0.1:5000" in (pg.url or ""):
            return pg
    return ctx.new_page()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    rel = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REL).replace("\\", "/")
    page_no = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    into = 100                       # 滚进页内多少 px
    OUT.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    print("目标论文：%s（滚到第 %d 页，页内 %dpx）\n" % (rel[:70], page_no, into))

    # ---------- 第 1 次启动：真应用，直接打开这篇 ----------
    print("=" * 68)
    print("① 第一次启动桌面应用（--open 直开这篇论文）")
    print("=" * 68)
    kill_app_processes()
    proc1 = launch_app(rel)
    if not wait_cdp():
        print("✗ WebView2 远程调试端口没起来 —— 查 logs/desktop.log")
        return 1
    print("✓ 应用窗口已起，CDP 已就绪")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % CDP_PORT)
        page = _pick_page(browser.contexts[0])
        try:
            page.route("**/api/paper/translate_page", lambda r: r.abort())
            page.route("**/api/sync**", lambda r: r.abort())
            print("✓ 已拦截 翻译 / 同步 接口")
        except Exception as e:
            print("⚠ 拦截没生效（%s）—— 靠快照兜底，别翻页太多" % e)

        page.wait_for_selector(".pdf-page-canvas", timeout=180000)
        print("✓ PDF 已渲染")
        time.sleep(6.0)

        # ---- 滚到第 N 页、滚进页内 into px ----
        page.evaluate("""([n, into]) => {
            const pane = document.getElementById('pdf-viewer-container');
            const card = document.getElementById('pdf-page-block-' + n);
            if (!pane || !card) return false;
            const d = card.getBoundingClientRect().top - pane.getBoundingClientRect().top;
            pane.scrollTop += d + into;
            return true;
        }""", [page_no, into])
        time.sleep(4.0)      # 等 400ms 防抖落盘 + 写进 WebView2 的 LevelDB

        rec = page.evaluate("""(key) => {
            const raw = localStorage.getItem(key);
            if (!raw) return null;
            const o = JSON.parse(raw);
            const k = Object.keys(o)[0];
            return {k: k, rec: o[k], count: Object.keys(o).length};
        }""", LS_KEY)
        left = (((rec or {}).get("rec") or {}).get("left") or {}) if rec else {}
        print("落盘：key=%s  page=%s  delta=%s  ratio=%s"
              % (rec and rec["k"], left.get("page"), left.get("delta"), left.get("ratio")))
        check("① 进度写进了真应用的 localStorage", rec is not None, str(rec and rec["k"]))
        check("① 记的是左栏原文的第 %d 页" % page_no, left.get("page") == page_no,
              "page=%s" % left.get("page"))
        check("① 页内位置也记下来了（%dpx）" % into, left.get("offset") == into,
              "offset=%s" % left.get("offset"))
        want_delta = left.get("delta")
        page.screenshot(path=str(OUT / "desktop_readpos_1_before.png"))

        # ---------- 关掉整个应用 ----------
        print("\n" + "=" * 68)
        print("② 关闭整个桌面应用（模拟点 X 关窗）")
        print("=" * 68)
        found = close_app_window()
        print("✓ 已向应用窗口发 WM_CLOSE（找到 %d 个窗口）" % (1 if found else 0))
        t0 = time.time()
        while time.time() - t0 < 40 and app_process_alive(proc1):
            time.sleep(0.5)
        check("② 应用进程确实退出了", not app_process_alive(proc1),
              "%.1fs 后退出" % (time.time() - t0))
        try:
            browser.close()
        except Exception:
            pass
        time.sleep(3.0)      # 让 WebView2 把用户目录刷盘

        # ---------- 第 2 次启动：真应用，不带 open_pdf，走用户路径 ----------
        print("\n" + "=" * 68)
        print("③ 第二次启动桌面应用（不带 --open → 从「仓库论文库」里点开这篇）")
        print("=" * 68)
        proc2 = launch_app(None)
        if not wait_cdp():
            print("✗ 第二次启动的 CDP 没起来")
            return 1
        browser2 = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % CDP_PORT)
        page2 = _pick_page(browser2.contexts[0])
        try:
            page2.route("**/api/paper/translate_page", lambda r: r.abort())
            page2.route("**/api/sync**", lambda r: r.abort())
        except Exception:
            pass

        page2.wait_for_function(
            "() => !!document.getElementById('vault-pdf-select')", timeout=120000)
        time.sleep(2.0)

        # 用户动作：扫仓库 → 在下拉框里选中这篇 → onchange → openVaultPdf
        picked = page2.evaluate("""async (rel) => {
            await loadVaultPdfOptions();
            const sel = document.getElementById('vault-pdf-select');
            if (!sel) return 'NO_SELECT';
            const opts = Array.from(sel.options).map(o => o.value);
            if (opts.indexOf(rel) < 0) return 'NOT_IN_LIST:' + opts.length;
            sel.value = rel;
            sel.dispatchEvent(new Event('change'));
            return 'PICKED';
        }""", rel)
        check("③ 下拉框里能选中这篇论文并触发打开", picked == "PICKED", str(picked))

        page2.wait_for_selector(".pdf-page-canvas", timeout=180000)
        print("✓ PDF 已渲染")
        time.sleep(9.0)

        after = page2.evaluate("""(n) => {
            const pane = document.getElementById('pdf-viewer-container');
            const card = document.getElementById('pdf-page-block-' + n);
            if (!pane || !card) return null;
            return {
                delta: pane.getBoundingClientRect().top - card.getBoundingClientRect().top,
                scrollTop: pane.scrollTop,
                scrollHeight: pane.scrollHeight,
                clientHeight: pane.clientHeight,
            };
        }""", page_no)
        print("重启后：%s" % json.dumps(after, ensure_ascii=False))
        check("③ 重开后左栏确实滚起来了（不是停在第 1 页）",
              bool(after) and after.get("scrollTop", 0) > 0,
              "scrollTop=%s" % (after or {}).get("scrollTop"))
        if after and want_delta is not None:
            expect = -want_delta
            check("③ 重开后回到**同一行**（页内误差 ≤ 3px）",
                  abs(after["delta"] - expect) <= 3,
                  "期望 %.0f 实际 %.0f" % (expect, after["delta"]))

        rec2 = page2.evaluate("""(key) => {
            const raw = localStorage.getItem(key);
            if (!raw) return null;
            const o = JSON.parse(raw);
            return o[Object.keys(o)[0]];
        }""", LS_KEY)
        check("③ 重开后进度记录仍在（没被清掉）", rec2 is not None)
        page2.screenshot(path=str(OUT / "desktop_readpos_2_after.png"))

        try:
            browser2.close()
        except Exception:
            pass
        close_app_window()
        t0 = time.time()
        while time.time() - t0 < 30 and app_process_alive(proc2):
            time.sleep(0.5)

    print("\n失败 %d 项：%s" % (len(FAILS), FAILS if FAILS else "无"))
    print("截图：%s\n      %s" % (OUT / "desktop_readpos_1_before.png",
                                 OUT / "desktop_readpos_2_after.png"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
