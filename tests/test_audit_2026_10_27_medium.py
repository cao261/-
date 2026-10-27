# -*- coding: utf-8 -*-
"""2026-10-27 审计的 MEDIUM 项护栏（子模块层）。

覆盖：
  M-4 `paper_fetcher._safe_redirect_chain` 绕过代理兜底
  M-5 `export_offline._safe_url` 放行协议相对 URL（//evil.com/x）
  M-2 `translation.sanitize_translation` 破坏代码块里的 $ 与 \\(
"""
from __future__ import annotations

import re

import pytest


# ===========================================================================
# M-4 代理兜底要覆盖**所有**出网路径
# ===========================================================================

def test_redirect_chain_uses_proxy_fallback(monkeypatch):
    """`_safe_redirect_chain` 是唯一绕过 netutil 兜底的出网调用 → 兜底在这里断了。

    实测（2026-10-27 审计）：代理死掉时，DOI 跳转链这一步直接抛 ProxyError，
    而其它出网路径都有兜底。
    """
    import inspect
    from microbench import paper_fetcher

    src = inspect.getsource(paper_fetcher._safe_redirect_chain)
    assert "netutil.direct_kwargs()" in src, src


def test_redirect_chain_passes_proxies_when_proxy_is_dead(monkeypatch):
    """行为级：代理被判死时，那一跳必须带上 proxies={http:None,...}。"""
    from microbench import paper_fetcher
    from microbench import netutil

    monkeypatch.setattr(netutil, "proxy_bypassed", lambda: True)
    seen = {}

    class _Resp:
        status_code = 200
        headers = {"Location": ""}

        def close(self):
            pass

    def _fake_request(method, url, **kwargs):
        seen.update(kwargs)
        seen["_method"] = method
        seen["_url"] = url
        return _Resp()

    monkeypatch.setattr(paper_fetcher.requests, "request", _fake_request)
    monkeypatch.setattr(paper_fetcher, "_assert_public_http_url", lambda u: None)

    paper_fetcher._safe_redirect_chain("https://example.org/doi/10.1/x")

    assert seen.get("proxies") == {"http": None, "https": None}, seen
    assert "timeout" in seen, seen


# ===========================================================================
# M-5 协议相对 URL 必须拒绝
# ===========================================================================

def test_safe_url_rejects_protocol_relative_urls():
    """`//evil.com/x` 以 "/" 开头 → 会被相对路径白名单放行。

    离线 HTML 是用 file:// 打开的，点一下就会向外网发请求
    → 泄露阅读行为 / IP，或被当钓鱼跳转。
    """
    from microbench import export_offline

    for payload in ("//evil.com/x", "//evil.com", "//EVIL.com/x",
                    "//evil.com/x?a=1", "  //evil.com/x  "):
        assert export_offline._safe_url(payload) == "#", payload


def test_safe_url_still_allows_normal_urls():
    """不能把正常链接一起挡掉。"""
    from microbench import export_offline

    assert export_offline._safe_url("https://example.org/a") == "https://example.org/a"
    assert export_offline._safe_url("http://example.org/a") == "http://example.org/a"
    assert export_offline._safe_url("images/a.png") == "images/a.png"
    assert export_offline._safe_url("/abs/path.png") == "/abs/path.png"
    assert export_offline._safe_url("#anchor") == "#anchor"
    assert export_offline._safe_url("mailto:a@b.c") == "mailto:a@b.c"


def test_safe_url_still_rejects_dangerous_schemes():
    from microbench import export_offline

    for payload in ("javascript:alert(1)", "data:text/html,<script>",
                    "vbscript:msgbox", "file:///C:/Windows/win.ini"):
        assert export_offline._safe_url(payload) == "#", payload


# ===========================================================================
# M-2 译文清洗不能破坏代码块
# ===========================================================================

def test_translation_keeps_shell_variables_in_code_blocks():
    """代码块里的 $ 是 shell 变量，不是数学 —— 实测被改成全角 ＄ 过。"""
    from microbench import translation

    src = "```bash\nls -la $USER\n```"
    out = translation.sanitize_translation(src)
    assert "$USER" in out, out
    assert "＄" not in out, out


def test_translation_keeps_inline_code_intact():
    from microbench import translation

    out = translation.sanitize_translation("看 `price is $5` 这行")
    assert "`price is $5`" in out, out


def test_translation_keeps_latex_inside_code_blocks():
    """代码块里的 \\( \\) 是字面量，不该被归一化成 $...$。"""
    from microbench import translation

    src = "```tex\n\\( x = 1 \\)\n```"
    out = translation.sanitize_translation(src)
    assert "\\( x = 1 \\)" in out, out


def test_translation_still_normalizes_math_outside_code():
    """代码块外的行为**不能变**（这是 KaTeX 管线依赖的）。"""
    from microbench import translation

    out = translation.sanitize_translation("公式 \\( x = 1 \\) 在这里")
    assert "$ x = 1 $" in out, out


def test_translation_still_pairs_display_math():
    from microbench import translation

    out = translation.sanitize_translation("块公式 \\[ y = 2 \\] 结束")
    assert "$$ y = 2 $$" in out, out
