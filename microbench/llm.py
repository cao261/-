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
import inspect
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

# ---------------------------------------------------------------------------
# DeepSeek（内置第三方 provider）
# ---------------------------------------------------------------------------
# 为什么单独做内置 provider，而不是塞进 .env 的 LLM_CUSTOM_N_* 槽：
#   - DeepSeek 是**独立网关**（自己的 base_url + 自己的 key），
#     MiniMax / Agnes 两个网关的 /v1/models 里都没有它（实测 2026-10-26）；
#   - 用户原话："mini Max 不够用，然后 Agnes 又太慢了，我就换 deep seek"
#     → 这是一个**日常要手动切**的选项，必须开箱即用，不能要求先手编 .env；
#   - 它有**视觉/无视觉之分**，而表格抄录全靠多模态读图
#     （无视觉模型会"自信地编造"整张表，见 VISION_CAPABLE_MODELS 的说明）。
#
# 模型 id 用 DeepSeek 官方的名字（deepseek-flash / deepseek-v4-pro），
# 与前端下拉框里的 id 一致，方便排查。
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"

# DeepSeek 官方 API 当前暴露的模型（2026-10-26 实测 GET /v1/models）：
#   deepseek-flash   —— 支持图片输入（视觉）
#   deepseek-v4-pro  —— **不支持**视觉
# ⚠️ 官方文档确认："deepseek-v4-pro does not support vision"，
#    "deepseek-flash accepts JPEG/PNG/GIF/WebP as image_url blocks"。
#
# ⚠️⚠️ 视觉差别不是"能力差一点"，是"会造假"：
#   实测（2026-10-26，JACERS 综述第 2 页生产坐标裁图）：
#     deepseek-flash  → 抄出 −90｜−175｜−109｜｜~700｜16.5，与论文印刷值一致
#     deepseek-v4-pro → **完全编造了另一张表**（石英/钛酸钡/PZT-5H…），
#                       数值没有一个对得上，但格式完全像张真表
#   → 无视觉模型**绝不能**接抄表任务，否则用户会拿到一份"看着像真的"错表。
#     这跟 §4.6「错公式比没公式危险得多」是同一条红线。
#
# 抄表路径因此必须做能力检查：模型不支持视觉 → 换用支持视觉的模型，
# 而不是把编造的表格拼进译文。
VISION_CAPABLE_MODELS: set[str] = {
    "MiniMax-M3",
    "MiniMax-M3.1-Flash",
    "MiniMax-M2.7",
    "agnes-3.0-flash",
    "deepseek-flash",
}

# 无视觉模型 → 抄表时的替补（按顺序找第一个支持视觉的）。
# 选 deepseek-flash：它是唯一实测能正确读表格的第三方模型，且比 Agnes 快
# （实测 1.9s vs Agnes 25~35s）。
VISION_FALLBACK_CHAIN: tuple[str, ...] = (
    "deepseek-flash",
    "MiniMax-M3",
    "agnes-3.0-flash",
)

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

# 「这次没产出正文」导致的**短 TTL 降格**时长（秒）。
# 为什么是短 TTL 而不是 0 或 900：
#   - 0（不降格）：主模型每次都会被重试 2 次（escalation + 抬预算重试），
#     每次白烧一次思考额度（独立审查实测：连续 3 次调用，主模型每次 2 次）；
#   - 900（通用降格）：一次任务的毛病让后面 15 分钟全走备用模型，太重。
# 60 秒刚好：这一波请求走备用模型，下一波自动回主模型。
NO_OUTPUT_DEGRADE_TTL_SECONDS = 60

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


# ---------------------------------------------------------------------------
# thinking 参数：**按模型分别**决定能不能发（2026-10-26 实测）
# ---------------------------------------------------------------------------
# ⚠️ 不能一刀切。实测三个模型对 thinking={"type":"disabled"} 的反应**完全
# 不同**，而且其中两种是**静默失败**（HTTP 200、看起来成功、内容是空的）：
#
#   模型                     传 disabled                  不传
#   ------------------------------------------------------------------
#   MiniMax-M3               7.6s / 2033字 ✅ 干净        32.5s / 混入思考文本
#   MiniMax-M3.1-Flash       **HTTP 400 直接拒**          正常（强制思考）
#   Agnes 3.0 Flash          **98.6s / 正文 0 字** ❌      27.9s / 2092字 ✅
#   DeepSeek Flash           3.9s / 906字 ✅ 干净          20.9s / **正文 0 字** ❌
#
# 两个必须记住的坑：
#   1. **Agnes 传 disabled 会静默吃光预算**：不报错、返回 200，但
#      reasoning_content 有 9072 字符、content 一字没有、max_tokens 4000
#      全被思考吃掉 → 用户等 98 秒拿到空白。原代码对所有模型统一发这个
#      参数，所以**一旦选 Agnes 翻译就会白等**（2026-10-26 实测发现）。
#   2. **DeepSeek 不传反而空**：它默认开着思考（reasoning 8293 字符、
#      content 0 字）→ DeepSeek **必须**传 disabled。
#
# 判据按 **provider** 走（不看模型名），因为模型名会变、provider 不会。

# 这些模型**不能**接受 thinking 参数（发了会被拒或吞掉预算）：
#   - MiniMax-M3.1-Flash：强制 adaptive thinking，传 disabled 直接 400
#   - Agnes（整个 provider）：传了会静默把预算全花在思考上 → 正文为空
_THINKING_PARAM_REJECTED_PROVIDERS: tuple[str, ...] = ("agnes",)


def cfg_accepts_thinking_param(cfg: dict) -> bool:
    """这个配置能不能接受 thinking={"type":"disabled"} 参数？

    不能的：
      - MiniMax-M3.1-Flash（强制 adaptive thinking）→ 直接 400
      - Agnes 整个 provider → **静默**把 max_tokens 预算全花在思考上，
        正文一字不出（实测 98.6s / 0 字）。这是最阴险的一种：不报错，
        用户只看到"翻译完但什么都没有"。

    判据走 provider 而不是模型名（模型名会变，provider 不会）。
    """
    if not cfg:
        return True
    model = (cfg.get("model") or "").strip()
    if model in _THINKING_FORCED_MODELS or model in _THINKING_DISABLE_UNSUPPORTED:
        return False
    base = (cfg.get("base_url") or "").lower()
    if any(p in base or p in model.lower() for p in _THINKING_PARAM_REJECTED_PROVIDERS):
        return False
    return True


def should_send_thinking_disabled(cfg: dict, disable_thinking: bool) -> bool:
    """最终判断：这次请求要不要带 thinking={"type":"disabled"}。

    disable_thinking=False → 永远不发（要思考时用）。
    disable_thinking=True  → 看这个 provider 吃不吃这套参数。
    """
    if not disable_thinking:
        return False
    return cfg_accepts_thinking_param(cfg)


# ---------------------------------------------------------------------------
# 思考强度：reasoning_effort（2026-10-26 实测）
# ---------------------------------------------------------------------------
# 用户诉求："思考型模型思考要三十秒到九十秒……你把它给设定成 low，
#            思考强度设定的低一点。Max token 可以设定高一点，
#            反正就保证他都能正常运行就行。"
#
# ⚠️ 之前只有"开/关"两档（thinking.type=disabled / 不传），
#    强制思考模型（M3.1-Flash）被卡在中间：**关不掉**，只能全开 →
#    思考烧光预算 → 正文 0 字。
#
# 实测（MiniMax M3.1-Flash 的 400 报错原文，2026-10-26）：
#   'model "MiniMax-M3.1-Flash" requires adaptive thinking;
#    reasoning effort none is not allowed (2013)'
#   'thinking.type="disabled" (including reasoning.effort=none) is not allowed'
# → 上游**认 reasoning_effort 这个参数**，只是 `none` 被拒。
#   `low` / `minimal` 都**通过校验**（实测返 429 = 过了参数校验，只是额度用完）。
#
# 跨模型实测 reasoning_effort 的效果（DeepSeek Flash，同一问题）：
#   不传      → 正文 68 字 / 思考 73 字 / 1.9s
#   low       → 正文 70 字 / 思考 157 字 / 2.0s   ← 不降思考量！
#   minimal   → 正文 95 字 / 思考 238 字 / 2.1s   ← 反而更多！
#   none      → 正文 65 字 / 思考   9 字 / 0.9s   ← 真的关掉了
# → **low 并不等于"思考更少"**，只有 none 是"完全关"。
#   对强制思考模型，none 不可用 → 能做的只是"给足预算"，
#   让思考吃完还有余额留给正文（见 app.QA_MAX_TOKENS）。
#
# 所以本模块提供的是**能力判定**（这个模型/网关能不能吃这个参数），
# 具体值由调用方决定；判定错了会 400，所以一律先问 provider。
#
# 这些 provider 实测**接受** reasoning_effort：
#   - DeepSeek（api.deepseek.com）：none 有效（思考 73 → 9 字）
#   - Agnes（apihub.agnes-ai.com）：none 有效
#   - MiniMax（api.minimaxi.com）：low/minimal 通过校验；none 被拒
#
# ⚠ 2026-10-27 审计对齐（平板端那边已经改对了）：
#   判据必须是**精确主机名**，不能用子串匹配。
#   原因：用户可以在 .env 里把自己的接口指向**任意** OpenAI 兼容网关，
#   那种网关上照样挂着「MiniMax-M3.1-Flash」这个模型名，
#   但它**不保证**转发 reasoning_effort → 发了就 **HTTP 400（请求直接被拒）**。
#   子串匹配还会把 `https://minimax.中转.example.com/v1` 这种
#   **中转站**地址误判成官方网关。
#   → 网关才是决定「认不认这个参数」的东西，模型名不是。
#   （平板端 tablet/app.js 的 acceptsReasoningEffort() 是同一套判据。）
_REASONING_EFFORT_OK_HOSTS: tuple[str, ...] = (
    "api.minimaxi.com",
    "api.minimax.io",
    "api.deepseek.com",
    "apihub.agnes-ai.com",
)


def _base_hostname(cfg: dict) -> str:
    """取 base_url 的主机名（去协议 / userinfo / 端口 / 路径），全小写。

    要处理的几种写法（用户可能这么填 .env）：
        https://api.deepseek.com/v1        → api.deepseek.com
        https://api.deepseek.com:443/v1    → api.deepseek.com
        https://u:p@api.deepseek.com/v1     → api.deepseek.com
        api.deepseek.com/v1                → api.deepseek.com   （无协议）
        api.deepseek.com                   → api.deepseek.com
    """
    raw = str((cfg or {}).get("base_url") or "").strip().lower()
    if not raw:
        return ""
    host = raw
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", host)
    if m:
        host = m.group(1)
    else:
        # 无协议：先切掉路径（"/" 之后），再切掉 query/fragment
        host = re.split(r"[/?#]", host, maxsplit=1)[0]
    host = host.split("@")[-1]     # 去掉 userinfo（https://user:pass@host）
    host = host.split(":")[0]      # 去掉端口
    return host.strip()


# 强制思考模型**不能**用 none（会 400）。
def cfg_accepts_reasoning_effort(cfg: dict) -> bool:
    """这个配置能不能接受 reasoning_effort 参数？

    判错的后果是 **HTTP 400（请求直接被拒，不是静默失败）**，
    所以判定必须保守：不确定就不发。

    判据 = **网关主机名在白名单里**（不看模型名 —— 见上面的说明）。
    """
    if not cfg:
        return False
    return _base_hostname(cfg) in _REASONING_EFFORT_OK_HOSTS


def reasoning_effort_for(cfg: dict, level: str = "low") -> Optional[str]:
    """算出这次请求该带的 reasoning_effort 值（None = 不带）。

    level: "low"（默认）/ "minimal" / "none"
    - 强制思考模型：none 会被 400 → 强制降级到 "low"。
    - 不支持该参数的 provider：返回 None（干脆不发）。
    """
    if not cfg_accepts_reasoning_effort(cfg):
        return None
    lv = (level or "low").strip().lower()
    if lv not in ("low", "minimal", "none"):
        lv = "low"
    if lv == "none" and model_forces_thinking(cfg.get("model") or ""):
        # 强制思考模型：none = 关思考 = 400
        lv = "low"
    return lv

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
        #
        # ⚠️ 这里要分**三态**，不是两态（2026-10-27 审计 L-9）：
        #   True  = 确认额度耗尽
        #   False = 确认**不是**额度耗尽（是每分钟限流）
        #   None  = **判定失败/未知**（探测本身抛异常）
        # 旧代码把 except 归到 False → 无论真相如何都说成「请求太频繁」，
        # 于是「额度用完」被说成「等几十秒就好」→ 用户一直干等；
        # 这与分类文案契约（quota / rate_limit / no_output 三类）相悖。
        try:
            is_quota = model_manager.is_quota_exhausted_error(429, quota_probe_text or "")
        except Exception:
            is_quota = None      # 未知 ≠ 没耗尽
        if is_quota is True:
            return (
                "模型额度用完了（HTTP 429 · 配额耗尽）：套餐/余额额度已用尽，"
                "现在重试也一样。等额度恢复再试，或在 .env 里配一个你自己的模型。"
            )
        if is_quota is False:
            return (
                "请求太频繁（HTTP 429 · 限流）：刚发出的请求超过了该模型的每分钟上限。"
                "等几十秒再试，或换一个模型/拉长间隔。"
            )
        # 判定失败：不能替上游下结论（也不能说"没耗尽"）。
        return (
            "模型返回 HTTP 429，但暂时分不清是配额耗尽还是每分钟请求数超限"
            "（额度判定失败）。先等几十秒再试；若反复出现，请检查 .env 里的"
            "模型额度与同时进行的请求数。"
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
    # ⚠️ 内部标记必须先拦下来，不能甩给用户（2026-10-26）。
    # 空正文（模型只吐思考、正文一个字没出）用户看到的是：
    #   "__TRUNCATED_BY_MAX_TOKENS__: 未产出正文(stream ended with no
    #    visible content, max_tokens=100)"
    # —— 一堆内部标记 + 英文，用户看不懂，只会觉得"程序坏了"。
    if TRUNCATED_NO_BODY in msg:
        return (
            "模型这次没有产出任何正文（只输出了思考过程，正文为空）。"
            "换个模型再试，或把问题问得更具体一点。"
        )
    if TRUNCATED_BODY_CUT in msg:
        return (
            "模型输出被长度限制截断了，回答不完整。"
            "换个模型，或缩小范围（少看上下文）再试。"
        )
    m = _HTTP_CODE_RE.search(msg)
    if not m:
        # ⚠️ 没有 HTTP 码的异常（网络/协议/URL 解析错误）**也必须脱敏+抹路径**：
        #   实测 `https://user:sk-xxxx@gw.internal` 这种带凭据的 base_url
        #   在 URL 畸形时会连着 key 一起进异常消息，原样送到浏览器 = 泄密；
        #   OSError 的消息里还带着本机绝对路径。
        #   （安全审计 H2 / M2）
        return _mask_paths(_redact_secrets(msg))[:300]
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
    # ⚠️ prefix 也要脱敏+抹路径：它来自异常消息，可能含带凭据的 URL 或本机路径。
    prefix = _mask_paths(_redact_secrets(prefix))
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
        # 下一个允许发车的时刻（预占制）。
        #
        # 为什么需要它：min_interval 的间隔必须按**预占**算，不能按"上次
        # 实际调用时间"算 —— 后者只在 sleep 结束后才更新，两个线程同时
        # 进来时会用同一个旧基准，算出同一个 planned → 一起发车，
        # 并发下限流形同虚设（app._prepare_tables 就是 gather 并发读表）。
        self._next_allowed: float = 0.0

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
        `_next_allowed` (= planned + min_interval) is advanced before the
        lock is released, so a second thread arriving during the sleep
        waits its own full interval instead of piggy-backing on the first
        thread's slot.

        ⚠️ 间隔判据必须读**预留时间**（`_last_planned_time`），不能读
        **实际触发时间**（`_last_call_time`）——
        `_last_call_time` 是 sleep **之后**才更新的，N 个并发调用者会
        全部看到「上次还没触发」→ 全部 sleep 同一个 min_interval →
        **齐射**（实测 5 线程 min_interval=6.0：
        gaps=['6.00','0.00','0.00','0.00']）→ 打到上游每分钟上限 →
        被误判成「配额耗尽」→ mark_degraded 全局降格 15 分钟
        （用户额度其实还有）。
        """
        with self._lock:
            now = time.time()
            # Clean expired timestamps outside the rolling window
            cutoff = now - self.window_seconds
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            wait_time = 0.0

            # Rule 1: Min interval spacing
            #
            # ⚠ **两个基准都要看**（本机审计轮 + 对方审计轮各修了一半，合起来取并集）：
            #   - `_last_planned_time`：上一次**预占**的时刻（可能在未来）
            #   - `_last_call_time`：上一次**真的**发车了多久之前
            #   - `_next_allowed`：上一次**预占**的下一个可用时刻
            #
            # 只看「实际触发时间」会有并发漏洞（`_last_call_time` 只在 sleep
            # **之后**才更新 → N 个并发调用者算出同一个 planned → **齐射**；
            # 实测 5 线程 min_interval=6.0：gaps=['6.00','0.00','0.00','0.00']
            # → 打到上游每分钟上限 → 被误判成「配额耗尽」→ 全局降格 15 分钟）。
            # 只看「预留时间」则会在**欠账补睡**（见函数末尾）之后算短一点。
            # → 两个基准都算，**取更严的那个**。
            since_planned = now - self._last_planned_time
            since_call = now - self._last_call_time
            since_last = min(since_planned, since_call)
            if since_last < self.min_interval:
                wait_time = max(wait_time, self.min_interval - since_last)
            if self._next_allowed - now > wait_time:
                wait_time = self._next_allowed - now

            # Rule 2: Sliding window request cap
            if len(self._timestamps) >= self.max_requests:
                oldest_in_window = self._timestamps[0]
                window_wait = (oldest_in_window + self.window_seconds) - now
                if window_wait > wait_time:
                    wait_time = window_wait

            # 预占本轮的槽位（并把"下一个可用时刻"一起推进），然后放锁去睡。
            planned = now + wait_time
            self._timestamps.append(planned)
            self._last_planned_time = planned
            self._next_allowed = planned + self.min_interval

        if wait_time > 0:
            time.sleep(wait_time)

        # Record the moment the call actually happened (after the sleep).
        #
        # ⚠️ 线程可能被调度延迟唤醒 → **实际**间隔可能小于 min_interval
        #    （A 睡过头、B 按时醒 → 两者贴在一起发出去）。限流器宁可慢，
        #    也不能齐射 → 不够就补睡，欠的账在这里还。
        extra_waited = 0.0
        while True:
            with self._lock:
                fired = time.time()
                gap = (fired - self._last_call_time) if self._last_call_time \
                    else self.min_interval
                if gap >= self.min_interval:
                    self._last_call_time = fired
                    self._actual_times.append(fired)
                    cutoff = fired - self.window_seconds
                    self._actual_times = [t for t in self._actual_times if t > cutoff]
                    break
                extra = self.min_interval - gap
            time.sleep(extra)
            extra_waited += extra
        return wait_time + extra_waited

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


# ---------------------------------------------------------------------------
# DeepSeek provider（内置，独立网关）
# ---------------------------------------------------------------------------

# DeepSeek 的模型清单（2026-10-26 实测 GET https://api.deepseek.com/v1/models）
DEEPSEEK_MODELS: tuple[dict, ...] = (
    {
        "id": "deepseek-flash",
        "label": "DeepSeek V4.1 Flash",
        "hint": "支持读图（表格/公式）· 快 · 独立于 MiniMax 额度",
    },
    {
        "id": "deepseek-v4-pro",
        "label": "DeepSeek V4 Pro",
        "hint": "推理更强 · 不支持读图（表格会自动换用 DeepSeek Flash）",
    },
)


def deepseek_model_configs() -> list[dict]:
    """DeepSeek 内置 provider 的模型配置。

    key 从 DEEPSEEK_API_KEY 读；留空则复用 LLM_API_KEY（自建网关同 key 的情形）。
    没配 key → configured=False → 界面上标注"⚠ 没配 API key"，不静默失败。
    """
    api_key = (os.getenv("DEEPSEEK_API_KEY", "") or "").strip() \
        or (os.getenv("LLM_API_KEY", "") or "").strip()
    base_url = (os.getenv("DEEPSEEK_BASE_URL", "") or "").strip().rstrip("/") \
        or DEFAULT_DEEPSEEK_BASE_URL
    default_model = (os.getenv("DEEPSEEK_MODEL", "") or "").strip() \
        or DEFAULT_DEEPSEEK_MODEL
    try:
        rpm = int((os.getenv("DEEPSEEK_RPM", "") or "").strip() or DEFAULT_CUSTOM_RPM)
    except ValueError:
        rpm = DEFAULT_CUSTOM_RPM
    rpm = max(1, min(rpm, 120))
    min_interval = max(1.0, round(60.0 / rpm * 0.9, 2)) \
        if rpm != DEFAULT_CUSTOM_RPM else DEFAULT_CUSTOM_MIN_INTERVAL

    out: list[dict] = []
    for m in DEEPSEEK_MODELS:
        # DEEPSEEK_MODEL 指定的模型排在最前（用户想固定用哪个）
        is_default = m["id"] == default_model
        out.append({
            "id": m["id"],
            "label": m["label"] + ("（默认）" if is_default else ""),
            "hint": m["hint"],
            "api_key": api_key,
            "base_url": base_url,
            "model": m["id"],
            "rpm": rpm,
            "min_interval": min_interval,
            "configured": bool(api_key),
            "is_fallback": False,
            # 走**自己的** base_url/key → 与自定义模型同一条代码路径
            # （先试它，失败自动退回主模型 → 备用模型）。
            "is_custom": True,
            "is_deepseek": True,
            "supports_vision": m["id"] in VISION_CAPABLE_MODELS,
        })
    return out


def model_supports_vision(model_id: str) -> bool:
    """这个模型能不能读图（表格/公式抄录靠它）。

    判据刻意**保守**：只认白名单里的模型。
    未知模型（用户自己配的）按"不支持"处理 → 抄表会自动换用已验证的视觉模型。
    理由：让用户读到"表格未能读出"的提示，远好过让用户读到一份编造的表格。
    """
    return (model_id or "").strip() in VISION_CAPABLE_MODELS


def vision_fallback_model() -> Optional[str]:
    """找一个已验证支持视觉的模型（抄表读图失败时的替补）。

    按 VISION_FALLBACK_CHAIN 顺序取第一个**当前能用**的。
    """
    for mid in VISION_FALLBACK_CHAIN:
        if mid in VISION_CAPABLE_MODELS:
            return mid
    return None


def resolve_model_config(model_id: str) -> Optional[dict]:
    """按模型 id 找配置。

    - ""            → None（跟随默认设置）
    - "agnes"       → 备用模型配置（Agnes）
    - "custom:xxx"  → 自定义模型配置
    - "deepseek-*"  → DeepSeek 内置 provider 的模型
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
    for cfg in deepseek_model_configs():
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
    # ⚠️ 必须 (?i)：RFC 7235 里 header 名大小写不敏感，
    #    上游报错常小写回显 "authorization: bearer <key>" → 大小写敏感会漏。
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}"), 1),
    # ⚠️ Basic 也必须排在"关键词+分隔符"那条**前面**：
    #    否则 authorization 那条会先把 "Basic" 当成值替换掉，
    #    后面的 base64 载荷就漏了（实测）。
    (re.compile(r"(?i)\b(basic\s+)[A-Za-z0-9+/=]{8,}"), 1),
    # 关键词 + 分隔符（允许中间有引号/空白）+ 值。
    # 覆盖 api_key / apiKey / api-key / api_secret / access_token /
    #     refresh_token / client_secret / private_key / token / secret /
    #     password / authorization / cookie。
    # ⚠️ 旧版只有一条 `(?:api[_-]?key|token|secret|password)\s*[=:]\s*`，
    #    JSON 形态 `{"api_key":"<hex>"}` 因关键词和分隔符之间隔了一个引号
    #    而不匹配 → key 明文进 logs/desktop.log（安全审计实测 24 种形态）。
    (re.compile(
        r"""(?ix)
        ( ["']? \b (?:
              api[_-]?keys? | apikey | api[_-]?secrets?
            | access[_-]?tokens? | refresh[_-]?tokens? | id[_-]?tokens?
            | client[_-]?secrets? | private[_-]?keys?
            | tokens? | secrets? | passwords? | passwd
            | authorization | cookie
        ) \b ["']? \s* [:=] \s* )
        ( ["']? )                      # 值的可选引号
        ( [^"',;\s}\]]+ )              # 值本身
        """
    ), 1),
    # URL 里的 userinfo：scheme://user:pass@host
    (re.compile(r"(://[^:/\s@]+:)[^@/\s]+(@)"), 1),
    # 厂商 key 前缀（本项目主要用 sk-，这些是顺手补的通用性）
    (re.compile(r"\b(?:gh[pousr]_|AKIA|AIza|xox[baprs]-|sk_live_|sk_test_)"
                r"[A-Za-z0-9_\-]{8,}"), None),
    # JWT（三段式）
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), None),
    # PEM 私钥块
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?"
                r"-----END [A-Z ]*PRIVATE KEY-----"), None),
)


def _mask_paths(text: str) -> str:
    """抹掉文本里的本机绝对路径（内部别名，见 mask_paths）。"""
    return mask_paths(text)


def mask_paths(text: str) -> str:
    """公开入口：抹掉本机绝对路径（app.py 等模块要用，别跨模块调私有名）。

    OSError / FileNotFoundError 的消息里带着本机绝对路径
    （'E:\\zaiwu\\个人工作台\\logs\\xxx.json'），原样送到浏览器
    = 内部路径泄露（安全审计 H2 / M2）。
    """
    if not text:
        return text
    # ⚠️ Windows 路径要按"路径段"匹配，不能用 `[^\s'"]+`：
    #    带空格的路径（C:\Program Files (x86)\...\msedge.exe）
    #    会在第一个空格处停下 → 只遮一半，后半截照样泄露（独立审查实测）。
    out = re.sub(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]*",
                 "<本机路径>", text)
    # POSIX 绝对路径（/opt/... /home/...）
    # ⚠️ 不能吃掉 URL 里的路径部分：`https://example.com/a/b/c` 里
    #    `://` 之后的那段会被当绝对路径抹掉 → 整个 URL 变成 "https:<本机路径>"
    #    （独立审查 M-3 实测）。所以要求斜杠前面不是 ":" 的一部分。
    out = re.sub(r"(?<![\w<:/])(/(?:[^\s'\"]+/)+[^\s'\"]*)", r"<本机路径>", out)
    return out


def _redact_secrets(text: str) -> str:
    """Strip credential-shaped substrings from text that will be displayed."""
    return redact_secrets(text)


def redact_secrets(text: str) -> str:
    """公开入口：脱敏（app.py 等模块要用，别跨模块调私有名）。"""
    if not text:
        return text
    out = text
    for pat, keep_group in _SECRET_PATTERNS:
        if keep_group is None:
            out = pat.sub("***", out)
        else:
            out = pat.sub(lambda m, g=keep_group: m.group(g) + "***", out)
    # URL 里的 userinfo 单独再处理一次（上面那条只覆盖"带密码"的形态；
    # `http://user@host` 这种没有密码的也会暴露用户名）
    out = re.sub(r"(://[^:/\s@]+)@", r"\1:***@", out)
    return out


def _safe_err(text: object) -> str:
    """错误文本的**统一出口**：脱敏（凭据）+ 抹本机路径。

    同步链早就这么做了（friendly_error_text / probe_primary），
    流式链原来没有 —— 上游报错里的 base_url userinfo / Bearer /
    厂商 key 前缀会跟着错误消息进日志、进 SSE 事件
    （2026-10-27 审计 L-7）。这里统一一个出口，别再各写各的。
    """
    return _mask_paths(_redact_secrets(str(text)))


def _degrade_text(kind: str) -> str:
    """降格原因的人话说明（给界面用）。

    ⚠️ 以前界面上写死了「主模型额度用完了」，但降格有三种完全不同的原因，
    处置方式也完全不同 —— 说错会让用户白等或白折腾：
      quota      → 5 小时滚动窗口的额度真的用完了，要等窗口恢复
      rate_limit → 每分钟请求数超限，等几十秒就好（不该说成额度用完）
      no_output  → 主模型连上了但这次没吐出正文，跟额度没关系

    注：目前**只有 quota 会被真正 mark_degraded** ——
    限流和"没出正文"都只做本次 failover，不改全局状态
    （见 chat() / chat_stream_async()）。这两个分支是给"将来若放宽
    降格条件"预留的，文案必须站得住脚。

    ⚠️ 别给限流建议"调大 LLM_TRANSLATION_DEADLINE" —— 那是**单次请求时限**，
    跟每分钟请求数（限流）完全是两回事，调它没用（本轮自己写错又改掉的）。
    """
    return {
        "quota": "主模型配额用完了（5 小时滚动窗口），要等窗口恢复；"
                 "期间先用备用模型顶着用。",
        "rate_limit": "主模型被限流了（每分钟请求数超限），等几十秒就好；"
                      "如果频繁出现，请减少同时进行的翻译/答疑数量，"
                      "或换一个限流更宽松的模型。",
        "no_output": "主模型这次没吐出正文（思考把 token 预算烧光了），"
                     "已临时改用备用模型；这跟额度无关。",
    }.get(kind, "")


def _safe_base_url(url: str) -> str:
    """Return a URL with any embedded userinfo (user:password@) removed."""
    if not url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        if not parts.hostname:
            # ⚠ 没有 hostname 也不能原样返回 —— 那条 URL 可能长成
            #   "https://user:sk-xxx@"，凭据就在里面。
            return redact_secrets(url)
        netloc = parts.hostname
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        # ⚠⚠ 这里原来是 `return url` —— 兜底分支等于「把秘密原样吐回去」。
        #
        # 什么时候会走到这？urlsplit 本身不抛，但 `parts.port` 在**端口不是
        # 数字**时会抛 ValueError（"Port could not be cast to integer"）；
        # IPv6 括号不配对同样会炸。而「端口写错」恰恰是用户手写 base_url 时
        # 最常见的错误 —— 于是畸形 URL 正好命中这条兜底。
        #
        # 实测（2026-10-26 审计）：
        #   _safe_base_url("https://user:sk-SECRET@gw.internal:notaport/v1")
        #   → 旧写法原样返回 → 凭据进 /api/llm/status 响应体。
        #
        # 修法：兜底也走脱敏，绝不原样返回。
        return redact_secrets(url)


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
        self.degraded_kind: str = ""   # quota / rate_limit / no_output
        self.degraded_until: float = 0.0   # 短 TTL 降格的到期时间（0 = 不自动恢复）
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

    def mark_degraded(self, reason: str, sticky: bool = False, kind: str = "",
                       ttl_seconds: float = 0.0):
        """
        标记主模型降格。

        kind：降格的**分类**（界面要照它说人话，不能一律说"额度用完了"）：
          "quota"      → 主模型配额/额度真的用完了（5 小时滚动窗口），
                         要等窗口恢复；用户需要知道"等多久"
          "rate_limit" → 主模型被限流了（每分钟请求数超限），等几十秒就好
          "no_output"  → 主模型连上了但没吐出正文（思考烧光预算等），
                         这是**这次任务**的问题，不代表额度没了

        ⚠️ 不分类的后果（真实踩过）：界面 toast 写死了"主模型额度用完了"，
        而真实原因可能是限流或空正文 → 用户一看自己的额度明明还有，
        只会觉得"这程序在瞎说"。

        ttl_seconds：**短 TTL 降格**（默认 0 = 不自动恢复）。
        用途：no_output 这类"这次任务的问题"不该永久压着主模型
        （那会让后续每个请求都白烧一次主模型调用），
        但也完全不该永久降格（15 分钟）→ 压 60 秒刚好。
        """
        with self._lock:
            self.is_degraded = True
            self.degraded_reason = reason
            self.degraded_kind = kind
            self.degraded_until = (time.time() + ttl_seconds) if ttl_seconds else 0.0
            self.degraded_at = time.time()
            self.last_primary_probe = time.time()
            self.sticky_degraded = sticky
            print(
                f"[MicroBench] ⚠️ 主模型触发降格 ({kind or 'unknown'}): "
                f"{_redact_secrets(str(reason))[:300]}. "
                f"已自动切换至备用模型 (Agnes 3.0 Flash, 20 RPM 保护)."
                + (f" [{int(ttl_seconds)}s 后自动恢复]" if ttl_seconds else "")
                + (" [需手动探测升格]" if sticky else ""),
                file=sys.stderr,
            )

    def _ttl_expired(self) -> bool:
        """短 TTL 降格是否已到期（不持锁调用，避免死锁）。"""
        with self._lock:
            if not self.is_degraded or not self.degraded_until:
                return False
            return time.time() >= self.degraded_until

    def mark_recovered(self):
        with self._lock:
            self.is_degraded = False
            self.degraded_reason = ""
            # ⚠️ kind 也要清：否则 is_degraded=False 但 status 仍返回
            #    degraded_kind='quota' + degraded_text="主模型配额用完了…"
            #    → 任何不同时检查 is_degraded 的客户端都会显示"额度用完了"
            #    （独立审查 I-3）。
            self.degraded_kind = ""
            self.degraded_until = 0.0
            self.sticky_degraded = False
            self.last_primary_probe = time.time()
            print(
                "[MicroBench] 🚀 主模型额度已恢复正常，系统已自动升格回主模型 (MiniMax)!",
                file=sys.stderr,
            )

    def should_opportunistic_probe(self) -> bool:
        """Return True if degraded and probe cooldown interval has elapsed."""
        # 短 TTL 降格到期 → 自动恢复（不用等 15 分钟的通用探针）
        if self._ttl_expired():
            self.mark_recovered()
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
            # 只有**明确无歧义**的"额度真的用完了"短语才能盖过限流判定。
            #
            # ⚠ 2026-10-27 修正：原来用 quota_signals 整体回查，于是
            #   "429 billing tier rate limit exceeded" → 命中 "billing"
            #   → 被当成配额耗尽 → 全局降格 15 分钟。
            #   用户看到的是"我额度明明还有，怎么一直用备用模型"
            #   （AGENTS.md 里那类"写死的错误提示比没有提示更糟"）。
            #
            #   所以这里只认硬信号；"billing" / "quota" / "exhausted"
            #   这些**歧义词不算** —— 上游既会用它们表示计费档位限流，
            #   也会用 "resource exhausted" 表示并发限流。
            hard_quota_signals = (
                "token plan",          # MiniMax: "Token Plan limit exceeded"
                "2056",                # MiniMax 配额错误码
                "额度", "套餐", "充值", "余额", "欠费",
                "quota exceeded",
                "insufficient balance",
                "insufficient_quota",
                "out of credit",
                "credit balance",
                "resource exhausted",  # Google 风格的配额耗尽
                "daily limit",
            )
            if not any(sig in text for sig in hard_quota_signals):
                # 纯限流：等几十秒就恢复，**不能**触发 15 分钟的全局降格
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
            # ⚠⚠ requests 的 InvalidURL / InvalidSchema 异常消息里含**完整 URL**
            #   （实测："Failed to parse: https://user:sk-xxx@gw.internal:notaport/
            #   v1/chat/completions"），而这条 message 由 /api/llm/probe
            #   **原样返回浏览器** → 点一次「探测升格」就把 base_url 里的
            #   凭据送到页面上。
            #
            #   同步调用路径（llm.chat → friendly_error_text）是脱敏的，
            #   探针这条分支漏了。这里补上，行为与同步路径对齐。
            msg = _mask_paths(_redact_secrets(str(e)))
            return {"success": False, "message": f"探针网络请求异常: {msg}"}

    def get_status(self) -> dict:
        # 短 TTL 到期 → 顺手恢复，别让 API 继续返回陈旧的降格状态
        # （徽标会一直显示"无正文"，独立审查 M-5）。
        if self._ttl_expired():
            self.mark_recovered()
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
            # 降格**分类** + **人话原因**：界面必须照它说，
            # 不能一律说"额度用完了"（真实反馈：用户额度明明还有，
            # 却被告知额度用完，只会觉得这程序在瞎说）。
            "degraded_kind": self.degraded_kind,
            "degraded_text": _degrade_text(self.degraded_kind),
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
            # DeepSeek（内置 provider，独立网关 + 独立额度）
            "deepseek_models": [
                {
                    "id": c["id"],
                    "label": c["label"],
                    "model": c["model"],
                    "base_url": _safe_base_url(c["base_url"]),
                    "configured": c["configured"],
                    "rpm": c["rpm"],
                    "min_interval": c["min_interval"],
                    "supports_vision": c.get("supports_vision", False),
                    "rate_limit": limiter_for_config(c).stats(),
                }
                for c in deepseek_model_configs()
            ],
        }


# Global model manager instance
model_manager = DualModelManager()


def _config() -> dict:
    """
    Read current effective LLM config.
    Returns dict with keys: api_key, base_url, model, configured (bool), is_fallback (bool)

    ⚠ 不要在这里调 `model_manager.get_status()`：它是给界面用的**全量**
    状态（两组模型列表 + 每模型 limiter.stats()），而本函数每个请求都要走
    一次。原来这里有一行 `st = get_status()`，结果从未被使用 —— 纯浪费。
    """
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
    # 用 _config() 而不是 get_status()：后者是给界面看的**全量**状态
    # （两组模型列表 + 每个 limiter 的统计），而这个函数在**每个翻译请求**
    # 里都要调一次（当缓存键用），白构建一份没人看的字典。
    # 两者在降格时的取值是一致的（都是当前生效模型的 model 名）。
    return _config().get("model") or "default"


# ---------------------------------------------------------------------------
# 答疑（划词解答）专用的模型 —— 与翻译**分开**
# ---------------------------------------------------------------------------
# 用户诉求（2026-10-26）：
#   "翻译用 M3（不开思考）… 解答问题就用 MiniMax 的 3.1 Flash 版本，
#    或者也能换 deep seek V4.1 flash"→ 两者要**各自独立**切换。
#
# 为什么要分开（不是重复造轮子）：
#   翻译的诉求 = 快 + 完整 → M3，关思考（实测 8.4s / 2918 字）
#   答疑的诉求 = 会推导   → M3.1-Flash，**必须开着思考**（强制思考型，
#     传 thinking.type=disabled 直接 400）
# 共用一个下拉框 → 想换翻译模型就必须连带换掉答疑模型，反之亦然。

DEFAULT_QA_MODEL = "MiniMax-M3.1-Flash"


def qa_model_name() -> str:
    """答疑任务专用的模型名（空串 = 跟随主模型）。

    默认 MiniMax-M3.1-Flash：答疑要的是"会推导"，而它是**强制思考**型
    （翻译场景下它是灾难：整页 41.7s、推理吃光预算导致正文被截断）。
    """
    return (os.getenv("LLM_QA_MODEL", "") or "").strip()


def qa_active_model() -> str:
    """答疑实际会用到的模型名（给界面徽标/下拉框"当前"标记用）。"""
    q = qa_model_name()
    if q:
        return q
    primary = model_manager.get_primary_config().get("model") or ""
    # 主模型不是强制思考型时，答疑默认用 M3.1-Flash（更会推导）
    if primary and primary != DEFAULT_QA_MODEL:
        return DEFAULT_QA_MODEL
    return primary or DEFAULT_QA_MODEL


def qa_disable_thinking() -> bool:
    """答疑要不要**主动**关思考模式。

    判据：只有"明确知道支持关思考"的模型才主动关。

    - MiniMax-M3 / M2.7 / DeepSeek Flash：支持关 → 关掉更省时（实测
      DeepSeek Flash 关思考后 reasoning_content 长度归 0）。
    - MiniMax-M3.1-Flash：**强制思考**，传 disabled 直接 400 → 不传。
      （即使误传了，_invoke_model 也会在 400 时自动去掉参数重试一次，
        并把这个模型记进 _THINKING_DISABLE_UNSUPPORTED —— 但那是"撞了
        再退"，每个新进程都要白撞一次，所以这里直接不传。）
    - Agnes：非思考型，关不关无所谓；不传最安全。
    - 未知模型：不传（宁可慢，不要 400）。

    ⚠️ 答疑**默认是要思考的**（"会推导"）—— 这是与翻译最大的区别。
    """
    return False


def model_prefers_no_thinking(model_id: str) -> bool:
    """这个模型适合**主动**关掉思考模式吗（用于答疑/直译这类"要正文不要推导"的场景）。"""
    return (model_id or "").strip() in (
        "MiniMax-M3",
        "MiniMax-M2.7",
        "deepseek-flash",
    )


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

def _err_text(resp) -> str:
    """从错误响应里取出「人话」错误文本（保证不抛、保证返回 str）。

    上游的错误体形状并不统一：

        {"error": {"message": "..."}}   ← OpenAI / MiniMax 标准形状
        {"error": {"detail": "..."}}
        {"error": "some string"}        ← 一些网关直接给字符串
        {"error": null}
        <html>...</html>                ← 代理 / WAF 错误页

    原来两处都写成 `resp.json().get("error", {}).get("message", ...)`：
    后三种会让 `.get` 抛 **AttributeError**，而 except 只捕
    (ValueError, KeyError) → 异常从 `chat()` 里逃出去变成 500，
    `friendly_error_text()` 根本没机会解释一句人话。
    """
    fallback = (getattr(resp, "text", "") or "")[:200]
    try:
        body = resp.json()
    except Exception:
        return fallback
    if not isinstance(body, dict):
        return fallback

    err = body.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "type"):
            val = err.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return fallback
    if isinstance(err, str) and err.strip():
        return err
    # 有些上游把错误直接放在顶层
    for key in ("message", "detail"):
        val = body.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return fallback


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
    if should_send_thinking_disabled(cfg, disable_thinking):
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
            return False, f"代理连接失败: {_redact_secrets(str(e))}", last_status
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
            return False, f"网络连接失败 (重试 {max_retries} 次): {_redact_secrets(str(e))}", last_status
        except requests.RequestException as e:
            return False, f"请求异常: {_redact_secrets(str(e))}", last_status

        if resp.status_code != 200:
            # 统一解析：上游可能给 dict / 字符串 / null / HTML，
            # 原来只捕 (ValueError, KeyError) 会让 AttributeError 逃出 chat()
            err = _err_text(resp)

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
            return False, f"LLM 响应格式异常: {_safe_err(e)}; raw={_safe_err(resp.text[:200])}", 200

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
    # ⚠️ "agnes" 是**备用模型**的别名，不是"主模型网关下的另一个模型名"。
    #   把它当成模型名 → primary 被改成 model="agnes" → 拿着 MiniMax 的 key
    #   去打 "agnes" → HTTP 400 invalid params, unknown model 'agnes'
    #   （2026-10-26 实测：用户选「Agnes 3.0 Flash」翻译时必现）
    #   → 命中 fallback 配置时，必须走 force_fallback 链路，不能改 primary。
    is_fallback_alias = bool(
        custom_cfg and custom_cfg.get("is_fallback")
    )
    if (model_name and not is_custom and not is_fallback_alias
            and model_name != primary["model"]):
        primary = dict(primary, model=model_name)
    fallback = model_manager.get_fallback_config()

    if not primary["configured"] and not fallback["configured"] and not is_custom:
        raise LLMError(
            "LLM 未配置: 请设置环境变量 LLM_API_KEY (主模型) 或 AGNES_API_KEY (备用模型). "
            "可在工作台根目录编辑 .env 文件."
        )

    # 强制走备用模型：直接用 fallback 配置，跳过主模型与降级探测
    if force_fallback or is_fallback_alias:
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
            # ⚠ 必须**抬高**预算，绝不能砍。
            #
            # max_tokens 是「思考 + 正文」共用的池子：思考吃光就没正文。
            # 这里原来写死 `min(max_tokens * 2, 4096)` —— 而翻译路径会传
            # 16000~32000（app._translation_max_tokens）→ 重试反而把预算
            # **砍到 4096**，必然再空一次，白等一整轮（120~200 秒），
            # 用户看到的就是"翻译卡了两分钟然后失败"。
            # 与 _invoke_with_budget_escalation（:1772）和流式路径（:2863）
            # 对齐：下限 MIN_REASONABLE_MAX_TOKENS，上限 MAX_ESCALATION_MAX_TOKENS。
            bigger = min(max(max_tokens * 2, MIN_REASONABLE_MAX_TOKENS),
                         MAX_ESCALATION_MAX_TOKENS)
            # 已经到顶就别重试了 —— 同样的预算只会得到同样的空正文
            if bigger > max_tokens:
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
        # ⚠️ 只有**真的配额耗尽**才改全局状态（mark_degraded）。
        #    "这次没产出正文"是**单次任务**的问题（思考烧光预算等），
        #    不该让后面 15 分钟所有请求都改走备用模型 ——
        #    用户会看到"降格"却不知道自己额度明明还有（真实反馈）。
        #    这种情况下只做本次 failover（换备用模型把这一单做完），
        #    全局状态不动。
        is_quota = model_manager.is_quota_exhausted_error(status_code or 0, res)
        if fallback["configured"] and is_quota:
            model_manager.mark_degraded(res, kind="quota")
        elif fallback["configured"] and no_usable_output:
            # ⚠️ 空正文是**这次任务**的问题（思考烧光预算等），
            #    不该永久压着主模型（15 分钟太重），但也完全不该不降格 ——
            #    不降格的话主模型每次都会被重试 2 次（escalation + 抬预算），
            #    每次白烧一次思考额度（独立审查实测量化过）。
            #    → 短 TTL 降格：压 60 秒，之后自动恢复。
            model_manager.mark_degraded(res, kind="no_output",
                                       ttl_seconds=NO_OUTPUT_DEGRADE_TTL_SECONDS)
        if fallback["configured"] and (is_quota or no_usable_output):
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


def chat_isolated(messages: list[dict], max_tokens: int = DEFAULT_MAX_TOKENS,
                  temperature: float = DEFAULT_TEMPERATURE,
                  timeout: int = DEFAULT_TIMEOUT,
                  disable_thinking: bool = False,
                  model_name: Optional[str] = None) -> str:
    """**不改变全局降格状态**的单次调用（只打主模型）。

    为什么需要（2026-10-25）：「符号认字」是**后台补齐**任务，
    它的失败是**这一小任务**的问题 —— 但走 chat() 时，"没产出正文"
    会触发 60 秒的全局降格（mark_degraded kind=no_output），
    于是用户的**翻译**也被迫改走备用模型 60 秒。
    → 一个后台小任务不该连累用户正在做的事。

    与 chat() 的差别：
      - 只调主模型，**不** failover 到 Agnes（Agnes 不支持关思考，
        认字这种"看图说符号"的活它做不好，实测答非所问）
      - **不**改任何全局状态（不 mark_degraded、不升格）
      - 失败就抛异常，调用方自己决定怎么降级
    """
    if requests is None:
        raise LLMError("requests 包未安装, 无法调用模型")
    primary = model_manager.get_primary_config()
    if model_name:
        cfg = resolve_model_config(model_name)
        if cfg and cfg.get("configured"):
            primary = dict(primary, model=model_name)
    if not primary.get("configured"):
        raise LLMError("主模型未配置 API Key")
    success, res, _status = _invoke_model(
        primary, messages, max_tokens, temperature, timeout, False,
        max_retries=1, disable_thinking=disable_thinking,
        budget=[2], deadline=None,
    )
    if success:
        return res
    raise LLMError(f"主模型 ({primary['model']}) 调用失败: {res}")


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


def _accepts_timeout(func) -> bool:
    """这个 client.stream 收不收 timeout 形参？

    httpx 收；但老的/自造的 client 替身（测试里的 _Client.stream）可能
    只有 (method, url, json, headers) → 硬传 timeout 会 TypeError。
    探测一下再传，不改调用形态。
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return "timeout" in params


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

    ⚠️ timeout 也是缓存的一部分（2026-10-27 审计 M-12）：池化客户端的
    timeout 会被**首次创建者**冻结 —— 第一次 timeout=60，之后所有调用方
    都拿到 60s（大请求被提前掐断）。现在缓存里记着本次的 timeout，
    变了就就地更新 `client.timeout`（连接池继续复用），并且每次请求
    还会**显式**传 timeout（见 _invoke_model_async / _stream_model）。
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
            client, cached_trust_env, cached_timeout = entry
            if cached_trust_env == trust_env:
                if cached_timeout != default_timeout:
                    # ⚠️ 池化客户端的 timeout 会被**首次调用冻结**：
                    #    实测 _get_async_client(timeout=60) 之后再要
                    #    timeout=300，拿到的仍是 Timeout(60) → 长请求被
                    #    提前掐断（用户看到"莫名其妙超时"）。
                    #    这里就地更新默认 timeout（连接池继续复用，不重建），
                    #    而且每次请求还会**显式**传 timeout
                    #    （见 _invoke_model_async / _stream_model）→
                    #    并发不同 timeout 的请求互不干扰。
                    try:
                        client.timeout = httpx.Timeout(default_timeout)
                    except Exception:
                        pass
                    _async_client_cache[loop] = (client, trust_env, default_timeout)
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
        _async_client_cache[loop] = (client, trust_env, default_timeout)
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
    disable_thinking: bool = False,
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

    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if should_send_thinking_disabled(cfg, disable_thinking):
        payload["thinking"] = {"type": "disabled"}
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
        # ⚠️ 必须把 cfg 传下去：自定义模型（.env 的 LLM_CUSTOM_N_RPM）
        #    有自己的限流档，不传就按主模型档限流 → 它自己的 RPM 上限
        #    形同虚设。三条链路的写法要统一：
        #      同步 _invoke_model  → limiter_for_config(cfg)
        #      异步 _invoke_model_async → _acquire_limiter_async(is_agnes, cfg)
        #      流式 _stream_model      → _acquire_limiter_async(is_agnes, cfg)
        await _acquire_limiter_async(is_agnes, cfg)

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
            return False, f"代理连接失败: {_redact_secrets(str(e))}", last_status
        except RETRYABLE_EXC as e:
            last_err = str(e)
            if _cancelled():
                raise asyncio.CancelledError("client disconnected during retry") from e
            if attempt < max_retries:
                wait = 2 ** attempt
                await asyncio.sleep(wait)
                continue
            return False, f"网络连接失败 (重试 {max_retries} 次): {_redact_secrets(str(e))}", last_status
        except asyncio.CancelledError:
            # Propagate cleanly without retrying
            raise
        except Exception as e:
            # Non-retryable exception
            return False, f"请求异常: {_redact_secrets(str(e))}", last_status

        if resp.status_code != 200:
            # 统一解析：上游可能给 dict / 字符串 / null / HTML，
            # 原来只捕 (ValueError, KeyError) 会让 AttributeError 逃出 chat()
            err = _err_text(resp)

            last_err = err

            # 模型不支持 thinking 参数（例如 M3.1-Flash 强制 adaptive thinking）：
            # 去掉该参数重试一次，别直接失败。顺便把这个模型记下来：之后
            # 直接不再发该参数，省掉每个分块一次 400 + 重试的往返。
            # （同步路径 _invoke_model 里早有这段；异步路径原来漏了。）
            if resp.status_code == 400 and "thinking" in payload and "thinking" in err.lower():
                payload.pop("thinking", None)
                disable_thinking = False
                _THINKING_DISABLE_UNSUPPORTED.add(cfg["model"])
                if attempt < max_retries:
                    continue

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
            return False, f"LLM 响应格式异常: {_safe_err(e)}; raw={_safe_err(resp.text[:200])}", 200

    return False, last_err, last_status


async def chat_async(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    json_mode: bool = False,
    max_retries: int = 2,
    cancel_event: Optional[asyncio.Event] = None,
    disable_thinking: bool = False,
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
    # 异步路径漏了 → 答疑时选了 M3.1-Flash 实际发出去的���是 M3
    # （2026-10-25 独立审查发现：用户"换模型"的诉求在答疑入口全部落空）
    # ⚠️ "agnes" 是**备用模型**的别名，不是模型名 —— 见 _apply_model_alias 的说明。
    is_fallback_alias = bool(custom_cfg and custom_cfg.get("is_fallback"))
    if (model_name and not is_custom and not is_fallback_alias
            and model_name != primary["model"]):
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
            disable_thinking=disable_thinking,
        )
        if ok:
            return res
        print(f"[MicroBench] 自定义模型「{custom_cfg.get('label')}」调用失败，"
              f"自动退回默认模型链路: {res}", file=sys.stderr)

    # 强制走备用模型（用户在界面上选了 Agnes）
    # ⚠️ is_fallback_alias：model_name 直接传 "agnes" 时也要走这里
    #   （否则会被当成"主模型网关下的模型名"→ 拿着 MiniMax 的 key 打 agnes
    #    → HTTP 400 unknown model 'agnes'，2026-10-26 实测）
    if (force_fallback or is_fallback_alias) and fallback["configured"]:
        ok, res, _ = await _invoke_model_async(
            fallback, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, cancel_event=cancel_event,
            disable_thinking=disable_thinking,
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
            disable_thinking=disable_thinking,
        )
        if success:
            return res

        if fallback["configured"] and model_manager.is_quota_exhausted_error(status_code or 0, res):
            model_manager.mark_degraded(res, kind="quota")
            fb_success, fb_res, _ = await _invoke_model_async(
                fallback, messages, max_tokens, temperature, timeout, json_mode,
                max_retries=max_retries, cancel_event=cancel_event,
            disable_thinking=disable_thinking,
            )
            if fb_success:
                return fb_res
            raise LLMError(f"降格模型 (Agnes) 调用失败: {fb_res} (原主模型报错: {res})")

        raise LLMError(f"主模型 ({primary['model']}) 调用失败: {res}")

    if fallback["configured"]:
        success, res, _ = await _invoke_model_async(
            fallback, messages, max_tokens, temperature, timeout, json_mode,
            max_retries=max_retries, cancel_event=cancel_event,
            disable_thinking=disable_thinking,
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
    reasoning_effort: Optional[str] = None,
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

    reasoning_effort: "low" / "minimal" / "none" —— 思考强度（2026-10-26）。
    强制思考模型不接受 none（400），reasoning_effort_for() 会自动降级成 low。
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
    if should_send_thinking_disabled(cfg, disable_thinking):
        payload["thinking"] = {"type": "disabled"}
    # 思考强度（2026-10-26）："low" / "minimal"。
    # ⚠️ 强制思考模型（M3.1-Flash）**不能**用 none（实测 400：
    # "reasoning effort none is not allowed"）→ reasoning_effort_for 会挡住。
    if reasoning_effort:
        effort = reasoning_effort_for(cfg, reasoning_effort)
        if effort:
            payload["reasoning_effort"] = effort

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
    # ⚠️ 显式传本次 timeout（2026-10-27 审计 M-12）：
    #    池化客户端的默认 timeout 是**首次调用**冻结的，不显式传就会
    #    沿用别人的档（大请求被提前掐断 → 前端拿到半截答案）。
    #    read 给 3 倍：思考型模型首字节可能要 41-100s
    #    （M3.1-Flash 实测），read 跟 timeout 一样会把流砍在半路。
    stream_kwargs = {"json": payload, "headers": headers}
    if _accepts_timeout(client.stream):
        stream_kwargs["timeout"] = httpx.Timeout(timeout, read=timeout * 3)
    try:
        async with client.stream("POST", url, **stream_kwargs) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                # ⚠️ 上游报文必须脱敏：里面可能带 base_url 的 userinfo、
                #    Bearer、厂商 key 前缀（同步链早就在这里脱敏了，
                #    流式链原来没有 → 凭据跟着错误消息进日志/事件）。
                err = LLMError(
                    f"HTTP {resp.status_code}: "
                    f"{_safe_err(body[:200].decode('utf-8', errors='replace'))}"
                )
                # Carry the status code so callers can distinguish a
                # quota exhaustion (429) from a rate-limit (also 429 but
                # with a different body) without re-parsing the message.
                err.status_code = resp.status_code
                # ⚠️ 模型不支持 thinking 参数（例如 M3.1-Flash 强制 adaptive
                # thinking）→ 这里**不**就地重试：流式的 <think> 剥离需要
                # 缓冲器，就地重试会绕过它、让思考文本漏给浏览器。
                # 改由 chat_stream_async 层重试（它会先剥掉 thinking 再发）。
                # 已知模型由 should_send_thinking_disabled 预先拦住，
                # 这道只兜底未知模型。
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
                    # ⚠️ 这里**必须 break 而不是 return** —— 直接 return 会跳过
                    #    循环之后的空正文守卫，于是「上游正常发了 [DONE] 但正文
                    #    一个字都没吐」的情况被当成成功：前端收到 [DONE]、
                    #    渲染出一个空答案，AI 活动面板还显示「✅ 完成」。
                    #    （实测 2026-10-26：M3.1-Flash 答「帮我到网上找实物图」
                    #     思考 43 秒后只发思考、不发正文 → 用户看到的就是
                    #     「显示在回答、实际没有回答」。）
                    #    break 让流程落到循环后的守卫，两种断流方式一起兜住。
                    break
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
        raise LLMError(f"网络连接失败: {_safe_err(e)}")
    except httpx.TimeoutException as e:
        raise LLMError(f"请求超时: {_safe_err(e)}")
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
        raise LLMError(f"上游连接中断: {type(e).__name__}: {_safe_err(e)}")
    except Exception as e:
        raise LLMError(f"流式响应异常: {type(e).__name__}: {_safe_err(e)}")


async def chat_stream_async(
    messages: list[dict],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: int = DEFAULT_TIMEOUT,
    cancel_event: Optional[asyncio.Event] = None,
    disable_thinking: bool = False,
    model_name: Optional[str] = None,
    force_fallback: bool = False,
    reasoning_effort: Optional[str] = None,
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
    # ⚠️ "agnes" 是**备用模型**的别名，不是模型名 —— 见 _apply_model_alias 的说明。
    is_fallback_alias = bool(custom_cfg and custom_cfg.get("is_fallback"))
    if (model_name and not is_custom and not is_fallback_alias
            and model_name != primary["model"]):
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
                reasoning_effort=reasoning_effort,
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
                  f"自动退回默认模型链路: {_safe_err(e)}", file=sys.stderr)
            # 继续往下走主/备链路

    # 强制走备用模型（用户在界面上选了 Agnes 答疑）
    # ⚠️ is_fallback_alias：model_name 直接传 "agnes" 时也要走这里
    if (force_fallback or is_fallback_alias) and fallback["configured"]:
        async for chunk in _stream_model(
            fallback, messages, max_tokens, temperature, timeout, cancel_event,
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
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
                reasoning_effort=reasoning_effort,
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
            #
            # ⚠️ 但对**强制思考模型**（M3.1-Flash）这个重试是**空操作**：
            #    `should_send_thinking_disabled` 对它返回 False → 参数根本
            #    不会下发 → 发出去的请求与第一次**完全相同** → 再等 55 秒，
            #    还是 0 字。实测（2026-10-26）：空正文后重试把 55 秒变成
            #    111 秒，然后才报错 —— 用户白等一倍时间。
            #    → 这种模型要重试的话，必须**抬高 max_tokens**
            #      （思考与正文共用预算池，给足就能出正文）。
            if (
                not disable_thinking
                and TRUNCATED_MARKER in err_msg
                and TRUNCATED_NO_BODY in err_msg
            ):
                if should_send_thinking_disabled(primary, True):
                    # 这条重试真的会让请求不同（关掉思考）→ 值得重试。
                    async for chunk in _stream_model(
                        primary, messages, max_tokens, temperature, timeout,
                        cancel_event, disable_thinking=True,
                        reasoning_effort=reasoning_effort,
                    ):
                        yield chunk
                    return
                # 强制思考模型：关不掉思考 → 只能**加预算**再试一次。
                # 抬到 MAX_ESCALATION_MAX_TOKENS 为止，再失败就报错。
                bigger = min(max(max_tokens * 4, MIN_REASONABLE_MAX_TOKENS),
                              MAX_ESCALATION_MAX_TOKENS)
                if bigger > max_tokens:
                    async for chunk in _stream_model(
                        primary, messages, bigger, temperature, timeout,
                        cancel_event, disable_thinking=False,
                        reasoning_effort=reasoning_effort,
                    ):
                        yield chunk
                    return
            status = getattr(e, "status_code", None)
            # 模型不接受 thinking 参数 → 去掉它重试一次（还没吐任何内容，
            # 重试是无缝的）。已知模型由 should_send_thinking_disabled 预先
            # 拦住，这条是给未知模型兜底的。
            if (status == 400 and disable_thinking
                    and "thinking" in err_msg.lower()):
                _THINKING_DISABLE_UNSUPPORTED.add(primary.get("model") or "")
                async for chunk in _stream_model(
                    primary, messages, max_tokens, temperature, timeout,
                    cancel_event, disable_thinking=False,
                    reasoning_effort=reasoning_effort,
                ):
                    yield chunk
                return
            # ⚠️ status 为 None 表示这不是 HTTP 状态码错误（网络中断、协议错误、
            #    URL 解析失败……）—— 那种情况**不能**当成配额耗尽。
            #    旧的宽松分支（"429" in err_msg）会把这类错误也算成配额耗尽
            #    → 永久降格 15 分钟 → 用户看到"降格"却不知道自己额度还有。
            #    真正的 429 一定有 status_code（_stream_model 显式设了），
            #    走 is_quota_exhausted_error 的精确判定（限流 vs 配额分开）。
            quota_hit = (
                model_manager.is_quota_exhausted_error(status, err_msg)
                if status is not None
                else False
            )
            if fallback["configured"] and quota_hit:
                model_manager.mark_degraded(err_msg, kind="quota")
            else:
                raise

    if fallback["configured"]:
        # The fallback's rate limiting happens inside _stream_model(), so
        # it is not repeated here.
        async for chunk in _stream_model(
            fallback, messages, max_tokens, temperature, timeout, cancel_event,
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
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
        detail = friendly_error_text(e) if isinstance(e, LLMError) else _mask_paths(_redact_secrets(str(e)))
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