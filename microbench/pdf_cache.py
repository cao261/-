# -*- coding: utf-8 -*-
"""PDF 抽取结果的**磁盘缓存**（2026-10-27 性能优化）。

## 为什么需要

实测（2026-10-27，本机全库 27 篇 PDF）：

    抽取一遍 = 72.2 秒，平均 **2.7 秒/篇**，最慢 **9.7 秒**（36 页那篇）

瓶颈在 **pdfplumber 的页面解析**（pdfminer 逐页解内容流）：
`extract_text` 和 `extract_words` 共享同一份 chars 缓存，
所以它们**不是**重复解析 —— 慢的是首次解析本身。

试过的三条路（都不行 / 收益小）：

| 思路 | 实测 | 结论 |
|---|---|---|
| 线程池并行抽页 | 4 workers **0.97x**，8 workers **0.74x** | ❌ GIL + 内部锁，越多越慢 |
| 换 PyMuPDF 抽文本 | 快 **20.6x**（52ms vs 1075ms） | ❌ 会破坏双栏顺序（§4.5 红线）+ 丢符号标记 |
| **别重复抽** | 第二遍 **82ms**（842x） | ✅ **采纳** |

## 为什么这个缓存是安全的

缓存键包含三样东西，缺一个就会缓存出脏数据：

1. **PDF 的 (mtime_ns, size)** —— 文件改了必须失效
2. **`EXTRACTOR_VERSION`** —— 抽取算法改了必须失效
   （跟译文快照的版本闸门同款思路，AGENTS.md §4.18）
3. **符号词典的 (mtime_ns, size)** —— 词典认出了新符号，
   文本里的 ⟦G:n⟧ 标记对应的真实字符会变 → 必须重解

## 放哪、为什么不进同步

放在 vault 的 `logs/` 下 —— **不**在坚果云同步根里：

- 缓存是**性能**优化，不是**数据**。两台设备各自缓存、各自抽一次、各自变快，
  互不影响，也不会让同步通道多传一份。
- 反过来说：**数据面不能被缓存污染**（缓存写坏 → 两台设备的正文对不上）。

## 缓存不缓存什么

**只缓存原始抽取结果**（每页的 text + glyphs 列表）。
章节切分、符号认字、翻译缓存都不在这里 —— 那些各自有自己的闸门。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from .paper_text import EXTRACTOR_VERSION

# 缓存文件格式版本。改了这个 → 所有旧缓存全部失效（安全）。
CACHE_FORMAT_VERSION = 1

# 缓存目录：仓库的 logs/ 下（不进坚果云同步根，见模块文档）
_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _REPO_ROOT / "logs" / "pdf_extract_cache"


def _sig(path: Optional[Path]) -> list:
    """文件的 (mtime_ns, size) 签名；文件不存在返回 [0, 0]。"""
    try:
        st = path.stat()
        return [int(st.st_mtime_ns), int(st.st_size)]
    except (OSError, AttributeError, TypeError):
        return [0, 0]


def cache_key(pdf_path: Path, dict_path: Optional[Path] = None) -> str:
    """算这篇 PDF 的缓存键。"""
    raw = "|".join([
        str(Path(pdf_path).resolve()),
        str(_sig(Path(pdf_path))),
        str(EXTRACTOR_VERSION),
        str(_sig(Path(dict_path)) if dict_path else [0, 0]),
        str(CACHE_FORMAT_VERSION),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_file(key: str) -> Path:
    return CACHE_DIR / (key + ".json")


def read_cache(pdf_path: Path, dict_path: Optional[Path] = None):
    """读缓存。命中返回 pages（list），未命中返回 None。"""
    try:
        key = cache_key(pdf_path, dict_path)
        f = _cache_file(key)
        if not f.is_file():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("key") != key:
            return None
        pages = data.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        return pages
    except Exception:
        # 缓存坏了不能挡住正事 —— 当作没命中，重新抽
        return None


def write_cache(pdf_path: Path, dict_path: Optional[Path], pages: list, key: str) -> None:
    """写缓存。失败就失败（性能优化，不该影响功能）。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = _cache_file(key)
        # 先写临时文件再原子替换 —— 避免中途崩溃留下半个 JSON
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "pages": pages}, fh, ensure_ascii=False)
            os.replace(tmp, f)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        print(f"[pdfcache] 写缓存失败（忽略，不影响功能）: {e}", file=sys.stderr)


def clear_cache() -> int:
    """清空缓存，返回清掉的文件数。"""
    n = 0
    try:
        if CACHE_DIR.is_dir():
            for f in CACHE_DIR.glob("*.json"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    except Exception:
        pass
    return n
