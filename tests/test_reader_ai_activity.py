"""「AI 活动」进度可视化的护栏。

用户诉求（2026-10-25）："他有可能等了半天然后什么也没输出" ——
一个不动的转圈，用户分不清"在跑"还是"卡了"。

所以做了：统一任务登记处 + 每 500ms 刷新"已用时 + 阶段" +
工具栏徽标与可取消的面板（翻译 / 章节翻译 / 划词答疑三处都接上了）。

测法：node 跑 index.html 里**真实的那一段源码**（按段落标记切出来），
再补几条源码级断言，防止有人把某个入口的登记/收尾删掉。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO / "microbench" / "static" / "index.html"
JS_TEST = REPO / "microbench" / "_test_reader_ai_activity.js"


def test_ai_activity_registry_guardrail():
    """任务登记处的行为：登记 / 阶段 / 收尾 / 取消 / 历史。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— AI 活动护栏跳过")
    proc = subprocess.run([node, str(JS_TEST)], capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, f"AI 活动护栏失败:\n{proc.stdout}\n{proc.stderr}"
    assert "0 failed" in proc.stdout, proc.stdout


def test_ui_has_activity_pill_and_panel():
    """工具栏徽标 + 浮层面板必须在 DOM 里。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="ai-activity-pill"' in html, "工具栏缺少 AI 活动徽标"
    assert 'id="ai-panel"' in html, "缺少 AI 活动面板"
    assert "toggleAiPanel" in html, "面板没有开关函数"
    assert "ai-panel-body" in html, "面板没有内容容器"


def test_all_ai_entrypoints_register_and_close_tasks():
    """翻译 / 章节翻译 / 划词答疑，三处都要登记任务、并且都有收尾。

    漏了收尾 = 面板里永远挂着一个"还在跑"的任务；
    漏了登记 = 用户看不到这个 AI 请求在干什么。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")

    def body_of(fn_name):
        start = html.index("function " + fn_name)
        # 到下一个顶层函数结束
        end = html.index("\n    }\n", start)
        return html[start:end]

    # 划词答疑（askLLMStream）
    ask = body_of("askLLMStream")
    assert "_aiTaskStart" in ask, "划词答疑没有登记任务"
    assert "_aiTaskEnd" in ask, "划词答疑没有收尾"

    # 按页翻译（translatePdfPage）
    tr = body_of("translatePdfPage")
    assert "_aiTaskStart" in tr, "按页翻译没有登记任务"
    assert "_aiTaskEnd" in tr, "按页翻译没有收尾"

    # 章节翻译（translateSection）
    sec = body_of("translateSection")
    assert "_aiTaskStart" in sec, "章节翻译没有登记任务"
    assert "_aiTaskEnd" in sec, "章节翻译没有收尾"


def test_translation_tag_shows_live_elapsed_time():
    """页面上的翻译状态必须显示实时已用时（不然又是一个不动的转圈）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "ai-inline-status" in html, "没有行内实时状态样式"
    assert "_aiFormatMs(el)" in html, "翻译状态没有显示已用时"
    assert "ai-beat" in html, "实时状态没有心跳指示"


def test_slow_tasks_get_warned():
    """慢到一定程度要变色预警，而不是让人干等。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "_AI_WARN_MS" in html and "_AI_DANGER_MS" in html, "缺少慢任务预警阈值"
    assert "很慢" in html or "没有动静" in html, "缺少慢任务提示文案"
