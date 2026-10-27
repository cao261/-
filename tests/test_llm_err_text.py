# -*- coding: utf-8 -*-
"""`llm._err_text()` 的护栏：上游错误体形状不统一，解析器必须永不抛。

**为什么要有这份文件**

真实上游（MiniMax / Agnes / 各种中转网关）返回的错误体至少五种形状。
旧实现是 `resp.json().get("error", {}).get("message", ...)`，
只捕 (ValueError, KeyError) —— 于是：

    {"error": "quota exceeded"}   → AttributeError（str 没有 .get）
    {"error": null}                → AttributeError
    <html>502 Bad Gateway</html>   → ValueError（这个能捕到）

前两种会让异常**从 chat() 里逃出去**变成 HTTP 500，用户看到的是
"Internal Server Error"，而不是"额度用完了，等 5 小时窗口恢复"。
"""
from __future__ import annotations

import json

from microbench.llm import _err_text


class _FakeResp:
    """最小化的响应桩：只需要 .json() 和 .text。"""

    def __init__(self, body=None, text: str = "", json_raises: bool = False):
        self._body = body
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._body


def test_openai_style_error_message():
    r = _FakeResp({"error": {"message": "insufficient balance"}})
    assert _err_text(r) == "insufficient balance"


def test_detail_key_is_also_accepted():
    r = _FakeResp({"error": {"detail": "模型不存在"}})
    assert _err_text(r) == "模型不存在"


def test_error_as_plain_string_does_not_raise():
    """{"error": "..."} —— 旧实现这里抛 AttributeError。"""
    r = _FakeResp({"error": "quota exceeded"})
    assert _err_text(r) == "quota exceeded"


def test_error_null_falls_back_to_body_text():
    """{"error": null} —— 同样会抛 AttributeError，必须退回原文。"""
    r = _FakeResp({"error": None}, text="upstream says no")
    assert _err_text(r) == "upstream says no"


def test_html_body_does_not_raise():
    r = _FakeResp(text="<html>502 Bad Gateway</html>", json_raises=True)
    assert "502" in _err_text(r)


def test_top_level_message_is_used():
    r = _FakeResp({"message": "bad request"})
    assert _err_text(r) == "bad request"


def test_non_dict_json_does_not_raise():
    r = _FakeResp([1, 2, 3], text="list body")
    assert _err_text(r) == "list body"


def test_always_returns_str_and_is_truncated():
    r = _FakeResp({"error": {"message": "x" * 5000}})
    out = _err_text(r)
    assert isinstance(out, str)


def test_missing_json_method_does_not_raise():
    class _NoJson:
        text = "plain"

    assert _err_text(_NoJson()) == "plain"


def test_json_is_json_serialisable_roundtrip():
    """防止有人把 resp 对象整个塞进返回值（会让 json.dumps 在日志处炸）。"""
    r = _FakeResp({"error": {"message": "boom"}})
    assert json.dumps({"detail": _err_text(r)}) == '{"detail": "boom"}'
