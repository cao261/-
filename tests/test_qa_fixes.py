# -*- coding: utf-8 -*-
"""回归测试：划词答疑的三个修复。

1) 空正文自动恢复：思考把 max_tokens 烧光时，流式路径必须自己重试，
   而不是给前端一个空答案（用户看到的就是「一直转圈然后什么都没有」）。
2) 多模态兜底：请求里带页面截图时，消息必须是 OpenAI 兼容的
   content 数组（text + image_url），否则 M3 根本收不到图。
3) 侧边栏收缩：最小化按钮要收成窄竖条，而不是 460x46 的大饼。
"""
from pathlib import Path
import re

import pytest

VAULT = Path(__file__).resolve().parent.parent
INDEX = VAULT / "microbench" / "static" / "index.html"
APP = VAULT / "microbench" / "app.py"
LLM = VAULT / "microbench" / "llm.py"


# ---------------------------------------------------------------------------
# 1) 空正文自动恢复
# ---------------------------------------------------------------------------

def test_stream_model_raises_on_empty_body():
    """流结束时若一个**可见字符**都没吐出来，必须报错而不是静默结束。

    注意必须按「非空白字符」计数：上游曾经返回的正文是两个换行符
    ("\\n\\n")，按原始长度算是非零，判定就漏掉了 —— 结果空答案照样
    漏给前端，用户还是看到「一直转圈然后什么都没有」。
    """
    src = LLM.read_text(encoding="utf-8")
    assert "body_visible_chars == 0" in src, "流式路径没有检测空正文"
    # 计数必须忽略空白
    assert ".strip()" in src, "空正文检测没有忽略空白字符"
    # 报错要带 TRUNCATED_MARKER，上层据此判断「可以关思考重试」
    assert "TRUNCATED_MARKER" in src and "TRUNCATED_NO_BODY" in src


def test_empty_body_guard_covers_stream_without_done_sentinel():
    """守卫必须放在**循环之后**，不能只在 "[DONE]" 分支里。

    实测真实 API：当思考把 max_tokens 烧光时，上游发完
    finish_reason="length" 就直接断开 SSE 连接，**根本不发 "[DONE]"**。
    守卫只写在 "[DONE]" 分支里就永远触发不了 —— 空正文照样被当成
    成功答案交给前端，用户看到的就是「一直转圈然后什么都没有」。
    """
    src = LLM.read_text(encoding="utf-8")

    # 找到流式解析循环的结束位置
    loop_anchor = 'async for line in resp.aiter_lines():'
    assert loop_anchor in src, "找不到 SSE 解析循环"
    loop_at = src.index(loop_anchor)

    # 守卫必须出现在循环之后（缩进层级更浅，属于 with 块内、循环外）
    guard_at = src.index("body_visible_chars == 0", loop_at)
    guard_block = src[guard_at:guard_at + 700]
    # 守卫后面紧跟的应是 with 块之外的 except，而不是循环体内的 return
    assert "except asyncio.CancelledError" in guard_block, \
        "守卫不在循环结束之后 —— [DONE] 分支里的守卫覆盖不到断流的情况"

    # [DONE] 分支里不应该再放守卫（那样覆盖不到断流）
    done_at = src.index('if data == "[DONE]":')
    done_block = src[done_at:done_at + 700]
    assert "body_visible_chars == 0" not in done_block, \
        "守卫仍然只写在 [DONE] 分支里，断流时不会触发"


def test_chat_stream_async_retries_with_thinking_disabled():
    """空正文时必须自动用 thinking=disabled 重试一次。"""
    src = LLM.read_text(encoding="utf-8")
    assert "disable_thinking=True" in src, "空正文没有触发关思考重试"
    # 重试条件必须同时要求：没产出正文 + 还没流式输出过任何内容
    assert "TRUNCATED_NO_BODY in err_msg" in src, "空正文重试条件缺失"


def test_stream_model_supports_thinking_toggle():
    """_stream_model 必须支持 disable_thinking 参数。"""
    src = LLM.read_text(encoding="utf-8")
    assert re.search(
        r"async def _stream_model\([\s\S]*?disable_thinking: bool = False", src
    ), "_stream_model 缺少 disable_thinking 参数"
    assert 'payload["thinking"] = {"type": "disabled"}' in src, \
        "disable_thinking 没有真正下发到上游"


# ---------------------------------------------------------------------------
# 2) 多模态兜底
# ---------------------------------------------------------------------------

def test_explain_selection_accepts_page_image():
    src = APP.read_text(encoding="utf-8")
    assert "page_image" in src, "请求模型没有 page_image 字段"


def test_page_image_is_sent_as_image_url_content():
    """带图时 user 消息必须是 content 数组（text + image_url）。"""
    src = APP.read_text(encoding="utf-8")
    assert '"type": "image_url"' in src, "没有构造 image_url 内容块"
    assert "data:image/png;base64," in src, "图片没有用 data URL 编码"
    # 而且这个数组要真的被发出去
    assert '"role": "user", "content": user_content' in src, \
        "构造好的多模态消息没有传给模型"


def test_render_page_png_exists():
    """必须有把 PDF 页渲染成 PNG 的能力。"""
    from microbench.paper_text import render_page_png
    assert callable(render_page_png)


def test_render_page_png_works_on_real_pdf():
    """真实 PDF 必须能渲染出合法 PNG。"""
    import glob
    pdfs = glob.glob(
        str(VAULT / "01_Literature" / "04_Survey_综述" / "01_论文原文_PDF" / "*JACERS*")
    )
    if not pdfs:
        pytest.skip("vault 里没有 JACERS 样本")
    from microbench.paper_text import render_page_png
    data = render_page_png(pdfs[0], 3)
    assert data[:4] == b"\x89PNG", "渲染结果不是 PNG"
    assert len(data) > 5000, "渲染结果太小，多半是空白页"


def test_frontend_captures_and_sends_page_image():
    """前端必须真的截图并随请求发出。"""
    src = INDEX.read_text(encoding="utf-8")
    assert "_captureSelectionPageImage" in src, "前端没有截图函数"
    assert "page_image: pageImage" in src, "截图没有随请求发出"
    # 截图失败不能挡住答疑
    assert "return null;   // 截图失败不该挡住答疑" in src or \
           "return null;" in src, "截图失败没有兜底"


# ---------------------------------------------------------------------------
# 3) 最小化 = 侧边栏
# ---------------------------------------------------------------------------

def test_minimized_panel_becomes_narrow_rail():
    """最小化后必须是一条 44px 的窄竖条，不是 460x46 的大饼。"""
    src = INDEX.read_text(encoding="utf-8")
    assert ".reader-qa-panel.qa-collapsed" in src, "没有收起态样式"
    # 收起态有多条规则（面板本体 + 标题竖排 + 隐藏正文），逐条检查
    blocks = re.findall(
        r"\.reader-qa-panel\.qa-collapsed[^{]*\{([\s\S]*?)\}", src
    )
    assert blocks, "收起态样式块没找到"
    panel_block = blocks[0]
    assert "width: 44px" in panel_block, "收起后宽度不是 44px"
    # 旧规则会把高度压到 46px 但宽度不变 —— 必须不再依赖它
    assert "height: auto !important" in panel_block, "收起后高度没放开"
    # 竖排标题（在带后代选择器的另一条规则里）
    assert "writing-mode: vertical-rl" in src, "收起后标题没有竖排"
    # 收起后必须把正文藏起来
    assert re.search(
        r"\.reader-qa-panel\.qa-collapsed\s+\.qa-body[^{]*\{[^}]*display:\s*none",
        src,
    ), "收起后正文没有隐藏"


def test_minimize_toggle_uses_collapsed_class():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(r"function minimizeReaderQAPanel\(\)\s*\{([\s\S]*?)\n    \}", src)
    assert m, "minimizeReaderQAPanel 没找到"
    body = m.group(1)
    assert "qa-collapsed" in body, "最小化没有切换到收起态"
    # 展开时要还原位置/尺寸
    assert "restoreWidth" in body and "restoreHeight" in body, \
        "展开时没有还原之前的尺寸"


def test_open_panel_clears_collapsed_state():
    src = INDEX.read_text(encoding="utf-8")
    m = re.search(r"function openReaderQAPanel\([^)]*\)\s*\{([\s\S]*?)\n    \}", src)
    assert m, "openReaderQAPanel 没找到"
    assert 'classList.remove("qa-collapsed")' in m.group(1), \
        "打开面板时没有清掉收起态"


def test_collapsed_head_is_clickable_to_expand():
    """竖条上不方便点小按钮，所以点标题栏也要能展开。"""
    src = INDEX.read_text(encoding="utf-8")
    assert "_qaCollapsedHeadClick" in src, "没有收起态的点击处理"
    assert 'onclick="_qaCollapsedHeadClick(event)"' in src, \
        "标题栏没有绑定收起态的点击"
