"""模型层护栏：自定义模型（用户自己的 API 模型）/ 降级 / 限流 / 抄表校验。

用户诉求（2026-10-25）：
  - 要有一个能选的模型（下拉框）
  - 主模型额度用完要能自动降级
  - 想用自己的 API 模型（比如 DeepSeek）时，只要填一下接口信息就能用
  - 付费模型不吃速率限制，只有免费版 Agnes 有限制
  - 表格靠多模态读图 → 抄表失败不能把"这里没有表格"当表格拼进译文
"""
import os

import pytest

from microbench import llm
from microbench import app as app_mod


# ---------------------------------------------------------------------------
# 1) .env 自定义模型配置
# ---------------------------------------------------------------------------

def _set_custom_env(monkeypatch, slot=1, **overrides):
    base = {
        "LLM_CUSTOM_%d_NAME" % slot: "我的模型",
        "LLM_CUSTOM_%d_BASE_URL" % slot: "https://api.example.com/v1",
        "LLM_CUSTOM_%d_MODEL" % slot: "my-vision-model",
        "LLM_CUSTOM_%d_API_KEY" % slot: "sk-test-123456",
        "LLM_CUSTOM_%d_RPM" % slot: "30",
    }
    base.update(overrides)
    for k, v in base.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


def test_custom_model_is_parsed_from_env(monkeypatch):
    """自定义模型能从 .env 的 LLM_CUSTOM_N_* 读出来。"""
    _set_custom_env(monkeypatch)
    cfgs = llm.custom_model_configs()
    assert len(cfgs) == 1, cfgs
    c = cfgs[0]
    assert c["id"] == "custom:我的模型"
    assert c["label"] == "我的模型"
    assert c["base_url"] == "https://api.example.com/v1"
    assert c["model"] == "my-vision-model"
    assert c["configured"] is True
    assert c["is_custom"] is True


def test_custom_model_without_key_is_not_configured(monkeypatch):
    """没填 API key → 标成未配置，界面上给出提示，不静默失败。"""
    _set_custom_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("LLM_CUSTOM_1_API_KEY", raising=False)
    cfgs = llm.custom_model_configs()
    assert cfgs and cfgs[0]["configured"] is False


def test_custom_model_reuses_primary_key_when_blank(monkeypatch):
    """API key 留空 → 复用 LLM_API_KEY（多数自建网关同 key）。"""
    _set_custom_env(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-primary-key-abcdef")
    monkeypatch.delenv("LLM_CUSTOM_1_API_KEY", raising=False)
    cfgs = llm.custom_model_configs()
    assert cfgs[0]["api_key"] == "sk-primary-key-abcdef"


def test_incomplete_custom_model_is_ignored(monkeypatch):
    """只填了一半（没有 base_url 或 model）→ 不出现在清单里。"""
    _set_custom_env(monkeypatch)
    monkeypatch.delenv("LLM_CUSTOM_1_BASE_URL", raising=False)
    assert llm.custom_model_configs() == []


def test_custom_model_rpm_configurable(monkeypatch):
    """RPM 可以配（对方的限流更紧时调小）。"""
    _set_custom_env(monkeypatch, LLM_CUSTOM_1_RPM="6")
    c = llm.custom_model_configs()[0]
    assert c["rpm"] == 6
    assert c["min_interval"] >= 1.0


def test_paid_custom_model_defaults_to_permissive_limit(monkeypatch):
    """付费模型默认不吃严格限流（用户原话：DeepSeek/MiniMax 速率不用管）。"""
    _set_custom_env(monkeypatch, LLM_CUSTOM_1_RPM=None)
    c = llm.custom_model_configs()[0]
    assert c["rpm"] == llm.DEFAULT_CUSTOM_RPM >= 20
    assert c["min_interval"] <= 2.5


# ---------------------------------------------------------------------------
# 2) 按 id 找配置
# ---------------------------------------------------------------------------

def test_resolve_model_config_custom(monkeypatch):
    _set_custom_env(monkeypatch)
    cfg = llm.resolve_model_config("custom:我的模型")
    assert cfg and cfg["base_url"] == "https://api.example.com/v1"


def test_resolve_model_config_agnes(monkeypatch):
    cfg = llm.resolve_model_config("agnes")
    assert cfg is not None
    assert cfg.get("is_fallback") is True


def test_resolve_model_config_returns_none_for_plain_model_name(monkeypatch):
    """普通模型名 → None（含义：同一套 key/base_url 下的另一个模型名）。"""
    assert llm.resolve_model_config("MiniMax-M3") is None
    assert llm.resolve_model_config("") is None


def test_custom_model_uses_its_own_limiter(monkeypatch):
    """自定义模型按自己的 RPM 档限流，不跟主模型/Agnes 共用。"""
    _set_custom_env(monkeypatch, LLM_CUSTOM_1_RPM="6")
    cfg = llm.resolve_model_config("custom:我的模型")
    lim = llm.limiter_for_config(cfg)
    assert lim.max_requests == 5      # rpm 6 → 留 1 个余量
    assert lim is not llm.primary_limiter
    assert lim is not llm.agnes_limiter


def test_agnes_still_uses_its_own_strict_limiter(monkeypatch):
    """免费版 Agnes 的严格限流不能被改坏（11 RPM / 6 秒间隔）。"""
    cfg = llm.resolve_model_config("agnes")
    lim = llm.limiter_for_config(cfg)
    assert lim is llm.agnes_limiter
    assert lim.max_requests == 9
    assert lim.min_interval == 6.0


# ---------------------------------------------------------------------------
# 3) 界面清单：内置 + 自定义
# ---------------------------------------------------------------------------

def test_model_choices_include_custom_models(monkeypatch):
    _set_custom_env(monkeypatch)
    choices = app_mod.translation_model_choices()
    ids = [c["id"] for c in choices]
    assert "custom:我的模型" in ids
    assert "agnes" in ids and "" in ids
    cm = [c for c in choices if c["id"] == "custom:我的模型"][0]
    assert cm["custom"] is True
    assert "my-vision-model" in cm["hint"]


def test_resolve_translation_model_passes_custom_id_through(monkeypatch):
    """自定义模型的 id 要原样传下去（带自己的 base_url/key 去调）。"""
    _set_custom_env(monkeypatch)
    name, fallback, cache_key = app_mod._resolve_translation_model("custom:我的模型")
    assert name == "custom:我的模型"
    assert fallback is False
    assert cache_key == "custom:我的模型"


def test_resolve_translation_model_agnes_still_forces_fallback():
    name, fallback, _ = app_mod._resolve_translation_model("agnes")
    assert name == "" and fallback is True


# ---------------------------------------------------------------------------
# 4) 抄表返回值必须像表格
# ---------------------------------------------------------------------------

def test_markdown_table_detection():
    """模型说"这里没有表格" → 不算表格（不能被拼进译文）。"""
    real = "| 参数 | 值 |\n|---|---|\n| d31 | −90 |"
    assert app_mod._looks_like_markdown_table(real) is True
    # 模型回的一句说明（没有竖线）
    no_table = ("截图中的内容为论文正文（两栏段落文字），并未包含任何表格"
                "（无表题、无表头、无数据行、无表注）")
    assert app_mod._looks_like_markdown_table(no_table) is False
    # 空
    assert app_mod._looks_like_markdown_table("") is False
    assert app_mod._looks_like_markdown_table("   ") is False


def test_table_read_failure_has_fallback_text():
    """抄表失败 → 译文里放兜底提示，不是空白。"""
    src = (app_mod.__file__ and open(app_mod.__file__, encoding="utf-8").read())
    assert "【表格未能读出，请对照左侧 PDF 原文】" in src


def test_prepare_tables_passes_model_through():
    """整页翻译这条主路径必须把用户选的模型传给表格读图。"""
    src = open(app_mod.__file__, encoding="utf-8").read()
    assert "await _prepare_tables(page_text, pdf_path, req.page, req.model)" in src, \
        "翻译路径的表格读图没有跟随所选模型"
    assert "await _read_table_vision(pdf_path, page, rect, model)" in src, \
        "_prepare_tables 没有把 model 传给 _read_table_vision"


# ---------------------------------------------------------------------------
# 5) 答疑也要能用选中的模型
# ---------------------------------------------------------------------------

def test_qa_endpoints_accept_model():
    """三个答疑入口都要接受 model 参数。"""
    for cls in (app_mod.ExplainRequest, app_mod.ExplainSelectionRequest,
                app_mod.CrossQARequest):
        assert "model" in cls.model_fields, cls.__name__


def test_qa_model_args_mapping(monkeypatch):
    _set_custom_env(monkeypatch)
    assert app_mod._qa_model_args("")["model_name"] is None
    assert app_mod._qa_model_args("agnes")["force_fallback"] is True
    assert app_mod._qa_model_args("custom:我的模型")["model_name"] == "custom:我的模型"
    assert app_mod._qa_model_args("MiniMax-M3")["model_name"] == "MiniMax-M3"


def test_effective_model_name_reports_the_chosen_one(monkeypatch):
    """界面上的模型徽标要显示**实际**用的模型（不是全局 active_model）。"""
    _set_custom_env(monkeypatch)
    assert app_mod._effective_model_name("custom:我的模型") == "我的模型"
    assert app_mod._effective_model_name("agnes") != "我的模型"
