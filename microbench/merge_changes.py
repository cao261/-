#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Merge a tablet change pack back into the vault.

The tablet app edits notes locally (IndexedDB) and either writes them
straight into the synced folder (Android Chrome + File System Access) or
exports a change pack that has to be merged here. Either way the merge
logic is the same, so it lives in one place.

Conflict rule (never lose data)
-------------------------------
A pack carries, for every note, the hash of the content the tablet started
from (`baseHash`). If the file in the vault still hashes to that value,
nobody touched it since — the tablet's version wins and is written.

If the vault's file has moved on, BOTH sides changed. The tablet's version
is *not* written over it. Instead a new note `..._冲突合并_<时间>.md` is
created next to the original, holding the tablet version and the vault
version under separate headings. The user resolves it by hand.

Usage:
    python microbench/merge_changes.py <pack.json> [--vault DIR] [--dry-run]
    python microbench/merge_changes.py <dir-with-packs> --watch [SECONDS]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_VAULT = REPO

# 与 tablet/static/tablet/app.js 的 hash() 完全一致的 djb2/base36。
# 注意 JS 的 s.charCodeAt(i) 取的是 UTF-16 码元，Python 的 ord() 取的是
# 码点 —— 遇到 emoji（代理对）就对不上了，所以这里按 UTF-16 逐码元算。
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def djb2_base36(s: str) -> str:
    """JS: ((h << 5) + h + s.charCodeAt(i)) >>> 0  ->  h.toString(36)"""
    h = 5381
    raw = s.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        h = ((h << 5) + h + unit) & 0xFFFFFFFF
    if h == 0:
        return "0"
    out = []
    while h:
        h, r = divmod(h, 36)
        out.append(_B36[r])
    return "".join(reversed(out))


# ---------------------------------------------------------------------------
# 路径安全
# ---------------------------------------------------------------------------

def safe_target(vault: Path, rel: str) -> Path | None:
    """把包里的相对路径映射到 vault 内的真实文件，越界一律拒绝。

    平板上的包是可以被改的（它就是个 JSON 文件），所以这里必须当成
    不可信输入处理。
    """
    if not rel or rel.startswith("/") or ":" in rel:
        return None
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if not parts:
        return None
    if any(p in (".", "..") for p in parts):
        return None
    # 只允许写到 vault 的这几个目录里
    allowed = {
        "00_Dashboard", "01_Literature", "02_Reproduction",
        "03_Knowledge", "04_Weekly_Reports", "_Templates",
    }
    if parts[0] not in allowed:
        return None
    if not rel.lower().endswith(".md"):
        return None
    target = vault.joinpath(*parts)
    try:
        target.resolve().relative_to(vault.resolve())
    except ValueError:
        return None
    return target


def read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------

def merge_pack(pack: dict, vault: Path, dry_run: bool = False) -> dict:
    """Merge one pack. Returns a summary dict."""
    summary = {"written": [], "created": [], "skipped": [], "conflicts": [],
               "rejected": [], "total": 0}
    notes = pack.get("notes") or []
    summary["total"] = len(notes)

    for rec in notes:
        rel = rec.get("id") or rec.get("rel") or ""
        body = rec.get("body")
        base_hash = rec.get("baseHash") or ""
        if not isinstance(body, str):
            summary["rejected"].append(rel or "(空)")
            continue

        target = safe_target(vault, rel)
        if target is None:
            summary["rejected"].append(rel or "(空)")
            continue

        exists = target.exists()
        current = read_text(target) if exists else ""

        if not exists:
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
            summary["created"].append(rel)
            continue

        if base_hash and djb2_base36(current) == base_hash:
            # 平板改的期间没人动过这份文件 —— 平板的版本直接生效
            if current != body:
                if not dry_run:
                    target.write_text(body, encoding="utf-8")
                summary["written"].append(rel)
            else:
                summary["skipped"].append(rel)
            continue

        if current == body:
            summary["skipped"].append(rel)
            continue

        # 两边都改了 —— 绝不覆盖，另存一份合并稿
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = target.name[:-3] if target.name.lower().endswith(".md") else target.name
        merged_name = f"{name}_冲突合并_{stamp}.md"
        merged_rel = str(target.parent.relative_to(vault)).replace("\\", "/").strip("/")
        merged_rel = f"{merged_rel}/{merged_name}" if merged_rel else merged_name
        merged_target = safe_target(vault, merged_rel)
        if merged_target is None:
            summary["rejected"].append(rel)
            continue
        merged_body = (
            f"# {rec.get('title') or name}（冲突合并）\n\n"
            "## 平板版本\n\n"
            f"{body.rstrip()}\n\n"
            "## 电脑版本\n\n"
            f"{current.rstrip()}\n"
        )
        if not dry_run:
            merged_target.write_text(merged_body, encoding="utf-8")
        summary["conflicts"].append(rel)

    return summary


def load_pack(path: Path) -> dict | None:
    try:
        pack = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"  [跳过] {path.name}: 读不出来 ({e})")
        return None
    if not isinstance(pack, dict) or pack.get("format") != "microbench-tablet-pack":
        print(f"  [跳过] {path.name}: 不是变更包")
        return None
    return pack


def merge_path(path: Path, vault: Path, dry_run: bool = False) -> bool:
    pack = load_pack(path)
    if pack is None:
        return False
    s = merge_pack(pack, vault, dry_run)
    print(f"  {path.name}: 共 {s['total']} 篇 | 写入 {len(s['written'])} "
          f"新增 {len(s['created'])} 冲突 {len(s['conflicts'])} "
          f"跳过 {len(s['skipped'])} 拒绝 {len(s['rejected'])}")
    for r in s["rejected"]:
        print(f"      [拒绝] 路径不合法: {r}")
    for c in s["conflicts"]:
        print(f"      [冲突] 已另存合并稿，原文件未动: {c}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="把平板的变更包合并回 vault")
    ap.add_argument("source", help="变更包 JSON 文件，或放满变更包的目录")
    ap.add_argument("--vault", default=str(DEFAULT_VAULT),
                    help="vault 根目录 (默认: 工作台根目录)")
    ap.add_argument("--watch", nargs="?", const=60, type=int, metavar="SECONDS",
                    help="常驻监视目录, 有新包就合并")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写盘")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    vault = Path(args.vault).resolve()
    src = Path(args.source).resolve()

    if not args.watch:
        if src.is_dir():
            packs = sorted(src.glob("*.json"))
            if not packs:
                print("目录里没有变更包")
                return 0
            n = 0
            for p in packs:
                if merge_path(p, vault, args.dry_run):
                    n += 1
            return 0 if n else 1
        if not src.exists():
            print(f"找不到: {src}")
            return 1
        return 0 if merge_path(src, vault, args.dry_run) else 1

    print(f"[watch] 监视 {src} ,每 {args.watch} 秒检查一次")
    print("[watch] Ctrl+C 退出")
    seen = set()
    try:
        while True:
            if src.is_dir():
                for p in sorted(src.glob("*.json")):
                    key = (str(p), p.stat().st_mtime)
                    if key in seen:
                        continue
                    seen.add(key)
                    print(f"[watch] 合并 {p.name}")
                    merge_path(p, vault, args.dry_run)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\n[watch] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
