# -*- coding: utf-8 -*-
"""全库抽取前后对比工具（2026-10-27 批次 B）。

**为什么需要**

改 `paper_text` 的判据（短词误判 / 短双栏页阈值 / 公式段阈值）会改变
抽取输出 —— 也就是说**用户看到的正文会变**。这种改动不能靠"看一眼觉得
对"来验收，必须逐页对比：

    基线（旧代码）→ 存 JSON
    改完（新代码）→ 存 JSON
    diff 两份 → 逐页列出差异 + 归类

用法：
    python microbench/_extract_diff.py baseline    # 用当前代码存基线
    python microbench/_extract_diff.py compare     # 与基线对比（当前代码 = 新代码）

输出目录：logs/extract_diff/（不进同步根）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT_DIR = REPO / "logs" / "extract_diff"

# 只比**正文文本**。glyphs（未解出的字符清单）单独比，因为它的下标会随
# 文本变化而变，逐字比没有意义。
def _texts(pages: list) -> list[str]:
    return [p.get("text", "") for p in pages]


def _pdfs() -> list[Path]:
    lit = REPO / "01_Literature"
    if not lit.is_dir():
        return []
    out = []
    for p in lit.rglob("*.pdf"):
        if p.is_file() and not p.name.startswith("."):
            out.append(p)
    return sorted(out)


def _extract_all() -> dict:
    from microbench.paper_text import extract_text_from_pdf
    result = {}
    t0 = time.time()
    for i, pdf in enumerate(_pdfs(), 1):
        try:
            data = pdf.read_bytes()
            pages = extract_text_from_pdf(data)
            rel = str(pdf.relative_to(REPO)).replace("\\", "/")
            result[rel] = _texts(pages)
            print(f"[{i}] {rel}  {len(pages)} 页  ({time.time() - t0:.0f}s)")
        except Exception as e:
            print(f"[{i}] {pdf.name}  抽取失败: {e}")
    return result


def _save(obj: dict, name: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    total = sum(len(v) for v in obj.values())
    print(f"已存 {p}  ({len(obj)} 篇 / {total} 页)")


def _load(name: str) -> dict:
    p = OUT_DIR / name
    if not p.is_file():
        print(f"找不到 {p}，请先跑 baseline")
        sys.exit(1)
    return json.loads(p.read_text(encoding="utf-8"))


def _diff(a: str, b: str) -> list[str]:
    """返回 a→b 的差异行（统一 diff 格式）。"""
    import difflib
    return list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile="旧", tofile="新", lineterm="", n=1))


def do_baseline() -> None:
    _save(_extract_all(), "baseline.json")


def do_compare() -> None:
    base = _load("baseline.json")
    new = _extract_all()
    report = []
    changed_papers = 0
    changed_pages = 0
    total_pages = 0

    all_papers = sorted(set(base) | set(new))
    for rel in all_papers:
        b_pages = base.get(rel)
        n_pages = new.get(rel)
        if b_pages is None:
            report.append(f"### 新增: {rel}")
            changed_papers += 1
            continue
        if n_pages is None:
            report.append(f"### 消失: {rel}")
            changed_papers += 1
            continue

        total_pages += max(len(b_pages), len(n_pages))
        if len(b_pages) != len(n_pages):
            report.append(f"### {rel}\n  页数 {len(b_pages)} → {len(n_pages)}")
            changed_papers += 1

        for idx in range(max(len(b_pages), len(n_pages))):
            old = b_pages[idx] if idx < len(b_pages) else None
            cur = n_pages[idx] if idx < len(n_pages) else None
            if old == cur:
                continue
            changed_pages += 1
            if old is None or cur is None:
                report.append(f"### {rel} 第 {idx + 1} 页：{'(旧无)' if old is None else '(新无)'}")
                continue
            d = _diff(old, cur)
            report.append(f"### {rel} 第 {idx + 1} 页  (旧 {len(old)} 字 → 新 {len(cur)} 字)")
            report.extend("    " + line for line in d[:80])

    print()
    print("=" * 70)
    print(f"论文 {len(all_papers)} 篇，页 {total_pages}，"
          f"**变化 {changed_pages} 页**，涉及 {changed_papers} 篇")
    print("=" * 70)
    if report:
        out = OUT_DIR / "diff_report.txt"
        out.write_text("\n".join(report), encoding="utf-8")
        print(f"完整差异已存 {out}（{len(report)} 行），下面显示前 120 行：\n")
        print("\n".join(report[:120]))
    else:
        print("没有任何差异 —— 新代码与基线逐字一致。")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if cmd == "baseline":
        do_baseline()
    elif cmd == "compare":
        do_compare()
    else:
        print("用法: python microbench/_extract_diff.py [baseline|compare]")
        sys.exit(2)
