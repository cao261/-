"""DeepSeek provider 护栏（2026-10-26）。

用户诉求："mini Max 不够用，然后 Agnes 又太慢了，我就换 deep seek
（带视觉的版本或者 flash）"。

实测得到的关键事实（见 docs/LLM_CONTRACT.md §2）：
  - DeepSeek 是**独立网关**（MiniMax / Agnes 两边的 /v1/models 里都没有它）；
  - **deepseek-flash 支持图片输入**，deepseek-v4-pro **不支持**；
  - v4-pro 收下表格截图后会**编造整张表**（数值全错、格式像真表）
    → 表格路径必须做视觉能力检查，否则用户拿到的是"看着像真的"错表。

这些护栏锁的是上面那三条事实，尤其是最后一条（编造表格）。
"""
import os

import pytest

from microbench import llm
from microbench import app as app_mod


def _set_deepseek_env(monkeypatch, **overrides):
    base = {
        "DEEPSEEK_API_KEY": "sk-deepseek-test-123456",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
        "DEEPSEEK_RPM": "30",
    }
    base.update(overrides)
    for k, v in base.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# 1) DeepSeek 配置解析
# ---------------------------------------------------------------------------

def test_deepseek_models_are_parsed(monkeypatch):
    """DeepSeek 的两个模型都能从 env 解析出来，带自己的 base_url/key。"""
    _set_deepseek_env(monkeypatch)
    cfgs = llm.deepseek_model_configs()
    ids = [c["id"] for c in cfgs]
    assert "deepseek-flash" in ids
    assert "deepseek-v4-pro" in ids
    flash = [c for c in cfgs if c["id"] == "deepseek-flash"][0]
    assert flash["base_url"] == "https://api.deepseek.com/v1"
    assert flash["model"] == "deepseek-flash"
    assert flash["configured"] is True
    # 走自己的网关 → 与自定义模型同一条代码路径（先试它，失败自动退回）
    assert flash["is_custom"] is True


def test_deepseek_without_key_is_not_configured(monkeypatch):
    """没配 key → 标成未配置，界面上给提示，不静默失败。"""
    _set_deepseek_env(monkeypatch, DEEPSEEK_API_KEY=None)
    monkeypatch.setenv("LLM_API_KEY", "")
    cfgs = llm.deepseek_model_configs()
    assert cfgs and all(c["configured"] is False for c in cfgs)


def test_deepseek_uses_its_own_limiter(monkeypatch):
    """DeepSeek 有独立额度 → 限流档独立，不跟主模型/Agnes 共用。"""
    _set_deepseek_env(monkeypatch, DEEPSEEK_RPM="6")
    cfg = llm.resolve_model_config("deepseek-flash")
    lim = llm.limiter_for_config(cfg)
    assert lim.max_requests == 5      # rpm 6 → 留 1 个余量
    assert lim is not llm.primary_limiter
    assert lim is not llm.agnes_limiter


def test_resolve_model_config_finds_deepseek(monkeypatch):
    _set_deepseek_env(monkeypatch)
    cfg = llm.resolve_model_config("deepseek-flash")
    assert cfg is not None
    assert cfg["base_url"] == "https://api.deepseek.com/v1"


# ---------------------------------------------------------------------------
# 2) 视觉能力：这是防"编造表格"的红线
# ---------------------------------------------------------------------------

def test_only_flash_is_vision_capable():
    """deepseek-v4-pro 不支持视觉 → 不能接抄表任务。"""
    assert llm.model_supports_vision("deepseek-flash") is True
    assert llm.model_supports_vision("deepseek-v4-pro") is False


def test_unknown_model_is_treated_as_not_vision_capable():
    """未知模型按"不支持视觉"处理（保守：宁可读不出，也不要假表）。"""
    assert llm.model_supports_vision("some-unknown-model") is False
    assert llm.model_supports_vision("custom:我的模型") is False
    assert llm.model_supports_vision("") is False


def test_vision_fallback_is_a_known_vision_model():
    """抄表替补必须是已验证支持视觉的模型。"""
    alt = llm.vision_fallback_model()
    assert alt is not None
    assert llm.model_supports_vision(alt) is True
    assert alt != "deepseek-v4-pro"


def test_vision_capable_models_are_all_real():
    """白名单里的模型必须是真存在且已验证的（防手滑写错名字）。"""
    for mid in llm.VISION_CAPABLE_MODELS:
        assert mid.strip(), "白名单里不能有空串"
    assert "deepseek-v4-pro" not in llm.VISION_CAPABLE_MODELS


# ---------------------------------------------------------------------------
# 3) 抄表路径必须做视觉检查
# ---------------------------------------------------------------------------

def test_read_table_vision_rejects_non_vision_model(monkeypatch):
    """选了不支持视觉的模型 → 表格改用视觉模型，不给假表。

    反向验证：如果去掉视觉检查，这条测试必须变红
    （模型名会被原样发出去，编造的表格就会被拼进译文）。
    """
    import asyncio

    _set_deepseek_env(monkeypatch)
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return "| 参数 | 值 |\n|---|---|\n| d31 | −90 |"

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(app_mod, "_pdf_clip_png", lambda *a, **k: b"fake-png")

    # deepseek-v4-pro 不支持视觉 → 必须换成支持视觉的模型
    asyncio.run(app_mod._read_table_vision("x.pdf", 1, (0, 0, 10, 10), "deepseek-v4-pro"))
    assert calls, "抄表根本没有调用模型"
    used = calls[0]["model_name"]
    assert used != "deepseek-v4-pro", "不支持视觉的模型被派去抄表了"
    assert llm.model_supports_vision(used) is True


def test_read_table_vision_keeps_vision_model(monkeypatch):
    """选了支持视觉的模型 → 就用它（不能偷换成别的）。"""
    import asyncio

    _set_deepseek_env(monkeypatch)
    calls = []

    def fake_chat(**kwargs):
        calls.append(kwargs)
        return "| 参数 | 值 |\n|---|---|\n| d31 | −90 |"

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(app_mod, "_pdf_clip_png", lambda *a, **k: b"fake-png")

    asyncio.run(app_mod._read_table_vision("x.pdf", 1, (0, 0, 10, 10), "deepseek-flash"))
    assert calls[0]["model_name"] == "deepseek-flash"


# ---------------------------------------------------------------------------
# 4) 界面清单：DeepSeek 要出现在下拉框里
# ---------------------------------------------------------------------------

def test_model_choices_include_deepseek(monkeypatch):
    _set_deepseek_env(monkeypatch)
    choices = app_mod.translation_model_choices()
    ids = [c["id"] for c in choices]
    assert "deepseek-flash" in ids
    assert "deepseek-v4-pro" in ids
    dm = [c for c in choices if c["id"] == "deepseek-flash"][0]
    assert dm.get("deepseek") is True
    assert "DeepSeek" in dm["hint"]


def test_resolve_translation_model_passes_deepseek_id_through(monkeypatch):
    """DeepSeek 的 id 要原样传下去（带自己的 base_url/key 去调）。"""
    _set_deepseek_env(monkeypatch)
    name, fallback, cache_key = app_mod._resolve_translation_model("deepseek-flash")
    assert name == "deepseek-flash"
    assert fallback is False
    # 缓存键必须跟着模型走 → 换模型 = 真重译，不会命中旧译文
    assert cache_key == "deepseek-flash"


def test_qa_model_args_accept_deepseek(monkeypatch):
    """答疑也要能用 DeepSeek（模型名原样传下去）。"""
    _set_deepseek_env(monkeypatch)
    assert app_mod._qa_model_args("deepseek-flash")["model_name"] == "deepseek-flash"


def test_effective_model_name_reports_deepseek(monkeypatch):
    """界面徽标显示**实际**用的模型。"""
    _set_deepseek_env(monkeypatch)
    name = app_mod._effective_model_name("deepseek-flash")
    assert "DeepSeek" in name


def test_llm_status_exposes_deepseek(monkeypatch):
    """状态接口要带上 DeepSeek（界面下拉框/限流提示要用）。"""
    _set_deepseek_env(monkeypatch)
    status = llm.get_llm_status()
    ids = [m["id"] for m in status.get("deepseek_models", [])]
    assert "deepseek-flash" in ids
    entry = [m for m in status["deepseek_models"] if m["id"] == "deepseek-flash"][0]
    assert entry["configured"] is True
    assert entry["supports_vision"] is True
    # base_url 必须脱敏（虽然 DeepSeek 的 url 不含凭据，但走同一条脱敏路径）
    assert "sk-" not in entry["base_url"]
