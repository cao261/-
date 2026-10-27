# -*- coding: utf-8 -*-
"""划词答疑的能力边界与「不交白卷」护栏（2026-10-26）。

用户原话（2026-10-26）：

    "可能他不能联网，但是他也不告诉我他不能联网搜索……我要的是一个答案
     或者我要他不知道他也要回答我的问题，不能就是我等很久之后显示可能
     AI 活动里显示他回答，但是他实际上还是不回答。这个是我不想要的，
     呃，他要有知道自己的能力边界。然后可以回答，我不知道，或者说，他
     做不到。这都是可以的，但是不回答的话，这个情况尽量不要用。"

    "然后我给他反馈之后他会显示就是已有AI在回答中这个提问失败……但是
     他显然就不会再输出了。"

这一轮钉死三件事（全部**行为级**断言，不只是源码正则）：

1. **能力边界**：提示词必须写清"不能联网/没有工具/不要长时间空转"，
   并且要求"无论如何都要给出实质回答"。实测对照：
       现状提示词   → 45.0s / 正文 **0 字**
       加边界后     → 28.0s / 正文 **3175 字**（第一句就说"我没有联网检索能力"）
2. **空正文不交白卷**：上游正常发 [DONE] 但一个可见字符都没吐时，
   必须报人话错误，不能静默当成成功（否则界面显示"✅ 完成"却是空答案）。
   —— 这是"AI 活动显示回答了、实际没有回答"的机制性原因。
3. **重答不死锁**：新请求必须能替换旧请求；旧请求被中止后的回调不许
   覆盖新请求的答案区。
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from microbench import llm

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "microbench" / "app.py"
LLM = REPO / "microbench" / "llm.py"
INDEX = REPO / "microbench" / "static" / "index.html"
TABLET = REPO / "microbench" / "static" / "tablet" / "app.js"


# ---------------------------------------------------------------------------
# 1) 能力边界：提示词必须说清边界 + 必须回答
# ---------------------------------------------------------------------------

def _prompts():
    """从 app.py 里取出三个答疑/解读用的系统提示词。"""
    from microbench import app
    return {
        "划词答疑": app.EXPLAIN_SELECTION_SYSTEM_PROMPT,
        "章节解读": app.EXPLAIN_SYSTEM_PROMPT,
        "跨章节问答": app.CROSS_QA_SYSTEM_PROMPT,
    }


@pytest.mark.parametrize("name", ["划词答疑", "章节解读", "跨章节问答"])
def test_prompt_declares_no_network_capability(name):
    """提示词必须明确写出"不能联网/不能搜网页/不能拿图片"。"""
    text = _prompts()[name]
    assert "不能联网" in text, f"{name} 的提示词没写明不能联网"
    assert "搜索网页" in text or "搜索" in text, f"{name} 的提示词没写明不能搜索网页"
    assert "图片" in text, f"{name} 的提示词没写明拿不到图片/实物图"
    # 必须防止"为了像在搜索而长时间空转"
    assert "空转" in text, f"{name} 的提示词没防止长时间空转推理"


@pytest.mark.parametrize("name", ["划词答疑", "章节解读", "跨章节问答"])
def test_prompt_requires_substantive_answer(name):
    """提示词必须要求"无论如何都要给实质回答"，杜绝交白卷。"""
    text = _prompts()[name]
    assert "必须回答" in text, f"{name} 的提示词没有「必须回答」条款"
    assert "不允许交白卷" in text, f"{name} 的提示词没禁止交白卷"


def test_prompt_tells_model_to_say_it_cannot_browse():
    """遇到需要外部能力的问题，要**说清**做不到。

    ⚠️ 2026-10-26 用户改口径："不需要他第一句话就说自己的能力边界……
    你只需要最后能说一句就行了。你前面第一句话，每次第一句话说挺麻烦的。"
    → 能力边界放**最后一句**，先给实质内容。
    """
    text = _prompts()["划词答疑"]
    assert "最后一句" in text, "提示词没把能力边界放在最后一句"
    assert "我没有联网检索能力" in text, "提示词没给出明确的能力边界话术"
    # 不许再要求"第一句就说"（那是旧口径）
    assert "回答的第一句" not in text, \
        "还在要求第一句就声明能力边界 —— 用户已明确改口径（放最后一句）"


def test_tablet_prompt_has_same_capability_boundary():
    """平板端是**直连模型**的（不走后端提示词），边界说明要自己写一份。

    ⚠ 2026-10-27：能力边界条款已从两个提示词里抽成**共享常量**
    `CAPABILITY_BOUNDARY`（两处各抄一份会漂移 —— 实际已经漂移过半句）。
    → 这里验「两个提示词都引用了共享常量」，而不是在某个提示词里找文案。
    """
    src = TABLET.read_text(encoding="utf-8")
    assert "const CAPABILITY_BOUNDARY" in src, "平板端没有共享的能力边界常量"
    shared = src[src.index("const CAPABILITY_BOUNDARY"):][:1200]
    assert "不能联网" in shared, "平板端提示词没写明不能联网"
    assert "必须回答" in shared, "平板端提示词没有「必须回答」条款"
    # 两个科研提示词都要引用它
    for name in ("SYS_RESEARCH", "SYS_SUMMARY"):
        start = src.index(f"const {name}")
        block = src[start:start + 1200]
        assert "CAPABILITY_BOUNDARY" in block, f"{name} 没有引用共享的能力边界常量"


# ---------------------------------------------------------------------------
# 2) 空正文不交白卷（行为级：真跑 _stream_model）
# ---------------------------------------------------------------------------

class _FakeStream:
    """最小 httpx 风格的上游响应：按脚本吐几行 SSE。"""

    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = list(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeClient:
    def __init__(self, status_code, lines):
        self._status_code = status_code
        self._lines = lines

    def stream(self, *a, **kw):
        return _FakeStream(self._status_code, self._lines)


def _sse(delta_content=None, finish=None, done=True):
    """造一条 SSE data 行。"""
    if delta_content is not None:
        obj = {"choices": [{"delta": {"content": delta_content}}]}
        return "data: " + json.dumps(obj)
    if finish is not None:
        obj = {"choices": [{"delta": {}, "finish_reason": finish}]}
        return "data: " + json.dumps(obj)
    return "data: [DONE]"


def _run_stream(lines, monkeypatch):
    """跑一次 _stream_model，收集它 yield 出来的内容。"""
    cfg = {"model": "test-model", "base_url": "http://fake.invalid/v1",
           "api_key": "sk-test"}
    monkeypatch.setattr(llm, "_get_async_client",
                        lambda *a, **kw: _FakeClient(200, lines))
    monkeypatch.setattr(llm, "_acquire_limiter_async", _no_wait)

    async def _go():
        out = []
        async for chunk in llm._stream_model(
            cfg, [{"role": "user", "content": "hi"}], 100, 0.3, 30, None,
        ):
            out.append(chunk)
        return "".join(out)

    return asyncio.run(_go())


async def _no_wait(*a, **kw):
    return None


def test_done_sentinel_with_empty_body_raises(monkeypatch):
    """上游正常发 [DONE] 但正文为空 → 必须报错，不能静默成功。

    回归（2026-10-26 实测）：M3.1-Flash 答"帮我到网上找实物图"时，
    思考 43 秒后只发思考、不发正文，然后正常发 [DONE]。
    旧代码在 [DONE] 分支里直接 return，**跳过了循环后的空正文守卫**
    → 上层认为成功 → 前端渲染一个空答案，AI 活动面板显示"✅ 完成"。
    用户看到的正是"显示在回答、实际没有回答"。
    """
    lines = [_sse(delta_content=""), _sse(finish="length"), _sse(done=True)]
    with pytest.raises(llm.LLMError) as exc:
        _run_stream(lines, monkeypatch)
    assert llm.TRUNCATED_MARKER in str(exc.value)
    assert llm.TRUNCATED_NO_BODY in str(exc.value)


def test_stream_without_done_sentinel_and_empty_body_raises(monkeypatch):
    """断流（不发 [DONE]）且正文为空 → 同样必须报错。"""
    lines = [_sse(delta_content=""), _sse(finish="length")]
    with pytest.raises(llm.LLMError) as exc:
        _run_stream(lines, monkeypatch)
    assert llm.TRUNCATED_NO_BODY in str(exc.value)


def test_whitespace_only_body_is_treated_as_empty(monkeypatch):
    """只吐空白字符（"\\n\\n"）也算空 —— 按可见字符计数。"""
    lines = [_sse(delta_content="\n\n"), _sse(done=True)]
    with pytest.raises(llm.LLMError) as exc:
        _run_stream(lines, monkeypatch)
    assert llm.TRUNCATED_NO_BODY in str(exc.value)


def test_real_body_still_passes_through(monkeypatch):
    """有正文时必须正常返回 —— 守卫不能误伤。"""
    lines = [_sse(delta_content="你好"), _sse(done=True)]
    assert _run_stream(lines, monkeypatch) == "你好"


def test_empty_body_error_is_human_readable(monkeypatch):
    """空正文报错必须说人话 + 给下一步，不能甩英文机器话。"""
    lines = [_sse(delta_content=""), _sse(done=True)]
    with pytest.raises(llm.LLMError) as exc:
        _run_stream(lines, monkeypatch)
    text = llm.friendly_error_text(exc.value)
    assert "TRUNCATED" not in text, text
    assert "__" not in text, text
    # 必须说清发生了什么 + 下一步怎么做
    assert "没有产出" in text or "没产出" in text or "空" in text, text
    assert "换" in text or "重试" in text, text


# ---------------------------------------------------------------------------
# 3) 重答不死锁（旧请求被替换，回调不许覆盖新请求）
# ---------------------------------------------------------------------------

def _index_src():
    return INDEX.read_text(encoding="utf-8")


def test_new_request_replaces_old_instead_of_being_rejected():
    """新请求必须能替换旧请求，不能弹"已有 AI 请求进行中"就拒绝。

    用户原话（2026-10-26）："我给他反馈之后他会显示就是已有AI在回答中
    这个提问失败……但是他显然就不会再输出了。"
    """
    src = _index_src()
    assert "已有 AI 请求进行中" not in src, \
        "还在用『已有 AI 请求进行中』拒绝新请求 —— 用户被卡死"
    # 必须真的去中止旧请求
    assert "old.controller.abort()" in src, "替换旧请求时没有 abort 掉它"


def test_stale_request_callbacks_cannot_overwrite_new_answer():
    """被中止的旧请求的回调不许覆盖新请求的答案区（请求序号守卫）。"""
    src = _index_src()
    assert "_qaRequestSeq" in src, "缺少请求序号守卫"
    assert "_isCurrentAsk" in src, "缺少『我还是当前主人』的判定"


def test_frontend_rejects_empty_answer_instead_of_silent_success():
    """前端收到空正文必须报错，不能显示『✅ 完成』的空答案。"""
    src = _index_src()
    assert "模型没有产出任何内容" in src, "前端没有空正文的兜底提示"
    # 空正文不能走 onSuccess
    body = src[src.index("async function askLLMStream("):][:6000]
    assert "!accumulated.trim()" in body, "前端没有按可见字符判空"


def test_frontend_progress_hint_tells_user_what_to_do():
    """长时间没产出时，提示必须给出下一步（取消/换模型），不能只说"还在想"。"""
    src = _index_src()
    assert "可点取消换模型" in src or "建议取消并换模型重试" in src, \
        "进度提示没有告诉用户可以取消/换模型"


# ---------------------------------------------------------------------------
# 4) 源码层：确保 [DONE] 分支没有把守卫跳过去
# ---------------------------------------------------------------------------

def test_done_branch_does_not_return_before_guard():
    """[DONE] 分支必须是 break 而不是 return。

    return 会跳过循环后的空正文守卫 → 空正文被当成成功。
    """
    src = LLM.read_text(encoding="utf-8")
    done_at = src.index('if data == "[DONE]":')
    done_block = src[done_at:done_at + 1200]
    assert "body_visible_chars == 0" not in done_block, \
        "守卫又写回 [DONE] 分支里了"
    # 断流与空 [DONE] 两条路都要落到同一个守卫 → 用 break 汇合
    assert re.search(r'if data == "\[DONE\]":[\s\S]{0,1200}?\bbreak\b', done_block), \
        "[DONE] 分支没有 break —— 空正文会绕过循环后的守卫"


# ---------------------------------------------------------------------------
# 5) 思考预算：思考型模型必须给足 max_tokens 与时限
# ---------------------------------------------------------------------------

def test_qa_budget_is_enough_for_thinking_models():
    """答疑的 max_tokens 必须够思考型模型用。

    答疑默认模型是 M3.1-Flash —— **强制思考**，思考 token 与正文
    **共用** max_tokens 预算池。实测（7K 上下文 + 真实提示词）：

        max_tokens=4000  → 59.9s / 正文 **0 字**（报错）
        max_tokens=16000 → 86.2s / 正文 **4884 字** ✅

    → 预算给 4000 时思考把额度吃光 → 用户看到「一直想、不回答」。
    """
    from microbench import app
    assert app.QA_MAX_TOKENS >= 16000, \
        f"答疑预算 {app.QA_MAX_TOKENS} 不够思考型模型用（实测需要 ≥16000）"


def test_qa_timeout_allows_long_thinking():
    """时限要容得下长思考。

    用户原话："他即使思考再久也行，我只要他能回答问题。"
    → 时限只该防"真卡死"，不该砍掉还在思考的正常请求。
    """
    from microbench import app
    assert app.QA_TIMEOUT >= 240, \
        f"答疑时限 {app.QA_TIMEOUT}s 太短 —— 会砍掉还在长思考的请求"


def test_all_qa_endpoints_use_the_shared_budget():
    """三个答疑接口都必须用同一套预算 —— 漏一个就有一个入口继续"只思考不回答"。"""
    src = APP.read_text(encoding="utf-8")
    for fn in ("api_explain_selection_stream", "api_explain_stream",
               "api_cross_qa_stream"):
        start = src.index(f"async def {fn}(")
        end = src.index("\n@app.", start)
        body = src[start:end]
        assert "max_tokens=QA_MAX_TOKENS" in body, \
            f"{fn} 没用 QA_MAX_TOKENS（还在用旧的 1200/4000）"
        assert "timeout=QA_TIMEOUT" in body, \
            f"{fn} 没用 QA_TIMEOUT（默认只有 60 秒）"


def test_empty_body_retry_is_not_a_noop_for_thinking_models():
    """空正文后的重试，对强制思考模型必须**加预算**，不能原样重发。

    `should_send_thinking_disabled` 对 M3.1-Flash 返回 False
    → "关思考重试"发出去的请求与第一次**完全相同** → 再等 55 秒，
    还是 0 字（实测 55s → 111s 才报错）。用户白等一倍时间。
    → 强制思考模型必须走"抬高 max_tokens"那条路。
    """
    src = LLM.read_text(encoding="utf-8")
    start = src.index("async def chat_stream_async(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert "should_send_thinking_disabled(primary, True)" in body, \
        "空正文重试没有判断『这个模型到底能不能关思考』"
    # 加预算那条路必须真的存在
    assert re.search(r"max_tokens \* 4", body), \
        "强制思考模型没有『抬高 max_tokens 重试』的分支"


# ---------------------------------------------------------------------------
# 6) 思考强度：reasoning_effort（2026-10-26）
# ---------------------------------------------------------------------------

def test_qa_uses_low_reasoning_effort():
    """答疑要用 reasoning_effort="low"（用户原话："设定成 low"）。"""
    from microbench import app
    assert app.QA_REASONING_EFFORT == "low", \
        f"答疑思考强度是 {app.QA_REASONING_EFFORT!r}，用户要求 low"


def test_all_qa_endpoints_pass_reasoning_effort():
    """三个答疑接口都要把思考强度传下去。"""
    src = APP.read_text(encoding="utf-8")
    for fn in ("api_explain_selection_stream", "api_explain_stream",
               "api_cross_qa_stream"):
        start = src.index(f"async def {fn}(")
        end = src.index("\n@app.", start)
        body = src[start:end]
        assert "reasoning_effort=QA_REASONING_EFFORT" in body, \
            f"{fn} 没传 reasoning_effort"


def test_forced_thinking_model_never_gets_reasoning_effort_none():
    """强制思考模型不能拿 none —— 上游会 400。

    实测 MiniMax M3.1-Flash 的报错原文：
        'model "MiniMax-M3.1-Flash" requires adaptive thinking;
         reasoning effort none is not allowed (2013)'
    """
    from microbench import llm
    cfg = {"model": "MiniMax-M3.1-Flash",
           "base_url": "https://api.minimaxi.com/v1", "api_key": "k"}
    assert llm.reasoning_effort_for(cfg, "none") == "low", \
        "强制思考模型拿到了 none —— 上游会 400"


def test_reasoning_effort_not_sent_to_unknown_providers():
    """未知网关不发这个参数 —— 判错的后果是 HTTP 400（请求直接被拒）。"""
    from microbench import llm
    cfg = {"model": "my-model", "base_url": "https://example.com/v1", "api_key": "k"}
    assert llm.reasoning_effort_for(cfg, "low") is None, \
        "未知网关也发了 reasoning_effort —— 可能被 400"


def test_known_providers_accept_reasoning_effort():
    """已实测的三个网关都认这个参数（DeepSeek / Agnes / MiniMax）。"""
    from microbench import llm
    for cfg in (
        {"model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
         "api_key": "k"},
        {"model": "agnes-3.0-flash", "base_url": "https://apihub.agnes-ai.com/v1",
         "api_key": "k"},
        {"model": "MiniMax-M3.1-Flash", "base_url": "https://api.minimaxi.com/v1",
         "api_key": "k"},
    ):
        assert llm.reasoning_effort_for(cfg, "low") == "low", \
            f"{cfg['model']} 拿不到 low"


# ---------------------------------------------------------------------------
# 6b) 判定必须看**网关**，不看模型名（2026-10-27 审计对齐平板端）
# ---------------------------------------------------------------------------

def test_relay_gateway_with_minimax_name_does_not_get_reasoning_effort():
    """自建/中转网关即使挂着 MiniMax 模型名，也不能发 reasoning_effort。

    用户可以在 .env 里把自己的接口指向**任意** OpenAI 兼容网关，
    那种网关上照样挂着「MiniMax-M3.1-Flash」这个模型名，
    但它**不保证**转发 reasoning_effort → 发了就 **HTTP 400（请求直接被拒）**。

    ⚠ 这条是子串匹配的经典漏网场景：base 里含 "minimax" 就会命中。
    → 判据必须是**精确主机名**。
    """
    from microbench import llm
    for base in ("https://minimax.中转.example.com/v1",
                 "https://my-relay.example.com/v1",
                 "https://api.minimaxi.com.evil.example/v1"):
        cfg = {"model": "MiniMax-M3.1-Flash", "base_url": base, "api_key": "k"}
        assert llm.reasoning_effort_for(cfg, "low") is None, \
            f"{base} 误命中白名单 → 会发它不认的参数 → HTTP 400"


def test_reasoning_effort_hostname_parsing():
    """主机名解析要处理端口 / userinfo / 路径。"""
    from microbench import llm
    # 带端口 + userinfo 的官方网关 → 仍要认出来
    cfg = {"model": "deepseek-flash",
           "base_url": "https://user:pass@api.deepseek.com:443/v1", "api_key": "k"}
    assert llm.reasoning_effort_for(cfg, "low") == "low", \
        "带端口/userinfo 的官方网关没被认出来"
    # 没有协议前缀的裸主机名
    cfg2 = {"model": "deepseek-flash", "base_url": "api.deepseek.com/v1", "api_key": "k"}
    assert llm.reasoning_effort_for(cfg2, "low") == "low", \
        "裸主机名没被认出来"


def test_desktop_and_tablet_use_the_same_host_whitelist():
    """桌面端与平板端的网关白名单必须一致 —— 两端判据不同就会出现「一边 400」。"""
    import re as _re
    desktop = set(llm._REASONING_EFFORT_OK_HOSTS)
    tablet_src = TABLET.read_text(encoding="utf-8")
    start = tablet_src.index("AI_REASONING_EFFORT_OK_HOSTS")
    block = tablet_src[start:start + 400]
    # 白名单是 JS 数组字面量：一行里可能有多个字符串
    tablet = set(_re.findall(r'"([a-z0-9.\-]+\.[a-z]{2,})"', block))
    assert tablet, "没从平板端源码里解析出白名单 —— 解析方式要跟着格式改"
    assert desktop == tablet, \
        f"两端白名单不一致：桌面端 {desktop} vs 平板端 {tablet}"




def test_stream_model_passes_reasoning_effort_into_payload():
    """_stream_model 必须真的把 reasoning_effort 放进请求体。"""
    src = LLM.read_text(encoding="utf-8")
    assert 'payload["reasoning_effort"] = effort' in src, \
        "reasoning_effort 没有进请求体"


# ---------------------------------------------------------------------------
# 7) 降格标记要看着**这次实际用的模型**（2026-10-26 浏览器实测）
# ---------------------------------------------------------------------------

def test_explicit_model_is_not_marked_degraded_when_global_state_is():
    """用户显式选了独立额度的模型时，徽标**不许**显示"(降格)"。

    真实事故（2026-10-26 浏览器实测）：MiniMax 额度烧完 → 全局降格，
    但用户显式选了 DeepSeek（独立额度、完全正常）
    → 徽标却显示「🤖 DeepSeek V4.1 Flash（默认） (降格)」
    → 用户以为"换模型没生效"。
    """
    from microbench import app
    import microbench.llm as llm_mod

    orig = llm_mod.get_llm_status
    llm_mod.get_llm_status = lambda: {"is_degraded": True,
                                       "active_model": "MiniMax-M3.1-Flash"}
    try:
        # 显式选 DeepSeek（独立额度）→ 不该标降格
        assert app._is_degraded_for("deepseek-flash") is False, \
            "显式选的独立额度模型被标成了降格 —— 徽标在误导用户"
        # 跟随默认 → 主模型降格时要标出来
        assert app._is_degraded_for("") is True, \
            "跟随默认时主模型降格却没标出来"
    finally:
        llm_mod.get_llm_status = orig


