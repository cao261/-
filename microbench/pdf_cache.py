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

缓存键包含四样东西，缺一个就会缓存出脏数据：

1. **PDF 的 (mtime_ns, size)** —— 文件改了必须失效
2. **`EXTRACTOR_VERSION`** —— 抽取**有效性**改了必须失效
   （跟译文快照的版本闸门同款思路，AGENTS.md §4.18）
3. **`EXTRACT_IMPL_REV`** —— 抽取**实现**改了必须失效
   （与第 2 条**故意分开**：见下面「两个闸门为什么要拆开」）

### 两个闸门为什么要拆开（2026-10-27）

`EXTRACTOR_VERSION` 一动，**用户的译文快照整份作废**（要重翻，跨设备同步）。
但抽取实现改了，**不一定**要作废译文 —— 判据是「旧译文是否还对得上新抽取」。

批次 B 就是这种情形：`_is_mathish_line` 改了判据 → 429 页里 10 页输出变了，
但逐页取证 + fitz 裁图证明落在用户已译范围内的只有 1 页，且那一页是**改进**
（散文词 "as" 不再被吞进公式图）→ 译文快照**一页都不作废**。

可缓存里存的是**旧实现**抽出来的文本 → 必须失效。所以加了 `EXTRACT_IMPL_REV`：
**只让缓存重建（72s），不动用户的译文。**

⚠ 不做这一步会进入一个**没有告警的不一致状态**：同一份 PDF、同一份代码，
缓存冷热不同 → 结果不同；而且前端检测不到 —— 前端的逐页 `text_hash`
是按**客户端 pdf.js** 抽的文本算的，服务端抽取算法变了它一无所知
→ 旧译文配新抽取 = 错位。

### 为什么**不**把符号词典的签名放进来（2026-10-27 修正）

最初的版本把 `logs/glyph_dict.json` 的 (mtime, size) 也算进了键，理由是
「词典认出了新符号，文本里的 ⟦G:n⟧ 对应的真实字符会变」。

**这个理由不成立**：这个缓存存的是 `extract_text_from_pdf()` 的**原始输出**，
而 `paper_text.py` **全程不读词典**（它连 import 都没有）。文本里的
`⟦G:<下标>⟧` 是**页内 glyphs 列表的下标**，不是词典序号；把标记换成真实
字符是后面 `_resolve_document_glyphs()` 干的事，那时缓存已经写完了。

代价却是实打实的：后台认字（`_schedule_glyph_backfill`）每认出一个新符号
就 `save()` 一次词典 → 签名变 → **全库 27 篇的缓存一起失效**，
下次打开每篇都要重抽 2.7 秒（最慢 9.7 秒）。也就是说「后台认字越勤，
用户打开论文越慢」—— 正是这个模块要解决的问题本身。

**守住这条结论的前提**：写进缓存的必须始终是 `extract_text_from_pdf()`
的原始输出（`app.py` 的 `api_open_vault_pdf` 里，`write_cache` 必须在
`_resolve_document_glyphs` **之前**）。`tests/test_pdf_cache.py` 有一条
断言钉住「抽取层不依赖词典」。

> ⚠ 本机审计轮曾把「词典**路径**」也加进键，想防「没有词典」与
> 「词典文件不存在」撞键（两者都算 `[0, 0]`）。**那个修法是错的方向**：
> 抽取层不读词典 → 词典维度对缓存内容**没有任何影响** → 加上它只会让
> **不同调用点用不同参数算出不同键** → 一份写一份读 → 缓存**永远命中不了**
> （比撞键更糟：842x 加速完全失效，用户每次都等 2.7~9.7 秒）。
> → 正确做法是**所有调用点用同一个签名**（单一真相），并靠上面的
> `EXTRACT_IMPL_REV` 覆盖「抽取实现变了」这一真正需要失效的场景。

## 磁盘布局：唯一文件名（2026-10-27 审计 H-2）

快照文件 = `<key>.<uuid4>.json`，**每次写都是新文件，永不替换已存在的文件**。

为什么不用「固定文件名 + `os.replace`」：

    Windows 的 ReplaceFile/MoveFileEx 对**已被打开读**的目标文件返回
    ERROR_ACCESS_DENIED（Python 的 open 默认不带 FILE_SHARE_DELETE）。

实测（2026-10-27）：6 线程并发写同一个 key → **32 次**
`PermissionError(13, '拒绝访问。')`；一线程持续读 + 一线程持续写（2 秒）
→ **782 次** `PermissionError`。后果是双重的：

1. 缓存**写不进去** → 842x 加速在最需要它的并发场景拿不到；
2. 读侧把失败当「未命中」→ 用户每次都等 2.7~9.7 秒重抽。

（为什么不用「写个 `current` 指针文件」：指针文件同样会被读侧频繁打开
→ 替换指针一样撞 ERROR_ACCESS_DENIED。）

→ 改成「**唯一文件名 + 按 mtime 取最新**」：
写侧零竞争（`os.replace` 的目标是**全新**文件名，不会撞任何已打开的句柄），
读侧「同 key 的快照里最新的那个」就是当前快照。
同一个 key 的多个快照内容等价（key 里已经编码了 PDF/版本/实现修订号），
所以并发写多个文件、读侧挑最新的，**结果永远正确**。

## 放哪、为什么不进同步

放在 vault 的 `logs/` 下 —— **不**在坚果云同步根里：

- 缓存是**性能**优化，不是**数据**。两台设备各自缓存、各自抽一次、各自变快，
  互不影响，也不会让同步通道多传一份。
- 反过来说：**数据面不能被缓存污染**（缓存写坏 → 两台设备的正文对不上）。

## 缓存不缓存什么

**只缓存原始抽取结果**（每页的 text + glyphs 列表）。
章节切分、符号认字、翻译缓存都不在这里 —— 那些各自有自己的闸门。

## 孤儿清理（L-5）

缓存键一变（PDF 改了 / 词典改了 / 算法升级了），旧 key 的文件**永远不会再被读到**，
但没人删 → 孤儿堆积（实测 29 个文件 vs 27 篇 PDF，2.58MB 孤儿）。

→ `prune_cache()`：按 **TTL（默认 14 天）+ 文件数上限（默认 200，按 mtime LRU）** 清理。
调用点：**写路径**（节流后最多 5 分钟一次）+ `clear_cache()`。
⚠ **绝不在读路径上做清理** —— 读路径必须快。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from .paper_text import EXTRACTOR_VERSION

# 缓存文件格式版本。改了这个 → 所有旧缓存全部失效（安全）。
CACHE_FORMAT_VERSION = 1

# 抽取**实现**的修订号（缓存面闸门）。
#
# ⚠ 与 `EXTRACTOR_VERSION` 是**两回事**，别混：
#
#   EXTRACTOR_VERSION  数据面闸门：抽取**有效性**变了 → 旧译文快照整份作废
#                      （跨设备同步，代价 = 用户把全部译文重翻一遍）
#   EXTRACT_IMPL_REV   性能面闸门：抽取**实现**变了（哪怕输出只差一个字）
#                      → 磁盘缓存作废重抽（本机 27 篇重抽一次 ≈ 72s，
#                        两台设备各自付，互不影响）
#
# 2026-10-27 批次 B 正是这种情形：`_is_mathish_line` 改了判据 →
# 全库 429 页里 **10 页**的抽取输出变了。但逐页取证证明：
#   - 只有 1 页落在用户已译的 56 页里；
#   - 那一页的变化经 fitz 裁图验证是**改进**（散文词 "as" 不再被吞进
#     公式图），译文仍然对得上 → **EXTRACTOR_VERSION 不动**
#     （用户的 75 页译文一页都不用重翻）。
# 可缓存里存的是**旧实现**抽出来的文本 → 必须失效，否则会进入这个状态：
#
#     同一份 PDF、同一份代码，缓存冷热不同 → 结果不同。
#
# 而这个不一致**没有任何告警**：前端的逐页 `text_hash` 是按**客户端**
# pdf.js 抽的文本算的（index.html: `renderPdfPage` 的 `text: items`），
# 服务端抽取算法变了它检测不到 → 旧译文配新抽取 = **错位**。
EXTRACT_IMPL_REV = 2

# 缓存目录：仓库的 logs/ 下（不进坚果云同步根，见模块文档）
_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _REPO_ROOT / "logs" / "pdf_extract_cache"

# ---- 孤儿清理（L-5）----
CACHE_MAX_FILES = 200                 # 目录里最多留多少个快照文件
CACHE_TTL_SECONDS = 14 * 24 * 3600    # 快照最长留 14 天
_PRUNE_INTERVAL = 300.0               # 写路径上两次清理之间的最小间隔（秒）
_last_prune_at = 0.0


def _sig(path: Optional[Path]) -> list:
    """文件的 (mtime_ns, size) 签名；文件不存在返回 [0, 0]。"""
    try:
        st = path.stat()
        return [int(st.st_mtime_ns), int(st.st_size)]
    except (OSError, AttributeError, TypeError):
        return [0, 0]


def _abs_path(path: Path) -> str:
    """绝对路径（尽量 resolve，失败退 abspath）。"""
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        try:
            return os.path.abspath(str(path))
        except (OSError, ValueError):
            return str(path)


def cache_key(pdf_path: Path) -> str:
    """算这篇 PDF 的缓存键。

    ⚠ **不要**再把符号词典的签名加进来 —— 抽取层不读词典，
    加进去只会让后台认字每保存一次就废掉全库缓存（见模块文档）。

    ⚠ 也不要加「词典路径」维度来防撞键 —— 那是在给一个**不存在**的问题
    打补丁：抽取层不读词典 → 词典维度对缓存内容没有任何影响 → 加了只会
    让「同一份 PDF 被两个调用点用不同参数算出不同键」→ 一份写一份读，
    缓存**永远命中不了**（比撞键更糟：加速完全失效，用户每次都等 2.7~9.7 秒）。
    → 正确做法是**所有调用点用同一个签名**（单一真相）。

    键里有**两个**版本闸门，各管各的：
      - `EXTRACTOR_VERSION`：抽取**有效性**变了 → 译文快照也要作废
      - `EXTRACT_IMPL_REV`  ：抽取**实现**变了 → 只需作废磁盘缓存
    （为什么要有第二个，见 `EXTRACT_IMPL_REV` 的注释）
    """
    raw = "|".join([
        _abs_path(Path(pdf_path)),
        str(_sig(Path(pdf_path))),
        str(EXTRACTOR_VERSION),
        str(EXTRACT_IMPL_REV),
        str(CACHE_FORMAT_VERSION),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 快照文件：<key>.<uuid>.json（唯一文件名，写侧永不替换已存在的文件）
# ---------------------------------------------------------------------------

def _mtime_ns(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _snapshot_paths(key: str) -> list:
    """这个 key 的全部快照文件，按 mtime **从新到旧**排。

    读侧靠它找「当前快照」：同一个 key 的多个快照内容等价，
    最新的那个就是刚写进去的。
    """
    try:
        files = [f for f in CACHE_DIR.glob(key + ".*.json") if f.is_file()]
    except OSError:
        return []
    files.sort(key=lambda f: (_mtime_ns(f), f.name), reverse=True)
    return files


def _valid_pages(data, key: str) -> Optional[list]:
    """校验快照结构。**不合格一律当未命中**（返回 None）。

    ⚠ 为什么要逐项校验（2026-10-27 审计 H-3）：
      旧版只验 `pages` 是不是非空 list → `{"pages": [123]}` 能读出来 →
      下游 `p["text"]` 抛 TypeError → 端点 500，而不是「重抽一次」。
    """
    if not isinstance(data, dict):
        return None
    if data.get("key") != key:
        return None
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    for p in pages:
        if not isinstance(p, dict) or not isinstance(p.get("text"), str):
            return None
    return pages


def read_cache(pdf_path: Path):
    """读缓存。命中返回 pages（list），未命中返回 None。

    ⚠ **永远不抛异常** —— 缓存坏了当没命中，重新抽一遍。
    """
    try:
        key = cache_key(pdf_path)
        for f in _snapshot_paths(key):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue          # 这份坏了 → 试下一份（更旧的）
            pages = _valid_pages(data, key)
            if pages is not None:
                return pages
        return None
    except Exception:
        # 缓存坏了不能挡住正事 —— 当作没命中，重新抽
        return None


# 缓存文件数的软上限。超过就按 mtime 删掉最旧的一批。
#
# 为什么要管：缓存键随 (mtime,size) + EXTRACTOR_VERSION 变，一篇论文改一次
# 就多一份文件，而且**没有任何地方会删旧的**（clear_cache 只有测试在调）。
# 27 篇 × 几十个版本 = 磁盘上堆几百个 JSON，每个可能几百 KB。
# 只在上限之上才动手，平时零开销。
CACHE_MAX_FILES = 200
_CACHE_PRUNE_BATCH = 40


def _prune_old_cache_files() -> None:
    """缓存文件超过软上限时，删掉最旧的一批（按 mtime）。"""
    try:
        files = list(CACHE_DIR.glob("*.json"))
        if len(files) <= CACHE_MAX_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for f in files[:_CACHE_PRUNE_BATCH]:
            try:
                f.unlink()
            except OSError:
                pass
    except Exception:
        # 清理失败不该影响写入
        pass


def write_cache(pdf_path: Path, pages: list, key: str) -> None:
    """写缓存。失败就失败（性能优化，不该影响功能）。"""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # ⚠ 唯一文件名（2026-10-27 审计 H-2）：
        #   固定文件名 + os.replace 在 Windows 上会被「目标文件正被读」顶掉
        #   （ERROR_ACCESS_DENIED，实测并发写 32 次 / 读写同时 782 次失败）
        #   → 缓存写不进去 + 读侧误判未命中 → 用户每次都等 2.7~9.7 秒重抽。
        #   → 每次写一个**全新**文件名 → 零竞争；读侧按 mtime 取最新。
        final = CACHE_DIR / ("%s.%s.json" % (key, uuid.uuid4().hex))
        fd, tmp = tempfile.mkstemp(dir=str(CACHE_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"key": key, "pages": pages}, fh, ensure_ascii=False)
            os.replace(tmp, final)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        _prune_old_cache_files()
    except Exception as e:
        print(f"[pdfcache] 写缓存失败（忽略，不影响功能）: {e}", file=sys.stderr)
        return
    _maybe_prune()


# ---------------------------------------------------------------------------
# 孤儿清理（L-5）
# ---------------------------------------------------------------------------

def _unlink(path: Path) -> bool:
    """删一个文件；删不掉（正被读）就跳过，不报错。"""
    try:
        path.unlink()
        return True
    except OSError:
        return False


def prune_cache(max_files: int = CACHE_MAX_FILES,
                ttl_seconds: int = CACHE_TTL_SECONDS) -> int:
    """按 TTL + 文件数上限清掉过期/多余的快照。返回清掉的文件数。

    孤儿从哪来：缓存键一变（PDF 改了 / 词典改了 / 算法升级了），
    旧 key 的文件**永远不会被读到**，但没人删。
    """
    n = 0
    try:
        if not CACHE_DIR.is_dir():
            return 0
        now = time.time()
        files = []
        for f in CACHE_DIR.glob("*.json"):
            try:
                st = f.stat()
            except OSError:
                continue
            files.append((st.st_mtime, f))
        files.sort(key=lambda t: t[0])          # 旧的在前
        # ① TTL 过期
        for mtime, f in files:
            if now - mtime > ttl_seconds:
                if _unlink(f):
                    n += 1
        # ② 数量上限（LRU：留最新的）
        alive = [(m, f) for m, f in files if f.exists()]
        if len(alive) > max_files:
            for _mtime, f in alive[: len(alive) - max_files]:
                if _unlink(f):
                    n += 1
    except Exception:
        pass
    return n


def _maybe_prune() -> None:
    """写路径上的节流清理（**不在读路径上**）。"""
    global _last_prune_at
    now = time.time()
    if now - _last_prune_at < _PRUNE_INTERVAL:
        return
    _last_prune_at = now
    try:
        prune_cache()
    except Exception:
        pass


def clear_cache() -> int:
    """清空缓存，返回清掉的文件数。

    ⚠ 快照文件现在是 `<key>.<uuid>.json`（唯一文件名），
      但仍然全是 `*.json` → 这个清理依然能全清掉。
      另外把崩溃残留的 `*.tmp` 也清掉。
    """
    n = 0
    try:
        if CACHE_DIR.is_dir():
            for pattern in ("*.json", "*.tmp"):
                for f in CACHE_DIR.glob(pattern):
                    if _unlink(f):
                        n += 1
    except Exception:
        pass
    return n
