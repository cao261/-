# -*- coding: utf-8 -*-
"""2026-10-27 代码审计 · **Android 原生壳（Java）**的回归护栏。

这一组测试锁住本轮 7 处修复。每条都能**破坏性验证**：把对应的修复改回去，
这组测试立刻红。

对应审计条目：

| 护栏 | 挡住的回归 |
|---|---|
| test_c1_* | C-1 WebView 离开 appassets 源后仍持有原生桥（恶意页可删/写 vault、当 HTTPS 代理） |
| test_c1_httppost_* | C-1 原生 httpPost 是开放代理（只校验 https:// 前缀） |
| test_h1_* | H-1 AI 代理与云盘写入共用单线程池（保存笔记被堵 ≤150s 且静默失败） |
| test_h3_* | H-3 SettingsActivity 把同步根初始化成 vault 根（译文/批注不与电脑端共享） |
| test_m1_* | M-1 导出 vault 包 / 变更包在 APK 里点不动（无 DownloadListener） |
| test_m2_* | M-2 单次下载 128MB 上限 → 平板 OOM（应分档 + 流式） |
| test_l2_* | L-2 vaultRoot 未过滤 `..`（手填路径可越出 vault 根） |
| test_l3_* | L-3 readLine() 无行长上限（恶意 DAV 服务端可撑爆内存） |

⚠ 静态断言抄的是 tests/test_apk_delivery.py 的模式（那个文件就是用
  静态断言检查 Java / manifest 的）；行为断言则**真的把 Java 跑起来**
  （javac 编译 + 假 WebDAV 服务器对拍，见 android/_test_dav.py 的做法）。

跑法:
    python -m pytest tests/test_audit_2026_10_27_android.py -q
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import socketserver
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ANDROID = REPO / "android"
JAVA_DIR = ANDROID / "java" / "microbench" / "tablet"
STUB_DIR = ANDROID / "_stub"
TABLET_HTML = REPO / "microbench" / "static" / "tablet" / "index.html"


def _java(name: str) -> str:
    return (JAVA_DIR / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    """去掉**整行注释**后的 Java 源码。

    ⚠ 断言必须针对代码，不能被注释里的旧代码骗过 ——
      （注释里写「原来是 `String sync = vault;`」就会让断言假红。）
      只去掉整行注释，行尾注释保留（不会误伤 URL 里的 "//"）。
    """
    out = []
    in_block = False
    for line in _java(name).splitlines():
        s = line.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# C-1 —— WebView 不能带着原生桥去加载外域页面
# ---------------------------------------------------------------------------

def test_c1_main_activity_blocks_external_navigation():
    """外链必须交给系统浏览器，**绝不能**在 WebView 里加载。

    ⚠ 触发路径（实测可达）：笔记 / AI 答案里的 `[点我](https://evil)` →
      <a> 没有 target、WebView 又没有 shouldOverrideUrlLoading →
      顶层导航到外域 → 恶意页拿到 window.MicroBench →
      delete("01_Literature/x.pdf") 删原文、put(path, text) 往 vault 写内容
      （会同步回电脑端，污染用户笔记）、httpPost 把平板当跳板打用户内网。
    """
    src = _code("MainActivity.java")
    assert "shouldOverrideUrlLoading" in src, \
        "MainActivity 没有 shouldOverrideUrlLoading —— 外链会直接在 WebView 里打开"
    assert "WebResourceRequest" in src, \
        "要用 WebResourceRequest 版本的 shouldOverrideUrlLoading（API 24+）"
    assert "isForMainFrame" in src, \
        "必须区分主框架与 iframe（addJavascriptInterface 注入的对象在每个 frame 里都有）"
    assert "Intent.ACTION_VIEW" in src, \
        "外链要交给系统浏览器（Intent.ACTION_VIEW）"
    assert "isAppSource" in src, \
        "必须有一个「自己人」的判定（只有 appassets 源留在 WebView 里）"
    # 方法体里必须 return true（= 拦下来，不加载）
    idx = src.find("public boolean shouldOverrideUrlLoading")
    assert idx > 0, "找不到 shouldOverrideUrlLoading 的方法体"
    body = src[idx: idx + 1800]
    assert "return true;" in body, \
        "shouldOverrideUrlLoading 必须 return true 拦住外链"
    assert "openExternally" in body, \
        "主框架的外链要交给系统浏览器，而不是直接丢掉"


def test_c1_app_source_is_restricted_to_appassets():
    """留在 WebView 里的源只能是 appassets（不能是任意 http/https）。"""
    src = _code("MainActivity.java")
    assert "APP_DOMAIN" in src, "必须有 APP_DOMAIN 常量"
    seg = src.split("private static boolean isAppSource", 1)[1]
    seg = seg.split("\n    private ", 1)[0]
    assert "APP_DOMAIN" in seg, \
        "isAppSource 必须按 APP_DOMAIN 判定（不能放行任意域名）"
    for bad in ('"http"', "'http'", "startsWith(\"https://\")"):
        assert bad not in seg, f"isAppSource 里不该出现 {bad}（那等于放行外域）"
    # data: 页面也是「被加载的页面」→ 原生桥会注入进去 → 不能放行
    assert '"data"' not in seg and '"data:"' not in seg, \
        "isAppSource 不能放行 data: URL（原生桥会注入到那个页面里）"


def test_c1_tablet_page_has_csp_meta():
    """平板页面必须有 CSP meta（第二道防线）。

    ⚠ 关键项：
      - frame-src 'none'  → 不许任何 iframe（原生桥在每个 frame 里都有）
      - worker-src blob:  → PDF.js 的 worker 是 Blob URL 起来的
      - object-src 'none' / base-uri 'none'
    """
    html = TABLET_HTML.read_text(encoding="utf-8")
    assert "http-equiv=\"Content-Security-Policy\"" in html, \
        "microbench/static/tablet/index.html 没有 CSP meta"
    m = [ln for ln in html.splitlines() if "Content-Security-Policy" in ln]
    assert m, "找不到 CSP meta 行"
    csp = m[0]
    for token in ("default-src 'self'", "frame-src 'none'", "worker-src 'self' blob:",
                  "object-src 'none'", "base-uri 'none'"):
        assert token in csp, f"CSP 少了 {token}"
    # CSP 必须放在 <head> 里、且在脚本之前
    head_end = html.lower().find("</head>")
    assert html.find("Content-Security-Policy") < head_end, "CSP meta 必须在 <head> 里"
    first_script = html.lower().find("<script")
    assert first_script == -1 or html.find("Content-Security-Policy") < first_script, \
        "CSP meta 必须在第一个 <script> 之前"


def test_c1_httppost_has_host_allowlist():
    """原生 httpPost 的目标主机必须在**白名单**里（不是只校验 https://）。"""
    bridge = _code("DavBridge.java")
    prefs = _code("Prefs.java")
    assert "isAiHostAllowed" in bridge, \
        "httpPost 必须校验目标主机白名单（只校验 https:// = 开放代理）"
    assert "hostOf" in bridge, "httpPost 必须先取出主机名再判定"
    assert "DEFAULT_ALLOWED_HOSTS" in prefs, "白名单必须有一份明确的常量"
    # 白名单至少要含坚果云 + 桌面端在用的模型网关
    for host in ("dav.jianguoyun.com", "api.minimaxi.com", "apihub.agnes-ai.com"):
        assert host in prefs, f"白名单里少了 {host}"
    # 内网 / IP 字面量必须被拒（不然还是能把平板当跳板）
    assert "isPublicHostName" in prefs, "必须有「公网 DNS 名」的判定"
    assert "isIpv4Literal" in prefs, "必须拒掉 IP 字面量"


# ---------------------------------------------------------------------------
# H-1 —— AI 代理与云盘写入必须分池
# ---------------------------------------------------------------------------

def test_h1_dav_bridge_has_separate_pools():
    """DAV 写操作与 AI 请求必须**两个池**。"""
    src = _code("DavBridge.java")
    assert "POOL_DAV" in src, "缺少 DAV 池"
    assert "POOL_AI" in src, "缺少 AI 池（AI 与 DAV 共用一个池 = 保存笔记被 AI 堵住）"
    assert "newSingleThreadExecutor" in src, \
        "DAV 池必须单线程（写操作要保序，覆盖写不能乱序）"
    assert "newFixedThreadPool(2)" in src, \
        "AI 池必须是多线程（最多 2 个并发）"
    assert "POOL.execute" not in src, \
        "还在用那个共用单线程池 POOL —— AI 一跑，保存笔记全排在后面"


def test_h1_dav_writes_use_dav_pool_and_ai_uses_ai_pool():
    """逐个方法核对：写走 DAV 池，AI 走 AI 池。"""
    src = _code("DavBridge.java")

    def body_of(sig: str) -> str:
        seg = src.split(sig, 1)[1]
        return seg.split("\n    }", 1)[0]

    for sig, pool in (("public void put(", "POOL_DAV"),
                      ("public void delete(", "POOL_DAV"),
                      ("public void putSync(", "POOL_DAV"),
                      ("public void httpPost(", "POOL_AI")):
        seg = body_of(sig)
        assert pool in seg, f"{sig.strip()} 应该用 {pool}，实际：{seg.strip()[:120]}"


# ---------------------------------------------------------------------------
# H-3 —— 设置页不能把同步根初始化成 vault 根
# ---------------------------------------------------------------------------

def test_h3_sync_root_is_not_initialized_to_vault_root():
    """同步根的初值必须是空串（只在探测成功时才赋值）。

    ⚠ 原来写的是 `String sync = vault;` → 用户手填 vault 位置时 sync 非空
      → 探测永不执行 → 同步根被写成 vault 根 →
      平板从 <vault>/snapshots 找译文（空的）、把批注写进 <vault>/annotations
      （不是共享目录）→ 电脑端看不到，vault 里还多出垃圾目录。
    """
    src = _code("SettingsActivity.java")
    assert "String sync = vault;" not in src, \
        "同步根不能初始化成 vault 根（探测就永远不会执行）"
    assert 'String sync = "";' in src, \
        "同步根的初值必须是空串（只在探测成功时才赋值）"
    # saveIfOk：同步根无效就不覆盖旧值
    seg = src.split("void saveIfOk", 1)[1].split("\n    }", 1)[0]
    assert "setDavSyncRoot" in seg, "saveIfOk 要写同步根"
    assert "syncRoot.trim().isEmpty()" in seg or "!syncRoot.isEmpty()" in seg, \
        "saveIfOk 必须在同步根无效时**不覆盖**旧值"


def test_h3_sync_root_is_detected_not_guessed():
    """同步根必须靠探测（detectSyncRoot）得到，不能拿 vault 顶替。"""
    src = _code("SettingsActivity.java")
    assert "detectSyncRoot" in src, "同步根必须靠 detectSyncRoot 探测"


# ---------------------------------------------------------------------------
# M-1 —— 导出包要能下载
# ---------------------------------------------------------------------------

def test_m1_main_activity_has_download_listener():
    """APK 里点「导出」不能什么都不发生。"""
    src = _code("MainActivity.java")
    assert "setDownloadListener" in src, \
        "MainActivity 没有 setDownloadListener —— 导出 vault 包 / 变更包点不动"
    assert "DownloadListener" in src, "必须实现 DownloadListener"
    assert "blob:" in src, "blob: 下载要处理（导出走的就是 URL.createObjectURL）"
    assert "MicroBenchDownload" in src, \
        "blob: 的内容只有页面读得出来 —— 需要一个 JS 桥把它交回来"
    assert "DIRECTORY_DOWNLOADS" in src, "下载要落到下载目录（并给用户可见反馈）"
    assert "Toast" in src or "toast(" in src, "下载完要给用户看得见的反馈"


# ---------------------------------------------------------------------------
# M-2 —— 下载上限要分档，且大文件走流式
# ---------------------------------------------------------------------------

def test_m2_dav_client_has_tiered_limits():
    """单次下载上限必须分档（进内存的收紧，流式的另算）。"""
    src = _code("DavClient.java")
    assert "MAX_BUFFERED_BYTES" in src, "缺少「进内存」那一档的上限"
    assert "MAX_STREAM_BYTES" in src, "缺少「走流式」那一档的上限"
    assert "128L * 1024 * 1024" not in src, \
        "128 MB 的单档上限还在（平板扛不住一个 128MB 的 byte[]）"
    assert "openFile" in src, "缺少流式下载接口（大文件不能整份读进内存）"
    assert "DavStream" in src, "缺少流式响应对象"


def test_m2_dav_path_handler_uses_streaming():
    """DavPathHandler 必须走流式（它以前是 byte[] 再包 ByteArrayInputStream）。"""
    src = _code("DavPathHandler.java")
    assert "openFile" in src, "DavPathHandler 必须用 openFile（流式）"
    assert "c.get(path)" not in src, \
        "DavPathHandler 还在把整份文件读进 byte[]（平板 OOM）"


# ---------------------------------------------------------------------------
# L-2 —— vault 前缀里的 `..` 必须被过滤
# ---------------------------------------------------------------------------

def test_l2_vault_root_filters_dot_segments():
    """手填的 vault 前缀 / 同步根里的 `.` / `..` 必须被丢掉。"""
    client = _code("DavClient.java")
    prefs = _code("Prefs.java")
    # ⚠ 必须**逐个函数**查 —— 整文件里搜会出现假绿
    #   （appendPath 一直就过滤 ..，不能拿它冒充 normalizeVaultRoot 也过滤了）。
    norm = client.split("private static String normalizeVaultRoot", 1)[1]
    norm = norm.split("public void setVaultRoot", 1)[0]
    assert '"..".equals(seg)' in norm, \
        "DavClient 归一化 vault 前缀时必须过滤 .. 段（否则能越出 vault 根）"
    urlfor = client.split("public String urlFor", 1)[1]
    urlfor = urlfor.split("public String rawUrlFor", 1)[0]
    assert '"..".equals(seg)' in urlfor, "urlFor 拼 vault 前缀时也要过滤 .. 段"
    pnorm = prefs.split("private static String normalizeRoot", 1)[1]
    pnorm = pnorm.split("public String davSyncRoot", 1)[0]
    assert '"..".equals(seg)' in pnorm, \
        "Prefs 归一化路径时必须过滤 .. 段"


# ---------------------------------------------------------------------------
# L-3 —— readLine 要有行长上限
# ---------------------------------------------------------------------------

def test_l3_readline_has_length_limit():
    """readLine 必须有行长上限（恶意 DAV 服务端可发无换行长行）。"""
    src = _code("DavClient.java")
    assert "MAX_LINE_BYTES" in src, "缺少行长上限"
    assert "过长的行" in src, "超长行要给出明确错误（而不是把内存吃光）"
    seg = src.split("private static String readLine", 1)[1].split("\n    }", 1)[0]
    assert "MAX_LINE_BYTES" in seg, "readLine 里必须真的用上限判定"


# ---------------------------------------------------------------------------
# 行为断言：真的把 Java 跑起来
# ---------------------------------------------------------------------------

def _find_jdk() -> Path | None:
    """找一个可用的 JDK（找不到就 SKIP —— 环境问题不该让套件红）。"""
    cands = []
    for env in ("MB_JDK", "JAVA_HOME"):
        v = os.environ.get(env)
        if v:
            cands.append(Path(v))
    cands.append(Path(r"D:\Desktop\杂物\z\Zotero\jdk"))
    for base in (r"C:\Program Files\Java", r"C:\Program Files\Eclipse Adoptium",
                 r"C:\Program Files\Microsoft\jdk", r"D:\Android\Sdk"):
        p = Path(base)
        if p.is_dir():
            try:
                for child in sorted(p.iterdir()):
                    cands.append(child)
            except OSError:
                pass
    w = shutil.which("javac")
    if w:
        cands.append(Path(w).parent.parent)
    for c in cands:
        if (c / "bin" / "javac.exe").is_file() and (c / "bin" / "java.exe").is_file():
            return c
    return None


def _find_android_jar() -> Path | None:
    for base in (r"D:\Android\Sdk", os.environ.get("ANDROID_HOME", ""),
                 os.environ.get("ANDROID_SDK_ROOT", "")):
        if not base:
            continue
        plat = Path(base) / "platforms"
        if not plat.is_dir():
            continue
        jars = sorted(plat.glob("android-*/android.jar"))
        if jars:
            return jars[-1]
    return None


def _decode(raw: bytes) -> str:
    """JVM 在 Windows 上输出的是 GBK —— 两种都试一遍。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


@pytest.fixture(scope="module")
def java_env():
    """javac 编译 DavClient / Prefs / DavPathHandler + 桩类 + 两个自测程序。"""
    jdk = _find_jdk()
    if jdk is None:
        pytest.skip("这台机器没有 JDK —— Java 行为测试跳过（环境问题，不是代码问题）")
    stage = Path(os.environ.get("TEMP", r"C:\Windows\Temp")) / "mb_audit_android"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    out = stage / "classes"
    out.mkdir(exist_ok=True)

    sources = []
    for name in ("DavClient.java", "Prefs.java", "DavPathHandler.java"):
        shutil.copy(JAVA_DIR / name, stage / name)
        sources.append(stage / name)
    for name in ("TestDav.java", "TestDavPath.java"):
        shutil.copy(ANDROID / name, stage / name)
        sources.append(stage / name)
    stub_files = sorted(STUB_DIR.rglob("*.java"))
    assert stub_files, "找不到 android/_stub 下的桩类"
    for sf in stub_files:
        rel = sf.relative_to(STUB_DIR)
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(sf, dst)
        sources.append(dst)

    javac = str(jdk / "bin" / "javac.exe")
    r = subprocess.run([javac, "-encoding", "UTF-8", "-nowarn", "-d", str(out)]
                       + [str(s) for s in sources],
                       capture_output=True)
    assert r.returncode == 0, "javac 编不过:\n" + _decode(r.stdout) + _decode(r.stderr)
    return {"jdk": jdk, "out": out, "stage": stage}


def test_java_sources_compile_with_android_jar(java_env):
    """整包 Java（含 MainActivity / DavBridge / SettingsActivity）要能编过。"""
    jar = _find_android_jar()
    if jar is None:
        pytest.skip("这台机器没有 android.jar —— 跳过整包编译（环境问题）")
    out = java_env["stage"] / "classes_full"
    out.mkdir(exist_ok=True)
    files = [str(p) for p in sorted(JAVA_DIR.glob("*.java"))]
    javac = str(java_env["jdk"] / "bin" / "javac.exe")
    r = subprocess.run([javac, "-encoding", "UTF-8", "-nowarn", "-source", "8",
                        "-target", "8", "-bootclasspath", str(jar),
                        "-cp", str(jar), "-d", str(out)] + files,
                       capture_output=True)
    assert r.returncode == 0, ("整包 Java 编不过:\n" + _decode(r.stdout)
                               + _decode(r.stderr))


class _BadHandler(socketserver.BaseRequestHandler):
    """故意畸形的 DAV 服务器。

    · GET       → 谎报一个巨大的 Content-Length（**不真的发那么多字节**）
                   64 MB  → 测「进内存」那一档的上限（32 MB）
                   384 MB → 测「流式」那一档的上限（256 MB）
    · PROPFIND  → 回一行 20 KB 的响应头（没有换行）
    """

    def handle(self):
        try:
            self.request.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = self.request.recv(4096)
                if not chunk:
                    break
                data += chunk
            first = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            if first.startswith("PROPFIND"):
                body = b"{}"
                head = (b"HTTP/1.1 207 Multi-Status\r\n"
                        b"X-Pad: " + b"A" * 20000 + b"\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n"
                        + body)
            else:
                size = 384 * 1024 * 1024 if "__toolarge_stream__" in first else 64 * 1024 * 1024
                head = ("HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % size).encode()
            self.request.sendall(head)
        except OSError:
            pass
        finally:
            try:
                self.request.close()
            except OSError:
                pass


class _BadServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture(scope="module")
def dav_mock():
    """假 WebDAV 服务器（复用 android/_test_dav.py 里那一份）。"""
    path = ANDROID / "_test_dav.py"
    if not path.exists():
        pytest.skip("找不到 android/_test_dav.py（假 WebDAV 服务器）")
    spec = importlib.util.spec_from_file_location("mb_dav_mock", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv = mod.Server(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/dav/"
    srv.shutdown()


@pytest.fixture(scope="module")
def bad_server():
    """畸形服务器（超大文件 / 超长的行）。"""
    srv = _BadServer(("127.0.0.1", 0), _BadHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/dav/"
    srv.shutdown()


def _java_self_test_failed(out: str) -> bool:
    """输出里有没有失败项（区分 "  FAIL xxx" 与结尾的 "PASS 61  FAIL 0"）。"""
    return re.search(r"(?m)^\s*FAIL\s", out) is not None


def test_java_self_tests_pass(java_env, dav_mock, bad_server):
    """TestDav / TestDavPath 必须全绿（含本轮新增的 ⑤⑥⑦ 断言）。"""
    java = str(java_env["jdk"] / "bin" / "java.exe")
    cp = str(java_env["out"])
    r = subprocess.run([java, "-cp", cp, "TestDav", dav_mock, bad_server],
                       capture_output=True)
    out = _decode(r.stdout) + _decode(r.stderr)
    assert r.returncode == 0, "TestDav 失败:\n" + out
    assert not _java_self_test_failed(out), "TestDav 有失败项:\n" + out

    r2 = subprocess.run([java, "-cp", cp, "TestDavPath", dav_mock],
                        capture_output=True)
    out2 = _decode(r2.stdout) + _decode(r2.stderr)
    assert r2.returncode == 0, "TestDavPath 失败:\n" + out2
    assert not _java_self_test_failed(out2), "TestDavPath 有失败项:\n" + out2
