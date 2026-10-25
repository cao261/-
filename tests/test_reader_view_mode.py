"""阅读器视图切换护栏：PDF 模式必须藏章节视图区、留工具条。

回归（2026-10-25 浏览器实测抓到的既有 bug）：
  switchReaderMode() 用 `document.querySelector("#reader-modal > div > div:nth-child(2)")`
  定位章节视图区 —— 但那个位置是 .reader-topbar，不是 .reader-body。
  后果：切到 PDF 模式时藏掉的是 topbar →
    - PDF 模式的整条工具条（缩放 / 全篇重译 / 导出 / 同步 / 模型下拉框）消失；
    - 真正的章节视图区从没被藏 → 章节视图叠在 PDF 上面。
"""
import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "microbench" / "static" / "index.html"


def _src():
    return INDEX.read_text(encoding="utf-8")


def _code_only():
    """剥掉注释，只留代码。

    必须剥：修这个 bug 时我在注释里写了旧写法（nth-child），
    直接全文搜会把注释当成"还在用位置型选择器"。
    """
    src = _src()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def test_reader_body_has_id_anchor():
    """章节视图区必须有 id —— 定位靠 id，不靠位置。"""
    src = _src()
    assert re.search(r'<div class="reader-body" id="reader-body">', src), \
        "章节视图区缺少 id=\"reader-body\"（只能退回位置型选择器 → 会错位）"


def test_switch_reader_mode_does_not_use_nth_child():
    """switchReaderMode 不能用 nth-child 定位 —— 位置型选择器是隐式契约。"""
    src = _code_only()
    start = src.index("function switchReaderMode(")
    end = src.index("\n    }", start)
    body = src[start:end]
    assert "nth-child" not in body, \
        "switchReaderMode 还在用 nth-child 定位（加一个元素就会静默指向错的目标）"
    assert 'getElementById("reader-body")' in body, \
        "switchReaderMode 应该用 #reader-body 定位章节视图区"


def test_nth_child_is_not_used_anywhere_in_reader_modal():
    """整个 index.html 里都不该再用位置型选择器找 modal 里的区块。"""
    src = _code_only()
    hits = [m.start() for m in re.finditer(r'#reader-modal[^\n]*nth-child', src)]
    assert not hits, \
        f"还有 {len(hits)} 处用 #reader-modal > ... > nth-child 定位（位置型契约）"


def test_ai_activity_widgets_live_after_pdf_view():
    """AI 活动浮层和徽标必须挂在浮动区块（child 3 之后），不能塞进 topbar。

    塞进 topbar 的话，PDF 模式下 topbar 一藏，徽标也跟着藏 →
    用户点不开浮层 → 「✕ 取消」永远够不着。
    """
    src = _src()
    # topbar 区间
    tb_start = src.index('<div class="reader-topbar">')
    tb_end = src.index("<!-- ------- child 2: section view")
    topbar = src[tb_start:tb_end]
    assert "ai-activity-pill" not in topbar, \
        "AI 活动徽标又回到 topbar 里了（PDF 模式会被连带隐藏）"

    # 浮动区块里必须有它
    float_start = src.index("<!-- ---------------------- Floating affordances")
    float_end = src.index("</div>", src.index('id="reader-qa-floating-btn"'))
    floating = src[float_start:float_end + 200]
    assert "id=\"ai-activity-pill\"" in floating, \
        "AI 活动徽标不在浮动区块里"
    assert "id=\"ai-panel\"" in floating, \
        "AI 活动浮层不在浮动区块里"


def test_ai_activity_pill_is_fixed_positioned():
    """徽标必须 fixed —— 否则它是 .reader-shell 的 flex item，会被排到屏幕最下面。"""
    src = _src()
    m = re.search(r"\.ai-activity-pill\s*\{(.*?)\}", src, re.S)
    assert m, "找不到 .ai-activity-pill 的 CSS"
    assert "position: fixed" in m.group(1), \
        ".ai-activity-pill 不是 fixed 定位（会被 flex 布局排到屏幕底部）"


def test_ai_panel_auto_opens_when_task_starts():
    """有任务时浮层要自动展开 —— 徽标只是入口，用户不会想到去点它。"""
    src = _src()
    start = src.index("function _renderAiActivity(")
    end = src.index("\n    function ", start + 10)
    body = src[start:end]
    assert "_aiPanelAutoOpened" in body, \
        "浮层没有「有任务自动展开」的逻辑"
    assert 'classList.add("open")' in body, \
        "有任务时没有自动展开浮层"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
