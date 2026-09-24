"""
MicroBench Desktop Launcher

启动 FastAPI/uvicorn 后端在后台线程,然后用 Edge WebView2 打开一个原生窗口。
双击本文件即可(假设已 pip install pywebview)。

用法:
  python desktop.py
  python desktop.py --port 8765
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webview

from microbench.app import app


def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
    """阻塞等待端口响应,返回 True 表示服务已就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            # 0.3s 轮询在 uvicorn 启动 ~2s 期间浪费最多 6 次睡眠
            # 改 0.05s 后端口 listen 立刻命中,首屏更快
            time.sleep(0.05)
    return False


def _start_server(port: int) -> None:
    """在后台线程启动 uvicorn (daemon=True,主进程退出即终止)。"""
    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    server.run()


def _initial_focus(window) -> None:
    """
    后台线程: webview 窗口启动后 2 秒调用 activate() 抢焦点,
    5 秒后取消 always-on-top,让用户正常使用 (不会永久盖住其他窗口)。
    """
    try:
        time.sleep(2.0)
        if hasattr(window, "activate"):
            window.activate()
    except Exception:
        pass
    try:
        time.sleep(3.0)
        if hasattr(window, "on_top"):
            window.on_top = False
    except Exception:
        pass


SPLASH_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>MicroBench</title>
<style>
  body { margin:0; height:100vh; background:#f5f1ea; display:flex;
         align-items:center; justify-content:center;
         font-family: 'Cormorant Garamond', 'Times New Roman', serif; color:#222; }
  .wrap { text-align:center; }
  h1 { font-size: 56px; font-weight:300; margin:0 0 8px;
       letter-spacing: 0.02em; }
  h1 em { color:#a8511f; font-style:italic; }
  p { font-size: 14px; color:#666; margin: 18px 0 0; letter-spacing: 0.1em;
     text-transform: uppercase; font-family: 'Plus Jakarta Sans', sans-serif; }
  .dot { display:inline-block; width:6px; height:6px; margin:0 3px;
         background:#a8511f; border-radius:50%;
         animation: pulse 1.4s infinite ease-in-out; }
  .dot:nth-child(2) { animation-delay: 0.2s; }
  .dot:nth-child(3) { animation-delay: 0.4s; }
  @keyframes pulse { 0%,100% { opacity:0.2; } 50% { opacity:1; } }
</style></head>
<body><div class="wrap">
  <h1>Micro<em>Bench</em></h1>
  <p>Loading<span class="dot"></span><span class="dot"></span><span class="dot"></span></p>
</div></body></html>"""


def _splash_switch(window, target_url: str, delay: float = 0.15) -> None:
    """后台线程: 等 webview ready 后切到主 URL(避免白屏)。"""
    try:
        time.sleep(delay)
        if hasattr(window, "load_url"):
            window.load_url(target_url)
    except Exception:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="MicroBench 桌面启动器")
    parser.add_argument("--port", type=int, default=5000, help="后端服务端口 (默认 5000)")
    parser.add_argument("--width", type=int, default=1440, help="窗口宽度")
    parser.add_argument("--height", type=int, default=900, help="窗口高度")
    args = parser.parse_args()

    port = args.port

    print(f"[MicroBench] 启动后端服务 127.0.0.1:{port} ...")
    t = threading.Thread(target=_start_server, args=(port,), daemon=True, name="uvicorn-server")
    t.start()

    if not _wait_for_server(port):
        print("[MicroBench] 后端启动超时 (15s),请检查 5000 端口是否被占用", file=sys.stderr)
        return 1

    print(f"[MicroBench] 后端就绪,打开桌面窗口 ({args.width}x{args.height}) ...")
    # Cache-bust the URL so each restart of MicroBench forces Edge WebView2
    # to fetch the freshest static/index.html. Without this, the embedded
    # webview caches the page aggressively and any code patch the user
    # makes between sessions isn't picked up until they manually refresh.
    import secrets
    target_url = f"http://127.0.0.1:{port}/?v={secrets.token_hex(4)}"
    window = webview.create_window(
        title="MicroBench · 微电子科研工作台",
        url="data:text/html;charset=utf-8," + SPLASH_HTML.replace("\n", "").replace("  ", ""),
        width=args.width,
        height=args.height,
        min_size=(1024, 700),
        resizable=True,
        text_select=True,
        confirm_close=False,
        on_top=True,
    )
    # 后台线程: 启动 splash → 150ms 后切到主 URL (原 400ms, 砍掉 250ms 硬延迟)
    threading.Thread(target=_splash_switch, args=(window, target_url, 0.15), daemon=True).start()
    # 后台线程: 激活窗口 + 5 秒后取消置顶
    threading.Thread(target=_initial_focus, args=(window,), daemon=True).start()
    try:
        webview.start()
    except KeyboardInterrupt:
        pass
    print("[MicroBench] 窗口关闭,后端服务随之停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())