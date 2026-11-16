# -*- coding: utf-8 -*-
"""划词答疑：公式必须真的以「看得清」的图送到模型面前。

用户 2026-11-17 原话：
  「如果是划译文的词句问问题，因为公式是直接截图放在译文里的，
    ai 看上下文的时候完全看不到公式推导，只能猜」

两层根因（都已实测，不是推测）：
  ① 译文栏划选走的是 `source_type="translation"`，而前端的截图门禁写的是
     `source === "pdf"` → **整页截图根本没发给模型**
     （Playwright 拦截请求实测 `has_page_image: false`）。
  ② 就算发了，浏览器那张是 **PDF.js 画布** —— 画布按**栏宽**渲染
     （双栏模式下整页仅 ~785px 宽）→ 一条 223pt 宽的公式在整页图上
     只有 ~18px 高 → 多模态模型**读不出来**，于是开始"猜"。

→ 修法是**两段式**：整页图给「布局上下文」+ 该页公式按坐标**单独裁成
  高清图**给「公式细节」（JACERS 综述实测：裁出来 894×40px，
  而同一公式在整页图上只有 ~18px 高）。

⚠️ 这里**一律不读源码文本当断言**（`assert "xxx" in src`）：
   元护栏 tests/test_guard_efficacy.py 把那一类钉死不许再涨 ——
   那种断言改坏行为也照样绿（把 image_url 删了、把裁图 scale 调回 1、
   把附件说明删了，源码里该有的字符串还在 → 全绿）。
   → 本文件**真打端点**，拦下真正发给模型的 messages，断言**可观察的后果**。
"""
import base64
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS_TEST = REPO / "microbench" / "_test_qa_formula_frontend.js"

sys.path.insert(0, str(REPO))

fitz = pytest.importorskip("fitz")

PDF_REL = "01_Literature/04_Survey_综述/01_论文原文_PDF/公式护栏样本.pdf"
PAGE_NO = 1
# 一条 228×30pt 的「公式」（横跨整栏）
FORMULA_RECT = [72.0, 200.0, 300.0, 230.0]
PAGE_W, PAGE_H = 612.0, 792.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _make_paper(path: Path) -> None:
    """造一页 612×792 的样本：有正文、有一条横跨整栏的「公式」。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text((72, 90), "Magnetostrictive composites are studied here.",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 110), "The magnetoelectric coefficient follows the law:",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 215), "alpha_ME = dE / dH  (equation 3)",
                     fontsize=11, fontname="helv")
    page.insert_text((72, 300), "The following paragraph continues the discussion.",
                     fontsize=11, fontname="helv")
    doc.save(str(path))
    doc.close()


def _capture_messages(client, sandbox, monkeypatch, payload_extra=None):
    """真打答疑端点，拦下**真正发给模型**的 messages。

    不调真模型（省钱、也不受网络影响）—— 但整条链路（路由 → 渲染 →
    裁图 → 拼 content）都是真的。
    """
    from microbench import llm as llm_mod

    captured = {}

    async def fake_chat_stream_async(**kwargs):
        captured["messages"] = kwargs.get("messages") or []
        captured["kwargs"] = kwargs
        yield "（桩回答）"

    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat_stream_async", fake_chat_stream_async)

    pdf = sandbox / PDF_REL
    _make_paper(pdf)

    payload = {
        "selected_text": "磁电电压系数随磁场变化",
        "question": "这个系数是怎么定义的？",
        "paper_title": "公式护栏样本",
        "source_type": "translation",
        "pdf_relative_path": PDF_REL,
        "page_num": PAGE_NO,
    }
    payload.update(payload_extra or {})

    res = client.post("/api/paper/explain_selection_stream", json=payload)
    assert res.status_code == 200, res.text
    assert "messages" in captured, "模型压根没被调用 —— 桩没生效"
    return captured["messages"]


def _user_content(messages):
    """取 user 消息的 content（文本或 content 数组）。"""
    for m in messages:
        if m.get("role") == "user":
            return m.get("content")
    raise AssertionError("没有 user 消息：" + json.dumps(messages)[:400])


def _image_blocks(content):
    """从 content 数组里挑出所有 image_url 块。"""
    if not isinstance(content, list):
        return []
    return [b for b in content
            if isinstance(b, dict) and b.get("type") == "image_url"]


def _decode(block):
    """把 image_url 块解成 PNG 字节。"""
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,"), "图片不是 PNG data URL：" + url[:40]
    return base64.b64decode(url.split(",", 1)[1])


def _png_size(data):
    """读 PNG 尺寸（不引 PIL，PyMuPDF 也能干，但直接读 IHDR 最省事）。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return w, h


def _all_text(content):
    """把 content 里的所有文本块拼起来。"""
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text")


# ---------------------------------------------------------------------------
# 1) 模型到底收到了几张图
# ---------------------------------------------------------------------------
def test_qa_sends_the_page_image_plus_one_crop_per_formula(client, sandbox, monkeypatch):
    """译文栏划选 → 模型必须同时收到「整页图」和「每个公式一张高清图」。

    ⚠ 反向用例：如果只发整页图（0 张公式图），模型就回到"只能猜"的状态。
    """
    rects = [FORMULA_RECT, [72.0, 320.0, 260.0, 342.0]]
    messages = _capture_messages(client, sandbox, monkeypatch,
                                 {"formula_rects": rects})
    content = _user_content(messages)
    blocks = _image_blocks(content)
    assert len(blocks) == 3, \
        "应有 1 张整页图 + 2 张公式图，实际 %d 张" % len(blocks)
    for b in blocks:
        data = _decode(b)
        assert data[:4] == b"\x89PNG", "附件不是 PNG"


def test_formula_crop_is_far_bigger_than_the_same_formula_on_the_page_image(
        client, sandbox, monkeypatch):
    """⚠ 核心断言：**公式那张必须明显比整页图里清楚**。

    这正是用户那句「完全看不到公式推导」的量化版本：
      · 整页图（zoom=2.0 + max_pixels 护栏）→ 每 pt 约 1.8px
        → 一条 30pt 高的公式只有 ~54px，上下标糊成一片；
      · 公式裁图（scale=4）→ 30pt 高的公式有 ~120px。
    """
    messages = _capture_messages(client, sandbox, monkeypatch,
                                 {"formula_rects": [FORMULA_RECT]})
    blocks = _image_blocks(_user_content(messages))
    assert len(blocks) == 2, "应有一张整页图 + 一张公式图"

    page_png = _decode(blocks[0])
    crop_png = _decode(blocks[1])
    page_w, page_h = _png_size(page_png)
    crop_w, crop_h = _png_size(crop_png)

    # 整页图的每 pt 像素数
    px_per_pt = page_h / PAGE_H
    on_page = (FORMULA_RECT[3] - FORMULA_RECT[1]) * px_per_pt
    assert on_page < 70, (
        "整页图上这条公式居然有 %.0fpx 高 —— 测试前提变了（整页图分辨率被调高了）"
        % on_page)
    assert crop_h >= 3.5 * (FORMULA_RECT[3] - FORMULA_RECT[1]), \
        ("公式图只有 %dpx 高（应约 %dpx）—— 裁图没按 scale=4 渲染，"
         "模型照样看不清" % (crop_h, 4 * (FORMULA_RECT[3] - FORMULA_RECT[1])))
    assert crop_h >= 1.8 * on_page, (
        "公式图 %dpx vs 整页图里的 %.0fpx —— 几乎没有变清楚，"
        "这个缺陷等于没修" % (crop_h, on_page))
    assert crop_w < page_w, "公式图不该比整页还大"


def test_prompt_tells_the_model_to_trust_the_high_res_formula_images(
        client, sandbox, monkeypatch):
    """提示词必须明确「以高清公式图为准」+「看不清就说看不清，不要编造」。

    为什么这条也是红线：仓库既有原则是「**错公式比没公式危险得多**」
    （用户会照着它去算）。光把图发过去还不够 —— 模型可能仍旧按文本碎片猜。
    """
    messages = _capture_messages(client, sandbox, monkeypatch,
                                 {"formula_rects": [FORMULA_RECT]})
    text = _all_text(_user_content(messages))
    assert "高清" in text and "公式" in text, \
        "提示里没提「高清公式图」：\n" + text[-1200:]
    assert "猜测" in text or "猜" in text, \
        "提示里没禁止模型猜公式：\n" + text[-1200:]
    assert "为准" in text, "提示里没说「以图为准」：\n" + text[-1200:]


def test_qa_without_formula_rects_still_works(client, sandbox, monkeypatch):
    """没给公式坐标 → 退化成「只有整页图」，答疑不能因此挂掉。"""
    messages = _capture_messages(client, sandbox, monkeypatch)
    content = _user_content(messages)
    blocks = _image_blocks(content)
    assert len(blocks) == 1, "应只有一张整页图，实际 %d 张" % len(blocks)


def test_sanitizer_drops_every_kind_of_broken_rect():
    """⚠ 坐标校验必须在**服务端**自己做 —— 这是客户端可控的入参。

    为什么不能只靠下游的裁图函数兜底：
      · `x1 < x0`（反向）→ 裁图函数会抛「区域无效」，**碰巧**挡住了；
      · NaN / Infinity → `min(nan, 612)` 在 Python 里返回 nan →
        `cx1 - cx0` 是 nan → `nan <= 0` 为 **False** → **不会抛异常**，
        一路带着 nan 走进 PyMuPDF → 要么炸在里面，要么裁出一张空白图
        冒充公式（更糟：模型会拿它当依据）。
      → 所以这一层校验是**不可省**的，且必须自己测。
    """
    from microbench.app import _sanitize_formula_rects

    out = _sanitize_formula_rects([
        [float("nan"), 1.0, 2.0, 3.0],       # NaN
        [float("inf"), 1.0, 2.0, 3.0],       # +∞
        [float("-inf"), 1.0, 2.0, 3.0],      # -∞
        [300.0, 300.0, 100.0, 100.0],        # x1 < x0
        [300.0, 100.0, 300.0, 100.0],        # y1 < y0
        [10.0, 10.0],                        # 不是四元组
        [10.0, 10.0, 20.0],                  # 三个数
        ["a", "b", "c", "d"],                # 非数字
        None,                                # 不是列表
        [10.0, 10.0, 20.0, 20.0],            # 合法 → 必须留下
    ])
    assert len(out) == 1, "只应留下 1 条合法坐标，实际：" + repr(out)
    assert out[0] == (10.0, 10.0, 20.0, 20.0), out


def test_sanitizer_caps_the_number_of_rects():
    """一次最多 16 条 —— 再多对一次问答没意义，也免得把请求撑爆。"""
    from microbench.app import _sanitize_formula_rects

    many = [[float(i), 1.0, 2.0, 3.0] for i in range(50)]
    assert len(_sanitize_formula_rects(many)) <= 16


def test_broken_formula_rects_do_not_break_the_qa(client, sandbox, monkeypatch):
    """畸形坐标 → 不会变成额外的图片，答疑照常进行。

    ⚠ 分两层，各挡各的：
      · **非数字字符串**（"a"）→ pydantic 在**模型层**就拒了 → 整个请求 422；
      · **数字但畸形**（NaN / Infinity / 反向 / 不是四元组）→ 能进服务端，
        由 `_sanitize_formula_rects()` 逐条丢掉，其余坐标照常使用
        （这一层的细节由 test_sanitizer_drops_every_kind_of_broken_rect 细测）。
    """
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "formula_rects": [
            # ⚠ 用**字符串** "NaN" 而不是浮点 NaN：
            #   浮点 NaN 连请求体都发不出去（httpx 用 json.dumps(allow_nan=False)
            #   → 直接抛 "Out of range float values are not JSON compliant"），
            #   那样测的是客户端序列化，不是服务端的校验。
            ["NaN", 1.0, 2.0, 3.0],
            ["Infinity", 1.0, 2.0, 3.0],
            [300.0, 300.0, 100.0, 100.0],      # x1 < x0
            [10.0, 10.0],                        # 不是四元组
            [10.0, 10.0, 20.0, 20.0],           # 合法，应被保留
        ],
    })
    blocks = _image_blocks(_user_content(messages))
    assert len(blocks) == 2, \
        "应只剩 1 张整页图 + 1 张合法公式图，实际 %d 张" % len(blocks)


def test_non_numeric_formula_rects_are_rejected_by_the_schema(client, sandbox, monkeypatch):
    """坐标里塞非数字 → **422**（pydantic 模型层就挡住，不会进到裁图逻辑）。

    这是第一道防线：比服务端自己校验更早、更严格。
    """
    from microbench import llm as llm_mod

    async def fake_chat_stream_async(**kwargs):
        yield "（桩回答）"

    monkeypatch.setattr(llm_mod, "is_configured", lambda: True)
    monkeypatch.setattr(llm_mod, "chat_stream_async", fake_chat_stream_async)
    _make_paper(sandbox / PDF_REL)

    res = client.post("/api/paper/explain_selection_stream", json={
        "selected_text": "x", "question": "y",
        "pdf_relative_path": PDF_REL, "page_num": PAGE_NO,
        "formula_rects": [["a", "b", "c", "d"]],
    })
    assert res.status_code == 422, res.text
    assert "formula_rects" in res.text, res.text


def test_formula_rects_outside_the_pdf_are_dropped(client, sandbox, monkeypatch):
    """坐标落在页面外 → 不裁（否则会拿到一张空白图冒充公式）。"""
    messages = _capture_messages(client, sandbox, monkeypatch, {
        "formula_rects": [[5000.0, 5000.0, 6000.0, 6000.0]],
    })
    blocks = _image_blocks(_user_content(messages))
    assert len(blocks) == 1, \
        "越界的坐标也去裁了图（应是空白图），实际 %d 张" % len(blocks)


# ---------------------------------------------------------------------------
# 2) 前端：node 切片跑 index.html 里的**真实函数**
# ---------------------------------------------------------------------------
def test_qa_formula_frontend_node_guardrail_passes():
    """前端行为级护栏必须通过（node 跑真实函数，含变异自检）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("没装 node —— 划词答疑前端行为护栏跳过")
    proc = subprocess.run([node, str(JS_TEST)], capture_output=True,
                          text=True, encoding="utf-8", timeout=180)
    assert proc.returncode == 0, (
        "划词答疑前端护栏失败:\n" + proc.stdout + "\n" + proc.stderr)
    assert "FAIL 0" in proc.stdout, proc.stdout
    # 变异自检必须真的抓到东西 —— 否则这套断言是恒真的
    import re
    m = re.search(r"变异捕获\s+(\d+)/(\d+)", proc.stdout)
    assert m, "护栏输出里没有变异自检结果 —— 断言可能是恒真的：\n" + proc.stdout
    caught, total = int(m.group(1)), int(m.group(2))
    assert total > 0, "变异自检一个都没配 —— 等于没写：\n" + proc.stdout
    assert caught == total, \
        f"变异只抓到 {caught}/{total} —— 有一套断言是恒真的：\n" + proc.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
