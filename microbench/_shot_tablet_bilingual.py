# -*- coding: utf-8 -*-
"""渲染一张平板端「对照模式」的真实效果图（PNG），用来肉眼验收。

用**真**的东西：平板真 CSS（styles.css）+ 真 `renderBilingualInto`（app.js）
+ 真 KaTeX（平板 HTML 里内联的那份）+ 真公式原图（平板本地 PDF.js 裁出来的那张）。

公式图从 `logs/blockdiag/tablet-formula-shot-p3.png` 取
（由 `_test_tablet_formula_shot.py` 用真 `renderFormulaShot` 裁的），
以 data URL 塞进 `pdfState.formulaShots` —— 对应真页面里「裁完贴回」的那一步。

用法：python microbench/_shot_tablet_bilingual.py
输出：logs/blockdiag/tablet-bilingual-p3.png
"""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APPJS = REPO / "microbench" / "static" / "tablet" / "app.js"
CSS = REPO / "microbench" / "static" / "tablet" / "styles.css"
VENDOR = REPO / "microbench" / "static" / "vendor"
BASE = "http://127.0.0.1:5000"
SHOT_PNG = REPO / "logs" / "blockdiag" / "tablet-formula-shot-p3.png"
OUT = REPO / "logs" / "blockdiag" / "tablet-bilingual-p3.png"

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

_spec2 = importlib.util.spec_from_file_location(
    "_tab_bili", REPO / "microbench" / "_test_tablet_bilingual_render.py")
_tab = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_tab)
extract_const = _tab.extract_const
extract_css = _tab.extract_css


def find_browser() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


PROBE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>tablet shot</title>
<link rel="stylesheet" href="/static/vendor/katex/katex.min.css">
<script src="/static/vendor/katex/katex.min.js"></script>
<style>
html,body{{margin:0;padding:0;background:#F2EEE7;}}
:root{{--line-hairline:#E6E1D8;--surface-sunken:#F4F1EA;--font-ui:system-ui,sans-serif;}}
#host{{width:980px;margin:0 auto;padding:16px 0;}}
.hdr{{font:12px system-ui,sans-serif;color:#8A8279;padding:0 4px 8px;}}
{rules}
</style></head>
<body>
<div id="host">
  <div class="hdr">MicroBench 平板 · 对照模式（第 3 页）· 左栏译文回左栏、右栏回右栏</div>
  <div class="tb-pdf-page lay-bilingual" id="page"></div>
</div>
<script>
function el(tag, cls, text){{
  var n=document.createElement(tag);
  if (cls) n.className=cls;
  if (text!=null) n.textContent=text;
  return n;
}}
var pdfState = {{ layout:'bilingual', trans:{{}}, blockTrans: {{}}, formulaShots: {{}}, doc: null }};
pdfState.blockTrans = {{ 1: {{ version:1, blocks: {blocks} }} }};
pdfState.formulaShots = {{ 1: {{ "76.4,552.2,299.6,577.9": "{shot}" }} }};

{code}

renderBilingualInto(document.getElementById('page'), 1);
</script></body></html>
"""


def main() -> int:
    browser = find_browser()
    if not browser:
        print("SKIP: 找不到 Edge/Chrome")
        return 0
    if not SHOT_PNG.exists():
        print("FAIL: 缺公式原图 %s（先跑 _test_tablet_formula_shot.py）" % SHOT_PNG)
        return 1

    shot = "data:image/png;base64," + base64.b64encode(SHOT_PNG.read_bytes()).decode()

    css = CSS.read_text(encoding="utf-8")
    rules = extract_css(css)
    src = APPJS.read_text(encoding="utf-8")
    # esc 是多行箭头函数，单独切出来
    m = re.search(r"const esc = \(s\) =>[\s\S]*?;\n", src)
    if not m:
        print("FAIL: 抽不到 esc")
        return 1
    esc_src = m.group(0)
    # 消毒用的常量（ALLOWED_TAGS 等）也在 sanitizeHtml 前面，一起切出来
    try:
        i0 = src.index("const ALLOWED_TAGS")
        j0 = src.index("function sanitizeHtml")
        sanitize_consts = src[i0:j0]
    except ValueError:
        print("FAIL: 切不出 sanitizeHtml 的常量段")
        return 1
    try:
        code = "\n\n".join([
            # ① 平板端的 Markdown + KaTeX 渲染链（真代码）
            'const E_AMP = String.fromCharCode(38);',
            esc_src,
            sanitize_consts,
            extract_function(src, "sanitizeHtml"),
            extract_function(src, "safeHref"),
            "const INLINE_MAX = 60;",
            "const MATH_TOKEN = (i) => `@@MBK${i}@@`;",
            "const _mathCache = new Map(); const MATH_CACHE_MAX = 400;",
            extract_function(src, "stripFrontmatter"),
            extract_function(src, "extractMath"),
            extract_function(src, "renderMath"),
            extract_function(src, "renderMarkdownInner"),
            extract_function(src, "renderMarkdown"),
            # ② 对照模式 + 公式图
            extract_const(src, "FORMULA_MARK_RE"),
            extract_const(src, "FORMULA_SHOT_SCALE"),
            extract_function(src, "formulaRectsOf"),
            extract_function(src, "formulaKeyOf"),
            extract_function(src, "fillFormulaShot"),
            extract_function(src, "formulaHolder"),
            extract_function(src, "formulaBlocksOf"),
            extract_function(src, "paintFormulaShots"),
            extract_function(src, "ensureFormulaShots"),
            extract_function(src, "renderBilingualInto"),
        ])
    except (KeyError, ValueError) as e:
        print("FAIL: 抽取真代码失败：%s" % e)
        return 1

    blocks = [
        {"i": 0, "kind": "body", "col": "S", "bbox": [60, 21, 562, 31],
         "translation": "2008 年 2 月"},
        {"i": 1, "kind": "body", "col": "L", "bbox": [60, 453, 299, 480],
         "translation": "图 2. 室温下 PZT/NiFe₂O₄/PZT 叠层断裂表面的光学显微照片与扫描电子显微镜（SEM）图像。"},
        {"i": 2, "kind": "body", "col": "L", "bbox": [60, 492, 299, 541],
         "translation": "其中 n 是磁致伸缩相的体积分数，e 会随磁滞现象下降；此外，复合材料的有效压电常数也可以用类似的混合规则计算出来。"},
        {"i": 3, "kind": "formula", "col": "L", "bbox": [76, 552, 299, 578],
         "translation": "⟦FORMULA:76.4,552.2,299.6,577.9⟧"},
        {"i": 4, "kind": "heading", "col": "R", "bbox": [323, 408, 536, 427],
         "translation": "（3）陶瓷与 Terfenol-D（或 Galfenol）合金 ME 复合材料"},
        {"i": 5, "kind": "body", "col": "R", "bbox": [323, 430, 562, 638],
         "translation": "介电常数 $\\varepsilon_{33}^{eff}$ 与体积比 $n$ 的关系如图 3(a) 所示。"},
        {"i": 6, "kind": "table", "col": "R", "bbox": [323, 640, 562, 759],
         "translation": "| 参数 | BaTiO$_3$ | PZT-5 |\n|---|---|---|\n| $d_{31}$ (pC/N) | −90 | −175 |\n| $\\varepsilon$ | 1700 | 1750 |"},
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # ⚠ 页面必须用 **http** 打开：KaTeX（CSS + JS + 字体）要从
    #   /static/vendor/katex 取，file:// 下取不到。
    #   → 探针页临时写进 static/vendor/（那里被挂在 /static/vendor 下）。
    probe = VENDOR / "_shot_tablet_bilingual.html"
    probe.write_text(
        PROBE.format(rules=rules, code=code,
                     blocks=json.dumps(blocks, ensure_ascii=False),
                     shot=shot),
        encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory(prefix="mbtshot") as td:
            png = Path(td) / "shot.png"
            cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox",
                   "--hide-scrollbars", "--window-size=1020,1400",
                   "--user-data-dir=" + td,
                   "--virtual-time-budget=10000",
                   f"--screenshot={png}",
                   f"{BASE}/static/vendor/_shot_tablet_bilingual.html"]
            p = subprocess.run(cmd, capture_output=True, timeout=180)
            if not png.exists():
                print("FAIL: 没生成截图")
                print(p.stderr.decode("utf-8", "replace")[-800:])
                return 1
            shutil.copyfile(png, OUT)
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    print(f"✓ {OUT}  ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
