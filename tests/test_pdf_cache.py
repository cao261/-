# -*- coding: utf-8 -*-
"""PDF 抽取缓存的护栏（2026-10-27 性能优化）。

**为什么要有这份文件**

缓存是**性能**优化，但它是**数据面**的东西 —— 一旦缓存出脏数据，
用户看到的是「打开的论文正文跟昨天不一样」，而且很难查。

所以必须钉死三件事：

1. **缓存键必须包含版本**（文件 mtime/size + 抽取器版本 + 符号词典签名）
   → 任何一个变了都必须失效重建。
2. **缓存不能进同步根** → 两台设备的数据面不能被缓存污染
   （缓存是性能优化，不是数据；各自缓存、各自变快）。
3. **缓存坏了不能挡住功能** → 读失败当未命中，写失败只记日志。
"""
import json
from pathlib import Path

import pytest

from microbench import pdf_cache
from microbench.paper_text import EXTRACTOR_VERSION

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1) 缓存键：任何影响结果的输入变了都必须失效
# ---------------------------------------------------------------------------

def test_cache_key_changes_when_the_pdf_changes(tmp_path):
    """PDF 改了（mtime 或 size 变）→ 缓存键必须变。"""
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"hello world")
    k1 = pdf_cache.cache_key(pdf, None)

    pdf.write_bytes(b"hello world v2 longer")
    k2 = pdf_cache.cache_key(pdf, None)
    assert k1 != k2, "PDF 内容变了，缓存键却没变 → 会返回旧正文"


def test_cache_key_changes_when_the_glyph_dictionary_changes(tmp_path):
    """符号词典变了 → 缓存键必须变。

    词典认出了新符号，文本里的 ⟦G:n⟧ 对应的真实字符就变了
    → 必须重新抽取，不能用旧缓存。
    """
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    d1 = tmp_path / "dict.json"
    d1.write_text("{}", encoding="utf-8")
    k1 = pdf_cache.cache_key(pdf, d1)

    d1.write_text('{"k": "v"}', encoding="utf-8")   # 大小变了
    k2 = pdf_cache.cache_key(pdf, d1)
    assert k1 != k2, "符号词典变了，缓存键却没变 → 会用到旧的符号解析结果"


def test_cache_key_includes_the_extractor_version(tmp_path):
    """缓存键必须包含抽取器版本。

    跟译文快照的版本闸门同款思路（AGENTS.md §4.18）：
    抽取算法改了（栏序、公式、表格、符号锚点…）→ 旧抽取结果一律作废。
    """
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"x")
    key = pdf_cache.cache_key(pdf, None)
    # 键是哈希，看不出内容 —— 所以验的是「版本常量确实参与了计算」：
    # 改掉模块里的 EXTRACTOR_VERSION，键必须跟着变。
    orig = pdf_cache.EXTRACTOR_VERSION
    try:
        pdf_cache.EXTRACTOR_VERSION = orig + 1
        key2 = pdf_cache.cache_key(pdf, None)
    finally:
        pdf_cache.EXTRACTOR_VERSION = orig
    assert key != key2, "抽取器版本没参与缓存键 → 算法改了还在用旧抽取结果"


def test_cache_key_differs_between_different_papers(tmp_path):
    """不同的论文不能共用一个缓存条目。"""
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_bytes(b"same bytes")
    b.write_bytes(b"same bytes")
    assert pdf_cache.cache_key(a, None) != pdf_cache.cache_key(b, None), \
        "两篇不同的论文用了同一个缓存键 → 会互相串正文"


# ---------------------------------------------------------------------------
# 2) 缓存读写：坏了不能挡住功能
# ---------------------------------------------------------------------------

def test_write_then_read_roundtrip(tmp_path, monkeypatch):
    """写进去能原样读出来。"""
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", tmp_path / "cache")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"data")
    pages = [{"page": 1, "text": "hello", "glyphs": []}]
    key = pdf_cache.cache_key(pdf, None)
    pdf_cache.write_cache(pdf, None, pages, key)
    got = pdf_cache.read_cache(pdf, None)
    assert got == pages, "缓存读回来的内容跟写进去的不一样"


def test_corrupted_cache_is_treated_as_a_miss(tmp_path, monkeypatch):
    """缓存文件损坏 → 当作没命中（不能抛异常挡住用户）。"""
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", cache_dir)
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"data")
    key = pdf_cache.cache_key(pdf, None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / (key + ".json")).write_text("{ this is not json", encoding="utf-8")
    assert pdf_cache.read_cache(pdf, None) is None, \
        "损坏的缓存没有被当成未命中"


def test_read_cache_on_missing_file_returns_none(tmp_path, monkeypatch):
    """缓存目录都不存在时，读缓存不能抛异常。"""
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", tmp_path / "nope")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"data")
    assert pdf_cache.read_cache(pdf, None) is None


def test_write_cache_failure_does_not_raise(tmp_path, monkeypatch):
    """缓存写失败（磁盘满/权限）不能抛异常 —— 它只是性能优化。"""
    # 指向一个**不能创建**的路径
    monkeypatch.setattr(pdf_cache, "CACHE_DIR", tmp_path / "a" / "b" / "\0bad")
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"data")
    key = pdf_cache.cache_key(pdf, None)
    # 不抛异常就算过（pytest 默认不允许异常逃出）
    pdf_cache.write_cache(pdf, None, [{"page": 1, "text": "x", "glyphs": []}], key)


# ---------------------------------------------------------------------------
# 3) 缓存的落位：不能进同步根
# ---------------------------------------------------------------------------

def test_cache_lives_outside_the_sync_root():
    """缓存必须放在**同步根之外**。

    ⚠ 为什么：同步根（坚果云）是两台设备的**数据面**。
    缓存写坏 → 两台设备的正文对不上，而且极难排查。
    缓存是**性能**优化，不是数据 → 各自缓存、各自变快，互不影响。
    """
    from microbench.app import _sync_root
    cache_dir = pdf_cache.CACHE_DIR.resolve()
    sync_root = _sync_root().resolve()
    assert cache_dir != sync_root
    assert sync_root not in cache_dir.parents, \
        f"缓存目录 {cache_dir} 落在同步根 {sync_root} 里面 → 会污染两台设备的数据面"


def test_cache_dir_is_under_the_repo_logs():
    """缓存放在仓库 logs/ 下（可 gitignore，不进版本库也不进同步）。"""
    assert pdf_cache.CACHE_DIR.name == "pdf_extract_cache"
    assert pdf_cache.CACHE_DIR.parent.name == "logs"
    assert pdf_cache.CACHE_DIR.parent.parent.resolve() == REPO.resolve()


def test_cache_files_are_gitignored():
    """缓存文件不能进 git（体积大、每台机器不一样）。

    实现方式：`.gitignore` 里已经有 `logs/` 整目录（Obsidian vault 的一部分），
    缓存放在 `logs/pdf_extract_cache/` 下 → 天然被忽略。
    → 这里验的是**实际效果**（git 会不会把缓存列出来），而不是某行字面量。
    """
    import subprocess
    cache_dir = pdf_cache.CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    probe = cache_dir / "_gitignore_probe.json"
    probe.write_text("{}", encoding="utf-8")
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain", "--ignored=no",
             "--", str(cache_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        assert probe.name not in out.stdout, \
            f"缓存文件被 git 当成待提交内容了:\n{out.stdout}"
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 4) 版本闸门跟抽取器版本对齐（防止缓存跨版本复用）
# ---------------------------------------------------------------------------

def test_cache_module_reads_the_current_extractor_version():
    """缓存模块用的 EXTRACTOR_VERSION 必须跟 paper_text 的一致。

    如果缓存模块自己抄了一份常量，paper_text 升级了缓存不会失效
    → 用户看到"算法已修但打开还是旧的正文"。
    """
    assert pdf_cache.EXTRACTOR_VERSION == EXTRACTOR_VERSION, \
        "缓存模块的版本常量跟 paper_text 不一致 → 跨版本复用了旧抽取结果"
