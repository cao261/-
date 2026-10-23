# -*- coding: utf-8 -*-
"""
Validate the generated tablet bundle.

The bundle is one HTML file the user drops onto a tablet. If anything
needed at runtime is missing, the page silently comes up empty — so these
checks are about "the artefact is complete and self-contained", not about
how it looks.

Run:  python microbench/_test_tablet_export.py
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "MicroBench_平板工作台.html"

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if not BUNDLE.exists():
        print(f"产物不存在: {BUNDLE}\n先跑: python microbench/export_offline.py --tablet")
        return 1

    doc = BUNDLE.read_text(encoding="utf-8")

    print("\n[1] 自包含: 不能依赖任何外部资源")
    ext = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//[^"\']+', doc)
    check(not ext, f"没有外部资源引用 ({len(ext)} 个)")
    non_data = re.findall(r"url\((?!data:)", doc)
    check(not non_data, f"所有 url() 都是 data: ({len(non_data)} 个例外)")
    check("font/woff2;base64" in doc, "KaTeX 字体已内嵌为 base64")
    check("data:image/png;base64" in doc, "应用图标已内嵌")
    check(not re.search(r"@import\s+url\(\s*[\"']?https?:", doc), "CSS 没有 @import 外部表")

    print("\n[2] 运行时齐备")
    check("katex" in doc and "renderToString" in doc, "KaTeX 运行时已内联")
    check("renderMarkdown" in doc, "渲染器已内联")
    check("showDirectoryPicker" in doc, "目录直连能力在 (File System Access)")
    check("indexedDB" in doc, "IndexedDB 本地存储在")
    check("chat/completions" in doc, "AI 直连调用在")
    check("microbench-tablet-pack" in doc, "变更包格式在")

    print("\n[3] 触控 / 平板适配")
    # 平板工作台是触控优先的：触控目标尺寸是无条件放大的，
    # 不靠 @media (pointer: coarse) —— 那个断点只用在桌面版 index.html 上。
    check("min-height:44px" in doc.replace(" ", "") or "min-height: 44px" in doc,
          "触控目标 >= 44px")
    check("viewport-fit=cover" in doc, "viewport-fit=cover 已声明")
    check("safe-area-inset" in doc, "使用了安全区内边距")
    check("100dvh" in doc, "用了 dvh 动态视口高度")
    check("tb-tabbar" in doc, "窄屏底部标签栏在")
    flat = doc.replace(" ", "")
    check("@media (pointer: coarse)" in flat or "min-height:44px" in flat,
          "触控目标已按触控设备放大")

    print("\n[4] 数据块")
    m = re.search(r'<script type="application/json" id="mb-data">(.*?)</script>', doc, re.S)
    check(m is not None, "数据块存在")
    if not m:
        print("cannot continue")
        return 1
    raw = m.group(1)
    check("</" not in raw.replace("<\\/", ""), "数据块里没有会提前关闭 script 的 </")
    payload = json.loads(raw.replace("<\\/", "</"))
    notes = payload["notes"]
    check(len(notes) >= 30, f"导出了 {len(notes)} 篇笔记")
    check(all(n.get("id") for n in notes), "每篇都有 id")
    check(all(n.get("title") for n in notes), "每篇都有标题")
    check(all(n.get("body") for n in notes), "每篇都有 Markdown 源码 (可编辑)")
    check(all(n.get("group") for n in notes), "每篇都有分组")
    with_math = [n for n in notes if "$" in (n.get("body") or "")]
    check(len(with_math) > 0, f"{len(with_math)} 篇笔记含公式")

    print("\n[5] 内联的 JS 必须语法正确 (node --check)")
    blocks = re.findall(r"<script>\n(.*?)\n</script>", doc, re.S)
    app_block = None
    for b in blocks:
        if "renderMarkdown" in b:
            app_block = b
            break
    check(app_block is not None, "找到内联的 app.js")
    if app_block:
        tmp = REPO / "logs" / "_tablet_app_check.js"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(app_block, encoding="utf-8")
        proc = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        check(proc.returncode == 0, f"node --check 通过 ({proc.stderr.strip()[:120]})")
        tmp.unlink(missing_ok=True)

    print("\n[6] 与源码一致 (内联不能被 str.replace 的 $ 规则改坏)")
    src = (REPO / "microbench" / "static" / "tablet" / "app.js").read_text(encoding="utf-8")
    check(app_block == src, "内联的 app.js 与源文件逐字节一致")

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
