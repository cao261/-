# -*- coding: utf-8 -*-
"""「➕ 存入避坑库」一键保存 —— 前端载荷 ↔ 后端模型的**契约**护栏。

**背景**（2026-11-17 全面功能审查 D1，确证缺陷）：
  桌面端 `saveQAToTroubleshoot()` 发的请求体里 `raw_error` / `root_cause`
  是**空串**，而后端 `TroubleshootRequest` 这两个字段都是 `min_length=1`
  → **必然 422**，「一键存入避坑库」这个功能从来就没成功过；
  而且报错被写成「API 暂不可用」，把排查方向带偏。

**这条护栏为什么不是「读源码文本当断言」**：
  它让 node 去跑 `microbench/_test_troubleshoot_save_frontend.js` ——
  那个脚本把 index.html 里**真实的函数切出来真跑一遍**，拿到**实际发出的
  请求体**；这里再用**后端自己的 Pydantic 模型**校验它。
  → 前后端任一侧改坏（字段清空 / 改名 / 加必填），这条都红。
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
JS_TEST = REPO / "microbench" / "_test_troubleshoot_save_frontend.js"

sys.path.insert(0, str(REPO))


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("没装 node —— 前端载荷护栏跳过")
    return exe


def test_troubleshoot_save_frontend_node_guardrail_passes():
    """前端护栏（含变异自检）必须全绿。"""
    node = _node()
    assert JS_TEST.exists(), f"测试脚本不见了: {JS_TEST}"
    proc = subprocess.run(
        [node, str(JS_TEST)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=str(REPO),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        "「存入避坑库」前端护栏失败:\n" + out[-3000:])


def test_troubleshoot_payload_passes_the_backend_model():
    """前端**实际发出**的请求体，必须能被后端的 Pydantic 模型接受。

    这是真正的「前后端契约」检查：不是比对字段名，而是拿后端模型去校验。
    """
    node = _node()
    proc = subprocess.run(
        [node, str(JS_TEST), "--dump-payload"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=str(REPO),
    )
    assert proc.returncode == 0, "前端脚本跑不起来:\n" + (proc.stderr or "")[-2000:]
    line = ""
    for ln in (proc.stdout or "").splitlines():
        if ln.startswith("PAYLOAD:"):
            line = ln
            break
    assert line, "前端脚本没吐出载荷:\n" + (proc.stdout or "")[-2000:]
    payloads = json.loads(line[len("PAYLOAD:"):])
    assert payloads, "没发出任何请求"

    from microbench.app import TroubleshootRequest
    for i, payload in enumerate(payloads):
        try:
            TroubleshootRequest.model_validate(payload)
        except Exception as e:
            pytest.fail(
                f"前端第 {i + 1} 次请求的载荷过不了后端校验（会 422）：{e}\n"
                f"载荷：{json.dumps(payload, ensure_ascii=False)[:400]}")


def test_troubleshoot_endpoint_rejects_empty_fields():
    """反证：后端**确实**拒绝空串（否则这条契约护栏就是恒真的）。"""
    from microbench.app import TroubleshootRequest
    with pytest.raises(Exception):
        TroubleshootRequest.model_validate({
            "tool_name": "T", "category": "TCAD仿真收敛", "error_keyword": "K",
            "raw_error": "", "root_cause": "", "solution": "S",
        })
