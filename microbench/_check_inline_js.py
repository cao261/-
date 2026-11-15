# -*- coding: utf-8 -*-
"""把 HTML 里的内联 <script> 抽出来做语法检查（node --check）。

用法：python microbench/_check_inline_js.py [html路径...]
不带参数 = 检查桌面端 index.html + 平板端 index.html
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def scripts_of(html: str) -> list[str]:
    out = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html,
                         re.S | re.I):
        body = m.group(1)
        if body.strip():
            out.append(body)
    return out


def check(path: Path) -> int:
    html = path.read_text(encoding="utf-8")
    blocks = scripts_of(html)
    if not blocks:
        print(f"  {path.name}: 没有内联 script")
        return 0
    bad = 0
    for i, body in enumerate(blocks):
        # 去掉 type="application/json" 之类非 JS 块
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8",
                                         delete=False) as f:
            f.write(body)
            tmp = f.name
        try:
            r = subprocess.run(["node", "--check", tmp], capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            if r.returncode != 0:
                bad += 1
                print(f"  ✗ {path.name} script#{i}: {r.stderr.strip()[:600]}")
            else:
                print(f"  ✓ {path.name} script#{i} ({len(body.splitlines())} 行)")
        finally:
            Path(tmp).unlink(missing_ok=True)
    return bad


def main() -> int:
    targets = [Path(a) for a in sys.argv[1:]] or [
        REPO / "microbench" / "static" / "index.html",
        REPO / "microbench" / "static" / "tablet" / "index.html",
    ]
    bad = 0
    for t in targets:
        if not t.exists():
            print(f"  ! 不存在: {t}")
            continue
        bad += check(t)
    print("全部通过" if bad == 0 else f"有 {bad} 处语法错误")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
