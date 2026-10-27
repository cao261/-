# -*- coding: utf-8 -*-
"""定期把「另一台设备」推上来的升级同步到本机 **和** 平板 APK。

背景
----
两台设备共用一个 GitHub 仓库（`cao261/-`），代码走 GitHub、数据走坚果云。
对方设备一直在做桌面端的升级（阅读器、多模态读图、独立窗口……），
本机 + 平板 APK 必须**跟着一起升**，否则就是「两个版本」：
用户会在电脑上看到新版、在平板上看到旧版，还以为是两个 bug。

这个脚本就是干这件事的，**幂等**：没有新提交就什么都不做。

流程（每一步失败都会**停手**，绝不把半成品推出去）
--------------------------------------------------
1. 工作区必须干净（不干净就跳过 —— 不碰用户正在改的东西）
2. `git fetch origin`（这台机 GitHub 访问需要默认代理；清代理要重试）
3. 没有新提交 → 直接结束
4. 合并 `origin/main`
   - **有冲突** → `git merge --abort` → 报告 → 结束（**不推**）
5. 跑全套测试（pytest 254 项 + 12 个独立套件）
   - **有红的** → `git merge --abort` → 报告 → 结束（**不推**）
     （带病上线比不同步更糟：对方 pull 下来就坏了）
6. 重打平板 APK（`android/build_apk.ps1`，末尾自带交付验收）
   - 失败 → 报告 → 结束（**不推**）
7. `git push origin main`（对方设备 pull 就能拿到）
8. 报告：合了什么、APK 的 SHA-256、要不要重启服务

关于「自动 push」
---------------
只在**测试全绿**之后才推。对方设备直接 pull 这个分支 —— 推出去的就是
「已经验过的」版本。测试红 = 对方这次提交有问题，此时**回退合并**并报告，
让对方去修，而不是把问题扩散到两台设备。

跑法
----
    python microbench/auto_sync.py                # 正常同步
    python microbench/auto_sync.py --dry-run      # 只看有没有新提交，不动任何东西
    python microbench/auto_sync.py --no-push      # 合并+重打 APK，但不推 GitHub
    python microbench/auto_sync.py --no-apk       # 只同步代码，不重打 APK

输出
----
最后一行是 `MB_SYNC_RESULT: {json}`，给定时任务的 agent 读。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# 已知的绝对路径只是**兜底**：两台设备都把 PowerShell 7 装在**用户目录**下
# （%LOCALAPPDATA%\Programs\PowerShell\7\pwsh.exe），路径里的用户名不同。
# 所以真正可靠的是先问 PATH，再按 %LOCALAPPDATA% 通配 —— 写死某个用户名
# 会在另一台设备上悄悄失效（实测：本机 25353 跑这脚本时找不到 pwsh，
# 于是每次都「跳过重打 APK」，而日志里只有一行不像错误的提示）。
PWSH_CANDIDATES = [
    r"D:\Program Files\PowerShell\7\pwsh.exe",
    r"C:\Program Files\PowerShell\7\pwsh.exe",
]
PROXY = "http://127.0.0.1:65532"

# pytest 收不到的独立套件（`_test_*.py` / `_test_*.js`）—— 跑一条红一条。
# ⚠ `microbench/_test_regressions.py` 与 `_test_lan.py` 必须用**模块模式**跑：
#   `python microbench/_test_regressions.py` 会因为相对导入而失败（假红）。
EXTRA_SUITES = [
    ("py", "-m", "microbench._test_regressions"),
    ("py", "-m", "microbench._test_lan"),
    ("py", "microbench/_test_tablet_sync.py"),
    ("py", "microbench/_test_tablet_export.py"),
    ("py", "microbench/_test_offline_export.py"),
    ("py", "android/_test_dav.py"),
    ("py", "android/_test_apk_contract.py"),
    ("js", "microbench/_test_tablet_renderer.js"),
    ("js", "microbench/_test_tablet_ai.js"),
    ("js", "microbench/_test_tablet_hardening.js"),
    ("js", "microbench/_test_tablet_xss.js"),
    ("js", "microbench/_test_tablet_dav.js"),
    ("js", "microbench/_test_reader_annotations.js"),
    ("js", "microbench/_test_reader_markdown.js"),
]


_LOG_FH = None


def log(msg: str) -> None:
    print(msg, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(msg + "\n")
            _LOG_FH.flush()
        except (OSError, ValueError):
            pass


def open_log():
    """日志落盘：logs/auto_sync.log（追加）。

    为什么要落盘：定时任务在后台跑，出问题时用户不会盯着屏幕。
    有一份历史记录，才能回答「上周三那次同步到底拉了什么」。
    """
    global _LOG_FH
    try:
        d = REPO / "logs"
        d.mkdir(parents=True, exist_ok=True)
        _LOG_FH = open(d / "auto_sync.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        _LOG_FH = None


def run(cmd, timeout=1800, env=None):
    """跑一个命令，返回 (rc, 输出文本)。"""
    try:
        p = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=env)
    except subprocess.TimeoutExpired:
        return 124, "超时（%ss）" % timeout
    except OSError as exc:
        return 127, "起不来: %s" % exc
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def git(*args, env=None, timeout=600):
    return run(["git", "-C", str(REPO)] + list(args), timeout=timeout, env=env)


def env_with_proxy(use_proxy: bool):
    e = dict(os.environ)
    if use_proxy:
        e["HTTP_PROXY"] = PROXY
        e["HTTPS_PROXY"] = PROXY
        e["http_proxy"] = PROXY
        e["https_proxy"] = PROXY
    else:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            e[k] = ""
    return e


def git_with_proxy_retry(*args):
    """这台机：GitHub 访问**需要**默认代理；清代理的 push 有时也能通。
    → 两种都试（顺序：带代理 → 清代理 → 再带代理）。"""
    last = ""
    for use_proxy in (True, False, True):
        rc, out = git(*args, env=env_with_proxy(use_proxy))
        if rc == 0:
            return rc, out
        last = out
        time.sleep(2)
    return 1, last


def workspace_clean() -> tuple[bool, str]:
    rc, out = git("status", "--porcelain")
    if rc != 0:
        return False, "git status 失败: " + out[-300:]
    return (not out.strip()), out.strip()


def head_short():
    rc, out = git("rev-parse", "--short", "HEAD")
    return out.strip() if rc == 0 else "?"


def run_tests() -> tuple[bool, list[str]]:
    """全套测试。返回 (全绿?, 失败明细)。"""
    failures = []

    log("  [1/2] pytest tests -m 'not slow' ...")
    t0 = time.time()
    rc, out = run([sys.executable, "-m", "pytest", "tests", "-q", "-m", "not slow"],
                  timeout=1800)
    log("      用时 %.0fs" % (time.time() - t0))
    if rc != 0:
        failures.append("pytest 失败:\n" + out[-2500:])
    else:
        tail = [ln for ln in out.splitlines() if "passed" in ln]
        log("      " + (tail[-1] if tail else "OK"))

    log("  [2/2] 12 个独立套件 ...")
    for spec in EXTRA_SUITES:
        kind = spec[0]
        cmd = ([sys.executable] + list(spec[1:])) if kind == "py" else ["node"] + list(spec[1:])
        rc, out = run(cmd, timeout=900)
        if rc != 0:
            failures.append("套件失败: %s\n%s" % (" ".join(spec[1:]), out[-1200:]))
        else:
            log("      OK  %s" % " ".join(spec[1:]))
    return (not failures), failures


def find_pwsh() -> Path | None:
    """找到 PowerShell 7 的可执行文件，找不到返回 None。

    顺序（最可靠 → 最不依赖环境）：
      1. PATH 上的 `pwsh`（两台设备都把用户目录写进了用户级 PATH）
      2. 程序目录下的绝对路径（系统级安装 / 非标准盘符）
      3. `%LOCALAPPDATA%\\Programs\\PowerShell\\<版本>\\pwsh.exe` 通配，
         版本号取**最大的**（同一台机器可能装着 7.4 和 7.6）
    """
    exe = shutil.which("pwsh")
    if exe:
        p = Path(exe)
        if p.is_file():
            return p

    for raw in PWSH_CANDIDATES:
        p = Path(raw)
        if p.is_file():
            return p

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        base = Path(local_appdata) / "Programs" / "PowerShell"
        try:
            # 按版本号倒序：7.10 > 7.6 > 7.4（字符串排序会把 7.10 排在 7.6 前面，
            # 但两者都能用，这里只需要「优先新的」，不追求严格语义版本比较）
            for p in sorted(base.glob("*/pwsh.exe"), reverse=True):
                if p.is_file():
                    return p
        except OSError:
            pass
    return None


def build_apk() -> tuple[bool, str]:
    """重打平板 APK（脚本末尾自带交付验收）。"""
    pwsh = find_pwsh()
    if pwsh is None:
        return False, ("找不到 PowerShell 7（PATH、程序目录、%LOCALAPPDATA% 都没有）"
                       "—— 跳过重打 APK")
    script = REPO / "android" / "build_apk.ps1"
    if not script.exists():
        return False, "找不到 android/build_apk.ps1"
    log("  重打平板 APK（%s）..." % pwsh.name)
    rc, out = run([str(pwsh), "-NoProfile", "-File", str(script)], timeout=1800)
    if rc != 0:
        return False, "构建失败:\n" + out[-2500:]
    for ln in out.splitlines():
        if "APK:" in ln or "交付验收通过" in ln:
            log("      " + ln.strip())
    return True, out


def apk_fingerprint() -> dict:
    apk = REPO / "MicroBench-平板工作台.apk"
    if not apk.exists():
        return {}
    h = hashlib.sha256(apk.read_bytes()).hexdigest().upper()
    return {"path": str(apk), "bytes": apk.stat().st_size, "sha256": h}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看有没有新提交")
    ap.add_argument("--no-push", action="store_true", help="不推 GitHub")
    ap.add_argument("--no-apk", action="store_true", help="不重打 APK")
    args = ap.parse_args()

    # ⚠ 控制台编码：Windows 上 stdout 默认按 GBK(936) 编码，
    #   遇到「⚠」这种 GBK 编不出来的字符会抛 UnicodeEncodeError ——
    #   而这里抛的时机**最坏**：同步已经全部做完（测试绿、APK 好、push 成功），
    #   结果最后一行 `MB_SYNC_RESULT` 打不出来 → 脚本 exit 1 →
    #   定时任务的 agent 以为同步失败了。
    #   （实测踩过：2026-10-25 那轮 push 成功了，脚本却报失败。）
    # → 兜底：errors="replace"，编不出来的字符换成 '?'，**绝不崩**。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    started = datetime.now()
    open_log()
    log("=== MicroBench 自动同步 · %s ===" % started.strftime("%Y-%m-%d %H:%M:%S"))
    result = {
        "ok": False, "action": "none", "at": started.isoformat(timespec="seconds"),
        "head_before": head_short(), "pushed": False, "apk": {},
        "messages": [],
    }

    def finish(**kw):
        result.update(kw)
        result["ok"] = bool(result.get("ok"))
        print("MB_SYNC_RESULT: " + json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result["ok"] else 1

    # 1) 工作区必须干净
    clean, detail = workspace_clean()
    if not clean:
        result["action"] = "skipped_dirty"
        result["messages"].append("工作区有未提交改动，本次不碰（避免把临时改动混进合并）")
        log("!! 工作区不干净，跳过：")
        log("   " + detail[:600].replace("\n", "\n   "))
        return finish()

    # 2) fetch
    log("fetch origin ...")
    rc, out = git_with_proxy_retry("fetch", "origin")
    if rc != 0:
        result["action"] = "fetch_failed"
        result["messages"].append("fetch 失败: " + out[-300:])
        log("!! fetch 失败: " + out[-300:])
        return finish()

    rc, remote = git("rev-parse", "origin/main")
    remote = remote.strip() if rc == 0 else ""
    rc, local = git("rev-parse", "HEAD")
    local = local.strip() if rc == 0 else ""
    log("本地 %s  远端 %s" % (head_short(), remote[:7]))

    if not remote or remote == local:
        result["action"] = "up_to_date"
        result["ok"] = True       # 「没有新东西」是**正常结束**，不是失败
        result["messages"].append("没有新提交，无需同步")
        log("== 已是最新，无需同步 ==")
        return finish()

    # 远端有没有新东西（本地是不是远端的祖先）
    rc, _ = git("merge-base", "--is-ancestor", remote, local)
    if rc == 0:
        # 本地已经包含了远端 —— 说明本机自己推过（反方向），不用合并
        result["action"] = "local_ahead"
        result["ok"] = True
        result["messages"].append("本地已包含远端全部提交（没有需要同步的新升级）")
        log("== 远端没有本机没有的提交，无需同步 ==")
        return finish()

    if args.dry_run:
        result["action"] = "would_update"
        result["ok"] = True
        rc, out = git("log", "--oneline", f"{local}..{remote}")
        result["messages"].append("有 %d 个新提交待同步" % len(out.splitlines()))
        log("== 有新提交（dry-run，不动手）==\n" + out[:1500])
        return finish()

    # 3) 备份分支（万一合出问题了能回退）
    backup = "backup-auto-sync-%s" % started.strftime("%Y%m%d-%H%M")
    git("branch", backup, local)

    # 4) 合并
    log("合并 origin/main ...")
    rc, out = git("merge", "origin/main", "--no-edit")
    if rc != 0:
        conflicted = []
        rc2, st = git("status", "--porcelain")
        for ln in (st or "").splitlines():
            if ln.startswith("UU") or ln.startswith("AA") or ln.startswith("DD"):
                conflicted.append(ln[3:].strip())
        git("merge", "--abort")
        result["action"] = "conflict"
        result["messages"].append(
            "合并冲突，已回退（没推）。冲突文件: " + ("、".join(conflicted) or "见 git status"))
        log("!! 合并冲突，已 abort：\n   " + "\n   ".join(conflicted))
        log("   备份分支: " + backup)
        return finish()

    merged = git("log", "--oneline", f"{local}..HEAD")
    n_new = len([ln for ln in (merged[1] or "").splitlines() if ln.strip()])
    log("  合进来 %d 个提交" % n_new)

    # 5) 测试
    log("跑全套测试 ...")
    green, failures = run_tests()
    if not green:
        git("merge", "--abort")
        result["action"] = "tests_failed"
        result["messages"].append(
            "测试没过，已回退合并（**没有推**）。对方这次提交有问题，"
            "详情:\n" + "\n".join(failures)[:2500])
        log("!! 测试没过，已 abort，不推。")
        for f in failures:
            log(f[:2000])
        log("   备份分支: " + backup)
        return finish()

    # 6) 重打 APK
    if args.no_apk:
        log("跳过重打 APK（--no-apk）")
    else:
        ok, out = build_apk()
        if not ok:
            result["action"] = "apk_failed"
            result["messages"].append("重打 APK 失败，**没有推**:\n" + out[:2000])
            log("!! 重打 APK 失败，不推:\n" + out[-2000:])
            return finish()
        result["apk"] = apk_fingerprint()

    # 7) 推
    if args.no_push:
        result["action"] = "merged_local_only"
        result["messages"].append("已合并 + 重打 APK，但按要求**没有推 GitHub**")
        log("== 已合并（未推送）==")
    else:
        log("push origin main ...")
        rc, out = git_with_proxy_retry("push", "origin", "main")
        if rc != 0:
            result["action"] = "push_failed"
            result["messages"].append("push 失败（合并保留在本机）:\n" + out[-800:])
            log("!! push 失败:\n" + out[-800:])
            return finish()
        result["pushed"] = True
        result["action"] = "updated"
        log("== 推送成功 ==")

    result["head_after"] = head_short()
    result["new_commits"] = n_new
    result["backup_branch"] = backup
    result["ok"] = True          # ← 走到这里就说明这一轮是成功的
    result["messages"].append(
        "已同步 %d 个提交，测试全绿%s" % (n_new, "，并已推送到 GitHub" if result["pushed"] else ""))
    if result.get("apk"):
        result["messages"].append(
            "平板 APK 已重打（%d KB，SHA-256 %s…）"
            % (result["apk"]["bytes"] // 1024, result["apk"]["sha256"][:16]))
    result["messages"].append(
        "注意：后端服务仍是**旧进程**：要生效需重启工作台窗口（合入的代码只影响重启后的运行态）")
    return finish()


if __name__ == "__main__":
    sys.exit(main())
