# -*- coding: utf-8 -*-
"""把「平板 PDF 布局 + 手写笔迹」的渲染护栏挂进 pytest（2026-10-28）。

**为什么需要这个文件**

用户实测反馈了两个真问题：

    ① 手写「没有任何作用」—— 画了没反应 / 存了电脑上看不见
    ② 缩放放大后「只能显示一部分，动不了了」

这两个都是**视觉/交互**问题，靠读代码看不出来（我第一遍就差点判错），
必须**真渲染**才能验 → 这两个护栏用 headless Edge 实测。

而且它们守的是**渲染结果**，不是代码形态 ——
改 CSS 写法、换个选择器名都不该让它们变红。

挂进来之前这两个脚本是手工跑的（`microbench/_test_tablet_pdf_layout.py`），
跟 §4.19 记的那个老教训一样：**不在 pytest 里的测试，对「验收全绿」没有保护力**。

跑法:  python -m pytest tests -q -m "not slow"
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# (脚本, 说明)
PY_SUITES = [
    ("microbench/_test_tablet_pdf_layout.py",
     "平板 PDF 布局（放大后能滚 / 手写层触摸手势 / 笔迹画布尺寸）"),
    ("microbench/_test_desktop_ink_render.py",
     "桌面端渲染手写笔迹（平板画的字电脑上要能看见）"),
]


@pytest.mark.parametrize("rel,label", PY_SUITES, ids=[s[0] for s in PY_SUITES])
def test_pdf_layout_guardrail_passes(rel: str, label: str):
    """每套渲染护栏都必须全绿 —— 有一条红就说明版面/手写出问题了。"""
    script = REPO / rel
    assert script.exists(), f"护栏脚本不见了: {rel}"
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, cwd=str(REPO),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"{label} 护栏没过（{rel}）\n"
        f"---- 输出 ----\n{out[-4000:]}\n"
    )
