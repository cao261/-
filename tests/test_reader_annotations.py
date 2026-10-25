"""阅读器批注（标注）的行为护栏。

用户诉求（2026-10-25）：
  1. 译文里的标注在**重新翻译**之后不能凭空消失 —— 记录必须留着，
     只是高亮找不到位置了（"位置失效"）。
  2. 标注列表要按**当前页**定位，不要每次都从第一页的第一条开始。

前端逻辑在单文件 microbench/static/index.html 里，这里用 node 跑**真实源码**
（microbench/_test_reader_annotations.js），保证测的是发布出去的那份代码。
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO / "microbench" / "static" / "index.html"
JS_TEST = REPO / "microbench" / "_test_reader_annotations.js"


def test_annotation_js_guardrail_passes():
    """批注的两条承诺必须成立（node 跑前端真实函数）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 前端批注护栏跳过")
    proc = subprocess.run([node, str(JS_TEST)], capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, f"批注护栏失败:\n{proc.stdout}\n{proc.stderr}"
    assert "0 failed" in proc.stdout, proc.stdout


def test_annotation_orphan_marker_is_present_in_frontend():
    """失效标记的三个落点都要在：判定、列表样式、编辑器提示。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "a.orphan = !(a.rects && a.rects.length)" in html, "缺少失效判定"
    assert "anno-item-orphan" in html, "缺少失效条目的样式"
    assert "anno-orphan-warn" in html, "缺少编辑器里的失效提示块"
    assert "笔记已保留" in html, "抽屉里没有说明笔记仍然保留"


def test_anno_drawer_scrolls_to_current_page():
    """抽屉要定位到当前页的批注，而不是永远第一条。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "el.dataset.page = String(a.page || 0)" in html, "条目没有记录页码"
    assert "block: \"start\"" in html, "抽屉没有按页定位滚动"
    # 旧写法（永远滚第一条）不能还在
    assert 'list.querySelector(".anno-item")' not in html, "抽屉仍在从第一条开始"


def test_annotation_record_is_never_dropped_on_save():
    """保存批注时不能因为高亮失效就把记录删掉。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # 只看 saveAnnotation 这个函数本身（按顶层函数的结束位置切），
    # 别把用户主动「删除批注」的路径圈进来 —— 那是合法的删除。
    start = html.index("async function saveAnnotation()")
    end = html.index("\n    }\n", start)
    body = html[start:end]
    assert "_annoState.items = " not in body, "保存时整体覆盖了 items"
    assert ".filter(" not in body, "保存时把某些批注过滤掉了"
    assert "existing.note = note" in body, "保存没有写回笔记"


def test_selection_origin_falls_back_to_range():
    """松手位置在文字外面时，要按选区本身定位（否则页码丢失 → 批注跑到章节视图）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "function _resolveSelectionOrigin(" in html, "缺少选区归属解析函数"
    # 函数内部必须有"range 兜底"这一路
    start = html.index("function _resolveSelectionOrigin(")
    end = html.index("\n    }\n", start)
    body = html[start:end]
    assert "commonAncestorContainer" in body, "选区归属没有用 range 兜底"
    assert "pick(el)" in body, "选区归属没有回退到选区公共祖先"
    # mouseup 处理器必须走这个函数，而不是自己判断 e.target
    assert "_resolveSelectionOrigin(" in html, "mouseup 处理器没有调用 _resolveSelectionOrigin"
