# -*- coding: utf-8 -*-
"""
Regression guards for the translation robustness fixes (2026-10-22).

背景：MiniMax 换成 M3 / M3.1-Flash 之后，直译出现「慢 + 乱」。实测根因有三
个，都不是玄学：

  1. 思考 token 与正文共用同一个 max_tokens 预算池。M3.1-Flash 强制思考，
     整页 2766 字符时 14688 字符的推理把 4000 token 预算吃光，正文只剩
     291 字符还被截断 —— 前端却显示「已翻译」。
  2. 响应在推理中途被截断时没有闭合的 </think> 标签，正则剥不掉，
     于是模型的英文推理被当成译文直接显示。
  3. 按页抽取的原文尾巴带着跨页断词和页码（"...in a highly mag-" + "2"），
     译文必然以半截词收尾。

这些用例把三处的行为钉住，防止以后再退化。

Run:
    python -m pytest tests/test_translation_robustness.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from microbench import llm  # noqa: E402
from microbench.app import _translation_max_tokens  # noqa: E402
from microbench.paper_text import _trim_page_tail  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 思考块剥离
# ---------------------------------------------------------------------------

class TestThinkingStrip:
    def test_normal_think_block_is_removed(self):
        assert llm._strip_thinking_blocks("<think>reasoning</think>译文") == "译文"

    def test_unterminated_think_block_does_not_leak(self):
        """被截断的推理没有闭合标签 —— 整段都不能漏给用户。

        这是「已翻译但内容是乱的」最直接的来源：模型的英文推理被当成译文。
        """
        out = llm._strip_thinking_blocks("<think>We need translate only. 推理被截断了")
        assert out == ""
        assert "We need translate" not in out

    def test_orphan_closing_tag_is_dropped(self):
        assert llm._strip_thinking_blocks("</think>译文") == "译文"

    def test_multiple_blocks(self):
        out = llm._strip_thinking_blocks("<think>a</think>中间<think>b</think>结尾")
        assert out == "中间结尾"

    def test_plain_text_untouched(self):
        assert llm._strip_thinking_blocks("普通译文") == "普通译文"

    def test_empty(self):
        assert llm._strip_thinking_blocks("") == ""


# ---------------------------------------------------------------------------
# 2. 截断必须被识别（不能当成成功）
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


def _invoke_with_fake_response(monkeypatch, payload):
    """Call _invoke_model against a canned response, return (ok, message)."""
    import microbench.llm as llm_mod

    class _FakePost:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, *a, **kw):
            return _FakeResponse(payload)

    monkeypatch.setattr(llm_mod.requests, "post", _FakePost())
    cfg = {
        "api_key": "k", "base_url": "https://example.invalid/v1",
        "model": "MiniMax-M3.1-Flash", "configured": True, "is_fallback": False,
    }
    ok, msg, status = llm_mod._invoke_model(
        cfg, [{"role": "user", "content": "hi"}], 4000, 0.2, 60, False, max_retries=0,
    )
    return ok, msg, status


class TestTruncationDetection:
    def test_truncated_body_is_reported_not_returned(self, monkeypatch):
        """正文有内容但 finish_reason=length —— 必须当失败，不能当成功。"""
        payload = {
            "choices": [{
                "message": {"content": "这些发现表明，磁弛豫", "reasoning_content": "x" * 100},
                "finish_reason": "length",
            }],
        }
        ok, msg, _ = _invoke_with_fake_response(monkeypatch, payload)
        assert ok is False
        assert llm.TRUNCATED_MARKER in msg
        assert llm.TRUNCATED_BODY_CUT in msg
        assert llm.TRUNCATED_NO_BODY not in msg

    def test_no_body_is_a_different_kind(self, monkeypatch):
        """一个字正文都没产出 —— 抬预算救不回来，应该交给降级逻辑。"""
        payload = {
            "choices": [{
                "message": {"content": "", "reasoning_content": "x" * 100},
                "finish_reason": "length",
            }],
        }
        ok, msg, _ = _invoke_with_fake_response(monkeypatch, payload)
        assert ok is False
        assert llm.TRUNCATED_MARKER in msg
        assert llm.TRUNCATED_NO_BODY in msg

    def test_complete_response_is_ok(self, monkeypatch):
        payload = {
            "choices": [{
                "message": {"content": "完整译文。", "reasoning_content": ""},
                "finish_reason": "stop",
            }],
        }
        ok, msg, _ = _invoke_with_fake_response(monkeypatch, payload)
        assert ok is True
        assert msg == "完整译文。"

    def test_null_content_is_not_a_success(self, monkeypatch):
        """provider 返回 content: null —— 绝不能当成功。"""
        payload = {
            "choices": [{
                "message": {"content": None},
                "finish_reason": "stop",
            }],
        }
        ok, msg, _ = _invoke_with_fake_response(monkeypatch, payload)
        assert ok is False
        assert "返回空内容" in msg


# ---------------------------------------------------------------------------
# 3. 按页文本的尾巴必须收拾干净
# ---------------------------------------------------------------------------

class TestPageTailTrim:
    def test_page_number_line_is_dropped(self):
        text = "A complete sentence.\n2"
        assert _trim_page_tail(text) == "A complete sentence."

    def test_cross_page_hyphen_fragment_is_dropped(self):
        text = "A complete sentence.\ncan be tuned in a highly mag-"
        out = _trim_page_tail(text)
        assert out == "A complete sentence."
        assert "mag-" not in out

    def test_incomplete_last_sentence_is_dropped(self):
        text = "First sentence is fine. The second one runs into the next page"
        out = _trim_page_tail(text)
        assert out == "First sentence is fine."

    def test_complete_text_is_untouched(self):
        text = "First sentence. Second sentence. Third sentence."
        assert _trim_page_tail(text) == text

    def test_text_without_any_terminator_is_kept(self):
        """整页没有句号（公式页/图注页）时不能乱砍。"""
        text = "x = 1\ny = 2"
        assert _trim_page_tail(text) == "x = 1\ny = 2"

    def test_empty(self):
        assert _trim_page_tail("") == ""


# ---------------------------------------------------------------------------
# 4. 翻译预算要跟着源文长度走
# ---------------------------------------------------------------------------

class TestTranslationBudget:
    def test_scales_with_source_length(self):
        assert _translation_max_tokens(1000) == 4000      # 下限
        assert _translation_max_tokens(5000) == 8000      # 5000 * 1.6
        assert _translation_max_tokens(10000) == 16000    # 10000 * 1.6

    def test_is_capped(self):
        assert _translation_max_tokens(1000000) == 16000

    def test_never_below_the_old_fixed_value(self):
        """旧值是 4000，不能因为自适应就调低 —— 那是思考 token 挤掉正文的档位。"""
        for n in (0, 100, 1000, 2500):
            assert _translation_max_tokens(n) >= 4000

    def test_forced_thinking_model_gets_a_bigger_budget(self):
        """强制思考的模型：推理和正文共用预算池，预算必须更大。"""
        normal = _translation_max_tokens(2627, "MiniMax-M3")
        forced = _translation_max_tokens(2627, "MiniMax-M3.1-Flash")
        assert forced > normal * 2

    def test_forced_thinking_budget_is_still_capped(self):
        assert _translation_max_tokens(10 ** 6, "MiniMax-M3.1-Flash") <= 32000

    def test_model_forces_thinking(self):
        assert llm.model_forces_thinking("MiniMax-M3.1-Flash") is True
        assert llm.model_forces_thinking("MiniMax-M3") is False
        assert llm.model_forces_thinking("MiniMax-M2.7") is False
        assert llm.model_forces_thinking("") is False


# ---------------------------------------------------------------------------
# 5. 翻译专用模型
# ---------------------------------------------------------------------------

class TestTranslationModel:
    def test_translation_model_from_env(self, monkeypatch):
        monkeypatch.setenv("LLM_TRANSLATION_MODEL", "MiniMax-M3")
        assert llm.translation_model_name() == "MiniMax-M3"
        assert llm.translation_active_model() == "MiniMax-M3"

    def test_empty_falls_back_to_primary(self, monkeypatch):
        monkeypatch.setenv("LLM_TRANSLATION_MODEL", "")
        name = llm.translation_model_name()
        assert name == ""
        # 未指定时跟随主模型
        assert llm.translation_active_model() == llm.get_llm_status()["active_model"]

    def test_disable_thinking_default_is_on(self, monkeypatch):
        monkeypatch.delenv("LLM_TRANSLATION_DISABLE_THINKING", raising=False)
        assert llm.translation_disable_thinking() is True

    def test_disable_thinking_can_be_turned_off(self, monkeypatch):
        monkeypatch.setenv("LLM_TRANSLATION_DISABLE_THINKING", "0")
        assert llm.translation_disable_thinking() is False
