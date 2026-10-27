# -*- coding: utf-8 -*-
"""paper_id 的哈希算法（**跨设备契约**）。

## 为什么单独一个模块

同一套 djb2/base36 之前有**两份**实现：

- `microbench/app.py` 的 `_djb2_base36_utf16`
- `microbench/merge_changes.py` 的 `djb2_base36`

两份都得对，因为 paper_id 决定「这是不是同一篇论文」——
两台设备算出的 id 不一致，批注 / 快照 / 阅读进度就**静默**对不上
（用户看到的是"我的批注不见了"）。

而且两份**并不完全一样**：

| | app.py | merge_changes.py |
|---|---|---|
| `encode("utf-16-le")` | `errors="surrogatepass"` | 无 errors 参数 |
| 遇到孤立代理项 | 不抛（能算完） | **UnicodeEncodeError** |

路径里有 emoji 就有可能出现孤立代理项 → merge_changes 那一侧会崩。
统一到这里之后，两边行为完全一致，且不会崩。

## ⚠ 契约红线

这个算法必须与前端 `microbench/static/index.html` 里 `_paperBase` +
`_djb2Base36` **逐字一致**。改这里 = 两台设备的 paper_id 全变
= 批注/快照/进度全部对不上。

- `tests/test_paper_id_contract.py` 锁死了这个契约。
- 哈希必须按 **UTF-16 码元**算，不是码点：
  JS 的 `s.charCodeAt(i)` 取 UTF-16 码元（emoji = 代理对，算 2 个），
  Python 的 `ord(ch)` 取码点（emoji 算 1 个）——
  路径里只要有 emoji，两端算出的 id 就不同。
"""
from __future__ import annotations

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def js_base36(n: int) -> str:
    """JS 的 `Number.prototype.toString(36)`。"""
    if n == 0:
        return "0"
    out = []
    while n:
        out.append(_B36[n % 36])
        n //= 36
    return "".join(reversed(out))


def djb2_base36(s: str) -> str:
    """JS: `((h << 5) + h + s.charCodeAt(i)) >>> 0` → `h.toString(36)`。

    **按 UTF-16 码元**迭代（不是码点）。

    `errors="surrogatepass"` 是有意加的：路径里出现孤立代理项时
    （emoji 被截断等），没有它会抛 UnicodeEncodeError，让整个
    paper_id 生成失败 —— 而 paper_id 是所有同步动作的钥匙，
    不能因为一个畸形路径就整体崩掉。
    """
    h = 5381
    raw = s.encode("utf-16-le", errors="surrogatepass")
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        h = ((h << 5) + h + unit) & 0xFFFFFFFF
    return js_base36(h)


def paper_id_for(relative_path: str) -> str:
    """复刻前端 index.html 的 paperId 生成：`base + "_" + djb2(完整相对路径)`。

    - 相对路径统一用 `/` 分隔（Windows 上的反斜杠先换掉）
    - base = 文件名去掉 `.pdf`，截前 70 字符，空则 "paper"
    """
    path = (relative_path or "").replace("\\", "/")
    base = path.split("/")[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    base = base[:70] or "paper"
    return f"{base}_{djb2_base36(path)}"


__all__ = ["js_base36", "djb2_base36", "paper_id_for"]
