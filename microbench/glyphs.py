"""
解不出的数学符号：看图认字 + 词典缓存。

**问题**（2026-10-25 用户反馈）：论文里凡是 "×"、"≈"、"≥" 这类符号，
译文里全变成 "[?]" —— 读不出来。

**根因**：TeX 排版的论文用的数学字体（MathematicalPi-Three / CMEX10 /
AdvP4C4E74 …）在 PDF 里**没有完整的 ToUnicode 映射**，pdfplumber 只能吐
"(cid:3)"。抽取层为了防止模型**瞎编**（曾把 cid 猜成 \\approx / \\times，
编出论文里根本没有的公式），把 cid 换成 "[?]"。

**为什么"看图认字"是对的**：这些字形在 PDF 里是**画出来的**（有字形轮廓），
只是文本层映射不出来。裁一小块图交给多模态模型，它认得 ≈ 和 ×。
这和表格读图、公式裁图是同一套思路：**图上什么样，就是什么样**。

**为什么要有词典**：同一个符号会在整份论文里反复出现（实测 JACERS 一页 3 个，
全库 391 页共 116 个唯一字形）。认一次就存下来，之后查表即可 ——
零模型调用、零成本、跨论文复用。

**安全阀**：模型给的符号必须在白名单内（数学符号集），否则丢弃 → 退回 "[?]"。
宁可空着，也不能指错（指错等于编造，AGENTS.md §4.6 的教训）。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

from . import llm

GLYPH_MARKER_RE = re.compile(r"⟦G:(\d+)⟧")

# 只接受这些符号（模型给的其它东西一律丢弃 → 退回 "[?]"）
ALLOWED_SYMBOLS = set(
    "≈≠≤≥<>×÷±∓∝∂∆√∞°′″⋅∘∗"
    "αβγδεζηθικλμνξορστυφχψω"
    "ΓΔΘΛΞΠΣΥΦΨΩ"
    "∈∉⊂⊃⊆⊇∪∩∅∇⊕⊗¬∧∨"
    "→←↔⇒⇔∀∃∅ℝℕℤℚ"
)

# 词典文件（放 vault 的 logs 下 → 两台设备通过坚果云共用同一份）
DEFAULT_DICT_PATH = Path("logs") / "glyph_dict.json"

# 归一化位图边长（16×16 = 256 bit，对形状足够敏感，又对字号/抗锯齿不敏感）
_NORM_SIZE = 16
# 汉明距离容差：256 位里允许差这么多位仍算同一个符号。
# 实测（JACERS 综述的 ≈ ≥ × 三个真符号）：
#   同一符号渲染在不同倍率 → 差 4 / 10 / 26 位
#   不同符号之间         → 差 75 / 84 / 105 位
# 所以 40 是个安全阈值：既能认出"同一个符号的不同字号/不同倍率"，
# 又远小于不同符号之间的距离（不会把 ≈ 认成 ≥）。
#
# ⚠️ 但这个阈值只在**同一次渲染参数**下可信（见 _MATCH_MAX_DIFF 的实测补充）：
# 全库 24 篇实测（生产渲染代码 + 真实产物）显示，跨文档的"同一符号不同实例"
# 汉明差中位数是 61，"异符号"中位数是 74 —— 两者重叠 → 纯形状匹配既
# 命中不了又会把 ≈ 认成 ≥。所以形状匹配**降级为二级校验**，
# 真正的主键是语义键（见 semantic_key）。
_MATCH_MAX_DIFF = 40


def semantic_key(font: str, cid: str) -> str:
    """符号的**语义键**：字体基名 + 字符码。

    为什么用它当主键（2026-10-25 实测）：
      - PDF 里的字体名带子集前缀（"JNKPHE+MathematicalPi-Three"），
        同一个原始字体在不同论文里前缀不同 → 去掉 "+" 前缀才跨文档稳定。
      - (基名, 字符码) 在**同一篇论文里**就是同一个字形：TeX 数学字体
        （MathematicalPi-* / CMEX10 / Universal-GreekwithMathPi …）的
        字符码是固定的，cid=2 永远是同一个符号。
      - 全库实测：24 篇论文、6184 个字形实例 → 只有 **101** 个不同的
        语义键，其中 76 个跨 ≥2 篇论文重复 → 词典天然可跨论文复用。
      - 对比形状指纹：同一符号的跨实例汉明差中位 61、异符号 74 → 重叠，
        靠形状匹配必然要么命不中、要么认错（把 ≈ 认成 ≥）。

    语义键的**唯一风险**：两篇论文里同名基名的字体其实是不同字形。
    这种情况由 _shape_matches 做二级校验兜住（对不上就当没认出来，
    退回 "[?]"，绝不猜 —— AGENTS.md §4.6）。
    """
    base = (font or "").split("+")[-1].strip()
    return f"{base}|{cid}"

_RECOGNIZE_PROMPT = (
    "下面每个编号后面，是从一篇英文论文里裁出来的一个数学符号（可能是 ×、≈、≥ "
    "这类关系符号，也可能是希腊字母）。\n"
    "请按编号顺序说出每个符号**本身**，用英文逗号分隔，格式：1=×, 2=≈\n"
    "看不清或拿不准的写 ?。只回答符号，不要解释。"
)


def strip_markers(text: str) -> str:
    """把没认出来的 ⟦G:n⟧ 退回成 "[?]"（模型绝不能看见这种内部标记）。"""
    if not text:
        return text
    return GLYPH_MARKER_RE.sub("[?]", text)


def _open_doc(pdf_source):
    """pdf_source 可以是路径或字节。返回 (fitz.Document, is_temp_path)。"""
    import fitz
    if isinstance(pdf_source, (bytes, bytearray)):
        return fitz.open(stream=bytes(pdf_source), filetype="pdf"), False
    return fitz.open(str(pdf_source)), True


def _render_glyph(doc, page_no: int, bbox: dict, zoom: float = 12.0) -> Optional[bytes]:
    """裁出一个字形的小图（PNG 字节）。"""
    try:
        import fitz
    except Exception:
        return None
    try:
        page = doc[page_no - 1]
    except Exception:
        return None
    try:
        x0, x1 = float(bbox["x0"]), float(bbox["x1"])
        top, bottom = float(bbox["top"]), float(bbox["bottom"])
        # PyMuPDF 的 Rect 与 pdfplumber 一样是**左上角原点**（y 往页顶方向为正，
        # 但数值从页顶往下量）—— 不要按 PDF 原生的左下角去换算，
        # 换错了会裁到旁边的文字（实测裁出 "va" / "se"，模型把乱图认成了 ν）。
        rect = fitz.Rect(x0 - 1.0, top - 1.0, x1 + 1.0, bottom + 1.0)
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            return None
        pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None


def _shape_bits(png: bytes) -> str:
    """把字形图归一化成 16×16 的二值位图（hex 字符串，256 bit）。

    做法：灰度 → 裁到墨迹 → 面积平均（BOX）缩到 16×16 → 二值化。
    二值化让抗锯齿、轻微的渲染差异消失。

    **不用精确哈希**：同一个字形在不同字号下，归一化后的采样点位置会差几个
    像素 → 精确 sha1 完全不同 → 词典永远不命中（实测同一形状 16×16 与 32×32
    指纹不同）。改成按汉明距离容差匹配（见 _hamming / GlyphDictionary.get）。

    ⚠️ 这个版本**拉伸**到正方形，会丢掉长宽比 → 同一符号在不同字号下
    差异很大（实测跨文档组内差中位 61）。它是**旧词典格式**的基准，
    保留以兼容既有文件；新的语义键方案用 _shape_bits_stable。
    """
    try:
        from PIL import Image
    except Exception:
        return ""
    try:
        img = Image.open(io.BytesIO(png)).convert("L")
        inv = img.point(lambda p: 255 - p)
        box = inv.getbbox()
        if not box:
            return ""
        resample = getattr(Image, "Resampling", Image)
        img = img.crop(box).resize((_NORM_SIZE, _NORM_SIZE), resample.BOX)
        # 1 bit / 像素（256 位）：用查表法阈值化，不走 convert("1") 的抖动
        bits = img.point([0] * 128 + [255] * 128, mode="1").tobytes()
        return bits.hex()
    except Exception:
        return ""


def _shape_bits_stable(png: bytes) -> str:
    """把字形图归一化成 16×16 二值位图（**等比补白**，不拉伸）。

    为什么不用拉伸版：拉伸会把长宽比压掉 → 同一个符号在字号不同、
    上下文不同（上下标）时形状差很多。实测（生产渲染代码 + 全库真实产物）：
      拉伸版：同语义键跨文档 汉明差 中位 61 / p90 48+，且异符号中位 74
             → 阈值 40 既命不中又会把 ≈ 认成 ≥
      等比补白：同语义键跨文档 汉明差 **中位 0** / p75 2 → 极稳定
    代价是异符号之间也变近（中位 47）→ 所以它只能当**二级校验**，
    不能单独当主键（主键是语义键）。

    等比缩放到正方形会改变长宽比 → 保留长宽比、居中补白到正方形。
    """
    try:
        from PIL import Image
    except Exception:
        return ""
    try:
        img = Image.open(io.BytesIO(png)).convert("L")
        inv = img.point(lambda p: 255 - p)
        box = inv.getbbox()
        if not box:
            return ""
        img = img.crop(box)
        w, h = img.size
        side = max(w, h)
        canvas = Image.new("L", (side, side), 255)
        canvas.paste(img, ((side - w) // 2, (side - h) // 2))
        resample = getattr(Image, "Resampling", Image)
        canvas = canvas.resize((_NORM_SIZE, _NORM_SIZE), resample.BOX)
        return canvas.point([0] * 128 + [255] * 128, mode="1").tobytes().hex()
    except Exception:
        return ""


# 二级校验（形状）用的汉明容差。
# 标定数据（全库 24 篇 / 6184 实例，生产渲染代码）：
#   同语义键·跨文档：p50=0  p75=2  p90=48  max=81
#   异语义键·同基名：p5=7  p25=27  p50=47  min=0
# 语义键本身已经是可靠主键（同键跨文档形状几乎完全一致），
# 形状校验只用来挡住"极端不像"的情况 → 阈值放宽到 96（256 位里的 37.5%）：
#   同键误杀率 ≈ 0%（max 才 81）
#   异键漏放：min=0 那类（不同语义键但形状完全相同）本来就没法靠形状区分
_SHAPE_GUARD_MAX_DIFF = 96


def _hamming(a: str, b: str) -> int:
    """两个 hex 位图的不同位数（越少越像）。"""
    if not a or not b or len(a) != len(b):
        return 10 ** 9
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 10 ** 9


class GlyphDictionary:
    """字形 → 符号 的持久化词典。

    **双层结构**（2026-10-25 改造）：

    - 主键 = **语义键** `semantic_key(font, cid)` = 字体基名|字符码。
      跨论文、跨设备稳定 → 认一次就长期命中，零模型调用。
    - 二级校验 = 形状指纹（`_shape_bits`）。同一个语义键在不同论文里
      渲染出的形状应当接近；对不上就**当没认出来**（退回 "[?]"，绝不猜）。

    词典文件格式（向后兼容旧文件）：
      {"s:<语义键>": {"sym": "×", "shape": "<hex>"},
       "<旧版纯 hex 形状键>": "×"}
    旧版条目（纯 hex → 符号）继续可读，作为"只有形状线索"的兜底。
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_DICT_PATH
        self.data: dict = {}
        self.dirty = False

    def load(self) -> "GlyphDictionary":
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = {}
                    for k, v in raw.items():
                        if isinstance(v, dict):
                            sym = str(v.get("sym") or "")
                            if sym:
                                self.data[str(k)] = {
                                    "sym": sym,
                                    "shape": str(v.get("shape") or ""),
                                }
                        elif isinstance(v, str) and v:
                            # 旧格式：形状指纹 → 符号
                            self.data[str(k)] = {"sym": v, "shape": ""}
        except Exception:
            self.data = {}
        return self

    def save(self) -> None:
        if not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1,
                                      sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
            self.dirty = False
        except Exception as e:
            print(f"[MicroBench] 符号词典写入失败: {e}", file=sys.stderr)

    def get(self, key: str) -> Optional[str]:
        """按**语义键**取符号；没命中再退回旧的形状指纹匹配。

        形状匹配只在**旧格式条目**（shape 为空的那批）上做 —— 那批没有
        形状基准可比，纯靠距离最近来找，属兜底行为。
        """
        if not key:
            return None
        ent = self.data.get(key)
        if isinstance(ent, dict):
            return ent.get("sym") or None
        if isinstance(ent, str) and ent:
            return ent
        # 旧格式兜底：形状指纹最近邻
        best, best_d = None, 10 ** 9
        for k, v in self.data.items():
            if not isinstance(v, dict) or v.get("shape"):
                continue          # 有形状基准的条目不参与纯距离匹配
            d = _hamming(key, k)
            if d < best_d:
                best, best_d = v.get("sym"), d
        if best is not None and best_d <= _MATCH_MAX_DIFF:
            return best
        return None

    def get_verified(self, sem_key: str, shape_key: str) -> Optional[str]:
        """取符号并做**形状二级校验**。

        语义键命中但形状对不上 → 返回 None（视为没认出来）。
        这样"两个同名基名的字体其实是不同字形"的情况不会串味，
        宁可退回 "[?]" 也不指错符号（AGENTS.md §4.6）。
        """
        ent = self.data.get(sem_key)
        sym = None
        if isinstance(ent, dict):
            sym = ent.get("sym") or None
        elif isinstance(ent, str):
            sym = ent or None
        if not sym:
            return None
        stored_shape = ""
        if isinstance(ent, dict):
            stored_shape = ent.get("shape") or ""
        if not stored_shape or not shape_key:
            # 老条目没有形状基准 → 采信（它当年就是这么用过的）
            return sym
        d = _hamming(shape_key, stored_shape)
        if d <= _SHAPE_GUARD_MAX_DIFF:
            return sym
        return None

    def put(self, key: str, ch: str, shape_key: str = "") -> None:
        """记一条"符号 → 字符"。

        **必须校验白名单**：这是最后一道闸 —— 万一模型说了个不该收的字符
        （"文"、"字"……），宁可丢掉，也不能让它进词典污染以后的翻译。

        key 传语义键（"s:CMEX10|18"）时存成带形状基准的条目；
        传旧的裸 hex 时保持旧行为（纯形状条目）。
        """
        if not key or not ch or len(ch) != 1:
            return
        if ch not in ALLOWED_SYMBOLS:
            return
        if key.startswith("s:"):
            old = self.data.get(key)
            old_sym = old.get("sym") if isinstance(old, dict) else (old if isinstance(old, str) else "")
            if old_sym == ch and (not shape_key or
                                  (isinstance(old, dict) and old.get("shape") == shape_key)):
                return
            self.data[key] = {"sym": ch, "shape": shape_key or ""}
            self.dirty = True
            return
        old = self.data.get(key)
        if old != ch:
            self.data[key] = ch
            self.dirty = True


def _compose_strip(items: list[tuple[str, bytes]]) -> Optional[bytes]:
    """把若干 (编号, 字形图) 拼成一张竖排图，编号在左、字形在右。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    if not items:
        return None
    try:
        cell = 64
        width = 320
        height = cell * len(items)
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)
        for i, (label, png) in enumerate(items):
            y = i * cell
            draw.text((6, y + cell // 3), label, fill="black")
            try:
                g = Image.open(io.BytesIO(png)).convert("RGB")
            except Exception:
                continue
            # 等比缩放到 cell 大小内
            scale = min((cell - 12) / max(1, g.width), (cell - 12) / max(1, g.height))
            nw, nh = max(1, int(g.width * scale)), max(1, int(g.height * scale))
            g = g.resize((nw, nh), Image.LANCZOS)
            canvas.paste(g, (48, y + (cell - nh) // 2))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _parse_recognition(raw: str, n: int) -> dict[int, str]:
    """解析模型的回答 → {序号: 符号}。只收白名单里的单字符。"""
    out: dict[int, str] = {}
    if not raw:
        return out
    text = raw.strip()
    # 优先解析 "1=×, 2=≈" 这种带编号的
    for m in re.finditer(r"(\d{1,2})\s*[=＝:：]\s*(\S)", text):
        try:
            idx = int(m.group(1))
        except ValueError:
            continue
        sym = m.group(2).strip()
        if 1 <= idx <= n and len(sym) == 1 and sym in ALLOWED_SYMBOLS:
            out[idx] = sym
    if out:
        return out
    # 退化：按顺序的逗号分隔列表
    parts = [p.strip() for p in re.split(r"[,，、;；\s]+", text) if p.strip()]
    for i, p in enumerate(parts[:n], start=1):
        if len(p) == 1 and p in ALLOWED_SYMBOLS:
            out[i] = p
    return out


# 一次多模态调用最多认多少个字形（拼图太高模型会读不动）
_BATCH = 20

# 单次认字调用的超时（秒）。实测主模型认 6 个字形要 29s，备用模型 48s。
# 给 90s 让慢路径能跑完，但**不让它无限拖**（llm.chat 内部还有重试预算）。
_RECOGNIZE_TIMEOUT = 90

# 认字回答的 token 预算。
# 每个符号的答案是 "12=×" 这样 4~5 个字符 → 20 个符号约需 100 token。
# 但**思考 token 与正文共享同一个 max_tokens 池**（主模型是推理型），
# 思考一烧就正文为空（实测 max_tokens=800 时被截断 0 字符）。
# 给足余量，认字这种"必须完整"的场景宁可多花 token 也不能整批作废。
_RECOGNIZE_MAX_TOKENS = 2000


def _recognize_batch(strip: bytes, count: int, model_name: str = "",
                     force_fallback: bool = False, deadline: Optional[float] = None,
                     isolated: bool = False) -> str:
    """把拼好的字形图交给多模态模型，返回原始回答文本。

    **isolated=True** → 走 llm.chat_isolated（只打主模型、不改全局降格状态）。
    后台补齐任务必须用它：认字失败是**这一小任务**的问题，
    不该触发全局降格把用户正在跑的翻译也拖去备用模型。

    抽成独立函数的原因：单页版和整篇版都要用，且**必须**保证
    data URL 的格式正确 —— 格式错了上游会返回 HTTP 400
    "image data url missing comma separator"，而不是清晰报错。
    """
    import base64
    content = [
        {"type": "text", "text": _RECOGNIZE_PROMPT},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,"
                      + base64.b64encode(strip).decode("ascii")}},
    ]
    messages = [
        {"role": "system",
         "content": "你是符号识别助手，只回答符号本身。"},
        {"role": "user", "content": content},
    ]
    if isolated:
        # 只打主模型、**不改全局降格状态**（后台补齐必须用它）。
        return llm.chat_isolated(
            messages,
            max_tokens=_RECOGNIZE_MAX_TOKENS,
            temperature=0.0,
            timeout=_RECOGNIZE_TIMEOUT,
            disable_thinking=True,
            model_name=model_name or None,
        )
    return llm.chat(
        messages=messages,
        max_tokens=_RECOGNIZE_MAX_TOKENS,
        temperature=0.0,
        timeout=_RECOGNIZE_TIMEOUT,
        # 必须关思考：主模型是推理型，思考会把 token 预算吃光，
        # 正文一个字都吐不出来（实测 max_tokens=300 → 被截断、0 字符）
        disable_thinking=llm.translation_disable_thinking(),
        model_name=model_name or None,
        force_fallback=force_fallback,
        deadline=deadline,
    )


def resolve_document_glyphs(pages: list, pdf_source, model_name: str = "",
                            force_fallback: bool = False, dict_path=None,
                            deadline: Optional[float] = None,
                            recognize: bool = True,
                            isolated: bool = False) -> dict:
    """**整篇一次性**解析所有页里的解不出符号。

    为什么必须有这个函数（2026-10-25 用户反馈"上传本地 pdf 半天没反应"）：

    旧实现是**逐页**调 `_resolve_page_glyphs`，每页一次多模态请求。
    实测 2008-JAP 综述（36 页 / 35 页含符号）→ 35 次**串行**请求 ×
    29~48s/次 = **8 分钟以上**，而 43s 的那篇只是 3 页含符号的。
    → 用户看到的就是"点一下然后半天没反应"。

    现在改成：
      1. 扫**整篇**，按语义键（字体基名|字符码）全局去重
         → 6184 个字形实例只剩 101 个不同符号（每篇 1~46 个）
      2. 每个语义键只渲染一次
      3. 词典命中（跨论文复用）→ 零模型调用
      4. 未命中的**一次性批量**送模型（每批 20 个，全篇通常 1~3 批）
      5. 认不出来 → "[?]"，绝不猜

    返回统计信息，便于接口把"识别了几个符号"告诉前端。
    """
    stats = {"pages": 0, "glyph_pages": 0, "instances": 0,
             "unique": 0, "resolved": 0, "model_calls": 0, "failed": False}
    if not pages:
        return stats

    dictionary = GlyphDictionary(dict_path).load()
    resolved_by_sem: dict[str, str] = {}
    doc = None
    try:
        doc, _ = _open_doc(pdf_source)

        # ---- 1) 整篇扫描：按语义键全局去重 ----
        # reps[sem_key] = (png, shape, [(page_no, glyph_index), ...])
        reps: dict[str, tuple[bytes, str, list]] = {}
        for p in pages:
            glyphs = p.get("glyphs") or []
            if not glyphs:
                continue
            stats["glyph_pages"] += 1
            stats["instances"] += len(glyphs)
            for gi, g in enumerate(glyphs):
                sk = "s:" + semantic_key(g.get("font", ""), g.get("cid", ""))
                ent = reps.get(sk)
                if ent is None:
                    png = _render_glyph(doc, p["page"], g)
                    ent = (png or b"", _shape_bits_stable(png) if png else "", [])
                    reps[sk] = ent
                ent[2].append((p["page"], gi))
        stats["pages"] = len(pages)
        stats["unique"] = len(reps)
        if not reps:
            return stats

        # ---- 2) 词典命中 → 直接用（跨论文复用，零模型调用）----
        resolved_by_sem: dict[str, str] = {}
        pending: list[str] = []
        for sk, (_png, shape, _uses) in reps.items():
            if not shape:
                continue
            hit = dictionary.get_verified(sk, shape)
            if hit:
                resolved_by_sem[sk] = hit
            else:
                pending.append(sk)

        # ---- 3) 未命中的：一次性批量送多模态 ----
        # recognize=False → 只查词典、不调模型（打开论文走这条：亚秒级）
        batches = range(0, len(pending), _BATCH) if recognize else range(0)
        for start in batches:
            if deadline is not None and time.monotonic() >= deadline:
                print("[MicroBench] 符号认字超出时间预算，剩余退回 [?]",
                      file=sys.stderr)
                stats["failed"] = True
                break
            batch = pending[start:start + _BATCH]
            items = []
            for sk in batch:
                png, shape, _uses = reps[sk]
                items.append((sk, shape))
            strip = _compose_strip([(str(i + 1), reps[sk][0])
                                    for i, (sk, _shape) in enumerate(items)])
            if not strip:
                continue
            try:
                stats["model_calls"] += 1
                raw = _recognize_batch(strip, len(items), model_name,
                                       force_fallback, deadline=deadline,
                                       isolated=isolated)
                parsed = _parse_recognition(raw, len(items))
                for i, (sk, shape) in enumerate(items, start=1):
                    sym = parsed.get(i)
                    if sym:
                        dictionary.put(sk, sym, shape)
                        resolved_by_sem[sk] = sym
            except Exception as e:
                stats["failed"] = True
                print(f"[MicroBench] 符号认字失败（退回 [?]）: {e}", file=sys.stderr)
                break      # 认不出来就整篇退回 [?]，不再空转更多批

        # ---- 4) 回填所有页（在 finally 里执行，保证异常路径也不漏标记）----
        stats["resolved"] = len(resolved_by_sem)
        try:
            dictionary.save()
        except Exception:
            pass
    except Exception as e:
        stats["failed"] = True
        print(f"[MicroBench] 整篇符号解析异常（退回 [?]）: {e}", file=sys.stderr)
    finally:
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass
        # ⚠️ **必须**在这里回填，而不是放在 try 里：
        #   打开 PDF 失败 / 渲染失败 / 认字抛异常 等任何一条路径，
        #   如果跳过回填，⟦G:n⟧ 内部标记就会**原样进 prompt** ——
        #   模型会照抄或瞎编（AGENTS.md §4.6 红线）。
        #   认不出来的符号一律 "[?]"（宁可空着也不能指错）。
        for _p in pages:
            _g = _p.get("glyphs") or []
            if not _g:
                continue
            _txt = _p.get("text") or ""
            if not GLYPH_MARKER_RE.search(_txt):
                continue

            def _repl_doc(m, _glyphs=_g):
                gi = int(m.group(1))
                if gi >= len(_glyphs):
                    return "[?]"
                g = _glyphs[gi]
                sk = "s:" + semantic_key(g.get("font", ""), g.get("cid", ""))
                return resolved_by_sem.get(sk, "[?]")

            _p["text"] = GLYPH_MARKER_RE.sub(_repl_doc, _txt)
    return stats


def resolve_glyphs_sync(text: str, glyphs: list, pdf_source, page_no: int,
                         model_name: str = "", force_fallback: bool = False,
                         dict_path=None) -> str:
    """把文本里的 ⟦G:n⟧ 换成真实符号。

    流程：
      1. 用到的字形按**语义键**（字体基名|字符码）去重
      2. 每种字形只渲染一次 → 算"形状指纹"
      3. 词典里有的直接用（零成本，**跨论文复用**）
      4. 没有的：拼成小图 → 交给多模态模型认 → 白名单校验 → 存进词典
      5. 认不出来的退回 "[?]"

    任何一步出错都退回 "[?]"，绝不猜。

    ⚠️ 单页版（resolve_glyphs_sync）只用于「按页翻译」那条路径。
    **打开/上传论文走 resolve_document_glyphs**（整篇一次）——
    逐页调用会让 36 页的论文发起 35 次串行多模态请求（实测 8 分钟以上）。
    """
    if not text or not glyphs:
        return text
    used = sorted({int(m.group(1)) for m in GLYPH_MARKER_RE.finditer(text)
                   if int(m.group(1)) < len(glyphs)})
    if not used:
        return text

    dictionary = GlyphDictionary(dict_path).load()
    doc = None
    try:
        doc, _ = _open_doc(pdf_source)

        # 1) 按语义键去重：每个 (字体基名, 字符码) 只渲染一次
        reps: dict[str, tuple[bytes, str, str]] = {}   # sem_key -> (png, shape, sem)
        for gi in used:
            g = glyphs[gi]
            sk = "s:" + semantic_key(g.get("font", ""), g.get("cid", ""))
            if sk in reps:
                continue
            png = _render_glyph(doc, page_no, g)
            reps[sk] = (png or b"", _shape_bits_stable(png) if png else "", sk)

        # 2) 词典命中 → 直接用（语义键命中 + 形状二级校验）
        resolved: dict[int, str] = {}
        pending: list[tuple[bytes, str, str]] = []     # (png, shape, sem_key)
        for gi in used:
            g = glyphs[gi]
            sk = "s:" + semantic_key(g.get("font", ""), g.get("cid", ""))
            png, shape, _sk = reps.get(sk, (b"", "", sk))
            if not shape:
                continue
            hit = dictionary.get_verified(sk, shape)
            if hit:
                resolved[gi] = hit
            elif sk not in {p[2] for p in pending}:
                pending.append((png, shape, sk))

        # 3) 待认的字形分批交给多模态
        for start in range(0, len(pending), _BATCH):
            batch = pending[start:start + _BATCH]
            items = [(str(i + 1), png) for i, (png, _s, _k) in enumerate(batch)]
            strip = _compose_strip(items)
            if not strip:
                continue
            try:
                raw = _recognize_batch(strip, len(items), model_name, force_fallback)
                parsed = _parse_recognition(raw, len(items))
                for i, (_png, shape, sk) in enumerate(batch, start=1):
                    sym = parsed.get(i)
                    if sym:
                        dictionary.put(sk, sym, shape)
            except Exception as e:
                print(f"[MicroBench] 符号认字失败（退回 [?]）: {e}", file=sys.stderr)

        # 4) 统一回填
        for gi in used:
            if gi in resolved:
                continue
            g = glyphs[gi]
            sk = "s:" + semantic_key(g.get("font", ""), g.get("cid", ""))
            _png, shape, _sk = reps.get(sk, (b"", "", sk))
            if shape:
                hit = dictionary.get_verified(sk, shape)
                if hit:
                    resolved[gi] = hit
        dictionary.save()
    except Exception as e:
        print(f"[MicroBench] 符号解析异常（退回 [?]）: {e}", file=sys.stderr)
    finally:
        try:
            if doc is not None:
                doc.close()
        except Exception:
            pass

    def _repl(m):
        gi = int(m.group(1))
        return resolved.get(gi, "[?]")

    return GLYPH_MARKER_RE.sub(_repl, text)


async def resolve_glyphs(text: str, glyphs: list, pdf_source, page_no: int,
                         model_name: str = "", force_fallback: bool = False,
                         dict_path=None) -> str:
    """异步包装：同步实现放到线程里跑（不阻塞事件循环）。"""
    return await asyncio.to_thread(
        resolve_glyphs_sync, text, glyphs, pdf_source, page_no,
        model_name, force_fallback, dict_path)


async def resolve_document_glyphs_async(pages: list, pdf_source, model_name: str = "",
                                        force_fallback: bool = False,
                                        dict_path=None,
                                        deadline: Optional[float] = None,
                                        recognize: bool = True,
                                        isolated: bool = False) -> dict:
    """异步包装：整篇解析放到线程里跑（不阻塞事件循环）。"""
    return await asyncio.to_thread(
        resolve_document_glyphs, pages, pdf_source, model_name,
        force_fallback, dict_path, deadline, recognize, isolated)


__all__ = [
    "ALLOWED_SYMBOLS",
    "GLYPH_MARKER_RE",
    "GlyphDictionary",
    "resolve_glyphs",
    "resolve_glyphs_sync",
    "resolve_document_glyphs",
    "resolve_document_glyphs_async",
    "semantic_key",
    "strip_markers",
]
