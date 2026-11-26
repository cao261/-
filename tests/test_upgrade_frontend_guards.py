# -*- coding: utf-8 -*-
"""桌面前端全量升级 · 真实浏览器端到端行为护栏。

启动真实 uvicorn + sandbox 沙箱，使用 Playwright Chromium 验证：
1. 所有 5 份 CSS 与 14 份 ES 模块均以 HTTP 200 与正确 MIME 类型加载，无外部字体 CDN 请求；
2. 浅色 / 夜间主题切换与多视图切换（总览工作台 ↔ 文献图书馆 ↔ 实验室与同步）全程零 console/page 错误；
3. 文献图书馆基于 SQLite 检索并可就地修改笔记精读状态（同步写回磁盘 YAML 与索引）；
4. 快捷键面板 `?` 与 `Escape` 统一栈行为正常。
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from microbench import app as app_mod


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _minimal_pdf_bytes() -> bytes:
    try:
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=400, height=500)
        page.insert_text((40, 72), "GaN HEMT 2DEG Mobility Benchmark")
        return doc.tobytes()
    except Exception:
        return (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 200 200]/Parent 2 0 R>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
            b"0000000056 00000 n \n0000000111 00000 n \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n178\n%%EOF\n"
        )


def test_desktop_modular_ui_e2e(sandbox: Path):
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright

    # 造一组沙箱论文原文与笔记，保证图书馆、总览与 YAML 写回均走真实数据链
    lit_pdf_dir = sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" / "01_论文原文_PDF"
    lit_note_dir = sandbox / "01_Literature" / "01_Device_TCAD_器件仿真" / "02_精读笔记"
    lit_pdf_dir.mkdir(parents=True, exist_ok=True)
    lit_note_dir.mkdir(parents=True, exist_ok=True)

    (lit_pdf_dir / "2026_Test_GaN_HEMT.pdf").write_bytes(_minimal_pdf_bytes())
    note_file = lit_note_dir / "2026_Test_GaN_HEMT.md"
    note_file.write_text(
        "---\n"
        "title: 2026_Test_GaN_HEMT\n"
        "status: 待精读\n"
        "tags: [TCAD, GaN]\n"
        "---\n\n"
        "# 核心结论\n\n二维电子气迁移率实测提升 18%。\n",
        encoding="utf-8",
    )

    port = _free_port()
    config = uvicorn.Config(app_mod.app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn test server failed to start"

    console_errors: list[str] = []
    page_errors: list[str] = []
    external_font_reqs: list[str] = []
    static_responses: dict[str, tuple[int, str]] = {}
    unexpected_http_errors: list[tuple[int, str]] = []
    snapshot_miss_404 = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            pg = ctx.new_page()

            pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: page_errors.append(str(e)))
            pg.on("request", lambda r: external_font_reqs.append(r.url)
                  if ("fonts.googleapis.com" in r.url or "fonts.gstatic.com" in r.url) else None)

            def _on_response(r):
                nonlocal snapshot_miss_404
                if "/static/desktop/" in r.url:
                    static_responses[r.url.split(base_url, 1)[-1]] = (
                        r.status, (r.headers.get("content-type") or "").lower()
                    )
                if r.status >= 400:
                    if "/api/pdf/snapshot/load" in r.url and r.status == 404:
                        snapshot_miss_404 += 1
                    else:
                        unexpected_http_errors.append((r.status, r.url))

            pg.on("response", _on_response)

            pg.goto(base_url + "/", wait_until="networkidle", timeout=30000)

            # 1. 零外部 Google Fonts 请求；桌面 CSS / JS 模块全部 HTTP 200 且 MIME 正确
            assert not external_font_reqs, f"仍有外部 Google Fonts 请求: {external_font_reqs}"
            assert "/static/desktop/styles/00-tokens.css" in static_responses
            assert "/static/desktop/js/main.js" in static_responses
            assert "/static/desktop/js/views/library.js" in static_responses
            assert "/static/desktop/js/views/labs.js" in static_responses
            assert "/static/desktop/js/reader/shortcuts.js" in static_responses
            for path_key, (status, ctype) in static_responses.items():
                assert status == 200, f"模块加载失败: {path_key} -> {status}"
                if path_key.endswith(".css"):
                    assert "text/css" in ctype, f"CSS MIME 异常: {path_key} -> {ctype}"
                elif path_key.endswith(".js"):
                    assert "javascript" in ctype, f"JS MIME 异常: {path_key} -> {ctype}"

            # 2. 浅色 / 夜间主题切换正常
            assert pg.get_attribute("html", "data-theme") in ("light", "night")
            pg.click("#theme-toggle")
            t1 = pg.get_attribute("html", "data-theme")
            pg.click("#theme-toggle")
            t2 = pg.get_attribute("html", "data-theme")
            assert t1 != t2, "主题切换未改变 html[data-theme]"

            # 3. 切到「文献图书馆」：能搜到沙箱笔记 + 原文，并可在线改写 paper_status
            pg.click('button[data-wb-tab="library"]')
            pg.wait_for_selector("#view-library .lib-card", timeout=10000)
            lib_text = pg.inner_text("#lib-results-grid")
            assert "2026_Test_GaN_HEMT" in lib_text

            note_sel = 'select.lib-status-sel[data-lib-status$="2026_Test_GaN_HEMT.md"]'
            pg.wait_for_selector(note_sel, timeout=5000)
            pg.select_option(note_sel, "已精读")
            pg.wait_for_function(
                """() => {
                    const el = document.querySelector('#lib-results-grid select.lib-status-sel');
                    return el && el.value === '已精读';
                }""",
                timeout=5000,
            )
            # 给磁盘异步刷新一点时间，校验真实 YAML 已更新
            time.sleep(0.3)
            note_disk = note_file.read_text(encoding="utf-8")
            assert 'status: "已精读"' in note_disk or "status: 已精读" in note_disk

            # 4. 切到「实验室与同步」：展示健康监控状态
            pg.click('button[data-wb-tab="labs"]')
            pg.wait_for_function(
                "() => (document.getElementById('labs-health-kv')?.innerText || '').includes('ok')",
                timeout=10000,
            )

            # 5. `?` 快捷键弹出导览，Escape 关闭
            pg.keyboard.press("Shift+/")
            pg.wait_for_selector('#shortcuts-modal[style*="flex"]', timeout=5000)
            pg.keyboard.press("Escape")
            pg.wait_for_function(
                "() => getComputedStyle(document.getElementById('shortcuts-modal')).display === 'none'",
                timeout=5000,
            )

            # 6. 从文献图书馆打开双栏 PDF 阅读器并确认 PDF.js 渲染与 Esc 关闭正常
            pg.click('button[data-wb-tab="library"]')
            pg.click('button[data-lib-open-pdf$="2026_Test_GaN_HEMT.pdf"]')
            pg.wait_for_selector("#pdf-page-block-1", timeout=20000)
            pg.keyboard.press("Escape")
            pg.wait_for_function(
                "() => getComputedStyle(document.getElementById('reader-modal')).display === 'none'",
                timeout=5000,
            )

            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)

    assert not unexpected_http_errors, f"页面请求出现意外 HTTP 错误: {unexpected_http_errors}"
    assert not page_errors, f"页面出现未捕获 JS 异常: {page_errors}"
    # 浏览器会对 /api/pdf/snapshot/load 未命中快照的 404 自动打印一条控制台消息，其它 error 一律判红
    non_snapshot_errors = [e for e in console_errors if "status of 404" not in e]
    assert not non_snapshot_errors, f"浏览器控制台出现 error: {non_snapshot_errors}"
    assert len(console_errors) <= snapshot_miss_404, f"存在意外 404 控制台错误: {console_errors}"
