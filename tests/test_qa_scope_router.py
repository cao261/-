# -*- coding: utf-8 -*-
"""scope router 的**契约**与**跨端一致性**（2026-11-19）。

背景（实测，见 logs/qa_router/{before,after3}/）：
  ① 旧 router 提示词里**根本没有 section 档**，而前端 `_SCOPE_CAPS` 早就有
     → 「这一节讲了什么」被判成 mid（= 当前页），小节跨页时**后半页漏掉**；
  ② 旧提示词写着「若问题开放、不能确定 → 选更大一档」
     → 是/否问题也拿 6000 字，无关内容稀释注意力；
  ③ router 给的 budget 可能**超该档上限**（实测 full 给了 60000 > cap 50000）
     → 「router 说的 budget」与「真发出去的字数」对不上（§4.107）。

这一轮落地：
  · `QA_SCOPE_ROUTER_PROMPT` 重写：加 section 档 + 按「证据位置」选档；
  · `QA_SCOPE_MODES` / `QA_SCOPE_CAPS` 抽成模块级常量；
  · router 端点：`mode` 白名单认 section；budget 按该档上限夹住；
  · 前端白名单同步加 section（两边**必须**一致，否则 router 结果被静默丢弃）。

⚠ 断言的是**行为**（真打端点、真跑 node 护栏），不是 grep 源码。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS_TEST = REPO / "microbench" / "_test_qa_section_scope.js"

sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------
# 工具：真打 router 端点，把 LLM 的返回换掉
# ---------------------------------------------------------------------------
def _route_with_stub(client, monkeypatch, stub: dict):
    """真打 `/api/paper/classify_qa_scope`，但把模型返回换掉。"""
    from microbench import llm as llm_mod

    def fake_chat(**kwargs):
        # ⚠ llm.chat 是**同步**函数（端点用 asyncio.to_thread 包它）→ 桩必须同步
        return json.dumps(stub, ensure_ascii=False)

    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat", fake_chat)

    res = client.post("/api/paper/classify_qa_scope", json={
        "selected_text": "k31p is transverse electromechanical coupling coefficient",
        "question": "这一节讲了什么？",
    })
    assert res.status_code == 200, res.text
    return res.json()


def test_router_endpoint_accepts_the_section_mode(client, monkeypatch):
    """⚠ 核心：router 必须能返回 section 档。

    旧白名单是 ("term","mid","wide","full") → 返回 section 会被判「非法」→
    502 → 前端静默退回关键词启发式 → 用户看到的是「关键词 fallback」。
    """
    data = _route_with_stub(client, monkeypatch,
                            {"mode": "section", "reason": "整节", "budget": 12000})
    assert data["mode"] == "section", data
    assert data["budget"] == 12000, data


def test_router_endpoint_clamps_budget_to_that_mode_s_cap(client, monkeypatch):
    """router 给的 budget 不能超该档上限（实测 full 给 60000 > cap 50000）。

    超了会怎样：服务端再砍一刀 → 「router 说的 budget」与真发出去的字数对不上
    → 徽标说谎（AGENTS.md §4.107）。
    """
    from microbench.app import QA_SCOPE_CAPS

    data = _route_with_stub(client, monkeypatch,
                            {"mode": "full", "reason": "整篇", "budget": 60000})
    assert data["budget"] == QA_SCOPE_CAPS["full"], \
        "full 档的 budget 没被夹到上限：%s" % data


def test_router_endpoint_rejects_an_unknown_mode(client, monkeypatch):
    """白名单外的档位必须被拒（不能放行 front/back 不认的档）。"""
    from microbench import llm as llm_mod

    def fake_chat(**kwargs):
        return json.dumps({"mode": "huge", "reason": "x", "budget": 100},
                          ensure_ascii=False)

    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat", fake_chat)

    res = client.post("/api/paper/classify_qa_scope", json={
        "selected_text": "x", "question": "y"})
    assert res.status_code == 502, res.text
    assert "huge" in res.text, res.text


def test_router_prompt_lists_all_five_modes():
    """router 提示词必须列出 5 个档（含 section），且与 QA_SCOPE_MODES 一致。

    ⚠ 这是提示词契约：档位少了 → router 永远判不到那一档。
    """
    from microbench.app import QA_SCOPE_MODES, QA_SCOPE_ROUTER_PROMPT

    for mode in QA_SCOPE_MODES:
        assert '"%s"' % mode in QA_SCOPE_ROUTER_PROMPT, \
            "router 提示词里没有 %s 档" % mode
    # 旧提示词的「开放就升档」会让是/否问题也给整页
    assert "选更大一档" not in QA_SCOPE_ROUTER_PROMPT, \
        "router 提示词还在教它「拿不准就升档」—— 是/否问题会拿到整页"


def test_router_user_message_states_the_real_mode_count(client, monkeypatch):
    """router 发给模型的 user 消息里说的「档数」必须等于真实档数。

    2026-11-22 独立审查实测：system prompt 早就列了 5 档
    （term / mid / section / wide / full），但 **user 消息里还写着「4 档」**
    —— 那是加 section 档之前留下的**陈旧文案**。
    → 模型被告知「4 档」，而眼前列了 5 档 → 自相矛盾（§4.6：对模型说假话）。

    ⚠ 断言的是**真发出去的请求**（拦下 `llm.chat` 的实参看内容），
    不是 grep 源码 —— 改文案就能过的那种断言不算数。
    """
    from microbench import llm as llm_mod
    from microbench.app import QA_SCOPE_MODES

    seen = {}

    def fake_chat(**kwargs):
        seen["messages"] = kwargs.get("messages") or []
        return json.dumps({"mode": "mid", "reason": "x", "budget": 8000},
                          ensure_ascii=False)

    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat", fake_chat)

    res = client.post("/api/paper/classify_qa_scope", json={
        "selected_text": "x", "question": "y"})
    assert res.status_code == 200, res.text

    msgs = seen.get("messages") or []
    assert msgs, "没抓到发给模型的请求 —— 这条断言就成恒真了"
    user_msg = "\n".join(str(m.get("content", "")) for m in msgs
                         if m.get("role") == "user")
    m = re.search(r"(\d+)\s*档", user_msg)
    assert m, "user 消息里根本没提档数：%r" % user_msg
    assert int(m.group(1)) == len(QA_SCOPE_MODES), (
        "user 消息说 %s 档，实际有 %d 档（%s）—— 对模型说假话"
        % (m.group(1), len(QA_SCOPE_MODES), list(QA_SCOPE_MODES)))


def test_router_prompt_describes_modes_with_the_real_caps():
    """提示词里各档的区间要与真实上限对得上（别写「60K」而实际是 50K）。"""
    from microbench.app import QA_SCOPE_CAPS, QA_SCOPE_ROUTER_PROMPT

    assert "50000" in QA_SCOPE_ROUTER_PROMPT or "50K" in QA_SCOPE_ROUTER_PROMPT, \
        "提示词里没有 full 档的真实上限（50000）"
    for mode, cap in QA_SCOPE_CAPS.items():
        assert cap > 0, mode


@pytest.fixture(scope="module")
def _js_scope_contracts_stdout():
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 跨端一致性检查跳过")
    proc = subprocess.run([node, str(JS_TEST), "--contracts-only"],
                          capture_output=True, text=True,
                          encoding="utf-8", timeout=180)
    assert proc.returncode == 0, (
        "前端护栏失败:\n" + proc.stdout + "\n" + proc.stderr)
    return proc.stdout


# ---------------------------------------------------------------------------
# 跨端一致性：前端的 _SCOPE_CAPS 必须与后端的 QA_SCOPE_CAPS 逐字一致
# ---------------------------------------------------------------------------
def test_frontend_scope_caps_match_the_backend(_js_scope_contracts_stdout):
    """⚠ 两端的档位上限必须逐字一致（§4.107：徽标显示的字数 = 真发出去的字数）。

    做法：真跑 node 护栏（它把前端 `_SCOPE_CAPS` 原样吐出来）→ 与后端常量比对。
    不是「grep 源码」—— 是**跑前端代码**拿到的运行时值。
    """
    m = re.search(r"^SCOPE_CAPS_JSON: (\{.*\})$", _js_scope_contracts_stdout, re.M)
    assert m, "node 护栏没有输出 SCOPE_CAPS_JSON —— 跨端检查失效：\n" + _js_scope_contracts_stdout
    front = json.loads(m.group(1))

    from microbench.app import QA_SCOPE_CAPS
    assert front == QA_SCOPE_CAPS, \
        "前后端的档位上限不一致：前端 %s / 后端 %s" % (front, QA_SCOPE_CAPS)


def test_frontend_router_allowlist_matches_the_backend(_js_scope_contracts_stdout):
    """前端白名单必须与后端 `QA_SCOPE_MODES` 一致。

    不一致的后果：router 返回的档位在前端被判「非法」→ 抛错 →
    **静默**退回关键词启发式（用户看不出发生了什么）。
    """
    m = re.search(r"^ROUTER_MODES_JSON: (\[.*\])$", _js_scope_contracts_stdout, re.M)
    assert m, "node 护栏没有输出 ROUTER_MODES_JSON：\n" + _js_scope_contracts_stdout
    front = json.loads(m.group(1))

    from microbench.app import QA_SCOPE_MODES
    assert sorted(front) == sorted(QA_SCOPE_MODES), \
        "前后端的 router 白名单不一致：前端 %s / 后端 %s" % (front, list(QA_SCOPE_MODES))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
