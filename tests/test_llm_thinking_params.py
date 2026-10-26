"""thinking 参数按 provider 判定的护栏（2026-10-26）。

这个文件锁的是一次**真实的静默失败**：

实测（生产页面文本 + 生产翻译提示词，JACERS 综述第 2 页）：

    模型                  传 thinking={"type":"disabled"}      不传
    ---------------------------------------------------------------------
    MiniMax-M3            7.6s / 2033 字 ✅ 干净              32.5s / 混入思考文本
    MiniMax-M3.1-Flash    **HTTP 400 直接拒**                 正常（强制思考）
    Agnes 3.0 Flash       **98.6s / 正文 0 字** ❌             27.9s / 2092 字 ✅
    DeepSeek Flash        3.9s / 906 字 ✅ 干净                20.9s / **正文 0 字** ❌

两种失败都是**静默**的：HTTP 200、看起来成功、内容是空的。
用户看到的现象是"翻译完但什么都没有"，或者"等了一分钟拿到空白"。

原代码对所有模型统一发 thinking 参数 → 选 Agnes 翻译就会白等 98 秒拿到空白。
现在改成按 provider 判定。
"""
import pytest

from microbench import llm


def _cfg(model, base_url):
    return {"model": model, "base_url": base_url, "api_key": "sk-test"}


# ---------------------------------------------------------------------------
# 1) 每个模型该不该收到 thinking 参数
# ---------------------------------------------------------------------------

def test_minimax_m3_accepts_thinking_param():
    """M3 支持关思考 → 直译快 4 倍且译文干净（实测 7.6s vs 32.5s）。"""
    cfg = _cfg("MiniMax-M3", "https://api.minimaxi.com/v1")
    assert llm.should_send_thinking_disabled(cfg, True) is True


def test_m31_flash_never_gets_thinking_param():
    """M3.1-Flash 强制 adaptive thinking → 传 disabled 直接 400。

    不能等"撞了 400 再退"：每个新进程都要白撞一次，
    而且翻译一章要发好几次请求。
    """
    cfg = _cfg("MiniMax-M3.1-Flash", "https://api.minimaxi.com/v1")
    assert llm.should_send_thinking_disabled(cfg, True) is False
    assert llm.cfg_accepts_thinking_param(cfg) is False


def test_agnes_never_gets_thinking_param():
    """Agnes 传 disabled 会**静默**把 max_tokens 预算全花在思考上。

    实测 98.6s / reasoning 9072 字符 / content 0 字 / HTTP 200。
    这是最阴险的一种：不报错，用户只看到"翻译完但什么都没有"。
    """
    cfg = _cfg("agnes-3.0-flash", "https://apihub.agnes-ai.com/v1")
    assert llm.should_send_thinking_disabled(cfg, True) is False
    assert llm.cfg_accepts_thinking_param(cfg) is False


def test_agnes_detected_by_provider_not_model_name():
    """判据按 provider 走 —— 模型名会变，provider 不会。"""
    cfg = _cfg("agnes-some-future-model", "https://apihub.agnes-ai.com/v1")
    assert llm.should_send_thinking_disabled(cfg, True) is False
    # 即使模型名里没有 "agnes"，只要 base_url 是 Agnes 网关就不发
    cfg2 = _cfg("flash-3.0", "https://apihub.agnes-ai.com/v1")
    assert llm.should_send_thinking_disabled(cfg2, True) is False


def test_deepseek_must_get_thinking_param():
    """DeepSeek Flash **必须**收到 thinking 参数。

    不发的话它默认开着思考 → reasoning 8293 字符 / content 0 字 / 20.9s。
    这跟 Agnes 正好相反：Agnes 是"发了就空"，DeepSeek 是"不发就空"。
    """
    cfg = _cfg("deepseek-flash", "https://api.deepseek.com/v1")
    assert llm.should_send_thinking_disabled(cfg, True) is True


def test_disable_thinking_false_never_sends_param():
    """要思考的时候（答疑用 M3.1-Flash）永远不发这个参数。"""
    cfg = _cfg("MiniMax-M3", "https://api.minimaxi.com/v1")
    assert llm.should_send_thinking_disabled(cfg, False) is False


def test_unknown_model_is_permissive_but_not_aggressive():
    """未知模型：允许发（多数 OpenAI 兼容网关支持），但由 400 自动降级兜底。"""
    cfg = _cfg("some-vendor-model", "https://api.vendor.com/v1")
    assert llm.should_send_thinking_disabled(cfg, True) is True


# ---------------------------------------------------------------------------
# 2) 运行时撞过 400 的模型要被记住（不再发）
# ---------------------------------------------------------------------------

def test_model_that_rejected_thinking_is_remembered(monkeypatch):
    """撞过 400 的模型记进黑名单 → 之后不再发该参数。"""
    monkeypatch.setattr(llm, "_THINKING_DISABLE_UNSUPPORTED", set())
    cfg = _cfg("vendor-thinking-hostile", "https://api.vendor.com/v1")
    assert llm.should_send_thinking_disabled(cfg, True) is True
    llm._THINKING_DISABLE_UNSUPPORTED.add("vendor-thinking-hostile")
    assert llm.should_send_thinking_disabled(cfg, True) is False


# ---------------------------------------------------------------------------
# 3) 限流档：只有 Agnes 严格（用户 2026-10-26 的原话）
# ---------------------------------------------------------------------------

def test_only_agnes_has_a_strict_rate_limit():
    """「RPM 这块只有 Agnes 要注意，其他的都正常」——护栏锁住这条。"""
    agnes = llm.resolve_model_config("agnes")
    assert llm.limiter_for_config(agnes) is llm.agnes_limiter
    assert llm.agnes_limiter.max_requests == 9
    assert llm.agnes_limiter.min_interval == 6.0

    # DeepSeek：宽松档，但**独立**于主模型（额度独立 → 限流也独立）
    ds = llm.resolve_model_config("deepseek-flash")
    if ds is not None:
        lim = llm.limiter_for_config(ds)
        assert lim is not llm.agnes_limiter
        assert lim.min_interval <= 2.5

    # 主模型：宽松档
    primary = {"model": "MiniMax-M3", "base_url": "https://api.minimaxi.com/v1"}
    assert llm.limiter_for_config(primary) is llm.primary_limiter
    assert llm.primary_limiter.min_interval <= 2.5


# ---------------------------------------------------------------------------
# 4) 翻译模型与答疑模型是两套独立清单
# ---------------------------------------------------------------------------

def test_translation_and_qa_model_lists_differ():
    """翻译清单和答疑清单不是一回事（否则用户没法各选各的）。"""
    from microbench import app as app_mod

    tr_ids = {m["id"] for m in app_mod.translation_model_choices()}
    qa_ids = {m["id"] for m in app_mod.qa_model_choices()}

    # 答疑默认是 M3.1-Flash —— 它**不该**出现在翻译清单的主推位置
    assert "MiniMax-M3.1-Flash" in qa_ids
    # 翻译清单里的直译型模型（M3 / M2.7）不该出现在答疑清单
    assert "MiniMax-M2.7" not in qa_ids
    # 两个清单都有这三家
    for mid in ("agnes", "deepseek-flash"):
        assert mid in tr_ids and mid in qa_ids


def test_qa_active_model_defaults_to_reasoning_model():
    """答疑默认模型 = M3.1-Flash（会推导），不是主模型 M3（直译型）。"""
    from microbench import llm as llm_mod
    assert llm_mod.qa_active_model() == llm_mod.DEFAULT_QA_MODEL
    assert llm_mod.DEFAULT_QA_MODEL == "MiniMax-M3.1-Flash"


# ---------------------------------------------------------------------------
# 5) "agnes" 是**备用模型的别名**，不是模型名（2026-10-26 实测 bug）
# ---------------------------------------------------------------------------

def test_agnes_alias_is_not_treated_as_a_model_name():
    """model_name="agnes" 必须走备用模型链路。

    踩过的坑（实测）："agnes" 被当成"主模型网关下的另一个模型名" →
    primary 被改成 model="agnes" → 拿着 MiniMax 的 key 去打 →
    HTTP 400 invalid params, unknown model 'agnes'
    → 用户选「Agnes 3.0 Flash」翻译时**必然失败**。
    """
    cfg = llm.resolve_model_config("agnes")
    assert cfg is not None
    assert cfg.get("is_fallback") is True
    # 关键：备用别名不能被当成"自定义模型"（自定义 = 自己的 base_url/key）
    assert not cfg.get("is_custom")


def test_agnes_alias_resolves_to_fallback_config():
    """resolve_model_config('agnes') 要给的是 fallback 配置（带 is_fallback）。"""
    cfg = llm.resolve_model_config("agnes")
    assert cfg["model"] == "agnes-3.0-flash"
    assert cfg.get("is_fallback") is True
    assert cfg.get("base_url") == "https://apihub.agnes-ai.com/v1"


def test_deepseek_alias_is_custom_like_not_fallback():
    """DeepSeek 是**自己的网关**（自己的 base_url/key）→ 走自定义模型链路，
    不是备用模型链路。"""
    cfg = llm.resolve_model_config("deepseek-flash")
    assert cfg is not None
    assert cfg.get("is_fallback") is False
    assert cfg.get("is_custom") is True
    assert cfg["base_url"] == "https://api.deepseek.com/v1"


# ---------------------------------------------------------------------------
# 6) 行为级断言：真正的调用必须走对链路（不是只测配置解析）
# ---------------------------------------------------------------------------

def test_chat_with_agnes_alias_calls_the_fallback_endpoint(monkeypatch):
    """model_name="agnes" → 实际请求必须打到 Agnes 网关 + 用 agnes 模型名。

    只测配置解析不够：原来的 bug 是「配置解析对了，但调用时把它当成
    模型名发给了主模型网关」。这条断言直接看**发出去的 URL 和 model**。
    """
    sent = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "译文正文"}}]}

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None, **kw):
        sent["url"] = url
        sent["model"] = json.get("model")
        sent["payload"] = json
        return FakeResp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setattr(llm.netutil, "proxy_bypassed", lambda: False)
    # 限流器会真的 sleep → 换成一个不睡的
    monkeypatch.setattr(llm.primary_limiter, "acquire", lambda: 0.0)
    monkeypatch.setattr(llm.agnes_limiter, "acquire", lambda: 0.0)

    out = llm.chat(messages=[{"role": "user", "content": "hi"}],
                   model_name="agnes", disable_thinking=True)

    assert out == "译文正文"
    assert "agnes" in sent["url"], f"请求没打到 Agnes 网关: {sent['url']}"
    assert sent["model"] == "agnes-3.0-flash", f"模型名错了: {sent['model']}"
    # ⚠️ 关键：Agnes 不能收到 thinking 参数（会静默吃光预算 → 正文 0 字）
    assert "thinking" not in sent["payload"], "Agnes 收到了 thinking 参数 → 正文会为空"


def test_chat_with_deepseek_sends_thinking_param(monkeypatch):
    """model_name="deepseek-flash" → 打到 DeepSeek 网关 + 带 thinking 参数。

    DeepSeek 不带 thinking 参数就会正文为空（实测 reasoning 8293 字符 /
    content 0 字），这是与 Agnes 正好相反的行为。
    """
    sent = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "译文正文"}}]}

    def fake_post(url, json=None, headers=None, timeout=None, proxies=None, **kw):
        sent["url"] = url
        sent["model"] = json.get("model")
        sent["payload"] = json
        return FakeResp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setattr(llm.netutil, "proxy_bypassed", lambda: False)
    monkeypatch.setattr(llm.primary_limiter, "acquire", lambda: 0.0)
    monkeypatch.setattr(llm.agnes_limiter, "acquire", lambda: 0.0)
    monkeypatch.setattr(llm, "_limiter_for", lambda rpm, mi: llm.primary_limiter)

    out = llm.chat(messages=[{"role": "user", "content": "hi"}],
                   model_name="deepseek-flash", disable_thinking=True)

    assert out == "译文正文"
    assert "deepseek.com" in sent["url"], f"请求没打到 DeepSeek: {sent['url']}"
    assert sent["model"] == "deepseek-flash"
    # DeepSeek 必须收到 thinking 参数
    assert sent["payload"].get("thinking") == {"type": "disabled"}, \
        "DeepSeek 没收到 thinking 参数 → 正文会为空"

