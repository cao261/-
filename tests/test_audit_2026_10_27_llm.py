# -*- coding: utf-8 -*-
"""2026-10-27 审计的 6 处修复的回归护栏。

对应 `docs/AUDIT_2026-10-26.md` 的 M-9 / M-12 / L-7 / L-9，
以及本次一并修掉的 `paper_fetcher.py` 无 timeout（bandit B113）。

每条护栏都要求**真的能红**：把对应修复改回去，下面对应的用例必须失败。
（AGENTS.md §4.51：护栏对核心修复零覆盖 = 不允许。）

  ① 限流器 min_interval 并发下失效   → test_min_interval_holds_under_concurrency
  ② 池化 httpx 客户端 timeout 被冻结   → test_pooled_client_timeout_is_not_frozen
                                          test_stream_model_passes_timeout_explicitly
  ③ 异步非流式链用错限流档            → test_async_non_streaming_uses_cfg_limiter
                                          test_custom_model_gets_its_own_limiter_slot
  ④ 流式链异常构造处没脱敏            → test_stream_http_error_body_is_redacted
                                          test_stream_transport_error_is_redacted
                                          test_stream_fallback_log_is_redacted
  ⑤ 额度判定失败被当成"没耗尽"        → test_quota_probe_failure_is_unknown_not_rate_limit
                                          test_429_wording_contract_kept
  ⑥ paper_fetcher 出网无 timeout       → test_paper_fetcher_get_has_timeout
                                          test_paper_fetcher_head_has_timeout
                                          test_paper_fetcher_caller_timeout_is_kept
                                          test_paper_fetcher_proxy_fallback_keeps_timeout
                                          test_proxy_dead_ttl_still_auto_recovers
"""
import asyncio
import json
import threading
import time

import pytest
import requests

from microbench import llm, netutil, paper_fetcher


# ---------------------------------------------------------------------------
# 公共替身
# ---------------------------------------------------------------------------

class _FakeAsyncResponse:
    """最小 httpx 风格响应（非流式）。"""

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {
            "choices": [{"message": {"content": "ok"}}]
        }
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """最小 httpx AsyncClient 替身（只支持 post）。"""

    def __init__(self, status_code=200, payload=None):
        self._status_code = status_code
        self._payload = payload
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers,
                           "timeout": timeout})
        return _FakeAsyncResponse(self._status_code, self._payload)


class _FakeStream:
    """最小 httpx 风格响应（流式）。"""

    def __init__(self, status_code=200, body=b"", lines=()):
        self.status_code = status_code
        self._body = body
        self._lines = list(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return self._body

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


async def _no_wait(*a, **kw):
    """替掉限流等待（测试里不该真的睡）。"""
    return None


# ---------------------------------------------------------------------------
# ① 限流器：min_interval 在并发下必须真的成立
# ---------------------------------------------------------------------------

def test_min_interval_holds_under_concurrency():
    """5 个并发调用者：**每一次实际触发**的间隔都 ≥ min_interval。

    旧实现用 `_last_call_time`（sleep 之后才更新）判间隔 → N-1 个调用者
    全部 sleep 同一个 min_interval 后**齐射**（实测 gaps
    ['6.00','0.00','0.00','0.00']）→ 打到上游每分钟上限 → 被误判成
    「配额耗尽」→ 全局降格 15 分钟。

    修法：间隔判据改用**预留时间**（`_last_planned_time`），
    再加一道"实际触发也不许早于 min_interval"的补睡。
    """
    min_interval = 0.15
    limiter = llm.SlidingWindowRateLimiter(
        max_requests=100, window_seconds=60.0, min_interval=min_interval)

    fired = []
    lock = threading.Lock()

    def worker():
        limiter.acquire()
        with lock:
            fired.append(time.time())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(fired) == 5, f"只有 {len(fired)} 个调用真的跑完了"

    fired.sort()
    gaps = [b - a for a, b in zip(fired, fired[1:])]
    # 10ms 容差：只吃调度抖动，不吃"齐射"（齐射的间隔是 0.00）
    for gap in gaps:
        assert gap >= min_interval - 0.01, (
            f"并发下有两次触发贴在一起了：gaps={[round(g, 3) for g in gaps]}")

    # 也不能退化成"串行排队 5 倍慢"：预留式应该 ≈ (n-1)×min_interval 走完
    total = fired[-1] - fired[0]
    assert total >= (len(fired) - 1) * min_interval - 0.02, \
        f"间隔根本没生效：total={total:.3f}"
    assert total <= len(fired) * min_interval + 0.5, \
        f"退化成串行排队了：total={total:.3f}"


def test_limiter_stats_still_report_last_actual_call():
    """改判据不能把 stats() 弄坏（界面上的「距上次调用」）。"""
    limiter = llm.SlidingWindowRateLimiter(max_requests=10, window_seconds=60.0,
                                           min_interval=0.05)
    limiter.acquire()
    st = limiter.stats()
    assert st["recent_requests_1m"] == 1, st
    assert st["last_call_seconds_ago"] is not None, st
    assert st["last_call_seconds_ago"] < 1.0, st


# ---------------------------------------------------------------------------
# ② 池化 httpx 客户端：timeout 不能被首次调用冻结
# ---------------------------------------------------------------------------

def test_pooled_client_timeout_is_not_frozen():
    """同一个池化客户端，第二次要 timeout=300 就得是 300（不能还是 60）。

    注意：池化客户端是**同一个对象**，timeout 变了就地更新，所以要
    在**每次调用当场**取值，不能事后回头看同一个对象的属性。
    """
    async def go():
        first = llm._get_async_client(60)
        t1 = first.timeout.read
        second = llm._get_async_client(300)
        t2 = second.timeout.read
        return t1, t2

    t1, t2 = asyncio.run(go())
    assert t1 == 60, f"第一次拿到的 timeout 不对：{t1}"
    assert t2 == 300, (
        f"timeout 还是被首次调用冻结了：{t2}")


def test_stream_model_passes_timeout_explicitly(monkeypatch):
    """流式请求必须**显式**带本次 timeout（不能只靠客户端默认值）。"""
    recorded = {}

    class _Client:
        def stream(self, method, url, **kw):
            recorded.update(kw)
            recorded["_method"] = method
            recorded["_url"] = url
            return _FakeStream(200, lines=[
                'data: {"choices":[{"delta":{"content":"hi"}}]}',
                "data: [DONE]",
            ])

    monkeypatch.setattr(llm, "_get_async_client", lambda *a, **kw: _Client())
    monkeypatch.setattr(llm, "_acquire_limiter_async", _no_wait)

    cfg = {"model": "test-model", "base_url": "http://fake.invalid/v1",
           "api_key": "sk-test"}

    async def go():
        async for _ in llm._stream_model(
                cfg, [{"role": "user", "content": "hi"}], 100, 0.3, 42, None):
            pass

    asyncio.run(go())
    assert "timeout" in recorded, (
        f"流式请求没带 timeout：{sorted(recorded)}")
    tmo = recorded["timeout"]
    # read 给 3 倍：思考型模型首字节可能要 41-100s，read 跟 timeout
    # 一样会把流砍在半路（前端把半截答案当完整答案渲染）。
    assert tmo.connect == 42, tmo
    assert tmo.read == 42 * 3, tmo


# ---------------------------------------------------------------------------
# ③ 异步非流式链：限流档必须按 cfg 选
# ---------------------------------------------------------------------------

def test_async_non_streaming_uses_cfg_limiter(monkeypatch):
    """`_invoke_model_async` 必须把 cfg 传给限流器。

    旧实现算了一个 `limiter = limiter_for_config(cfg)` 却**从不使用**
    （死代码），真正调用时又只传 `is_agnes` → 自定义模型（.env 的
    LLM_CUSTOM_N_RPM）被按主模型档限流，它自己的 RPM 上限形同虚设。
    """
    seen = []

    async def fake_acquire(is_agnes, cfg=None):
        seen.append((is_agnes, cfg))

    monkeypatch.setattr(llm, "_acquire_limiter_async", fake_acquire)
    client = _FakeAsyncClient()
    monkeypatch.setattr(llm, "_get_async_client", lambda *a, **kw: client)

    cfg = {
        "id": "custom:测试模型", "label": "测试模型", "model": "my-model",
        "base_url": "http://fake.invalid/v1", "api_key": "sk-test",
        "rpm": 97, "min_interval": 1.0, "configured": True,
        "is_fallback": False, "is_custom": True,
    }

    ok, content, status = asyncio.run(llm._invoke_model_async(
        cfg, [{"role": "user", "content": "hi"}], 100, 0.1, 30, False,
        max_retries=0))

    assert ok is True and content == "ok", (ok, content, status)
    assert seen, "限流器压根没被调用"
    assert seen[0][1] is cfg, (
        "限流器没拿到 cfg → 自定义模型被按主模型档限流："
        f"{seen[0][1]!r}")


def test_custom_model_gets_its_own_limiter_slot(monkeypatch):
    """按 cfg 取限流器时，自定义模型走**自己那档**，不动主模型那档。"""
    cfg = {
        "id": "custom:档位测试", "model": "my-model",
        "base_url": "http://fake.invalid/v1", "api_key": "sk-test",
        "rpm": 97, "min_interval": 1.0, "configured": True,
        "is_fallback": False, "is_custom": True,
    }
    limiter = llm.limiter_for_config(cfg)
    assert limiter is not llm.primary_limiter, "自定义模型跟主模型共用了限流器"
    assert limiter is not llm.agnes_limiter, "自定义模型跟 Agnes 共用了限流器"

    before_custom = limiter.stats()["recent_requests_1m"]
    before_primary = llm.primary_limiter.stats()["recent_requests_1m"]

    asyncio.run(llm._acquire_limiter_async(False, cfg))

    assert limiter.stats()["recent_requests_1m"] == before_custom + 1, \
        "自定义模型的限流器没被走到"
    assert llm.primary_limiter.stats()["recent_requests_1m"] == before_primary, \
        "自定义模型的请求打到了主模型那档限流器上"


# ---------------------------------------------------------------------------
# ④ 流式链的错误构造处必须脱敏
# ---------------------------------------------------------------------------

SECRET = "sk-STREAMLEAK0123456789"


def test_stream_http_error_body_is_redacted(monkeypatch):
    """上游 429 的报文里带凭据 → 抛出的 LLMError 里不能有凭据明文。"""
    body = json.dumps({
        "error": {"message": f"invalid credential {SECRET}"},
        "authorization": f"Bearer {SECRET}",
        "base_url": "https://user:pass-SECRET2@gw.internal/v1",
    }).encode()

    class _Client:
        def stream(self, method, url, **kw):
            return _FakeStream(429, body=body)

    monkeypatch.setattr(llm, "_get_async_client", lambda *a, **kw: _Client())
    monkeypatch.setattr(llm, "_acquire_limiter_async", _no_wait)

    cfg = {"model": "m", "base_url": "http://fake.invalid/v1", "api_key": "sk-x"}

    async def go():
        async for _ in llm._stream_model(
                cfg, [{"role": "user", "content": "hi"}], 100, 0.3, 30, None):
            pass

    with pytest.raises(llm.LLMError) as ei:
        asyncio.run(go())

    msg = str(ei.value)
    assert SECRET not in msg, f"凭据明文进了错误消息：{msg}"
    assert "pass-SECRET2" not in msg, f"URL 里的口令进了错误消息：{msg}"
    assert "***" in msg, f"没有脱敏痕迹：{msg}"


def test_stream_transport_error_is_redacted(monkeypatch):
    """传输层异常的消息里带凭据 → 包成 LLMError 时也要脱敏。"""

    class _Client:
        def stream(self, method, url, **kw):
            raise llm.httpx.ConnectError(
                f"failed to connect https://user:{SECRET}@gw.internal/v1")

    monkeypatch.setattr(llm, "_get_async_client", lambda *a, **kw: _Client())
    monkeypatch.setattr(llm, "_acquire_limiter_async", _no_wait)

    cfg = {"model": "m", "base_url": "http://fake.invalid/v1", "api_key": "sk-x"}

    async def go():
        async for _ in llm._stream_model(
                cfg, [{"role": "user", "content": "hi"}], 100, 0.3, 30, None):
            pass

    with pytest.raises(llm.LLMError) as ei:
        asyncio.run(go())

    msg = str(ei.value)
    assert SECRET not in msg, f"凭据明文进了错误消息：{msg}"
    assert "***" in msg, f"没有脱敏痕迹：{msg}"


def test_stream_fallback_log_is_redacted(monkeypatch, capsys):
    """流式链退回默认模型时写的那行 stderr 日志也不能带凭据。"""

    async def fake_stream_model(cfg, *a, **kw):
        # 必须是**异步生成器**（_stream_model 是 async generator），
        # 否则 chat_stream_async 的 `async for` 会 TypeError。
        raise llm.LLMError(f"上游炸了 credential={SECRET}")
        yield  # pragma: no cover

    monkeypatch.setattr(llm, "_stream_model", fake_stream_model)

    custom_cfg = {
        "id": "custom:日志测试", "label": "日志测试", "model": "my-model",
        "base_url": "http://fake.invalid/v1", "api_key": "sk-test",
        "rpm": 97, "min_interval": 1.0, "configured": True,
        "is_fallback": False, "is_custom": True,
    }
    monkeypatch.setattr(llm, "resolve_model_config",
                        lambda name: custom_cfg if name else None)
    monkeypatch.setattr(llm.model_manager, "get_primary_config",
                        lambda: {"configured": False, "model": "m",
                                 "base_url": "http://x", "api_key": ""})
    monkeypatch.setattr(llm.model_manager, "get_fallback_config",
                        lambda: {"configured": False, "model": "f",
                                 "base_url": "http://x", "api_key": "",
                                 "is_fallback": True})

    async def go():
        async for _ in llm.chat_stream_async(
                [{"role": "user", "content": "hi"}], model_name="custom:日志测试"):
            pass

    with pytest.raises(llm.LLMError):
        asyncio.run(go())

    err = capsys.readouterr().err
    assert SECRET not in err, f"凭据明文进了 stderr 日志：{err}"
    assert "***" in err, f"日志里没有脱敏痕迹：{err}"


# ---------------------------------------------------------------------------
# ⑤ 额度判定失败 ≠ 确认没耗尽
# ---------------------------------------------------------------------------

def test_quota_probe_failure_is_unknown_not_rate_limit(monkeypatch):
    """额度判定**失败**时不能断言"没耗尽"（旧代码 except → is_quota=False）。"""

    def boom(*a, **kw):
        raise RuntimeError("quota probe exploded")

    monkeypatch.setattr(type(llm.model_manager),
                        "is_quota_exhausted_error", boom)

    out = llm._friendly_http_error(429, "HTTP 429: Too Many Requests")
    assert "请求太频繁" not in out, (
        f"判定失败被说成限流了：{out}")
    assert "现在重试也一样" not in out, (
        f"判定失败被说成额度耗尽了：{out}")
    assert "分不清" in out, f"没有说清是未知：{out}"


def test_429_wording_contract_kept():
    """已知的两种 429 文案不能被这次改动带偏（tests/test_llm_error_friendly.py 也锁着）。"""
    rate = llm._friendly_http_error(429, "HTTP 429: Too Many Requests")
    assert "请求太频繁" in rate and "几十秒" in rate, rate

    quota = llm._friendly_http_error(429, "HTTP 429: Token Plan limit exceeded")
    assert "额度" in quota and "几十秒" not in quota, quota


# ---------------------------------------------------------------------------
# ⑥ paper_fetcher：出网请求必须带 timeout
# ---------------------------------------------------------------------------

class _StubResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {}

    def raise_for_status(self):
        return None


def test_paper_fetcher_get_has_timeout(monkeypatch):
    """requests.get 必须带 timeout（bandit B113：不带就是无限等）。"""
    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw))
        return _StubResp()

    monkeypatch.setattr(paper_fetcher.requests, "get", fake_get)
    paper_fetcher._get("https://example.invalid/x")

    assert calls, "requests.get 没被调用"
    assert "timeout" in calls[0][1], f"没带 timeout：{calls[0][1]}"
    assert calls[0][1]["timeout"] == paper_fetcher.DEFAULT_TIMEOUT


def test_paper_fetcher_head_has_timeout(monkeypatch):
    """requests.head 也必须带 timeout。"""
    calls = []

    def fake_head(url, **kw):
        calls.append((url, kw))
        return _StubResp()

    monkeypatch.setattr(paper_fetcher.requests, "head", fake_head)
    paper_fetcher._head("https://example.invalid/x")

    assert calls, "requests.head 没被调用"
    assert "timeout" in calls[0][1], f"没带 timeout：{calls[0][1]}"


def test_paper_fetcher_caller_timeout_is_kept(monkeypatch):
    """调用方自己指定的 timeout 不能被默认值覆盖（也不能变成重复实参 TypeError）。"""
    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw))
        return _StubResp()

    monkeypatch.setattr(paper_fetcher.requests, "get", fake_get)
    paper_fetcher._get("https://example.invalid/x", timeout=15)

    assert calls[0][1]["timeout"] == 15, calls[0][1]


def test_paper_fetcher_proxy_fallback_keeps_timeout(monkeypatch):
    """代理死掉时的兜底分支：仍然用 requests.get + proxies 置空 + 带 timeout。

    ⚠️ 不能改成 requests.Session —— 测试靠 monkeypatch requests.get，
    换 Session 会让打桩失效（会真的发网请求）。
    """
    state = {"dead": False}
    monkeypatch.setattr(netutil, "proxy_bypassed", lambda: state["dead"])
    monkeypatch.setattr(netutil, "mark_proxy_dead",
                        lambda err: state.__setitem__("dead", True))

    calls = []

    def fake_get(url, **kw):
        calls.append((url, kw))
        if len(calls) == 1:
            raise requests.exceptions.ProxyError("proxy down")
        return _StubResp()

    monkeypatch.setattr(paper_fetcher.requests, "get", fake_get)
    paper_fetcher._get("https://example.invalid/x")

    assert len(calls) == 2, f"兜底分支没走：{len(calls)} 次调用"
    assert calls[1][1].get("proxies") == {"http": None, "https": None}, \
        f"兜底分支没有直连：{calls[1][1]}"
    assert calls[1][1].get("timeout") == paper_fetcher.DEFAULT_TIMEOUT, \
        f"兜底分支丢了 timeout：{calls[1][1]}"


def test_proxy_dead_ttl_still_auto_recovers(monkeypatch):
    """「代理已死」不能变成永久直连：bypassed_until 到期自动恢复（AGENTS.md §4.9）。"""
    monkeypatch.setitem(netutil._state, "bypassed_until", 0.0)
    netutil.mark_proxy_dead(RuntimeError("proxy down"))
    assert netutil.proxy_bypassed() is True

    monkeypatch.setitem(netutil._state, "bypassed_until", time.time() - 1)
    assert netutil.proxy_bypassed() is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
