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


def _except_blocks(src, marker):
    """按行切出每个 `except ... as e:` 块（到下一个 except / 顶层定义为止）。

    不能用正则 `(?=\\n\\s*except |\\n\\s*\\n)` 切：块内一个空行就会把块截断
    （独立审查实测：9 个分支只匹配到 1 个，其余全漏检）。
    """
    lines = src.split("\n")
    starts = [i for i, ln in enumerate(lines) if marker in ln]
    blocks = []
    for i in starts:
        end = len(lines)
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if re.match(r"\s*except\b", s) or re.match(r"(def |async def |@app\.|class )", s):
                end = j
                break
        blocks.append("\n".join(lines[i:end]))
    return blocks


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
    """app.py 里回前端的异常消息要走 _safe_err（脱敏 + 抹路径）。

    ⚠️ 上一轮的正则只认 `detail=f"...{e}"`（插值紧跟引号），
    恰恰漏掉了它要防的那一种形态 `{e})"`（独立审查实测：
    改回 `{e}` 护栏依然全绿）。现在改成**按 except 块切分**再在块内找插值。
    """
    src = APP.read_text(encoding="utf-8")
    checked = 0
    for blk in _except_blocks(src, "except Exception as e:"):
        if "llm.LLMError" not in blk:
            continue          # 普通异常不必人话化
        if "_safe_err(e)" in blk or "friendly_error_text" in blk:
            continue          # 已做转换
        for m in re.finditer(r"\{(str\(e\)|e)[^}]*\}", blk):
            checked += 1
            assert False, (
                "except Exception 分支处理了 LLMError，却把原始异常插值给了用户: "
                f"...{blk[max(0, m.start() - 160):m.end() + 20][-180:]}"
            )
    assert checked == 0, "有 except 块把原始异常直接插给了前端"


def test_mask_paths_handles_spaces_and_keeps_urls():
    """路径遮罩：带空格的 Windows 路径要整段遮住；URL 不能被误伤。"""
    # 带空格 → 必须整段遮住（旧正则遇空格即停 → 只遮一半）
    out = llm.mask_paths(r"打开失败: C:\Program Files (x86)\Microsoft\Edge\msedge.exe")
    assert "Program Files" not in out, f"带空格的路径只遮了一半: {out}"
    assert "msedge.exe" not in out, out
    # URL → 不能把路径部分吃掉
    out = llm.mask_paths("请求失败: https://example.com/api/v1/chat")
    assert "example.com" in out, f"URL 被误伤: {out}"
    # 普通内容不能误伤
    for text in ("2026/10/25", "分数 1/2", "a/b"):
        assert llm.mask_paths(text) == text, f"误伤: {text!r} -> {llm.mask_paths(text)!r}"


def test_badge_does_not_show_fallback_rpm():
    """徽标不能显示备用模型的限流 RPM（用户会读成自己被限流）。"""
    src = (ROOT / "microbench" / "static" / "index.html").read_text(encoding="utf-8")
    assert "降格 ${rpmInfo.rpm} RPM" not in src, \
        "徽标还在显示备用模型的限流 RPM"
    assert "kindLabel" in src, "徽标应该显示降格分类（配额/限流/无正文）"


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
# 2b) 消毒器的绕过与保真（独立审查实测在真实 Edge 里会执行）
# ---------------------------------------------------------------------------

CDATA_CONTAINER_PAYLOADS = [
    "<xmp><img src=x onerror=alert(1)></xmp>",
    "<noembed><img src=x onerror=alert(1)></noembed>",
    "<noframes><img src=x onerror=alert(1)></noframes>",
    "<plaintext><img src=x onerror=alert(1)>",
    "<title><img src=x onerror=alert(1)></title>",
    "<textarea><img src=x onerror=alert(1)></textarea>",
]


@pytest.mark.parametrize("payload", CDATA_CONTAINER_PAYLOADS,
                         ids=[p.split(">")[0][1:] for p in CDATA_CONTAINER_PAYLOADS])
def test_export_sanitizer_blocks_cdata_container_bypass(payload):
    """CDATA/RCDATA 容器标签（<xmp>/<noembed>/<noframes>/<plaintext>/<title>）

    Python 3.13 的 HTMLParser 把它们当 CDATA/RCDATA 容器 →
    容器里的内容被当**纯文本**交给 handle_data → 如果不转义就原样透传，
    绕过全部标签/属性/协议过滤。

    独立审查用真实 Edge 实测：`<xmp><img src=x onerror=...></xmp>`
    在打开导出物的瞬间就执行了（无需用户交互）。
    """
    from microbench import export_offline
    out = export_offline.markdown_to_html(payload)
    low = out.lower()
    assert "onerror" not in low, f"事件属性存活: {out}"
    assert "<img" not in low, f"img 标签存活: {out}"
    assert "alert" not in low, f"载荷内容存活: {out}"


def test_export_sanitizer_preserves_html_entities():
    """实体必须保真 —— 丢掉的话 'AT&T' 会变成 'ATT'（英文论文里很常见）。"""
    from microbench import export_offline
    for src, want in [("AT&T 增长", "AT&amp;T"), ("x &lt; y", "&lt;"),
                      ("&nbsp;空格", "&nbsp;"), ("&#65; 大写", "&#65;")]:
        out = export_offline.markdown_to_html(src)
        assert want in out, f"实体丢失: {src!r} -> {out}"


def test_export_sanitizer_keeps_code_blocks():
    """代码块要保留（<pre><code class=...>）。"""
    from microbench import export_offline
    out = export_offline.markdown_to_html("```py\nprint(1)\n```")
    assert "<pre>" in out, out
    assert "<code" in out, out
    assert "print(1)" in out, out


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


def test_no_output_does_not_globally_degrade_permanently(monkeypatch):
    """「这次没产出正文」不能永久压着主模型。

    ⚠️ 这条护栏原来是用**源码正则匹配**写的 → 双向失效（独立审查实测）：
      - 把原 bug 原样接回去 → 护栏仍绿（假阴性，没保护它声称保护的行为）
      - 只把变量 `is_quota` 改名 → 护栏变红（假阳性，无害重构也报）
    现在改成**行为级断言**：mock 主模型返回"未产出正文" → 调 chat() →
    断言降格是**短 TTL**（会自己到期），而不是永久。

    ⚠️ 备用模型必须"已配置"才会进降格分支 —— 这里用 monkeypatch 单独设
    （**不能**在 conftest 模块级 setdefault：那会挡住 .env 里的真 key，
    让"真调模型"的端点测试全部拿到占位符而变红）。
    """
    from microbench import llm as llm_mod

    monkeypatch.setenv("AGNES_API_KEY", "test-dummy-agnes-key")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "test-dummy-agnes-key")
    # ⚠️ 主模型也要"已配置"：chat() 里 `if not is_degraded and primary["configured"]`
    #    这一整块才是降格逻辑所在的分支 —— 主模型没配置就直接走备用模型，
    #    根本不会进降格逻辑（干净检出上就是这样"看起来没降格"）。
    monkeypatch.setenv("LLM_API_KEY", "test-dummy-primary-key")

    calls = {"n": 0}

    def fake_invoke(cfg, messages, max_tokens, temperature, timeout, json_mode,
                    max_retries=2, disable_thinking=False, budget=None,
                    deadline=None, **kw):
        calls["n"] += 1
        if cfg.get("is_fallback"):
            return True, "备用模型答出来了", 200
        # 主模型：永远"未产出正文"
        return False, "模型返回空内容", 200

    orig = llm_mod._invoke_with_budget_escalation
    orig_async = llm_mod._invoke_model_async
    orig_invoke = llm_mod._invoke_model
    llm_mod._invoke_with_budget_escalation = fake_invoke
    llm_mod._invoke_model_async = fake_invoke
    # ⚠️ _invoke_model 也要 mock：chat() 内部"空正文 → 抬预算重试"走的是它，
    #    不 mock 就会**真的发一次网络请求**（可能还真成功 → 断言假失败）。
    llm_mod._invoke_model = fake_invoke
    llm_mod.model_manager.mark_recovered()
    try:
        try:
            llm_mod.chat([{"role": "user", "content": "hi"}], max_tokens=100)
        except Exception:
            pass
        st = llm_mod.model_manager.get_status()
        assert st["is_degraded"] is True, \
            "空正文时应该临时降格（否则主模型每次都被重试、白烧思考额度）"
        assert st.get("degraded_kind") == "no_output", \
            f"降格分类不对: {st.get('degraded_kind')}"
        # 短 TTL：不能是永久的（永久 = 15 分钟通用探针才恢复）
        assert getattr(llm_mod.model_manager, "degraded_until", 0) > 0, \
            "空正文的降格必须有 TTL（否则一次任务的毛病压 15 分钟）"
    finally:
        llm_mod._invoke_with_budget_escalation = orig
        llm_mod._invoke_model_async = orig_async
        llm_mod._invoke_model = orig_invoke
        llm_mod.model_manager.mark_recovered()


def test_no_output_degrade_expires_on_its_own(monkeypatch):
    """短 TTL 降格到期后自动恢复主模型。"""
    from microbench import llm as llm_mod

    monkeypatch.setenv("AGNES_API_KEY", "test-dummy-agnes-key")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "test-dummy-agnes-key")
    llm_mod.model_manager.mark_recovered()
    llm_mod.model_manager.mark_degraded("空正文", kind="no_output", ttl_seconds=0.05)
    assert llm_mod.model_manager.get_status()["is_degraded"] is True
    import time as _t
    _t.sleep(0.2)
    llm_mod.model_manager.should_opportunistic_probe()
    assert llm_mod.model_manager.get_status()["is_degraded"] is False, \
        "TTL 到期没有自动恢复"
    llm_mod.model_manager.mark_recovered()


def test_mark_recovered_clears_degrade_kind(monkeypatch):
    """恢复后不能还带着旧的降格分类（否则 API 会说'额度用完了'但已恢复）。"""
    from microbench import llm as llm_mod
    monkeypatch.setenv("AGNES_API_KEY", "test-dummy-agnes-key")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "test-dummy-agnes-key")
    llm_mod.model_manager.mark_degraded("x", kind="quota")
    llm_mod.model_manager.mark_recovered()
    st = llm_mod.model_manager.get_status()
    assert st["is_degraded"] is False
    assert not st.get("degraded_kind"), \
        f"恢复后 degraded_kind 没清: {st.get('degraded_kind')!r}"


def test_stream_path_no_loose_429_degrade():
    """流式路径不能用「消息里有 429 就算配额」这种宽松判定。

    status 为 None 表示这不是 HTTP 状态码错误（网络/协议错误），
    按配额处理会让一次网络抖动把全局降格 15 分钟。
    """
    src = (ROOT / "microbench" / "llm.py").read_text(encoding="utf-8")
    assert 'else ("429" in err_msg or "quota" in err_msg.lower())' not in src, \
        "流式路径还在用宽松的 429 判定（网络错误会被当成配额耗尽 → 全局降格）"


def test_real_429_keeps_status_code():
    """真 429 时 LLMError 必须带 status_code=429。

    这是上面那条收紧判定的**前提**：如果 status 丢了，
    `getattr(e, "status_code", None)` 是 None → 走"不降格"分支 →
    **真配额耗尽就永远不再降格**（用户会一直卡在失败的请求上）。
    """
    import asyncio

    class _Resp:
        status_code = 429

        async def aread(self):
            return b'{"base_resp":{"status_code":2056,' \
                   b'"status_msg":"Token Plan limit exceeded"}}'

        async def aiter_lines(self):
            return
            yield  # pragma: no cover

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def stream(self, method, url, json=None, headers=None):
            return _Resp()

    orig_client = llm._get_async_client
    orig_acquire = llm._acquire_limiter_async

    async def _noop(*a, **k):
        return None

    llm._get_async_client = lambda timeout=60: _Client()
    llm._acquire_limiter_async = _noop
    try:
        async def run():
            cfg = {"api_key": "k", "base_url": "http://x", "model": "m"}
            try:
                async for _ in llm._stream_model(
                    cfg, [{"role": "user", "content": "hi"}], 100, 0.3, 30, None
                ):
                    pass
            except llm.LLMError as exc:
                return exc
            return None

        exc = asyncio.run(run())
    finally:
        llm._get_async_client = orig_client
        llm._acquire_limiter_async = orig_acquire

    assert exc is not None, "429 应该抛 LLMError"
    assert getattr(exc, "status_code", None) == 429, \
        f"429 的 LLMError 没带 status_code: {exc}"
    # 带得上 → 精确判定能认出"配额耗尽" → 仍会正确降格
    assert llm.model_manager.is_quota_exhausted_error(429, str(exc)) is True, \
        "配额耗尽的报文没被认出来（会导致真配额耗尽不降格）"


def test_rate_limit_messages_are_not_treated_as_quota():
    """限流报文不能被当成配额耗尽（否则一次限流就全局降格 15 分钟）。"""
    rate_msgs = [
        '{"base_resp":{"status_code":1008,"status_msg":"Too many requests"}}',
        '{"base_resp":{"status_code":1008,"status_msg":"rate limit exceeded"}}',
        '{"base_resp":{"status_code":1008,"status_msg":"requests per minute exceeded"}}',
        # 2026-10-27：上游会用"计费档位"描述限流，命中了 "billing" 这个
        # 歧义词 → 旧逻辑判成配额耗尽 → 全局降格 15 分钟。
        # 实测报错原文："429 billing tier rate limit exceeded (org-xxx)"
        '429 billing tier rate limit exceeded',
        # "resource exhausted" 单独出现时是并发/限流语义（无 resource 前缀时）
        '429 rate limit: exhausted retries',
    ]
    for msg in rate_msgs:
        assert llm.model_manager.is_quota_exhausted_error(429, msg) is False, \
            f"限流报文被误判成配额耗尽: {msg}"


def test_quota_message_is_still_detected():
    """真配额耗尽仍要认得出来（别把降格功能改没了）。"""
    quota_msg = ('{"base_resp":{"status_code":2056,'
                 '"status_msg":"Token Plan limit exceeded"}}')
    assert llm.model_manager.is_quota_exhausted_error(429, quota_msg) is True


def test_frontend_does_not_hardcode_quota_message():
    """界面不能写死"额度用完了" —— 要照后端给的分类说人话。"""
    src = (ROOT / "microbench" / "static" / "index.html").read_text(encoding="utf-8")
    assert "主模型额度用完了" not in src, \
        "界面还写死了『主模型额度用完了』（真实原因可能是限流或没出正文）"
    assert "degraded_text" in src, "界面没有用后端给的 degraded_text"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
