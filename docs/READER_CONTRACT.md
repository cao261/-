# MicroBench 阅读器契约（PDF 原文 + 译文 + 划词答疑）

> 改 `microbench/paper_text.py`（抽取/排序）、`microbench/llm.py`（模型调用）、
> `microbench/static/index.html`（阅读器 UI）之前先读这里。
>
> 同步相关的契约见 [SYNC_CONTRACT.md](SYNC_CONTRACT.md)。

---

## 1. 页内阅读顺序：一页内由左至右

**用户诉求（2026-10-23）**："一页中就是从左到右翻译，一直都是从左到右"。

### 1.1 顺序规则

最终送进模型的页面文本，顺序必须是：

```
整宽块（页眉/标题/整宽图注）
  → 左栏正文（从上到下，整段读完）
  → 右栏正文（从上到下，整段读完）
  → 跨栏单元（整宽表格，整块保留）
```

**绝不能左右交替**（左1→右1→左2→右2）—— 译文会左右横跳。

### 1.2 关键坑：左右两栏的起始高度可能不同

`JACERS 综述第 3 页` 是典型：右栏图注从 `y=227` 就开始，左栏正文要到 `y=450` 才开始
（页顶被一张大图占住了左栏）。

旧实现把"双栏正文起点（`y_split`）以上"**整宽裁一刀当页眉**，于是右栏那半张页面
被抢到最前面，左栏正文反而排后面 —— 用户看到的正是"先翻译右边的了"。

**现在**：顶部区域也用同一套"按带找栏缝"的判据分栏（`_extract_top_region`）：
- 真的双栏的带 → 左右分开，各自归位
- 整宽的带（页眉/标题/整宽图注）→ 单独成块，仍排最前
- 只有一栏有字的带 → 归那一栏

### 1.3 改这块时必须跑

```bash
python -m pytest tests/test_column_order.py -v
```

该文件锁死了：左栏整段早于右栏整段、跨栏表格整块保留且在正文之后、
不对称双栏页（`JACERS` 第 3 页）的顺序契约。

**全库回归**（可选，改动较大时跑）：

```python
# 24 篇 250 页：0 报错，123 个双栏页分栏归属全部干净
#   （仅 8 处是公式上标碎片页，单字符无法判定栏归属，非真回退）
```

---

## 2. 划词答疑：为什么会有"一直转圈然后什么都没有"

### 2.1 根因

**MiniMax-M3 的思考 token 和正文共用同一个 `max_tokens` 预算池。**

实测（真实 API，17600 字上下文 + `max_tokens=4000`，即答疑面板的原始配置）：

| 模式 | 结果 |
|---|---|
| 默认（开思考） | 思考烧掉 4000 token，**正文只剩 2 个字符**（两个换行） |
| `thinking={"type":"disabled"}` | 立刻返回 **8238 字符**完整答案 |

### 2.2 修复：三处必须同时改

任一处漏掉都不生效（我在这里栽了两次）：

**a) 空正文检测按非空白字符计数**

```python
body_visible_chars += len(x.strip())   # 不能是 len(x)
```
两个换行会被当成"有内容"，守卫永远不触发。

**b) 守卫必须放在 SSE 循环 _之后_**

实测：思考烧光时上游发完 `finish_reason="length"` 就**直接断流，
根本不发 `[DONE]` 哨兵**。

守卫只写在 `if data == "[DONE]"` 分支里 → 永远触发不了。

**c) `emitted` 按非空白字符判定**

```python
if chunk.strip():
    emitted = True
```
空正文时那两个换行已经被 yield 出去了，`emitted=True` 会让
"已输出内容就别降级重试"的分支直接抛出。

### 2.3 现在的行为

正文为空 → 自动用 `thinking=disabled` 重试一次 → 用户拿到完整答案。
（同步路径 `chat()` 早就有类似机制，流式路径此前缺失。）

---

## 3. 表格/曲线图：截图直接给 M3（多模态）

**用户原话**："minimaxm3 是多模态模型，如果表或者图不好文字化给他的话，
完全可以截图给它，让它回答问题"。

### 3.1 为什么需要

表格和曲线图被 pdfplumber 抽出来只是碎片（`"2 4 3"` 这种上下标、坐标轴刻度），
模型看到一堆没意义的字符，自然答不上来。

### 3.2 链路

| 环节 | 位置 | 说明 |
|---|---|---|
| 渲染 | `paper_text.render_page_png()` | PyMuPDF（`fitz`），`zoom=2.0` ≈ 144 DPI，带总像素上限 |
| 采集 | `index.html` `_captureSelectionPageImage()` | 直接裁剪**已渲染好的** PDF.js 画布（划选矩形包围盒 + 外扩），不额外请求服务端 |
| 传输 | `app.py` `ExplainSelectionRequest.page_image` | base64 PNG（不含 data URL 前缀） |
| 组装 | `app.py` `explain_selection_stream` | OpenAI 兼容 content 数组：`[{"type":"text"}, {"type":"image_url"}]` |

### 3.3 约束

- 截图失败**自动降级**为纯文本答疑，不挡路（前端 `return null` + 后端只看有没有 `page_image`）
- `fitz` 本机已装，**不需要新依赖**
- 模型侧：M3 实测支持 `image_url` + `data:image/png;base64,...`（HTTP 200）

### 3.4 实测效果

问"Fig.3 曲线峰值大约在 H 等于多少 Oe、峰值多少 mV/cm-Oe"，
模型读图答出 **约 60 Oe / 约 450 mV/cm-Oe** —— 与论文 Fig.3 实际吻合。

---

## 4. 公式：不猜 LaTeX，直接裁论文原图

**用户原话（2026-10-24）**："公式渲染还有问题……基本没有和文中的公式一样的……
分子分母分不清谁是谁……反正这个公式就还没有参考意义"。

### 4.1 为什么不能拼 LaTeX

公式在 PDF 里是矢量排版（分子一行、分母一行、上下标各一行），文本层抽出来
是乱序碎片。照碎片拼 LaTeX 极容易把**分子分母装反** —— 而错公式比没公式
危险得多：用户会照着它去算（本项目的铁律，见 AGENTS.md §4.6）。

### 4.2 现在的做法：只记坐标，裁原图

| 环节 | 位置 | 说明 |
|---|---|---|
| 识别 | `paper_text._formula_spans()` | 连续的「数学行」且含等号 → 一个公式块 |
| 记坐标 | `paper_text._lines_to_text()` | 输出 `⟦FORMULA:x0,y0,x1,y1⟧`（PDF 页坐标，pt） |
| 穿过模型 | `app.py` 提示词第 13 条 | 要求模型**逐字照抄**标记（连坐标一起） |
| 清洗 | `translation.sanitize_translation()` | 坐标坏了（数量不对/不成比例）→ 退回文字提示 |
| 裁图 | `app.py` `GET /api/paper/formula_image` | PyMuPDF 按坐标裁 PNG，`scale=4`（≈288 DPI） |
| 显示 | `index.html` `renderFormulaImages()` | 标记 → `<img class="formula-shot-img">`，失败退回文字 |

实测（JACERS 综述第 2 页 eq (2)）：裁出来的就是论文里那个分式，
**分子 `n d₃₃ᵐ g₃₁ᵖ`、分母 `n s₁₁ᵖ(1−(k₃₁ᵖ)²) − s₃₃ᵐ(1−n)` 一模一样**。

### 4.3 判据（改动时必须一起改）

一条行被当成「数学行」的条件：

1. **词状 token < 2 个**（≥3 字母的纯字母词，`(cid:N)` 不算）
   → 正文优先：`are assumed: α = 0.05, …` 有 3 个词状 token → 是正文；
   `eeﬀ ¼ 33 33 (3)` 只有 1 个 → 是公式。
2. 含关系符号（`= ≈ ≤ ≥`，PDF 里常被抽成 `¼`）
   → **必须有等号**：表格行也含大量 `(cid:)` 碎片，但表格行没有等号 ——
   没有这一条，整张表会变成一张图，用户就没法翻译表里的内容了。
3. 块高 ≤ 90pt（超高的多半是表格）。

⚠️ **正文绝不能被吞进公式图**（实测踩过：IEEE 论文
`f(ω) = … Substituting the inverse` 整行被吞 → 译文里这句话直接消失）。
判据放宽前先跑 `tests/test_reader_formula_table.py`。

### 4.4 改这块时必须跑

```bash
python -m pytest tests/test_reader_formula_table.py -v
```

---

## 5. 表格：Markdown 表格必须渲染成真正的 `<table>`

**用户原话（2026-10-24）**："表格的翻译，他都是很乱非常之乱……
他这个原有的表格形式完全看不出来，而且占的篇幅非常大"。

模型本来就把表格输出成 `| 性能 | BaTiO₃ | … |` 的 **Markdown 表格**，
但渲染层没有表格支持 → 用户看到的是一堆竖线加换行。

- 渲染：`index.html` `formatMarkdownLite()` 里的 `_matchMarkdownTable()`
  （表头 + `|---|---|` 分隔行 → `<table class="md-table">`）
- 样式：`index.html` `.md-table`（单元格 `nowrap` + 横向滚动 → 纵向占位大幅缩小）
- 提示词：`app.py` 第 14/15 条 —— 表格用 Markdown 表格输出，量纲单位保持原样
- 护栏：`microbench/_test_reader_markdown.js`（node 跑真实前端代码）
  + `tests/test_reader_formula_table.py` 里的一条 pytest 包装

---

## 6. 翻译快照必须带「抽取算法版本号」

**踩坑（2026-10-24）**：10-23 14:17 存的快照被 10-23 16:36 的栏序修复甩在
后面 → 读者照旧恢复旧快照 → 用户看到的还是"先翻译右边那栏"，
**还以为修复没生效**（其实代码早就修好了）。

- `paper_text.EXTRACTOR_VERSION`：改了「页内顺序 / 公式 / 表格抽取」就 +1
- 快照保存时由服务端盖上 `extractor_version`
- 加载时（`GET /api/pdf/snapshot/load`）版本对不上 → **整份作废**，
  返回空页列表 → 阅读器照常把没翻的页重翻一遍
- 前端会弹一条提示："♻️ 旧版译文已失效（抽取算法已升级…），正在重新翻译"

---

## 7. 答疑面板最小化 = 收成侧边栏

**用户原话**："划线解答有点问题，最好点最小化能够缩小至侧边栏"。

- 旧实现：只把高度压到 46px，**宽度仍是 460px** —— 屏幕上仍占一大块挡着 PDF
- 现在：`.qa-collapsed` → 44px 宽的窄竖条，标题竖排，贴着面板所在那一侧的屏幕边缘
- 点标题栏即可展开（竖条上不方便点小按钮），位置/尺寸还原到收起前

---

## 8. 改完必跑

```bash
python -m pytest tests -q
node microbench/_test_reader_markdown.js
```

- `tests/test_column_order.py` —— 页内顺序契约
- `tests/test_qa_fixes.py` —— 空正文检测 / 多模态 / 侧边栏契约
- `tests/test_reader_formula_table.py` —— 公式坐标 / 表格渲染 / 快照版本化

仓库有 pre-commit 钩子，提交时会自动跑离线测试套件。
