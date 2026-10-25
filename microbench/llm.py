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
import weakref
from pathlib import Path

from . import netutil
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

# 注意: MiniMax-M3.1-Flash 强制开启思考模式，思考 token 与输出 token 共用
# max_tokens 预算。上限给太小会导致思考吃光额度、最终 content 为空或被截断。
DEFAULT_MAX_TOKENS = 4000
DEFAULT_TEMPERATURE = 0.3
DEFAULT_TIMEOUT = 60

# Hard cap on the number of upstream API calls ONE user request may make,
# counted across the probe, the primary provider's retries and the
# fallback provider's retries.
#
# Worst case before this cap: 7 upstream calls and ~12.5 minutes of wall
# clock for a single browser request (probe 15s + 3 primary attempts at
# 120s each with 1s/2s backoffs + 3 fallback attempts at 120s each with
# 3.5s/7s backoffs). A 3-chunk section translation calls chat() three
# times, so it could fire 21 upstream calls inside one minute — enough to
# trip the very per-minute cap the rate limiter exists to protect.
MAX_UPSTREAM_CALLS_PER_REQUEST = 6

# 标记：本次响应因为 max_tokens 被截断（思考模型把额度全花在 reasoning 上），
# 还没产出正文。上层据此抬高预算重试，或降级到非思考型备用模型。
TRUNCATED_MARKER = "__TRUNCATED_BY_MAX_TOKENS__"

# 截断有两种形态，处理方式相反，必须区分开：
#   TRUNCATED_NO_BODY  —— 思考烧光了预算，正文一个字没产出：抬预算救不回来，
#                         应该降级到非思考型模型（否则白等一两分钟）。
#   TRUNCATED_BODY_CUT —— 正文出来了但被截断（译文断在半句）：抬预算重试即可。
TRUNCATED_NO_BODY = "未产出正文"
TRUNCATED_BODY_CUT = "译文不完整"

# 抬高预算重试时的上限。再大也没有意义：M3.1-Flash 强制思考，实测 32000
# token 也可能不产出正文。
MAX_ESCALATION_MAX_TOKENS = 32000

# 已经确认「不接受 thinking.type=disabled」的模型名。
# MiniMax-M3.1-Flash 强制 adaptive thinking，传 disabled 直接 400。记住之后
# 就不再发这个参数，省掉每个分块一次 400 + 重试的往返（翻译一章要发好几次）。
_THINKING_DISABLE_UNSUPPORTED: set[str] = set()

# 已知「强制开启思考模式」的模型：推理 token 和正文共用同一个 max_tokens
# 预算池，预算给小了正文就一个字都出不来。给这类模型要预留更大的预算。
_THINKING_FORCED_MODELS: set[str] = {"MiniMax-M3.1-Flash"}


def model_forces_thinking(model: str) -> bool:
    """该模型是否强制开启思考模式。

    两种来源：
      1) 已知的强制思考模型（M3.1-Flash）—— 不接受 thinking.type=disabled。
      2) 运行期撞过 400 的模型（记在 _THINKING_DISABLE_UNSUPPORTED 里）。

    翻译这类「长文直译」任务要用它来决定预算：强制思考的模型，光给正文留
    额度是不够的，推理会先吃光（实测 M3.1-Flash 4000 token 时正文只剩 291
    字符，8000 token 时正文一个字都没有）。
    """
    if not model:
        return False
    return model in _THINKING_FORCED_MODELS or model in _THINKING_DISABLE_UNSUPPORTED

# 思考型模型（M3.1-Flash）的最小合理输出预算。低于这个值几乎必然被思考
# 吃光；达到这个值还失败，说明是输入本身让模型陷入无限思考，应直接降级。
MIN_REASONABLE_MAX_TOKENS = 4000


def _is_empty_content_error(msg: str) -> bool:
    """True when the failure was "the model returned no visible text"."""
    if not msg:
        return False
    return "返回空内容" in msg or "仅返回思考过程" in msg


class LLMError(Exception):
    """Raised when LLM call fails for any reason. Wraps upstream errors cleanly.

    `status_code` carries the upstream HTTP status when one is known, so
    callers can tell a quota exhaustion (429) from other failures without
    re-parsing the human-readable message.
    """

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# 错误人话化
# ---------------------------------------------------------------------------
# 上游返回的是英文机器话（HTTP 429: {"base_resp":{"status_msg":"..."}}），
# 直接甩到界面上用户看不懂，只会觉得"程序坏了"。这里把它翻译成一句
# 能照着做的中文，同时把原始报文写进 stderr（排查用），界面不再出现
# 英文原文 + 技术栈。
#
# ⚠️ 关键：翻译只发生在**给人看的那一层**（friendly_error_text）。
#    内部判定（配额耗尽 vs 限流、要不要降格）必须继续看原始报文，
#    所以 is_quota_exhausted_error() 吃的仍然是原始 text，不是这里的中文。

_HTTP_CODE_RE = re.compile(r"HTTP\s+(\d{3})\s*[:：]\s*(.*)", re.S)


def _friendly_http_error(code: int, quota_probe_text: str) -> str:
    """把一个上游 HTTP 错误翻译成一句能照着做的中文。

    quota_probe_text：用于判定"配额耗尽 vs 速率限制"的文本。
    传整条错误消息（而不是只有响应体）是有意的 —— 像
    "降格模型调用失败: HTTP 429: Too Many Requests (原主模型报错: HTTP 429: Token Plan limit exceeded)"
    这种，主模型那半句才是"额度用完"的证据，只看响应体会误判成限流。
    """
    if code == 429:
        # 配额耗尽（5 小时窗口 / 余额）和 速率限制（每分钟上限）是两回事：
        # 前者只能等额度恢复，后者等几十秒就行 —— 提示必须分开。
        try:
            is_quota = model_manager.is_quota_exhausted_error(429, quota_probe_text or "")
        except Exception:
            is_quota = False
        if is_quota:
            return (
                "模型额度用完了（HTTP 429 · 配额耗尽）：套餐/余额额度已用尽，"
                "现在重试也一样。等额度恢复再试，或在 .env 里配一个你自己的模型。"
            )
        return (
            "请求太频繁（HTTP 429 · 限流）：刚发出的请求超过了该模型的每分钟上限。"
            "等几十秒再试，或换一个模型/拉长间隔。"
        )
    if code == 401:
        return (
            "鉴权失败（HTTP 401）：API Key 无效或没有权限，"
            "检查 .env 里的 LLM_API_KEY / AGNES_API_KEY。"
        )
    if code == 403:
        return "鉴权被拒（HTTP 403）：API Key 没有这个模型的权限，换一个 key 或模型。"
    if code == 404:
        return "接口或模型不存在（HTTP 404）：检查 .env 里的 base_url 和模型名是否写错。"
    if code == 400:
        return "请求被上游拒绝（HTTP 400）：模型名或参数不对，检查 .env 里的模型配置。"
    if 500 <= code < 600:
        return f"上游模型服务暂时不可用（HTTP {code}）：稍后重试；一直失败就换个模型。"
    return f"模型接口返回错误（HTTP {code}）。"


def friendly_error_text(exc: BaseException) -> str:
    """把内部异常翻译成**给用户看**的中文。

    设计约束（重要）：
    - 内部判定（配额 vs 限流、要不要降格、TRUNCATED 标记）继续吃原始报文，
      这里只负责"给人看的那一句"，所以不改 str(exc) 的语义。
    - 已经是中文的错误（超时 / 降格模型调用失败 …）原样返回，
      不做二次包装。
    """
    msg = str(exc)
    m = _HTTP_CODE_RE.search(msg)
    if not m:
        return msg
    code = int(m.group(1))
    raw = m.group(2)
    # 原始报文只进日志，不进界面。
    # ⚠️ 打印前必须脱敏：上游报文里可能带 key-like 字段
    #    （实测 {"api_key":"sk-..."} 会原样进 stderr）。
    print(
        f"[MicroBench] 上游返回 HTTP {code}: {_redact_secrets(raw)[:400]}",
        file=sys.stderr,
    )
    friendly = _friendly_http_error(code, msg)
    # 保留前面那段中文上下文（例如「主模型 (MiniMax-M3) 调用失败:」），
    # 它告诉用户是**哪条链路**挂了，比光一句"限流"有用。
    prefix = msg[:m.start()].strip().rstrip(":：").strip()
    if prefix:
        return f"{prefix}：{friendly}"
    return friendly


# ---------------------------------------------------------------------------
# Sliding-window Rate Limiter (shared by both providers)
# ---------------------------------------------------------------------------
# One generic class replaces the previously copy-pasted AgnesRateLimiter
# and PrimaryRateLimiter pairs. Each provider instantiates one with its
# own (max_requests, min_interval) — see agnes_limiter / primary_limiter
# below.

class SlidingWindowRateLimiter:
    """
    Thread-safe sliding-window rate limiter shared across providers.

    Two guards enforce together:
      1. min_interval: minimum seconds between any two consecutive requests
      2. max_requests within window_seconds (sliding-window RPM cap)

    Defaults reflect the Agnes free-tier 10 RPM (we cap at 9/60s with
    6s minimum spacing for safety). Primary (MiniMax) gets 28/60s + 2s
    spacing — generous enough for batch translation without tripping
    the 5-hour Token Plan quota.
    """

    def __init__(self, max_requests: int = 9, window_seconds: float = 60.0, min_interval: float = 6.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.min_interval = min_interval
        self._lock = Lock()
        # Planned slot times — may be in the future. Used for the window
        # cap and the min-interval spacing, so a pending reservation
        # correctly counts against the budget.
        self._timestamps: list[float] = []
        # Actual call times — only appended once the slot has really been
        # used. Used for the "recent requests" statistic, which must not
        # count a reservation that has not fired yet.
        self._actual_times: list[float] = []
        self._last_call_time: float = 0.0
        # The *planned* time of the most recent reservation. It can be in
        # the future (that is the whole point of reserving a slot), so it
        # must not be used to compute "seconds since the last call" — that
        # would report a negative number.
        self._last_planned_time: float = 0.0

    def acquire(self) -> float:
        """
        Block until safe to make a request under the RPM cap and min interval.
        Returns the number of seconds waited (if any).

        The sleep happens OUTSIDE the lock. The previous implementation held
        the lock across `time.sleep()`, so a queued request froze every other
        caller of the limiter — including `stats()`, which the
        /api/llm/status endpoint and both translation endpoints call. A
        single queued translation could therefore stall the status readout
        for the whole min-interval window.

        Correctness of the min-interval spacing is preserved by *reserving*
        the slot under the lock: the planned call time is recorded and
        `_last_call_time` is advanced before the lock is released, so a
        second thread arriving during the sleep still sees the reservation
        and waits its own turn.
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
                # Reserve the slot atomically, then sleep without the lock.
                planned = now + wait_time
                self._timestamps.append(planned)
                self._last_planned_time = planned
            else:
                self._timestamps.append(now)
                self._last_planned_time = now

        if wait_time > 0:
            time.sleep(wait_time)
        # Record the moment the call actually happened (after the sleep).
        with self._lock:
            fired = time.time()
            self._last_call_time = fired
            self._actual_times.append(fired)
            cutoff = fired - self.window_seconds
            self._actual_times = [t for t in self._actual_times if t > cutoff]
        return wait_time

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            # Only count slots that have actually fired. A reservation for
            # a future time is not a request that has happened yet.
            active = [t for t in self._actual_times if t > cutoff]
            return {
                "rpm_limit": self.max_requests + 2,  # +2 buffer for display
                "safe_rpm_cap": self.max_requests,
                "recent_requests_1m": len(active),
                "last_call_seconds_ago": round(now - self._last_call_time, 1) if self._last_call_time else None,
                "next_slot_in_seconds": (
                    round(max(0.0, self._last_planned_time - now), 1)
                    if self._last_planned_time else None
                ),
            }


# Free-tier Agnes (10 RPM ceiling → 9 cap + 6s min spacing)
agnes_limiter = SlidingWindowRateLimiter(max_requests=9, min_interval=6.0)

# Primary (MiniMax M2.7) — 30 RPM observed ceiling → 28 cap + 2s min spacing
primary_limiter = SlidingWindowRateLimiter(max_requests=28, min_interval=2.0)

# 自定义模型（.env 里 LLM_CUSTOM_N_*）各有自己的限流档，不能跟主模型共用。
# 这里按 (rpm, min_interval) 缓存 limiter：同档的模型共用一个，不同档的分开。
_custom_limiters: dict[tuple, SlidingWindowRateLimiter] = {}
_custom_limiters_lock = Lock()


def _limiter_for(rpm: int, min_interval: float) -> SlidingWindowRateLimiter:
    """按 (rpm, 间隔) 取一个限流器；同档共用一个。"""
    cap = max(1, rpm - 1)
    key = (cap, round(min_interval, 2))
    with _custom_limiters_lock:
        lim = _custom_limiters.get(key)
        if lim is None:
            lim = SlidingWindowRateLimiter(max_requests=cap, min_interval=min_interval)
            _custom_limiters[key] = lim
        return lim


# ---------------------------------------------------------------------------
# 自定义模型（用户自己的 API 模型）
# ---------------------------------------------------------------------------
# 在 .env 里按编号配置，最多 5 个：
#
#   LLM_CUSTOM_1_NAME=我的视觉模型
#   LLM_CUSTOM_1_BASE_URL=https://api.example.com/v1
#   LLM_CUSTOM_1_MODEL=my-vision-model
#   LLM_CUSTOM_1_API_KEY=sk-xxx          # 留空则复用 LLM_API_KEY
#   LLM_CUSTOM_1_RPM=30                   # 每分钟请求上限（默认 30，同主模型）
#
# 前提：**OpenAI 兼容**（POST {base_url}/chat/completions）。
# 表格/公式要靠多模态读图，所以模型还得支持图片输入（见 docs/LLM_CONTRACT.md）。
#
# 限流：只有 **Agnes（免费版）** 有严格速率限制，所以它单独一档（9/60s + 6s 间隔）。
# 付费模型（MiniMax / DeepSeek / 自己的 API）默认按主模型那档走（30 RPM + 2s 间隔），
# 基本感觉不到；对方的限制更紧时把 LLM_CUSTOM_N_RPM 调小即可。

CUSTOM_MODEL_PREFIX = "custom:"
CUSTOM_MODEL_SLOTS = 5
# 付费模型的默认档：跟主模型一样宽松（用户原话："DeepSeek 和 MiniMax 这个速率
# 就不用管了"—— 只有免费版 Agnes 才吃速率）
DEFAULT_CUSTOM_RPM = 30
DEFAULT_CUSTOM_MIN_INTERVAL = 2.0


def custom_model_configs() -> list[dict]:
    """读 .env 里的自定义模型清单（LLM_CUSTOM_N_*）。"""
    out: list[dict] = []
    for i in range(1, CUSTOM_MODEL_SLOTS + 1):
        base_url = (os.getenv(f"LLM_CUSTOM_{i}_BASE_URL", "") or "").strip().rstrip("/")
        model = (os.getenv(f"LLM_CUSTOM_{i}_MODEL", "") or "").strip()
        if not base_url or not model:
            continue
        name = (os.getenv(f"LLM_CUSTOM_{i}_NAME", "") or "").strip() or model
        api_key = (os.getenv(f"LLM_CUSTOM_{i}_API_KEY", "") or "").strip() \
            or (os.getenv("LLM_API_KEY", "") or "").strip()
        try:
            rpm = int((os.getenv(f"LLM_CUSTOM_{i}_RPM", "") or "").strip() or DEFAULT_CUSTOM_RPM)
        except ValueError:
            rpm = DEFAULT_CUSTOM_RPM
        rpm = max(1, min(rpm, 120))
        # 间隔按 rpm 推，留 10% 余量；至少 1 秒
        min_interval = max(1.0, round(60.0 / rpm * 0.9, 2)) \
            if rpm != DEFAULT_CUSTOM_RPM else DEFAULT_CUSTOM_MIN_INTERVAL
        out.append({
            "id": f"{CUSTOM_MODEL_PREFIX}{name}",
            "label": name,
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
            "rpm": rpm,
            "min_interval": min_interval,
            "configured": bool(api_key),
            "is_fallback": False,
            "is_custom": True,
        })
    return out


def resolve_model_config(model_id: str) -> Optional[dict]:
    """按模型 id 找配置。

    - ""            → None（跟随默认设置）
    - "agnes"       → 备用模型配置（Agnes）
    - "custom:xxx"  → 自定义模型配置
    - 其它名字       → None（表示"同一套 key/base_url 下的另一个模型名"）
    """
    mid = (model_id or "").strip()
    if not mid:
        return None
    if mid.lower() in ("agnes", "fallback", "agnes-3.0-flash"):
        cfg = dict(model_manager.get_fallback_config())
        cfg.setdefault("id", "agnes")
        cfg.setdefault("label", "Agnes 3.0 Flash")
        cfg.setdefault("rpm", 11)
        cfg.setdefault("min_interval", 6.0)
        return cfg
    if mid.startswith(CUSTOM_MODEL_PREFIX):
        for cfg in custom_model_configs():
            if cfg["id"] == mid:
                return cfg
    return None


def limiter_for_config(cfg: dict) -> SlidingWindowRateLimiter:
    """按配置取限流器：主/备用各用自己那套；自定义模型按它自己的 RPM 档。"""
    if cfg.get("is_fallback"):
        return agnes_limiter
    if cfg.get("is_custom"):
        return _limiter_for(int(cfg.get("rpm") or DEFAULT_CUSTOM_RPM),
                             float(cfg.get("min_interval") or 6.0))
    return primary_limiter


def is_agnes_config(cfg: dict) -> bool:
    """这个配置是不是走 Agnes 档（决定 429 退避策略与限流档）。"""
    if not cfg:
        return False
    return bool(cfg.get("is_fallback")) or \
        "agnes" in (cfg.get("base_url") or "").lower() or \
        "agnes" in (cfg.get("model") or "").lower()


# ---------------------------------------------------------------------------
# Secret redaction for anything that reaches the browser
# ---------------------------------------------------------------------------

# (pattern, group index whose *content* is replaced by "***"; None = replace
#  the whole match). Keeping the prefix (e.g. "Bearer ") makes the message
# readable while still removing the credential.
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{4,}"), None),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}"), 1),
    (re.compile(r"((?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+", re.IGNORECASE), 1),
)


def _redact_secrets(text: str) -> str:
    """Strip credential-shaped substrings from text that will be displayed."""
    if not text:
        return text
    out = text
    for pat, keep_group in _SECRET_PATTERNS:
        if keep_group is None:
            out = pat.sub("***", out)
        else:
            out = pat.sub(lambda m, g=keep_group: m.group(g) + "***", out)
    return out


def _safe_base_url(url: str) -> str:
    """Return a URL with any embedded userinfo (user:password@) removed."""
    if not url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        if not parts.hostname:
            return url
        netloc = parts.hostname
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return url


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
                base_url = "https://api.minimaxi.com/v1"
            else:
                base_url = DEFAULT_BASE_URL
        if not model:
            model = "MiniMax-M3.1-Flash" if "minimax" in base_url.lower() else DEFAULT_MODEL

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

    def mark_degraded(self, reason: str, sticky: bool = False):
        """
        标记主模型降格。

        sticky=True 用于「主模型不是不可用，而是对当前任务产不出有效正文」
        这类情况（例如思考模型遇到乱序 PDF 文本陷入无限思考）：这种降格
        不能靠 15 分钟一次的通用探针自动恢复——探针只是发个 "1"，必然成功，
        但真正的翻译任务还是会失败，来回升格/降格只会白白多等几十秒。
        这类情况需要用户手动点「探测升格」。
        """
        with self._lock:
            self.is_degraded = True
            self.degraded_reason = reason
            self.degraded_at = time.time()
            self.last_primary_probe = time.time()
            self.sticky_degraded = sticky
            print(
                f"[MicroBench] ⚠️ 主模型触发降格: {reason}. "
                f"已自动切换至备用模型 (Agnes 3.0 Flash, 20 RPM 保护)."
                + (" [需手动探测升格]" if sticky else ""),
                file=sys.stderr,
            )

    def mark_recovered(self):
        with self._lock:
            self.is_degraded = False
            self.degraded_reason = ""
            self.sticky_degraded = False
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
            if getattr(self, "sticky_degraded", False):
                return False  # 需要用户手动探测升格
            now = time.time()
            return (now - self.last_primary_probe) >= self.probe_interval_seconds

    def is_quota_exhausted_error(self, status_code: int, err_text: str) -> bool:
        """Detect if an error signifies MiniMax 5-hour quota exhaustion or Token Plan limit.

        This distinction matters a lot, because it decides whether the app
        permanently demotes to the weaker fallback model (Agnes 3.0 Flash,
        20 RPM). The old keyword list included the bare words "limit" and
        "exceeded", so an ordinary per-minute RPM cap — e.g.
        "429 rate limit exceeded (per-minute RPM cap)" — was classified as
        "the 5-hour token plan is spent". The app then demoted to Agnes,
        and 15 minutes later the probe succeeded and promoted MiniMax back,
        so the user saw the same question answered well one minute and badly
        the next, while burning quota on both models.

        Quota exhaustion now requires an *unambiguously quota-shaped* signal.
        A pure rate-limit message (RPM / QPS / "Too Many Requests") is NOT
        quota exhaustion and must not trigger a demotion.
        """
        if status_code != 429:
            return False
        text = (err_text or "").lower()

        quota_signals = (
            "token plan",      # MiniMax: "Token Plan limit exceeded"
            "2056",            # MiniMax quota error code
            "quota",
            "额度",
            "套餐",
            "充值",
            "余额",
            "欠费",
            "insufficient",
            "balance",
            "billing",
            "exhausted",       # "resource exhausted" / "quota exhausted"
        )
        if not any(sig in text for sig in quota_signals):
            return False

        # A message that is *only* about rate limiting is not a quota
        # problem, even if it happens to contain a quota-ish word.
        rate_only_signals = (
            "rate limit",
            "too many requests",
            "requests per minute",
            "per-minute",
            "rpm",
            "qps",
            "每秒",
            "每分钟",
        )
        has_rate_signal = any(sig in text for sig in rate_only_signals)
        if has_rate_signal:
            # "rate limit" + a genuine quota word (e.g. "quota exceeded")
            # is still a quota problem; "rate limit exceeded" alone is not.
            quota_only = [s for s in quota_signals if s in text]
            if all(s in ("quota", "exhausted") for s in quota_only):
                # "quota exceeded" from a rate limiter — still ambiguous,
                # so treat the rate-limit reading as the safe one.
                return False
        return True

    def probe_primary(self) -> dict:
        """
        Explicitly probe primary model with a minimal request to test if quota has reset.
        If successful, auto-upgrades back to primary.
        """
        if requests is None:
            return {"success": False, "message": "requests 包未安装, 无法探针"}

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
            # The probe is a real upstream call — it must go through the
            # primary limiter, otherwise the probe itself can be the call
            # that trips the per-minute cap the limiter protects against.
            primary_limiter.acquire()
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                self.mark_recovered()
                return {
                    "success": True,
                    "model": primary["model"],
                    "message": f"主模型 ({primary['model']}) 探测成功，已自动升格为首选模型！",
                }
            else:
                # Only the status code is surfaced. The raw body can carry
                # provider internals (and, for a misconfigured gateway,
                # an echoed Authorization header), and this string is
                # returned to the browser by /api/llm/probe.
                err = f"HTTP {resp.status_code}"
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
            "degraded_reason": _redact_secrets(self.degraded_reason),
            "degraded_at": self.degraded_at,
            "primary": {
                "model": primary["model"],
                # Redacted: a self-hosted gateway is commonly configured as
                # https://user:sk-secret@host/v1, and /api/llm/status hands
                # this string straight to the browser.
                "base_url": _safe_base_url(primary["base_url"]),
                "configured": primary["configured"],
            },
            "fallback": {
                "model": fallback["model"],
                "base_url": _safe_base_url(fallback["base_url"]),
                "configured": fallback["configured"],
            },
            "next_probe_in_seconds": next_probe,
            "rate_limit": {
                "primary": primary_limiter.stats(),
                "agnes": agnes_limiter.stats(),
            },
            # 自定义模型（用户自己的 API 模型）：给界面下拉框和限流提示用
            "custom_models": [
                {
                    "id": c["id"],
                    "label": c["label"],
                    "model": c["model"],
                    "base_url": _safe_base_url(c["base_url"]),
                    "configured": c["configured"],
                    "rpm": c["rpm"],
                    "min_interval": c["min_interval"],
                    "rate_limit": limiter_for_config(c).stats(),
                }
                for c in custom_model_configs()
            ],
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


def translation_disable_thinking() -> bool:
    """
    翻译任务是否关闭思考模式。

    实测（2015 IEEE 双栏论文整页 2766 字符，max_tokens=4000）:
        MiniMax-M2.7        开着思考 23.0s，且 thinking.type=disabled 被
                            **静默忽略**（content 2630 字符里约 1500 字符是
                            思考），译文本身是完整的
        MiniMax-M3          关掉思考  8.4s，译文完整、无思考泄漏
        MiniMax-M3.1-Flash  强制思考，传 disabled 直接 400；
                            开着思考 41.7s，14688 字符的推理把 4000 token
                            预算吃光，正文只剩 291 字符还被截断 —— 这就是
                            「慢 + 乱」的来源

    可用环境变量 LLM_TRANSLATION_DISABLE_THINKING=0 关掉。
    """
    v = (os.getenv("LLM_TRANSLATION_DISABLE_THINKING", "1") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def translation_model_name() -> str:
    """
    翻译任务专用的模型名（空串 = 跟随主模型）。

    为什么需要单独指定：MiniMax-M3.1-Flash **强制**开启思考模式
    （thinking.type=disabled 直接 400），实测整页翻译 41.7s、83.2s，推理把
    预算吃光导致正文被截断；MiniMax-M3 支持关思考，同一页只要 8.4s 且译文
    完整。所以「读论文」和「做推理」用不同模型更合适：

        LLM_MODEL_NAME=MiniMax-M3.1-Flash   # 解读 / 答疑（要推理）
        LLM_TRANSLATION_MODEL=MiniMax-M3    # 批量直译（要快、要完整）

    两者用同一把 key、同一个 base_url，只是模型名不同。
    """
    return (os.getenv("LLM_TRANSLATION_MODEL", "") or "").strip()


def translation_active_model() -> str:
    """翻译任务实际会用到的模型名（翻译专用模型优先，否则跟随主模型）。

    上报和缓存键都要用这个 —— 之前用主模型的 active_model 上报，翻译其实
    走的是另一个模型，界面上显示的模型名是错的，缓存也会串。
    """
    t = translation_model_name()
    if t:
        return t
    return model_manager.get_status().get("active_model") or "default"


# ---------------------------------------------------------------------------
# HTTP session helpers (proxy-aware)
# ---------------------------------------------------------------------------
#
# 很多国内用户的电脑上跑着本地代理客户端（VPN），它会把 HTTP_PROXY /
# HTTPS_PROXY 指向 127.0.0.1:<端口>。一旦那个软件没在运行，所有出网请求
# 都会抛 ProxyError —— 工作台看起来「坏了」，但界面上没有任何显式线索。
#
# 处理方式：第一次撞到 ProxyError 就记下「环境代理是死的」，之后所有请求
# 直接绕过代理；直连再失败才把原始错误抛给上层。

_sessions: dict = {}


def _get_session(use_env_proxy: bool):
    """Get a requests.Session, optionally honoring environment proxies.

    `trust_env=False` means "ignore HTTP_PROXY/HTTPS_PROXY entirely" —
    that's what we fall back to when the local proxy client isn't running.
    """
    key = bool(use_env_proxy)
    sess = _sessions.get(key)
    if sess is None:
        sess = requests.Session()
        sess.trust_env = key
        _sessions[key] = sess
    return sess


def _mark_proxy_dead(err: Exception) -> None:
    """Remember that the environment proxy is unusable (once).

    状态放在 netutil 里，和 paper_fetcher 共用同一份 —— 抓论文和调模型
    走的是同一个本地代理，代理挂了要一起知道。
    """
    netutil.mark_proxy_dead(err)

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
    budget: Optional[list[int]] = None,
    disable_thinking: bool = False,
    deadline: Optional[float] = None,
) -> tuple[bool, str, Optional[int]]:
    """
    Execute request against a specific model configuration.
    Returns: (success: bool, content_or_error: str, status_code: Optional[int])

    `budget` is a one-element list shared by every provider call of ONE user
    request. Each upstream attempt decrements it; when it hits zero the
    call gives up instead of continuing to retry. This bounds the worst
    case for a single browser request (see MAX_UPSTREAM_CALLS_PER_REQUEST).

    `deadline` is a `time.monotonic()` timestamp: no new upstream attempt may
    start after it, and each attempt's own timeout is clamped to the time
    left. Without it, `max_retries` + the budget-escalation retry could keep a
    request running for many minutes past the caller's own limit.

    disable_thinking=True 会带上 thinking={"type":"disabled"}，让模型直接给
    正文不做推理。适合"长文直译"这类不需要推理的任务：实测 MiniMax-M3
    整页直译关思考 21.5s 就有完整译文，开启思考反而容易在推理里打转。

    注意不是所有 MiniMax 模型都接受这个参数：M3.1-Flash 强制 adaptive
    thinking，传 disabled 会返回 400。这里遇到 400 且错误信息提到 thinking
    时会自动去掉该参数重试一次。
    """
    is_agnes = is_agnes_config(cfg)
    # 限流器按配置取：主/备用各一套，自定义模型按它自己的 RPM 档
    limiter = limiter_for_config(cfg)

    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if disable_thinking and cfg["model"] not in _THINKING_DISABLE_UNSUPPORTED:
        payload["thinking"] = {"type": "disabled"}
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
        # Global per-request budget: stop instead of burning more quota
        # (and more of the user's wall clock) once the cap is reached.
        if budget is not None:
            if budget[0] <= 0:
                return False, (
                    f"重试预算耗尽 (单次请求上游调用上限 "
                    f"{MAX_UPSTREAM_CALLS_PER_REQUEST})"
                ), last_status
            budget[0] -= 1

        # Deadline: no new upstream attempt may start past it, and this
        # attempt's own timeout is clamped to whatever time is left. Without
        # this, `max_retries` (x3 attempts) plus the budget-escalation retry
        # could keep one browser request running for minutes past the
        # caller's limit.
        attempt_timeout = timeout
        if deadline is not None:
            left = deadline - time.monotonic()
            # 剩不到 3 秒就别开新调用了 —— 开了也来不及，还会超出时限
            if left < 3:
                return False, "超出本次请求的时限, 已放弃重试", last_status
            attempt_timeout = max(2, min(timeout, int(left)))

        # 限流：主/备用各一套；自定义模型按它自己的 RPM 档
        limiter.acquire()
        try:
            if netutil.proxy_bypassed():
                # 已经确认环境代理是死的 → 用 proxies 显式置空来直连。
                # 不要换成 requests.Session：调用路径一变，测试对
                # requests.post 的 monkeypatch 就失效（会静默真的发出网请求，
                # 而且全局的"代理已死"标志会让后续用例全部走偏）。
                # proxies 置空同样能直连：requests 的 merge_environment_settings
                # 用 setdefault，键已存在就不会被环境变量覆盖。
                resp = requests.post(
                    url, json=payload, headers=headers, timeout=attempt_timeout,
                    proxies={"http": None, "https": None},
                )
            else:
                resp = requests.post(url, json=payload, headers=headers, timeout=attempt_timeout)
            last_status = resp.status_code
        except requests.exceptions.ProxyError as e:
            # 本机配了本地代理但代理软件没运行 → 记下来，改直连重试一次
            if not netutil.proxy_bypassed():
                _mark_proxy_dead(e)
                continue
            return False, f"代理连接失败: {e}", last_status
        except RETRYABLE_EXC as e:
            last_err = str(e)
            if deadline is not None and (deadline - time.monotonic()) < 3:
                # 这次超时是被时限截断的，不是网络故障 —— 说人话，别把
                # requests 的原始异常甩给界面。
                return False, "超出本次请求的时限, 已放弃重试", last_status
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

            # 模型不支持 thinking 参数（例如 M3.1-Flash 强制 adaptive thinking），
            # 去掉该参数重试一次，别直接失败。顺便把这个模型记下来：之后
            # 直接不再发该参数，省掉每个分块一次 400 + 重试的往返。
            if resp.status_code == 400 and "thinking" in payload and "thinking" in err.lower():
                payload.pop("thinking", None)
                disable_thinking = False
                _THINKING_DISABLE_UNSUPPORTED.add(cfg["model"])
                if attempt < max_retries:
                    continue

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

            return False, f"HTTP {resp.status_code}: {_redact_secrets(err)}", resp.status_code

        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message", {}) or {}
            finish_reason = choice.get("finish_reason", "")
            content = message.get("content")
            if not isinstance(content, str):
                # provider 返回 content: null —— 绝不能当成成功返回，
                # 否则调用方会拿着 None 继续走，最终 500 或把 null 回给用户。
                content = ""
            content = _strip_thinking_blocks(content)
            if not content.strip():
                # 思考模型（MiniMax-M3.1-Flash 强制开思考）可能把 max_tokens
                # 全部花在 reasoning_content 上，正文为空。这种情况必须显式
                # 报错并抬高预算重试，绝不能把空串当成"翻译成功"返回 ——
                # 否则前端会显示"已翻译"但页面里什么都没有。
                reasoning = message.get("reasoning_content") or ""
                if finish_reason == "length":
                    return False, (
                        f"{TRUNCATED_MARKER}: 输出被 max_tokens 截断"
                        f"(finish_reason=length, 思考约占 {len(reasoning)} 字符), 未产出正文"
                    ), 200
                if reasoning.strip():
                    # 正文全在 <think> 块里，剥掉思考后没有可见文本。
                    return False, (
                        f"LLM 仅返回思考过程, 无正文 (finish_reason={finish_reason!r}, "
                        f"思考约占 {len(reasoning)} 字符, max_tokens={max_tokens})"
                    ), 200
                return False, (
                    f"模型返回空内容 (finish_reason={finish_reason!r}, "
                    f"思考约占 {len(reasoning)} 字符, max_tokens={max_tokens})"
                ), 200
            if finish_reason == "length":
                # 正文有内容但被 max_tokens 截断（思考 token 与正文共用同一个
                # 预算池，思考吃多了正文就断在半句）。在翻译场景这就是"译文断
                # 在半截"，绝不能当成成功返回 —— 否则前端显示"已翻译"，用户
                # 拿到的却是残缺译文。必须上报截断，让上层抬高预算重试。
                return False, (
                    f"{TRUNCATED_MARKER}: 输出被 max_tokens 截断"
                    f"(finish_reason=length, 正文 {len(content)} 字符, "
                    f"思考 {len(message.get('reasoning_content') or '')} 字符), "
                    f"译文不完整"
                ), 200
            return True, content, 200
        except (KeyError, IndexError, ValueError, AttributeError) as e:
            return False, f"LLM 响应格式异常: {e}; raw={resp.text[:200]}", 200

    return False, last_err, last_status


def _invoke_with_budget_escalation(
    cfg: dict,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    timeout: int,
    json_mode: bool,
    max_retries: int,
    disable_thinking: bool = False,
    budget: Optional[list[int]] = None,
    deadline: Optional[float] = None,
) -> tuple[bool, str, Optional[int]]:
    """
    调用模型；若正文因 max_tokens 被截断，按截断的形态分别处理：

    1) 正文被截断（"译文不完整"）—— 预算不够，抬高额度重试一次。
       思考 token 与正文共享同一个 max_tokens 预算池，思考吃多了正文就断在
       半句。旧逻辑是「max_tokens >= MIN_REASONABLE 就不抬」，而翻译默认
       用的就是 4000 —— 恰恰是最容易被截断的档位，截断了却不重试，等于把
       残缺译文直接抛给用户（表现为「已翻译但内容是乱的」）。

    2) 一个字正文都没产出（"未产出正文"）—— 思考型模型把整个预算烧在推理上
       （实测 pdfplumber 抽出的乱序双栏文本会这样，32000 token 仍不产出正文）。
       这时再抬预算只是白白多等一两分钟，应该直接让 chat() 降级到非思考型
       的备用模型。

    `budget` 与 chat() 共享同一个上游调用计数器，抬高重试同样计入总预算，
    避免"重试 + 降级 + 抬高"叠在一起把单次请求的上游调用数放大到失控。
    """
    success, res, status = _invoke_model(
        cfg, messages, max_tokens, temperature, timeout, json_mode,
        max_retries=max_retries, disable_thinking=disable_thinking, budget=budget,
        deadline=deadline,
    )
    if success or TRUNCATED_MARKER not in (res or ""):
        return success, res, status
    if budget is not None and budget[0] <= 0:
        return success, res, status
    if TRUNCATED_NO_BODY in (res or ""):
        # 形态 2：思考烧光了预算，正文一个字没有。抬预算救不回来，直接降级。
        return success, res, status
    if deadline is not None and time.monotonic() >= deadline:
        # 已经没有时间做"抬高预算重试"了 —— 别让用户为一次重试再等一分钟。
        return success, res, status

    bigger = min(
        max(max_tokens * 2, MIN_REASONABLE_MAX_TOKENS),
        MAX_ESCALATION_MAX_TOKENS,
    )
    if bigger <= max_tokens:
        return success, res, status
    success2, res2, status2 = _invoke_model(
        cfg, messages, bigger, temperature, timeout, json_mode,
        max_retries=max_retries, disable_thinking=disable_thinking, budget=budget,
        deadline=deadline,
    )
    if success2:
        return True, res2, status2
    return False, f"{res} | 抬高 max_tokens 至 {bigger} 重试后仍失败: {res2}", status2


def chat(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    json_mode: bool = False,
    max_retries: int = 2,
    disable_thinking: bool = False,
    model_name: Optional[str] = None,
    deadline: Optional[float] = None,
    force_fallback: bool = False,
    ret: Optional[dict] = None,
) -> str:
    """
    Single-turn chat completion with automatic failover and degradation management.

    1. If degraded and probe cooldown passed, attempts Primary. If successful, upgrades back.
    2. Otherwise, if not degraded, calls Primary. If Primary returns quota 429,
       immediately triggers degradation and fails over to Agnes 3.0 Flash seamlessly.
    3. If degraded, calls Agnes 3.0 Flash with 20 RPM sliding window rate limiting.

    disable_thinking=True：请求时带 thinking={"type":"disabled"}，让模型直接
    输出正文不做推理（长文直译这类任务用它又快又稳；模型不支持时自动降级）。

    model_name：临时换模型（同一套 key / base_url）。批量翻译用得上 ——
    MiniMax-M3.1-Flash 强制思考不适合直译，详见 translation_model_name()。

    deadline：`time.monotonic()` 时间戳。过了这个点就不再发起新的上游调用，
    每次调用的超时也会被压到剩余时间内。翻译这类"质量优先但别无限等"的
    场景用它把整条链路兜在固定时限内。

    force_fallback：强制走备用模型（Agnes 3.0 Flash），无视当前的降级状态。
    用户在界面上手动"换模型翻译"时用 —— 主模型额度用完了想换免费模型，
    不该非得等降级探测。
    """
    if requests is None:
        raise LLMError("requests 包未安装, 请先 pip install requests")

    def _note(used_model: str, fell_back: bool = False) -> None:
        """记录**实际**用的是哪个模型。

        界面上的模型徽标必须显示实际用的模型：用户选了自定义模型但它连不上
        时，我们会自动退回主模型 —— 这时如果还显示"用户选的那个"，
        用户会以为"选模型没生效"（或者反过来，以为新模型在工作）。
        """
        if ret is not None:
            ret["model"] = used_model
            ret["fallback_used"] = fell_back

    primary = model_manager.get_primary_config()
    custom_cfg = resolve_model_config(model_name) if model_name else None
    is_custom = bool(custom_cfg and custom_cfg.get("is_custom"))
    if model_name and not is_custom and model_name != primary["model"]:
        primary = dict(primary, model=model_name)
    fallback = model_manager.get_fallback_config()

    if not primary["configured"] and not fallback["configured"] and not is_custom:
        raise LLMError(
            "LLM 未配置: 请设置环境变量 LLM_API_KEY (主模型) 或 AGNES_API_KEY (备用模型). "
            "可在工作台根目录编辑 .env 文件."
        )

    # 强制走备用模型：直接用 fallback 配置，跳过主模型与降级探测
    if force_fallback:
        if not fallback["configured"]:
            raise LLMError("备用模型未配置 (AGNES_API_KEY), 无法强制使用")
        success, res, status_code = _invoke_with_budget_escalation(
            fallback, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, disable_thinking=disable_thinking,
            budget=[MAX_UPSTREAM_CALLS_PER_REQUEST], deadline=deadline,
        )
        if success:
            _note(fallback["model"])
            return res
        raise LLMError(f"备用模型调用失败: {res}")

    # 用户选了自定义模型（自己的 base_url/key）：先用它。
    # 连不上 / 报错 → **自动退回**主模型 → 备用模型链路（用户原话：
    # "如果发现连不上了，它能自动退"）。退回时打 stderr，别静悄悄换模型。
    if is_custom:
        if not custom_cfg.get("configured"):
            raise LLMError(
                f"自定义模型「{custom_cfg.get('label')}」没有配置 API key"
                f"（在 .env 里填 LLM_CUSTOM_N_API_KEY，或让它复用 LLM_API_KEY）"
            )
        success, res, status_code = _invoke_with_budget_escalation(
            custom_cfg, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, disable_thinking=disable_thinking,
            budget=[MAX_UPSTREAM_CALLS_PER_REQUEST], deadline=deadline,
        )
        if success:
            _note(custom_cfg.get("label") or custom_cfg.get("model"))
            return res
        print(f"[MicroBench] 自定义模型「{custom_cfg.get('label')}」调用失败，"
              f"自动退回默认模型链路: {res}", file=sys.stderr)
        # 继续往下走主/备链路（primary 已经是默认配置，没被改写）

    # Opportunistic auto-recovery probe if degraded and probe cooldown reached
    if model_manager.is_degraded and primary["configured"] and model_manager.should_opportunistic_probe():
        probe_res = model_manager.probe_primary()
        if probe_res.get("success"):
            # Successfully upgraded!
            pass

    # Shared budget for the WHOLE user request — every upstream call this
    # request makes (probe + primary attempts + fallback attempts) draws
    # from it. Without a global cap the worst case was 7 upstream calls
    # and ~12.5 minutes of wall clock for a single browser request:
    #   probe(1) + primary×3 (3×120s + 1s + 2s) + fallback×3 (3×120s + 3.5s + 7s)
    # A 3-chunk section translation calls chat() three times, so it could
    # fire 21 upstream calls in one minute — which is itself enough to trip
    # the per-minute cap the limiter is supposed to protect against.
    budget = [MAX_UPSTREAM_CALLS_PER_REQUEST]

    # If NOT degraded and Primary is configured, try Primary first
    if not model_manager.is_degraded and primary["configured"]:
        success, res, status_code = _invoke_with_budget_escalation(
            primary, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, disable_thinking=disable_thinking, budget=budget,
            deadline=deadline,
        )
        if success:
            _note(primary["model"])
            return res

        # Reasoning-model budget guard: MiniMax-M2.7 emits a <think> block
        # before the answer. When max_tokens is too small the whole budget
        # is consumed by the reasoning and the visible answer comes back
        # empty. Measured: max_tokens=200 → empty, 800 → fine, and the
        # exact threshold moves with the question's difficulty. Retry once
        # with a doubled budget instead of showing the user a blank box.
        # (被 max_tokens 截断的情况已在 _invoke_with_budget_escalation 内部
        #  抬高预算重试过，这里只兜底"没截断但正文为空"的情况。)
        if _is_empty_content_error(res) and budget[0] > 0:
            bigger = min(max_tokens * 2, 4096)
            retry_success, retry_res, _ = _invoke_model(
                primary, messages, bigger, temperature, timeout, json_mode,
                max_retries=0, budget=budget, disable_thinking=disable_thinking,
                deadline=deadline,
            )
            if retry_success:
                _note(primary["model"])
                return retry_res
            res = f"{res} (重试 {bigger} tokens 仍为空)"

        # 降级条件有两种：
        #   1) 429 配额耗尽（如 MiniMax 5 小时限额）
        #   2) 主模型没产出有效正文（思考模式把额度烧光 / 响应格式异常）
        #      实测：pdfplumber 抽出的乱序双栏文本会让 M3.1-Flash 陷入无限
        #      思考（32000 token 仍不产出正文），此时必须切到非思考型 Agnes。
        no_usable_output = (
            TRUNCATED_MARKER in (res or "")
            or "返回空内容" in (res or "")
            or "响应格式异常" in (res or "")
        )
        if fallback["configured"] and (
            model_manager.is_quota_exhausted_error(status_code or 0, res)
            or no_usable_output
        ):
            model_manager.mark_degraded(res)
            # Seamlessly fail over to fallback!
            fb_success, fb_res, _ = _invoke_with_budget_escalation(
                fallback, messages, max_tokens, temperature, timeout, json_mode,
                max_retries=max_retries, disable_thinking=disable_thinking, budget=budget,
                deadline=deadline,
            )
            if fb_success:
                _note(fallback["model"], fell_back=True)
                return fb_res
            raise LLMError(f"降格模型 (Agnes) 调用失败: {fb_res} (原主模型报错: {res})")

        # If primary failed with non-quota error or fallback is not configured, raise
        raise LLMError(f"主模型 ({primary['model']}) 调用失败: {res}")

    # Degraded or Primary not configured: use Fallback (Agnes)
    if fallback["configured"]:
        success, res, _ = _invoke_with_budget_escalation(
            fallback, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, disable_thinking=disable_thinking, budget=budget,
            deadline=deadline,
        )
        if success:
            _note(fallback["model"], fell_back=True)
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


# ---------------------------------------------------------------------------
# Shared async HTTP client
# ---------------------------------------------------------------------------
# A fresh `httpx.AsyncClient` per call means a fresh TLS handshake per
# request and no keep-alive reuse. For a reading session that fires a
# dozen AI calls, that is a few hundred milliseconds of pure handshake
# overhead. One pooled client per event loop fixes it.
#
# httpx.AsyncClient is bound to the event loop it was created in, so the
# cache is keyed by the running loop. A client from a dead loop (e.g. one
# created inside a pytest-asyncio test) is simply dropped — httpx closes
# its pool when the object is garbage collected.
_async_client_cache: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_async_client_lock = Lock()


def _get_async_client(default_timeout: int = DEFAULT_TIMEOUT, trust_env: Optional[bool] = None):
    """Return a pooled httpx.AsyncClient for the current event loop.

    The cache is a WeakKeyDictionary keyed by the loop object itself, so a
    loop that is closed (e.g. one created by `asyncio.run()` in a test)
    drops out of the cache and both the loop and its client become
    collectable. An earlier version keyed by `id(loop)` while also
    holding a *strong* reference to the loop, which meant every
    `asyncio.run()` leaked a closed ProactorEventLoop (and its IOCP
    handle) plus an unclosed client with its connection pool — forever.

    `trust_env=False` makes httpx ignore HTTP_PROXY/HTTPS_PROXY. When the
    environment proxy turns out to be dead we flip to direct connections and
    rebuild the pooled client (the old one is closed asynchronously).
    """
    if httpx is None:
        raise LLMError("httpx 包未安装, 请先 pip install httpx")
    if trust_env is None:
        trust_env = not netutil.proxy_bypassed()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        # No running loop — caller is misusing the async API.
        raise LLMError("必须在事件循环内调用")
    with _async_client_lock:
        entry = _async_client_cache.get(loop)
        # `getattr` rather than a direct attribute access: a client object
        # supplied by a test double (or any future wrapper) may not expose
        # `is_closed`, and an AttributeError here would abort the request
        # instead of simply creating a fresh client.
        if entry is not None and not getattr(entry[0], "is_closed", False):
            client, cached_trust_env = entry
            if cached_trust_env == trust_env:
                return client
            # 代理策略变了 → 换一个 client，旧的那个异步关掉
            try:
                loop.create_task(client.aclose())
            except Exception:
                pass
        client = httpx.AsyncClient(
            timeout=default_timeout,
            trust_env=trust_env,
            limits=httpx.Limits(
                max_connections=16,
                max_keepalive_connections=8,
                keepalive_expiry=30.0,
            ),
        )
        # NOTE: the value must NOT hold a reference to the key (the loop).
        # A WeakKeyDictionary whose value strongly references its own key
        # keeps the key alive forever, so the entry would never be dropped
        # and the loop + client would leak.
        _async_client_cache[loop] = (client, trust_env)
        return client


def close_async_clients() -> None:
    """Close every pooled client. Call from the app's shutdown hook."""
    with _async_client_lock:
        _async_client_cache.clear()


async def _acquire_limiter_async(is_agnes: bool, cfg: Optional[dict] = None):
    """Await the right provider's rate limiter without blocking the loop.

    Both providers must be limited on the async path. The primary limiter
    used to be applied only on the synchronous path, so the three SSE
    endpoints could fire at MiniMax with no RPM protection at all — which
    is exactly how a per-minute cap gets hit, which used to be
    misclassified as "quota exhausted" and triggered a demotion.

    cfg 给了就按配置选限流器（自定义模型有自己的 RPM 档）；没给就走老的两档。
    """
    limiter = limiter_for_config(cfg) if cfg else (agnes_limiter if is_agnes else primary_limiter)
    await asyncio.to_thread(limiter.acquire)


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

    is_agnes = is_agnes_config(cfg)
    limiter = limiter_for_config(cfg)

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

        # Both providers are rate-limited on the async path. The primary
        # limiter used to be missing here, which is how the per-minute cap
        # got hit by the SSE endpoints.
        await _acquire_limiter_async(is_agnes)

        try:
            client = _get_async_client(timeout)
            resp = await client.post(
                url, json=payload, headers=headers, timeout=timeout
            )
            last_status = resp.status_code
        except httpx.ProxyError as e:
            # 本机配了本地代理但代理软件没运行 → 记下来，改直连重试一次
            if not netutil.proxy_bypassed():
                _mark_proxy_dead(e)
                continue
            return False, f"代理连接失败: {e}", last_status
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

            return False, f"HTTP {resp.status_code}: {_redact_secrets(err)}", resp.status_code

        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message", {}) or {}
            finish_reason = choice.get("finish_reason", "")
            content = message.get("content")
            if not isinstance(content, str):
                content = ""
            content = _strip_thinking_blocks(content)
            if not content.strip():
                # 同同步路径：思考模型可能把 max_tokens 全花在思考上，正文为空。
                reasoning = message.get("reasoning_content") or ""
                if finish_reason == "length":
                    return False, (
                        f"{TRUNCATED_MARKER}: 输出被 max_tokens 截断"
                        f"(finish_reason=length, 思考约占 {len(reasoning)} 字符), 未产出正文"
                    ), 200
                if reasoning.strip():
                    return False, (
                        f"LLM 仅返回思考过程, 无正文 (finish_reason={finish_reason!r}, "
                        f"思考约占 {len(reasoning)} 字符, max_tokens={max_tokens})"
                    ), 200
                return False, (
                    f"模型返回空内容 (finish_reason={finish_reason!r}, "
                    f"思考约占 {len(reasoning)} 字符, max_tokens={max_tokens})"
                ), 200
            if finish_reason == "length":
                # 同同步路径：正文有内容但被 max_tokens 截断 = 译文断在半截，
                # 不能当成成功。
                return False, (
                    f"{TRUNCATED_MARKER}: 输出被 max_tokens 截断"
                    f"(finish_reason=length, 正文 {len(content)} 字符, "
                    f"思考 {len(message.get('reasoning_content') or '')} 字符), "
                    f"译文不完整"
                ), 200
            return True, content, 200
        except (KeyError, IndexError, ValueError, AttributeError) as e:
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
    model_name: Optional[str] = None,
    force_fallback: bool = False,
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
    custom_cfg = resolve_model_config(model_name) if model_name else None
    is_custom = bool(custom_cfg and custom_cfg.get("is_custom"))
    # 换模型（同一套 key/base_url 下的另一个模型名）—— 同步 chat() 一直有这步，
    # 异步路径漏了 → 答疑时选了 M3.1-Flash 实际发出去的还是 M3
    # （2026-10-25 独立审查发现：用户"换模型"的诉求在答疑入口全部落空）
    if model_name and not is_custom and model_name != primary["model"]:
        primary = dict(primary, model=model_name)

    if not primary["configured"] and not fallback["configured"] and not is_custom:
        raise LLMError(
            "LLM 未配置: 请设置环境变量 LLM_API_KEY (主模型) 或 AGNES_API_KEY (备用模型). "
            "可在工作台根目录编辑 .env 文件."
        )

    # 用户选了自定义模型：先用它；连不上就自动退回主模型 → 备用模型
    if is_custom:
        if not custom_cfg.get("configured"):
            raise LLMError(
                f"自定义模型「{custom_cfg.get('label')}」没有配置 API key"
                f"（在 .env 里填 LLM_CUSTOM_N_API_KEY，或让它复用 LLM_API_KEY）"
            )
        ok, res, _ = await _invoke_model_async(
            custom_cfg, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, cancel_event=cancel_event,
        )
        if ok:
            return res
        print(f"[MicroBench] 自定义模型「{custom_cfg.get('label')}」调用失败，"
              f"自动退回默认模型链路: {res}", file=sys.stderr)

    # 强制走备用模型（用户在界面上选了 Agnes）
    if force_fallback and fallback["configured"]:
        ok, res, _ = await _invoke_model_async(
            fallback, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, cancel_event=cancel_event,
        )
        if ok:
            return res
        raise LLMError(f"备用模型调用失败: {res}")

    # Opportunistic auto-recovery probe (async — the probe is a blocking
    # requests.post with a 15s timeout; running it inline on the event
    # loop froze the whole server, including every SSE stream).
    if model_manager.is_degraded and primary["configured"] and model_manager.should_opportunistic_probe():
        try:
            await asyncio.to_thread(model_manager.probe_primary)
        except Exception:
            pass

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
    disable_thinking: bool = False,
) -> AsyncIterator[str]:
    """Yield content chunks from a single model via httpx streaming.

    Yields the `content` field from each SSE `data: {...}` line.
    Raises LLMError on non-200 status, asyncio.CancelledError on cancel.
    Does NOT retry — callers wrap this in their own retry logic if needed.

    disable_thinking=True: send thinking={"type":"disabled"} so the model
    answers directly instead of reasoning first. This is the key fix for
    "the Q&A panel never produces an answer" (see chat_stream_async):
    M3 thinks by default and reasoning tokens share the same max_tokens
    budget as the visible answer. Measured with 20K chars of context and
    max_tokens=4000, the reasoning burned the whole budget and the visible
    body was 2 characters - the user saw a spinner and then nothing.

    Models that reject the parameter (M3.1-Flash forces adaptive thinking)
    get a 400; we drop the parameter and retry once, and remember the model
    in _THINKING_DISABLE_UNSUPPORTED so we stop sending it.
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
    if disable_thinking and cfg["model"] not in _THINKING_DISABLE_UNSUPPORTED:
        payload["thinking"] = {"type": "disabled"}

    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "MicroBench-Academic-Assistant/1.2",
    }

    # Rate-limit whichever provider we are about to hit. This lives here
    # rather than at the call sites so that no streaming path can bypass
    # it — the primary (MiniMax) limiter used to be missing entirely from
    # the async/stream code, which is how the per-minute cap got hit.
    is_agnes = is_agnes_config(cfg)
    await _acquire_limiter_async(is_agnes, cfg)

    # Use the shared pooled client (per event loop) instead of building a
    # new AsyncClient per stream: a fresh client means a fresh TLS
    # handshake and no keep-alive reuse, on the one path that fires most
    # requests. The client is NOT closed here — it is shared.
    client = _get_async_client(timeout)
    # Buffer for <think> stripping. The reasoning block can be split across
    # several SSE chunks, so it cannot be filtered chunk-by-chunk — the
    # text has to be accumulated until the block closes. The old code
    # streamed the raw reasoning straight to the browser (the module
    # comment claimed the frontend filtered it, but the frontend has no
    # <think> handling at all), so every answer opened with the model's
    # internal monologue.
    think_buf = ""
    in_think = False
    try:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                err = LLMError(
                    f"HTTP {resp.status_code}: {body[:200].decode('utf-8', errors='replace')}"
                )
                # Carry the status code so callers can distinguish a
                # quota exhaustion (429) from a rate-limit (also 429 but
                # with a different body) without re-parsing the message.
                err.status_code = resp.status_code
                raise err

            body_visible_chars = 0
            async for line in resp.aiter_lines():
                if cancel_event is not None and cancel_event.is_set():
                    raise asyncio.CancelledError("client disconnected mid-stream")
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    # Flush any tail that arrived after the last </think>.
                    if not in_think and think_buf:
                        body_visible_chars += len(think_buf.strip())
                        yield think_buf
                    return
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0]["delta"].get("content", "") or ""
                    if not delta:
                        continue
                    if in_think:
                        think_buf += delta
                        if "</think>" in think_buf:
                            in_think = False
                            think_buf = think_buf.split("</think>", 1)[1]
                            if think_buf:
                                body_visible_chars += len(think_buf.strip())
                                yield think_buf
                                think_buf = ""
                        continue
                    if "<think>" in delta:
                        before, _, rest = delta.partition("<think>")
                        if before:
                            body_visible_chars += len(before.strip())
                            yield before
                        in_think = True
                        think_buf = rest
                        if "</think>" in think_buf:
                            in_think = False
                            think_buf = think_buf.split("</think>", 1)[1]
                            if think_buf:
                                body_visible_chars += len(think_buf.strip())
                                yield think_buf
                                think_buf = ""
                        continue
                    body_visible_chars += len(delta.strip())
                    yield delta
                except (json.JSONDecodeError, KeyError, IndexError):
                    # Skip malformed lines (heartbeats, comments)
                    continue

            # The loop ended without a "[DONE]" sentinel. Measured against the
            # real API: when the reasoning eats the whole max_tokens budget,
            # the upstream emits finish_reason="length" and then just closes
            # the connection. That path produced an empty body which the
            # frontend rendered as a blank answer box - the user saw
            # "no answer at all" with no error and no clue why.
            if body_visible_chars == 0:
                raise LLMError(
                    f"{TRUNCATED_MARKER}: {TRUNCATED_NO_BODY}"
                    f"(stream ended with no visible content, "
                    f"max_tokens={max_tokens})"
                )
    except asyncio.CancelledError:
        raise
    except LLMError:
        # 本函数自己抛出的 LLMError（例如上游返回 HTTP 429）必须**原样**上抛。
        # 以前这里没有这个分支 → 下面那个 `except Exception` 会把它再包一层，
        # 用户看到的是「流式响应异常: LLMError: HTTP 429: {...}」：
        # 一层无意义的"流式响应异常"前缀 + 一层重复的 "LLMError:" 类名。
        raise
    except httpx.ConnectError as e:
        raise LLMError(f"网络连接失败: {e}")
    except httpx.TimeoutException as e:
        raise LLMError(f"请求超时: {e}")
    except httpx.HTTPError as e:
        # Every other httpx transport/protocol failure (ReadError,
        # RemoteProtocolError, WriteError, CloseError, ProxyError …)
        # is a subclass of httpx.HTTPError but NOT of ConnectError or
        # TimeoutException, so it used to escape as a raw httpx
        # exception. The SSE wrapper only catches llm.LLMError, so the
        # stream aborted with no terminal event — and the frontend
        # treats a stream that ends without [DONE] as a *successful*
        # answer. The user therefore saw a half-written answer
        # rendered as a complete, confident one. Wrapping it as an
        # LLMError makes the frontend show the error instead.
        raise LLMError(f"上游连接中断: {type(e).__name__}: {e}")
    except Exception as e:
        raise LLMError(f"流式响应异常: {type(e).__name__}: {e}")


async def chat_stream_async(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    cancel_event: Optional[asyncio.Event] = None,
    disable_thinking: bool = False,
    model_name: Optional[str] = None,
    force_fallback: bool = False,
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

    Empty-body recovery (the "no answer" bug):
      MiniMax-M3 reasons by default, and the reasoning tokens share the same
      max_tokens budget as the visible answer. With a large context the
      reasoning can consume the entire budget, leaving a 2-character body.
      When that happens this function retries the call once with thinking
      disabled, so the user always gets an answer. Nothing was streamed at
      that point, so the retry is seamless.

    disable_thinking=True skips the reasoning phase from the start (used by
    the translation path, where reasoning only wastes latency).

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
    custom_cfg = resolve_model_config(model_name) if model_name else None
    is_custom = bool(custom_cfg and custom_cfg.get("is_custom"))
    # 换模型：与同步 chat() / chat_async() 保持一致
    # （漏了这步 → 答疑时选了别的模型，实际发出去的仍是主模型，
    #   而界面徽标还显示"用户选的那个" → 骗人）
    if model_name and not is_custom and model_name != primary["model"]:
        primary = dict(primary, model=model_name)

    if not primary["configured"] and not fallback["configured"] and not is_custom:
        raise LLMError(
            "LLM 未配置: 请设置环境变量 LLM_API_KEY 或 AGNES_API_KEY."
        )

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError("client disconnected before stream start")

    # 用户选了自定义模型：先用它；连不上就自动退回主模型 → 备用模型
    if is_custom:
        if not custom_cfg.get("configured"):
            raise LLMError(
                f"自定义模型「{custom_cfg.get('label')}」没有配置 API key"
                f"（在 .env 里填 LLM_CUSTOM_N_API_KEY，或让它复用 LLM_API_KEY）"
            )
        emitted = False
        try:
            async for chunk in _stream_model(
                custom_cfg, messages, max_tokens, temperature, timeout, cancel_event,
                disable_thinking=disable_thinking,
            ):
                if chunk.strip():
                    emitted = True
                yield chunk
            return
        except asyncio.CancelledError:
            raise
        except LLMError as e:
            if emitted:
                # 已经吐了一半内容 → 不能换模型重来（会出现两段答案粘在一起）
                raise
            print(f"[MicroBench] 自定义模型「{custom_cfg.get('label')}」流式调用失败，"
                  f"自动退回默认模型链路: {e}", file=sys.stderr)
            # 继续往下走主/备链路

    # 强制走备用模型（用户在界面上选了 Agnes 答疑）
    if force_fallback and fallback["configured"]:
        async for chunk in _stream_model(
            fallback, messages, max_tokens, temperature, timeout, cancel_event,
            disable_thinking=disable_thinking,
        ):
            yield chunk
        return

    # Opportunistic auto-recovery probe. Without this, the three SSE
    # reader panels could never leave the degraded (Agnes) state on their
    # own: only a *sync* llm.chat() call triggered the probe, so a user
    # who only ever used the reader panels stayed on the weaker 20 RPM
    # fallback for the rest of the session even after the 5-hour token
    # plan reset. The probe is a blocking requests.post, so it must run
    # in a worker thread.
    if (
        model_manager.is_degraded
        and primary["configured"]
        and model_manager.should_opportunistic_probe()
    ):
        try:
            await asyncio.to_thread(model_manager.probe_primary)
        except Exception:
            pass

    if not model_manager.is_degraded and primary["configured"]:
        # Track whether anything was already streamed. A partial answer
        # cannot be replayed, so once the primary model has produced
        # output we must NOT fall back to Agnes: the user would otherwise
        # see the primary's half-answer glued onto the fallback's complete
        # answer ("PART-A PART-B FALLBACK-1 FALLBACK-2"), which reads as one
        # confident nonsense answer.
        # "emitted" must mean "the user can see real content", not "some
        # bytes were emitted". The upstream's empty body is exactly two
        # newlines; counting those as output made the recovery branch below
        # bail out with "already streamed" and the user got a blank answer.
        emitted = False
        try:
            async for chunk in _stream_model(
                primary, messages, max_tokens, temperature, timeout, cancel_event,
                disable_thinking=disable_thinking,
            ):
                if chunk.strip():
                    emitted = True
                yield chunk
            return
        except asyncio.CancelledError:
            raise
        except LLMError as e:
            err_msg = str(e)
            if emitted:
                # Already streamed partial content — fail fast, keep the
                # partial output the user already has.
                raise
            # Empty body: the model burned the whole max_tokens budget on
            # reasoning and never produced a visible answer. Retrying with
            # thinking disabled makes it answer directly (measured: same
            # 20K context, max_tokens=4000 -> 14398 chars of reasoning and a
            # 2-char body with thinking on, vs a clean answer with it off).
            # Nothing was streamed yet, so the retry is seamless.
            if (
                not disable_thinking
                and TRUNCATED_MARKER in err_msg
                and TRUNCATED_NO_BODY in err_msg
            ):
                async for chunk in _stream_model(
                    primary, messages, max_tokens, temperature, timeout,
                    cancel_event, disable_thinking=True,
                ):
                    yield chunk
                return
            status = getattr(e, "status_code", None)
            quota_hit = (
                model_manager.is_quota_exhausted_error(status, err_msg)
                if status is not None
                else ("429" in err_msg or "quota" in err_msg.lower())
            )
            if fallback["configured"] and quota_hit:
                model_manager.mark_degraded(err_msg)
            else:
                raise

    if fallback["configured"]:
        # The fallback's rate limiting happens inside _stream_model(), so
        # it is not repeated here.
        async for chunk in _stream_model(
            fallback, messages, max_tokens, temperature, timeout, cancel_event,
            disable_thinking=disable_thinking,
        ):
            yield chunk
        return

    raise LLMError("无可用的大模型配置")


_THINKING_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINKING_OPEN = "<think>"
_THINKING_CLOSE = "</think>"


def _strip_thinking_blocks(text: str) -> str:
    """Remove <think>...</think> reasoning blocks from reasoning-model output.

    Also handles the *unterminated* case, which is the dangerous one: when the
    response is cut off (finish_reason=length) inside the reasoning, the model
    never emits the closing tag, so the regex below matches nothing and the raw
    English reasoning would be served to the user as if it were the answer —
    which is exactly what "已翻译但内容是乱的" looked like.

    A stray closing tag without an opening one is dropped as well.
    """
    if not text:
        return text
    cleaned = _THINKING_RE.sub("", text)

    # Unterminated block: everything from the last opening tag to the end is
    # reasoning, not an answer.
    idx = cleaned.rfind(_THINKING_OPEN)
    if idx != -1:
        cleaned = cleaned[:idx]

    # Orphan closing tag (some servers put the answer *after* </think>).
    if cleaned.lstrip().startswith(_THINKING_CLOSE):
        cleaned = cleaned.lstrip()[len(_THINKING_CLOSE):]

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
            max_tokens=4000,  # 思考模式开启时，思考+输出共享该预算
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
        # LLMError 要人话化（上游 429/401…）；别的异常保持原样。
        # ⚠️ 这里把错误**吞掉**了（返回 partial），所以外层 app.py 的
        # `except llm.LLMError` 根本看不到它 —— 人话化必须在这里做。
        detail = friendly_error_text(e) if isinstance(e, LLMError) else _redact_secrets(str(e))
        return {
            "tldr": f"({title[:30]}... — LLM 未配置或调用失败: {detail[:80]})",
            "key_points": [
                "⚠️ 请检查 .env 中的 LLM_API_KEY 或 AGNES_API_KEY 配置",
                f"原文摘要 ({len(meta.get('abstract') or '')} 字) 可手动阅读",
            ],
            "takeaway": "提示: 降级到 Agnes 3.0 Flash 后，在 20 RPM 限制下可稳定完成自动摘要。",
            "error": detail,
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