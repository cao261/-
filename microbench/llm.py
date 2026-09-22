"""
LLM client wrapper for MicroBench with Dual-Model Failover & Auto-Recovery.

Supports:
- Primary Model: High-performance academic model (e.g. MiniMax-M2.7),
  which may be subject to a 5-hour Token Plan quota limit.
- Fallback Model: Free unlimited model (e.g. Agnes 3.0 Flash via apihub.agnes-ai.com),
  which has unlimited tokens but is subject to a strict 20 RPM rate limit.

Key capabilities:
1. Automatic Degradation (降格): If Primary fails due to 429 / quota exhaustion,
   automatically switch active model to Fallback (Agnes 3.0 Flash) and execute the request.
2. Automatic Probing & Recovery (升格): Periodic opportunistic probe (or manual trigger).
   When Primary quota window resets and probe returns 200, automatically upgrade back.
3. 20 RPM Rate Limiting (速率保护): Dedicated thread-safe sliding window rate limiter
   for Agnes (18 RPM cap + 3.0s min interval) with exponential backoff on 429.
"""

import os
import sys
import json
import re
import time
import asyncio
from pathlib import Path
from threading import Lock
from typing import Optional, AsyncIterator

try:
    import requests
except ImportError:
    requests = None  # type: ignore

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

# Auto-load .env from the workbench root (override=False so existing env vars take precedence)
try:
    from dotenv import load_dotenv
    _WORKBENCH_ROOT = Path(__file__).resolve().parent.parent
    _ENV_FILE = _WORKBENCH_ROOT / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:
    pass


DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

DEFAULT_AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
DEFAULT_AGNES_MODEL = "agnes-3.0-flash"

DEFAULT_MAX_TOKENS = 800
DEFAULT_TEMPERATURE = 0.3
DEFAULT_TIMEOUT = 60


class LLMError(Exception):
    """Raised when LLM call fails for any reason. Wraps upstream errors cleanly."""


# ---------------------------------------------------------------------------
# Agnes 20 RPM Rate Limiter
# ---------------------------------------------------------------------------

class AgnesRateLimiter:
    """
    Thread-safe sliding-window rate limiter designed for Agnes 3.0 Flash.

    Agnes Free tier has a 20 RPM limit. We use:
      - max_requests: 18 per 60s (safe buffer below 20 RPM)
      - min_interval: 3.0s between consecutive requests
    """

    def __init__(self, max_requests: int = 18, window_seconds: float = 60.0, min_interval: float = 3.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.min_interval = min_interval
        self._lock = Lock()
        self._timestamps: list[float] = []
        self._last_call_time: float = 0.0

    def acquire(self) -> float:
        """
        Block until safe to make a request under the RPM cap and min interval.
        Returns the number of seconds waited (if any).
        """
        with self._lock:
            now = time.time()
            # Clean expired timestamps outside the rolling window
            cutoff = now - self.window_seconds
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            wait_time = 0.0

            # Rule 1: Min interval spacing
            since_last = now - self._last_call_time
            if since_last < self.min_interval:
                wait_time = max(wait_time, self.min_interval - since_last)

            # Rule 2: Sliding window request cap
            if len(self._timestamps) >= self.max_requests:
                oldest_in_window = self._timestamps[0]
                window_wait = (oldest_in_window + self.window_seconds) - now
                if window_wait > wait_time:
                    wait_time = window_wait

            if wait_time > 0:
                # Sleep release then re-acquire time
                time.sleep(wait_time)
                now = time.time()
                # Re-clean after sleep
                cutoff = now - self.window_seconds
                self._timestamps = [t for t in self._timestamps if t > cutoff]

            self._timestamps.append(now)
            self._last_call_time = now
            return wait_time

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            active = [t for t in self._timestamps if t > cutoff]
            return {
                "rpm_limit": 20,
                "safe_rpm_cap": self.max_requests,
                "recent_requests_1m": len(active),
                "last_call_seconds_ago": round(now - self._last_call_time, 1) if self._last_call_time else None,
            }


# Global rate limiter instance for Agnes
agnes_limiter = AgnesRateLimiter()


# ---------------------------------------------------------------------------
# Dual-Model Manager (Primary + Fallback + Auto-Recovery)
# ---------------------------------------------------------------------------

class DualModelManager:
    """
    Coordinates primary (MiniMax) and fallback (Agnes) models.
    Handles automatic degradation on 429 quota exhaustion and automatic
    probing & recovery when quota resets.
    """

    def __init__(self, probe_interval_seconds: float = 900.0):
        self.probe_interval_seconds = probe_interval_seconds
        self._lock = Lock()
        self.is_degraded: bool = False
        self.degraded_reason: str = ""
        self.degraded_at: float = 0.0
        self.last_primary_probe: float = 0.0

    def get_primary_config(self) -> dict:
        api_key = os.getenv("LLM_API_KEY") or ""
        base_url = os.getenv("LLM_BASE_URL", "").rstrip("/")
        model = os.getenv("LLM_MODEL_NAME", "")

        # Sensible defaults for MiniMax if model name indicates MiniMax
        if not base_url:
            if "minimax" in model.lower():
                base_url = "https://api.minimax.chat/v1"
            else:
                base_url = DEFAULT_BASE_URL
        if not model:
            model = "MiniMax-M2.7" if "minimax" in base_url.lower() else DEFAULT_MODEL

        return {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "configured": bool(api_key),
            "is_fallback": False,
        }

    def get_fallback_config(self) -> dict:
        api_key = os.getenv("AGNES_API_KEY") or os.getenv("LLM_FALLBACK_API_KEY") or ""
        base_url = (os.getenv("AGNES_BASE_URL") or DEFAULT_AGNES_BASE_URL).rstrip("/")
        model = os.getenv("AGNES_MODEL_NAME") or DEFAULT_AGNES_MODEL

        return {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "configured": bool(api_key),
            "is_fallback": True,
        }

    def mark_degraded(self, reason: str):
        with self._lock:
            self.is_degraded = True
            self.degraded_reason = reason
            self.degraded_at = time.time()
            self.last_primary_probe = time.time()
            print(
                f"[MicroBench] ⚠️ 主模型触发降格: {reason}. "
                f"已自动切换至备用模型 (Agnes 3.0 Flash, 20 RPM 保护).",
                file=sys.stderr,
            )

    def mark_recovered(self):
        with self._lock:
            self.is_degraded = False
            self.degraded_reason = ""
            self.last_primary_probe = time.time()
            print(
                "[MicroBench] 🚀 主模型额度已恢复正常，系统已自动升格回主模型 (MiniMax)!",
                file=sys.stderr,
            )

    def should_opportunistic_probe(self) -> bool:
        """Return True if degraded and probe cooldown interval has elapsed."""
        with self._lock:
            if not self.is_degraded:
                return False
            now = time.time()
            return (now - self.last_primary_probe) >= self.probe_interval_seconds

    def is_quota_exhausted_error(self, status_code: int, err_text: str) -> bool:
        """Detect if an error signifies MiniMax 5-hour quota exhaustion or Token Plan limit."""
        if status_code != 429:
            return False
        text_lower = err_text.lower()
        keywords = [
            "token plan", "2056", "quota", "额度", "套餐", "limit",
            "exceeded", "充值", "上限", "insufficient"
        ]
        return any(k in text_lower or k in err_text for k in keywords)

    def probe_primary(self) -> dict:
        """
        Explicitly probe primary model with a minimal request to test if quota has reset.
        If successful, auto-upgrades back to primary.
        """
        primary = self.get_primary_config()
        if not primary["configured"]:
            return {"success": False, "message": "主模型未配置 API Key"}

        url = f"{primary['base_url']}/chat/completions"
        payload = {
            "model": primary["model"],
            "messages": [{"role": "user", "content": "1"}],
            "max_tokens": 1,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {primary['api_key']}",
            "Content-Type": "application/json",
            "User-Agent": "MicroBench-Academic-Assistant/1.1",
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                self.mark_recovered()
                return {
                    "success": True,
                    "model": primary["model"],
                    "message": f"主模型 ({primary['model']}) 探测成功，已自动升格为首选模型！",
                }
            else:
                err = resp.text[:200]
                with self._lock:
                    self.last_primary_probe = time.time()
                return {
                    "success": False,
                    "status_code": resp.status_code,
                    "message": f"主模型仍不可用 (HTTP {resp.status_code}): {err}",
                }
        except Exception as e:
            with self._lock:
                self.last_primary_probe = time.time()
            return {"success": False, "message": f"探针网络请求异常: {e}"}

    def get_status(self) -> dict:
        primary = self.get_primary_config()
        fallback = self.get_fallback_config()

        # Determine effective active model
        if self.is_degraded and fallback["configured"]:
            active_model = fallback["model"]
            active_provider = "Agnes (降格生效中)"
        elif primary["configured"]:
            active_model = primary["model"]
            active_provider = "Primary (MiniMax/自定义)"
        elif fallback["configured"]:
            active_model = fallback["model"]
            active_provider = "Agnes"
        else:
            active_model = None
            active_provider = None

        now = time.time()
        next_probe = max(0, int(self.probe_interval_seconds - (now - self.last_primary_probe))) if self.is_degraded else None

        return {
            "configured": bool(primary["configured"] or fallback["configured"]),
            "active_model": active_model,
            "active_provider": active_provider,
            "is_degraded": self.is_degraded,
            "degraded_reason": self.degraded_reason,
            "degraded_at": self.degraded_at,
            "primary": {
                "model": primary["model"],
                "base_url": primary["base_url"],
                "configured": primary["configured"],
            },
            "fallback": {
                "model": fallback["model"],
                "base_url": fallback["base_url"],
                "configured": fallback["configured"],
            },
            "next_probe_in_seconds": next_probe,
            "rate_limit": agnes_limiter.stats(),
        }


# Global model manager instance
model_manager = DualModelManager()


def _config() -> dict:
    """
    Read current effective LLM config.
    Returns dict with keys: api_key, base_url, model, configured (bool), is_fallback (bool)
    """
    st = model_manager.get_status()
    if model_manager.is_degraded:
        fb = model_manager.get_fallback_config()
        if fb["configured"]:
            return fb
    pm = model_manager.get_primary_config()
    if pm["configured"]:
        return pm
    fb = model_manager.get_fallback_config()
    if fb["configured"]:
        return fb
    return {
        "api_key": "",
        "base_url": DEFAULT_BASE_URL,
        "model": DEFAULT_MODEL,
        "configured": False,
        "is_fallback": False,
    }


def is_configured() -> bool:
    """Return True if at least Primary or Fallback API key is set."""
    st = model_manager.get_status()
    return st["configured"]


def get_llm_status() -> dict:
    """Public function returning comprehensive dual-model status and rate limits."""
    return model_manager.get_status()


def probe_primary_model() -> dict:
    """Public function to manually probe MiniMax and trigger upgrade if recovered."""
    return model_manager.probe_primary()


# ---------------------------------------------------------------------------
# Core Chat Implementation with Failover & Agnes Rate Limiting
# ---------------------------------------------------------------------------

def _invoke_model(
    cfg: dict,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: int,
    json_mode: bool,
    max_retries: int = 2,
) -> tuple[bool, str, Optional[int]]:
    """
    Execute request against a specific model configuration.
    Returns: (success: bool, content_or_error: str, status_code: Optional[int])
    """
    is_agnes = cfg.get("is_fallback") or "agnes" in cfg.get("base_url", "").lower() or "agnes" in cfg.get("model", "").lower()

    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "MicroBench-Academic-Assistant/1.1",
    }

    RETRYABLE_EXC = (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.SSLError,
        requests.exceptions.ChunkedEncodingError,
    )

    last_err = ""
    last_status = None

    for attempt in range(max_retries + 1):
        if is_agnes:
            # Respect Agnes 20 RPM rate limit & min spacing
            agnes_limiter.acquire()

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            last_status = resp.status_code
        except RETRYABLE_EXC as e:
            last_err = str(e)
            if attempt < max_retries:
                wait = 2 ** attempt
                time.sleep(wait)
                continue
            return False, f"网络连接失败 (重试 {max_retries} 次): {e}", last_status
        except requests.RequestException as e:
            return False, f"请求异常: {e}", last_status

        if resp.status_code != 200:
            try:
                err_body = resp.json().get("error", {})
                err = (
                    err_body.get("message", "")
                    or err_body.get("detail", "")
                    or getattr(resp, "text", "")[:200]
                )
            except (ValueError, KeyError):
                err = getattr(resp, "text", "")[:200]

            last_err = err

            # Agnes 429 rate limit backoff (exponential wait then retry)
            if resp.status_code == 429 and is_agnes and attempt < max_retries:
                wait = 3.5 * (attempt + 1)
                time.sleep(wait)
                continue

            # Transient 502/503/504 retry
            if resp.status_code in (502, 503, 504) and attempt < max_retries:
                wait = 2 ** attempt
                time.sleep(wait)
                continue

            return False, f"HTTP {resp.status_code}: {err}", resp.status_code

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            content = _strip_thinking_blocks(content)
            return True, content, 200
        except (KeyError, IndexError, ValueError) as e:
            return False, f"LLM 响应格式异常: {e}; raw={resp.text[:200]}", 200

    return False, last_err, last_status


def chat(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    json_mode: bool = False,
    max_retries: int = 2,
) -> str:
    """
    Single-turn chat completion with automatic failover and degradation management.

    1. If degraded and probe cooldown passed, attempts Primary. If successful, upgrades back.
    2. Otherwise, if not degraded, calls Primary. If Primary returns quota 429,
       immediately triggers degradation and fails over to Agnes 3.0 Flash seamlessly.
    3. If degraded, calls Agnes 3.0 Flash with 20 RPM sliding window rate limiting.
    """
    if requests is None:
        raise LLMError("requests 包未安装, 请先 pip install requests")

    primary = model_manager.get_primary_config()
    fallback = model_manager.get_fallback_config()

    if not primary["configured"] and not fallback["configured"]:
        raise LLMError(
            "LLM 未配置: 请设置环境变量 LLM_API_KEY (主模型) 或 AGNES_API_KEY (备用模型). "
            "可在工作台根目录编辑 .env 文件."
        )

    # Opportunistic auto-recovery probe if degraded and probe cooldown reached
    if model_manager.is_degraded and primary["configured"] and model_manager.should_opportunistic_probe():
        probe_res = model_manager.probe_primary()
        if probe_res.get("success"):
            # Successfully upgraded!
            pass

    # If NOT degraded and Primary is configured, try Primary first
    if not model_manager.is_degraded and primary["configured"]:
        success, res, status_code = _invoke_model(
            primary, messages, max_tokens, temperature, timeout, json_mode, max_retries=max_retries
        )
        if success:
            return res

        # Check if Primary failed due to 429 quota exhaustion (e.g. MiniMax 5-hour limit)
        if fallback["configured"] and model_manager.is_quota_exhausted_error(status_code or 0, res):
            model_manager.mark_degraded(res)
            # Seamlessly fail over to fallback!
            fb_success, fb_res, _ = _invoke_model(
                fallback, messages, max_tokens, temperature, timeout, json_mode, max_retries=max_retries
            )
            if fb_success:
                return fb_res
            raise LLMError(f"降格模型 (Agnes) 调用失败: {fb_res} (原主模型报错: {res})")

        # If primary failed with non-quota error or fallback is not configured, raise
        raise LLMError(f"主模型 ({primary['model']}) 调用失败: {res}")

    # Degraded or Primary not configured: use Fallback (Agnes)
    if fallback["configured"]:
        success, res, _ = _invoke_model(
            fallback, messages, max_tokens, temperature, timeout, json_mode, max_retries=max_retries
        )
        if success:
            return res
        raise LLMError(f"备用模型 ({fallback['model']}) 调用失败: {res}")

    # Fallback not configured, primary failed
    raise LLMError("无可用的大模型配置")


# ---------------------------------------------------------------------------
# Async variants (Stage 1 of async llm refactor)
#
# Why a separate async path instead of converting chat() in place?
#   - Sync chat() powers the high-throughput translation/summarization
#     endpoints where cancellation is irrelevant (server-side batch work).
#   - Async chat_async() powers the 3 reader-modal cancel-button endpoints
#     where the user may abandon mid-flight; honoring cancellation saves
#     API quota and reduces perceived latency on the cancel button.
#
# Behavior parity with sync chat():
#   - Same failover (primary → Agnes on quota exhaustion)
#   - Same retry semantics (transient 502/503/504, 429 backoff)
#   - Same response shape (just strip thinking blocks at the end)
#
# New behaviors:
#   - Uses httpx.AsyncClient (truly interruptible mid-HTTP-request, vs
#     requests.post which blocks the worker thread)
#   - Honors cancel_event (asyncio.Event): when set, skip retry storms
#     and raise asyncio.CancelledError to short-circuit the endpoint
# ---------------------------------------------------------------------------


async def _invoke_model_async(
    cfg: dict,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: int,
    json_mode: bool,
    max_retries: int = 2,
    cancel_event: Optional[asyncio.Event] = None,
) -> tuple[bool, str, Optional[int]]:
    """Async equivalent of _invoke_model().

    Returns: (success: bool, content_or_error: str, status_code: Optional[int])

    Raises asyncio.CancelledError when cancel_event is set (e.g. client
    disconnected), skipping the rest of the retry loop so the endpoint can
    bail out cleanly without burning more API quota.
    """
    if httpx is None:
        return False, "httpx 包未安装, 请先 pip install httpx", None

    is_agnes = cfg.get("is_fallback") or "agnes" in cfg.get("base_url", "").lower() or "agnes" in cfg.get("model", "").lower()

    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "User-Agent": "MicroBench-Academic-Assistant/1.1",
    }

    # httpx equivalents of the requests exceptions we care about.
    # SSLError surfaces as httpx.ConnectError (with SSL context wrapped),
    # ChunkedEncodingError surfaces as httpx.RemoteProtocolError.
    RETRYABLE_EXC = (
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.WriteError,
    )

    last_err = ""
    last_status = None

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    for attempt in range(max_retries + 1):
        # Check cancellation BEFORE attempting — saves quota on early cancel.
        if _cancelled():
            raise asyncio.CancelledError("client disconnected before retry")

        if is_agnes:
            # AgnesRateLimiter uses threading.Lock for cross-thread safety;
            # acquire() is fast and non-blocking (it may sleep briefly to
            # enforce min spacing — that sleep would normally be a thread
            # sleep, but we run it from the async loop, so wrap with
            # asyncio.to_thread to avoid blocking the event loop).
            await asyncio.to_thread(agnes_limiter.acquire)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
            last_status = resp.status_code
        except RETRYABLE_EXC as e:
            last_err = str(e)
            if _cancelled():
                raise asyncio.CancelledError("client disconnected during retry") from e
            if attempt < max_retries:
                wait = 2 ** attempt
                await asyncio.sleep(wait)
                continue
            return False, f"网络连接失败 (重试 {max_retries} 次): {e}", last_status
        except asyncio.CancelledError:
            # Propagate cleanly without retrying
            raise
        except Exception as e:
            # Non-retryable exception
            return False, f"请求异常: {e}", last_status

        if resp.status_code != 200:
            try:
                err_body = resp.json().get("error", {})
                err = (
                    err_body.get("message", "")
                    or err_body.get("detail", "")
                    or getattr(resp, "text", "")[:200]
                )
            except (ValueError, KeyError):
                err = getattr(resp, "text", "")[:200]

            last_err = err

            # Agnes 429 rate limit backoff
            if resp.status_code == 429 and is_agnes and attempt < max_retries:
                if _cancelled():
                    raise asyncio.CancelledError("client cancelled during 429 backoff")
                wait = 3.5 * (attempt + 1)
                await asyncio.sleep(wait)
                continue

            # Transient 502/503/504 retry
            if resp.status_code in (502, 503, 504) and attempt < max_retries:
                if _cancelled():
                    raise asyncio.CancelledError("client cancelled during transient retry")
                wait = 2 ** attempt
                await asyncio.sleep(wait)
                continue

            return False, f"HTTP {resp.status_code}: {err}", resp.status_code

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            content = _strip_thinking_blocks(content)
            return True, content, 200
        except (KeyError, IndexError, ValueError) as e:
            return False, f"LLM 响应格式异常: {e}; raw={resp.text[:200]}", 200

    return False, last_err, last_status


async def chat_async(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    json_mode: bool = False,
    max_retries: int = 2,
    cancel_event: Optional[asyncio.Event] = None,
) -> str:
    """Async version of chat() with cancellation support.

    Same failover + degradation logic as chat(), but uses httpx.AsyncClient
    so in-flight HTTP requests can be interrupted by asyncio.CancelledError
    (vs sync requests.post which would block until the server replies).

    The cancel_event is checked before each retry; once set, raises
    asyncio.CancelledError immediately so the endpoint can bail without
    burning more API quota on a doomed request.
    """
    if httpx is None:
        raise LLMError("httpx 包未安装, 请先 pip install httpx")

    primary = model_manager.get_primary_config()
    fallback = model_manager.get_fallback_config()

    if not primary["configured"] and not fallback["configured"]:
        raise LLMError(
            "LLM 未配置: 请设置环境变量 LLM_API_KEY (主模型) 或 AGNES_API_KEY (备用模型). "
            "可在工作台根目录编辑 .env 文件."
        )

    # Opportunistic auto-recovery probe (sync, fast — keep as-is)
    if model_manager.is_degraded and primary["configured"] and model_manager.should_opportunistic_probe():
        model_manager.probe_primary()

    if not model_manager.is_degraded and primary["configured"]:
        success, res, status_code = await _invoke_model_async(
            primary, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, cancel_event=cancel_event,
        )
        if success:
            return res

        if fallback["configured"] and model_manager.is_quota_exhausted_error(status_code or 0, res):
            model_manager.mark_degraded(res)
            fb_success, fb_res, _ = await _invoke_model_async(
                fallback, messages, max_tokens, temperature, timeout, json_mode,
                max_retries=max_retries, cancel_event=cancel_event,
            )
            if fb_success:
                return fb_res
            raise LLMError(f"降格模型 (Agnes) 调用失败: {fb_res} (原主模型报错: {res})")

        raise LLMError(f"主模型 ({primary['model']}) 调用失败: {res}")

    if fallback["configured"]:
        success, res, _ = await _invoke_model_async(
            fallback, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, cancel_event=cancel_event,
        )
        if success:
            return res
        raise LLMError(f"备用模型 ({fallback['model']}) 调用失败: {res}")

    raise LLMError("无可用的大模型配置")


# ---------------------------------------------------------------------------
# Streaming variants (Stage 2 of async llm refactor)
#
# Why a separate stream path?
#   - chat_async() returns the full response when complete — the user
#     waits 30-120s with no feedback. chat_stream_async() yields chunks
#     so the frontend can render text progressively ("typing" effect).
#   - Streaming also lets us cancel mid-token: close the httpx stream
#     immediately when cancel_event is set, instead of waiting for the
#     full response to come back.
#
# What stays the same as chat_async():
#   - Same failover (primary → Agnes on quota exhaustion)
#   - Same cancel_event plumbing (skip mid-stream retries)
#
# What's different:
#   - Uses httpx.AsyncClient.stream() instead of post()
#   - No mid-stream retry (partial content can't be replayed; fail fast)
#   - Returns AsyncIterator[str] (caller wraps in SSE or ReadableStream)
#
# Note on thinking blocks:
#   - Reasoning models (MiniMax-M2.7) emit <think>...</think> blocks
#     BEFORE the actual content. For Stage 2 we yield raw chunks without
#     stripping — frontend applies display-side filtering or accepts raw
#     reasoning. Stage 3 can add server-side stripping if needed.
# ---------------------------------------------------------------------------


async def _stream_model(
    cfg: dict,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: int,
    cancel_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[str]:
    """Yield content chunks from a single model via httpx streaming.

    Yields the `content` field from each SSE `data: {...}` line.
    Raises LLMError on non-200 status, asyncio.CancelledError on cancel.
    Does NOT retry — callers wrap this in their own retry logic if needed.
    """
    if httpx is None:
        raise LLMError("httpx 包未安装, 请先 pip install httpx")

    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,  # request SSE
    }

    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "MicroBench-Academic-Assistant/1.2",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise LLMError(
                        f"HTTP {resp.status_code}: {body[:200].decode('utf-8', errors='replace')}"
                    )

                async for line in resp.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        raise asyncio.CancelledError("client disconnected mid-stream")
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        # Skip malformed lines (heartbeats, comments)
                        continue
        except asyncio.CancelledError:
            raise
        except httpx.ConnectError as e:
            raise LLMError(f"网络连接失败: {e}")
        except httpx.TimeoutException as e:
            raise LLMError(f"请求超时: {e}")


async def chat_stream_async(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    cancel_event: Optional[asyncio.Event] = None,
) -> AsyncIterator[str]:
    """Streaming version of chat() with cancellation support.

    Async generator that yields content chunks as they arrive from the
    upstream LLM. Same failover logic as chat_async():
      1. If degraded, use Fallback (Agnes) directly
      2. Otherwise try Primary first
      3. If Primary hits quota exhaustion (429), switch to Agnes

    Cancellation:
      - cancel_event checked before each chunk (mid-stream close)
      - Pre-stream check saves an HTTP call when cancel fires very early

    Usage (in FastAPI endpoint):
        async def event_gen():
            try:
                async for chunk in llm.chat_stream_async(messages, ..., cancel_event=cancel_event):
                    yield f"data: {json.dumps({'text': chunk})}\\n\\n"
                yield "data: [DONE]\\n\\n"
            except asyncio.CancelledError:
                yield "data: {\\"cancelled\\": true}\\n\\n"
            except llm.LLMError as e:
                yield f"data: {json.dumps({'error': str(e)})}\\n\\n"
    """
    primary = model_manager.get_primary_config()
    fallback = model_manager.get_fallback_config()

    if not primary["configured"] and not fallback["configured"]:
        raise LLMError(
            "LLM 未配置: 请设置环境变量 LLM_API_KEY 或 AGNES_API_KEY."
        )

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError("client disconnected before stream start")

    if not model_manager.is_degraded and primary["configured"]:
        try:
            async for chunk in _stream_model(
                primary, messages, max_tokens, temperature, timeout, cancel_event,
            ):
                yield chunk
            return
        except asyncio.CancelledError:
            raise
        except LLMError as e:
            err_msg = str(e)
            if fallback["configured"] and (
                "429" in err_msg or "quota" in err_msg.lower()
            ):
                model_manager.mark_degraded(err_msg)
            else:
                raise

    if fallback["configured"]:
        if fallback.get("is_fallback"):
            await asyncio.to_thread(agnes_limiter.acquire)
        async for chunk in _stream_model(
            fallback, messages, max_tokens, temperature, timeout, cancel_event,
        ):
            yield chunk
        return

    raise LLMError("无可用的大模型配置")


_THINKING_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _strip_thinking_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from reasoning-model output."""
    if not text:
        return text
    cleaned = _THINKING_RE.sub("", text)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Prompt templates for paper summarization & Q&A
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = """你是微电子与半导体科研助手, 帮助硕博生快速理解英文学术论文。
请根据提供的论文元数据(标题/作者/摘要/会议), 输出:
1. 一句话中文 TL;DR (不超过 60 字)
2. 3-5 个关键技术要点 (bullet 列表)
3. 对该方向硕博生的 1 条研究启示

要求:
- 直接输出 JSON, 禁止长篇思考
- 使用学术但平实的语言, 避免空洞套话
- 必须严格按以下 schema:
{"tldr": "...", "key_points": ["...", "..."], "takeaway": "..."}
"""

QA_SYSTEM_PROMPT = """你是微电子与半导体科研助手, 基于用户的本地文献库(vault)回答问题。
规则:
1. 仅基于提供的 context 回答, 不要编造 context 中没有的事实
2. 引用时必须标注来源(用 [[filename]] 形式)
3. 如果 context 不包含答案, 明确说"vault 中未找到相关信息", 不要猜测
4. 用中文回答, 学术但简洁
"""


def summarize_paper(meta: dict) -> dict:
    """
    Generate TL;DR + key points + takeaway from paper metadata.
    Returns dict with tldr / key_points / takeaway.
    Falls back to a heuristic skeleton if LLM call fails (so the UI never breaks).
    """
    user_msg = json.dumps({
        "title": meta.get("title", ""),
        "authors": meta.get("authors", ""),
        "year": meta.get("year", ""),
        "venue": meta.get("venue", ""),
        "abstract": (meta.get("abstract") or "")[:3000],  # generous for Agnes / MiniMax
    }, ensure_ascii=False)

    try:
        raw = chat(
            messages=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=600,
            temperature=0.3,
            json_mode=True,
        )
        result = json.loads(raw)
        return {
            "tldr": str(result.get("tldr", "")).strip()[:200],
            "key_points": [str(p).strip() for p in result.get("key_points", [])][:5],
            "takeaway": str(result.get("takeaway", "")).strip()[:300],
        }
    except (LLMError, json.JSONDecodeError, KeyError) as e:
        title = meta.get("title", "(未知标题)")
        return {
            "tldr": f"({title[:30]}... — LLM 未配置或调用失败: {str(e)[:80]})",
            "key_points": [
                "⚠️ 请检查 .env 中的 LLM_API_KEY 或 AGNES_API_KEY 配置",
                f"原文摘要 ({len(meta.get('abstract') or '')} 字) 可手动阅读",
            ],
            "takeaway": "提示: 降级到 Agnes 3.0 Flash 后，在 20 RPM 限制下可稳定完成自动摘要。",
            "error": str(e),
        }


def answer_with_context(question: str, context_chunks: list[dict]) -> str:
    """
    Answer a user question using vault chunks as context.
    context_chunks: list of {"path": ..., "title": ..., "snippet": ...}
    """
    if not context_chunks:
        return "vault 中未找到与该问题相关的内容。请确认问题关键词是否匹配已有文献, 或先录入更多文献。"

    context_lines = []
    for i, chunk in enumerate(context_chunks, 1):
        context_lines.append(
            f"[{i}] {chunk.get('title', chunk.get('path', 'unknown'))}\n"
            f"路径: {chunk.get('path', '?')}\n"
            f"片段: {chunk.get('snippet', '')}\n"
        )
    context_block = "\n---\n".join(context_lines)

    user_msg = (
        f"用户问题: {question}\n\n"
        f"vault 检索结果 (按相关度排序):\n{context_block}\n\n"
        f"请基于以上 context 给出回答, 并在引用处用 [[路径]] 标注来源。"
    )

    return chat(
        messages=[
            {"role": "system", "content": QA_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=0.2,
    )


__all__ = [
    "LLMError",
    "is_configured",
    "get_llm_status",
    "probe_primary_model",
    "chat",
    "chat_async",
    "_invoke_model_async",
    "chat_stream_async",
    "_stream_model",
    "summarize_paper",
    "answer_with_context",
    "agnes_limiter",
    "model_manager",
]