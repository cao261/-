# -*- coding: utf-8 -*-
"""问答上下文装配：长论文的后半部分小节必须问得到（2026-11-15）。

用户原话：

    "有次让 ai 回答问题，让他总结这一小节的主要内容嘛，它说给它的上下文里没有，
     它只能靠猜……是不是因为论文太长了，因为那个问题出现的位置是一篇长论文的
     后半部分，给的上下文位置是不是不对"

**根因不是模型，是截断策略**：三个入口都按「论文顺序取前 N 节」拼上下文 ——
后端 `if total > 24000: break`、前端 `.slice(0, 20)`、前端 full 模式 `.slice(0, 8)`。
于是长论文后半部分的节根本不在 prompt 里，模型只能回答「上下文里没有，我只能猜」。

**实测（修复前）**：20 节 / 每节 3000 字 / 问第 18 节 → prompt 里**只有前 6 节**；
把同一节挪到第 3 位 → 它就出现了。**位置决定生死**。

这一轮钉死：
1. 焦点节（用户当前所在/正在问的那一节）**永远**在上下文里；
2. 问题里「第 N 节」指代的节**一定**在上下文里；
3. 长论文（>20 节）不再被截成前 N 节；
4. 划词答疑的焦点小节走**独立字段**，不受上下文截断影响。
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from microbench.qa_context import (  # noqa: E402
    extract_ordinals, pick_context, score_sections, QA_CONTEXT_BUDGET,
)

INDEX = REPO / "microbench" / "static" / "index.html"
APP = REPO / "microbench" / "app.py"


def _sections(n, size=3000):
    out = []
    for i in range(1, n + 1):
        out.append({
            "section": f"SEC{i:02d}",
            "title": f"{i}. 第{i}节 标题",
            "number": str(i),
            "text": f"【SEC{i:02d} 开始】" + (f"第{i}节正文。" * (size // 5)) + f"【SEC{i:02d} 结束】",
        })
    return out


def _present(ctx, n):
    return [f"SEC{i:02d}" for i in range(1, n + 1) if f"SEC{i:02d}" in ctx]


# ---------------------------------------------------------------------------
# 1) 序号识别
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,expected", [
    ("请总结第 18 节的主要内容", {18}),
    ("第22节讲了什么", {22}),
    ("总结一下第四章", {4}),
    ("概括第五章的内容", {5}),
    ("Section IV 说了什么", {4}),
    ("section 7 please summarize", {7}),
    ("这一节讲了什么", set()),          # 指代型 —— 没有序号，靠焦点节兜底
    ("这篇论文有什么创新", set()),
])
def test_extract_ordinals(question, expected):
    assert extract_ordinals(question) == expected, \
        f"问题「{question}」的节号识别成了 {extract_ordinals(question)}"


def test_ordinal_fallback_to_list_position():
    """论文自带编号可能和列表顺序对不上 —— 列表序号也要能命中。"""
    secs = [{"section": "abstract", "title": "摘要", "number": "", "text": "x"},
            {"section": "s1", "title": "Introduction", "number": "I", "text": "x"},
            {"section": "s2", "title": "Methods", "number": "II", "text": "x"}]
    scores = score_sections(secs, "总结第 3 节")
    # 第 3 节 = 下标 2（列表第 3 个）→ 应拿到兜底分
    assert scores[2] > 0, "列表序号兜底没生效"


# ---------------------------------------------------------------------------
# 2) 装配：焦点节与命中节永远在
# ---------------------------------------------------------------------------

def test_focus_section_is_always_included():
    """★ 用户真实场景：长论文后半部分 + 指代型提问 + 焦点节。"""
    secs = _sections(20)
    ctx, meta = pick_context(secs, question="总结一下这一小节的主要内容", focus_index=17)
    assert "SEC18" in _present(ctx, 20), "焦点节（第 18 节）不在上下文里"
    assert meta["focus_hit"] is True


def test_ordinal_section_is_always_included():
    """问「第 18 节」→ 第 18 节必须在上下文里（哪怕它在最后）。"""
    secs = _sections(20)
    ctx, _ = pick_context(secs, question="请总结第 18 节的主要内容")
    assert "SEC18" in _present(ctx, 20), "问到的节号不在上下文里"


def test_ordinal_section_is_included_even_in_long_paper():
    """25 节长论文（超过原来的 20 节上限）也要能问到第 22 节。"""
    secs = _sections(25)
    ctx, _ = pick_context(secs, question="请总结第 22 节的主要内容")
    assert "SEC22" in _present(ctx, 25)


def test_position_no_longer_decides_luck():
    """同一节，无论排在第 3 位还是第 18 位，都必须进上下文。"""
    a = _sections(20)
    b = _sections(20)
    moved = b.pop(17)
    b.insert(2, moved)
    ctx_a, _ = pick_context(a, question="请总结第 18 节的主要内容")
    ctx_b, _ = pick_context(b, question="请总结第 18 节的主要内容")
    assert "SEC18" in _present(ctx_a, 20)
    assert "SEC18" in _present(ctx_b, 20)


def test_focus_text_is_used_when_no_focus_index():
    """划词答疑：焦点正文走独立字段，也必须进上下文。"""
    ctx, meta = pick_context(
        _sections(20), question="总结这一小节",
        focus_text="这是焦点小节的正文内容", focus_title="第 18 节 标题")
    assert "这是焦点小节的正文内容" in ctx, "focus_text 没进上下文"
    assert meta["focus_hit"] is True


def test_context_respects_budget():
    """预算不能被突破（否则请求体/上下文无限膨胀）。"""
    secs = _sections(40, size=6000)
    ctx, meta = pick_context(secs, question="总结这一节", focus_index=0)
    assert len(ctx) <= QA_CONTEXT_BUDGET + 200, f"超预算：{len(ctx)}"
    assert meta["chars"] <= QA_CONTEXT_BUDGET + 200


def test_long_section_keeps_head_and_tail():
    """超长单节 → 头尾各取一半（保住开头与结论），不是只留开头。"""
    long_text = "HEAD_MARKER" + ("中" * 20000) + "TAIL_MARKER"
    ctx, _ = pick_context([{"section": "S", "title": "T", "number": "1", "text": long_text}],
                          question="总结", focus_index=0)
    assert "HEAD_MARKER" in ctx, "超长节的开头丢了"
    assert "TAIL_MARKER" in ctx, "超长节的结尾丢了"


# ---------------------------------------------------------------------------
# 3) 端点级（行为级：看真正发出去的 prompt）
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, appmod, monkeypatch):
        self.captured = {}

        async def fake_stream(messages, **kw):
            self.captured["messages"] = messages
            yield "（占位）"

        monkeypatch.setattr(appmod.llm, "chat_stream_async", fake_stream)


def _blob(captured):
    parts = []
    for m in captured["messages"]:
        c = m["content"]
        parts.append(c if isinstance(c, str) else str(c))
    return "\n".join(parts)


@pytest.fixture
def client_and_capture(monkeypatch):
    from fastapi.testclient import TestClient
    from microbench import app as appmod
    fake = _FakeClient(appmod, monkeypatch)
    return TestClient(appmod.app), fake


def test_explain_stream_keeps_late_section(client_and_capture):
    """端点级：问第 18 节 → 发出去的 prompt 里必须有第 18 节。"""
    client, fake = client_and_capture
    resp = client.post("/api/paper/explain_stream", json={
        "question": "请总结第 18 节的主要内容",
        "sections": _sections(20),
        "paper_title": "长论文",
    })
    assert resp.status_code == 200, resp.text
    assert "SEC18" in _blob(fake.captured), "第 18 节没进 prompt"


def test_explain_stream_accepts_more_than_20_sections(client_and_capture):
    """25 节必须能发进来（原来 20 节上限 → 直接 422）。"""
    client, fake = client_and_capture
    resp = client.post("/api/paper/explain_stream", json={
        "question": "总结第 22 节",
        "sections": _sections(25),
        "paper_title": "长论文",
    })
    assert resp.status_code == 200, f"25 节被拒了：{resp.text[:200]}"
    assert "SEC22" in _blob(fake.captured)


def test_explain_stream_focus_index_wins(client_and_capture):
    """指代型提问 + focus_index → 焦点节必须在 prompt 里。"""
    client, fake = client_and_capture
    resp = client.post("/api/paper/explain_stream", json={
        "question": "总结一下这一小节的主要内容",
        "sections": _sections(20),
        "focus_index": 17,
        "paper_title": "长论文",
    })
    assert resp.status_code == 200, resp.text
    assert "SEC18" in _blob(fake.captured)


# ---------------------------------------------------------------------------
# 4) 前端：行为级（node 跑 index.html 里的真代码）
#
# ⚠️ 这里**不能**用「读源码文本找某段字符串」的写法：
#    那种护栏把 `focus_index: null` 写坏了也照样绿（字符串还在），
#    而且项目自己的元护栏（tests/test_guard_efficacy.py）
#    明确规定「读源码文本当护栏」的条数不得增加。
#    -> 改成把 index.html 里的真函数抽出来跑（microbench/_test_focus_context.js）。
#
# ⚠️ 2026-11-19：划词答疑那条路径（explain_selection_stream）**不在本文件
#    的范围内** —— 30461 台已把它做成更细的 scope 机制（term/mid/section/
#    wide/full + 真小节定位，见 QA_SCOPE_CAPS 与 _buildScopeContextParts），
#    护栏在 tests/test_qa_section_context.py / test_qa_scope_router.py。
#    本文件只守**整篇问答**（explain_stream）这条路径。
# ---------------------------------------------------------------------------

JS_TEST = REPO / "microbench" / "_test_focus_context.js"


def _node():
    exe = shutil.which("node")
    if not exe:
        pytest.skip("没装 node —— 前端焦点上下文护栏跳过")
    return exe


def test_frontend_explain_payload_behaviour():
    """前端必须真的做对这两件事（行为级，不是看源码里有没有那个字符串）：

    1. `_buildExplainPayload` **全量**发送章节（不再 slice(0,20)）——
       长论文的后半部分（恰恰是用户正在问的那部分）不能被静默丢掉；
    2. 它带上 `focus_index`（用户当前所在的那一节）与 `number` / 页码字段
       （后端靠 number 匹配「第 N 节」、靠页码定位所属节）。
    """
    node = _node()
    proc = subprocess.run(
        [node, str(JS_TEST)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        cwd=str(REPO),
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def test_backend_models_accept_long_paper():
    """后端 Pydantic 上限必须 ≥ 25（原来 20 → 直接 422）。"""
    from microbench import app as appmod
    assert appmod.ExplainRequest.model_fields["sections"].metadata[-1].max_length >= 25, \
        "ExplainRequest.sections 上限还是 20 —— 25 节的长论文会 422"
    assert appmod.CrossQARequest.model_fields["sections"].metadata[-1].max_length >= 25
