# -*- coding: utf-8 -*-
"""段落级译文 —— 「译文按原文排版」（双语同位对照）的翻译侧。

用户诉求（2026-11-12）：
    "就像那种翻译插件一样，他们和原文的排版是一模一样的，就是没什么区别。
     原文是双栏，他们也是双栏。"

**为什么整页译文做不到**：现在的 `/api/paper/translate_page` 是**整页一次
翻译**，产物是一整段 Markdown 流 —— 它不知道"第几段对应原文哪个位置"。
要同位渲染，必须**按段落**翻译，并且译文能**对回段号**。

**协议照抄现成的 `⟦TABLE_n⟧`**（见 `app.py` 的 `_prepare_tables`）：
    输入段：⟦B0⟧原文…
    输出段：⟦T0⟧译文…⟦/T0⟧
    → 调用次数**不变**（还是每页一次），不撞 RPM 限流
    → 漏段有现成的兜底套路（缺的那几段合并起来再问一次）

**为什么另起一个模块**：`app.py` 已经 250KB+；这一层有独立的判据
（协议 / 解析 / 兜底），单独成文件才好测。
"""
from __future__ import annotations

import re
import time
from typing import Callable, Iterable, Optional

# 输出段标记：⟦T12⟧译文⟦/T12⟧
_BLOCK_OUT_RE = re.compile(r"⟦T(\d{1,3})⟧(.*?)⟦/T\1⟧", re.S)
# 输入段标记：⟦B12⟧
_BLOCK_IN_RE = re.compile(r"^⟦B(\d{1,3})⟧", re.M)
# 只给开标记（见 parse_block_translations 的注释：模型经常忘写闭标记）
_BLOCK_OUT_OPEN_RE = re.compile(r"⟦T(\d{1,3})⟧")
_BLOCK_CLOSE_RE = re.compile(r"⟦/T\d{1,3}⟧")

# 对外暴露的标记样例（前端/测试用它认协议）
BLOCK_OUT_MARKER = "⟦T0⟧"

# 段落级翻译的补充提示词（漏段时重问用）
_BLOCK_SYS = (
    "你是学术论文翻译助手。下面给你论文一页的若干个段落，"
    "每段前面有编号标记（例如 ⟦B0⟧、⟦B1⟧）。\n\n"
    "请把每一段翻译成中文，**逐段输出**，格式严格如下：\n"
    "⟦T0⟧第 0 段的中文译文⟦/T0⟧\n"
    "⟦T1⟧第 1 段的中文译文⟦/T1⟧\n\n"
    "规则：\n"
    "1. **编号必须原样保留**：输入标记是 ⟦B7⟧，输出就必须是 ⟦T7⟧"
    "（编号可能不连续，比如 0、2、5 —— 不要重新编号、不要补齐）。\n"
    "2. 段落的**顺序**必须与原文一致，不要合并、不要拆分、不要遗漏、不要编号。\n"
    "3. 严格忠于原文，不要添加原文没有的内容。\n"
    "4. 保留术语的英文原文（如 TMR、spin-transfer torque）。\n"
    "5. 公式用 $...$ 原样保留，不要翻译或改写公式。\n"
    "6. 输出里只允许出现 ⟦T数字⟧ 与 ⟦/T数字⟧ 这两种标记。\n"
)

# 补问时用（只翻译指定的几段）
_BLOCK_SYS_FEW = (
    "你是学术论文翻译助手。下面给你论文的**几段**原文，"
    "每段前面有编号标记（例如 ⟦B3⟧、⟦B7⟧）。\n\n"
    "请把每一段翻译成中文，**逐段输出**，格式严格如下：\n"
    "⟦T3⟧第 3 段的中文译文⟦/T3⟧\n"
    "⟦T7⟧第 7 段的中文译文⟦/T7⟧\n\n"
    "规则：\n"
    "1. **编号必须原样保留**（⟦B3⟧ → ⟦T3⟧），编号可能不连续，不要重新编号。\n"
    "2. 段落的顺序与原文一致，不要合并、不要拆分、不要遗漏。\n"
    "3. 严格忠于原文。\n"
    "4. 保留术语的英文原文；公式用 $...$ 原样保留。\n"
    "5. 输出里只允许出现 ⟦T数字⟧ 与 ⟦/T数字⟧ 这两种标记。\n"
)

# 段数对不上时的兜底标记
UNTRANSLATED = "【本段未能翻译，请对照原文】"

# ⚠ **独立版本闸门**（2026-11-12）
#
# 为什么不能动 `paper_text.EXTRACTOR_VERSION`：
#   它一动 → 用户**全部**整页译文作废重翻，而且跨设备同步会波及另一台。
#   段落块是**增量**产物（页级文本逐字未变，实测 77 页 A/B 一致），
#   它有自己的版本号 —— 段落切分算法改了，只有段落译文作废，整页译文无恙。
#
# ⚠ 这个版本号**只**管段落译文，不要拿去当抽取层版本用（那是 EXTRACTOR_VERSION 的活）。
BLOCK_TRANSLATION_VERSION = 1


def build_block_input(blocks: list[dict]) -> str:
    """把段落块拼成带编号的输入文本。"""
    parts = []
    for b in blocks:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        parts.append("⟦B%d⟧\n%s" % (int(b.get("i", 0)), text))
    return "\n\n".join(parts)


def blocks_signature(blocks: list[dict]) -> str:
    """段落块的**内容签名** —— 用作缓存键的文本部分。

    为什么要签名而不是直接用 `build_block_input`：
      · 签名要含 **blocks_version** —— 段落切分算法改了 → 签名变 → 缓存 miss
        → 旧译文不会被当成新的用（这正是"独立版本闸门"）；
      · 签名要含**每一段的原文** —— 原文变了（PDF 改了）→ 签名变 → 缓存 miss。
      · 签名**不含 bbox** —— 坐标变了但文字没变时，译文照样能用
        （坐标只影响贴图位置，不影响译文内容）。
    """
    parts = ["v%d" % BLOCK_TRANSLATION_VERSION]
    for b in blocks or []:
        parts.append("%s:%s" % (b.get("i", 0), (b.get("text") or "").strip()))
    return "\n".join(parts)


def concatenate_blocks(blocks: list[dict]) -> str:
    """把段落译文拼成一份**整页译文**（段落间空行分隔）。

    为什么要有：现有的快照结构里，每页只存**一个** ``translation`` 字段
    （整页译文）。段落译文是**另一种**形态，如果只有它，快照就存不下
    —— 整页译文那份要么是空的（前端要另存一份，等于两份真相），
    要么被段落译文顶替（用户点"整页译文"看到的是拼接结果，格式变了）。

    → 两种形态**都**产出：``blocks`` 给对照模式用（带坐标），
      ``translation`` 给整页面板用（老样子）。
    """
    out: list[str] = []
    for b in blocks or []:
        t = (b.get("translation") or "").strip()
        if not t:
            continue
        out.append(t)
    return "\n\n".join(out).strip()


def _flatten_inline_newlines(txt: str) -> str:
    """把**单个**换行换成空格（双换行是真分段，保留）。

    为什么：PDF 抽出来的段落是**一行一行**的，行折会原样进到译文里 →
    译文里下标/上标被拆成单独一行（实测 JACERS 第 2 页：
    "其中，a 为横向 ME 电压系数，v 为整体比\\nE,31\\nE 表示整个…"）。
    段落是一句话 → 行折必须合成空格。
    """
    return re.sub(r"(?<!\n)\n(?!\n)", " ", txt or "").strip()


def parse_block_translations(raw: str, want_ids: Optional[set[int]] = None) -> dict[int, str]:
    """从模型返回里解析出每段的译文。

    返回 {段号: 译文}。解析不出来就返回空 dict（调用方走兜底）。

    ⚠⚠ **必须容忍「只有开标记、没有闭标记」的返回**（2026-11-12 实测）：
      JACERS 第 2 页共 12 段，模型把编号**全写对了**（T0/T1/T2/T4/T5…/T13，
      连跳号都对），却**一个 ⟦/Tn⟧ 都没写** → 严格解析一段都拿不到
      → 整页 502「模型没有按段落编号返回译文」。
      真相是模型**听话**，只是忘了闭标记 —— 不能因此把整页的活判死。

    所以这里**两段式**：
      ① 严格：有 ⟦T5⟧…⟦/T5⟧ 就按成对解析（最准，闭标记能切掉尾随内容）；
      ② 兜底：**按下一个开标记切段**；段内若出现闭标记，就截到闭标记为止。
      两段合并（① 优先），同一段号以严格结果为准。
    """
    out: dict[int, str] = {}
    text = raw or ""

    def _keep(idx: int, txt: str) -> None:
        txt = _flatten_inline_newlines(txt)
        if not txt:
            return
        if want_ids is not None and idx not in want_ids:
            return
        out[idx] = txt

    # ① 严格：成对的 ⟦T5⟧…⟦/T5⟧
    for m in _BLOCK_OUT_RE.finditer(text):
        _keep(int(m.group(1)), m.group(2))

    # ② 兜底：按开标记切段（模型忘了闭标记时靠这个）
    opens = list(_BLOCK_OUT_OPEN_RE.finditer(text))
    for i, m in enumerate(opens):
        idx = int(m.group(1))
        if idx in out:
            continue
        end = opens[i + 1].start() if i + 1 < len(opens) else len(text)
        chunk = text[m.end():end]
        cm = _BLOCK_CLOSE_RE.search(chunk)
        if cm:
            chunk = chunk[:cm.start()]
        _keep(idx, chunk)
    return out


def _align_by_paragraphs(raw: str, blocks: list[dict]) -> dict[int, str]:
    """模型没按标记输出时的兜底：按**空行**切段，段数对得上就按顺序对齐。

    为什么要有：模型偶尔会忽略标记直接输出译文（很常见）。
    段数一致时按顺序对齐是安全的；不一致就返回空（宁可让前端退回整页模式，
    也不能把译文**贴到错的段落**上 —— 那比没有更糟）。
    """
    if not raw:
        return {}
    paras = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    # 去掉模型可能加的引号/标题行
    paras = [p for p in paras if not p.startswith("⟦")]
    ids = [int(b.get("i", 0)) for b in blocks
           if (b.get("text") or "").strip()]
    if len(paras) != len(ids):
        return {}
    return {i: _flatten_inline_newlines(p) for i, p in zip(ids, paras)}


async def translate_blocks(
    blocks: list[dict],
    chat: Callable[..., "object"],
    model: str = "",
    m_name: str = "",
    m_fallback: bool = False,
    max_tokens: int = 16000,
    deadline: Optional[float] = None,
    call_info: Optional[dict] = None,
    disable_thinking: bool = True,
    skip_kinds: Iterable[str] = ("formula", "table"),
) -> tuple[list[dict], str]:
    """把一页的段落块翻译掉，译文回填到每块的 ``translation`` 字段。

    返回 (blocks, stop_reason)：
      - blocks：原样返回，每块多了 ``translation``（可能是 UNTRANSLATED）
      - stop_reason："" 正常 / "deadline" 超时 / "parse" 模型没按格式返回 /
        "partial" 有段落没译出来

    ⚠ ``chat`` 必须是 **async 可调用对象**（内部直接 await）。
      `llm.chat` 是**同步**的 requests 调用 → 调用方要包一层
      `asyncio.to_thread`（AGENTS.md §4.58：直接调会占住事件循环）。

    ⚠ ``skip_kinds`` 里的块**不送模型**（默认 formula / table）：
        公式的文本层是乱序碎片 → 模型会"顺手补全"出原文里根本没有的公式
        （**错公式比没公式危险得多**，AGENTS.md §4.6）；
        表格的数值在文本层会整行错位 → 要裁图让多模态逐格抄（§4.13）。
        这两类块的 ``translation`` 留空，由调用方自己填。
    """
    blocks = [dict(b) for b in blocks]
    skip = set(skip_kinds or ())
    todo = [b for b in blocks
            if (b.get("text") or "").strip() and b.get("kind") not in skip]
    for b in blocks:
        b["translation"] = ""
    if not todo:
        return blocks, ""

    if deadline is None:
        deadline = time.monotonic() + 300
    remaining = deadline - time.monotonic()
    if remaining <= 5:
        return blocks, "deadline"

    user_msg = build_block_input(todo)
    call_timeout = int(max(20, min(300, remaining / 2)))
    try:
        raw = await chat(
            messages=[
                {"role": "system", "content": _BLOCK_SYS},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            timeout=call_timeout,
            # 翻译要的是「同一段原文 → 同一份译文」，temperature 必须为 0；
            # 且直译任务关掉思考（快 4 倍、不会在推理里打转到正文为空）。
            disable_thinking=disable_thinking,
            model_name=m_name,
            force_fallback=m_fallback,
            deadline=deadline,
            ret=call_info,
        )
    except Exception:
        raise

    got = parse_block_translations(raw)
    if not got:
        got = _align_by_paragraphs(raw, todo)

    # ---- 兜底一：漏段的合并成一次补问 ----
    missing = [b for b in todo if int(b.get("i", 0)) not in got]
    if missing and (deadline - time.monotonic()) > 8:
        try:
            again = await chat(
                messages=[
                    {"role": "system", "content": _BLOCK_SYS_FEW},
                    {"role": "user", "content": build_block_input(missing)},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
                timeout=int(max(20, min(180, (deadline - time.monotonic()) / 2))),
                disable_thinking=disable_thinking,
                model_name=m_name,
                force_fallback=m_fallback,
                deadline=deadline,
                ret=None,
            )
            got.update(parse_block_translations(again))
        except Exception:
            pass

    for b in blocks:
        idx = int(b.get("i", 0))
        # ⚠ 只给**送进模型的**段填 UNTRANSLATED。
        #   被跳过的块（公式/表格）译文是 "" —— 由调用方自己填
        #   （公式贴原图、表格贴多模态抄出来的 Markdown），
        #   这里要是也填上"未能翻译"，前端就会把公式图换成一句提示。
        if b.get("kind") in skip:
            continue
        b["translation"] = got.get(idx) or UNTRANSLATED

    reason = ""
    if not got:
        reason = "parse"
    elif any(b.get("translation") == UNTRANSLATED for b in blocks):
        reason = "partial"
    return blocks, reason


__all__ = [
    "BLOCK_OUT_MARKER",
    "BLOCK_TRANSLATION_VERSION",
    "UNTRANSLATED",
    "blocks_signature",
    "build_block_input",
    "concatenate_blocks",
    "parse_block_translations",
    "translate_blocks",
]
