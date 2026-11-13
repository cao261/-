# -*- coding: utf-8 -*-
"""渲染一张「对照模式」的真实效果图（PNG），用来肉眼验收。

用**真**的东西：桌面端真 CSS + 真 `renderBilingualPage` + 真 `renderFormulaImages`
（公式原图）+ 真 KaTeX（行内公式）+ 后端真返回的段落块与译文。

⚠ 页面必须**用 http 打开**（不能 file://）：
   公式原图走 `/api/paper/formula_image`，KaTeX 走 `/static/vendor/katex/...`
   —— file:// 下这两个都是跨源/根相对路径，取不到。
   所以探针页临时写进 `microbench/static/vendor/`（那里被挂在 /static/vendor 下），
   截完图立刻删掉。

用法：
    python microbench/_shot_bilingual.py                 # 默认 JACERS 第 3 页
    python microbench/_shot_bilingual.py --page 1
输出：logs/blockdiag/bilingual-<paper>-p<page>.png
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "logs" / "blockdiag"
VENDOR = REPO / "microbench" / "static" / "vendor"
BASE = "http://127.0.0.1:5000"

DEFAULT_PDF = ("01_Literature/04_Survey_综述/01_论文原文_PDF/"
               "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf")

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


def find_browser() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def _page_png_data_url(rel: str, page: int) -> str:
    """把论文某一页渲染成 PNG 的 data URL（给「原页图」那一栏用）。"""
    try:
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from microbench import paper_text as pt
        pdf = REPO / rel
        if not pdf.exists():
            return ""
        png = pt.render_page_png(pdf, page, zoom=1.15)
        import base64
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    except Exception as exc:
        print("渲染原页图失败（跳过这一栏）: %s" % exc)
        return ""


def api(path: str, payload: dict | None = None) -> dict:
    url = BASE + path
    if payload is None:
        with urllib.request.urlopen(url, timeout=300) as r:
            return json.loads(r.read().decode("utf-8"))
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


PROBE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>shot</title>
<link rel="stylesheet" href="/static/vendor/katex/katex.min.css">
<script src="/static/vendor/katex/katex.min.js"></script>
<style>
html,body{{margin:0;padding:0;background:#F2EEE7;}}
:root{{
  --surface-raised:#fff; --fg-strong:#15130F; --fg-subtle:#8a857c;
  --fg-muted:#5d584f; --line-hairline:#E6E1D8; --accent-2:#C4703A;
  --font-cn:"Microsoft YaHei","PingFang SC",sans-serif;
  --font-sans:system-ui,sans-serif; --s-3:6px; --s-4:10px; --s-5:14px;
  --r-md:8px; --t-sm:12px;
}}
.wrap{{width:1000px;margin:0 auto;padding:18px 0;}}
.hdr{{font-family:var(--font-sans);font-size:12px;color:#8a857c;padding:0 4px 8px;}}
{rules}
</style></head>
<body>
<div class="wrap">
  <div class="hdr">MicroBench · 对照模式（第 {page} 页）· 左栏段落的译文回左栏、右栏回右栏{orignote}</div>
  {body}
</div>
<script>
function showToast(){{}} function _scheduleAutoSnapshot(){{}} function fetchModelStatus(){{}}
function _recordDiag(){{}} function escapeHtml(s){{return String(s==null?'':s);}}

var _pdfState = {{
  paperId:'shot', relativePath:{rel!r}, bilingual:true,
  pages:[{{ num:{page}, blocks:{blocks}, blockTranslations:{trans} }}]
}};

{code}

renderBilingualPage({page});
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--page", type=int, default=3)
    ap.add_argument("--paper-id", default="shot-jacers")
    ap.add_argument("--height", type=int, default=1600)
    ap.add_argument("--orig", action="store_true",
                    help="同时显示原页图（对照模式的「🖼 原页图」开关效果）")
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("SKIP: 找不到 Edge/Chrome")
        return 0

    rel = args.pdf.replace("\\", "/")
    q = urllib.parse.quote(rel, safe="")
    print("取段落块…")
    pb = api(f"/api/paper/page_blocks?relative_path={q}&page={args.page}")
    print("取段落译文（真调模型，缓存命中就很快）…")
    tb = api("/api/paper/translate_blocks", {
        "paper_id": args.paper_id, "page": args.page,
        "relative_path": rel, "force": False, "model": "",
    })
    blocks = tb.get("blocks") or pb.get("blocks") or []
    trans = {b["i"]: b.get("translation", "") for b in blocks if b.get("translation")}

    html_src = (REPO / "microbench" / "static" / "index.html").read_text(
        encoding="utf-8", errors="replace")
    rules = _desk.extract_css(html_src)
    parts = [
        _desk.extract_function(html_src, "_matchMarkdownTable"),
        _desk.extract_function(html_src, "formatMarkdownLite"),
        _desk.extract_function(html_src, "_formulaShotNode"),
        _desk.extract_function(html_src, "renderFormulaImages"),
        _desk.extract_function(html_src, "renderMathIn"),
        _desk.extract_function(html_src, "_bilingualHost"),
        _desk.extract_function(html_src, "_bilingualRenderInto"),
        _desk.extract_function(html_src, "renderBilingualPage"),
    ]
    code = "\n\n".join(parts)

    # ---- 「🖼 原页图」：把原页画在左边（真 app 里那是 PDF.js 的 canvas，
    #      这里用服务端渲染的整页 PNG 占同一个位置，视觉等价）----
    body = ('<div class="bilingual-page" id="bilingual-page-%d"></div>'
            % args.page)
    orignote = ""
    if args.orig:
        png_b64 = _page_png_data_url(args.pdf, args.page)
        if png_b64:
            body = (
                '<div class="pdf-page-block bilingual-orig">'
                '<div class="pdf-page-label">PAGE %d</div>'
                '<div class="pdf-page-canvas-wrapper" '
                'style="width:480px;height:632px;">'
                '<img class="pdf-page-canvas" src="%s" '
                'style="width:480px;height:632px;"></div>'
                '<div class="bilingual-page" id="bilingual-page-%d"></div>'
                '</div>' % (args.page, png_b64, args.page))
            orignote = " · 🖼 同时显示原页图"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(rel).stem.replace(" ", "_")[:36]
    out = OUT_DIR / f"bilingual-{name}-p{args.page}.png"

    probe = VENDOR / "_shot_bilingual.html"
    probe.write_text(
        PROBE.format(rules=rules, code=code, page=args.page, rel=rel,
                     body=body, orignote=orignote,
                     blocks=json.dumps(blocks, ensure_ascii=False),
                     trans=json.dumps(trans, ensure_ascii=False)),
        encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="mbshot") as td:
            tmp_png = Path(td) / "shot.png"
            cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox",
                   "--hide-scrollbars", "--window-size=1040,%d" % args.height,
                   "--user-data-dir=" + td,
                   "--virtual-time-budget=12000",
                   f"--screenshot={tmp_png}",
                   f"{BASE}/static/vendor/_shot_bilingual.html"]
            p = subprocess.run(cmd, capture_output=True, timeout=180)
            if not tmp_png.exists():
                print("FAIL: 没生成截图")
                print(p.stderr.decode("utf-8", "replace")[-800:])
                return 1
            shutil.copyfile(tmp_png, out)
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    print(f"✓ {out}  ({out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
