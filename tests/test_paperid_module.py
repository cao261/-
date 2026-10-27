# -*- coding: utf-8 -*-
"""paper_id 哈希：app.py / merge_changes.py 共用一份，且必须与前端一致。

**为什么要有这份文件**

`paper_id` 决定「这是不是同一篇论文」。两台设备算出的 id 不一致 →
批注 / 快照 / 阅读进度**静默**对不上（用户看到的是"我的批注不见了"）。

之前这份算法有两份实现（app.py 一份、merge_changes.py 一份），而且
**行为不一样**：merge_changes 那份 `encode("utf-16-le")` 没有
`errors="surrogatepass"` → 路径里出现孤立代理项时抛 UnicodeEncodeError。

这里钉死：
1. 两处 import 的是**同一个**实现（不是两份拷贝）；
2. 孤立代理项不会抛异常；
3. 与前端 index.html 的算法逐字一致（contract 测试已覆盖）。
"""
from __future__ import annotations

from microbench import app, merge_changes, paperid


def test_app_and_merge_changes_share_the_same_implementation():
    """两处必须用同一个函数对象，不是两份拷贝。"""
    assert app._djb2_base36_utf16 is paperid.djb2_base36
    assert merge_changes.djb2_base36 is paperid.djb2_base36


def test_lone_surrogate_does_not_raise():
    """孤立代理项（emoji 被截断等）不能把 paper_id 生成搞崩。

    paper_id 是所有同步动作的钥匙 —— 一个畸形路径不该让整个功能挂掉。
    """
    lone = "bad\ud800path.pdf"
    out = paperid.paper_id_for(lone)
    assert out and out.startswith("bad")
    assert "_" in out


def test_emoji_is_hashed_as_two_utf16_code_units():
    """emoji = 代理对 = 2 个 UTF-16 码元 → 哈希必须按 2 个算。

    这是 JS `charCodeAt` 的语义；按码点算（Python 的 ord）会得到不同的
    paper_id → 两台设备对不上。
    """
    with_emoji = "01_Literature/\U0001F600.pdf"
    # 按 UTF-16 码元手工算一遍
    h = 5381
    raw = with_emoji.encode("utf-16-le")
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        h = ((h << 5) + h + unit) & 0xFFFFFFFF
    expected = paperid.js_base36(h)
    assert paperid.djb2_base36(with_emoji) == expected


def test_paper_id_for_normalises_backslashes():
    """Windows 的反斜杠必须先换成 `/`（前端喂的是 / 分隔的相对路径）。"""
    a = paperid.paper_id_for("01_Literature/sub/x.pdf")
    b = paperid.paper_id_for("01_Literature\\sub\\x.pdf")
    assert a == b


def test_paper_id_for_truncates_base_to_70():
    long_name = "a" * 200 + ".pdf"
    out = paperid.paper_id_for(f"01_Literature/{long_name}")
    base = out.rsplit("_", 1)[0]
    assert len(base) == 70


def test_empty_path_still_produces_an_id():
    out = paperid.paper_id_for("")
    assert out.startswith("paper_")
