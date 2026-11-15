# -*- coding: utf-8 -*-
"""平板端「整块公式原图（本地裁）」的**真渲染**护栏（2026-11-12）。

用户原话："公式什么的也要能正常显示"。

⚠ 之前平板端对照视图里的整块公式只有一句「【公式：见原文】」——
   平板访问不到电脑端的 /api/paper/formula_image（没有 baseURL、CSP、
   原生桥白名单三处拦着）。现在平板自己用 PDF.js 离屏裁图。

这个护栏证明两件事：
  ① 平板端裁出来的**确实是公式**（不是白纸、不是裁错位置）；
  ② 平板端裁的**位置和电脑端裁的完全一致**（坐标换算没错）。

验证办法：把 app.js 里**真实的** `renderFormulaShot` 抽出来，在 headless
Edge 里对着真 PDF 跑一遍，再跟服务端 PyMuPDF 裁的同一块做像素比对。
（MuPDF 与 pdf.js 的抗锯齿不同 → 用「暗像素包围盒 + 掩膜 IoU」这种
  对渲染器差异不敏感的指标，而不是逐像素相等。）

⚠ **为什么不用 --dump-dom / --virtual-time-budget**：
   虚拟时间会在 PDF 还没下载完时就把预算跑完、浏览器直接退出
   （实测：DOM 里连 RESULT 都没有）。所以这里让浏览器**常驻**，
   探针页算完把结果 **POST 回来**，Python 端轮询收。

运行：python microbench/_test_tablet_formula_shot.py
"""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APPJS = REPO / "microbench" / "static" / "tablet" / "app.js"
VENDOR = REPO / "microbench" / "static" / "vendor"
OUT_DIR = REPO / "logs" / "blockdiag"
BASE = "http://127.0.0.1:5000"

PDF_REL = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
           "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")
PAGE_NO = 3
# 真数据：这一页的公式块 bbox（实测值）
RECT = [76.42, 552.16, 299.55, 577.92]

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

_spec = importlib.util.spec_from_file_location(
    "_desk_bili", REPO / "microbench" / "_test_desktop_bilingual_render.py")
_desk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_desk)
extract_function = _desk.extract_function


def extract_const(src: str, name: str) -> str:
    """抽 app.js 里的一行 const（护栏要抽真代码，不能抄）。

    ⚠ 不能用 `.*?;` —— 行尾注释里没有 `;`，会一路吃到**下一行**的 `;`
       （实测把两个 const 粘成了一坨）。用 `[^;\\n]*;` 只吃本行。
    """
    m = re.search(r"(?m)^[ \t]*(?:const|let|var)\s+" + re.escape(name) + r"\s*=[^;\n]*;",
                  src)
    if not m:
        raise KeyError(f"app.js 里找不到常量 {name}")
    return m.group(0).strip()


def find_browser() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


# ---------------------------------------------------------------- 回收端
class _Result:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    result = None

    def do_POST(self):                      # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        with self.result.lock:
            self.result.items.append(body)
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):                   # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, *a):              # 静音
        pass


PROBE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>probe</title>
<style>html,body{{margin:0;background:#fff;font:12px monospace;}}
#shot{{padding:8px;}}</style>
</head>
<body>
<div id="shot">裁图中…</div>
<script src="/static/vendor/pdfjs/pdf.min.js"></script>
<script>
{code}
function __post(o){{ return fetch("http://127.0.0.1:{port}/result", {{
  method: "POST", body: JSON.stringify(o),
}}); }}
(async () => {{
  const out = {{ok:false, err:"", len:0, w:0, h:0, b64:"", stage:"start"}};
  try {{
    await __post({{stage:"js-start"}});
    pdfjsLib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.js";
    const doc = await pdfjsLib.getDocument({{
      url: "/api/paper/serve_pdf?relative_path=" + encodeURIComponent({pdf_url!r}),
    }}).promise;
    out.stage = "doc";
    await __post({{stage:"doc", pages: doc.numPages}});
    const page = await doc.getPage({page_no});
    out.stage = "page";
    const url = await renderFormulaShot(page, {rect});
    out.stage = "cropped";
    out.len = url ? url.length : 0;
    if (url) {{
      const img = new Image();
      img.src = url;
      await new Promise((res, rej) => {{ img.onload = res; img.onerror = rej; }});
      out.w = img.naturalWidth; out.h = img.naturalHeight;
      out.b64 = url.split(",")[1] || "";
      document.getElementById('shot').innerHTML = "";
      document.getElementById('shot').appendChild(img);
      out.ok = true;
      out.stage = "done";
    }}
  }} catch (e) {{
    out.err = String((e && e.message) || e);
    out.stage = "error";
  }}
  try {{
    await __post(out);
  }} catch (e) {{
    document.title = "POSTFAIL:" + String(e);
  }}
}})();
</script></body></html>
"""


def fetch_server_crop() -> bytes:
    rect = ",".join(f"{x:.1f}" for x in RECT)
    url = (f"{BASE}/api/paper/formula_image?relative_path="
           f"{urllib.parse.quote(PDF_REL, safe='')}&page={PAGE_NO}"
           f"&rect={urllib.parse.quote(rect, safe=',')}&scale=4")
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def ink_stats(png_bytes: bytes):
    """返回 (w, h, 暗像素包围盒, 暗像素掩膜) —— 掩膜用于算 IoU。"""
    from PIL import Image
    im = Image.open(io.BytesIO(png_bytes)).convert("L")
    w, h = im.size
    px = im.load()
    mask = bytearray(w * h)
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = y * w
        for x in range(w):
            if px[x, y] < 200:
                mask[row + x] = 1
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    if maxx < 0:
        return w, h, None, mask
    return w, h, (minx, miny, maxx, maxy), mask


def main() -> int:
    browser = find_browser()
    if not browser:
        print("SKIP: 找不到 Edge/Chrome")
        return 0

    src = APPJS.read_text(encoding="utf-8")
    try:
        code = "\n\n".join([
            extract_const(src, "FORMULA_SHOT_SCALE"),
            extract_function(src, "isBlankShot"),
            extract_function(src, "renderFormulaShot"),
        ])
    except (KeyError, ValueError) as e:
        print("FAIL: 抽取真代码失败：%s" % e)
        return 1

    result = _Result()
    _Handler.result = result
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    probe_path = VENDOR / "_probe_formula_shot.html"
    html = PROBE.format(code=code, pdf_url=PDF_REL, page_no=PAGE_NO,
                         rect=json.dumps(RECT), port=port)
    probe_path.write_text(html, encoding="utf-8")
    url = f"{BASE}/static/vendor/_probe_formula_shot.html"

    proc = None
    td = tempfile.mkdtemp(prefix="mbprobe")
    try:
        cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox",
               "--user-data-dir=" + td, url]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        deadline = time.time() + 150
        while time.time() < deadline:
            with result.lock:
                if any(b'"ok":true' in b for b in result.items):
                    break
            if proc.poll() is not None:
                time.sleep(0.5)
                with result.lock:
                    if any(b'"ok":true' in b for b in result.items):
                        break
                break
            time.sleep(0.4)
    finally:
        if proc is not None:
            # ⚠ Edge 会派生子进程（渲染进程/utility）→ 必须**杀整棵进程树**，
            #   否则 temp profile 里的文件还被占着，删不掉（WinError 32）。
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=30)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        httpd.shutdown()
        try:
            probe_path.unlink()
        except OSError:
            pass
        shutil.rmtree(td, ignore_errors=True)

    with result.lock:
        items = list(result.items)
    for it in items:
        try:
            d = json.loads(it.decode("utf-8"))
            print("  探针阶段:", {k: v for k, v in d.items() if k != "b64"})
        except Exception:
            print("  探针阶段: <无法解析>")
    final = None
    for it in reversed(items):
        try:
            d = json.loads(it.decode("utf-8"))
        except Exception:
            continue
        if d.get("ok") or d.get("err"):
            final = d
            break
    if final is None:
        print("FAIL: 探针没回传结果（浏览器可能起不来，或 PDF 加载失败）")
        return 1
    res = final
    print("探针结果: ok=%s err=%r 尺寸=%sx%s 字节=%s" %
          (res.get("ok"), res.get("err"), res.get("w"), res.get("h"), res.get("len")))

    if not res.get("ok") or not res.get("b64"):
        print("FAIL: 平板端没裁出公式图")
        return 1

    tablet_png = base64.b64decode(res["b64"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_png = OUT_DIR / "tablet-formula-shot-p3.png"
    out_png.write_bytes(tablet_png)

    server_png = fetch_server_crop()
    sw, sh, sbbox, smask = ink_stats(server_png)
    tw, th, tbbox, tmask = ink_stats(tablet_png)

    print("电脑端(PyMuPDF): %dx%d  暗像素框=%s" % (sw, sh, sbbox))
    print("平板端(PDF.js) : %dx%d  暗像素框=%s" % (tw, th, tbbox))

    checks = []
    checks.append(("平板端裁出了非空图片", len(tablet_png) > 1000, f"{len(tablet_png)} 字节"))
    checks.append(("平板端裁到了有内容的地方（不是白纸）", tbbox is not None, str(tbbox)))
    exp_w = int(round((RECT[2] - RECT[0]) * 4))
    exp_h = int(round((RECT[3] - RECT[1]) * 4))
    checks.append(("裁出来的尺寸与 scale=4 预期一致",
                   abs(tw - exp_w) <= 4 and abs(th - exp_h) <= 4,
                   f"实际 {tw}x{th}，预期 ≈{exp_w}x{exp_h}"))
    checks.append(("与电脑端裁的尺寸一致",
                   abs(tw - sw) <= 4 and abs(th - sh) <= 4,
                   f"电脑端 {sw}x{sh}"))

    if sbbox and tbbox:
        scx = (sbbox[0] + sbbox[2]) / 2.0
        scy = (sbbox[1] + sbbox[3]) / 2.0
        tcx = (tbbox[0] + tbbox[2]) / 2.0
        tcy = (tbbox[1] + tbbox[3]) / 2.0
        dcx = abs(scx - tcx)
        dcy = abs(scy - tcy)
        checks.append(("公式位置与电脑端一致（暗像素中心偏差 < 6px）",
                       dcx < 6 and dcy < 6, f"Δx={dcx:.1f} Δy={dcy:.1f}"))
        inter = sum(1 for i in range(len(smask)) if smask[i] and tmask[i])
        union = sum(1 for i in range(len(smask)) if smask[i] or tmask[i])
        iou = inter / union if union else 0.0
        checks.append(("公式墨迹与电脑端高度重合（IoU > 0.55）", iou > 0.55, f"IoU={iou:.3f}"))

    print("\n=== 平板端整块公式原图 · 真渲染护栏（%s）===" % Path(browser).name)
    npass = 0
    for name, ok, detail in checks:
        print("  %s  %s  [%s]" % ("PASS" if ok else "FAIL", name, detail))
        npass += 1 if ok else 0
    nfail = len(checks) - npass
    print("\nPASS %d  FAIL %d" % (npass, nfail))
    print("对照图: %s" % out_png)
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
