"""把平板页面侧的 JS 测试挂进 pytest。

**为什么需要这个文件**

平板工作台的三套 JS 测试（渲染器 / AI 面板 / 数据安全）以前是**手工跑**的
脚本 —— 文件名是 `_test_*.js`，pytest 默认的 `python_files` 一个都收不到：

    $ pytest microbench/ --collect-only
    no tests collected in 0.87s

于是「验收命令全绿」这件事对平板页面代码**完全没有保护力**：
渲染器或同步逻辑回退了，`pytest tests/` 照样绿，交付给用户的却是坏的。
（真出过这种事：Markdown 的引号转义坏了整整一个版本，38 条渲染器测试
全绿 —— 因为它们恰好避开了会失败的攻击面。）

本文件把这三套脚本挂进 `pytest tests/`，跑一条就红一条。

跑法:  python -m pytest tests -q -m "not slow"
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# (脚本, 说明)
NODE_SUITES = [
    ("microbench/_test_tablet_renderer.js", "渲染器（Markdown / 公式 / 消毒 / hash）"),
    ("microbench/_test_tablet_ai.js", "AI 面板（发送后清空输入栏 / 多轮上下文 / Enter）"),
    ("microbench/_test_tablet_hardening.js", "数据安全（种子不覆盖本地 / XSS / 嵌套公式）"),
    ("microbench/_test_tablet_pdf_ink.js",
     "PDF 缩放 + 手写层（放大能滚 / 触摸手势 / 笔迹存取 / 桌面端能看见）"),
]


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("没装 node —— 平板页面侧的 JS 测试跳过")
    return exe


@pytest.mark.parametrize("rel,label", NODE_SUITES, ids=[s[0] for s in NODE_SUITES])
def test_tablet_node_suite_passes(rel: str, label: str):
    """每套平板 JS 测试都必须全绿 —— 有一条红就说明页面侧出问题了。"""
    node = _node()
    script = REPO / rel
    assert script.exists(), f"测试脚本不见了: {rel}"
    proc = subprocess.run(
        [node, str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, cwd=str(REPO),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (
        f"平板 JS 测试失败（{label}）:\n"
        f"  脚本: {rel}\n"
        f"  退出码: {proc.returncode}\n"
        + "\n".join("  " + ln for ln in out.strip().splitlines()[-25:])
    )
    # 脚本自己会打印 PASS n FAIL m —— 再确认一次，避免「脚本忘了 fail」
    assert "FAIL 0" in out, f"{rel} 没有报 FAIL 0（脚本本身可能坏了）:\n{out[-800:]}"
