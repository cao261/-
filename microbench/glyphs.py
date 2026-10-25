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
_MATCH_MAX_DIFF = 40

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


def _hamming(a: str, b: str) -> int:
    """两个 hex 位图的不同位数（越少越像）。"""
    if not a or not b or len(a) != len(b):
        return 10 ** 9
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 10 ** 9


class GlyphDictionary:
    """字形 → 符号 的持久化词典。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_DICT_PATH
        self.data: dict[str, str] = {}
        self.dirty = False

    def load(self) -> "GlyphDictionary":
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = {str(k): str(v) for k, v in raw.items()}
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
        """按**汉明距离**找最接近的已知形状。

        精确匹配会漏（同一字形在不同字号下采样点会差几位），
        所以允许差 _MATCH_MAX_DIFF 位以内。≈ 与 × 的差异远大于这个阈值，
        不会互相认错。
        """
        if not key:
            return None
        exact = self.data.get(key)
        if exact:
            return exact
        best, best_d = None, 10 ** 9
        for k, v in self.data.items():
            d = _hamming(key, k)
            if d < best_d:
                best, best_d = v, d
        if best is not None and best_d <= _MATCH_MAX_DIFF:
            return best
        return None

    def put(self, key: str, ch: str) -> None:
        """记一条"形状 → 符号"。

        **必须校验白名单**：这是最后一道闸 —— 万一模型说了个不该收的字符
        （"文"、"字"……），宁可丢掉，也不能让它进词典污染以后的翻译。
        """
        if not key or not ch or len(ch) != 1:
            return
        if ch not in ALLOWED_SYMBOLS:
            return
        if self.data.get(key) != ch:
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


async def resolve_glyphs(text: str, glyphs: list, pdf_source, page_no: int,
                         model_name: str = "", force_fallback: bool = False,
                         dict_path=None) -> str:
    """把文本里的 ⟦G:n⟧ 换成真实符号。

    流程：
      1. 用到的字形按 (字体, 字符号) 去重（同一子集里同一 code 就是同一字形）
      2. 每种字形只渲染一次 → 算"形状指纹"（归一化位图的 sha1）
      3. 词典里有的直接用（零成本）
      4. 没有的：拼成小图 → 交给多模态模型认 → 白名单校验 → 存进词典
      5. 认不出来的退回 "[?]"

    任何一步出错都退回 "[?]"，绝不猜。
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

        # 1) 按 (字体, 字符号) 去重：每个组合只渲染一次
        reps: dict[tuple, tuple[bytes, str]] = {}   # (font,cid) -> (png, shape_key)
        for gi in used:
            g = glyphs[gi]
            kk = (g.get("font", ""), g.get("cid", ""))
            if kk in reps:
                continue
            png = _render_glyph(doc, page_no, g)
            reps[kk] = (png or b"", _shape_bits(png) if png else "")

        # 2) 词典命中 → 直接用
        resolved: dict[int, str] = {}
        pending: list[tuple[bytes, str]] = []      # (png, shape_key)，待认
        seen_keys: set[str] = set()
        for gi in used:
            g = glyphs[gi]
            kk = (g.get("font", ""), g.get("cid", ""))
            png, key = reps.get(kk, (b"", ""))
            if not key:
                continue
            hit = dictionary.get(key)
            if hit:
                resolved[gi] = hit
            elif key not in seen_keys:
                seen_keys.add(key)
                pending.append((png, key))

        # 3) 待认的字形分批交给多模态
        for start in range(0, len(pending), _BATCH):
            batch = pending[start:start + _BATCH]
            items = [(str(i + 1), png) for i, (png, _k) in enumerate(batch)]
            strip = _compose_strip(items)
            if not strip:
                continue
            try:
                import base64
                content = [
                    {"type": "text", "text": _RECOGNIZE_PROMPT},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64,"
                                  + base64.b64encode(strip).decode("ascii")}},
                ]
                raw = await asyncio.to_thread(
                    llm.chat,
                    messages=[
                        {"role": "system",
                         "content": "你是符号识别助手，只回答符号本身。"},
                        {"role": "user", "content": content},
                    ],
                    max_tokens=800,
                    temperature=0.0,
                    timeout=120,
                    # 必须关思考：主模型是推理型，思考会把 token 预算吃光，
                    # 正文一个字都吐不出来（实测 max_tokens=300 → 被截断、0 字符）
                    disable_thinking=llm.translation_disable_thinking(),
                    model_name=model_name or None,
                    force_fallback=force_fallback,
                )
                parsed = _parse_recognition(raw, len(items))
                for i, (_png, key) in enumerate(batch, start=1):
                    sym = parsed.get(i)
                    if sym:
                        dictionary.put(key, sym)
            except Exception as e:
                print(f"[MicroBench] 符号认字失败（退回 [?]）: {e}", file=sys.stderr)

        # 4) 统一回填
        for gi in used:
            if gi in resolved:
                continue
            g = glyphs[gi]
            kk = (g.get("font", ""), g.get("cid", ""))
            _png, key = reps.get(kk, (b"", ""))
            if key:
                hit = dictionary.get(key)
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


__all__ = [
    "ALLOWED_SYMBOLS",
    "GLYPH_MARKER_RE",
    "GlyphDictionary",
    "resolve_glyphs",
    "strip_markers",
]
