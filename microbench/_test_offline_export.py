# -*- coding: utf-8 -*-
"""Verify the offline export: formulas rendered, no leftovers, links sane."""
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path(__file__).resolve().parent.parent / "MicroBench_离线阅读版.html"
)
doc = path.read_text(encoding="utf-8")

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


# The notes live inside a <script type="application/json"> block, so their
# HTML is JSON-escaped in the raw file (class=\"katex\"). Every content check
# has to run against the *parsed* payload, not the raw text.
data = re.search(r'<script type="application/json" id="mb-data">(.*?)</script>', doc, re.S)
check(data is not None, "数据块存在")
if not data:
    print("cannot continue without the data block")
    sys.exit(1)

payload = json.loads(data.group(1).replace("<\\/", "</"))
notes = payload["notes"]

note_html = "\n".join(n["html"] for n in notes)
katex_spans = len(re.findall(r'class="katex"', note_html))
katex_display = len(re.findall(r'class="katex-display"', note_html))
math_errors = len(re.findall(r'class="math-error"', note_html))
leftover_tokens = len(re.findall(r'@@MBK\d+@@', note_html))
# A lone "$" is currency ("$33/篇"), not math — the app's own renderer leaves
# those alone too. Only a *paired* $...$ means something was missed.
leftover_dollars = len(re.findall(r'(?<!\$)\$[^\$\n]+?\$(?!\$)', note_html))

check(katex_spans > 0, f"公式已渲染成 KaTeX ({katex_spans} 处)")
check(katex_display > 0, f"存在块级公式 ({katex_display} 处)")
check(math_errors == 0, f"没有渲染失败的公式 ({math_errors} 处 math-error)")
check(leftover_tokens == 0, f"没有残留占位符 ({leftover_tokens} 个)")
check(leftover_dollars == 0, f"没有残留未解析的 $ ({leftover_dollars} 个)")

# Structure
check('<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">'
      in doc, "viewport-fit=cover 已声明")
check("safe-area-inset" in doc, "使用了 safe-area 内边距")
check("@media (pointer: coarse)" in doc, "有触控适配样式")
check("font/woff2;base64" in doc, "KaTeX 字体已内嵌为 data URI")

check(len(notes) >= 30, f"导出了 {len(notes)} 篇笔记")
check(all(n["title"] for n in notes), "每篇笔记都有标题")
check(all(n["html"].strip() for n in notes), "每篇笔记都有正文")
with_formula = sum(1 for n in notes if "katex" in n["html"])
check(with_formula > 0, f"{with_formula} 篇笔记含已渲染公式")

# Wiki links that resolve to a note must point at a real id
ids = {n["id"] for n in notes}
bad = []
for n in notes:
    for m in re.finditer(r'href="#/note/(\d+)"', n["html"]):
        if int(m.group(1)) not in ids:
            bad.append(n["rel"])
check(not bad, f"双链都指向存在的笔记 (坏链 {len(bad)})")

check("<code" in note_html, "代码块已渲染为 code 元素")
check("<table" in note_html, "表格已渲染")

print(f"\n{'=' * 56}\nPASS {len(PASS)}  FAIL {len(FAIL)}")
for f in FAIL:
    print(f"  FAILED: {f}")
sys.exit(1 if FAIL else 0)
