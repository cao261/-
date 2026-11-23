# -*- coding: utf-8 -*-
"""远端改动可用性自检 —— **两台设备都能直接跑**。

做三件事，逐项给结论（不是「跑完了」，是「过没过」）：

  ① git：本地 HEAD 与 origin/main 是否一致；工作区是否干净
  ② 服务：/api/paper/outline 对 vault 里**每一篇**论文是否都 200
          （2026-11-22 修的 P0-1：extract_outline 必崩，vault 27 篇里 11 篇必 500）
  ③ 护栏：本轮新增/扩大的护栏是否全绿（可选，--guards 打开）

用法：
    python microbench/_verify_remote.py                 # ① + ②（需要服务已启动）
    python microbench/_verify_remote.py --guards        # ① + ② + ③
    python microbench/_verify_remote.py --port 5011     # 换端口
    python microbench/_verify_remote.py --skip-git      # 跳过 git 检查（纯数据目录）

⚠ 退出码：0 = 全过；1 = 有项目没过。**「SKIP」也算过**（数据本来就不在）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _say(ok: bool | None, label: str, detail: str = "") -> bool:
    """ok=None → SKIP（不是失败，但要说清楚）。"""
    if ok is None:
        print(f"  SKIP  {label}" + (f"  [{detail}]" if detail else ""))
        return True
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {label}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


def _warn(label: str, detail: str = "") -> None:
    """只提醒、不影响退出码（例如本地有未提交改动 —— 不等于远端不可用）。"""
    print(f"  WARN  {label}" + (f"  [{detail}]" if detail else ""))


def _run_git(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def check_git() -> bool:
    print("\n[1] git：本地与 origin/main 对齐了吗")
    rc, out = _run_git(["rev-parse", "HEAD"])
    if rc != 0:
        return _say(None, "不在 git 仓库里", out.strip()[:120])
    head = out.strip()
    _say(True, "本地 HEAD", head[:12])

    rc, out = _run_git(["rev-parse", "origin/main"])
    if rc != 0:
        return _say(None, "读不到 origin/main", "先 git fetch origin")
    remote = out.strip()
    aligned = head == remote
    _say(aligned, "HEAD == origin/main",
         "一致" if aligned else f"HEAD={head[:12]} origin/main={remote[:12]}（要先 git pull）")

    # 工作区有未提交改动 ≠ 远端不可用 → 只提醒，不影响退出码
    rc, out = _run_git(["status", "--short"])
    dirty = [ln for ln in out.splitlines() if ln.strip()]
    if dirty:
        _warn("工作区有未提交改动（不影响远端可用性，但别忘了提交或 stash）",
              f"{len(dirty)} 个文件")
        for ln in dirty[:10]:
            print(f"        {ln}")
    else:
        _say(True, "工作区干净")
    return aligned


def _http_get(url: str, timeout: int = 60) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa
        return -1, str(e).encode("utf-8", "replace")


def check_service(port: int) -> bool:
    print(f"\n[2] 服务（127.0.0.1:{port}）：每篇论文的 outline 都要 200")
    code, _ = _http_get(f"http://127.0.0.1:{port}/", timeout=15)
    if code != 200:
        return _say(False, "服务没起来",
                    f"HTTP {code} —— 先起服务（desktop.py 或 uvicorn microbench.app:app --port {port}）")

    vault = REPO / "01_Literature"
    pdfs = sorted(p for p in vault.rglob("*.pdf") if p.is_file())
    if not pdfs:
        return _say(None, "vault 里没有 PDF", "这台机器还没放论文")

    ok = 0
    bad = []
    for p in pdfs:
        rel = p.relative_to(REPO).as_posix()
        url = (f"http://127.0.0.1:{port}/api/paper/outline?relative_path="
               + urllib.parse.quote(rel, safe=""))
        st, body = _http_get(url)
        if st == 200:
            ok += 1
            try:
                n = len((json.loads(body.decode("utf-8")) or {}).get("sections") or [])
            except Exception:
                n = -1
            print(f"        200  {rel}  (sections={n})")
        else:
            bad.append((rel, st, body[:160].decode("utf-8", "replace")))
            print(f"        FAIL {st} {rel}")
    print()
    good = _say(not bad, f"outline 全通（{ok}/{len(pdfs)} 篇）")
    for rel, st, msg in bad:
        print(f"        {st} {rel} :: {msg}")
    if bad:
        print("        → extract_outline 又崩了？见 AGENTS.md §4.129~4.132 与本轮 P0-1 修复")
    return good


def check_guards() -> bool:
    print("\n[3] 本轮新增/扩大的护栏")
    guards = [
        ("microbench/_test_qa_refine_payload.py", "QA 载荷（真渲染）", 14),
        ("microbench/_test_readpos_resume.py", "阅读续读（真渲染）", 24),
        ("microbench/_test_tablet_export.py", "平板导出（真渲染）", 48),
    ]
    all_ok = True
    for rel, label, expect in guards:
        script = REPO / rel
        if not script.exists():
            all_ok &= _say(None, f"{label}（{rel} 不存在）", "")
            continue
        proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", cwd=str(REPO))
        out = (proc.stdout or "") + (proc.stderr or "")
        m = [ln for ln in out.splitlines() if "FAIL " in ln and "PASS" not in ln]
        got = None
        for ln in reversed(out.splitlines()):
            if ln.startswith("PASS ") and "FAIL" in ln:
                try:
                    got = int(ln.split()[1])
                except Exception:
                    pass
        all_ok &= _say(proc.returncode == 0 and not m,
                       f"{label}（期望 {expect} 断言）",
                       f"实得 {got}" if got is not None else f"退出码 {proc.returncode}")
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--guards", action="store_true", help="连护栏一起跑（慢）")
    ap.add_argument("--skip-git", action="store_true")
    args = ap.parse_args()

    print("=" * 62)
    print("远端改动可用性自检 ——", REPO)
    print("=" * 62)

    ok = True
    if not args.skip_git:
        ok &= check_git()
    ok &= check_service(args.port)
    if args.guards:
        ok &= check_guards()

    print("\n" + "=" * 62)
    print("结论：" + ("全部通过 ✅" if ok else "有项目没过 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
