# -*- coding: utf-8 -*-
"""诊断：平板端「并排/堆叠」探针里的真实几何（临时诊断脚本）。"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "_tab", REPO / "microbench" / "_test_tablet_bilingual_render.py")
tab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tab)

css = tab.CSS.read_text(encoding="utf-8")
rules = tab.extract_css(css, tab.CSS_RULES_SIDE)
print("=== 抽到的 CSS ===")
print(rules)

src = tab.APPJS.read_text(encoding="utf-8")
code = "\n\n".join([
    tab.extract_const(src, "TB_SIDE_MIN_TRANS_WIDTH"),
    tab.extract_const(src, "TB_SIDE_GAP"),
    tab.extract_function(src, "applyOrigSideLayout"),
])

PROBE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>probe-diag</title>
<style>
html,body{margin:0;padding:0;background:#fff;}
:root{--line-hairline:#E6E1D8;--font-ui:system-ui,sans-serif;}
*{box-sizing:border-box;}
#host{width:900px;margin:0 auto;}
{rules}
</style></head>
<body>
<div id="host">
  <div class="tb-pdf-page lay-bilingual show-orig" id="page">
    <div class="tb-pdf-orig"><canvas style="width:360px;height:480px"></canvas></div>
    <div class="tb-pdf-trans tb-bilingual">
      <div class="tb-pdf-trans-body"><p>对照译文栏内容</p></div>
    </div>
  </div>
</div>
<script>
{code}
var hostEl=document.getElementById('host');
var card=document.getElementById('page');
var orig=card.querySelector('.tb-pdf-orig');
var trans=card.querySelector('.tb-pdf-trans');
var canvas=card.querySelector('canvas');
var side=applyOrigSideLayout(card);
var out={side:side, cardClass:card.className,
  cardRect:card.getBoundingClientRect().toJSON(),
  cardStyleW:card.style.width, cardW:card.clientWidth,
  cardCS:(()=>{var c=getComputedStyle(card);return {display:c.display,dir:c.flexDirection,
      jc:c.justifyContent,ai:c.alignItems,w:c.width,minw:c.minWidth,ml:c.marginLeft,mr:c.marginRight,
      box:c.boxSizing,pad:c.padding};})(),
  hostRect:hostEl.getBoundingClientRect().toJSON(),
  cardVar:card.style.getPropertyValue('--tb-card-w'),
  sideVar:card.style.getPropertyValue('--tb-side-w'),
  transVar:card.style.getPropertyValue('--tb-trans-w'),
  orig:{rect:orig.getBoundingClientRect().toJSON(),
        off:orig.offsetWidth,
        cs:getComputedStyle(orig).width+'/'+getComputedStyle(orig).flex},
  canvas:{rect:canvas.getBoundingClientRect().toJSON(),
        styleW:canvas.style.width, off:canvas.offsetWidth,
        cs:getComputedStyle(canvas).width},
  trans:{rect:trans.getBoundingClientRect().toJSON(),
        off:trans.offsetWidth,
        cs:getComputedStyle(trans).width+'/'+getComputedStyle(trans).flex},
  hostW:hostEl.clientWidth};
document.title='RESULT:'+JSON.stringify(out);
</script></body></html>
"""


def main() -> int:
    browser = tab.find_browser()
    with tempfile.TemporaryDirectory() as td:
        html = Path(td) / "probe.html"
        html.write_text(PROBE.replace("{rules}", rules).replace("{code}", code),
                        encoding="utf-8")
        cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox",
               "--window-size=1400,1000", "--virtual-time-budget=4000",
               "--dump-dom", html.resolve().as_uri()]
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        dom = p.stdout.decode("utf-8", errors="replace")
        m = re.search(r"RESULT:(\{.*?\})</title>", dom, re.S)
        if not m:
            print("没拿到结果，DOM 长度", len(dom))
            print(dom[-2000:])
            return 1
        raw = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
        print("\n=== 几何 ===")
        print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
