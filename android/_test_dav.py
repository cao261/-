# -*- coding: utf-8 -*-
"""
一个最小的 WebDAV 服务器，用来对拍 DavClient（Java）。

真机上没法自动测 WebDAV，所以这里用 Python 起一个只实现
PROPFIND / GET / PUT / MKCOL / DELETE 的假服务器，然后让
Java 的 DavClient 连上来跑一遍。

跑法:  python android/_test_dav.py
"""
import base64
import http.server
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "_davtest_root"
PORT = 8765

# 假服务器上的文件树
FILES = {
    "我的坚果云/MicroBench工作台/00_Dashboard/主工作台看板.md": "# 看板\n\n内容\n",
    "我的坚果云/MicroBench工作台/01_Literature/01_论文原文_PDF/a paper.pdf": "PDF-BYTES",
    # 文件名带 "+" —— WebDAV 服务器完全可能把 "+" 原样写在 href 里
    # (RFC 3986 里路径里的 "+" 就是字面量)。而 java.net.URLDecoder 走的是
    # form 编码规则, 会把 "+" 解成空格 —— 「C++入门.pdf」就变成
    # 「C 入门.pdf」, 那个文件永远打不开。
    "我的坚果云/MicroBench工作台/01_Literature/C++入门.pdf": "PDF-PLUS",
}


def xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# 已经「限流」过的路径 —— 每个路径只限流一次
_RATELIMIT_SEEN = set()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _auth_ok(self) -> bool:
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(h[6:]).decode("utf-8")
        except Exception:
            return False
        return raw == "me@example.com:app-pass-123"

    def _deny(self):
        body = b"unauthorized"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="dav"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Allow", "OPTIONS,GET,PUT,PROPFIND,MKCOL,DELETE")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PROPFIND(self):
        if not self._auth_ok():
            return self._deny()
        path = self.path.split("?")[0]
        # 请求行里的 path 是百分号编码的, 这里要解回来再和文件树比对
        path = unquote(path)
        # 剥掉 dav 根, 剩下的才和 FILES 的 key 对得上
        if path.startswith("/dav/"):
            path = path[4:]
        path = path.lstrip("/")
        # ⚠ 限流模拟：路径里带 __ratelimit__ 的，第一次回 503
        #   BlockedTemporarily（坚果云限流时的原样响应），
        #   之后正常返回 —— 用来验证客户端的退避重试。
        if "__ratelimit__" in path and path not in _RATELIMIT_SEEN:
            _RATELIMIT_SEEN.add(path)
            data = (b'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
                    b'<d:error xmlns:d="DAV:" xmlns:s="http://ns.jianguoyun.com">'
                    b'<s:exception>BlockedTemporarily</s:exception>'
                    b'<s:message>Too many requests are received recently</s:message>'
                    b'</d:error>')
            self.send_response(503)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path and not path.endswith("/"):
            path += "/"
        prefix = path
        items = []
        seen = set()
        for full in FILES:
            if not full.startswith(prefix):
                continue
            rest = full[len(prefix):]
            if "/" not in rest:
                items.append((rest, False, len(FILES[full].encode("utf-8"))))
            else:
                d = rest.split("/")[0] + "/"
                if d not in seen:
                    seen.add(d)
                    items.append((d, True, 0))
        body = ['<?xml version="1.0" encoding="utf-8"?>',
                 '<D:multistatus xmlns:D="DAV:">']
        for name, is_dir, size in items:
            href = "/dav/" + quote(prefix + name)
            # ⚠ 故意把 "%2B" 还原成字面 "+" —— 模拟那些不把 "+" 百分号编码
            #   的 WebDAV 服务器（真实存在）。客户端必须扛住这个。
            href = href.replace("%2B", "+")
            body.append("<D:response>")
            body.append(f"<D:href>{xml_escape(href)}</D:href>")
            body.append("<D:propstat><D:prop>")
            if is_dir:
                body.append("<D:resourcetype><D:collection/></D:resourcetype>")
            else:
                body.append("<D:resourcetype/>")
                body.append(f"<D:getcontentlength>{size}</D:getcontentlength>")
            # getlastmodified —— 客户端靠它判断文件有没有变（省请求）
            body.append("<D:getlastmodified>Tue, 25 Oct 2026 03:14:15 GMT</D:getlastmodified>")
            body.append("</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>")
            body.append("</D:response>")
        body.append("</D:multistatus>")
        data = "".join(body).encode("utf-8")
        if os.environ.get("DAV_DEBUG"):
            print(f"[mock] PROPFIND {self.path} -> {len(items)} items, {len(data)} bytes",
                  file=sys.stderr, flush=True)
            print(data.decode("utf-8")[:600], file=sys.stderr, flush=True)
        self.send_response(207)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth_ok():
            return self._deny()
        path = self.path.split("?")[0]
        key = path.lstrip("/")
        if key.startswith("dav/"):
            key = key[4:]
        key = unquote(key)
        if key not in FILES:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        data = FILES[key].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        if not self._auth_ok():
            return self._deny()
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        key = unquote(self.path.split("?")[0].lstrip("/"))
        if key.startswith("dav/"):
            key = key[4:]
        FILES[key] = body.decode("utf-8", "replace")
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_MKCOL(self):
        if not self._auth_ok():
            return self._deny()
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        if not self._auth_ok():
            return self._deny()
        key = unquote(self.path.split("?")[0].lstrip("/"))
        if key.startswith("dav/"):
            key = key[4:]
        FILES.pop(key, None)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def quote(p: str) -> str:
    from urllib.parse import quote as q
    return q(p, safe="/")


def unquote(p: str) -> str:
    from urllib.parse import unquote as u
    return u(p)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def find_jdk():
    """找一个可用的 JDK。

    ⚠ 之前这里写死了「D:\\Desktop\\杂物\\z\\Zotero\\jdk」——
      那是**这台机**的路径。另一台设备（25353）没有这个目录，
      javac 直接失败 → 这个套件红 → 它的 auto_sync 判定「测试失败」，
      **再也推不上去**。而且那个红是环境问题，不是代码问题。
      → 改成多路查找；实在找不到就 SKIP（不算失败）。
    """
    import shutil as _sh
    cands = []
    for env in ("MB_JDK", "JAVA_HOME"):
        v = os.environ.get(env)
        if v:
            cands.append(v)
    cands.append(r"D:\Desktop\杂物\z\Zotero\jdk")
    # 常见安装位置
    for base in (r"C:\Program Files\Java", r"C:\Program Files\Eclipse Adoptium",
                 r"C:\Program Files\Microsoft\jdk", r"D:\Android\Sdk"):
        p = Path(base)
        if p.is_dir():
            try:
                for child in sorted(p.iterdir()):
                    cands.append(str(child))
            except OSError:
                pass
    # PATH 上的 javac
    w = _sh.which("javac")
    if w:
        cands.append(str(Path(w).parent.parent))

    seen = set()
    for c in cands:
        if not c or c in seen:
            continue
        seen.add(c)
        javac = Path(c) / "bin" / "javac.exe"
        java = Path(c) / "bin" / "java.exe"
        if javac.is_file() and java.is_file():
            return c
    return None


def main() -> int:
    jdk = find_jdk()
    if not jdk:
        print("SKIP: 没找到 JDK（javac），跳过 Java 测试。"
              "（环境问题，不是代码问题 —— 设 MB_JDK 指向 JDK 根目录即可启用）")
        return 0

    srv = Server(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print(f"mock webdav on 127.0.0.1:{PORT}")
    time.sleep(0.5)

    # 编译并跑 Java 测试。
    # 注意: javac 的命令行参数经 os.system 走一遍 cmd, 路径里有中文/空格
    # 就会被 cmd 拆坏。所以整个测试挪到纯 ASCII 目录编译。
    stage = Path(os.environ.get("TEMP", r"C:\Windows\Temp")) / "mb_davtest"
    if stage.exists():
        import shutil
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(HERE / "java" / "microbench" / "tablet" / "DavClient.java",
                stage / "DavClient.java")
    shutil.copy(HERE / "TestDav.java", stage / "TestDav.java")

    # ⚠ 桩类：让 Prefs / DavPathHandler（依赖 Android 框架类）能在普通 JDK 下跑。
    #   桩放在 android/_stub/ 下，**不能**进 android/java ——
    #   build_apk.ps1 会把 android/java 全编译一遍，桩类跟真 android.jar 撞类，
    #   APK 直接编不过。
    stub_src = HERE / "_stub"
    stub_files = sorted(stub_src.rglob("*.java"))
    for sf in stub_files:
        rel = sf.relative_to(stub_src)          # 保住 android/content/... 的层级
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(sf, dst)

    # 真实依赖 Android 的两个类 + 对应测试
    for cls in ("Prefs.java", "DavPathHandler.java"):
        shutil.copy(HERE / "java" / "microbench" / "tablet" / cls, stage / cls)
    shutil.copy(HERE / "TestDavPath.java", stage / "TestDavPath.java")

    out = stage / "classes"
    out.mkdir(exist_ok=True)

    javac = str(Path(jdk) / "bin" / "javac.exe")
    java = str(Path(jdk) / "bin" / "java.exe")
    sources = [str(stage / "DavClient.java"), str(stage / "TestDav.java")]
    sources += [str(stage / c) for c in ("Prefs.java", "DavPathHandler.java")]
    sources += [str(stage / "TestDavPath.java")]
    sources += [str(stage / sf.relative_to(stub_src)) for sf in stub_files]
    cmd = [javac, "-encoding", "UTF-8", "-nowarn", "-d", str(out)] + sources
    print("compiling...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        print("javac 失败")
        return 1
    print("running...", flush=True)
    r = subprocess.run([java, "-cp", str(out), "TestDav", f"http://127.0.0.1:{PORT}/dav/"],
                       capture_output=True, text=True)
    print(r.stdout)
    if r.stderr.strip():
        print(r.stderr, file=sys.stderr)
    rc = r.returncode

    # 第二个测试：真跑在手机上的那条链路（DavPathHandler + 自愈）
    print("--- TestDavPath（DavPathHandler 自愈链路）---", flush=True)
    r2 = subprocess.run([java, "-cp", str(out), "TestDavPath",
                         f"http://127.0.0.1:{PORT}/dav/"],
                        capture_output=True, text=True)
    print(r2.stdout)
    if r2.stderr.strip():
        print(r2.stderr, file=sys.stderr)
    if r2.returncode != 0:
        rc = r2.returncode

    srv.shutdown()
    return rc


if __name__ == "__main__":
    sys.exit(main())
