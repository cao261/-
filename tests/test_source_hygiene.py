# -*- coding: utf-8 -*-
"""源码卫生护栏：NUL 字节 / BOM / 行尾。

**为什么要有这个文件**（2026-11-17 全面功能审查 §4.103）：
  `microbench/static/tablet/app.js` 里混进了 **1 个 NUL 字节（0x00）**。
  ripgrep 判定「二进制文件」→ **整个文件被直接跳过** →
  任何基于 grep 的检索/审计都**一条都搜不到**，
  看起来就像「这个文件里没有这些功能」——
  本轮就有好几次搜索全部落空，差点把「平板端没实现划词带图」当成事实。

  这类问题**不报任何错**，只是让工具**安静地少给你看东西**。

**检查项**：
  ① 源码文件里不能有 NUL 字节（否则 ripgrep / git diff / 编辑器都会当二进制）；
  ② 源码文件不能有 BOM（`.gitattributes` 要求 UTF-8 无 BOM）；
  ③ `.py` / `.js` / `.html` / `.css` 必须是 **LF**（仓库约定：`* text=auto eol=lf`；
     `.bat` 必须是 CRLF —— cmd.exe 按 LF 解析会错位，见 AGENTS §2.6）。
"""
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

SOURCE_GLOBS = ("*.py", "*.js", "*.html", "*.css", "*.md", "*.json", "*.ps1", "*.java")
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    "logs", "_vendor", "vendor", ".obsidian", "_stub", "res",
}
# 生成物 / 二进制产物不在管辖范围内
SKIP_SUFFIX = {".apk", ".pdf", ".woff", ".woff2", ".ttf", ".png", ".gif",
               ".jpg", ".ico", ".idsig", ".jks", ".zip", ".class", ".dex"}


def _tracked_files() -> list:
    """扫描 git 已跟踪文件及新增未跟踪的桌面/路由/安卓源码文件（排除生成物）。"""
    import subprocess
    seen: set[Path] = set()
    names: list[Path] = []
    allowed_suffix = {".py", ".js", ".html", ".css", ".md", ".json", ".ps1", ".bat", ".java"}

    def _add(path: Path) -> None:
        rp = path.resolve()
        if rp in seen or not path.is_file():
            return
        if path.suffix.lower() not in allowed_suffix:
            return
        try:
            parts = path.relative_to(REPO).parts
        except ValueError:
            return
        if any(part in SKIP_DIRS for part in parts):
            return
        seen.add(rp)
        names.append(path)

    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(REPO),
            capture_output=True, timeout=60,
        )
    except Exception:
        out = None
    if out is not None and out.returncode == 0:
        for raw in out.stdout.split(b"\x00"):
            if raw:
                _add(REPO / raw.decode("utf-8", "replace"))
        # 同时纳管工作区中尚未 git add 的新源码目录与测试文件
        for scan_root in (REPO / "microbench", REPO / "tests", REPO / "tools", REPO / "android" / "java"):
            if scan_root.is_dir():
                for path in sorted(scan_root.rglob("*")):
                    if path.suffix.lower() in {".py", ".js", ".html", ".css", ".java", ".ps1"}:
                        _add(path)
        return names

    for glob in SOURCE_GLOBS:
        for path in REPO.rglob(glob):
            if path.suffix.lower() in SKIP_SUFFIX:
                continue
            _add(path)
    return names


def _source_files():
    return _tracked_files()


@pytest.mark.parametrize("path", list(_source_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_source_file_has_no_nul_byte(path: Path):
    """源码里不能有 NUL 字节 —— 否则 ripgrep/git 会把它当二进制（整个文件隐身）。"""
    data = path.read_bytes()
    idx = data.find(b"\x00")
    assert idx < 0, (
        f"{path.relative_to(REPO)} 第 {idx} 字节是 NUL(0x00) —— "
        f"ripgrep 会把整个文件当二进制跳过，检索/审计会静默地搜不到任何东西")


@pytest.mark.parametrize("path", list(_source_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_source_file_has_no_bom(path: Path):
    """源码必须是 UTF-8 无 BOM（BOM 会让某些工具把首行读成乱码）。"""
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), (
        f"{path.relative_to(REPO)} 带 UTF-8 BOM —— 仓库约定是无 BOM")


@pytest.mark.parametrize("path", list(_source_files()), ids=lambda p: str(p.relative_to(REPO)))
def test_source_file_uses_expected_line_endings(path: Path):
    """`.py/.js/.html/.css` 必须 LF；`.bat` 必须 CRLF（cmd.exe 按 LF 解析会错位）。"""
    rel = path.relative_to(REPO)
    data = path.read_bytes()
    if path.suffix.lower() == ".bat":
        # .bat 里出现 CRLF 即可（混合也算过，但要真的有 CRLF）
        assert b"\r\n" in data, f"{rel} 没有 CRLF —— cmd.exe 按 LF 解析会错位"
        return
    if path.suffix.lower() not in {".py", ".js", ".html", ".css", ".java"}:
        return
    assert b"\r\n" not in data, (
        f"{rel} 里有 CRLF —— 仓库约定 .py/.js/.html/.css 全 LF"
        f"（.gitattributes: * text=auto eol=lf）")
