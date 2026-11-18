# -*- coding: utf-8 -*-
"""「选中一个标题 → 让 AI 总结这一小节」的上下文链路（2026-11-18）。

用户原话：
  「我一般的使用场景是选中一个标题，让 ai 先帮我总结一下这一小节的内容，
    要注意这个上下文的给法，使其能做到给我合适的阅读学习帮助」

这一轮落地的东西：
  ① `paper_text.extract_outline()`：从 PDF 里抽**小节大纲**
     （标题 + 本小节正文 + 本小节的公式坐标）；
  ② `GET /api/paper/outline`：把大纲交给前端；
  ③ 前端命中标题 → 上下文换成「本小节完整正文」，**跳过 LLM 路由**，
     载荷里带上 `selection_kind / heading_title / section_pages /
     formula_rects_with_page`；
  ④ 服务端在提示里明确「用户选中的是本小节标题」，并按 section 档截断。

⚠️ 这里**一律不读源码文本当断言**（`assert "xxx" in src`）：
   元护栏 tests/test_guard_efficacy.py 把那一类钉死不许再涨 ——
   那种断言改坏行为也照样绿。
   → 本文件**真打端点**、**真抽 PDF**，断言**可观察的后果**。
"""
import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS_TEST = REPO / "microbench" / "_test_qa_section_scope.js"

sys.path.insert(0, str(REPO))

fitz = pytest.importorskip("fitz")

PDF_REL = "01_Literature/04_Survey_综述/01_论文原文_PDF/小节上下文样本.pdf"
PAGE_W, PAGE_H = 612.0, 792.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _make_paper(path: Path) -> None:
    """造一份 2 页样本：有编号式小节标题、有编号开头的**正文句**、有参考文献段。

    为什么要专门放一条「3 Measurements were performed on the samples.」：
    它长得跟标题一模一样（编号 + 大写开头）→ 一旦被当成标题，
    真正的小节就被从中间劈成两半 → 用户问「总结这一节」，AI 拿到的是半节。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((72, 90), "I. Introduction", fontsize=11, fontname="helv")
    page.insert_text((72, 120), "Magnetoelectric laminates are studied in this work.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 140), "The magnetostrictive layer is bonded to a piezoelectric one.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 180), "II. Experimental Procedure", fontsize=11, fontname="helv")
    page.insert_text((72, 210), "Samples were prepared by co-sintering the two phases.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 230), "The coupling coefficient is measured with a lock-in amplifier.",
                     fontsize=11, fontname="helv")

    page2 = doc.new_page(width=PAGE_W, height=PAGE_H)
    page2.insert_text((72, 90), "3 Measurements were performed on the samples.",
                      fontsize=11, fontname="helv")
    page2.insert_text((72, 120), "The samples were annealed at 900 degrees for two hours.",
                      fontsize=11, fontname="helv")
    page2.insert_text((72, 160), "References", fontsize=11, fontname="helv")
    page2.insert_text((72, 190), "[1] A. Author and B. Writer, Journal of Things, 12, 34 (2009).",
                      fontsize=11, fontname="helv")
    doc.save(str(path))
    doc.close()


def _capture_messages(client, sandbox, monkeypatch, payload_extra=None):
    """真打答疑端点，拦下**真正发给模型**的 messages（不调真模型）。"""
    from microbench import llm as llm_mod

    captured = {}

    async def fake_chat_stream_async(**kwargs):
        captured["messages"] = kwargs.get("messages") or []
        yield "（桩回答）"

    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat_stream_async", fake_chat_stream_async)

    _make_paper(sandbox / PDF_REL)

    payload = {
        "selected_text": "II. Experimental Procedure",
        "question": "帮我总结这一小节",
        "paper_title": "小节上下文样本",
        "source_type": "pdf",
        "pdf_relative_path": PDF_REL,
        "page_num": 1,
    }
    payload.update(payload_extra or {})

    res = client.post("/api/paper/explain_selection_stream", json=payload)
    assert res.status_code == 200, res.text
    assert "messages" in captured, "模型压根没被调用 —— 桩没生效"
    return captured["messages"]


def _user_content(messages):
    for m in messages:
        if m.get("role") == "user":
            return m.get("content")
    raise AssertionError("没有 user 消息")


def _all_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text")


def _image_blocks(content):
    if not isinstance(content, list):
        return []
    return [b for b in content
            if isinstance(b, dict) and b.get("type") == "image_url"]


def _png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    return (int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"))


# ---------------------------------------------------------------------------
# 1) 抽取层：小节大纲
# ---------------------------------------------------------------------------
def test_outline_finds_numbered_headings_and_keeps_the_body(sandbox):
    """编号式标题（"I. Introduction"）要被认出来，正文要归到**它自己那一节**。"""
    from microbench.paper_text import extract_outline

    pdf = sandbox / PDF_REL
    _make_paper(pdf)
    outline = extract_outline(pdf)

    titles = [s["title"] for s in outline["sections"]]
    assert "I. Introduction" in titles, "没找到一级标题：" + repr(titles)
    assert "II. Experimental Procedure" in titles, "没找到二级标题：" + repr(titles)

    intro = next(s for s in outline["sections"] if s["title"] == "I. Introduction")
    assert "magnetostrictive" in intro["text"], "本节正文没进来"
    # 下一节的内容**不能**混进本节
    assert "Experimental Procedure" not in intro["text"], "本节正文串到下一节了"

    proc = next(s for s in outline["sections"]
                if s["title"] == "II. Experimental Procedure")
    assert "lock-in amplifier" in proc["text"], "本节正文没进来"
    assert proc["page_start"] == 1 and proc["page_end"] == 2, \
        "小节跨页范围不对：%s-%s" % (proc["page_start"], proc["page_end"])


def test_outline_does_not_split_a_section_on_a_numbered_body_sentence(sandbox):
    """⚠ 核心防误判：编号开头的**正文句**不能被当成标题。

    误判的代价不对称（AGENTS.md §4.6 同款思路）：
      把正文当标题 → 真正的小节被劈成两半 → 用户问「总结这一节」，
      AI 拿到的是**半节**；漏判只是多给一点上下文。
    """
    from microbench.paper_text import extract_outline

    pdf = sandbox / PDF_REL
    _make_paper(pdf)
    outline = extract_outline(pdf)
    titles = [s["title"] for s in outline["sections"]]
    assert not any("Measurements" in t for t in titles), \
        "编号开头的正文句被当成标题了：" + repr(titles)

    proc = next(s for s in outline["sections"]
                if s["title"] == "II. Experimental Procedure")
    assert "annealed at 900 degrees" in proc["text"], \
        "小节被那句正文截断了 —— AI 会只拿到半节"


def test_outline_stops_at_the_reference_list(sandbox):
    """参考文献不该灌进上下文（它是书目，不是这一节的正文）。"""
    from microbench.paper_text import extract_outline

    pdf = sandbox / PDF_REL
    _make_paper(pdf)
    outline = extract_outline(pdf)
    body = "\n".join(s["text"] for s in outline["sections"])
    assert "Journal of Things" not in body, "参考文献混进了正文"
    # 最后一节应该是参考文献**之前**的那一节
    assert outline["sections"][-1]["title"] == "II. Experimental Procedure", \
        "最后一节不对：" + outline["sections"][-1]["title"]


def test_outline_is_empty_but_not_broken_on_a_broken_pdf(sandbox):
    """坏文件不能把整个端点带崩（返回空大纲即可）。"""
    from microbench.paper_text import extract_outline

    bad = sandbox / "01_Literature" / "broken.pdf"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a pdf at all")
    outline = extract_outline(bad)
    assert outline["sections"] == [], "坏 PDF 不该产出小节"


# ---------------------------------------------------------------------------
# 2) 端点：/api/paper/outline
# ---------------------------------------------------------------------------
def test_outline_endpoint_returns_sections(client, sandbox):
    _make_paper(sandbox / PDF_REL)
    res = client.get("/api/paper/outline", params={"relative_path": PDF_REL})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["pages"] == 2, "页数不对"
    titles = [s["title"] for s in data["sections"]]
    assert "II. Experimental Procedure" in titles, repr(titles)
    sec = next(s for s in data["sections"]
               if s["title"] == "II. Experimental Procedure")
    assert sec["chars"] > 0, "小节正文是空的"


def test_outline_endpoint_is_cached(client, sandbox):
    """同一篇论文不该反复重算（pdfplumber 逐页，8 页要 ~2s）。"""
    _make_paper(sandbox / PDF_REL)
    first = client.get("/api/paper/outline", params={"relative_path": PDF_REL})
    assert first.status_code == 200
    second = client.get("/api/paper/outline", params={"relative_path": PDF_REL})
    assert second.status_code == 200
    assert second.json().get("cached") is True, "第二次没走缓存"


def test_outline_endpoint_rejects_paths_outside_the_vault(client):
    res = client.get("/api/paper/outline",
                     params={"relative_path": "../../Windows/win.ini"})
    assert res.status_code == 400, res.text


def test_outline_endpoint_404s_on_a_missing_pdf(client):
    res = client.get("/api/paper/outline",
                     params={"relative_path": "01_Literature/根本没有.pdf"})
    assert res.status_code == 404, res.text


# ---------------------------------------------------------------------------
# 3) 答疑端点：标题模式
# ---------------------------------------------------------------------------
def test_heading_selection_tells_the_model_this_is_a_section(client, sandbox, monkeypatch):
    """提示里必须写明「用户选中的是本小节标题」，否则模型会以为用户划了半句话。"""
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "selection_kind": "heading",
        "heading_title": "II. Experimental Procedure",
        "section_pages": [1, 2],
        "force_scope": "section",
        "surrounding_context": "【本小节完整正文】:\nSamples were prepared by co-sintering.",
    })
    text = _all_text(_user_content(messages))
    assert "选中的是一个小节标题" in text, "提示里没写明这是标题：\n" + text[:600]
    assert "II. Experimental Procedure" in text, "提示里没有小节标题：\n" + text[:600]
    assert "本小节" in text, "提示里没提「本小节」：\n" + text[:600]


def test_heading_selection_sends_every_page_formula_of_the_section(
        client, sandbox, monkeypatch):
    """⚠ 小节常常跨页 → 公式图必须**按页**给，不能只给选中页那一张。

    反向用例：只给选中页的公式图 → 小节后一半页的推导模型根本看不到
    （AGENTS.md §4.6：错公式比没公式危险得多）。
    """
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "selection_kind": "heading",
        "heading_title": "II. Experimental Procedure",
        "section_pages": [1, 2],
        "force_scope": "section",
        "surrounding_context": "【本小节完整正文】:\n本小节正文",
        "formula_rects_with_page": [
            [1, 72.0, 200.0, 300.0, 230.0],
            [2, 72.0, 200.0, 300.0, 230.0],
        ],
    })
    blocks = _image_blocks(_user_content(messages))
    # 1 张整页图 + 2 张公式图（第 1 页、第 2 页各一张）
    assert len(blocks) == 3, "应有 1 张整页图 + 2 张公式图，实际 %d 张" % len(blocks)
    text = _all_text(_user_content(messages))
    assert "第 2 页" in text, "公式图没标页码：\n" + text[-800:]


def test_formula_crops_of_a_section_are_high_resolution(client, sandbox, monkeypatch):
    """跨页公式图也得是**高清**的（scale=4），不然模型还是读不出上下标。"""
    rect = [72.0, 200.0, 300.0, 230.0]
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "selection_kind": "heading",
        "heading_title": "II. Experimental Procedure",
        "section_pages": [1, 2],
        "formula_rects_with_page": [[1, *rect], [2, *rect]],
    })
    blocks = _image_blocks(_user_content(messages))
    assert len(blocks) == 3, blocks
    for b in blocks:
        url = b["image_url"]["url"]
        assert url.startswith("data:image/png;base64,"), url[:40]
    page_png = base64.b64decode(blocks[0]["image_url"]["url"].split(",", 1)[1])
    crop_png = base64.b64decode(blocks[1]["image_url"]["url"].split(",", 1)[1])
    _, page_h = _png_size(page_png)
    _, crop_h = _png_size(crop_png)
    assert crop_h >= 3.5 * (rect[3] - rect[1]), \
        ("公式图只有 %dpx 高（应约 %dpx）—— 裁图没按 scale=4 渲染"
         % (crop_h, 4 * (rect[3] - rect[1])))
    # 同一条公式在**整页图**上只有多少高（整页图会被降采样）
    px_per_pt = page_h / PAGE_H
    on_page = (rect[3] - rect[1]) * px_per_pt
    assert crop_h >= 1.8 * on_page, \
        ("公式图 %dpx vs 整页图里的 %.0fpx —— 几乎没有变清楚"
         % (crop_h, on_page))


def test_section_scope_caps_the_context(client, sandbox, monkeypatch):
    """section 档的上限必须是 16K（前端徽标显示的字数要跟真发出去的一致）。"""
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "force_scope": "section",
        "surrounding_context": "X" * 40000,
    })
    text = _all_text(_user_content(messages))
    assert text.count("X") <= 16000, "section 档没按 16K 截断：%d" % text.count("X")


def test_normal_selection_has_no_heading_instruction(client, sandbox, monkeypatch):
    """普通划词（不是标题）→ 不许出现「选中的是一个小节标题」的约束。"""
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "selection_kind": "body",
        "surrounding_context": "普通上下文",
    })
    text = _all_text(_user_content(messages))
    assert "选中的是一个小节标题" not in text, \
        "普通划词也被当标题了：\n" + text[:600]
    assert "该片段所在的章节/页面上下文背景" in text, \
        "上下文标签不对：\n" + text[:600]


def test_broken_section_formula_rects_do_not_break_the_qa(client, sandbox, monkeypatch):
    """畸形坐标 → 不裁图，答疑照常进行。"""
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "selection_kind": "heading",
        "heading_title": "II. Experimental Procedure",
        "formula_rects_with_page": [
            # ⚠ 用**字符串** "NaN" 而不是浮点 NaN：
            #   浮点 NaN 连请求体都发不出去（httpx 用 json.dumps(allow_nan=False)
            #   → 直接抛 "Out of range float values are not JSON compliant"），
            #   那样测的是客户端序列化，不是服务端的校验。
            ["NaN", 1.0, 2.0, 3.0, 4.0],
            ["Infinity", 1.0, 2.0, 3.0, 4.0],
            [0, 10.0, 10.0, 20.0, 20.0],          # 页码 0 → 越界
            [1, 300.0, 300.0, 100.0, 100.0],      # 反向坐标
            [1, 10.0, 10.0, 20.0],                 # 不是五元组
            [2, 72.0, 200.0, 300.0, 230.0],        # 合法 → 留下
        ],
    })
    blocks = _image_blocks(_user_content(messages))
    # 1 张整页图 + 1 张合法公式图
    assert len(blocks) == 2, "畸形坐标也去裁图了，实际 %d 张" % len(blocks)


def test_sanitize_formula_rects_with_page_drops_broken_values():
    from microbench.app import _sanitize_formula_rects_with_page

    out = _sanitize_formula_rects_with_page([
        [float("nan"), 1.0, 2.0, 3.0, 4.0],
        [float("inf"), 1.0, 2.0, 3.0, 4.0],
        [0, 1.0, 2.0, 3.0, 4.0],           # 页码 0
        [1.5, 1.0, 2.0, 3.0, 4.0],         # 页码不是整数
        [2, 300.0, 300.0, 100.0, 100.0],   # 反向坐标
        [2, 10.0, 10.0, 20.0],             # 不是五元组
        ["a", "b", "c", "d", "e"],          # 非数字
        [3, 10.0, 10.0, 20.0, 20.0],       # 合法 → 留下
    ])
    assert len(out) == 1, "只应留下 1 条合法坐标，实际：" + repr(out)
    assert out[0] == (3, (10.0, 10.0, 20.0, 20.0)), out


def test_sanitize_formula_rects_with_page_caps_the_count():
    from microbench.app import _sanitize_formula_rects_with_page

    many = [[i + 1, 1.0, 2.0, 3.0, 4.0] for i in range(50)]
    assert len(_sanitize_formula_rects_with_page(many)) <= 16


# ---------------------------------------------------------------------------
# 4) 前端行为级护栏（node 跑 index.html 真源码 + 变异自检）
# ---------------------------------------------------------------------------
def test_qa_section_scope_frontend_guardrail_passes():
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 小节上下文前端护栏跳过")
    proc = subprocess.run([node, str(JS_TEST)], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, (
        "小节上下文前端护栏失败:\n" + proc.stdout + "\n" + proc.stderr)
    assert "FAIL 0" in proc.stdout, proc.stdout
    m = re.search(r"变异捕获\s+(\d+)/(\d+)", proc.stdout)
    assert m, "护栏输出里没有变异自检结果 —— 断言可能是恒真的：\n" + proc.stdout
    caught, total = int(m.group(1)), int(m.group(2))
    assert total > 0, "变异自检一个都没配 —— 等于没写：\n" + proc.stdout
    assert caught == total, \
        f"变异只抓到 {caught}/{total} —— 有一套断言是恒真的：\n" + proc.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
