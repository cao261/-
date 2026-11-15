# -*- coding: utf-8 -*-
"""译文替换版 PDF —— 「原文页 + 替换页」交替输出。

用户诉求（2026-11-15 原话）：
  「直接就是原文上直接把翻译打上去……这篇论文一共十页，
    然后我翻译完之后就变成二十页。第一页就是原来的那个文章，
    第二页就是原来的那个字儿直接削去……然后第三页又是原来的第二页……
    然后原本的图表还都保持不变。」

要做的事：
  1. **原文页原样保留**（第 1、3、5… 页）；
  2. **替换页**（第 2、4、6… 页）= 同一页，但
     **正文文字被抹掉**、**中文译文原地填回原来的位置**，
     **图表 / 公式 / 矢量图一个不动**。

为什么在服务端用 PyMuPDF 做（而不是前端 canvas 叠字）：
  - `apply_redactions` **只删文字**：图片与矢量图可以完整保留
    （`images=PDF_REDACT_IMAGE_NONE` / `graphics=PDF_REDACT_LINE_ART_NONE`）；
  - 写进去的是**真正的 PDF 文字**（可选中、可检索、可打印），
    不是画上去的一张图；
  - 排版由 PDF 自己的排版引擎算（`insert_textbox`），
    不用前端量像素、也不用操心字体回退。

坐标系（**两套必须一致**，否则中文会贴到别的地方去）：
  段落块的 `bbox` = `[x0, top, x1, bottom]`，**PDF 点、左上角原点**
  —— 与抽取层的 ⟦FORMULA⟧/⟦TABLE⟧ 标记、`/api/paper/formula_image`
  裁图用的是**同一套**（pdfplumber 与 PyMuPDF 都是左上角原点）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# PyMuPDF 内置的简体中文字体（Droid Sans Fallback）—— **最后兜底**。
#
# ⚠ 内置字体的 ASCII 是**全角**（'M' 也占 1em）→
#   中英混排时 "ME 系数" 会拉成 "ＭＥ　系数"，很难看。
#   → 优先用系统里的**半角拉丁**中文字体（见 _resolve_font）。
CJK_FONT = "china-ss"

# 候选中文字体（正文, 粗体）。按顺序试，**第一个通过往返校验的**就用。
#
# ⚠ 顺序不是「谁好看谁优先」，是「谁**能原样取回**谁优先」——
#   见 _font_roundtrips：Noto Sans SC（可变字体）写「料/理/墨」时，
#   PDF 的 ToUnicode 里记成 **U+F9BE**（兼容表意文字），
#   渲染出来一模一样，**但用户在阅读器里搜「材料」搜不到**。
#   这种字体必须被**自动拒掉**，不能靠人记住。
FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),   # 微软雅黑
    (r"C:\Windows\Fonts\simhei.ttf", None),                            # 黑体
    (r"C:\Windows\Fonts\simsun.ttc", None),                            # 宋体
    (r"C:\Windows\Fonts\NotoSansSC-VF.ttf", None),                     # Noto Sans SC（开源）
)

# 往返探针：常用汉字 + 中文里真会出现的标点。
#
# 只放**汉字**——数字/拉丁在所有候选里都能原样取回；
# 而数学符号（∂ ∇ ⊂ ⇒…）各家字体覆盖不同，缺了由 fix_missing_glyphs 兜。
_FONT_PROBE = (
    "磁致伸缩复合材料磁电效应上下标耦合系数薄膜压电陶瓷层压结构"
    "温度磁场电场应变介电常数损耗角正切动态磁机耦合因子"
    "研究进展综述实验结果理论模型计算方法表征分析优化"
    "一二三四五六七八九十百千万亿零"
    "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
    "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    "抑扬顿挫潇洒走笔挥毫泼墨镌刻雕琢琢磨雕栏玉砌"
    "陡峭深邃蜿蜒崎岖平坦辽阔广袤幽静喧嚣鼎沸"
    "料墨理型理论和乐器钢铁"
)

# 只有这些 kind 会被「抹掉 + 填中文」。
# ⚠ formula / table **不动** —— 用户要的就是「原本的图表还都保持不变」：
#   公式硬拼 LaTeX 极易把分子分母装反（AGENTS.md §4.6：错公式比没公式危险得多），
#   表格重绘会毁掉原始版式，索性保留原样。
REPLACEABLE_KINDS = ("body", "heading", "text")

# 中文字号相对英文字号的折算。
#
# 为什么是 0.85：CJK 字的字面框比拉丁字母高（asc-desc 1.31 vs 1.20），
# 同样点号排在一起会显得「大一号」。
# 副作用正好是想要的：1.043（中文字体 ascent）× 0.85 ≈ 0.887 ≈ 拉丁字体 ascent
# → **中英文基线对得上**，替换版看起来就是同一份排版。
CJK_SIZE_RATIO = 0.85

# 中文行距（字号倍数）
LINE_RATIO = 1.35

# 字号下限 = 原字号的这个比例（再小就不是「原地替换」而是「缩小版」了）
MIN_FS_RATIO = 0.62

# 正文以下限字号为准（避免把脚注字号再缩成蚂蚁）
MIN_FONT_SIZE = 6.0

# 允许往下借用的空隙（pt）—— 中文字面框比英文字面框高，
# 单行块不借这点就得把字号缩到 0.6 倍才塞得进去（标题会明显变小）。
MAX_BORROW = 3.0

# 试字号时的步长（pt）
FS_STEP = 0.25

# 两端对齐（跟英文原文一样）
TEXT_ALIGN_JUSTIFY = 3


@dataclass
class CjkFont:
    """一套中文字体（正文 + 可选的粗体变体）。"""
    name: str
    path: Optional[str] = None
    bold_name: Optional[str] = None
    bold_path: Optional[str] = None
    _buf_cache: Dict[bool, Optional[bytes]] = field(default_factory=dict, repr=False)
    _font_cache: Dict[bool, object] = field(default_factory=dict, repr=False)

    def kwargs(self, bold: bool = False) -> Dict[str, str]:
        """给 `insert_textbox` 的字体参数。"""
        if bold and self.bold_name:
            return {"fontname": self.bold_name, "fontfile": self.bold_path or ""}
        if self.path:
            return {"fontname": self.name, "fontfile": self.path}
        return {"fontname": self.name}

    def font_obj(self, bold: bool = False):
        """缓存的 fitz.Font（用来查字形覆盖）。"""
        key = bool(bold)
        if key not in self._font_cache:
            obj = None
            path = (self.bold_path if bold else self.path)
            if path:
                try:
                    import fitz
                    obj = fitz.Font(fontfile=path)
                except Exception:
                    obj = None
            self._font_cache[key] = obj
        return self._font_cache[key]

    def has_glyph(self, ch: str, bold: bool = False) -> bool:
        """这个字体有没有这个字的字形。"""
        f = self.font_obj(bold)
        if f is None:
            return True          # 内置字体查不了 → 放行
        try:
            return bool(f.has_glyph(ord(ch)))
        except Exception:
            return True

    def buffer(self, bold: bool = False) -> Optional[bytes]:
        """字体文件字节（**只解析一次**，之后整篇复用）。

        ⚠ 为什么必须缓存：不缓存的话，**每一次** `insert_textbox(fontfile=…)`
          都要重新解析那个 17MB 的字体文件 —— 实测 600 次试排 = 17 秒。
          → 解析一次拿到 buffer，之后 `insert_font(fontbuffer=…)` 注册即可。
        """
        if bold in self._buf_cache:
            return self._buf_cache[bold]
        path = (self.bold_path if bold else self.path)
        buf = None
        if path:
            try:
                import fitz
                f = fitz.Font(fontfile=path)
                raw = getattr(f, "buffer", None)
                buf = bytes(raw) if raw else None
            except Exception:
                buf = None
        self._buf_cache[bold] = buf
        return buf


_FONT_CACHE: Optional[CjkFont] = None

# 被**自动拒掉**的字体（路径, 原因）—— 只为了排查时能说清楚「为什么没用 Noto」
_FONT_REJECTED: List[Tuple[str, str]] = []


def _font_roundtrips(font: CjkFont, bold: bool = False) -> Tuple[bool, str]:
    """这套字体写进去的字，能不能**原样取回来**。

    ⚠ 这是「中译替换版」能不能用的**硬门槛**：
      写进去的必须是**真 PDF 文字**（可选中、可检索），
      而不是「看起来像文字、实际搜不到」的兼容码位。

    踩过的坑（2026-11-15）：Noto Sans SC（可变字体）写「料/理/墨」时，
      MuPDF 生成的 ToUnicode CMap 里记的是 **U+F9BE / U+F902**（兼容表意文字）
      —— 渲染出来跟正常汉字一模一样，**但搜「材料」「理论」都搜不到**。
      肉眼完全看不出，只有把文字取回来逐字比才发现。

    → 所以：写一遍 → 取回来 → 逐字核对。
    """
    import fitz
    doc = fitz.open()
    try:
        page = doc.new_page(width=1200, height=700)
        kw = font.kwargs(bold)
        name = kw.get("fontname") or "probe"
        buf = font.buffer(bold)
        if buf:
            page.insert_font(fontname=name, fontbuffer=buf)
        else:
            page.insert_font(fontname=name)
        rc = page.insert_textbox(fitz.Rect(10, 10, 1190, 690), _FONT_PROBE,
                                 fontsize=12, fontname=name)
        if rc < 0:
            return False, "试排没写下去（rc=%s）" % rc
        got = re.sub(r"\s+", "", page.get_text())
        missing = [c for c in dict.fromkeys(_FONT_PROBE) if c not in got]
        if missing:
            return False, "取回来对不上：" + "".join(missing[:12])
        return True, ""
    except Exception as exc:
        return False, "异常：%s" % exc
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _resolve_font() -> CjkFont:
    """挑一套**能原样取回**的中文字体。

    顺序：半角拉丁好看 → 通过往返校验 → 第一个胜出。
    粗体变体单独校验；不合格就**退回常规体**（宁可粗体不明显，
    也不能出现「搜不到字」）。
    """
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    import os
    for path, bold_path in FONT_CANDIDATES:
        try:
            if not os.path.exists(path):
                continue
        except OSError:
            continue
        name = "mb_cjk_" + os.path.splitext(os.path.basename(path))[0].replace("-", "_")
        bname = bpath = None
        if bold_path:
            try:
                if os.path.exists(bold_path):
                    bname = name + "_b"
                    bpath = bold_path
            except OSError:
                bname = bpath = None
        cand = CjkFont(name=name, path=path, bold_name=bname, bold_path=bpath)
        ok, why = _font_roundtrips(cand, False)
        if not ok:
            _FONT_REJECTED.append((path, why))
            continue
        if cand.bold_path:
            bok, bwhy = _font_roundtrips(cand, True)
            if not bok:
                _FONT_REJECTED.append((bpath or "", bwhy))
                cand.bold_name = None
                cand.bold_path = None
        _FONT_CACHE = cand
        return cand
    # 兜底：PyMuPDF 内置简体中文字体（ASCII 是全角，但至少能显示、也能取回）
    builtin = CjkFont(name=CJK_FONT)
    ok, why = _font_roundtrips(builtin, False)
    if not ok:
        _FONT_REJECTED.append((CJK_FONT, why))
    _FONT_CACHE = builtin
    return builtin


@dataclass
class Block:
    """一个段落块（几何 + 译文）。"""
    index: int
    bbox: Tuple[float, float, float, float]
    translation: str
    kind: str = "body"
    col: str = "S"


@dataclass
class BuildStats:
    pages_total: int = 0
    pages_replaced: int = 0
    blocks_written: int = 0
    blocks_skipped: int = 0
    blocks_unfit: int = 0
    blocks_overflow: int = 0
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pages_total": self.pages_total,
            "pages_replaced": self.pages_replaced,
            "blocks_written": self.blocks_written,
            "blocks_skipped": self.blocks_skipped,
            "blocks_unfit": self.blocks_unfit,
            "blocks_overflow": self.blocks_overflow,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# 文本清洗：快照里的译文是给网页渲染用的 Markdown，
# 直接塞进 PDF 会看到 ** 符号和 LaTeX 源码。
# ---------------------------------------------------------------------------
_MARKER_RE = re.compile(r"⟦[^⟧]*⟧")
_MD_BOLD_RE = re.compile(r"(\*\*|__|\*|`)")

# \text{X} / \mathrm{X} / \mathbf{X} … → 只留内容
_LATEX_TEXT_RE = re.compile(
    r"\\(?:text|mathrm|mathbf|mathit|textit|textbf|textrm|mbox|operatorname|rm|bf|it)"
    r"\{([^{}]*)\}")

# 行内/块级公式
_MATH_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
_MATH_INLINE_RE = re.compile(r"\$([^$]+)\$")

# 上下标：_{X} / ^{X}
_SUBSCRIPT_RE = re.compile(r"_\{([^{}]*)\}")
_SUPERSCRIPT_RE = re.compile(r"\^\{([^{}]*)\}")

# 公式里的简写下标：Cr$_2$O$_3$ → Cr₂O₃
_MATH_SHORT_SUB_RE = re.compile(r"_([A-Za-z0-9])")

_LATEX_CMD_RE = re.compile(r"\\([a-zA-Z]+)")

# LaTeX 命令 → Unicode 符号。
#
# ⚠ 为什么**不直接删掉**（旧实现就是删）：
#   删掉 `\alpha` 译文里就只剩「效应  很好」—— **信息没了**。
#   → 换成真正的希腊字母，读者看到的就是论文里那个符号。
_LATEX_SYMBOLS = {
    # 希腊字母
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "omicron": "ο",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
    # 运算符与关系
    "times": "×", "cdot": "·", "pm": "±", "mp": "∓",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡",
    "sim": "∼", "propto": "∝", "asymp": "≈",
    # 集合与逻辑
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "supset": "⊃", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "forall": "∀", "exists": "∃", "neg": "¬",
    "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨",
    # 箭头
    "rightarrow": "→", "to": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "Leftrightarrow": "⇔", "mapsto": "↦",
    # 分析与几何
    "infty": "∞", "partial": "∂", "nabla": "∇", "int": "∫",
    "iint": "∬", "oint": "∮", "sum": "∑", "prod": "∏",
    "sqrt": "√", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "circ": "°", "deg": "°", "ell": "ℓ", "hbar": "ℏ",
    "prime": "′", "aleph": "ℵ",
    # 排版控制（不产生字符）
    "left": "", "right": "", "quad": " ", "qquad": "  ",
    "displaystyle": "", "nonumber": "", "limits": "", "nolimits": "",
    "mathrm": "", "mathbf": "", "mathit": "", "text": "",
    "cdotp": "·", "ldots": "…", "dots": "…", "cdots": "⋯",
    "vdots": "⋮", "ddots": "⋱",
}

# Unicode 下标 / 上标数字（真下标，不是"_2"）
_SUB_DIGITS = str.maketrans("0123456789+-", "₀₁₂₃₄₅₆₇₈₉₊₋")
_SUP_DIGITS = str.maketrans("0123456789+-", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻")


def _make_sub(inner: str) -> str:
    """下标：纯数字 → 真下标字符；其它 → 下划线写法（a_ME）。"""
    inner = (inner or "").strip()
    if not inner:
        return ""
    if all(c in "0123456789" for c in inner):
        return inner.translate(_SUB_DIGITS)
    return "_" + inner


def _make_sup(inner: str) -> str:
    """上标：纯数字 → 真上标字符；其它 → ^ 写法。"""
    inner = (inner or "").strip()
    if not inner:
        return ""
    if all(c in "0123456789" for c in inner):
        return inner.translate(_SUP_DIGITS)
    return "^" + inner


def _clean_math(s: str) -> str:
    """公式片段 → 能直接排版的纯文本。"""
    s = _SUBSCRIPT_RE.sub(lambda m: _make_sub(m.group(1)), s)
    s = _MATH_SHORT_SUB_RE.sub(lambda m: _make_sub(m.group(1)), s)
    s = _SUPERSCRIPT_RE.sub(lambda m: _make_sup(m.group(1)), s)
    s = _LATEX_CMD_RE.sub(lambda m: _LATEX_SYMBOLS.get(m.group(1), ""), s)
    return s.replace("{", "").replace("}", "")


def clean_translation(text: str) -> str:
    """把 Markdown / LaTeX 味洗成「能直接排版的中文」。"""
    if not text:
        return ""
    out = _MARKER_RE.sub("", text)
    # ⚠ 坏标记残留：抽取层偶尔吐**半截**标记，
    #   实测「为避免混淆�/T18⟧」—— ⟦ 变成了坏字符，只剩一个落单的 ⟧。
    #   规则：⟧ 前面**没有汉字**的一小段（标记残骸）一起去掉，
    #   免得把「/T18」这种垃圾打进 PDF。
    out = re.sub(r"[^\u4e00-\u9fff]{1,40}⟧", "", out)
    # 未闭合的 ⟦（内容是 ASCII 标记体）→ 整段去掉
    out = re.sub(r"⟦[A-Za-z0-9:.,;_\-\s]{0,60}", "", out)
    out = out.replace("⟧", "").replace("�", "")
    out = _LATEX_TEXT_RE.sub(r"\1", out)
    out = _MATH_BLOCK_RE.sub(lambda m: _clean_math(m.group(1)), out)
    out = _MATH_INLINE_RE.sub(lambda m: _clean_math(m.group(1)), out)
    out = _SUBSCRIPT_RE.sub(lambda m: _make_sub(m.group(1)), out)
    out = _SUPERSCRIPT_RE.sub(lambda m: _make_sup(m.group(1)), out)
    out = _LATEX_CMD_RE.sub(lambda m: _LATEX_SYMBOLS.get(m.group(1), ""), out)
    out = out.replace("$", "").replace("\\(", "").replace("\\)", "")
    out = _MD_BOLD_RE.sub("", out)
    # ⚠ 换行必须变成空格：`insert_textbox` 把 "\n" 当成**强制换行**，
    #   而模型给的译文里常带换行 → 实际行数比「按宽度折行」算出来的多
    #   → PyMuPDF 拒写（实测 7 块「算着放得下、实际被拒」全是这个原因）。
    out = re.sub(r"\s*\n\s*", " ", out)
    out = re.sub(r"[ \t]+", " ", out)
    return out.strip()


# 字体缺字时的替换表。
#
# 模型偶尔会吐一些冷门 Unicode —— 最常见的是**上标逗号**「˒」(U+02D2)，
# 用来表示 "3˒4" 这种引用。Noto Sans SC 里没有这个字形 →
# PDF 里就是一个**豆腐块 □**（实测 JACERS 综述第 1 页出现 3 处）。
_GLYPH_FALLBACK = {
    "˒": ",", "˓": ",", "˕": "'",
    "ˈ": "'", "ʼ": "'", "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", "　": " ",
    "﻿": "", "": "",
    # 少见的箭头/关系符号（Windows 中文字体基本都没有）
    "⇒": "=>", "⇐": "<=", "↔": "<->", "⇔": "<=>",
    # ⚠ 不要动「·」「…」「°」：微软雅黑**有**这些字形，
    #   取回来时只是变成同形的「∙」「⋯」（视觉无损），换掉反而变丑。
    #   实测：msyh 的「·」→ U+2219，Noto 的「·」→ U+30FB，都是同形字。
}

# Unicode 上下标数字 → 普通数字。
#
# ⚠ 微软雅黑**没有**上下标数字的字形（实测 has_glyph('₂')=0）
#   → 不映射的话，fix_missing_glyphs 会把它们**直接丢掉**：
#   "Cr₂O₃" 变成 "CrO"（数值都没了，比不替换还糟）。
#   → 有上下标字体的字体仍保持真下标（Noto 等），雅黑退化成 "Cr2O3"。
for _i in range(10):
    _GLYPH_FALLBACK[chr(0x2080 + _i)] = str(_i)   # ₀..₉
    _GLYPH_FALLBACK[chr(0x2070 + _i)] = str(_i)   # ⁰..⁹
_GLYPH_FALLBACK.update({
    "₊": "+", "₋": "-", "₌": "=", "₍": "(", "₎": ")",
    "⁺": "+", "⁻": "-", "⁼": "=", "⁽": "(", "⁾": ")",
})

# 上下标**字母**（μᵣ、Qₘ、T꜀ 这种）→ 普通字母。
# ⚠ 同上：雅黑没有这些字形，不映射就整段丢（"μ = 10000" 少了下标 r）。
for _cp, _ch in {
    0x2090: "a", 0x2091: "e", 0x2092: "o", 0x2093: "x", 0x2094: "h",
    0x2095: "h", 0x2096: "k", 0x2097: "l", 0x2098: "m", 0x2099: "n",
    0x209A: "p", 0x209B: "s", 0x209C: "t",
    0x1D62: "i", 0x1D63: "r", 0x1D64: "u", 0x1D65: "v",
    0x1D66: "β", 0x1D67: "γ", 0x1D68: "ρ", 0x1D69: "φ", 0x1D6A: "χ",
}.items():
    _GLYPH_FALLBACK[chr(_cp)] = _ch

# 修饰字母大写（U+A700 起的 MODIFIER LETTER CAPITAL X）→ 当成下标
_GLYPH_FALLBACK["꜀"] = "_C"          # T꜀ → T_C（居里温度）
_GLYPH_FALLBACK["∼"] = "~"            # 波浪号算子（雅黑没有）
# ⚠ 矩阵/分式括号碎片（⎛⎜⎝⎞⎟⎠，抽取层从 PDF 字形认出来的）
#   → **直接丢**：它们本来就是被拆散的公式碎片，留着只是乱码，
#     真正的公式在 PDF 原页上原封不动（公式块不参与替换）。


def fix_missing_glyphs(text: str, font: CjkFont, bold: bool = False) -> str:
    """把字体里**没有的字符**换掉 —— 豆腐块（□）比缺字难看得多。"""
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            out.append(" ")
            continue
        if font.has_glyph(ch, bold):
            out.append(ch)
            continue
        rep = _GLYPH_FALLBACK.get(ch)
        if rep and (not rep or font.has_glyph(rep, bold)):
            out.append(rep)
        # 真的没有 → 直接丢掉
    return "".join(out)


# ---------------------------------------------------------------------------
# 几何小工具
# ---------------------------------------------------------------------------
def _clamp_rect(bbox: Sequence[float], page_rect):
    import fitz
    x0, top, x1, bottom = [float(v) for v in bbox[:4]]
    r = fitz.Rect(
        max(page_rect.x0, min(x0, page_rect.x1)),
        max(page_rect.y0, min(top, page_rect.y1)),
        max(page_rect.x0, min(x1, page_rect.x1)),
        max(page_rect.y0, min(bottom, page_rect.y1)),
    )
    if r.width < 0:
        r.width = 0
    if r.height < 0:
        r.height = 0
    return r


def _bg_color(page) -> Tuple[float, float, float]:
    """页面背景色（页边一环的众数）。

    扫描件常常是米色/浅灰 —— 硬填白色会露出一块块白斑。
    数字版 PDF 的页边基本恒为白，取众数即可。
    """
    try:
        import fitz
        pix = page.get_pixmap(dpi=24)
    except Exception:
        return (1.0, 1.0, 1.0)
    w, h, n = pix.width, pix.height, pix.n
    if w <= 0 or h <= 0 or n < 3:
        return (1.0, 1.0, 1.0)
    samples = pix.samples
    counts: Dict[Tuple[int, int, int], int] = {}
    for y in (0, 1, h - 2, h - 1):
        base = y * w * n
        for x in range(w):
            i = base + x * n
            px = (samples[i], samples[i + 1], samples[i + 2])
            counts[px] = counts.get(px, 0) + 1
    if not counts:
        return (1.0, 1.0, 1.0)
    (r, g, b), _ = max(counts.items(), key=lambda kv: kv[1])
    return (r / 255.0, g / 255.0, b / 255.0)


def _dominant_font_size(spans: dict, rect) -> float:
    """这一块里**原文**的字号（按字符数取众数）。

    译文要用**同一个字号**才像「原版排版」——
    中文比英文短，用同样字号放进去，段落的高度自然就短一截，
    和真·中文化排版一个样子。
    """
    sizes: Dict[float, int] = {}
    for blk in (spans.get("blocks") or []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines") or []:
            for sp in line.get("spans") or []:
                sb = sp.get("bbox")
                if not sb:
                    continue
                cx = (sb[0] + sb[2]) / 2.0
                cy = (sb[1] + sb[3]) / 2.0
                if not (rect.x0 <= cx <= rect.x1 and rect.y0 <= cy <= rect.y1):
                    continue
                size = round(float(sp.get("size") or 0.0), 1)
                if size <= 0:
                    continue
                weight = len((sp.get("text") or "").strip()) or 1
                sizes[size] = sizes.get(size, 0) + weight
    if not sizes:
        return 0.0
    return max(sizes.items(), key=lambda kv: kv[1])[0]


def _fallback_font_size(rect) -> float:
    """量不到字号时的兜底：按块高估（单行块高 ≈ 1.2 × 字号）。"""
    return max(MIN_FONT_SIZE, min(12.0, (rect.height or 10.0) / 1.2))


def _next_block_top(blocks: Sequence[Tuple["Block", object]], rect,
                    col: Optional[str] = None) -> Optional[float]:
    """同栏里**下一个块**的上沿 —— 中文放不下时可以往下借空间（不越界）。

    ⚠ 必须**按栏**找：双栏页里右栏第一块的 y0 可能在页面顶部，
    拿它当「下一个块」→ 借的空间算成了负数 → 永远借不到。
    """
    best: Optional[float] = None
    fallback: Optional[float] = None
    for blk, r in blocks:
        if r.y0 <= rect.y0 + 0.5:
            continue
        if fallback is None or r.y0 < fallback:
            fallback = r.y0
        if col and blk.col != col:
            continue
        if best is None or r.y0 < best:
            best = r.y0
    return best if best is not None else fallback


# ---------------------------------------------------------------------------
# 涂改范围：必须对齐到**原页真实的文字行**
#
# ⚠ 踩过的坑（2026-11-15，JACERS 综述第 2 页右栏）：
#   快照里段落块的 bbox 是 [304.2, 41.8, 533.4, 289.9]，
#   但这一段的**其余行**都从 x=294.2 开始（只有首行是 304.2）。
#   → 直接拿块 bbox 去涂 → 每行最左边 2~3 个英文字母**留在原地**
#     （实测残留成一条竖排的 'ert' 'in' 'ot' 'ar' …）。
#   → 修法：涂改范围 = 块自己的框 ∪ **与之重叠的原页文字行**（按行收紧），
#     而不是一个大矩形 —— 大矩形会误伤同一栏里的图表标签。
# ---------------------------------------------------------------------------
def _page_lines(spans: dict) -> List[Tuple[float, float, float, float]]:
    """原页的文字行 bbox。"""
    out: List[Tuple[float, float, float, float]] = []
    for blk in (spans.get("blocks") or []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines") or []:
            bb = line.get("bbox")
            if not bb:
                continue
            if (bb[2] - bb[0]) <= 0.5 or (bb[3] - bb[1]) <= 0.5:
                continue
            out.append((bb[0], bb[1], bb[2], bb[3]))
    return out


def _protected_rects(page) -> List[object]:
    """图片 / 矢量图的区域 —— 里面的文字**不参与**涂改范围的扩张。

    为什么要护：图表里的坐标轴标签、图内标注也是「文字」，
    跟着段落一起被涂掉就违背了「原本的图表还都保持不变」。
    """
    import fitz
    rects: List[object] = []
    try:
        page_area = abs(page.rect.get_area()) or 1.0
    except Exception:
        page_area = 1.0
    try:
        for info in page.get_image_info():
            bb = info.get("bbox")
            if not bb:
                continue
            r = fitz.Rect(bb)
            if abs(r.get_area()) > page_area * 0.5:
                continue          # 整页背景图 → 不当保护区
            rects.append(r)
    except Exception:
        pass
    try:
        for d in page.get_drawings():
            bb = d.get("rect")
            if not bb:
                continue
            r = fitz.Rect(bb)
            if abs(r.get_area()) > page_area * 0.5:
                continue
            if abs(r.get_area()) < 200:
                continue          # 分隔线之类
            rects.append(r)
    except Exception:
        pass
    return rects


def _in_protected(line, protected) -> bool:
    """这行字是不是在图/矢量图里。"""
    import fitz
    lx0, ly0, lx1, ly1 = line
    cx = (lx0 + lx1) / 2.0
    cy = (ly0 + ly1) / 2.0
    for r in protected:
        if r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1:
            return True
    return False


def _line_group(rect, lines, protected, tol: float = 1.5):
    """与块框**重叠**的原页文字行（图表里的行除外）。

    重叠的判据：行的**垂直中心**落在块的 y 范围内，
    且行的 x 范围与块的 x 范围有交集 —— 后者保证不会把**另一栏**的文字拉进来。
    """
    group = []
    for ln in lines:
        lx0, ly0, lx1, ly1 = ln
        cy = (ly0 + ly1) / 2.0
        if cy < rect.y0 - tol or cy > rect.y1 + tol:
            continue
        if lx1 < rect.x0 - tol or lx0 > rect.x1 + tol:
            continue
        if _in_protected(ln, protected):
            continue
        group.append(ln)
    return group


# ---------------------------------------------------------------------------
# 试排：让 MuPDF 自己回答「这段字在这个框里放不放得下」
# ---------------------------------------------------------------------------
class _Sizer:
    """用 MuPDF 自己的排版引擎量「放不放得下」。

    ⚠ 为什么不能自己按宽度估算行数：
      拉丁词**不能从中间断行**（`NiFeO₂₄`、`Pb(Zr,Ti)O₃`、`viehland@mse.vt.edu`…），
      纯 CJK 行的「总宽 ÷ 框宽」会**低估行数** →
      算着放得下、PyMuPDF 实际拒写（实测 6 块栽在这）。
      → 直接拿 MuPDF 自己排一遍，**精确**判定。
    """

    def __init__(self, font: "CjkFont"):
        import fitz
        self._fitz = fitz
        self.font = font
        self._doc = fitz.open()
        # ⚠ 只用**一张**临时页反复试排：
        #   字体资源是**按页**的 —— 每换一页就要重新注册（16MB 的字体
        #   复制 600 次 = 十几秒）。固定一页 → 字体注册一次就够。
        #   页要开得足够大：所有待测框都要放得下。
        self._page = self._doc.new_page(width=4000, height=4000)
        self._registered: Dict[bool, str] = {}

    def _ensure_registered(self, page, bold: bool) -> str:
        """在**这个文档**里注册一次字体，之后都按 fontname 引用。"""
        if bold in self._registered:
            return self._registered[bold]
        kw = self.font.kwargs(bold)
        name = kw["fontname"]
        buf = self.font.buffer(bold)
        try:
            if buf:
                page.insert_font(fontname=name, fontbuffer=buf)
            elif kw.get("fontfile"):
                page.insert_font(fontname=name, fontfile=kw["fontfile"])
            else:
                page.insert_font(fontname=name)
        except Exception:
            return name
        self._registered[bold] = name
        return name

    def fits(self, rect, text: str, fs: float, align: int, bold: bool = False,
             line_ratio: float = LINE_RATIO) -> bool:
        """在**同宽同高**的临时页上试排一次，看它肯不肯全写下去。"""
        w = max(float(rect.width), 1.0)
        h = max(float(rect.height), 1.0)
        page = self._page
        try:
            name = self._ensure_registered(page, bold)
            rc = page.insert_textbox(
                self._fitz.Rect(0, 0, w, h), text, fontsize=fs,
                fontname=name, align=align, lineheight=line_ratio)
            return rc >= 0
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:
            pass


def _size_candidates(base_fs: float, min_fs: float):
    """从 base_fs 往下试到 min_fs（步长 FS_STEP）。"""
    fs = base_fs
    while fs > min_fs:
        yield round(fs, 2)
        fs -= FS_STEP
    yield round(min_fs, 2)


# ---------------------------------------------------------------------------
# 主构建
# ---------------------------------------------------------------------------
def build_replaced_pdf(
    src_pdf: Path,
    out_pdf: Path,
    pages: Dict[int, List[Block]],
    *,
    font: Optional[CjkFont] = None,
    align: int = TEXT_ALIGN_JUSTIFY,
    min_fs_ratio: float = MIN_FS_RATIO,
    cjk_size_ratio: float = CJK_SIZE_RATIO,
    progress: Optional[Callable[[int, int], None]] = None,
) -> BuildStats:
    """构建「原文页 + 替换页」交替的译文替换版 PDF。

    pages: {页码(1 起): [Block, ...]} —— 只给**有译文**的块，
           没有译文的块会在替换页上**保留英文原样**（而不是留个白洞）。
    """
    import fitz

    font = font or _resolve_font()
    stats = BuildStats()
    src = fitz.open(str(src_pdf))
    out = fitz.open()
    sizer = _Sizer(font)
    try:
        stats.pages_total = src.page_count
        for idx in range(src.page_count):
            if progress:
                progress(idx, src.page_count)
            pno = idx + 1
            # 1) 原文页 —— 原样保留
            out.insert_pdf(src, from_page=idx, to_page=idx)

            blocks = pages.get(pno) or []
            usable = [b for b in blocks
                      if (b.translation or "").strip() and b.kind in REPLACEABLE_KINDS]
            if not usable:
                stats.blocks_skipped += len(blocks)
                continue

            # 2) 替换页 —— 复制一份来改（改副本，原文页不受影响）
            out.insert_pdf(src, from_page=idx, to_page=idx)
            page = out.load_page(out.page_count - 1)

            # 涂之前先量：字号要靠**原文**的 span 算（涂完就没了）
            spans = page.get_text("dict")
            bg = _bg_color(page)

            rects: List[Tuple[Block, object]] = []
            for blk in sorted(usable, key=lambda b: b.index):
                r = _clamp_rect(blk.bbox, page.rect)
                if r.width <= 1 or r.height <= 1:
                    stats.blocks_skipped += 1
                    continue
                rects.append((blk, r))

            # 原页的真实文字行 + 图表保护区（涂改范围要贴着行，见 _line_group）
            page_lines = _page_lines(spans)
            protected = _protected_rects(page)

            # ⚠ **先算落位方案，再涂**：
            #   涂了才知道放不放得下 → 放不下就只能截断，
            #   而「截断的译文」比「保留英文原文」更糟（信息直接丢了）。
            #   → 放不下的块**不涂不写**，替换页上保留它的英文原样。
            plans = []
            for blk, r in rects:
                bold = (blk.kind == "heading")
                text = clean_translation(blk.translation)
                text = fix_missing_glyphs(text, font, bold)
                if not text:
                    stats.blocks_skipped += 1
                    continue
                en_fs = _dominant_font_size(spans, r) or _fallback_font_size(r)
                # 中文按 0.85 折算 → 基线跟原文对齐（见 CJK_SIZE_RATIO 注释）
                base_fs = en_fs * cjk_size_ratio
                min_fs = max(MIN_FONT_SIZE, base_fs * min_fs_ratio)

                # 涂改范围 + 中文可用宽度：**贴着原页真实的行**
                # （块的 bbox 只反映首行起点，会漏掉每行最左边几个字母）
                group = _line_group(r, page_lines, protected)
                gx0 = min([r.x0] + [g[0] for g in group])
                gx1 = max([r.x1] + [g[2] for g in group])

                # 空间：先借一点（MAX_BORROW）——
                # 中文字面框比英文字面框高（asc-desc 1.31 vs 1.20），
                # 单行块（标题/图注）不借这点就放不下 → 只能把字号缩到 0.6 倍。
                nxt = _next_block_top(rects, r, blk.col)
                gap = (nxt - r.y1) if nxt else (page.rect.y1 - r.y1)
                allowance = max(0.0, min(MAX_BORROW, gap))
                target = fitz.Rect(gx0, r.y0, gx1, min(r.y1 + allowance, page.rect.y1))
                fs = _pick_size(sizer, target, text, base_fs, min_fs, align, bold)
                if fs is None and gap > allowance:
                    # 还不够 → 一直借到**同栏下一个块的上沿**（不越界）
                    grown = fitz.Rect(gx0, r.y0, gx1, min(r.y1 + gap, page.rect.y1))
                    fs = _pick_size(sizer, grown, text, base_fs, min_fs, align, bold)
                    if fs is not None:
                        target = grown
                if fs is None:
                    stats.blocks_unfit += 1
                    stats.warnings.append(
                        "第 %d 页第 %d 块：中文放不进原框（框 %.1f×%.1fpt）→ 保留英文原样"
                        % (pno, blk.index, r.width, r.height))
                    continue
                plans.append((blk, r, target, fs, text,
                              [fitz.Rect(g) for g in group]))

            # ⚠ 关键：**只删文字**，图片与矢量图原样保留
            #   （用户要「原本的图表还都保持不变」）
            for _blk, _r, _t, _fs, _txt, _lines in plans:
                page.add_redact_annot(_r, fill=bg)
                # 补上与块重叠的**原页文字行** → 不留半截英文
                for _lr in _lines:
                    page.add_redact_annot(_lr, fill=bg)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )

            # 3) 原地写中文
            for blk, r, target, fs, text, _lines in plans:
                bold = (blk.kind == "heading")
                rc = page.insert_textbox(
                    target, text, fontsize=fs, align=align, lineheight=LINE_RATIO,
                    fontname=_register_font(page, font, bold),
                )
                if rc < 0:
                    # 理论上到不了这里（方案阶段已经用同一套参数试排过）——
                    # 真到了就说明环境变了，如实计数不装没。
                    stats.blocks_overflow += 1
                    stats.warnings.append(
                        "第 %d 页第 %d 块：写入时被拒（框 %.1f×%.1fpt，字号 %.1f）"
                        % (pno, blk.index, target.width, target.height, fs))
                stats.blocks_written += 1
            stats.pages_replaced += 1

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        # 子集化字体：不这么做的话，嵌入的 Noto/雅黑会**整个**塞进 PDF
        #（16~19MB/份）→ 生成的文件大得没法发。
        try:
            out.subset_fonts()
        except Exception:
            pass
        # garbage=4：清掉被涂掉的文字残留（不然 PDF 里还能搜到英文）
        out.save(str(out_pdf), garbage=4, deflate=True)
    finally:
        sizer.close()
        out.close()
        src.close()
    return stats


def _register_font(page, font: CjkFont, bold: bool) -> str:
    """在这一页上注册字体，返回字体名（之后 insert_textbox 按名引用）。

    字体资源是**按页**的 → 每张替换页都要注册一次；
    但字体文件**只解析一次**（CjkFont.buffer 有缓存）→ 不慢。
    """
    kw = font.kwargs(bold)
    name = kw["fontname"]
    buf = font.buffer(bold)
    try:
        if buf:
            page.insert_font(fontname=name, fontbuffer=buf)
        elif kw.get("fontfile"):
            page.insert_font(fontname=name, fontfile=kw["fontfile"])
    except Exception:
        pass
    return name


def _pick_size(sizer: "_Sizer", rect, text: str, base_fs: float, min_fs: float,
               align: int, bold: bool = False) -> Optional[float]:
    """返回「放得下的最大字号」；都放不下就返回 None。"""
    for fs in _size_candidates(base_fs, min_fs):
        if sizer.fits(rect, text, fs, align, bold):
            return fs
    return None
