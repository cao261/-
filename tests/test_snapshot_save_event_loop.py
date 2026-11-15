# -*- coding: utf-8 -*-
"""快照保存端点**不许**把事件循环冻住（2026-11-15 合并云端时审计抓到的 P0）。

## 坑是怎么来的

云端 origin/main 给「读旧值 → 合并 → 写回」这段加了 `threading.Lock`
（防两个写者读到同一个旧基线，后写的把先写的页整份盖掉）——
**方向完全正确**，但端点当时是 `async def`：

    FastAPI 只把 **`def` 端点**丢进线程池；`async def` 端点跑在**事件循环线程**上。
    `threading.Lock.acquire()` 是**阻塞**的
    → 在事件循环线程上等锁 = 整条 FastAPI 冻结。

而等锁的那一头是 `/api/sync`（线程池线程），它的临界区里要写
**坚果云共享目录** —— 耗时不可控。

## 实测（合并后的代码，改之前）

    CONTROL  async endpoint latency, no contention :     15.0 ms
    DURING   async endpoint latency, lock held     :   1547.0 ms
    AFTER    async endpoint latency, released      :     16.0 ms
    VERDICT: EVENT LOOP BLOCKED

期间一个**完全无关**的 `GET /api/pdf/snapshot/list` 从 15ms 变成 1547ms
→ SSE 翻译流、看板轮询全停。

## 这条护栏盯什么

钉住「锁被占住时，**别的**端点照样秒回」——
也就是「等锁不许发生在事件循环线程上」。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

# 锁被占住的时长 / 无关请求的时限
# ⚠ 阈值放宽到 1.5s（正常是 5~15ms）→ 机器再卡也不会假红；
#   而回归时无关请求要等满 3s → 稳稳地红。
LOCK_HOLD_SECONDS = 3.0
UNRELATED_MAX_SECONDS = 1.5


def _snap_payload(paper: str, page: int = 1, translation: str = "译文"):
    """一份合法的快照保存请求体。"""
    return {
        "paper_id": paper,
        "paper_title": "事件循环护栏测试论文",
        "relative_path": "01_Literature/evloop.pdf",
        "paper_mtime": 1.0,
        "pages": [{
            "page_num": page,
            "text_hash": f"{page:064d}",
            "translation": translation,
            "model": "test",
            "translated_at": "2026-11-15T09:00:00",
        }],
    }


def test_snapshot_save_does_not_freeze_the_event_loop(client, sandbox):
    """锁被占住时，**别的**端点不许被拖住。

    ⚠ 这条是**行为级**的：真的发两个并发请求，量一个无关请求的耗时。
      不是去读源码确认端点是不是 async（那样改法一换就假绿）。
    """
    from microbench import app

    paper = "evloop_freeze_paper"
    lock = app._snapshot_lock(paper)
    held = threading.Event()
    release = threading.Event()

    def holder():
        """模拟 /api/sync：持锁去做「云端 I/O」（这里就只是睡一会儿）。"""
        with lock:
            held.set()
            release.wait(timeout=LOCK_HOLD_SECONDS + 5)

    def save():
        return client.post("/api/pdf/snapshot/save", json=_snap_payload(paper))

    with ThreadPoolExecutor(max_workers=2) as ex:
        holder_future = ex.submit(holder)
        assert held.wait(timeout=10), "持锁线程没起来"

        # 保存请求会卡在锁上（它等的是同一把锁）
        save_future = ex.submit(save)
        time.sleep(0.2)   # 让它真的走到等锁那一步

        # 关键测量：一个**完全无关**的读请求要多久
        t1 = time.perf_counter()
        resp = client.get("/api/pdf/snapshot/list")
        unrelated = time.perf_counter() - t1

        assert resp.status_code == 200, resp.text
        # ⚠ 顺序要紧：**先量延迟**。
        #   回归时（端点又变回 async）事件循环被冻住 → 连这个无关请求都要
        #   等到持锁方超时才回得来 → 先报「延迟」，报错才直指病灶；
        #   下面的「竞争」判定只是证明这条护栏没在空跑。
        assert unrelated < UNRELATED_MAX_SECONDS, (
            f"锁被占住时，无关请求耗时 {unrelated * 1000:.0f}ms —— "
            "事件循环被冻住了（保存端点在事件循环线程上等 blocking 锁）。"
            "修复：把 api_pdf_snapshot_save 改成 **def**（同步端点），"
            "让 FastAPI 把它丢进线程池。")

        # 放锁之前，保存请求必须**还在等** —— 这才证明真的发生了竞争
        # （否则可能是「压根没跟持锁方抢同一把锁」，这条护栏就成了空跑）
        assert not save_future.done(), (
            "保存请求在锁被占住时就返回了 —— 它没跟持锁方抢同一把锁，"
            "这条护栏等于没测")
        release.set()
        save_resp = save_future.result(timeout=30)
        holder_future.result(timeout=30)

    # 保存请求最终必须成功（等锁之后照常写盘）
    assert save_resp.status_code == 200, save_resp.text
