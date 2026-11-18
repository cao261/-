# -*- coding: utf-8 -*-
"""AI 答疑的**提示词契约**与**上下文准确性**（2026-11-19）。

用户原话：
  「你现在要做的是确定 ai 回答问题这块切实有效，上下文截取送给 ai 的准确性，
    另一个设备上做的改动主要是给 ai 的提示词放松了，
    因为现在的模板很机械，做不到很好的让 ai 回答问题的能力，
    而 ai 的能力现在已经很强了，这会起到束缚作用，而且回答的很多不需要的内容」

这一轮落地的东西：
  ① 系统提示词**去模板化**：不再强制 4 个固定小节、不再写死领域、
     不再强制「必须给工程与研究启发」；改成「长度服从问题、不要套固定模板」；
  ② 提示里对模型说的话**必须与上下文的实际形态一致**：
     section 档的英文原文里公式是【公式N】占位（对应高清图），
     别的档给的是 PDF.js 扁平文本 → 公式才是碎片。
     旧写法一刀切说「上下文里的公式都是乱序碎片」→ 等于叫模型别信正文里的公式；
  ③ 服务端截断改成**头尾都留**（旧写法纯头部 → 砍掉小节结尾的结论）；
  ④ 标题场景要**如实说明**「中文译文是整页翻译」。

⚠️ 这里断言的都是**真正发给模型的内容**（真打端点、拦 messages），
    不是 grep 源码文件 —— 改坏行为会红，改文案也会红（提示词契约就是要锁文案）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

fitz = pytest.importorskip("fitz")

PDF_REL = "01_Literature/提示词契约样本.pdf"
PAGE_W, PAGE_H = 612.0, 792.0

# 旧模板强制的 4 个小节 —— 它们在不在，就是「模板有没有绑架回答」的判据
TEMPLATE_SECTIONS = (
    "【直击疑问】",
    "【物理背景与器件机制】",
    "【术语全称与公式拆解】",
    "【工程与研究启发】",
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _make_paper(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((72, 90), "II. Experimental Procedure", fontsize=11, fontname="helv")
    page.insert_text((72, 120), "Samples were prepared by co-sintering the two phases.",
                     fontsize=11, fontname="helv")
    doc.save(str(path))
    doc.close()


def _capture(client, sandbox, monkeypatch, payload_extra=None):
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
        "selected_text": "Experimental Procedure",
        "question": "这个系数是什么意思？",
        "paper_title": "提示词契约样本",
        "source_type": "pdf",
        "pdf_relative_path": PDF_REL,
        "page_num": 1,
    }
    payload.update(payload_extra or {})

    res = client.post("/api/paper/explain_selection_stream", json=payload)
    assert res.status_code == 200, res.text
    assert "messages" in captured, "模型压根没被调用 —— 桩没生效"
    return captured["messages"]


def _system_text(messages) -> str:
    for m in messages:
        if m.get("role") == "system":
            content = m.get("content")
            return content if isinstance(content, str) else str(content)
    raise AssertionError("没有 system 消息")


def _user_text(messages) -> str:
    for m in messages:
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            return "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
    raise AssertionError("没有 user 消息")


# ---------------------------------------------------------------------------
# 1) 系统提示词：去模板化，但红线必须还在
# ---------------------------------------------------------------------------
def test_system_prompt_has_no_forced_template_sections(client, sandbox, monkeypatch):
    """⚠ 核心：提示词**不许**再强制那 4 个固定小节。

    实测（2026-11-19，同一篇论文同一批问题）：
      旧模板 → 问「这个系数怎么定义」给 **6047 字**，
               里面塞了【物理背景与器件机制】+【工程与研究启发】，
               甚至把磁电器件硬扯到「Sub-3nm/2nm 节点研究的类比意义」；
      放松后 → **1103 字**，且没有一处跑偏。
    """
    system = _system_text(_capture(client, sandbox, monkeypatch))
    for name in TEMPLATE_SECTIONS:
        assert name not in system, \
            "提示词又强制这个栏目了（回答会被模板牵着走）：" + name
    assert "Sub-3nm" not in system and "2nm" not in system, \
        "提示词还在要求模型扯先进工艺节点 —— 跨领域跑偏的来源"
    assert "微电子与集成电路领域" not in system, \
        "提示词又把领域写死了 —— 换个领域的论文就会跑偏"


def test_system_prompt_keeps_the_hard_limits(client, sandbox, monkeypatch):
    """放松 ≠ 放飞：这些红线一条都不能少。"""
    system = _system_text(_capture(client, sandbox, monkeypatch))
    # ① 公式不许凭空补全（AGENTS.md §4.6：错公式比没公式危险得多）
    assert "绝" in system and "凭空" in system, \
        "提示词里没有「不许凭空补公式」的红线"
    # ② 不许编造网址 / 文献编号 / DOI / 图号
    assert "DOI" in system, "提示词里没有「不许编造 DOI」的禁令"
    assert "网址" in system or "链接" in system, "提示词里没有「不许编造链接」的禁令"
    # ③ 不许交白卷（AGENTS.md §4.37~4.39 踩过的坑）
    assert "白卷" in system, "提示词里没有「必须给出实质回答」的要求"
    # ④ 事实以论文为准，片段里没有就说没有
    assert "片段中未提及" in system, \
        "提示词里没有「片段里没有就直说」的要求 —— 模型会拿记忆冒充论文结论"


def test_system_prompt_tells_the_model_to_keep_answers_short(client, sandbox, monkeypatch):
    """长度原则必须在位（用户明确抱怨「回答的很多不需要的内容」）。"""
    system = _system_text(_capture(client, sandbox, monkeypatch))
    assert "长度服从问题" in system, "提示词里没有「长度服从问题」的原则"
    assert "不要套固定模板" in system, "提示词里没有「不要套固定模板」的要求"


# ---------------------------------------------------------------------------
# 2) 提示里对模型说的话，必须与**上下文的实际形态**一致
# ---------------------------------------------------------------------------
def test_section_scope_says_formula_placeholders_match_the_images(client, sandbox, monkeypatch):
    """section 档：英文原文里公式是【公式N】占位 → 不能说成「乱序碎片」。

    旧写法一刀切说「上下文里的公式都是乱序碎片、不要根据上下文补全」
    → 等于叫模型**别信正文里的公式** → 连【公式N】占位一起忽略掉。
    """
    messages = _capture(client, sandbox, monkeypatch, {
        "selection_kind": "heading",
        "heading_title": "II. Experimental Procedure",
        "section_pages": [1],
        "force_scope": "section",
        "surrounding_context": "【本小节完整正文】:\n正文里的公式是【公式1】",
        "formula_rects_with_page": [[1, 72.0, 200.0, 300.0, 230.0]],
    })
    text = _user_text(messages)
    assert "一一对应" in text, \
        "没告诉模型「占位与高清图一一对应」：\n" + text[-1200:]
    assert "碎片" not in text, \
        "section 档却告诉模型「上下文里的公式是碎片」—— 与占位矛盾：\n" + text[-1200:]


def test_page_scope_still_warns_that_formula_text_is_garbage(client, sandbox, monkeypatch):
    """反过来：PDF.js 扁平文本里的公式**确实是碎片** → 警告必须留着。"""
    messages = _capture(client, sandbox, monkeypatch, {
        "selection_kind": "body",
        "force_scope": "mid",
        "surrounding_context": "[Page 1]\nnd33m g31p",
        "formula_rects_with_page": [[1, 72.0, 200.0, 300.0, 230.0]],
    })
    text = _user_text(messages)
    assert "碎片" in text, \
        "PDF.js 扁平文本里的公式是碎片，必须警告不要照抄：\n" + text[-1200:]


def test_heading_mode_says_the_chinese_translation_covers_a_whole_page(client, sandbox, monkeypatch):
    """中文译文是**整页**翻的 → 必须如实说，否则模型会把摘要/引言当成本节内容。"""
    messages = _capture(client, sandbox, monkeypatch, {
        "selection_kind": "heading",
        "heading_title": "II. Experimental Procedure",
        "section_pages": [1, 2],
        "force_scope": "section",
        "surrounding_context": "【本小节完整正文】:\n本小节正文",
    })
    text = _user_text(messages)
    assert "整页" in text, \
        "没告诉模型「中文译文是整页翻译」：\n" + text[:1500]
    assert "英文原文" in text, \
        "没告诉模型「以英文原文为准定位本小节起止」：\n" + text[:1500]


# ---------------------------------------------------------------------------
# 3) 服务端截断：头尾都留（别砍掉小节结尾的结论）
# ---------------------------------------------------------------------------
def test_server_clip_keeps_the_tail_of_an_overlong_context(client, sandbox, monkeypatch):
    """⚠ 旧写法是 `text[:cap]` 纯头部截断 → 小节结尾的结论被砍掉。

    上下文里放一个**只在末尾**出现的标记 → 它必须还在模型看到的内容里。
    """
    tail_marker = "ZZTAILMARKERZZ"
    body = ("头" * 30000) + tail_marker
    messages = _capture(client, sandbox, monkeypatch, {
        "force_scope": "section",
        "surrounding_context": body,
    })
    text = _user_text(messages)
    assert tail_marker in text, \
        "服务端的截断把上下文**尾部**砍掉了 —— 小节的结论就在那里"
    assert "头" * 100 in text, "头也没保住"


def test_server_clip_does_not_stack_two_omission_markers(client, sandbox, monkeypatch):
    """上游（前端）已经省略过一次 → 服务端**不再叠第二个**省略标记。

    两个「中间已省略」会让模型分不清各省略了多少。
    """
    body = ("X" * 10000) + "\n…（中间约 5,000 字已省略）…\n" + ("Y" * 10000)
    messages = _capture(client, sandbox, monkeypatch, {
        "force_scope": "section",
        "surrounding_context": body,
    })
    text = _user_text(messages)
    assert text.count("…（中间") == 1, \
        "出现了 %d 个省略标记 —— 模型分不清各省略了多少" % text.count("…（中间")


def test_server_clip_head_and_tail_both_survive(client, sandbox, monkeypatch):
    """头尾都要在（不是只留头部，也不是只留尾部）。"""
    head_marker = "ZZHEADMARKERZZ"
    tail_marker = "ZZTAILMARKERZZ"
    body = head_marker + ("X" * 30000) + tail_marker
    messages = _capture(client, sandbox, monkeypatch, {
        "force_scope": "section",
        "surrounding_context": body,
    })
    text = _user_text(messages)
    assert head_marker in text, "头部被砍了"
    assert tail_marker in text, "尾部被砍了"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
