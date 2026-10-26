# -*- coding: utf-8 -*-
"""平板端的思考预算 / 思考强度 / 重答守卫护栏（2026-10-27）。

**为什么要有这份文件**

电脑端在 2026-10-26 修了一轮「答疑只思考不回答」（AGENTS.md §4.37~4.39），
但平板端是**另一份前端**（`microbench/static/tablet/app.js`，直连模型、
不走后端提示词）→ 电脑端的修复**一行都不会**自动到平板。

上游那次 commit（2216afa）只在平板补了一个提示词（`SYS_RESEARCH`），
其余三个根因在平板端**原样还在**：

1. **思考预算按「思考 + 正文」算**（§4.38）
   平板答疑原来给 `max_tokens: 8000` → M3.1-Flash 强制思考、
   思考 token 与正文共用预算池 → 思考吃光预算 → **正文 0 字**。
   电脑端实测：4000 → 0 字；16000 → 4884 字。
2. **空正文要自己救一次**（§4.38 连带修）
   平板原来只弹一句「换个模型或调大 max_tokens」就结束 —— 用户什么都没等到。
   → 必须自动抬预算重试；且对**强制思考**模型不能走「关思考」那条路
   （那是空操作：参数根本不下发 → 请求与第一次完全相同 → 再等 55 秒还是 0 字）。
3. **重答不死锁**（§4.37）
   平板原来没有请求序号守卫 → 被顶掉的旧请求收尾时会把
   **新请求正在用的停止按钮**藏掉，还会把旧答案塞进多轮上下文。

本文件把这些钉死 —— 平板前端回退了，pytest 立刻红。
（行为级断言在 `microbench/_test_tablet_ai.js`，由 `tests/test_tablet_frontend.py`
挂进 pytest；这里是**源码级**断言，两层都红才算数。）
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TABLET = REPO / "microbench" / "static" / "tablet" / "app.js"


@pytest.fixture(scope="module")
def src() -> str:
    return TABLET.read_text(encoding="utf-8")


def _const(src: str, name: str) -> int:
    """取出一个数值常量的字面值（找不到就报错，不要静默跳过）。"""
    m = re.search(rf"const {name}\s*=\s*(\d+)", src)
    assert m, f"平板端没有常量 {name}"
    return int(m.group(1))


# ---------------------------------------------------------------------------
# 1) 思考预算：按「思考 + 正文」算（§4.38）
# ---------------------------------------------------------------------------

def test_qa_budget_is_enough_for_thinking_models(src: str):
    """答疑的 max_tokens 必须够思考型模型用。

    M3.1-Flash 强制思考，思考 token 与正文**共用**同一个 max_tokens 预算池。
    电脑端实测（7K 上下文 + 生产提示词 + 真实模型）：

        max_tokens=4000  → 59.9s / 正文 **0 字**
        max_tokens=16000 → 86.2s / 正文 **4884 字** ✅
    """
    assert _const(src, "AI_QA_MAX_TOKENS") >= 16000, \
        f"平板答疑预算 {_const(src, 'AI_QA_MAX_TOKENS')} 不够思考型模型用（实测需要 >=16000）"


def test_translate_budget_is_not_copied_from_qa(src: str):
    """翻译按「正文」算，不能照抄答疑的数。

    ⚠ 同一个常量在不同任务里的**含义不同** —— 翻译是「关思考的直译型模型」
    （4000 够用），答疑是「思考型模型」（必须按思考+正文算）。
    """
    tr = _const(src, "AI_TRANSLATE_MAX_TOKENS")
    qa = _const(src, "AI_QA_MAX_TOKENS")
    assert tr == 4000, f"翻译预算 {tr} 不对（直译型模型按正文算，给 4000）"
    assert tr < qa, "翻译预算不该等于答疑预算（两种模型的算账方式不同）"


def test_escalation_cap_exists(src: str):
    """抬预算重试要有上限 —— 再大也没意义（32000 也可能不产出正文）。"""
    cap = _const(src, "AI_MAX_ESCALATION_MAX_TOKENS")
    assert 0 < cap <= 32000, f"抬预算上限 {cap} 不对"
    assert "AI_MAX_ESCALATION_MAX_TOKENS" in src, "重试时没用上限把预算按住"


def test_qa_timeout_allows_long_thinking(src: str):
    """时限要容得下长思考。

    用户原话："他即使思考再久也行，我只要他能回答问题。"
    → 时限只该防「真卡死」，不该砍掉还在正常思考的请求。
    电脑端 QA_TIMEOUT = 300s。
    """
    ms = _const(src, "AI_QA_TIMEOUT_MS")
    assert ms >= 240000, f"平板答疑时限 {ms // 1000}s 太短（电脑端是 300s）"


def test_forced_thinking_model_never_gets_thinking_disabled(src: str):
    """强制思考模型不能拿 thinking=disabled —— 上游会 400。

    实测 MiniMax M3.1-Flash 的报错原文：
        'model "MiniMax-M3.1-Flash" requires adaptive thinking;
         thinking.type="disabled" ... is not allowed'
    """
    assert 'm === "MiniMax-M3.1-Flash"' in src, \
        "平板端没有认「强制思考模型」"
    # 判定函数必须先挡掉强制思考模型
    fn = src[src.index("function shouldSendThinkingDisabled("):][:900]
    assert "modelForcesThinking" in fn, \
        "shouldSendThinkingDisabled 没查「这个模型能不能关思考」"


def test_agnes_never_gets_thinking_disabled(src: str):
    """Agnes 传 disabled 会**静默**把预算吃光（HTTP 200、正文 0 字）。

    实测：98.6s / reasoning 9072 字 / content 一字没有。
    → 判据走 provider（模型名会变，provider 不会）。
    """
    fn = src[src.index("function shouldSendThinkingDisabled("):][:900]
    assert "agnes" in fn.lower(), "shouldSendThinkingDisabled 没排除 Agnes provider"


# ---------------------------------------------------------------------------
# 2) 空正文要自己救一次（§4.38 连带修）
# ---------------------------------------------------------------------------

def test_empty_body_triggers_an_automatic_retry(src: str):
    """正文为空时必须**自动重试**，不能只弹一句提示就结束。

    用户看到的是「等了半天什么也没输出」—— 平板原来就只提示不重试。
    """
    body = src[src.index("async function runAI("):][:14000]
    assert "正文为空" in body, "runAI 里没有「正文为空」的处理分支"
    assert "sendOnce()" in body, "空正文后没有重试"


def test_empty_body_retry_raises_budget_for_forced_thinking_models(src: str):
    """对强制思考模型，空正文重试必须**加预算**，不能原样重发。

    ⚠ 抄电脑端的教训：`should_send_thinking_disabled` 对 M3.1-Flash 返回 False
    → "关思考重试"发出去的请求与第一次**完全相同** → 再等 55 秒，还是 0 字
    （实测 55s → 111s 才报错）。用户白等一倍时间。
    """
    body = src[src.index("async function runAI("):][:14000]
    assert "payload.max_tokens * 4" in body, \
        "强制思考模型没有「抬高 max_tokens 重试」的分支"
    assert "shouldSendThinkingDisabled(cfg.base, payload.model)" in body, \
        "空正文重试没有判断「这个模型到底能不能关思考」"


# ---------------------------------------------------------------------------
# 3) 重答不死锁（§4.37）
# ---------------------------------------------------------------------------

def test_new_request_replaces_old_instead_of_being_rejected(src: str):
    """新请求必须能替换旧请求，不能弹「已有 AI 请求进行中」就拒绝。

    用户原话（2026-10-26）："我给他反馈之后他会显示就是已有AI在回答中
    这个提问失败……但是他显然就不会再输出了。"
    """
    assert "已有 AI 请求进行中" not in src, \
        "平板端还在用『已有 AI 请求进行中』拒绝新请求 —— 用户被卡死"
    assert "aiAbort.abort()" in src, "替换旧请求时没有 abort 掉它"


def test_stale_request_callbacks_cannot_overwrite_new_answer(src: str):
    """被顶掉的旧请求不许覆盖新请求的答案区/停止按钮（请求序号守卫）。"""
    assert "aiRequestSeq" in src, "缺少请求序号守卫"
    assert "isCurrent" in src, "缺少「我还是当前主人」的判定"
    # 共享状态（停止按钮 / 多轮上下文）必须受守卫保护
    body = src[src.index("async function runAI("):][:14000]
    assert "if (isCurrent())" in body, "停止按钮/历史没有受序号守卫保护"


def test_ai_request_takes_signal_from_caller(src: str):
    """aiRequest 必须用**调用方传的** signal，不能读全局 aiAbort。

    ⚠ 全局 aiAbort 会被下一次请求换掉 —— 旧请求的回调就跟着新请求走了
    （旧请求的响应会被当成新请求的结果）。
    """
    fn = src[src.index("async function aiRequest("):][:2600]
    assert "aiAbort.signal" not in fn, \
        "aiRequest 又在读全局 aiAbort 了（会被下一次请求串味）"
    assert "signal: signal || undefined" in fn, \
        "aiRequest 没把调用方传的 signal 用到 fetch 上"


# ---------------------------------------------------------------------------
# 4) 思考强度 reasoning_effort（§4.39）
# ---------------------------------------------------------------------------

def test_reasoning_effort_default_is_low(src: str):
    """思考强度默认 low（用户原话："你把它给设定成 low"）。"""
    m = re.search(r'const AI_REASONING_EFFORT\s*=\s*"(\w+)"', src)
    assert m, "平板端没有 AI_REASONING_EFFORT 常量"
    assert m.group(1) == "low", f"思考强度是 {m.group(1)!r}，用户要求 low"


def test_reasoning_effort_not_sent_to_unknown_gateways(src: str):
    """未知网关不发这个参数 —— 判错的后果是 HTTP 400（请求直接被拒）。

    ⚠ 平板的用户可以把自己的接口指向**任意** OpenAI 兼容网关，
    那种网关上照样挂着「MiniMax-M3.1-Flash」这个模型名 → **不能靠模型名判**，
    只能看网关地址（base）。
    """
    fn = src[src.index("function acceptsReasoningEffort("):][:900]
    assert "base" in fn, "判定函数没看网关地址"
    body = fn[fn.index("const hay"):] if "const hay" in fn else fn
    # 判定依据里不能掺进模型名
    assert not re.search(r"hay\s*=\s*[^;]*model", fn), \
        "判定依据里掺了模型名 —— 自建网关挂着 MiniMax 模型名也会被误判"


def test_forced_thinking_model_never_gets_reasoning_effort_none(src: str):
    """强制思考模型不能拿 none —— 上游会 400（实测 429 才是过了校验）。"""
    fn = src[src.index("function reasoningEffortFor("):][:900]
    assert "modelForcesThinking" in fn, "reasoningEffortFor 没查强制思考模型"
    assert '"low"' in fn, "强制思考模型的 none 没有降级成 low"


def test_reasoning_effort_reaches_the_request_payload(src: str):
    """reasoning_effort 必须真的进请求体（不是只算不用）。"""
    body = src[src.index("async function runAI("):][:14000]
    assert "payload.reasoning_effort" in body, \
        "reasoning_effort 没有进请求体"


# ---------------------------------------------------------------------------
# 5) 能力边界：平板端是直连模型的，提示词必须自己写一份（§4.37）
# ---------------------------------------------------------------------------

def _prompt_block(src: str, const_name: str) -> str:
    start = src.index(f"const {const_name}")
    return src[start:start + 1600]


def test_all_research_prompts_declare_capability_boundary(src: str):
    """三个科研类提示词都要写清能力边界 + 必须回答。

    平板是**直连模型**的（不走后端提示词）→ 边界说明必须自己写一份。
    """
    for name in ("SYS_RESEARCH", "SYS_SUMMARY"):
        block = _prompt_block(src, name)
        assert "不能联网" in block, f"{name} 没写明不能联网"
        assert "必须回答" in block, f"{name} 没有「必须回答」条款"
        assert "不允许交白卷" in block, f"{name} 没禁止交白卷"


def test_capability_boundary_goes_in_the_last_sentence(src: str):
    """能力边界放**最后一句**，先给实质内容。

    用户明确要求："不需要他第一句话就说自己的能力边界。
    你只需要最后能说一句就行了。"
    """
    block = _prompt_block(src, "SYS_RESEARCH")
    assert "最后一句" in block, "提示词没把能力边界放在最后一句"
    assert "我没有联网检索能力" in block, "提示词没给出明确的能力边界话术"


def test_summary_prompt_is_actually_used(src: str):
    """摘要必须真的用带能力边界的提示词（别定义了却没接上）。"""
    assert "content: SYS_SUMMARY" in src, "摘要还在用旧提示词"
