#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Move the vault into a cloud-synced folder, keeping the old path working.

Why
---
The tablet edits notes directly inside the synced folder. For those edits
to reach the PC without the PC running a merge watcher, the folder the
tablet writes to and the folder the PC reads from have to be the *same*
folder on the *same* cloud. The cheapest way to get there is to let the
cloud client own the vault:

    坚果云/我的坚果云/MicroBench工作台/     <- 真正的文件在这里
    D:\\...\\个人工作台                     <- junction 指过去（路径不变）

A Windows junction is used rather than a copy or a symlink because it is
transparent to every tool: Obsidian, the FastAPI server, git, and every
relative path in the project keep working unchanged. Symlinks need
developer mode or admin rights; junctions need neither.

Reversible: `--undo` moves everything back and removes the junction.

Usage:
    python microbench/move_vault_to_cloud.py            # 只看会做什么
    python microbench/move_vault_to_cloud.py --apply    # 真搬
    python microbench/move_vault_to_cloud.py --undo     # 搬回去
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from microbench.export_offline import detect_cloud_dir  # noqa: E402

# vault 的内容。.obsidian 一起搬 —— 换了位置 Obsidian 的设置也跟着走。
VAULT_ITEMS = [
    "00_Dashboard",
    "01_Literature",
    "02_Reproduction",
    "03_Knowledge",
    "04_Weekly_Reports",
    "_Templates",
    ".obsidian",
    "assets",
]

CLOUD_SUBDIR = "MicroBench工作台"


def is_junction(p: Path) -> bool:
    """True if the path is a reparse point (junction/symlink)."""
    try:
        if not p.exists() and not p.is_symlink():
            return False
        st = os.lstat(p)
        # FILE_ATTRIBUTE_REPARSE_POINT
        return bool(getattr(st, "st_file_attributes", 0) & 0x400)
    except OSError:
        return False


def make_junction(link: Path, target: Path) -> bool:
    """Create a directory junction (no admin rights needed)."""
    if hasattr(os, "symlink"):
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            pass
    # fallback: mklink /J
    try:
        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


def dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def move_item(src: Path, dst: Path) -> bool:
    """Move one top-level entry. Uses os.replace when possible (same volume),
    otherwise shutil.move (cross volume = copy + delete)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return True
    except OSError:
        pass
    try:
        shutil.move(str(src), str(dst))
        return True
    except Exception as e:
        print(f"  [失败] {src.name}: {e}")
        return False


def do_move(apply: bool) -> int:
    cloud = detect_cloud_dir()
    if not cloud:
        print("没有找到可用的云盘目录。先把 vault 放进 OneDrive / 坚果云，"
              "或者手动用 --cloud 指定。")
        return 1

    target = cloud / CLOUD_SUBDIR
    print(f"云盘根目录 : {cloud}")
    print(f"目标目录   : {target}")
    print(f"原路径     : {REPO}")
    print()

    if is_junction(REPO):
        print("原路径已经是 junction 了 —— 已经搬过了。")
        print(f"  -> {os.readlink(REPO) if os.path.islink(REPO) else '(reparse point)'}")
        return 0

    items = [REPO / n for n in VAULT_ITEMS if (REPO / n).exists()]
    if not items:
        print("原目录里没有找到 vault 内容，没有可搬的东西。")
        return 1

    total = 0
    for it in items:
        size = dir_size(it) if it.is_dir() else it.stat().st_size
        total += size
        print(f"  {it.name:<20} {human(size):>10}")
    print(f"  {'合计':<20} {human(total):>10}")
    print()

    if not apply:
        print("这是演练，没有动任何文件。")
        print(f"确认无误后执行:  python microbench/move_vault_to_cloud.py --apply")
        print()
        print("搬之前请先关掉 Obsidian —— 它开着会锁住 .obsidian 里的文件。")
        return 0

    print("开始搬迁 ...")
    moved = []
    for it in items:
        dst = target / it.name
        if dst.exists():
            print(f"  [跳过] {it.name}: 目标已存在")
            continue
        if move_item(it, dst):
            print(f"  [OK] {it.name}")
            moved.append(it.name)
        else:
            print(f"  [失败] {it.name}")
            return 1

    print()
    print("建立 junction (原路径指向新位置) ...")
    # junction 的父目录就是 REPO 本身 —— 不能把 REPO 变成 junction，
    # 而是把 REPO 下的每个条目各自变成 junction。
    ok = True
    for name in moved:
        link = REPO / name
        dst = target / name
        if make_junction(link, dst):
            print(f"  [OK] {name} -> {dst}")
        else:
            print(f"  [失败] {name}: 建不了 junction")
            ok = False
    if not ok:
        print("\n有 junction 没建成功 —— 原路径现在是空的，文件都在云盘目录里。")
        print(f"手动补:  mklink /J \"{REPO / moved[0]}\" \"{target / moved[0]}\"")
        return 1

    print()
    print("搬迁完成。")
    print(f"  真实位置: {target}")
    print(f"  原路径  : {REPO}  (junction, 用法完全不变)")
    print()
    print("下一步:")
    print("  1. 确认坚果云已经把这个目录上传完 (等上传量不为 0)")
    print("  2. 平板上装坚果云 App, 等它把目录同步下来")
    print("  3. 平板用 Chrome 打开 MicroBench_平板工作台.html")
    print("     设置 -> 连接文件夹 -> 选坚果云里的 MicroBench工作台 目录")
    print("  4. 之后平板的改动会自动上云, PC 这边不用开着")
    return 0


def do_undo() -> int:
    cloud = detect_cloud_dir()
    if not cloud:
        print("找不到云盘目录，先用 --cloud 指定。")
        return 1
    target = cloud / CLOUD_SUBDIR
    if not target.is_dir():
        print(f"目标目录不存在: {target}")
        return 1

    print("搬回原路径 ...")
    for name in VAULT_ITEMS:
        link = REPO / name
        dst = target / name
        if not dst.exists():
            continue
        if link.exists() or is_junction(link):
            # 先删掉 junction 本身（不能删它的内容）
            if link.is_symlink() or is_junction(link):
                try:
                    os.rmdir(link) if link.is_dir() and not link.is_symlink() else os.unlink(link)
                except OSError as e:
                    print(f"  [失败] 删 junction {name}: {e}")
                    continue
            elif link.is_dir():
                print(f"  [跳过] {name}: 原路径已有同名目录")
                continue
        if move_item(dst, link):
            print(f"  [OK] {name}")
        else:
            return 1

    try:
        target.rmdir()
    except OSError:
        pass
    print("已搬回原路径。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把 vault 搬进云盘目录, 原路径用 junction 保持不变")
    ap.add_argument("--apply", action="store_true", help="真的执行 (默认只演练)")
    ap.add_argument("--undo", action="store_true", help="搬回原路径")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    if args.undo:
        return do_undo()
    return do_move(args.apply)


if __name__ == "__main__":
    sys.exit(main())
