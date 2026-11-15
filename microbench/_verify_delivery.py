# -*- coding: utf-8 -*-
"""交付验收：APK 里到底有没有这一轮的新功能。

为什么单独写一个脚本：`python -c "..."` 在 PowerShell 里会被引号吃掉
（AGENTS.md §2.2 的老坑）—— 写成文件再跑。
"""
from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APK = REPO / "MicroBench-平板工作台.apk"
BUNDLE = REPO / "MicroBench_平板工作台.html"

# (关键词, 说明)
TABLET_MARKS = [
    ("renderBilingualInto", "平板对照渲染函数"),
    ("tb-bilingual-cols", "平板对照分栏样式"),
    ("tb-bilingual-empty", "平板没有段落译文时的提示"),
    ("blockTrans", "平板的段落译文存放位置"),
    ("blocks_v", "平板段落译文的本地缓存键（与整页译文分开）"),
    ("bilingual", "平板布局里的「对照」一档"),
    ("公式：见原文", "公式坐标标记换成人话"),
    ("问这块", "上一轮的圈选问 AI（不能被这轮改坏）"),
    ("captureInkRegion", "上一轮的圈选裁图（不能被这轮改坏）"),
    # ---- 2026-11-12 这一轮：公式/表格正常显示 ----
    ("ensureFormulaShots", "平板本地裁公式原图"),
    ("paintFormulaShots", "公式图回填译文栏"),
    ("tb-formula-shot", "公式图样式"),
    ("data-formula-key", "公式图占位坐标键"),
    (".tb-pdf-trans-body table", "译文栏的表格样式"),
    ("renderMath", "行内公式 KaTeX 渲染"),
]

DESKTOP_MARKS = [
    ("toggleBilingualMode", "桌面端对照模式开关"),
    ("renderBilingualPage", "桌面端对照渲染函数"),
    ("/api/paper/translate_blocks", "桌面端调段落翻译接口"),
    ("_bilingualBlocksPayload", "桌面端把段落译文存进快照"),
    ("blocks_version", "桌面端的段落译文版本闸门"),
]


def main() -> int:
    bad = 0
    if not APK.exists():
        print("✗ APK 不存在")
        return 1
    with zipfile.ZipFile(APK) as z:
        names = z.namelist()
        if "assets/index.html" not in names:
            print("✗ APK 里没有 assets/index.html（是个空壳？）")
            return 1
        html = z.read("assets/index.html").decode("utf-8", "replace")
        print("APK assets/index.html: %.2f MB" % (len(html) / 1024 / 1024))

    print("--- 平板端功能标记 ---")
    for key, label in TABLET_MARKS:
        ok = key in html
        bad += 0 if ok else 1
        print("  %s %s" % ("✓" if ok else "✗", label))

    print("--- 与仓库根的单文件是否逐字节相同 ---")
    a = hashlib.sha256(html.encode("utf-8")).hexdigest()
    b = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
    same = a == b
    bad += 0 if same else 1
    print("  %s 根目录 HTML 与 APK 内一致（%s）" % ("✓" if same else "✗", a[:16]))

    print("--- 桌面端（对照模式在桌面端那份 index.html 里，不进 APK）---")
    desktop = (REPO / "microbench" / "static" / "index.html").read_text(
        encoding="utf-8", errors="replace")
    for key, label in DESKTOP_MARKS:
        ok = key in desktop
        bad += 0 if ok else 1
        print("  %s %s" % ("✓" if ok else "✗", label))

    print("\n%s" % ("全部通过" if bad == 0 else "有 %d 项没过" % bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
