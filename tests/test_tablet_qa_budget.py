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
    """取出某个提示词常量的**完整**定义块。

    ⚠ 2026-10-27 独立审查揪出来的 T2：原来这里写的是
    `src[start:start+1600]` —— 而 `const SYS_SUMMARY` 距
    `const SYS_RESEARCH` 只有 525 字符 → SYS_RESEARCH 的窗口**完整吞掉**
    了 SYS_SUMMARY 的全部文本 → 把 SYS_RESEARCH 的能力边界整段删掉，
    断言照样绿（命中的是 SYS_SUMMARY）。

    → 窗口改成「到下一个 `const SYS_` 为止」，两块互不污染。
    """
    start = src.index(f"const {const_name}")
    # 找到下一个顶层常量声明（行首是 const/let/function 且顶格）
    m = re.search(r"\n  (?:const|let|function) ", src[start + 10:])
    end = start + 10 + (m.start() if m else 1600)
    return src[start:end]


def test_all_research_prompts_declare_capability_boundary(src: str):
    """三个科研类提示词都要写清能力边界 + 必须回答。

    平板是**直连模型**的（不走后端提示词）→ 边界说明必须自己写一份。
    （内容抽在共享的 CAPABILITY_BOUNDARY 里，两处引用 —— 见下面那条）
    """
    shared = src[src.index("const CAPABILITY_BOUNDARY"):][:1200]
    for kw in ("不能联网", "必须回答", "不允许交白卷"):
        assert kw in shared, f"共享的能力边界条款里缺「{kw}」"
    for name in ("SYS_RESEARCH", "SYS_SUMMARY"):
        assert "CAPABILITY_BOUNDARY" in _prompt_block(src, name), \
            f"{name} 没有引用能力边界条款"


def test_capability_boundary_goes_in_the_last_sentence(src: str):
    """能力边界放**最后一句**，先给实质内容。

    用户明确要求："不需要他第一句话就说自己的能力边界。
    你只需要最后能说一句就行了。"
    """
    shared = src[src.index("const CAPABILITY_BOUNDARY"):][:1200]
    assert "最后一句" in shared, "提示词没把能力边界放在最后一句"
    assert "我没有联网检索能力" in shared, "提示词没给出明确的能力边界话术"


def test_summary_prompt_is_actually_used(src: str):
    """摘要必须真的用带能力边界的提示词（别定义了却没接上）。"""
    assert "content: SYS_SUMMARY" in src, "摘要还在用旧提示词"


def test_both_research_prompts_share_the_same_boundary_clause(src: str):
    """两个科研提示词的能力边界条款必须**引用同一份**。

    ⚠ 2026-10-27 独立审查揪出来的 T2：交接文档里写着
    「护栏会断言两者逐字一致」—— 但**当时没有任何一条断言在比较它们**，
    说的比做的多。更要命的是：那段边界原来在两处**各抄了一份**，
    实际已经漂移了半句（摘要那句多了个「最后能说一句」）。

    → 现在代码里抽成了共享常量 `CAPABILITY_BOUNDARY`，两个提示词都引用它。
      这条断言锁的就是「**两处引用同一个常量**」——
      比「逐字比较」更强：物理上不可能漂移。
    """
    assert "const CAPABILITY_BOUNDARY" in src, \
        "能力边界条款没抽成共享常量（两处各抄一份 → 一定会漂移）"
    for name in ("SYS_RESEARCH", "SYS_SUMMARY"):
        block = _prompt_block(src, name)
        assert "CAPABILITY_BOUNDARY" in block, \
            f"{name} 没有引用共享的 CAPABILITY_BOUNDARY"
        # 不能内联一份自己的（那等于又抄了一遍）
        assert "【能力边界】" not in block, \
            f"{name} 里内联了一份能力边界条款（又抄了一遍 → 会漂移）"


def test_capability_boundary_clause_content(src: str):
    """共享的那份边界条款本身必须内容齐全（不能抽空了）。"""
    start = src.index("const CAPABILITY_BOUNDARY")
    block = src[start:start + 1200]
    for kw in ("不能联网", "搜索网页", "图片", "空转", "必须回答", "不允许交白卷", "最后一句"):
        assert kw in block, f"共享的能力边界条款里缺「{kw}」"


# ---------------------------------------------------------------------------
# 6) 2026-10-27 独立审查揪出来的缺陷（回归护栏）
# ---------------------------------------------------------------------------

def test_native_timeout_exceeds_the_qa_timeout(src: str):
    """原生代理的超时必须**比答疑时限更宽**（D1）。

    原来所有原生调用共用 150s，而答疑的真实时限是 300s
    → APK 上 300s 永远触发不了：150s 时 nativeCall 先 reject，
    错误信息还是内部术语，用户还是什么都没拿到。
    """
    m = re.search(r"httpPost:\s*(\d+)", src)
    assert m, "没找到原生调用的 httpPost 超时配置"
    http_post_ms = int(m.group(1))
    qa_ms = _const(src, "AI_QA_TIMEOUT_MS")
    assert http_post_ms > qa_ms, (
        f"原生 httpPost 超时 {http_post_ms}ms 必须大于答疑时限 {qa_ms}ms —— "
        f"否则 APK 上答疑的超时分支永远走不到"
    )


def test_native_timeout_is_reported_in_plain_chinese(src: str):
    """原生超时不能把内部术语（"原生调用超时（httpPost）"）甩给用户（D1）。"""
    assert "nativeTimeout" in src, "原生超时没有打标记，上层认不出来"
    body = src[src.index("async function runAI("):][:16000]
    assert "nativeTimeout" in body, "上层没有处理原生超时（会显示成普通失败）"
    assert "等待模型响应超时" in body, "原生超时没有转成人话"


def test_429_swap_keeps_the_raised_budget(src: str):
    """429 换模型**不能**把刚抬上去的预算冲回原值（D2）。

    正文空 → 预算从 16000 抬到 32000 → 重试正好撞 429 → 换模型 →
    预算被重置回 16000 → 若新模型也是强制思考型 → 思考再吃一次预算 →
    **又 0 字**。用户白等三倍时间。
    """
    body = src[src.index("async function runAI("):][:16000]
    assert "payload.max_tokens = Math.max(payload.max_tokens" in body, \
        "429 换模型时预算用的是赋值覆盖，不是取最大值 —— 刚抬上去的预算会被冲掉"


def test_unknown_gateway_does_not_get_thinking_disabled(src: str):
    """未知网关不能发 thinking=disabled（D3）。

    原来的写法是「除 M3.1-Flash 和 Agnes 之外一律发」→ 用户填任意模型名
    （gpt-4o / glm-4.6）都会带上，严格校验的网关直接 400。
    → 必须是**白名单**：只有实测支持的 provider 才发。
    """
    fn = src[src.index("function shouldSendThinkingDisabled("):][:1200]
    assert "AI_THINKING_DISABLED_OK_PROVIDERS" in fn, \
        "shouldSendThinkingDisabled 没有白名单（未知网关会被塞 thinking 参数）"
    # 白名单必须是"命中才发"的形态，不能是"排除掉几个就发"
    assert "some((p) => hay.indexOf(p) >= 0)" in fn, \
        "白名单判定形态不对"


def test_reasoning_effort_uses_an_exact_host_whitelist(src: str):
    """reasoning_effort 的网关判定必须是**精确主机名**白名单（D4）。

    子串匹配会误命中中转站（`https://minimax.中转.example.com` 里含 "minimax"）
    → 发了它不认的参数 → HTTP 400 且无兜底。
    """
    fn = src[src.index("function acceptsReasoningEffort("):][:1400]
    assert "AI_REASONING_EFFORT_OK_HOSTS" in fn, \
        "acceptsReasoningEffort 没有用主机名白名单"
    assert ".includes(host)" in fn, "白名单判定不是精确匹配"


def test_four_hundred_strips_optional_params_without_relying_on_wording(src: str):
    """400 兜底不能依赖错误文案里有没有 'thinking' 字样（D3/D4）。

    上游可能说 "unrecognized request argument"，也可能把参数名写在第 200 字之后
    → 靠文案匹配会漏掉 → 直接失败。
    """
    body = src[src.index("async function runAI("):][:16000]
    assert "st === 400 && payload.reasoning_effort" in body, \
        "reasoning_effort 没有 400 剥离兜底"
    assert "st === 400 && payload.thinking" in body, \
        "thinking 没有 400 剥离兜底"
    # 不能再依赖文案匹配
    assert not re.search(r"400\s*&&\s*payload\.thinking\s*&&\s*/thinking/i", body), \
        "400 兜底还在靠错误文案里的 'thinking' 字样判断"
