"""
MicroBench 桌面应用启动器 —— 把工作台装进**独立的应用窗口**，不再走浏览器。

为什么要有它
------------
`start_workbench.bat` 干完"起服务"就 `start "" %URL%`，把页面丢进默认浏览器：
地址栏、标签页、书签栏、扩展按钮全在，读论文时想全屏还得自己按 F11，
点两下就跳回别的标签页 —— 用起来是"网页"，不是"应用"。

本模块用 **Edge WebView2**（Windows 10/11 自带的 Chromium 内核，pywebview 驱动）
开一个**真正独立的应用窗口**：

* 没有地址栏 / 标签页 / 扩展按钮，任务栏有独立条目和专属图标
* 窗口位置与尺寸跨次启动保留（logs/window_state.json）
* localStorage 持久化（主题、Obsidian vault 名、阅读器状态、答疑面板几何）
  —— pywebview 默认 private_mode=True 会在每次启动时清空，这里显式关掉
* 关闭窗口 = 退出应用；服务是自己起的就随之停止，自己没起就不动它

降级链（任何一环都不许静默失败）
--------------------------------
1. pywebview 可用          → 原生应用窗口（首选）
2. pywebview 缺失/起不来   → 用 Edge / Chrome 的 `--app=` 模式开一个"无浏览器外壳"的
                             独立窗口；此时服务改为**分离进程**启动，退出后仍在跑
3. 连 Edge/Chrome 都没有   → 弹系统 MessageBox 报错，并写 logs/desktop.log

用法
----
    python desktop.py                 # 起服务(若未运行) + 开应用窗口
    python desktop.py --attach        # 只连已经跑着的服务，不自己起
    python desktop.py --port 8765     # 换端口
    python desktop.py --web           # 强制走浏览器 --app 兜底路线
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(APP_DIR, "assets", "microbench.ico")
LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "desktop.log")
PROFILE_DIR = os.path.join(LOG_DIR, "webview_profile")
STATE_PATH = os.path.join(LOG_DIR, "window_state.json")

DEFAULT_PORT = 5000
APP_TITLE = "MicroBench · 微电子科研工作台"
DEFAULT_WIDTH = 1440
DEFAULT_HEIGHT = 900
MIN_WIDTH = 1100
MIN_HEIGHT = 700


# --------------------------------------------------------------------------
# 日志：pythonw.exe 下 sys.stdout / sys.stderr 是 None，
# uvicorn 的日志处理器拿到 None 会静默丢日志，所以先接管成文件流。
# --------------------------------------------------------------------------
class _Tee:
    """同时写控制台与日志文件；任意一端挂了都不影响另一端。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False


def _setup_streams() -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        return
    try:
        fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        fh = None
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)


def _log(msg: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print(f"[{stamp}] {msg}")
    except Exception:
        pass


def _message_box(title: str, text: str, icon: int = 0x10) -> None:
    """系统弹窗。pythonw 下没有控制台，必须给用户一个能看见的失败提示。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, icon)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 服务探测
# --------------------------------------------------------------------------
def _probe(url: str, timeout: float = 2.5) -> bool:
    """判断跑在这个地址上的**是不是我们自己的工作台**。

    不能只看端口通不通：Windows 上 127.0.0.1:50000 这类相邻端口很常见
    （Zotero 插件就占 50000），子串匹配会把别人误判成"已启动"。
    这里直接请求首页并校验页面特征串。
    ProxyHandler({}) 必须带：本机 HTTP_PROXY 指向本地代理客户端，
    客户端没运行时走代理会直接 ProxyError，探测永远失败。
    """
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=timeout) as resp:
            body = resp.read(16384)
        return b"MicroBench" in body
    except Exception:
        return False


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(0.8)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_ready(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe(f"http://127.0.0.1:{port}/", timeout=1.5):
            return True
        time.sleep(0.3)
    return False


# --------------------------------------------------------------------------
# 服务启动
# --------------------------------------------------------------------------
def _start_server_in_thread(port: int):
    """在后台线程跑 uvicorn（daemon 线程：主进程退出即随之结束）。"""
    import uvicorn

    from microbench.app import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    threading.Thread(
        target=server.run, name="microbench-uvicorn", daemon=True
    ).start()
    return server


def _start_server_detached(port: int) -> bool:
    """独立进程起服务（Edge --app 兜底路线用）。

    这条路线里启动器进程会马上退出，若服务跑在同进程线程里就会一起死，
    所以必须 detach 出去，让服务在窗口关闭后依然活着。
    """
    flags = 0
    if os.name == "nt":
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    # pythonw.exe 没有真实 stdio，uvicorn 的日志处理器会拿到 None。
    # 同一目录下的 python.exe 才是给子进程用的正确解释器。
    exe = sys.executable
    if os.name == "nt" and exe.lower().endswith("pythonw.exe"):
        sibling = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(sibling):
            exe = sibling
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        logfh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        logfh = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [
                exe,
                "-m",
                "uvicorn",
                "microbench.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=APP_DIR,
            stdout=logfh,
            stderr=logfh,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as exc:  # pragma: no cover - 环境相关
        _log(f"[X] 服务进程启动失败: {exc}")
        return False
    return True


# --------------------------------------------------------------------------
# 窗口位置记忆
# --------------------------------------------------------------------------
def _virtual_bounds() -> tuple[int, int, int, int]:
    """虚拟桌面范围（多显示器时是所有屏幕的并集）。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        return (
            user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
            user32.GetSystemMetrics(77),  # SM_YVIRTUALSCREEN
            user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
            user32.GetSystemMetrics(79),  # SM_CYVIRTUALSCREEN
        )
    except Exception:
        return (0, 0, 1920, 1080)


def _load_window_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_window_state(window) -> None:
    try:
        state = {
            "x": int(window.x),
            "y": int(window.y),
            "width": int(window.width),
            "height": int(window.height),
        }
    except Exception:
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except Exception:
        pass


def _restore_geometry() -> dict:
    """读上次的窗口位置；越界（换过显示器/分辨率）就丢弃，用默认值。"""
    state = _load_window_state()
    if not state:
        return {}
    try:
        x = int(state["x"])
        y = int(state["y"])
        width = max(MIN_WIDTH, int(state["width"]))
        height = max(MIN_HEIGHT, int(state["height"]))
    except Exception:
        return {}
    vx, vy, vw, vh = _virtual_bounds()
    if vw <= 0 or vh <= 0:
        return {}
    # 标题栏要留在屏幕内，否则窗口会"消失"
    if not (vx <= x < vx + vw - 120 and vy <= y < vy + vh - 60):
        return {}
    return {"x": x, "y": y, "width": width, "height": height}


# --------------------------------------------------------------------------
# 单实例：已经开着一个应用窗口时，第二次双击不再开新窗口，改为把已有窗口顶到前台
# （标准桌面应用行为）。没有这一步的话会出现"两个窗口、其中一个关掉就把服务带走"。
# --------------------------------------------------------------------------
WINDOW_TITLE_MARK = "微电子科研工作台"


def _find_existing_app_window() -> int | None:
    try:
        import ctypes
        import ctypes.wintypes as wt
    except Exception:
        return None
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        # 只认应用窗口，不认 "MicroBench Server - python -m uvicorn ..." 那个控制台
        if WINDOW_TITLE_MARK in buf.value:
            found.append(hwnd)
        return True

    try:
        user32.EnumWindows(cb, 0)
    except Exception:
        return None
    return found[0] if found else None


def _focus_window(hwnd: int) -> None:
    """把已有窗口顶到前台（SetForegroundWindow 跨进程经常失败，要借线程输入队列）。"""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_thread and fg_thread != cur:
            attached = bool(user32.AttachThreadInput(cur, fg_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur, fg_thread, False)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 浏览器兜底：Edge / Chrome 的 --app 模式
# --------------------------------------------------------------------------
def _find_browser() -> str | None:
    candidates: list[str] = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates += [
            os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
        ]
    if os.name == "nt":
        try:
            import winreg

            for hive, key in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        candidates.append(str(winreg.QueryValue(k, None)))
                except OSError:
                    pass
        except Exception:
            pass
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _open_browser_app(url: str, width: int, height: int) -> bool:
    """用 --app 模式开一个没有地址栏/标签页的独立窗口。"""
    exe = _find_browser()
    if not exe:
        return False
    try:
        subprocess.Popen(
            [
                exe,
                f"--app={url}",
                f"--window-size={width},{height}",
                "--new-window",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            cwd=APP_DIR,
        )
    except Exception as exc:
        _log(f"[X] 启动浏览器兜底失败: {exc}")
        return False
    _log(f"[i] 已用浏览器 --app 模式打开窗口: {exe}")
    return True


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _ensure_pywebview():
    """返回 webview 模块；缺失时尝试静默装一次（已在 requirements.txt 声明）。"""
    try:
        import webview  # noqa: F401

        return webview
    except Exception:
        pass
    _log("[i] 未检测到 pywebview，正在安装 ...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pywebview>=4.0", "--quiet", "--disable-pip-version-check"],
            cwd=APP_DIR,
            timeout=300,
        )
    except Exception as exc:
        _log(f"[X] pywebview 安装失败: {exc}")
        return None
    try:
        import webview  # noqa: F811

        return webview
    except Exception as exc:
        _log(f"[X] pywebview 仍不可用: {exc}")
        return None


def _run_webview(url: str, width: int, height: int, on_close=None) -> bool:
    """原生应用窗口。返回 False 表示这条路走不通，交给兜底。"""
    webview = _ensure_pywebview()
    if webview is None:
        return False

    geo = _restore_geometry()
    try:
        window = webview.create_window(
            title=APP_TITLE,
            url=url,
            width=geo.get("width", width),
            height=geo.get("height", height),
            x=geo.get("x"),
            y=geo.get("y"),
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            resizable=True,
            frameless=False,
            easy_drag=False,
            on_top=False,
            confirm_close=False,
            text_select=True,   # 划词批注必须能选中文字
            zoomable=False,
            draggable=False,
            background_color="#f5f1ea",
        )
    except Exception as exc:
        _log(f"[X] 创建应用窗口失败: {exc}")
        return False

    if window is None:
        _log("[X] pywebview 未能创建窗口")
        return False

    # pywebview 的事件派发规则（webview/event.py）：
    #   形参为 0 个      → 无参调用
    #   形参名含 window  → 传入窗口对象
    #   其它             → 按触发时给的 args 调用（loaded/closing 的 args 为空）
    # 所以形参**必须**叫 window，否则会因参数个数对不上抛 TypeError。
    def _on_loaded(window):
        try:
            _save_window_state(window)
        except Exception:
            pass

    def _on_closing(window):
        try:
            _save_window_state(window)
        except Exception:
            pass
        if on_close is not None:
            try:
                on_close()
            except Exception:
                pass
        return None

    try:
        window.events.loaded += _on_loaded
        window.events.closing += _on_closing
    except Exception:
        pass

    try:
        webview.start(
            icon=ICON_PATH if os.path.exists(ICON_PATH) else None,
            private_mode=False,       # localStorage 要跨次启动保留
            storage_path=PROFILE_DIR,
        )
    except Exception as exc:
        _log(f"[X] 应用窗口运行失败: {exc}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MicroBench 桌面应用启动器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="后端服务端口（默认 5000）")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help="窗口宽度")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help="窗口高度")
    parser.add_argument("--attach", action="store_true", help="只连已运行的服务，不自己起")
    parser.add_argument("--web", action="store_true", help="强制用浏览器 --app 模式打开（不装 pywebview 也能用）")
    parser.add_argument(
        "--open",
        default="",
        help="启动后直接打开某篇论文（vault 相对路径，如 01_Literature/.../xxx.pdf）",
    )
    args = parser.parse_args(argv)

    _setup_streams()
    port = args.port
    base_url = f"http://127.0.0.1:{port}"
    _log(f"[MicroBench] 桌面应用启动器 · 端口 {port}")

    server = None
    owns_server = False

    if _probe(base_url + "/"):
        _log(f"[i] 检测到工作台已在运行（{base_url}），直接连上去")
        existing = _find_existing_app_window()
        if existing:
            _log("[i] 已存在应用窗口，改为把它顶到前台（单实例）")
            _focus_window(existing)
            return 0
    elif _port_open(port):
        _log(f"[X] 端口 {port} 已被其它程序占用，且不是 MicroBench")
        _message_box(
            "MicroBench 无法启动",
            f"端口 {port} 被其它程序占用了。\n\n"
            f"请先关掉占用该端口的程序（常见：另一个 MicroBench 服务实例），再重新打开。",
        )
        return 1
    elif args.attach:
        _log("[X] --attach 模式下服务未运行")
        _message_box("MicroBench 无法启动", "工作台服务没有在运行，请先启动服务再打开应用窗口。")
        return 1
    else:
        _log("[i] 正在启动本地服务 ...")
        server = _start_server_in_thread(port)
        owns_server = True
        if not _wait_ready(port):
            _log("[X] 服务启动超时")
            _message_box(
                "MicroBench 无法启动",
                f"本地服务在 45 秒内没有就绪（端口 {port}）。\n\n"
                "请查看 logs/desktop.log 里的具体报错。",
            )
            return 1
        _log("[i] 服务已就绪")

    # cache-bust：WebView2 对 index.html 缓存得很凶，加个随机参数保证拿到最新版
    url = f"{base_url}/?v={os.urandom(4).hex()}"
    if args.open:
        from urllib.parse import quote

        url += "&open_pdf=" + quote(args.open.replace("\\", "/"), safe="")

    def _shutdown():
        if owns_server and server is not None:
            try:
                server.should_exit = True
                _log("[i] 正在停止服务 ...")
            except Exception:
                pass

    if not args.web:
        if _run_webview(url, args.width, args.height, on_close=_shutdown):
            _log("[MicroBench] 应用窗口已关闭")
            _shutdown()
            return 0
        _log("[!] 原生窗口不可用，改用浏览器 --app 模式")

    # 兜底：独立进程起服务（启动器进程要能马上退出）+ 浏览器 --app 窗口
    if owns_server and server is not None:
        try:
            server.should_exit = True
        except Exception:
            pass
        server = None
        owns_server = False
        if not _start_server_detached(port):
            _message_box("MicroBench 无法启动", "本地服务启动失败，请查看 logs/desktop.log。")
            return 1
        if not _wait_ready(port):
            _message_box("MicroBench 无法启动", "本地服务启动超时，请查看 logs/desktop.log。")
            return 1

    if not _open_browser_app(url, args.width, args.height):
        _message_box(
            "MicroBench 无法启动",
            "既没有可用的 pywebview，也找不到 Edge / Chrome 来打开应用窗口。\n\n"
            "请安装 pywebview：pip install pywebview",
        )
        return 1

    _log("[MicroBench] 已在独立窗口中打开（服务在后台运行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
