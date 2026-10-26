"""专注模式（Zen / Focus）的结构护栏。

用户诉求（2026-10-27）：
  「本机屏幕不大，所有内容都铺在屏幕上很乱 —— 做个极简版本，
    只有论文页和译文页，其他功能隐藏，需要时滑出来。」

这个文件锁的是**结构契约**（源码级）。行为级在
`microbench/_test_zen_mode.js`（node 跑 index.html 里的真实源码 + 变异自检）。

盯住的几条红线：
  1. `.zen-handle` 必须放在 `.reader-shell` 的**最后** —— 插在前面会把
     「child 1 = topbar / child 2 = 章节视图 / child 3 = PDF 视图」这个
     位置型契约挤偏（README 里明确写过 JS 曾因此定位错目标）。
  2. **不能**给 `.reader-shell` 加 `position: relative` —— 那会把里面
     `position: absolute` 的浮层（批注编辑器 / 笔记抽屉）的包含块换掉，
     位置全乱。
  3. 顶栏在专注模式下必须是 `position: fixed` + `translateY` 滑出，
     **不能**是 `display: none` —— 那样就再也滑不回来了。
  4. 默认必须是**关**的 —— 不影响现有用户。
  5. 窄屏媒体查询要能压住基础的两列规则，否则小屏反而更挤。
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / "microbench" / "static" / "index.html"
JS_TEST = REPO / "microbench" / "_test_zen_mode.js"


def _src():
    return INDEX.read_text(encoding="utf-8")


def _code_only():
    """取**脚本代码**（剥掉 HTML 注释与 JS 注释，但保留字符串字面量）。

    必须剥注释：index.html 里好几处**注释**写着旧写法
    （`#reader-modal > div > div:nth-child(2)`），那是「不要再用位置型选择器」
    的说明，不是代码 —— 不剥就会误判。
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


def _css_block(selector: str, src: str = None) -> str:
    """取出某个选择器的 CSS 规则块正文（selector 不带 '{'）。"""
    s = src if src is not None else _src()
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\}", s, re.S)
    assert m, f"找不到 {selector} 的 CSS 规则"
    return m.group(1)


def test_zen_node_guardrail_passes():
    """行为级护栏必须通过（node 跑前端真实函数，含变异自检）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 专注模式行为护栏跳过")
    proc = subprocess.run([node, str(JS_TEST)], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, f"专注模式护栏失败:\n{proc.stdout}\n{proc.stderr}"
    assert "FAIL 0" in proc.stdout, proc.stdout
    # 变异自检必须真的抓到东西 —— 否则这套断言是恒真的
    m = re.search(r"变异捕获\s+(\d+)/(\d+)", proc.stdout)
    assert m, "护栏输出里没有变异自检结果 —— 断言可能是恒真的：\n" + proc.stdout
    caught, total = int(m.group(1)), int(m.group(2))
    assert total > 0, "变异自检一个都没配 —— 等于没写：\n" + proc.stdout
    assert caught == total, \
        f"变异只抓到 {caught}/{total} —— 有一套断言是恒真的：\n" + proc.stdout


def test_zen_handle_is_the_last_child_of_reader_shell():
    """`.zen-handle` 必须在所有浮动区块之后 —— 位置型契约。"""
    src = _src()
    handle = src.index('id="zen-handle"')
    for anchor in ('id="sync-modal"', 'id="anno-drawer"', 'id="reader-qa-panel"',
                   'id="ai-panel"', 'id="reader-pdf-view"', 'id="reader-body"',
                   '<div class="reader-topbar">'):
        assert handle > src.index(anchor), (
            f".zen-handle 插到了 {anchor} 前面 —— 会挤偏 .reader-shell 的位置型契约"
        )
    # 它得在 .reader-shell 里面（不是跑到 modal 外面去了）
    shell_end = src.rindex("</div>\n  </div>\n\n\n</body>")
    assert handle < shell_end, ".zen-handle 跑到 .reader-shell 外面了"


def test_reader_shell_did_not_gain_position_relative():
    """不能给 .reader-shell 加 position:relative —— 会换掉浮层包含块。"""
    block = _css_block(".reader-shell")
    assert "position: relative" not in block, \
        ".reader-shell 加了 position:relative —— 批注编辑器 / 笔记抽屉的包含块会变，位置全乱"


def test_zen_topbar_is_fixed_and_slides_out():
    """专注模式的顶栏要能滑出来 —— 必须是 fixed + translateY，不能 display:none。"""
    block = _css_block("#reader-modal.zen .reader-topbar")
    assert "position: fixed" in block, "顶栏必须是 fixed（脱离文档流，正文才撑得满）"
    assert "translateY" in block, "顶栏要靠 translateY 滑出/滑入"
    assert "display: none" not in block, \
        "顶栏不能 display:none —— 那样就再也滑不回来了"
    # 滑出的那一档
    open_block = _css_block("#reader-modal.zen.zen-bar-open .reader-topbar")
    assert "translateY(0)" in open_block, "工具条滑出那一档该是 translateY(0)"


def test_zen_hides_the_chrome_but_keeps_the_selection_bubble():
    """默认收起的是「界面外壳」，不是「划词气泡」—— 气泡是临时出现的，不算 clutter。"""
    src = _src()
    assert "#reader-modal.zen .reader-nav" in src, "专注模式没收起章节导航"
    assert "#reader-modal.zen .reader-aux" in src, "专注模式没收起 AI 提问区"
    assert "#reader-modal.zen #reader-qa-floating-btn" in src, "专注模式没收起划词答疑悬浮钮"
    # 划词气泡不能被一起收掉 —— 划词答疑是核心工作流
    assert "zen .reader-selection-bubble" not in src, \
        "专注模式把划词气泡也藏了 —— 划词答疑就进不去了"


def test_zen_css_selectors_really_match_elements():
    """专注模式的每条 CSS 选择器都要**真的能匹配到元素**。

    血泪：原来写的是 `#reader-modal.zen .reader-qa-floating-btn`，
    可那个按钮身上的类叫 `.reader-floating-copilot-btn`
    （`reader-qa-floating-btn` 是它的 **id**）——
    规则匹配不到任何元素，**静默失效**：代码看着对、页面上按钮还在，
    而且「源码里有这条规则」这种断言照样绿。

    所以这里反向验证：把 CSS 里用到的每个类 / id 拿去 HTML 里找，
    找不到就是死选择器。
    动态类（JS 用 classList 切出来的）从 JS 里现算，不用手写白名单。
    """
    src = _src()
    body = src[src.index("<body"):]

    # 只扫 **CSS**（<style> 块）—— JS 里也有 "reader-modal" 字样，
    # 混进来会把 getElementById("reader-modal") 当成选择器。
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", src, re.S))
    assert css, "没找到 <style> 块"

    # 动态类：从 JS 的 classList.add/remove/toggle 参数里抽出来
    js = _code_only()
    dynamic = set()
    for args in re.findall(r"classList\.(?:add|remove|toggle)\(([^)]*)\)", js):
        dynamic.update(re.findall(r'"([^"]+)"', args))

    bad = []
    for sel in re.findall(r"#reader-modal[^{}]*\{", css):
        for cls in re.findall(r"\.([A-Za-z][\w-]*)", sel):
            if cls in dynamic:
                continue
            if not re.search(r'class="[^"]*\b' + re.escape(cls) + r'\b', body):
                bad.append(f"类 .{cls} 在 HTML 里不存在（选择器静默失效）")
        for elem_id in re.findall(r"#([A-Za-z][\w-]*)", sel):
            if elem_id == "reader-modal":
                continue
            if f'id="{elem_id}"' not in body:
                bad.append(f"id #{elem_id} 在 HTML 里不存在（选择器静默失效）")

    assert not bad, "；".join(sorted(set(bad)))


def test_zen_qa_float_button_is_hidden_by_id_not_by_wrong_class():
    """划词答疑悬浮钮必须用 id 命中 —— 用错类名会静默失效（真踩过）。"""
    src = _src()
    assert 'id="reader-qa-floating-btn" class="reader-floating-copilot-btn"' in src, \
        "划词答疑悬浮钮的 id / class 变了 —— 专注模式的隐藏规则要跟着改"
    assert "#reader-modal.zen #reader-qa-floating-btn" in src, \
        "专注模式应该用 id 命中划词答疑悬浮钮"
    assert "#reader-modal.zen .reader-qa-floating-btn" not in src, \
        "`.reader-qa-floating-btn` 是 id 不是类名 —— 写成类选择器会静默失效"


def test_zen_is_off_by_default():
    """默认必须关 —— 不影响现有用户。"""
    src = _src()
    assert "let _zenMode = false;" in src, "_zenMode 默认应为 false"
    assert "let _zenPref = false;" in src, "_zenPref 默认应为 false"
    # #reader-modal 不能默认带 zen 类
    assert not re.search(r'<div id="reader-modal"[^>]*class="[^"]*\bzen\b', src), \
        "#reader-modal 默认带了 zen 类 —— 会一上来就进专注模式"


def test_zen_entry_button_is_always_visible():
    """进入专注的按钮要一直可见（不能是 zen-only）。"""
    src = _src()
    m = re.search(r'<button[^>]*id="zen-toggle-btn"[^>]*>', src, re.S)
    assert m, "顶栏缺少「专注」入口按钮"
    assert "zen-only" not in m.group(0), \
        "「专注」入口按钮不能是 zen-only —— 那样没进专注就永远进不去"


def test_zen_only_buttons_are_hidden_outside_zen():
    """zen-only 的按钮在非专注模式下要藏起来。"""
    src = _src()
    assert "#reader-modal:not(.zen) .zen-only" in src, \
        "没有把 zen-only 按钮在非专注模式下藏起来的规则"
    for btn in ("zen-exit-btn", "zen-nav-btn", "zen-single-btn", "zen-aux-btn"):
        m = re.search(r'<button[^>]*id="' + btn + r'"[^>]*>', src, re.S)
        assert m, f"缺少按钮 {btn}"
        assert "zen-only" in m.group(0), f"{btn} 必须带 zen-only 类"


def test_zen_preference_is_persisted():
    """「上次开着」要能被记住 —— 否则每次开论文都要再点一次。"""
    src = _src()
    assert "zenPref: !!_zenPref" in src, "zenPref 没存进 localStorage"
    assert "if (s.zenPref === true) _zenPref = true;" in src, "zenPref 没从 localStorage 恢复"


def test_zen_single_column_panes_are_mutually_exclusive():
    """单栏模式下「论文页 / 译文页」必须互斥，且**不能把自己也藏了**。

    血泪：原来写的是 `#reader-modal.zen.zen-single #pdf-viewer-container { display:none }`
    （无条件生效）→ 切到「论文页」时 PDF 容器仍被藏 → **一片空白**。
    单元测试只查类名切换，抓不到 CSS 的后果 —— 这次是浏览器实测抓到的，
    所以在这里把「互斥」写成结构断言，让它以后改不回去。
    """
    src = _src()
    hide_pdf = _css_block("#reader-modal.zen.zen-single:not(.zen-show-orig) #pdf-viewer-container")
    assert "display: none" in hide_pdf, "单栏看译文时应该把 PDF 栏藏起来"

    hide_tr = _css_block("#reader-modal.zen.zen-single.zen-show-orig #pdf-translations-container")
    assert "display: none" in hide_tr, "单栏看论文页时应该把译文栏藏起来"

    # 旧的无条件写法不能还在（它会把「切到论文页」变成空白页）
    assert re.search(r"#reader-modal\.zen\.zen-single\s+#pdf-viewer-container", src) is None, \
        "隐藏 PDF 容器的规则不能无条件生效 —— 切到论文页会一片空白"

    # 章节视图那两栏也必须是互斥的两条
    # ⚠️ 血泪二（浏览器实测抓到）：原来写的是
    #   `.reader-pane:not(.reader-pane-alt) { display:none }`（永远藏原文）
    #   `.zen-show-orig .reader-pane-alt { display:none }`（show-orig 时藏译文）
    # 两条同时生效 → 切到「论文页」时**两栏都没了**（章节视图一片空白）。
    assert "display: none" in _css_block("#reader-modal.zen.zen-single .reader-pane"), \
        "单栏看译文时应该把章节视图的原文栏藏起来"
    assert "display: block" in _css_block("#reader-modal.zen.zen-single .reader-pane-alt"), \
        "单栏时章节视图的译文栏应该显示出来"
    assert "display: none" in _css_block("#reader-modal.zen.zen-single.zen-show-orig .reader-pane-alt"), \
        "单栏看论文页时应该把译文栏藏起来"
    show_orig = _css_block(
        "#reader-modal.zen.zen-single.zen-show-orig .reader-pane:not(.reader-pane-alt)")
    assert "display: block" in show_orig, \
        "单栏看论文页时**必须把原文栏放回来** —— 否则两栏全被藏，一片空白"


def test_reader_shell_is_full_width_not_fit_content():
    """.reader-shell 必须满宽 —— 它是 #reader-modal 的 flex item。

    血泪三（浏览器实测抓到，探针实测 `.reader-shell` 只有 616px / 视口 1280px）：
    `#reader-modal` 打开时是 `display:flex`（行向），`.reader-shell` 是它的
    flex item，`width:auto` 会走 **fit-content 收缩** —— 壳的宽度 = 内容的
    max-content，不是视口宽度。平时顶栏在文档流里、内容很宽，壳被撑满，
    看不出问题；一旦顶栏脱离文档流（专注模式 `position:fixed`），
    壳里只剩 `.pdf-split`，壳立刻缩成半屏 → PDF 右半屏全空。
    """
    block = _css_block(".reader-shell")
    assert re.search(r"width:\s*100%", block), \
        ".reader-shell 没写 width:100% —— 它会被 flex 收缩成 fit-content（半屏）"
    assert re.search(r"min-width:\s*0", block), \
        ".reader-shell 缺 min-width:0 —— min-content 撑大时 flex item 不肯缩"


def test_pdf_relayout_watches_the_container_not_only_the_window():
    """PDF 重绘不能只听 window resize。

    血泪四（浏览器实测抓到）：阅读器打开前是 `display:none`，
    `#pdf-pages-list` 的宽度是 0 → 首次渲染走兜底 520px；
    等阅读器真的显示出来，**窗口尺寸一点没变** → window resize 不触发
    → 画布永远停在 520px，右半屏全空。
    所以必须直接观察列表容器（ResizeObserver）。
    """
    src = _src()
    assert "ResizeObserver" in src, \
        "没观察 PDF 列表容器 —— 阅读器从 display:none 变可见时窗口尺寸没变，PDF 画布会停在兜底宽度"
    assert "_initPdfWidthObserver()" in src, "宽度观察器没被初始化"
    assert "_pdfRelayoutBusy" in src, \
        "重绘期间没有「忙」标记 —— 清空列表触发的尺寸回调会再排一次重绘，自激循环"


def test_zen_nav_and_aux_switch_to_section_view_first():
    """「章节」「提问」在 PDF 模式下必须先切到章节视图。

    章节导航（.reader-nav）和 AI 提问区（.reader-aux）都住在「章节视图」
    容器 #reader-body 里，而 PDF 双栏模式下那个容器整体 display:none ——
    这时候点这两个按钮**什么也不会发生**（点了像按钮坏了）。
    """
    src = _src()
    assert 'switchReaderMode("section")' in src, \
        "PDF 模式下点「章节」「提问」什么也不会发生 —— 章节导航/提问区住在章节视图里"


def test_narrow_screen_overrides_zen_two_columns():
    """窄屏媒体查询必须能压住专注模式的两列规则。"""
    src = _src()
    found = False
    for m in re.finditer(r"@media\s*\(max-width:\s*860px\)\s*\{(.*?)\n    \}", src, re.S):
        block = m.group(1)
        if "#reader-modal.zen .reader-body" in block and "grid-template-columns: 1fr" in block:
            found = True
            break
    assert found, \
        "窄屏媒体查询里没有压住专注模式的两列规则 —— 小屏反而还是两栏，更挤"


def test_zen_does_not_break_reader_positional_contract():
    """不能引入位置型选择器（#reader-modal > ... > nth-child）。

    只查**代码** —— 注释里写着旧写法是「不要再用」的说明，不是代码。
    """
    src = _code_only()
    hits = re.findall(r"#reader-modal[^\n]*nth-child", src)
    assert not hits, f"还有 {len(hits)} 处用位置型选择器定位 reader-modal 里的区块"


def test_zen_shortcuts_do_not_steal_input_keys():
    """Z / Esc 不能在输入框里生效（否则打字会误触发）。"""
    src = _src()
    start = src.index("function _initZenMode()")
    end = src.index("\n    }\n", start)
    body = src[start:end]
    for tag in ('"input"', '"textarea"', '"select"'):
        assert tag in body, f"快捷键处理没有跳过 {tag} —— 打字会误触发"
    assert "isContentEditable" in body, "快捷键处理没有跳过 contenteditable"


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
