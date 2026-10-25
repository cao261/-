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
    """取**脚本代码**（剥掉注释，但保留字符串字面量）。

    三层过滤，缺一不可：
    1) 只取 `<script>` 块 —— HTML 注释（结构契约说明、修复说明）里
       写着旧写法（nth-child），不排除就会误判"还在用位置型选择器"；
    2) 剥 JS 注释（// 和 /* */）；
    3) **保留字符串字面量** —— 剥字符串会把
       `getElementById("reader-body")` 变成 `getElementById("")`，
       断言全废；而用正则直接剥 `//` 又会把 `"http://x"` 里的 `//`
       当注释起点，把后面整行吃掉（独立审查实测能骗过断言）。
    所以这里用一个简易扫描器，自己识别字符串 / 行注释 / 块注释。
    """
    src = _src()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
    assert blocks, "没找到 <script> 块"
    js = "\n".join(blocks)

    out = []
    i, n = 0, len(js)
    while i < n:
        c = js[i]
        if c in "\"'`":
            q = c
            out.append(c)
            i += 1
            while i < n:
                if js[i] == "\\":
                    out.append(js[i:i + 2])
                    i += 2
                    continue
                out.append(js[i])
                if js[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if js.startswith("//", i):
            j = js.find("\n", i)
            i = n if j < 0 else j
            continue
        if js.startswith("/*", i):
            j = js.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


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
    """AI 活动浮层必须在浮动区块（child 3 之后）；徽标在 topbar 里。

    两者位置不同，理由不同：
    - 浮层是 fixed 且比 topbar 高 → 放 topbar 里会被 topbar 裁掉，
      必须放浮动区块（child 3 之后，符合结构契约）。
    - 徽标是 inline（跟随工具条流）→ **不能 fixed 在右上角**：
      PDF 模式下 topbar 会折行到 ~104px，fixed 在右上会压住「关闭」按钮，
      真人点击点不动（2026-10-25 独立审查用 CDP 实测：
      elementsFromPoint 里徽标排在按钮之前）。
      放回 topbar 的正常流里，两种模式都可见、也不压任何控件。
    """
    src = _src()
    topbar = src[src.index('<div class="reader-topbar">'):src.index("<!-- ------- child 2: section view")]
    assert "ai-activity-pill" in topbar, \
        "AI 活动徽标不在 topbar 里（fixed 定位会压住「关闭」按钮）"

    float_start = src.index("<!-- ---------------------- Floating affordances")
    float_end = src.index('id="reader-qa-floating-btn"')
    floating = src[float_start:float_end]
    assert 'id="ai-panel"' in floating, \
        "AI 活动浮层不在浮动区块里（child 3 之后）"
    assert 'id="ai-activity-pill"' not in floating, \
        "AI 活动徽标不该放浮动区块（会脱离工具条流、且 fixed 会压控件）"


def test_ai_activity_pill_is_not_fixed():
    """徽标不能是 fixed —— fixed 在右上角会压住 topbar 最右的「关闭」按钮。"""
    src = _src()
    m = re.search(r"\.ai-activity-pill\s*\{(.*?)\}", src, re.S)
    assert m, "找不到 .ai-activity-pill 的 CSS"
    assert "position: fixed" not in m.group(1), \
        ".ai-activity-pill 不能 fixed（会压住 PDF 模式的「关闭」按钮）"


def test_qa_panel_zindex_is_above_ai_panel():
    """QA 面板要盖在 AI 活动浮层之上。

    AI 浮层自动展开时会压住 QA 面板标题栏（模型徽标 / 最小化按钮），
    而 QA 面板要靠标题栏拖动 —— 盖住了就既点不动也拖不动。
    """
    src = _src()
    qa = re.search(r"\.reader-qa-panel\s*\{(.*?)\}", src, re.S)
    ai = re.search(r"\.ai-panel\s*\{(.*?)\}", src, re.S)
    assert qa and ai, "找不到 .reader-qa-panel / .ai-panel 的 CSS"

    def _z(block):
        m = re.search(r"z-index:\s*(\d+)", block)
        return int(m.group(1)) if m else 0

    assert _z(qa.group(1)) > _z(ai.group(1)), \
        (f"QA 面板 z-index({_z(qa.group(1))}) 必须高于 AI 浮层"
         f"({_z(ai.group(1))})，否则标题栏会被盖住、面板拖不动")


def test_ai_panel_auto_opens_when_task_starts():
    """有任务时浮层要自动展开 —— 徽标只是入口，用户不会想到去点它。

    语义：**每个新任务展开一次**（按任务 id 记）。
    独立审查抓到：用"是否有任务"的单一标志位时，PDF 队列连续翻译期间
    用户手动关一次，整个队列都不再自动弹（与注释不符）。
    """
    src = _src()
    start = src.index("function _renderAiActivity(")
    end = src.index("\n    function ", start + 10)
    body = src[start:end]
    assert "_aiPanelOpenedFor" in body, \
        "浮层没有「有任务自动展开」的逻辑"
    assert 'classList.add("open")' in body, \
        "有任务时没有自动展开浮层"
    assert "_aiPanelOpenedFor.clear()" in body, \
        "任务结束后没有清空「已展开」记录 → 下一批任务不会自动弹"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
