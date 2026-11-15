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

An **empty body** is never written (2026-10-27 audit H-1). A pack is
untrusted input: `body=""` (tablet editor bug, interrupted sync, or a
crafted pack) used to overwrite a real note with an empty file, with no
conflict copy and no backup — and that note lives in the vault, so the
Nutstore sync carried the loss to the other device as well. Empty (or
whitespace-only) bodies are counted as `rejected` instead.

Usage:
    python microbench/merge_changes.py <pack.json> [--vault DIR] [--dry-run]
    python microbench/merge_changes.py <dir-with-packs> --watch [SECONDS]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_VAULT = REPO

# 与 tablet/static/tablet/app.js 的 hash() 完全一致的 djb2/base36。
#
# → 实现已搬到 `microbench/paperid.py`（app.py 也用同一份）。
#   注意 JS 的 s.charCodeAt(i) 取的是 UTF-16 码元，Python 的 ord() 取的是
#   码点 —— 遇到 emoji（代理对）就对不上了，所以那里按 UTF-16 逐码元算，
#   并且带 errors="surrogatepass"（孤立代理项不崩）。
from .paperid import djb2_base36  # noqa: F401  （保持原有 import 路径可用）


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
    # ⚠ 包含性判断必须用**逻辑路径**（os.path.abspath），不能用 resolve()。
    #   resolve() 会**穿透 junction** —— 本机 vault 的 01_Literature 等目录
    #   全是指向坚果云的 junction，resolve 之后目标跑到 C 盘的坚果云目录里，
    #   相对 vault.resolve()（D 盘）自然「越界」→ 全部拒绝。
    #   实测：junction 部署下 safe_target 对每一条笔记都返回 None，
    #   平板上的编辑一条都合不回来。
    #   abspath 同样把 ".." 归一化，上面也已经显式拒了 ".." / 绝对路径 / 盘符，
    #   遍历攻击照拦。（与 generator._safe_path 的修法一致，见 AGENTS.md §4.4。）
    try:
        base = os.path.abspath(str(vault))
        tgt = os.path.abspath(str(target))
        if os.path.commonpath([base, tgt]) != base:
            return None
    except (OSError, ValueError):
        # commonpath 在跨盘（Windows）时会抛 ValueError
        return None
    return target


def read_text(p: Path) -> str | None:
    """读原笔记；失败返回 None（**不要**退化成 ""）。

    2026-11-09 审计：原来读失败（GBK、被占用、权限）静默返回空串，
    冲突稿里的「## 电脑版本」就变成空白，用户手动清理冲突时会以为
    原笔记本来就是空的 —— 「读不到」和「空文件」必须分开。
    """
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"  [拒绝] 原文件读不出来，已跳过以免造出空版本: {p.name} ({e})",
              file=sys.stderr)
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录唯一临时文件 + os.replace —— 绝不把用户笔记写成半截。

    原来三处都是 `target.write_text(...)`：写入中断 / 磁盘满 / 进程被杀
    会直接截断原笔记，与文件头 "never lose data" 矛盾（2026-11-09 审计）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    fd_taken = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_taken = True
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if not fd_taken:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------

def merge_pack(pack: dict, vault: Path, dry_run: bool = False) -> dict:
    """Merge one pack. Returns a summary dict."""
    summary = {"written": [], "created": [], "skipped": [], "conflicts": [],
               "rejected": [], "total": 0}
    notes = pack.get("notes") or []
    if not isinstance(notes, list):
        raise ValueError("变更包 notes 必须是数组")
    summary["total"] = len(notes)

    for rec in notes:
        # 2026-11-09 审计：畸形元素（字符串 / null）会让 rec.get 抛
        # AttributeError，而此时前面的笔记**已经落盘** → vault 处于
        # 「合并了一半」的中间态。load_pack 已做整包校验，这里再兜一层，
        # 保证直接调用 merge_pack 也不会半途崩。
        if not isinstance(rec, dict):
            summary["rejected"].append("(变更包元素不是对象)")
            continue
        rel = rec.get("id") or rec.get("rel") or ""
        body = rec.get("body")
        base_hash = rec.get("baseHash") or ""
        if not isinstance(body, str):
            summary["rejected"].append(rel or "(空)")
            continue

        # ⚠⚠ 空正文**一律不写**（2026-10-27 审计 H-1）。
        #   旧版：baseHash 命中 + body="" → `current != body` → 直接
        #   `target.write_text("")` → **把 vault 里的真笔记清空**，
        #   而且绕过了「两边都改 → 另存合并稿」的保数据策略。
        #   触发路径很常见：平板端 `body: n.body || ""` —— 用户清空正文、
        #   编辑器 bug、同步中断、或者恶意构造的包，都会产出空 body。
        #   该文件在 vault 里 → 经坚果云同步到另一台设备 → **两台设备正文一起丢**。
        #   → 空/纯空白正文一律计入 `rejected`，一个字都不落盘。
        #   （非空正文的写入行为**完全不变**。）
        if not body.strip():
            summary["rejected"].append(rel or "(空)")
            continue

        target = safe_target(vault, rel)
        if target is None:
            summary["rejected"].append(rel or "(空)")
            continue

        exists = target.exists()
        current = read_text(target) if exists else ""
        if current is None:
            summary["rejected"].append(rel)
            continue

        if not exists:
            if not dry_run:
                _atomic_write_text(target, body)
            summary["created"].append(rel)
            continue

        if base_hash and djb2_base36(current) == base_hash:
            # 平板改的期间没人动过这份文件 —— 平板的版本直接生效
            if current != body:
                if not dry_run:
                    _atomic_write_text(target, body)
                summary["written"].append(rel)
            else:
                summary["skipped"].append(rel)
            continue

        if current == body:
            summary["skipped"].append(rel)
            continue

        # 两边都改了 —— 绝不覆盖，另存一份合并稿
        # 秒级时间戳 + 短随机后缀（2026-11-09）：同秒重跑同一个包，
        # 或 watch 同时合并两个包且冲突到同一篇，原来会同名 write_text
        # 互相覆盖 → 悄悄丢掉一份平板版本。
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = target.name[:-3] if target.name.lower().endswith(".md") else target.name
        merged_name = f"{name}_冲突合并_{stamp}_{uuid.uuid4().hex[:6]}.md"
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
            _atomic_write_text(merged_target, merged_body)
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
    # 2026-11-09 审计：原来只校验顶层 format，`notes` 里混进一个字符串/null
    # 就会在合并到一半时抛 AttributeError —— 前面的笔记已写盘、后面的没写。
    # 整包先验完再开写，坏包一条都不落盘。
    notes = pack.get("notes") or []
    if not isinstance(notes, list):
        print(f"  [跳过] {path.name}: notes 不是数组，整包拒绝")
        return None
    bad = [i for i, rec in enumerate(notes) if not isinstance(rec, dict)]
    if bad:
        print(f"  [跳过] {path.name}: notes 第 {bad[:5]} 项不是对象，"
              f"整包拒绝（避免在写了一半时崩溃）")
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
        print(f"      [拒绝] 未写入（路径不合法或正文为空）: {r}")
    for c in s["conflicts"]:
        print(f"      [冲突] 已另存合并稿，原文件未动: {c}")
    return True


def _watch_once(src: Path, vault: Path, seen: set, dry_run: bool = False) -> None:
    """扫描一轮：把新出现的变更包合并进 vault。

    ⚠⚠ **单个包合并失败不许让整个 watch 进程退出**（2026-11-15 审计）。

    原来这里是 `merge_path(...)` 裸调 —— 而 `_atomic_write_text` 里的
    `os.replace` 在 Windows 上遇到 Obsidian / 杀软 / 坚果云同步客户端
    **占用了那篇笔记** → 抛 `PermissionError`（OSError）
    → 一路冒到 `while True` 外面 → 只被 `except KeyboardInterrupt` 兜着
    → **守护进程当场退出**，后面的变更包再也合并不进来（要人工重启才恢复）。

    同型缺陷 `export_offline.py` 在 2026-11-09 那轮已经修了（单轮 try/except
    + 失败不标记 → 下一轮重试），这里补齐。

    两条规矩：
      1. **失败不写 seen** → 下一轮自动重试
         （跟 export_offline 的 `last = None` 一个道理：
          失败的文件不算「处理过」）；
      2. **失败不打断本轮** → 后面的包照常合并。

    拆成独立函数是为了**能被护栏直接跑**（node/pytest 都跑真代码，
    别让这条只能靠「读源码文本」来守）。
    """
    if not src.is_dir():
        return
    try:
        packs = sorted(src.glob("*.json"))
    except OSError as e:
        # 目录本身读不出来（同步客户端正在换掉它 / 权限变化）→ 本轮跳过，别退出
        print(f"[watch] 变更包目录读不出来，本轮跳过: {e}", file=sys.stderr)
        return

    # 2026-11-09 审计：p.stat() 在包被删/移动时会抛 OSError
    # 让整个 watch 崩；秒级 mtime + 只增不减的 seen 还会漏掉
    # 同秒覆盖写、并让集合长期膨胀。
    current_keys = set()
    for p in packs:
        try:
            st = p.stat()
        except OSError:
            continue
        key = (str(p), st.st_mtime_ns, st.st_size)
        current_keys.add(key)
        if key in seen:
            continue
        print(f"[watch] 合并 {p.name}")
        try:
            merge_path(p, vault, dry_run)
        except Exception as e:
            # ⚠ 不写 seen → 下一轮会重试这个包
            print(f"[watch] 合并 {p.name} 失败（{e}）—— 下一轮自动重试，"
                  "本轮继续处理后面的包", file=sys.stderr)
            continue
        seen.add(key)
    seen.intersection_update(current_keys)


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
            _watch_once(src, vault, seen, args.dry_run)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\n[watch] 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
