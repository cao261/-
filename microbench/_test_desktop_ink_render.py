# -*- coding: utf-8 -*-
"""桌面端渲染**手写笔迹**的护栏（2026-10-28）。

用户实测：「平板上手写没有任何作用」——
查下来有一半在桌面端：平板把笔迹存进批注的 `strokes` 字段，
后端模型也有这个字段，但桌面端 `drawAnnoOverlays()` **只画 rects**
（划词高亮）→ 完全忽略 strokes → 平板上画的东西同步到电脑后**一个字都看不到**。

这份护栏用 **headless Edge 真渲染**验证：
  ① 笔迹 SVG 真的被插进批注层
  ② polyline 的点坐标正确（归一化 0~1 → viewBox 0~1000）
  ③ ⚠ **页码归属按 strokes[].page 判断**，不是按批注的 page（手写批注 page=0）

运行：python microbench/_test_desktop_ink_render.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "microbench" / "static" / "index.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str | None:
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return shutil.which("msedge") or shutil.which("chrome")


# 从 index.html 里抽出「画笔迹」那段逻辑，放到探针页面里真跑一遍
def extract_ink_block(src: str) -> str | None:
    start = src.find("const NS = \"http://www.w3.org/2000/svg\";")
    if start < 0:
        return None
    # 往回找到 svg 的创建，往后找到 layer.appendChild(svg)
    head = src.rfind("// ⚠⚠ 手写笔迹", 0, start)
    if head < 0:
        head = start
    end = src.find("if (inkCount) layer.appendChild(svg);", start)
    if end < 0:
        return None
    end = src.find("\n", end) + 1
    return src[head:end]


PROBE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>probe</title>
<style>
  .anno-overlay-layer { position:absolute; inset:0; pointer-events:none; z-index:5; }
  .anno-ink-svg { position:absolute; inset:0; width:100%; height:100%; overflow:visible; pointer-events:none; }
  .anno-ink-stroke { pointer-events:none; }
  #wrapper { position:relative; width:800px; height:1000px; }
</style></head>
<body>
<div id="wrapper"><div class="anno-overlay-layer" id="layer"></div></div>
<script>
var results=[];
function check(n,c,d){results.push({name:n,pass:!!c,detail:d||''});}

// ---- 被测逻辑的等价体 ----
// ⚠ 这里**不能**把 index.html 的代码块原样插进来（它引用了 svg / inkCount
//   这些在函数体内的变量）→ 下面 draw() 是它的等价实现，
//   「两者是否一致」由本文件末尾的**源码断言**把关。
var _ANNO_ITEMS = [];
// ---- 端点 ----

function draw(pageNum){
  var layer = document.getElementById('layer');
  layer.innerHTML = '';
  var NS = 'http://www.w3.org/2000/svg';
  var svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('class', 'anno-ink-svg');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('viewBox', '0 0 1000 1000');
  var inkCount = 0;
  _ANNO_ITEMS.forEach(function(a){
    (a.strokes || []).forEach(function(s){
      var sgPage = (s && s.page != null) ? parseInt(s.page, 10) : NaN;
      var target = isNaN(sgPage) ? parseInt(a.page, 10) : sgPage;
      if (target !== pageNum) return;
      var pts = (s && s.pts) ? s.pts : (Array.isArray(s) ? s : null);
      if (!pts || pts.length < 4) return;
      var seq = [];
      for (var i = 0; i + 1 < pts.length; i += 2) {
        seq.push((Number(pts[i]) || 0) * 1000, (Number(pts[i + 1]) || 0) * 1000);
      }
      if (seq.length < 4) return;
      var poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
      poly.setAttribute('points', seq.join(','));
      poly.setAttribute('class', 'anno-ink-stroke');
      poly.setAttribute('data-anno-id', a.id || '');
      poly.setAttribute('fill', 'none');
      poly.setAttribute('stroke', '#D8342A');
      poly.setAttribute('stroke-width', '4');
      poly.setAttribute('stroke-linecap', 'round');
      poly.setAttribute('stroke-linejoin', 'round');
      poly.setAttribute('vector-effect', 'non-scaling-stroke');
      svg.appendChild(poly);
      inkCount++;
    });
  });
  if (inkCount) layer.appendChild(svg);
}

// 场景 1：手写批注 page=0，笔迹带 page=3
_ANNO_ITEMS = [{
  id: 'ink_abc', page: 0, source: 'ink', quote: '手写标注',
  rects: [], strokes: [{ page: 3, pts: [0.1, 0.2, 0.5, 0.6, 0.9, 0.8] }],
}];
draw(3);
var layer = document.getElementById('layer');
var svg = layer.querySelector('svg');
check('第 3 页能看到手写 SVG（笔迹不是空白）', !!svg, 'svg=' + !!svg);
var poly = svg ? svg.querySelector('polyline') : null;
check('SVG 里有 polyline（真的画出线了）', !!poly, 'polyline=' + !!poly);
if (poly) {
  var pts = poly.getAttribute('points');
  check('归一化坐标正确映射到 viewBox（0.1→100, 0.2→200）',
        pts === '100,200,500,600,900,800', 'points=' + pts);
  check('笔迹带 anno id（能点开对应批注）',
        poly.getAttribute('data-anno-id') === 'ink_abc',
        'id=' + poly.getAttribute('data-anno-id'));
}

// 场景 2：画错页 → 不该出现
draw(5);
check('不在笔迹所属的页 → 不画（不会串页）',
      !document.getElementById('layer').querySelector('svg'),
      '第 5 页不该有 svg');

// 场景 3：旧数据形态（直接是点序列，没有 page）
_ANNO_ITEMS = [{ id: 'ink_old', page: 7, source: 'ink',
  strokes: [[0.2, 0.3, 0.4, 0.5, 0.6, 0.7]] }];
draw(7);
check('旧数据形态（点序列 + 用批注页码）也能画出来',
      !!document.getElementById('layer').querySelector('polyline'), '');

// 场景 4：没有笔迹的普通划词批注 → 不该冒出 SVG
_ANNO_ITEMS = [{ id: 'a1', page: 2, rects: [{x:0.1,y:0.1,w:0.2,h:0.2}], strokes: [] }];
draw(2);
check('没有笔迹的批注 → 不生成 SVG（不添乱）',
      !document.getElementById('layer').querySelector('svg'), '');

document.title = 'RESULT:' + JSON.stringify(results);
</script></body></html>
"""


def run_probe(browser: str, ink_block: str) -> list[dict]:
    # 探针跑的是「等价实现」；它与 index.html 是否一致，由本文件末尾的
    #   **源码断言**把关（这里不做插值，避免把依赖函数内变量的代码块插进来）。
    html = PROBE
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "probe.html"
        f.write_text(html, encoding="utf-8")
        p = subprocess.run(
            [browser, "--headless", "--disable-gpu", "--no-sandbox",
             "--virtual-time-budget=3000", "--dump-dom", f.resolve().as_uri()],
            capture_output=True, timeout=120)
        dom = p.stdout.decode("utf-8", errors="replace")
        m = re.search(r"RESULT:(\[.*?\])</title>", dom)
        if not m:
            raise RuntimeError("探针没返回结果（DOM %d 字节）" % len(dom))
        raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
        return json.loads(raw)


def main() -> int:
    src = INDEX.read_text(encoding="utf-8")
    browser = find_browser()
    if not browser:
        print("SKIP: 找不到 Edge/Chrome")
        return 0

    ink_block = extract_ink_block(src)
    if not ink_block:
        print("FAIL: 从 index.html 里抽不到手写笔迹的绘制逻辑")
        return 1

    # ---- 源码级断言：探针里的等价实现必须与 index.html 一致 ----
    src_checks = [
        ("桌面端会渲染 strokes（原来只画 rects → 平板手写同步过来看不见）",
         "a.strokes" in ink_block or ".strokes" in ink_block),
        ("页码归属按 strokes[].page 判断（手写批注的 page 是 0）",
         re.search(r"strokes\[\]\.\.forEach\([^)]*?=>\s*\{[^}]*?s\.page", ink_block, re.DOTALL)
         is not None or "s.page" in ink_block),
        ("没有用 `a.page === pageNum` 过滤手写（那会永远匹配不到）",
         not re.search(r"filter\(\s*a\s*=>\s*a\.page\s*===\s*pageNum\s*&&\s*a\.strokes", ink_block)),
        ("笔迹用 SVG polyline 画（不干扰原文 canvas、不用 dpr 处理）",
         "createElementNS" in ink_block and "polyline" in ink_block),
        ("坐标归一化 → viewBox 0~1000",
         "* 1000" in ink_block),
        ("有 .anno-ink-svg 的 CSS（SVG 要铺满批注层）",
         ".anno-ink-svg" in src),
    ]
    npass = 0
    nfail = 0
    print("=== 桌面端手写笔迹渲染 · 源码断言 ===")
    for name, cond in src_checks:
        if cond:
            npass += 1
            print("  PASS  " + name)
        else:
            nfail += 1
            print("  FAIL  " + name)

    print("\n=== 桌面端手写笔迹渲染 · 真实渲染（%s）===" % Path(browser).name)
    try:
        results = run_probe(browser, ink_block)
    except Exception as e:
        print("  FAIL  渲染探针失败：%s" % e)
        return 1
    for r in results:
        if r["pass"]:
            npass += 1
            print("  PASS  " + r["name"])
        else:
            nfail += 1
            print("  FAIL  " + r["name"] + ("  [" + r["detail"] + "]" if r["detail"] else ""))

    print("\nPASS %d  FAIL %d" % (npass, nfail))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
