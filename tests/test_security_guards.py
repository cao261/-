"""安全护栏：密钥脱敏 / 路径抹除 / 导出器 XSS 消毒 / 降格分类。

全部来自 2026-10-25 的安全审计（独立 verifier 实测 24 种密钥形态）：
  H1 _redact_secrets 对 JSON 形态的密钥完全失效 → key 明文进 logs/desktop.log
  H2 错误出口完全不过脱敏 → 含凭据的 URL 原文送到浏览器
  H3 离线导出 HTML 存储型 XSS（恶意论文原文 → vault .md → 导出物变活代码）
  M1 GET /api/paper/download_pdf 有写副作用，CSRF 守卫只查非安全方法
  M2 内部绝对路径随 str(e) 泄露给前端
"""
import re
from pathlib import Path

import pytest

from microbench import llm

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "microbench" / "app.py"
EXPORT = ROOT / "microbench" / "export_offline.py"

SECRET = "9f8ac2b1e77d4a6fSECRETVALUE"


# ---------------------------------------------------------------------------
# 1) 密钥脱敏：不能再有漏网形态
# ---------------------------------------------------------------------------

SECRET_SHAPES = [
    ('{"api_key":"%s"}' % SECRET, "JSON api_key"),
    ('{"apiKey":"%s"}' % SECRET, "JSON 驼峰 apiKey"),
    ('{"access_token":"%s"}' % SECRET, "JSON access_token"),
    ('{"refresh_token":"%s"}' % SECRET, "JSON refresh_token"),
    ('{"token":"%s"}' % SECRET, "JSON token"),
    ('{"secret":"%s"}' % SECRET, "JSON secret"),
    ('{"password":"%s"}' % SECRET, "JSON password"),
    ("Authorization: %s" % SECRET, "裸 Authorization"),
    ("Authorization: Basic %s" % SECRET, "Basic 认证"),
    ("http://user:%s@gw.internal/v1" % SECRET, "URL userinfo(带密码)"),
    ("http://user@gw.internal/v1", "URL userinfo(只有用户名)"),
    ("-----BEGIN RSA PRIVATE KEY-----\n%s\n-----END RSA PRIVATE KEY-----" % SECRET,
     "PEM 私钥块"),
    ("sk-abcdefgh12345", "sk- 前缀"),
    ("Bearer abcdefgh12345678", "Bearer"),
    ("api_key=%s" % SECRET, "query string 形态"),
]


@pytest.mark.parametrize("text,label", SECRET_SHAPES, ids=[s[1] for s in SECRET_SHAPES])
def test_redact_secrets_blocks_all_shapes(text, label):
    """密钥不能以任何形态留在文本里。"""
    out = llm._redact_secrets(text)
    assert SECRET not in out, f"{label} 没被脱敏: {out}"
    if label.startswith("sk-") or label == "Bearer":
        assert "abcdefgh12345" not in out and "abcdefgh12345678" not in out, out


def test_friendly_error_text_never_leaks_credentials():
    """前端可见的文本里不能有凭据。"""
    exc = llm.LLMError(
        "主模型 (MiniMax-M3) 调用失败: Invalid URL "
        "'https://user:sk-SUPERSECRET1234567890@gw.internal/v1/chat/completions'"
    )
    out = llm.friendly_error_text(exc)
    assert "SUPERSECRET" not in out, out
    assert "sk-SUPERSECRET" not in out, out


def test_friendly_error_text_masks_local_paths():
    """内部绝对路径不能送到前端。"""
    exc = llm.LLMError(
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'E:\\zaiwu\\个人工作台\\logs\\xxx.json'"
    )
    out = llm.friendly_error_text(exc)
    assert "E:\\zaiwu" not in out, out
    assert "个人工作台" not in out, out


def test_error_exits_redact_before_sending_to_browser():
    """同步/异步路径的异常出口必须脱敏（审计 H2 点名的 4 处）。"""
    src = (ROOT / "microbench" / "llm.py").read_text(encoding="utf-8")
    for bad in (
        'f"代理连接失败: {e}"',
        'f"网络连接失败 (重试 {max_retries} 次): {e}"',
        'f"请求异常: {e}"',
    ):
        assert bad not in src, f"异常出口没有脱敏: {bad}"


def test_app_error_details_use_safe_err():
    """app.py 里回前端的异常消息要走 _safe_err（脱敏 + 抹路径）。"""
    src = APP.read_text(encoding="utf-8")
    # 只看 except Exception / except llm.LLMError 块里的原始插值
    for m in re.finditer(r"detail=f\"([^\"]*?)\{(str\(e\)|e)\}\"", src):
        snippet = src[max(0, m.start() - 260):m.end() + 20]
        assert "_safe_err(e)" in snippet or "friendly_error_text" in snippet, \
            f"异常消息没走 _safe_err: ...{snippet[-160:]}"


# ---------------------------------------------------------------------------
# 2) 离线导出器：XSS 消毒
# ---------------------------------------------------------------------------

def test_export_sanitizer_strips_scripts():
    from microbench import export_offline
    out = export_offline.markdown_to_html("<script>alert('XSS')</script>")
    assert "<script" not in out.lower(), out
    assert "alert" not in out, out


def test_export_sanitizer_strips_event_handlers():
    from microbench import export_offline
    out = export_offline.markdown_to_html("[点我](javascript:alert(1)){onclick=alert('X')}")
    assert "onclick" not in out.lower(), out
    assert "javascript:" not in out.lower(), out


def test_export_sanitizer_strips_iframe_and_onerror():
    from microbench import export_offline
    out = export_offline.markdown_to_html("<iframe src='http://evil'></iframe><img src=x onerror=alert(1)>")
    assert "<iframe" not in out.lower(), out
    assert "onerror" not in out.lower(), out


def test_export_sanitizer_keeps_normal_content():
    """消毒不能把正常的表格/粗体/公式样式弄坏。"""
    from microbench import export_offline
    out = export_offline.markdown_to_html(
        "| a | b |\n|---|---|\n| 1 | 2 |\n\n**粗** <span class=\"katex\">k</span>"
    )
    assert "<table>" in out, out
    assert "<strong>" in out, out
    assert 'class="katex"' in out, out


def test_export_sanitizer_blocks_dangerous_urls():
    from microbench import export_offline
    out = export_offline.markdown_to_html('<a href="javascript:alert(1)">x</a>')
    assert "javascript:" not in out.lower(), out


# ---------------------------------------------------------------------------
# 3) M1：download_pdf 不能是 GET
# ---------------------------------------------------------------------------

def test_download_pdf_is_not_a_get_endpoint():
    """它有写副作用（mkdir + 写文件），GET 的话 CSRF 守卫拦不住跨站触发。"""
    src = APP.read_text(encoding="utf-8")
    assert '@app.get("/api/paper/download_pdf")' not in src, \
        "download_pdf 有写副作用，不能是 GET（CSRF 守卫只查非安全方法）"
    assert '@app.post("/api/paper/download_pdf")' in src, \
        "download_pdf 应该是 POST"


# ---------------------------------------------------------------------------
# 4) 降格分类：界面必须能说清"为什么降格"
# ---------------------------------------------------------------------------

def test_degrade_text_distinguishes_causes():
    """额度耗尽 / 限流 / 没出正文 —— 三种处置方式不同，不能混为一谈。"""
    quota = llm._degrade_text("quota")
    rate = llm._degrade_text("rate_limit")
    noout = llm._degrade_text("no_output")
    assert "配额" in quota and "等" in quota, quota
    assert "限流" in rate and "几十秒" in rate, rate
    assert "配额用完" not in rate and "额度用完" not in rate, rate  # 限流≠额度
    assert "配额用完" not in noout and "额度用完" not in noout, noout  # 没出正文≠额度
    assert quota != rate != noout


def test_status_exposes_degrade_kind_and_text():
    """status 要把分类和人话原因交给前端（否则界面只能说"额度用完了"）。"""
    st = llm.model_manager.get_status()
    assert "degraded_kind" in st, sorted(st)
    assert "degraded_text" in st, sorted(st)


def test_no_output_does_not_globally_degrade():
    """「这次没产出正文」是单次任务的问题，不该改全局状态。

    否则用户会看到"降格"却不知道自己额度明明还有（真实反馈）。
    """
    src = (ROOT / "microbench" / "llm.py").read_text(encoding="utf-8")
    # 同步路径：no_usable_output 只做本次 failover，不能触发 mark_degraded
    assert re.search(
        r"is_quota\s*=\s*model_manager\.is_quota_exhausted_error\([^)]*\)\s*\n"
        r"\s*if fallback\[.configured.\] and is_quota:\s*\n"
        r"\s*model_manager\.mark_degraded\(res, kind=\"quota\"\)",
        src,
    ), "同步路径的 mark_degraded 仍然被 no_usable_output 牵连"


def test_stream_path_no_loose_429_degrade():
    """流式路径不能用「消息里有 429 就算配额」这种宽松判定。

    status 为 None 表示这不是 HTTP 状态码错误（网络/协议错误），
    按配额处理会让一次网络抖动把全局降格 15 分钟。
    """
    src = (ROOT / "microbench" / "llm.py").read_text(encoding="utf-8")
    assert 'else ("429" in err_msg or "quota" in err_msg.lower())' not in src, \
        "流式路径还在用宽松的 429 判定（网络错误会被当成配额耗尽 → 全局降格）"


def test_frontend_does_not_hardcode_quota_message():
    """界面不能写死"额度用完了" —— 要照后端给的分类说人话。"""
    src = (ROOT / "microbench" / "static" / "index.html").read_text(encoding="utf-8")
    assert "主模型额度用完了" not in src, \
        "界面还写死了『主模型额度用完了』（真实原因可能是限流或没出正文）"
    assert "degraded_text" in src, "界面没有用后端给的 degraded_text"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
