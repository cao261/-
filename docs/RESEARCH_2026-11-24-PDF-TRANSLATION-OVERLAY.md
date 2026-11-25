# 调研：把译文覆盖回原 PDF 的开源实现（2026-11-24）

> **这份文件给任何接着做「中译替换版 PDF」的人看。**
> 核实方式：GitHub 元数据经 `ungh.cc`（免鉴权代理）+ `shields.io` 徽章 JSON 交叉核实；
> 源码逐行读 `raw.githubusercontent.com` 原文；PyMuPDF API 用本机 `fitz` 实测
> （本机版本 **PyMuPDF 1.27.2.3 / MuPDF 1.27.2**）。
>
> ⚠ **合规前提（贯穿全文）**：pdf2zh / BabelDOC / zotero-pdf-translate 均为 **AGPL-3.0**。
> 本文件只提炼**算法思路**，**不复制其源码**；落地为代码的片段一律自行重写
> （并优先参考 Unicode UTR #59 等公开标准）。

---

## 一、候选项目清单

| 名称 | URL | 技术路线 | 活跃度（2026-11 实测） | 许可证 | 可借鉴点 |
|---|---|---|---|---|---|
| **PDFMathTranslate / pdf2zh** | https://github.com/PDFMathTranslate/PDFMathTranslate （原 `Byaidu/PDFMathTranslate` 已重定向） | pdfminer.six 解析 + **内容流算子级重建**（丢 `T*` 文字算子、留图形算子）+ DocLayout-YOLO 版面掩码 | **38,596 ★**，最后推 commit **2026-11-23**（当天） | AGPL-3.0 | ★★★★★ **擦除/落位分离的底层范式**；中英混排加空格；行距自适应 |
| **BabelDOC** | https://github.com/funstory-ai/BabelDOC | 自建 IL（字符级 `pdf_character` / 段落 / 公式 `PdfFormula`）+ DocLayout-YOLO + 自研排版算法 + 自研 CID/ToUnicode 层 | **9,892 ★**，最后推 commit **2026-11-07** | AGPL-3.0 | ★★★★★ **排版三段式**、**ToUnicode 归一化**、**「原文页+替换页交替」现成实现** |
| **zotero-pdf-translate** | https://github.com/windingwind/zotero-pdf-translate | Zotero 插件，划词/标题/批注翻译 | **12,331 ★**，2026-11-13 | AGPL-3.0-or-later | ⚠ **不产出译文 PDF**，无版面覆盖借鉴价值 |
| **pot-desktop** | https://github.com/pot-app/pot-desktop | 划词翻译 + 截图 OCR | **19,322 ★**，2026-07-04（社区称已封存） | MIT / GPL-3.0 | ⚠ 同上，**不做 PDF 排版输出** |
| **immersive-translate** | https://github.com/immersive-translate/immersive-translate | 浏览器扩展 | **19,869 ★**，2026-11-20 | 混合 | 仅产品形态参考 |
| **MinerU** | https://github.com/opendatalab/MinerU | PDF → Markdown/JSON | **84,155 ★**，2026-11-22 | AGPL-3.0 | 若将来重做抽取层可参考 |
| **docling** | https://github.com/docling-project/docling | 版面 + 公式/表格模型 | **70,889 ★**，2026-11-22 | **MIT** | **MIT 可放心借鉴**版面/公式区域检测 |
| **PDFMathTranslate-next** | https://github.com/PDFMathTranslate/PDFMathTranslate-next | v2 内核桥接 → BabelDOC | **827 ★**，2026-11-10 | AGPL-3.0 | v2 已把内核交给 BabelDOC → 技术细节直接看 BabelDOC |
| **RockTranslate** | https://github.com/PerfectWin7777/RockTranslate | pdf2htmlEX 空间 DOM → 浏览器 → 矢量 PDF；`scaleX()` 横向微调 | **3 ★**，2026-11-13 | **MIT** | 「超宽文本用横向压扁而不是缩字号」很实用 |
| **translate-shell / Translators / PDF Xray** | — | — | — | — | ❌ 与本需求**无关**（CLI 文本翻译 / 翻译库 / PDF 取证工具） |

> ⚠ **未能核实**：`bytedance/Translators` 仓库已 404（GitHub API 与 shields 均 `repo not found`），
> 无法读取其 `pdfTranslator` 源码 → **不对其实现做任何断言**。
> ⚠ BabelDOC 主力内核已迁 Rust（`rust/p2_stages/src/stages/typesetting.rs`，62.8KB，**未逐行读**）。

---

## 二、三个「负面结论」（避免走弯路）

1. **zotero-pdf-translate 不产出译文 PDF** —— 仓库文件树里没有任何 PDF 写出模块，
   README 只描述划词/标题/摘要/批注翻译 + KaTeX 渲染。它是**在 Zotero 侧栏里显示译文**。
2. **pot-desktop 同样不做 PDF 覆盖** —— PDF 能力是「截图 OCR」「抽取文字 + 逐页 OCR」，
   输出纯文本/双栏预览，不是回写 PDF 的排版。
3. **translate-shell / Translators / PDF Xray 与本需求无关。**

> 结论：**真正值得借鉴的只有 pdf2zh 与 BabelDOC 两条线**，外加 MIT 的 docling / RockTranslate 作旁证。

---

## 三、关键技术细节

### 3.1 「保留原页图像/矢量，只擦文字层，再叠译文层」——有，且 pdf2zh 给出了最干净的范式

`pdf2zh/pdfinterp.py` 的 `PDFPageInterpreterEx.execute()` 在解析内容流时逐算子重建，
**丢弃所有 `T*` 文字算子**（判据 `name[0] == "T"`，覆盖 `Tj/TJ/Tf/Td/TD/Tm/TL/Tc/Tw/Tz/Ts/Tr`），
保留其余算子；`"`、`'`、`EI`、`MP`、`DP`、`BMC`、`BDC` 显式保留。

`process_page()` 最终把页面内容流拼成：

```
q {ops_base}Q 1 0 0 1 {x0} {y0} cm {ops_new}
```

- `ops_base` = 原始内容流去掉文字算子后的**图形部分**（含图像 XObject）
- `ops_new` = 设备侧收集到的**新排版内容**（译文 + 公式字形 + 重发的描边）
- `q/Q` 重置位置矩阵，保证译文**覆盖**在原图形之上

这正是「**擦文字层、留矢量与图像、再叠译文层**」的教科书实现，**完全不依赖 redaction**。

⚠ **但 pdf2zh 有个明显代价**：`do_f` / `do_F` / `do_f_a` / `do_B` / `do_B_a` 被**全部置空**
（填充型路径不绘制），`do_S` 只保留「两点、水平、黑色」的描边 →
**pdf2zh 输出里的图表是降级重画的，不是原图** → 与我们「图表/矢量图一个不动」的红线直接冲突。
→ **只能借它的「文字/图形分流」思想，不能照抄它的图形处理。**

### 3.2 擦除用的具体 API

| 方案 | 项目 | API / 文件 |
|---|---|---|
| 内容流算子过滤 | pdf2zh | `pdf2zh/pdfinterp.py` → `PDFPageInterpreterEx.execute()` / `process_page()` |
| IL 重建 | BabelDOC | `babeldoc/format/pdf/document_il/backend/pdf_creater.py` → `PDFCreater` |
| **PyMuPDF redaction** | **我们当前** | `microbench/replaced_pdf.py` → `page.apply_redactions(images=PDF_REDACT_IMAGE_NONE, graphics=PDF_REDACT_LINE_ART_NONE, text=PDF_REDACT_TEXT_REMOVE)` |
| 其它 | — | pikepdf/qpdf/pypdf/PDFBox/iText —— **本次调研的成熟项目都没用** |

**本机 PyMuPDF 实测可用**：`PDF_REDACT_IMAGE_NONE`、`PDF_REDACT_LINE_ART_NONE`、
`PDF_REDACT_TEXT_REMOVE`、`Page.cluster_drawings()`、`Page.get_texttrace()`、
`Page.insert_htmlbox()`、`fitz.Story`、`fitz.TextWriter`。

### 3.3 译文是怎么定位的？

**版面分析层**
- **pdf2zh**：`pdf2zh/doclayout.py` → `OnnxModel.predict()`（DocLayout-YOLO ONNX，imgsz=1024，
  conf>0.25）产出**逐像素布局掩码**；`converter.py` 中 `cls == 0` 表示「**保留区域**」
  （图表/公式），其中字符**不翻译、不改写**。
- **pdf2zh ultrafast**：`pdf2zh/ultrafast.py` → `extract_layout()` 用词级位置；
  `layout_mask()` 用「字号相近（0.75 < size/prev < 1.35）且同行或紧邻」聚成 region
  —— **不依赖深度模型的启发式版面**。
- **BabelDOC**：`babeldoc/format/pdf/document_il/utils/extract_char.py`
  - `_cluster_by_axis()`：字符 → **band**（垂直重叠 > `BAND_CREATION_OVERLAP_THRESHOLD=0.5`）
    → **DBSCAN**（`eps = 平均字宽 × LINE_CLUSTERING_EPS_MULTIPLIER=3.5`，metric=manhattan）→ 行
  - 再用 `LINE_SPLIT_SIZE_RATIO_THRESHOLD=1.5` **拆「过高/过宽的行」**（双栏被误并成一行时在此劈开）
  - `_merge_lines_on_page()`：包含合并（IoU>0.6）/ 相邻合并（垂直重叠>0.7 且水平间隙 < 平均字宽×1.5）
  - `_recalculate_line_text_with_spacing()`：间隙 > 平均字宽×0.45 时补空格

**坐标粒度**
- **pdf2zh**：`Paragraph(y, x, x0, x1, y0, y1, size, brk)` —— 段落级 bbox + 字号 + **原文硬换行标记**。
- **BabelDOC**：**字符级**。`page.pdf_character` 每字符带 `visual_bbox.box` 与 `pdf_style`；
  段落是 `page.pdf_paragraph` → `pdf_paragraph_composition`。
  **同一份 IL 同时供给「哪些字符要删」「译文放哪」「公式原样搬哪」** ——
  这正是「用同一份版面分析结果同时驱动擦除范围和译文落位」的现成答案。
- **我们当前**：抽取层给的**块级 bbox**（pdfplumber 坐标）＋ PyMuPDF 侧 `_page_lines()` /
  `_line_group()` 事后对齐到**行**。两套坐标源 → 根子上仍可能错位。

**OCR / LayoutLM / docTR**：pdf2zh 与 BabelDOC **主路径都不用 OCR**（OCR 只在扫描件兜底），
**都不用 LayoutLM / docTR**，用的是 **DocLayout-YOLO**（轻量 ONNX，纯 CPU）。
公式**不是**靠 OCR，而是靠**字体名 + Unicode 字符类别**规则。

### 3.4 字号 / 行距 / 对齐 / 放不下怎么办

**BabelDOC 三段式**（`docs/ImplementationDetails/Typesetting/Typesetting.md`
⚠ 该文档自带「可能含 AI 生成内容」免责声明，且主力内核已迁 Rust → 参数落地前必须自行实测验证）

段落内容分四类：**PDF 公式 / 单个原 PDF 字符 / 同样式原 PDF 字符串 / 同样式译文 Unicode 字符串**。
前三类**透明搬运**（原样重发，不重排不重绘），只有第四类需要排版。

- **Algorithm 1（reflow）**：从原段落 bbox 左上角起依次摆放元素，当前行放不下就换行。
- **Algorithm 2（放不下时降级）**：
  1. 行距初值 **1.5**，scale 初值 1.0；
  2. 跑 Algorithm 1；
  3. 放不下 → 行距**每次 -0.1**，下限 **1.4**；
  4. 仍放不下 → scale > 0.6 时每次 **-0.05**，≤ 0.6 时每次 **-0.1**，并把行距**重置为 1.5**；
  5. scale < 0.7 → 最小行距降到 1.1；
  6. scale < 0.1 才报错。
- **Algorithm 3（缩放前先扩框）**：**先尝试沿书写方向扩 bbox**，上限取页裁框宽的 **90%**，
  并检查右侧是否有重叠段落 / 重叠图形；扩框后仍放不下才进入降级。
- **中英混排**：中英交界处加 **0.5 字宽**间距，部分标点排除在这条规则外。
- **首行缩进**：指定时加 **2 个汉字宽**。
- **标点悬挂**：允许部分标点**越过右边距**。
- 引用标准：**Unicode UTR #59 East Asian Spacing**。

**pdf2zh 对应实现**（`pdf2zh/converter.py`）
- `LANG_LINEHEIGHT_MAP`：`zh-cn/zh-tw/zh-hans/zh-hant/zh = 1.4`，`ja = 1.1`，`ko = 1.2`，`en = 1.2`
- 自适应降行距：`while (lidx + 1) * size * line_height > height and line_height >= 1: line_height -= 0.05`
- 换行判定：`x + adv > x1 + 0.1 * size`（0.1 字宽容差）
- 逐字符推进用 `adv`；CJK 用 `noto.char_lengths(ch, size)[0]`
- **每行独立定位**：`y = dy + y - lidx * size * line_height`（**基线递推**，不是框顶递推）
- 译文写进内容流：`gen_op_txt()` → `/F {size} Tf 1 0 0 1 {x} {y} Tm [<hex>] TJ`

**我们当前**
- `CJK_SIZE_RATIO = 0.85`、`LINE_RATIO = 1.35`、`MIN_FS_RATIO = 0.62`、`MIN_FONT_SIZE = 6.0`、
  `FS_STEP = 0.25`、`MAX_BORROW = 3.0`
- `_Sizer.fits()` 用 MuPDF 自己的排版引擎试排（**精确判定**；我们踩过「自己估行数会低估」的坑）
- `_pick_size()` 从 base 字号按步长往下试；**放不下就完全不涂不写，保留英文原样**（避免截断丢信息）

> 🔍 **「质量不太行」的两条根因（已在本地取证，见 §五）**：
> ① `insert_textbox` 按**空格**分词断行 → 中文长句被整体推到下一行 → 行末大片留白；
> ② 译文被**顶格塞进原框** → 行数少但行宽撑满，与周围未翻译的英文行**基线网格对不齐**。
> 真正的中文化排版应该是：**行宽保持一致、行数变少、段末自然留白**（BabelDOC Algorithm 1 的语义）。

### 3.5 公式和图表是怎么保护的？

**(a) 公式判据（可直接移植思路）**
**BabelDOC `babeldoc/format/pdf/document_il/utils/formular_helper.py`** → `is_formulas_start_char()`：
- 字符含 `(cid:` → 是公式
- **当前字体没有该字形** → 是公式（廉价且很灵）
- 命中 `config.formular_char_pattern` 正则
- Unicode category ∈ {`Mn`,`Sk`,`Sm`,`Zl`,`Zp`,`Zs`,`Co`}（`Co` = 私用区，PDF 里常见于数学符号）
- 码位在 `0x370–0x400`（希腊字母）
- 匹配 `[0-9\[\]•]`

`is_formulas_font()`：**三层正则**
1. `precise_formula_font_pattern`：白名单（STIX Math / Cambria Math / Latin Modern Math / TeX Gyre Math / Fira Math / Libertinus Math / IBM Plex Math / Euler …）
2. `pattern_text`：**显式排除**名单（Times / Arial / Calibri / HelveticaNeue / CourierNewPS / GoudyOldStyleT …）
3. `broad_formula_font_pattern`（兜底）：`CM[^RB]`、`(MS|XY|MT|BL|RM|EU|LA|RS)[A-Z]`、`LINE`、`LCIRCLE`、`TeX-`、`.*Mono`、`.*Code`、`.*Sym`、`.*Math`

判定时先 `font_name.split("+")[-1]` 去掉子集前缀。
**pdf2zh `converter.py` → `vflag(font, char)`**：`(cid:` 前缀 / 字体名正则 /
字符 category ∈ {`Lm`,`Mn`,`Sk`,`Sm`,`Zl`,`Zp`,`Zs`} / 希腊字母区 —— 同源思路。

**(b) 公式「透明搬运」（关键差异）**
BabelDOC 的公式**不是重绘、也不是贴图**，而是**原字形原位置重发**：
IL 里公式是 `PdfFormula`（成员是原始 `pdf_character`，带原始 `cid` 与原始字体），
排版时作为**不可拆分的元素整体搬运**。
pdf2zh 同理：`raw_string()` 对原 CID 字体写 `%04x` of `ord(c)`（**原 CID 码**），
配合 `self.fontid[vch.font]` 用**原字体资源**重发 → 公式仍是矢量文字，可选可搜，**不是图片**。

> 📌 **「公式原样保留」≠「把公式抠成图片贴回去」**。正确做法是**根本不擦公式**。

**(c) 图表保护**
- **pdf2zh**：靠 DocLayout-YOLO 掩码标出「保留区」，且**把填充路径整个丢弃** → 图表被降级，**不满足我们红线**。
- **BabelDOC**：图/表作为 `pdf_xobject` 与公式、段落元素并存于 IL，重建时按原几何重发。
- **我们当前**：`_protected_rects(page)` 用 `page.get_image_info()` + `page.get_drawings()` 逐个取 rect。
  - ⚠ **实测缺陷**：一张渐变图在 PDF 里是 **251 条渐变矢量**，逐条取 rect → 保护区被切成
    252 条、每条面积 226（刚好卡在「面积<200 当分隔线跳过」的阈值之上）→
    **保护形同虚设** → 正文的大矩形 redaction 顺带把图盖了。
  - ✅ **PyMuPDF 1.24+ 的 `page.cluster_drawings()` 直接返回矢量图聚类矩形** —— 本机实测存在，
    JACERS 首页把 251 条渐变条聚成 **1 块**，纯正文页聚成 **0 块**（不误伤），有图页精确聚出每张子图。

### 3.6 双栏论文的栏序与跨栏坐标

- **pdf2zh**：`converter.py` 靠 layout 掩码 class 区分段落，
  用 `child.x0 > max(...)` / `abs(child.x0 - xt.x0) > vmax`（`vmax = 页宽/4`）判断「段内换行 vs 新公式块」。
  **它不显式建「栏」对象**，栏序由布局掩码隐式承担。
- **BabelDOC**：`extract_char.py` 的 `_cluster_by_axis()` 在 band 阶段按**垂直重叠**分带，
  再用 DBSCAN 分行；**双栏被误并成一行的情况由 `LINE_SPLIT_SIZE_RATIO_THRESHOLD=1.5` 拆分**。
- **我们当前**：已有 `Block.col` 字段，且 `_next_block_top(blocks, rect, blk.col)` **按栏找同栏下一块**借空间
  （右栏第一块 y0 在页顶，跨栏取会算成负数 —— 这个坑我们已经踩过并修对了，**应保留**）。

---

## 四、可直接落地的改进清单

> 难度：🟢 低 / 🟡 中 / 🔴 高。**所有条目均为独立实现，不复制 AGPL 源码。**

| # | 改什么 | 为什么能提质 | 参考做法 | 难度 |
|---|---|---|---|---|
| **1** | **译文落位改为「逐行基线定位」，不用 `insert_textbox` 自动折行** | `insert_textbox` 从框顶起排、无基线概念，与周围未翻译英文行**基线网格错位** | pdf2zh `converter.py` `gen_op_txt()` 每行 `y = y0 - lidx*size*line_height`；BabelDOC Alg.1 | 🟡 |
| **2** | **落位改成三段式：①原框内放 → ②先沿书写方向扩框（查右侧重叠）→ ③再降行距 → ④再降字号** | 现在是「直接缩字号到 0.62× + 最多借 3pt」，标题/图注被缩得明显变小；先扩框能保住字号 | BabelDOC Alg.2/3 | 🟡 |
| **3** | **不要「填满原框」：允许中文段末自然留白** | 中文比英文短，硬撑满会让每段顶到底、与相邻段挤在一起 | BabelDOC Alg.1「放得下就放」 | 🟡 |
| **4** | **中英混排自动加 0.5 字宽间距** | 「磁致伸缩复合材料Cr₂O₃」这类粘连处很挤 | BabelDOC（中英交界 0.5 字宽）；pdf2zh `text_spacing.py::add_cjk_latin_spacing()`（按 UTR #59 自行重写） | 🟢 |
| **5** | **标点悬挂 + 首行缩进 2 汉字** | 消除「行尾孤零零一个句号」与右侧参差 | BabelDOC（hanging punctuation / first line indent） | 🟢 |
| **6** | **行距 1.35 → 1.4，并允许自适应降到 1.2** | 现在固定 1.35；改成「按块高自适应降行距」比缩字号观感好 | pdf2zh `LANG_LINEHEIGHT_MAP`（zh=1.4） | 🟢 |
| **7** | **图表保护升级：用 `page.cluster_drawings()` 取代逐 drawing 取 rect** | 现在把一张渐变图拆成 251 条小矩形保护 → 保护形同虚设 → **图表被白矩形盖掉** | PyMuPDF `Page.cluster_drawings()`；docling(MIT) 的矢量区域聚类 | 🟢 |
| **8** | **公式判据前置：把命中「公式字体/公式字符」的原页行排除出涂改范围** | 目前只信抽取层的 `⟦FORMULA⟧` 标记，**行内公式**可能被当普通 body 擦掉 | BabelDOC `formular_helper.py`；pdf2zh `vflag()` | 🟡 |
| **9** | **ToUnicode 归一化：启用 Noto 等好字体，同时修正兼容码位** | 现在「Noto 写不对就拒用」→ 只能退到雅黑/黑体 | BabelDOC `pdf_creater.py::apply_normalization()` + `make_tounicode()` | 🟡 |
| **10** | **交替页输出改用 `insert_file` + `move_page`，只对「有译文的页」生成替换页** | 现在逐页 `insert_pdf` 把同一页资源复制两份 | BabelDOC `create_alternating_pages_dual_pdf()`（**与我们输出格式完全一致**） | 🟢 |
| **11** | **同一份坐标源驱动「擦除范围」与「译文落位」** | 现在是「抽取层块 bbox（pdfplumber）」与「PyMuPDF 行 bbox」两套坐标事后对齐 | BabelDOC 的 IL | 🟡 |
| **12** | **放不下时「保留英文 + 记录原因」，并把统计暴露给前端** | 现在行为正确但用户看不到「哪些段没替换」 | 我们自己的 `BuildStats.as_dict()` | 🟢 |
| **13** | **字体子集化显式 `fallback=False` + 超时保护** | 默认参数会把缺字字形补进子集，文件变大 | BabelDOC `subset_fonts_in_subprocess()` | 🟢 |
| **14** | **保存加 `clean=True` 但必须带超时回退** | `clean=True` 去无用对象，但大文件会卡死 | BabelDOC `save_pdf_with_timeout()` | 🟢 |
| **15** | （可选）**评估 `fitz.Story` / `page.insert_htmlbox()` 出文字层** | 自带 CJK 折行与 ToUnicode 生成 | PyMuPDF Story/insert_htmlbox | 🔴 |
| **16** | （可选）**用「横向微调」代替「缩字号」** | 译文比原文长时，`scaleX` 压扁 5–15% 比 9pt→6pt 观感好 | RockTranslate(MIT) | 🟡 |

---

## 五、对我们「替换版 PDF」的结论与推荐路线

### 结论：**继续 PyMuPDF 路线，但要把「redaction 大矩形 + `insert_textbox` 试排」升级为「行级擦除 + 行级基线落位 + 先扩框后缩字号」**

**理由**
1. **我们的差异化资产不能丢**：段落级译文 + 段落几何坐标的快照、PyMuPDF 依赖、
   已验证的 ToUnicode 往返校验、已实现的「原文页+替换页交替」输出格式 —— 都不是现成的。
2. **输出格式已被独立验证可行**：BabelDOC `create_alternating_pages_dual_pdf()`
   与我们是**同一个格式** → 行业认可的形态。
3. **切到 pdf2zh/BabelDOC 全套的成本远大于收益**：AGPL-3.0 传染；需引入 DocLayout-YOLO ONNX；
   需重做抽取与翻译管线；且 pdf2zh 的 `do_f/do_B` 被置空 → **图表被降级重画，直接违反我们的红线**。
4. **「整页重绘成图片」路线不可接受**：文字不可选、不可搜，与需求硬冲突。
5. **zotero-pdf-translate / pot / translate-shell / Translators 均不做 PDF 覆盖** → **不要在这几个方向继续投入**。

**明确不建议**
- ❌ 不引入 DocLayout-YOLO：我们的抽取层已有版面与公式/表格分类能力，投入产出比低，
  且会打破「可离线、无模型依赖」的项目属性。
- ❌ 不把公式「抠成图片贴回」：会让公式失去矢量可选中属性（正确做法是**根本不擦公式**）。
- ❌ 不改用报告式（左原文右译文）布局：用户明确要「原文页 + 替换页交替」。

**风险提示**
- BabelDOC `Typesetting.md` 自带「可能含 AI 生成内容」免责声明，其主力内核已迁 Rust（未逐行读）
  → **Algorithm 1/2/3 的参数落地前必须用真论文实测验证，不能直接照抄**。
- 本次逐行核实的是 **pdf2zh 的 v1 内核**；v2 走 `pdf2zh/kernel/precise.py` → BabelDOC，**v2 未逐行读**。

---

## 六、参考链接

**pdf2zh / PDFMathTranslate**
- https://github.com/PDFMathTranslate/PDFMathTranslate
- 内容流算子过滤 `pdfinterp.py`：`https://raw.githubusercontent.com/PDFMathTranslate/PDFMathTranslate/main/pdf2zh/pdfinterp.py`
- 段落装配与排版 `converter.py`：`.../main/pdf2zh/converter.py`
- 中英混排空格 `text_spacing.py`：`.../main/pdf2zh/text_spacing.py`
- 版面模型 `doclayout.py`：`.../main/pdf2zh/doclayout.py`
- 启发式版面 `ultrafast.py`：`.../main/pdf2zh/ultrafast.py`
- 论文：https://arxiv.org/abs/2507.03009

**BabelDOC**
- https://github.com/funstory-ai/BabelDOC
- 排版算法（核心）：`https://raw.githubusercontent.com/funstory-ai/BabelDOC/main/docs/ImplementationDetails/Typesetting/Typesetting.md`
- PDF 生成：`.../docs/ImplementationDetails/PDFCreation/PDFCreation.md`
- 公式判据：`.../babeldoc/format/pdf/document_il/utils/formular_helper.py`
- 字符聚类：`.../babeldoc/format/pdf/document_il/utils/extract_char.py`
- PDF 创建器：`.../babeldoc/format/pdf/document_il/backend/pdf_creater.py`

**标准与文档**
- Unicode UTR #59 East Asian Spacing：https://www.unicode.org/reports/tr59/
- PyMuPDF：https://pymupdf.readthedocs.io/
