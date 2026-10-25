"""错误文案护栏：上游报错必须说人话，不能把英文机器话甩给用户。

用户反馈（2026-10-25）：
  界面上看到的是「流式响应异常: LLMError: HTTP 429: {...Too Many Requests...}」
  —— 一层没意义的"流式响应异常"前缀 + 一层重复的 "LLMError:" 类名
  + 一坨英文 JSON。用户看不懂，只会觉得"程序坏了"。

这里锁死两件事：
  1) 人话化：429 / 401 / 404 / 5xx 都要变成能照着做的一句话。
  2) 不重复包装：_stream_model 自己抛的 LLMError 必须原样上抛，
     不能被下面的 `except Exception` 再包一层。
"""
import re
from pathlib import Path

import pytest

from microbench import llm


LLM_PATH = Path(__file__).resolve().parents[1] / "microbench" / "llm.py"
APP_PATH = Path(__file__).resolve().parents[1] / "microbench" / "app.py"


# ---------------------------------------------------------------------------
# 1) 429：限流 vs 配额耗尽，必须分开说
# ---------------------------------------------------------------------------

def test_429_rate_limit_is_human_readable():
    """普通 429（每分钟上限）→ 说"等几十秒再试"，不是英文原文。"""
    exc = llm.LLMError(
        'HTTP 429: {"base_resp":{"status_code":1008,"status_msg":"Too Many Requests"}}',
        status_code=429,
    )
    out = llm.friendly_error_text(exc)
    assert "Too Many Requests" not in out, out
    assert "base_resp" not in out, out
    assert "HTTP 429" in out, out
    assert "限流" in out or "太频繁" in out, out
    assert "几十秒" in out, out


def test_429_quota_exhaustion_says_quota_not_rate_limit():
    """配额耗尽（Token Plan）→ 必须说"额度用完"，不能误报成"等几十秒"。

    这两种 429 的处置完全不同：限流等几十秒就行，额度耗尽只能等窗口恢复
    或换模型。提示错了用户就会一直干等。
    """
    exc = llm.LLMError(
        'HTTP 429: {"base_resp":{"status_code":2056,"status_msg":"Token Plan limit exceeded"}}',
        status_code=429,
    )
    out = llm.friendly_error_text(exc)
    assert "额度" in out, out
    assert "几十秒" not in out, out
    assert "Token Plan" not in out, out


def test_429_quota_detected_through_wrapped_message():
    """降格失败的复合消息里，额度证据可能藏在后半句 —— 整条都要参与判定。"""
    exc = llm.LLMError(
        "降格模型 (Agnes) 调用失败: HTTP 429: Too Many Requests "
        "(原主模型报错: HTTP 429: Token Plan limit exceeded)"
    )
    out = llm.friendly_error_text(exc)
    assert "额度" in out, out


def test_401_is_human_readable():
    exc = llm.LLMError('HTTP 401: {"error":{"message":"invalid api key"}}', status_code=401)
    out = llm.friendly_error_text(exc)
    assert "401" in out
    assert "API Key" in out
    assert "invalid api key" not in out


def test_404_points_at_base_url():
    exc = llm.LLMError('HTTP 404: {"error":{"message":"model not found"}}', status_code=404)
    out = llm.friendly_error_text(exc)
    assert "404" in out
    assert "base_url" in out
    assert "model not found" not in out


def test_5xx_is_human_readable():
    exc = llm.LLMError("HTTP 503: upstream busy", status_code=503)
    out = llm.friendly_error_text(exc)
    assert "503" in out
    assert "upstream busy" not in out


def test_chinese_error_passes_through_unchanged():
    """已经是人话的错误不能被二次包装（超时、降格失败等）。"""
    exc = llm.LLMError("翻译超时（超出单次请求时限 180s）")
    assert llm.friendly_error_text(exc) == "翻译超时（超出单次请求时限 180s）"


def test_prefix_before_http_is_kept():
    """前面那段中文上下文（哪条链路挂了）要保留。"""
    exc = llm.LLMError("主模型 (MiniMax-M3) 调用失败: HTTP 429: Too Many Requests")
    out = llm.friendly_error_text(exc)
    assert "MiniMax-M3" in out, out
    assert "Too Many Requests" not in out, out


def test_friendly_error_does_not_mutate_exception_message():
    """人话化是"给人看的一层"，不能改掉 str(exc) —— 内部判定还要吃原文。"""
    raw = 'HTTP 429: {"status_msg":"Token Plan limit exceeded"}'
    exc = llm.LLMError(raw, status_code=429)
    llm.friendly_error_text(exc)
    assert str(exc) == raw
    assert exc.status_code == 429


# ---------------------------------------------------------------------------
# 2) 不重复包装：_stream_model 自己抛的 LLMError 必须原样上抛
# ---------------------------------------------------------------------------

def test_stream_model_does_not_double_wrap_its_own_llmerror():
    """回归：LLMError 被 `except Exception` 再包一层 → 界面出现
    「流式响应异常: LLMError: HTTP 429: ...」。

    修法是在 except 链里加一个 `except LLMError: raise`。
    """
    src = LLM_PATH.read_text(encoding="utf-8")
    # 定位 _stream_model 函数体
    start = src.index("async def _stream_model(")
    end = src.index("\nasync def ", start + 10)
    body = src[start:end]

    # except LLMError 必须存在，且在 except Exception 之前
    assert "except LLMError:" in body, \
        "_stream_model 没有原样上抛自己抛的 LLMError"
    llm_idx = body.index("except LLMError:")
    exc_idx = body.index("except Exception as e:")
    assert llm_idx < exc_idx, \
        "except LLMError 必须排在 except Exception 之前，否则还是会被包一层"

    # 这个 except 块里只能是 raise，不能是重新包装
    seg_lines = body[llm_idx:].split("\n")
    # 跳过注释行与空行，找到第一条真正的语句
    stmt = None
    for ln in seg_lines[1:]:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stmt = stripped
        break
    assert stmt == "raise", \
        f"except LLMError 分支必须原样 raise，实际第一句是: {stmt!r}"


def test_stream_model_error_prefix_is_not_leaked_to_user():
    """'流式响应异常' 这个前缀只应出现在兜底分支，不该盖住 LLMError 本身。"""
    src = LLM_PATH.read_text(encoding="utf-8")
    start = src.index("async def _stream_model(")
    end = src.index("\nasync def ", start + 10)
    body = src[start:end]
    # 兜底分支仍然保留（真·未知异常要有说法），但前面必须有 LLMError 直通分支
    assert "流式响应异常" in body
    assert "except LLMError:" in body


# ---------------------------------------------------------------------------
# 3) 接口出口必须走人话化
# ---------------------------------------------------------------------------

def _except_blocks(src, marker):
    r"""按行切出每个 `except ... as e:` 块。

    不能用正则 `(?=\n\s*except |\n\s*\n)` 切：块内一个空行就会把块截断
    （独立审查实测：块边界正则只匹配到 1 个分支，其余全漏检）。
    这里用行号法：每块从 except 行到下一个 except / 顶层定义为止。
    """
    lines = src.split("\n")
    starts = [i for i, ln in enumerate(lines) if marker in ln]
    blocks = []
    for k, i in enumerate(starts):
        end = len(lines)
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if re.match(r"\s*except\b", s) or re.match(r"(def |async def |@app\.|class )", s):
                end = j
                break
        blocks.append("\n".join(lines[i:end]))
    return blocks


def test_app_error_exits_use_friendly_text():
    """所有给用户看的 LLMError 出口都要过 friendly_error_text。

    ⚠️ 检查方式不能只扫单行 `detail=f"..."` —— 独立审查实测：
    新增一个**多行**写法的 `except llm.LLMError` 出口时，单行正则查不到，
    断言会**假通过**。所以这里按"except 块"切分，再在块内找所有
    `{str(e)}` / `{e}` 的插值（允许 isinstance 判断后再人话化的三元式）。
    """
    src = APP_PATH.read_text(encoding="utf-8")

    # 1) except llm.LLMError 的块：任何把异常原样插值的写法都要人话化
    blocks = _except_blocks(src, "except llm.LLMError as e:")
    assert len(blocks) >= 6, \
        f"只匹配到 {len(blocks)} 个 llm.LLMError 分支，正则/切块可能失效了"

    # 计数口径：**覆盖到的出口数**（块数 + SSE 数），
    # 不是"可疑插值数" —— 全部块都正确人话化时后者会很小，门槛会误报。
    checked = len(blocks)

    for blk in blocks:
        for m in re.finditer(r"\{(str\(e\)|e)\}", blk):
            snippet = blk[max(0, m.start() - 260):m.end() + 20]
            assert "friendly_error_text" in snippet, (
                "LLMError 的出口把原始异常插值给了用户，没有走人话化: "
                f"...{snippet[-180:]}"
            )
        # 直接把异常塞进响应字段的写法（如 llm_error）也要人话化
        for m in re.finditer(r'"llm_error":\s*([^,\n]+)', blk):
            assert "friendly_error_text" in m.group(1), \
                f"LLMError 的 llm_error 字段没有走人话化: {m.group(1)}"

    # 2) except Exception 里如果判了 LLMError，同样不能原样插值
    for blk in _except_blocks(src, "except Exception as e:"):
        if "llm.LLMError" not in blk:
            continue          # 普通异常不必人话化
        checked += 1
        if "friendly_error_text" in blk:
            continue          # 已做转换
        for m in re.finditer(r"\{(str\(e\)|e)\}", blk):
            assert False, (
                "except Exception 分支处理了 LLMError，却把原始异常插值给了用户: "
                f"...{blk[max(0, m.start() - 160):m.end() + 20][-180:]}"
            )

    # 3) SSE 出口：单独查（它们不在 except 块里）
    sse = re.findall(r'yield f\'data: \{json\.dumps\(\{"error": ([^}]+)\}', src)
    assert sse, "没找到 SSE 错误出口"
    for expr in sse:
        assert "friendly_error_text" in expr, \
            f"SSE 错误出口没有走人话化: {expr}"
    checked += len(sse)
    assert checked >= 10, f"检查到的出口太少（{checked}），可能匹配失效了"


def test_llm_module_own_error_outputs_are_friendly():
    """llm.py 内部自己吞掉 LLMError 的地方也要人话化。

    独立审查抓到：summarize_paper 把 LLMError 吞掉返回 partial，
    app.py 的 `except llm.LLMError` 根本看不到它 → 英文原文直接进 API 响应。
    """
    src = LLM_PATH.read_text(encoding="utf-8")
    start = src.index("def summarize_paper(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert "friendly_error_text" in body, \
        "summarize_paper() 内部吞掉 LLMError 时没有走人话化"


def test_stderr_log_redacts_secrets():
    """原始报文进 stderr 前必须脱敏（不能把 key 写进日志）。"""
    src = LLM_PATH.read_text(encoding="utf-8")
    start = src.index("def friendly_error_text(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]
    assert "上游返回 HTTP" in body, "找不到打印上游原始报文的日志行"
    idx = body.index("上游返回 HTTP")
    window = body[max(0, idx - 260):idx + 260]
    assert "_redact_secrets" in window, \
        "打印原始报文前没有脱敏（可能把 API key 写进日志）"


def test_friendly_error_text_is_exported():
    """llm.friendly_error_text 要能从模块外面调到（app.py 用它）。"""
    assert callable(llm.friendly_error_text)
    assert "friendly_error_text" in llm.__all__ or True  # __all__ 不强制


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
