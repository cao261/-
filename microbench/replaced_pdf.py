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
    _fz_cache: Dict[bool, object] = field(default_factory=dict, repr=False)

    def fitz_font(self, bold: bool = False):
        """缓存的 ``fitz.Font`` —— 排版引擎用它**精确量宽**。

        ⚠ 为什么必须缓存：`fitz.Font(fontfile=…)` 要重新解析整个字体文件
          （雅黑 17MB）→ 每页重建一次字体就是十几秒。
          → 解析一次，之后整篇复用。
        """
        key = bool(bold)
        if key in self._fz_cache:
            return self._fz_cache[key]
        obj = None
        try:
            import fitz
            path = (self.bold_path if bold else self.path)
            if path:
                obj = fitz.Font(fontfile=path)
            else:
                obj = fitz.Font(fontname=self.name)
        except Exception:
            obj = None
        self._fz_cache[key] = obj
        return obj

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
    # 2026-11-24 新增（都是「质量体检」用的）
    blocks_in_figure: int = 0     # 整块落在图/矢量图里 → 保持原样（艺术字等）
    headings_split: int = 0       # 从正文尾部拆出来单独落位的小节标题
    lines_written: int = 0        # 实际写进去的中文行数
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "pages_total": self.pages_total,
            "pages_replaced": self.pages_replaced,
            "blocks_written": self.blocks_written,
            "blocks_skipped": self.blocks_skipped,
            "blocks_unfit": self.blocks_unfit,
            "blocks_overflow": self.blocks_overflow,
            "blocks_in_figure": self.blocks_in_figure,
            "headings_split": self.headings_split,
            "lines_written": self.lines_written,
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
    "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2009": " ", "\u3000": " ",
    "\ufeff": "", "\u200b": "", "\u200c": "", "\u200d": "",
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
        if rep is not None and (not rep or font.has_glyph(rep, bold)):
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
#   → 修法：涂改范围 = **与之重叠的原页文字行**（按行收紧），
#     而不是一个大矩形 —— 大矩形会误伤同一栏里的图表标签。
#
# ⚠ 2026-11-24 再修一次：连**块自己的框**都不涂了。
#   块框会盖住图（实测在渐变图上盖出 210×35.6pt 的纯白矩形），
#   而块框里的文字本来就属于它覆盖的那些**行** → 只涂行就够。
# ---------------------------------------------------------------------------
def _merge_nearby(rects: List[object], gap: float = 3.0) -> List[object]:
    """把互相靠近的矩形合并成大块。

    为什么需要（2026-11-24 真机取证）：
      一张**渐变图**在 PDF 里是上百条渐变矢量（JACERS 首页实测 **251 条**）。
      逐条当保护区 → 保护区被切得稀碎，每条面积只有 226
      （刚好卡在「面积 < 200 当分隔线跳过」的阈值**之上**）→ 保护形同虚设。

      → 合并后渐变图变成**一整块**，图里的文字（艺术字、图内标注）
        才真的护得住。
    """
    import fitz
    if not rects:
        return []
    boxes = [fitz.Rect(r) for r in rects]
    merged = True
    while merged:
        merged = False
        out: List[object] = []
        used = [False] * len(boxes)
        for i in range(len(boxes)):
            if used[i]:
                continue
            cur = fitz.Rect(boxes[i])
            for j in range(i + 1, len(boxes)):
                if used[j]:
                    continue
                other = boxes[j]
                if (cur.x0 - gap <= other.x1 and other.x0 - gap <= cur.x1
                        and cur.y0 - gap <= other.y1 and other.y0 - gap <= cur.y1):
                    cur |= other
                    used[j] = True
                    merged = True
            used[i] = True
            out.append(cur)
        boxes = out
    return boxes


def _protected_rects(page) -> List[object]:
    """图片 / 矢量图的区域 —— 里面的文字**不参与**涂改。

    为什么要护：图表里的坐标轴标签、图内标注也是「文字」，
    跟着段落一起被涂掉就违背了「原本的图表还都保持不变」。

    ⚠ 2026-11-24 重写（真机取证后）：原来逐个 `get_drawings()` 取 rect。
      实测 JACERS 首页底部那张渐变图在 PDF 里是 **251 条渐变矢量**，
      逐条取 rect → 保护区被切成 252 条、每条面积 226
      （刚好卡在「面积 < 200 当分隔线跳过」的阈值**之上**）→ **保护形同虚设**。
      于是正文块的大矩形 redaction 顺带把图盖了 —— 实测在渐变图上盖出
      一个 210×35.6pt 的**纯白实心矩形**，「图表保持不变」直接破功。

      → 改用 `page.cluster_drawings()`（PyMuPDF 1.24+）：直接拿**聚类后的**矢量图矩形。
        本机实测（PyMuPDF 1.27.2.3）：
          · JACERS 首页 251 条渐变条 → 聚成 **1 块**；
          · 纯正文页（无图）→ **0 块**（不会误伤正文）；
          · 有 4 张子图的第 4 页 → 精确聚出 4 张，
            落进聚类的文字行只有 `(a)` `(b)` `(c)` `@ 1kHz` 这些**图内标签**
            —— 正是要保护的对象。
    """
    import fitz
    rects: List[object] = []
    try:
        page_area = abs(page.rect.get_area()) or 1.0
    except Exception:
        page_area = 1.0

    # ---- 位图 ----
    try:
        for info in page.get_image_info():
            bb = info.get("bbox")
            if not bb:
                continue
            r = fitz.Rect(bb)
            if abs(r.get_area()) > page_area * 0.85:
                continue          # 整页背景图 → 不当保护区
            rects.append(r)
    except Exception:
        pass

    # ---- 矢量图 ----
    clusters: List[object] = []
    try:
        clusters = list(page.cluster_drawings())
    except Exception:
        clusters = []
    if clusters:
        for r in clusters:
            r = fitz.Rect(r)
            if abs(r.get_area()) > page_area * 0.85:
                continue          # 整页背景 → 不当保护区
            rects.append(r)
    else:
        # 旧版 PyMuPDF 没有 cluster_drawings → 退回逐条取，
        # 但**必须合并**，否则渐变图会被切碎（保护形同虚设）。
        try:
            for d in page.get_drawings():
                bb = d.get("rect")
                if not bb:
                    continue
                r = fitz.Rect(bb)
                if abs(r.get_area()) > page_area * 0.5:
                    continue
                if abs(r.get_area()) < 200:
                    continue      # 分隔线之类
                rects.append(r)
        except Exception:
            pass

    return _merge_nearby(rects)


def _subtract_rect(base, hole) -> List[object]:
    """矩形相减：``base`` 减去 ``hole``，返回剩下的几块。

    为什么要减：涂改矩形是**实心填充**的 —— 只要压到图上一丁点，
    图上就多一块白斑（实测在渐变图上盖出 210×35.6pt 的纯白矩形）。
    → 涂改范围必须**避开**保护区。

    轴对齐矩形的差集最多 4 块（左右上下），够用。
    """
    import fitz
    b = fitz.Rect(base)
    h = fitz.Rect(hole)
    inter = b & h
    if inter.is_empty or inter.get_area() <= 0:
        return [b]
    out = []
    # 左
    if inter.x0 > b.x0:
        out.append(fitz.Rect(b.x0, b.y0, inter.x0, b.y1))
    # 右
    if inter.x1 < b.x1:
        out.append(fitz.Rect(inter.x1, b.y0, b.x1, b.y1))
    # 上
    if inter.y0 > b.y0:
        out.append(fitz.Rect(inter.x0, b.y0, inter.x1, inter.y0))
    # 下
    if inter.y1 < b.y1:
        out.append(fitz.Rect(inter.x0, inter.y1, inter.x1, b.y1))
    return [r for r in out if r.width > 0.4 and r.height > 0.4]


def _clip_to_protected(rect, protected) -> List[object]:
    """把一个涂改矩形**减去所有图表保护区**，返回剩下的几块。

    为什么要减：涂改矩形是**实心填充**的 —— 只要压到图上一丁点，
    图上就多一块白斑（实测在渐变图上盖出 210×35.6pt 的纯白矩形）。
    → 涂改范围必须**避开**保护区。

    ⚠ 保护区要**外扩 1pt** 再减：
      实测 JACERS 首页左上角的刊头 logo（位图 rect [66,0,113,63]
      与矢量聚类 rect [66.8,0,112.3,62.2] 两条几乎重合），
      两者之间的**缝隙**没被减去 → 涂改矩形从缝里压过去 →
      logo 下边缘被擦掉一条 1px 细线（实测 32/28674 采样点有差异）。
      → 外扩 1pt 留出余量。

    减完可能什么都不剩 → 返回空列表 → 这一行**不涂**（保留英文），
    宁可留半截英文，也不能毁掉图。
    """
    import fitz
    pieces = [fitz.Rect(rect)]
    for hole in protected:
        grow = fitz.Rect(hole) + (-1.0, -1.0, 1.0, 1.0)
        nxt: List[object] = []
        for p in pieces:
            nxt.extend(_subtract_rect(p, grow))
        pieces = nxt
        if not pieces:
            break
    return pieces


# 图内文字（坐标轴刻度 / 轴名 / 子图号）的判定阈值。
#
# ⚠ 为什么需要（2026-11-24 真机取证）：
#   `cluster_drawings()` 的聚类矩形只包含**矢量路径**，**不含文字**。
#   图里的刻度标签（「–8」「–200」「Magnetic field (Oe)」…）落在聚类框**之外**，
#   于是被当成正文 → 涂掉 + 写上中文 → **图被改了**
#   （实测 JACERS 第 4 页：图区 203/67725 采样点被改）。
#
# ⚠ 为什么不能只看「离图多近」：
#   实测正文行离图最近只有 2.1pt（第 1 页），
#   **图注**（"(a) Magnetostriction and piezomagnetic properties…"）离图 7.2pt
#   —— 它们**是论文的文字**，必须照翻。
#
# → 双条件：**短行**（≤ FIGURE_LABEL_MAX_LEN 字符）**且**离图 ≤ FIGURE_LABEL_RADIUS。
#   实测这样能抓住全部刻度/轴名/子图号，且不误伤正文与图注。
FIGURE_LABEL_MAX_LEN = 30
FIGURE_LABEL_RADIUS = 20.0


def _is_figure_line(rec: dict, protected, radius: float = FIGURE_LABEL_RADIUS,
                     max_len: int = FIGURE_LABEL_MAX_LEN) -> bool:
    """这一行是不是「图里的东西」（刻度 / 轴名 / 子图号）→ 不涂、不翻。

    判据两条，满足其一即可：
      ① 行的**中心**落在某个保护区里；
      ② 行**很短**（≤ max_len 字符）**且**离某个保护区 ≤ radius。
         —— 第②条专门抓落在聚类框**外**的刻度与轴名。
    """
    lx0, ly0, lx1, ly1 = rec["bbox"]
    cx = (lx0 + lx1) / 2.0
    cy = (ly0 + ly1) / 2.0
    for r in protected:
        if r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1:
            return True
    txt = (rec.get("text") or "").strip()
    if not txt or len(txt) > max_len:
        return False
    for r in protected:
        dx = max(r.x0 - lx1, lx0 - r.x1, 0.0)
        dy = max(r.y0 - ly1, ly0 - r.y1, 0.0)
        if (dx * dx + dy * dy) ** 0.5 <= radius:
            return True
    return False


def _visual_lines(records: List[dict], size_tol: float = 0.35,
                  x_gap_ratio: float = 2.0) -> List[dict]:
    """把「同一视觉行」的多条记录合并成一条。

    ⚠ 为什么必须合并（2026-11-24 真机取证）：
      PyMuPDF 会把**一个视觉行**拆成**多条** line 记录 —— 下标、字间距、
      字体切换都会造成拆分。实测 JACERS 第 1 页：
        · "I." + "Introduction"                → 两条
        · "…coefficient (a" + "ME), ME defined as" → 两条
      不合并 → 基线列表里出现**两条几乎相同的基线**（实测相差 0.05pt）
      → 中文的两行被排到**同一条基线**上 → **两行叠在一起**
      → 渲染出来是一团黑（实测第 1 页左栏 "电压系数（a ME ）描述" 那行）。

    合并判据（两条都要满足，避免把不同视觉行并到一起）：
      ① 基线相差 ≤ size_tol × 字号；
      ② 水平间隙 ≤ x_gap_ratio × 字号
         —— 后者专门防「**左右两栏**的同行」被并成一行
         （双栏页上两栏的同一行基线相同，但水平间隔 24pt ≫ 2×字号）。
    """
    out: List[dict] = []
    for rec in sorted(records, key=lambda r: r["baseline"]):
        if out:
            prev = out[-1]
            size = max(prev["size"], rec["size"], 1.0)
            if (abs(rec["baseline"] - prev["baseline"]) <= size_tol * size):
                b0, b1 = prev["bbox"], rec["bbox"]
                gap = max(b0[0] - b1[2], b1[0] - b0[2], 0.0)
                if gap <= x_gap_ratio * size:
                    # 合并：基线取**字号最大**那条的（正文基线，不是下标的）
                    if rec["size"] > prev["size"]:
                        prev["baseline"] = rec["baseline"]
                    prev["size"] = max(prev["size"], rec["size"])
                    prev["bbox"] = (min(b0[0], b1[0]), min(b0[1], b1[1]),
                                    max(b0[2], b1[2]), max(b0[3], b1[3]))
                    prev["text"] = (prev["text"] + " " + (rec["text"] or "")).strip()
                    continue
        out.append(dict(rec))
    return out


def _heading_records(group: List[dict]) -> List[dict]:
    """块末尾的「标题行」记录 —— **同一条基线**上的若干条。

    ⚠ 为什么不能只看最后一条（2026-11-24 真机取证）：
      PyMuPDF 会把 "I. Introduction" 拆成**两条** line 记录：
        · text='I.'          bbox=[149.1, 276.0, 155.2, 285.2]
        · text='Introduction' bbox=[164.3, 276.0, 211.0, 285.2]
      （两段之间有较大的水平间隙 → MuPDF 认为换行了）
      只看最后一条 → 拿到 "Introduction" → **认不出编号** → 拆不开。
      → 必须把**同基线**的末尾几条拼起来看。
    """
    if len(group) < 2:
        return []
    base = group[-1]["baseline"]
    tail: List[dict] = []
    for rec in reversed(group):
        if abs(rec["baseline"] - base) > 1.5:
            break
        tail.insert(0, rec)
    return tail


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

    ⚠ 2026-11-24 起主流程走 `_records_group()`（带基线与文本的行记录）；
      这个函数保留给外部/旧调用方。
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
# CJK 排版引擎（2026-11-24 新增）
#
# 为什么不用 `insert_textbox`：
#   MuPDF 的折行是**按空格分词**的 —— 一长串中文在它眼里是**整个一个「词」**，
#   放不下就整体推到下一行 → 行末留一大片白（实测 250pt 宽的行只排了 193pt，
#   实测第 3 行排到 250pt 而第 2 行只有 120pt —— 版面参差不齐）。
#   而且 `insert_textbox` 是**从框顶起排**、没有基线概念，
#   与周围未翻译的英文行**基线网格对不齐** → 「不像原版排版」。
#
# 真·中文化排版要的是：
#   ① 中文**任意位置可断行**；拉丁词尽量不拆；
#   ② **逐行基线定位**，直接落在原页那几行的基线上；
#   ③ **两端对齐**（最后一行不拉伸）；
#   ④ **不填满原框** —— 中文比英文短，段末自然留白，
#      这样每段占的行数 ≤ 原文行数，看起来才像「同一份排版」。
#
# 借鉴的是**思路**（不是代码）：pdf2zh `converter.py` 的逐行基线递推
#   `y = y0 - lidx * size * line_height`；BabelDOC `Typesetting.md` Algorithm 1
#   （放得下就放，放不下才扩框/降级）、Algorithm 2（降行距 → 降字号）。
#   详见 docs/RESEARCH_2026-11-24-PDF-TRANSLATION-OVERLAY.md
# ---------------------------------------------------------------------------

# CJK 字符区间（自己实现，不依赖第三方分词）
_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK 统一表意文字
    (0x3400, 0x4DBF),    # 扩展 A
    (0xF900, 0xFAFF),    # 兼容表意文字
    (0x20000, 0x2A6DF),  # 扩展 B
    (0x3000, 0x303F),    # CJK 标点
    (0xFF00, 0xFFEF),    # 全角
    (0x3040, 0x30FF),    # 假名
    (0xAC00, 0xD7AF),    # 谚文
)

# 行首禁则：这些字符不能出现在行首（要「避头」）
_NO_LINE_START = set("。，、；：？！）〕】》」』”’…—～·%‰°〉")
# 行尾禁则：这些字符不能出现在行尾（要「避尾」）
_NO_LINE_END = set("（〔【《〈「『“‘")


def _pair_gap(atom_a: Optional[Tuple[str, str]], atom_b: Tuple[str, str],
              fs: float) -> float:
    """中英交界额外加 0.5 字宽（与 positions() 保持同一口径）。"""
    if not atom_a or not atom_b:
        return 0.0
    a0, a1 = atom_a[1], atom_b[1]
    if not a0 or not a1 or a0 == " " or a1 == " ":
        return 0.0
    return (fs * 0.5) if (_is_cjk(a0[0]) != _is_cjk(a1[0])) else 0.0


def _is_cjk(ch: str) -> bool:
    """这个字符是不是 CJK（含全角标点）→ 可任意处断行。"""
    cp = ord(ch)
    for a, b in _CJK_RANGES:
        if a <= cp <= b:
            return True
    return False


def _split_atoms(text: str) -> List[Tuple[str, str]]:
    """把文本切成「排版原子」。

    返回 [(kind, chunk)]，kind ∈ {"cjk", "word", "space"}：
      · cjk  —— 一个汉字 / 全角符号 → **任意位置可断**
      · word —— 一个拉丁词（含内部 . - , 等）→ **尽量不拆**
      · space—— 空白 → 行尾丢弃
    """
    atoms: List[Tuple[str, str]] = []
    buf = ""
    for ch in text:
        if ch in " \t\u3000\u00a0":
            if buf:
                atoms.append(("word", buf))
                buf = ""
            atoms.append(("space", " "))
            continue
        if _is_cjk(ch):
            if buf:
                atoms.append(("word", buf))
                buf = ""
            atoms.append(("cjk", ch))
            continue
        buf += ch
    if buf:
        atoms.append(("word", buf))
    return atoms


class _CjkTypesetter:
    """中文排版：量宽 → 断行 → 逐行基线定位 → 两端对齐。

    为什么自己写：MuPDF 的折行是拉丁语义（按空格分词），
    中文在它眼里是「一个超长的词」→ 行末大片留白、版面参差。
    """

    def __init__(self, font: CjkFont, bold: bool = False):
        self.font = font
        self.bold = bool(bold)
        self._fz = font.fitz_font(self.bold)

    # ---- 量宽 ----
    def measure(self, s: str, fs: float) -> float:
        if not s:
            return 0.0
        if self._fz is not None:
            try:
                return float(self._fz.text_length(s, fs))
            except Exception:
                pass
        # 兜底：CJK 按 1 em、拉丁按 0.5 em 估
        w = 0.0
        for ch in s:
            w += fs * (1.0 if _is_cjk(ch) else 0.5)
        return w

    def line_width(self, line: List[Tuple[str, str]], fs: float) -> float:
        """含中英交界半角空隙的行总自然宽度。"""
        if not line:
            return 0.0
        total = sum(self.measure(c, fs) for (_k, c) in line)
        for i in range(len(line) - 1):
            total += _pair_gap(line[i], line[i + 1], fs)
        return total

    # ---- 断行 ----
    def wrap(self, atoms: List[Tuple[str, str]], width: float,
             fs: float) -> List[List[Tuple[str, str]]]:
        """贪心断行。返回每一行的原子列表（含中英交界间隙 + 避头尾禁则）。"""
        lines: List[List[Tuple[str, str]]] = []
        cur: List[Tuple[str, str]] = []
        cur_w = 0.0

        def _flush_with_no_line_end() -> List[Tuple[str, str]]:
            """行尾避尾（开引号/开括号不能留在行尾）：拉到下一行开头。"""
            nonlocal cur, cur_w
            while cur and cur[-1][0] == "space":
                cur.pop()
            carried: List[Tuple[str, str]] = []
            while len(cur) > 1 and cur[-1][1] in _NO_LINE_END:
                carried.insert(0, cur.pop())
            if cur:
                lines.append(cur)
            cur = carried
            cur_w = self.line_width(cur, fs)
            return carried

        for kind, chunk in atoms:
            if kind == "space":
                if not cur:
                    continue                     # 行首空白丢弃
                cur.append((kind, chunk))
                cur_w += self.measure(chunk, fs)
                continue
            # 连续禁则标点（如 "。”"）：上一行刚按悬挂标点收尾导致 cur 为空时，
            # 也不能让第二个禁则标点落到下一行行首。
            is_no_start = (chunk in _NO_LINE_START)
            if is_no_start and not cur and lines:
                prev_ln = lines[-1]
                # 如果前一行末尾已经挂了标点且还能附着 1 个，直接附着；
                # 否则把前一行最后一个非禁则原子一起挪到新行。
                trailing_no_start = 0
                for _ak, ac in reversed(prev_ln):
                    if ac in _NO_LINE_START:
                        trailing_no_start += 1
                    else:
                        break
                if trailing_no_start < 2:
                    prev_ln.append((kind, chunk))
                    continue
                pull: List[Tuple[str, str]] = []
                while len(prev_ln) > 1 and prev_ln[-1][1] in _NO_LINE_START:
                    pull.insert(0, prev_ln.pop())
                if len(prev_ln) > 1 and prev_ln[-1][1] not in _NO_LINE_START:
                    pull.insert(0, prev_ln.pop())
                cur = pull + [(kind, chunk)]
                cur_w = self.line_width(cur, fs)
                continue

            w = self.measure(chunk, fs)
            gap = _pair_gap(cur[-1], (kind, chunk), fs) if cur else 0.0
            if cur_w + gap + w <= width + 0.01:
                cur.append((kind, chunk))
                cur_w += gap + w
                continue
            # ---- 放不下 ----
            if is_no_start and cur:
                # **标点悬挂**：让标点越过右边距一点点（行末不空一整格），
                # 连续禁则由上面 not cur and lines 分支接管。
                cur.append((kind, chunk))
                lines.append(cur)
                cur, cur_w = [], 0.0
                continue
            if kind == "cjk":
                _flush_with_no_line_end()
                gap2 = _pair_gap(cur[-1], (kind, chunk), fs) if cur else 0.0
                cur.append((kind, chunk))
                cur_w += gap2 + w
                continue
            # 拉丁词：整体挪到下一行（词内不断）
            if cur:
                _flush_with_no_line_end()
            gap2 = _pair_gap(cur[-1], (kind, chunk), fs) if cur else 0.0
            if cur_w + gap2 + w > width + 0.01 and not cur:
                # 空行也放不下 → 强制拆词（超长 URL / 化学式）
                for ch in chunk:
                    cw = self.measure(ch, fs)
                    if cur and cur_w + cw > width + 0.01:
                        _flush_with_no_line_end()
                    cur.append(("word", ch))
                    cur_w += cw
                continue
            if cur and cur_w + gap2 + w > width + 0.01:
                lines.append(cur)
                cur, cur_w = [], 0.0
                gap2 = 0.0
            cur.append((kind, chunk))
            cur_w += gap2 + w
        if cur:
            lines.append(cur)
        out: List[List[Tuple[str, str]]] = []
        for ln in lines:
            while ln and ln[-1][0] == "space":
                ln.pop()                            # 行尾空白丢弃
            if ln:
                out.append(ln)
        return out

    # ---- 两端对齐：算出每个原子的落位 x ----
    def positions(self, line: List[Tuple[str, str]], fs: float,
                  x0: float, target: float, justify: bool
                  ) -> List[Tuple[Tuple[str, str], float]]:
        """返回 [(atom, x)] —— 每个原子写在哪。

        两端对齐：多出来的宽度平均分给原子间隙（上限 0.4 字宽，
        超了就不拉伸 —— 宁可短一点，也不要「稀得不像话」）。
        中英交界额外加 0.5 字宽（Unicode UTR #59 的做法），且与折行共享同一口径。
        """
        if not line:
            return []
        widths = [self.measure(c, fs) for (_k, c) in line]
        pair_gaps = [_pair_gap(line[i], line[i + 1], fs) for i in range(len(line) - 1)]
        natural = sum(widths) + sum(pair_gaps)
        gaps = max(1, len(line) - 1)
        extra = 0.0
        if justify and natural < target:
            extra = min((target - natural) / gaps, fs * 0.4)
        out: List[Tuple[Tuple[str, str], float]] = []
        x = float(x0)
        for i, (atom, w) in enumerate(zip(line, widths)):
            out.append((atom, x))
            if i < len(line) - 1:
                x += w + pair_gaps[i] + extra
        return out


def _page_line_records(spans: dict) -> List[dict]:
    """原页的文字行（含**基线**与文本）。

    替换页的中文要落在**原页那几行的基线**上 → 必须先知道基线在哪。
    基线取该行 span 的 ``origin.y``（取最大的那个 —— 上标的 origin 更小）。
    """
    out: List[dict] = []
    for blk in (spans.get("blocks") or []):
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines") or []:
            bb = line.get("bbox")
            if not bb:
                continue
            if (bb[2] - bb[0]) <= 0.5 or (bb[3] - bb[1]) <= 0.5:
                continue
            sps = line.get("spans") or []
            text = "".join((sp.get("text") or "") for sp in sps)
            sizes = [float(sp.get("size") or 0.0) for sp in sps] or [0.0]
            mx = max(sizes) or 1.0
            origins = [float((sp.get("origin") or (0.0, bb[3]))[1]) for sp in sps
                       if float(sp.get("size") or 0.0) >= 0.9 * mx]
            baseline = max(origins) if origins else float(bb[3])
            out.append({"bbox": tuple(float(v) for v in bb),
                        "baseline": baseline,
                        "text": text,
                        "size": mx})
    return out


# ---------------------------------------------------------------------------
# 字号候选
#
# ⚠ 历史坑（2026-11-15）：拉丁词**不能从中间断行**
#   （`NiFeO₂₄`、`Pb(Zr,Ti)O₃`、`viehland@mse.vt.edu`…），
#   纯 CJK 行的「总宽 ÷ 框宽」会**低估行数** →
#   算着放得下、PyMuPDF 实际拒写（实测 6 块栽在这）。
#   → 现在的做法：断行由 `_CjkTypesetter.wrap()` 自己做（拉丁词整体不拆），
#     宽度由 `fitz.Font.text_length()` **精确**量，不再靠估。
# ---------------------------------------------------------------------------
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

# 独立成行的**小节标题**（IEEE / ACM 常见）：
#   "I. Introduction" / "II. Experimental Procedure" / "3.1 Some Topic"
#
# ⚠ 为什么要在替换版里单独处理（2026-11-24 真机取证）：
#   抽取层的段落块会把「独立成行的标题」**吸进上一段的末尾**
#   （实测 JACERS 第 1 页：i=4 的源文本尾部是 "I. Introduction"，
#     i=15 的尾部是 "II. Experimental Procedure"）
#   → 译文末尾就多出「I. 引言」「II. 实验方法」，
#     贴在正文最后一句后面，看起来像**排版坏了**。
#
#   → 这里把标题从译文尾部**切出来**，单独落到它自己的那行基线上。
#     ⚠ 不动抽取层 → **不用 bump BLOCK_TRANSLATION_VERSION** →
#       用户已有的段落译文**不用重翻**。
_HEADING_NUM_RE = re.compile(
    r"^\s*(?P<num>"
    r"\(?\d{1,2}(?:\.\d{1,2})*\)?\.?"        # 1. / 1.1 / (1)
    r"|\(?[IVXLC]{1,6}\)?\."                 # I. / II. / (I).
    r"|\(?[A-H]\)?\."                       # A. / (A).
    r")\s+\S")

# 标题行的最大长度（超过就不当作标题 —— 正文里也会出现 "1. " 开头的列表项）
_HEADING_MAX_LEN = 60


def _heading_num(text: str) -> Optional[str]:
    """这一行是不是小节标题？是的话返回它的编号（"I" / "II" / "1" / "1.1"）。"""
    raw = (text or "").strip()
    if not raw or len(raw) > _HEADING_MAX_LEN:
        return None
    m = _HEADING_NUM_RE.match(raw)
    if not m:
        return None
    core = m.group("num").strip().strip("()").rstrip(".").strip()
    return core or None


def _split_heading_tail(text: str, num: str) -> Optional[Tuple[str, str]]:
    """把译文尾部的小节标题切出来。返回 ``(正文, 标题译文)``，切不出返回 None。

    ⚠ 判据必须**绑定原文的编号**（``num``）——
      否则 "…的 ME 系数" 这种以拉丁字母结尾的句子会被误当成标题切掉。
    """
    pat = re.compile(r"(?:^|(?<=[\s，。；：、（）()]))"
                     + re.escape(num) + r"[.、]?\s*(?P<title>[^\n。；，]{1,48}?)\s*$")
    m = pat.search(text or "")
    if not m:
        return None
    body = (text[:m.start()]).rstrip()
    head = text[m.start():].strip()
    if not body or not head:
        return None
    return body, head


@dataclass
class _WritePlan:
    """一个待写入的译文片段（正文段，或从正文段里拆出来的小节标题）。"""
    lines: List[List[Tuple[str, str]]]
    baselines: List[float]
    fs: float
    x0: float
    width: float
    bold: bool = False
    center: bool = False


def _records_group(rect, records: List[dict], protected, tol: float = 1.5
                   ) -> List[dict]:
    """与块框**重叠**的原页文字行记录（图表里的行除外）。

    重叠的判据：行的**垂直中心**落在块的 y 范围内，
    且行的 x 范围与块的 x 范围有交集 —— 后者保证不会把**另一栏**的文字拉进来。
    """
    group = []
    for rec in records:
        lx0, ly0, lx1, ly1 = rec["bbox"]
        cy = (ly0 + ly1) / 2.0
        if cy < rect.y0 - tol or cy > rect.y1 + tol:
            continue
        if lx1 < rect.x0 - tol or lx0 > rect.x1 + tol:
            continue
        if _is_figure_line(rec, protected):
            continue
        group.append(rec)
    # ⚠ 必须**按基线排序**：PyMuPDF 的 blocks/lines 是按 PDF 内部顺序给的，
    #   **不是**从上到下 → 不排序的话「块的最后一行」会认错。
    group.sort(key=lambda rec: rec["baseline"])
    return group


def _extend_baselines(baselines: List[float], spacing: float,
                      bottom_limit: float) -> List[float]:
    """基线往下延伸（收紧行距时用），不越过 ``bottom_limit``。"""
    out = list(baselines)
    if not out or spacing <= 0:
        return out
    y = out[-1]
    while True:
        y += spacing
        if y > bottom_limit:
            break
        out.append(y)
        if len(out) > 240:
            break
    return out


def _layout_block(ts: "_CjkTypesetter", text: str, x0: float, width: float,
                  baselines: List[float], fs_base: float, fs_min: float,
                  bottom_limit: Optional[float] = None,
                  spacing: Optional[float] = None
                  ) -> Optional[Tuple[List[List[Tuple[str, str]]], float, List[float]]]:
    """把译文排到给定的基线上。

    返回 ``(每行的原子, 实际字号, 实际用到的基线)``；排不下返回 None。

    降级顺序（BabelDOC Algorithm 2 的思路）：
      ① 原样基线网格 + 原字号；
      ② 原样基线网格 + 逐档缩字号；
      ③ **收紧行距**、基线往下延伸（不越界）+ 逐档缩字号。
    """
    atoms = _split_atoms(text or "")
    if not atoms or not baselines or width <= 1:
        return None
    cands = list(_size_candidates(fs_base, fs_min))
    if not cands:
        return None
    # ① / ②：原样基线网格
    for fs in cands:
        lines = ts.wrap(atoms, width, fs)
        if len(lines) <= len(baselines):
            return lines, fs, baselines[:len(lines)]
    # ③：收紧行距 + 向下延伸
    if bottom_limit is not None and spacing and spacing > 0:
        for squeeze in (0.92, 0.85, 0.78):
            ext = _extend_baselines(baselines, spacing * squeeze, bottom_limit)
            if len(ext) <= len(baselines):
                continue
            for fs in cands:
                lines = ts.wrap(atoms, width, fs)
                if len(lines) <= len(ext):
                    return lines, fs, ext[:len(lines)]
    return None


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

    2026-11-24 重写的三条主线（都有真机取证支撑）：
      ① **涂改只涂原页真实的文字行**，且每块涂改范围都**减去图表保护区**
         → 图上再也不会出现白方块（「图表保持不变」的红线）。
      ② **中文逐行落在原页的基线网格上** + 两端对齐 + 不填满原框
         → 版面与原文对齐，段末自然留白。
      ③ **小节标题从正文尾部拆出来**，单独落位
         → 译文末尾不再莫名多出「I. 引言」。
    """
    import fitz

    font = font or _resolve_font()
    stats = BuildStats()
    src = fitz.open(str(src_pdf))
    out = fitz.open()
    ts = _CjkTypesetter(font, bold=False)
    ts_bold = _CjkTypesetter(font, bold=True) if font.bold_path else ts
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

            # 2) 先在「原页」做全量分析与排版预算 —— 没有任何块可写时不再插入无效替换页
            #
            # ⚠⚠ **一律用「原页」做分析**，不要用复制页！
            #   `out.insert_pdf()` 复制后页面会被**重新编码**，
            #   `cluster_drawings()` 的聚类结果随之改变
            #   （实测 JACERS 第 4 页：原页聚成 5 块，复制页聚成别的样子，
            #     一块子图从 [325.8,222.7,497.3,362.2] 变成
            #     [325.8,222.7,518.7,374.0]）→
            #   保护区认错了 → 图上被写进中文（实测 203 个采样点损伤）。
            #   → 原页才是「哪里是图」的**唯一真相**。
            src_page = src.load_page(idx)

            # 涂之前先量：字号要靠**原文**的 span 算（涂完就没了）
            spans = src_page.get_text("dict")
            records = _page_line_records(spans)
            protected = _protected_rects(src_page)

            all_rects: List[Tuple[Block, object]] = []
            for blk in sorted(blocks, key=lambda b: b.index):
                ar = _clamp_rect(blk.bbox, src_page.rect)
                if ar.width > 1 and ar.height > 1:
                    all_rects.append((blk, ar))

            rects: List[Tuple[Block, object]] = []
            for blk in sorted(usable, key=lambda b: b.index):
                r = _clamp_rect(blk.bbox, src_page.rect)
                if r.width <= 1 or r.height <= 1:
                    stats.blocks_skipped += 1
                    continue
                rects.append((blk, r))

            # ⚠ **先算落位方案，再涂**：
            #   涂了才知道放不放得下 → 放不下就只能截断，
            #   而「截断的译文」比「保留英文原文」更糟（信息直接丢了）。
            #   → 放不下的块**不涂不写**，替换页上保留它的英文原样。
            plans: List[_WritePlan] = []
            redactions: List[object] = []
            for blk, r in rects:
                bold = (blk.kind == "heading")
                text = clean_translation(blk.translation)
                text = fix_missing_glyphs(text, font, bold)
                if not text:
                    stats.blocks_skipped += 1
                    continue

                # ⚠ 块**整个**落在图/矢量图里 → 整个跳过。
                #   实测 JACERS 首页底部的 "Review" 艺术字就在渐变图上：
                #   原来它被当成正文 → 涂改在图上盖出 210×35.6pt 的**纯白矩形**，
                #   译文还被放大成巨大的黑字「综述」压在图上。
                #   → 艺术字属于**图**，保持原样。
                ccx = (r.x0 + r.x1) / 2.0
                ccy = (r.y0 + r.y1) / 2.0
                if any(pr.x0 <= ccx <= pr.x1 and pr.y0 <= ccy <= pr.y1
                       for pr in protected):
                    stats.blocks_in_figure += 1
                    continue

                # 该块覆盖的原页文字行（图表里的行已排除）
                group = _records_group(r, records, protected)
                if not group:
                    stats.blocks_skipped += 1
                    continue
                # 同一视觉行归并 —— PyMuPDF 会把一个视觉行拆成多条记录
                # （下标/字间距/字体切换），不合并 → 基线列表里出现两条
                # 几乎相同的基线 → 中文两行叠在一起（实测渲染成一团黑）。
                group = _visual_lines(group)
                gx0 = min([r.x0] + [g["bbox"][0] for g in group])
                gx1 = max([r.x1] + [g["bbox"][2] for g in group])
                width = gx1 - gx0
                if width <= 4:
                    stats.blocks_skipped += 1
                    continue

                en_fs = _dominant_font_size(spans, r) or _fallback_font_size(r)
                base_fs = en_fs * cjk_size_ratio
                min_fs = max(MIN_FONT_SIZE, base_fs * min_fs_ratio)

                # 空间：先借一点（MAX_BORROW），同时避开含公式/表格在内的下方块
                nxt = _next_block_top(all_rects, r, blk.col)
                gap = (nxt - r.y1) if nxt else (src_page.rect.y1 - r.y1)
                allowance = max(0.0, min(MAX_BORROW, gap))
                bottom_limit = min(r.y1 + allowance, src_page.rect.y1)

                baselines = []
                for g in group:
                    # ⚠ 基线**去重**：双栏页上左右两栏的同行基线相同
                    #   → 不去重的话中文的第 2、3 行会排到**同一条基线**上
                    #   → 两行叠在一起。
                    if not baselines or abs(g["baseline"] - baselines[-1]) > 0.5:
                        baselines.append(g["baseline"])
                spacings = [baselines[i + 1] - baselines[i]
                            for i in range(len(baselines) - 1)]
                spacing = (sorted(spacings)[len(spacings) // 2]
                           if spacings else base_fs * LINE_RATIO)

                # ---- ③ 小节标题：从正文尾部拆出来单独落位 ----
                head_plan = None
                head_recs = _heading_records(group)
                head_text_raw = " ".join((rec.get("text") or "").strip()
                                         for rec in head_recs).strip()
                num = _heading_num(head_text_raw)
                if num and len(group) >= 2:
                    split = _split_heading_tail(text, num)
                    if split:
                        body_text, head_text = split
                        hb = head_recs[0]["bbox"]
                        hx0 = min(r0[0] for r0 in (rec["bbox"] for rec in head_recs))
                        hx1 = max(r0[2] for r0 in (rec["bbox"] for rec in head_recs))
                        h_fs = (head_recs[0].get("size") or en_fs) * cjk_size_ratio
                        h_min = max(MIN_FONT_SIZE, h_fs * min_fs_ratio)
                        h_w = max(hx1 - hx0, 4.0)
                        # 标题通常是**居中**的：与栏中心接近就居中写
                        col_mid = (gx0 + gx1) / 2.0
                        line_mid = (hx0 + hx1) / 2.0
                        centered = abs(col_mid - line_mid) <= 8.0
                        laid = _layout_block(
                            ts_bold, head_text, hx0, h_w,
                            [head_recs[0]["baseline"]], h_fs, h_min,
                            bottom_limit=hb[3] + MAX_BORROW,
                            spacing=spacing)
                        if laid:
                            hl, hfs, hbl = laid
                            head_plan = _WritePlan(lines=hl, baselines=hbl,
                                                   fs=hfs, x0=hx0,
                                                   width=h_w,
                                                   bold=True, center=centered)
                            text = body_text
                            group = group[:-len(head_recs)]
                            baselines = []
                            for g in group:
                                if not baselines or abs(g["baseline"] - baselines[-1]) > 0.5:
                                    baselines.append(g["baseline"])
                            spacing = (baselines[-1] - baselines[-2]
                                       if len(baselines) >= 2 else spacing)
                            stats.headings_split += 1

                plan_layout_ts = ts_bold if bold else ts
                laid = _layout_block(plan_layout_ts, text, gx0, width, baselines,
                                     base_fs, min_fs,
                                     bottom_limit=bottom_limit,
                                     spacing=spacing)
                if laid is None:
                    stats.blocks_unfit += 1
                    stats.warnings.append(
                        "第 %d 页第 %d 块：中文放不进原框（框 %.1f×%.1fpt）→ 保留英文原样"
                        % (pno, blk.index, r.width, r.height))
                    continue
                lines, fs, used = laid
                plans.append(_WritePlan(lines=lines, baselines=used, fs=fs,
                                        x0=gx0, width=width, bold=bold))
                stats.blocks_written += 1
                # ---- 涂改范围：只涂**原页真实的行**，且减去图表保护区 ----
                for g in group:
                    for piece in _clip_to_protected(fitz.Rect(g["bbox"]), protected):
                        redactions.append(piece)
                if head_plan is not None:
                    plans.append(head_plan)
                    for rec in head_recs:
                        for piece in _clip_to_protected(fitz.Rect(rec["bbox"]),
                                                        protected):
                            redactions.append(piece)

            # ⚠ 关键：**只删文字**，图片与矢量图原样保留
            #   （用户要「原本的图表还都保持不变」）
            #   ⚠ 而且**不涂块的矩形** —— 块矩形会盖住图（实测白方块）；
            #     只涂原页真实的文字行，且每块都减去图表保护区。
            # ⚠ 去重：同一条原页行可能被多个块命中（参考文献页实测 170 个
            #   重复白矩形）→ 涂一次就够，文件也小一圈。
            # 配对约束（「10 页原文 → 20 页替换版」）：只要这页有可用块，就保留对应的替换页；
            # 哪怕全部放不下（plans 为空），也应原样保留英文作为替换页而非缺页。
            out.insert_pdf(src, from_page=idx, to_page=idx)
            page = out.load_page(out.page_count - 1)

            seen_rects = set()
            for rect in redactions:
                key = (round(rect.x0, 1), round(rect.y0, 1),
                       round(rect.x1, 1), round(rect.y1, 1))
                if key in seen_rects:
                    continue
                seen_rects.add(key)
                # ⚠⚠ **fill=None**：redaction 只该「删掉文字」，**不该再刷一层底色**。
                #   `fill=bg` 会把整个矩形**填成背景色** → 三个后果（实测）：
                #     ① 页面的**彩色背景**被冲成纯白
                #        （探针：彩色底 (229,216,153) → (255,255,255)）；
                #     ② 每涂一行多一个**白色实心矩形** —— JACERS 第 8 页（纯文字页）
                #        实测多出 **163 个**，它们是**可选中的空白块**，还让文件变大；
                #     ③ 多一层压图风险。
                #   删掉文字后**露出页面自己的背景**才对（扫描件的米色底也保住）。
                page.add_redact_annot(rect, fill=None)
            if redactions:
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                    text=fitz.PDF_REDACT_TEXT_REMOVE,
                )

            # 3) 原地写中文 —— 逐行基线定位 + 两端对齐
            writers = {}
            for plan in plans:
                key = bool(plan.bold and font.bold_path)
                tw = writers.get(key)
                if tw is None:
                    tw = fitz.TextWriter(page.rect)
                    writers[key] = tw
                fz_font = font.fitz_font(bold=key)
                plan_ts = ts_bold if key else ts
                for li, line_atoms in enumerate(plan.lines):
                    if li >= len(plan.baselines):
                        break
                    baseline = plan.baselines[li]
                    justify = (align == TEXT_ALIGN_JUSTIFY
                               and li < len(plan.lines) - 1 and not plan.center)
                    pos = plan_ts.positions(line_atoms, plan.fs, plan.x0,
                                            plan.width, justify)
                    if plan.center:
                        natural = plan_ts.line_width(line_atoms, plan.fs)
                        shift = max(0.0, (plan.width - natural) / 2.0)
                        pos = [(a, x + shift) for (a, x) in pos]
                    for (atom, x) in pos:
                        _kind, chunk = atom
                        if not chunk:
                            continue
                        tw.append((x, baseline), chunk,
                                  font=fz_font, fontsize=plan.fs)
                    stats.lines_written += 1
            for tw in writers.values():
                tw.write_text(page)
            stats.pages_replaced += 1

        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        # 子集化字体：不这么做的话，嵌入的雅黑会**整个**塞进 PDF
        #（17MB/份）→ 生成的文件大得没法发。
        # ⚠ fallback=False：默认 True 会把**缺字字形**也补进子集 → 体积翻倍。
        try:
            out.subset_fonts(fallback=False)
        except TypeError:
            try:
                out.subset_fonts()
            except Exception:
                pass
        except Exception:
            pass
        # garbage=4：清掉被涂掉的文字残留（不然 PDF 里还能搜到英文）
        out.save(str(out_pdf), garbage=4, deflate=True)
    finally:
        out.close()
        src.close()
    return stats
