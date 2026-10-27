# -*- coding: utf-8 -*-
"""限流器的并发正确性护栏（2026-10-27）。

**为什么要有这份文件**

`SlidingWindowRateLimiter.acquire()` 的 `min_interval` 是防止把上游打到
429 的最后一道闸。它原来按「上次**实际**调用时间」算间隔，而这个时间
只在 `time.sleep()` **结束之后**才写入 —— 于是两个线程同时进来时：

    线程 A: 算出 wait=2.0s → 预占 t+2.0 → 放锁去睡
    线程 B: 读到的仍是**旧**的 last_call_time → 又算出 wait=2.0s
            → 预占 ≈ 同一个 t+2.0 → 两个请求一起发车

单线程永远看不出来，所以这个缺陷长期潜伏；而 `app._prepare_tables` 正是用
`asyncio.gather` **并发**读表的 —— 一页里有 2 张表就会撞上。

修法是「预占下一个可用时刻」（`_next_allowed`）并在放锁前推进它。
下面这条测试用真线程 + 真实小间隔把它钉死：任何回退都会红。
"""
from __future__ import annotations

import threading
import time

from microbench.llm import SlidingWindowRateLimiter


def test_sequential_calls_respect_min_interval():
    """单线程连续调用：第二次必须等够 min_interval。"""
    lim = SlidingWindowRateLimiter(max_requests=100, min_interval=0.15)
    fired = []
    for _ in range(3):
        lim.acquire()
        fired.append(time.time())
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    assert all(g >= 0.13 for g in gaps), f"间隔不足: {gaps}"


def test_concurrent_calls_do_not_share_a_slot():
    """并发调用不能一起发车 —— 这是原实现的实际失效点。

    3 个线程同时 acquire，min_interval=0.2s → 实际发车时刻必须两两
    相隔 >= 0.15s（留 50ms 给调度抖动）。
    """
    lim = SlidingWindowRateLimiter(max_requests=100, min_interval=0.2)
    fired: list[float] = []
    guard = threading.Lock()
    start = threading.Barrier(3)

    def worker():
        start.wait()          # 尽量让三个线程真正同时进入 acquire
        lim.acquire()
        with guard:
            fired.append(time.time())

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(fired) == 3, "有线程没跑完"
    fired.sort()
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    assert all(g >= 0.15 for g in gaps), (
        f"并发请求挤在同一个槽位（间隔 {['%.3f' % g for g in gaps]}s）"
        " → min_interval 在并发下失效，会把上游打成 429"
    )


def test_window_cap_still_holds():
    """滑动窗口上限没被上面的改动破坏。

    窗口取 1 秒（而不是 60 秒）—— 这条要真等，跑成 10 秒会拖慢整套测试。
    """
    lim = SlidingWindowRateLimiter(max_requests=2, window_seconds=1.0, min_interval=0.0)
    t0 = time.time()
    for _ in range(3):
        lim.acquire()
    assert time.time() - t0 >= 0.9, "第 3 次调用没有等第一个槽位滑出窗口"


def test_stats_never_reports_negative_wait():
    """`next_slot_in_seconds` / `last_call_seconds_ago` 都不能是负数。"""
    lim = SlidingWindowRateLimiter(max_requests=10, min_interval=0.05)
    lim.acquire()
    lim.acquire()
    st = lim.stats()
    assert st["next_slot_in_seconds"] is None or st["next_slot_in_seconds"] >= 0.0
    assert st["last_call_seconds_ago"] is None or st["last_call_seconds_ago"] >= 0.0
