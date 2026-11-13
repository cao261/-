# 交接 · 平板「圈选问 AI」+ 全版本「译文按原文排版」（2026-11-12）

> **这轮做了什么**
> ① 平板端：手写笔圈一块 → 直接问 AI / 一键总结这块（**已落地，可交付**）
> ② 全版本：译文排版跟原文一模一样（沉浸式翻译那种）——
>    **已全链路落地**（抽取层段落坐标 → 段落级翻译 → 两端对照视图 → 快照同步）
>
> **还没做的**：② 的**图片/图表**在对照视图里看不到（不在 PDF 文本层里，
> 切回「原文」视图即可）—— 见 §3.7。
> 动手前先读 §3.5 的三条硬约束，别重走我踩过的路。

---

## 0. 一句话结论

**两件事都能做，而且都做完了。**

| 需求 | 能不能做 | 现在到哪一步 |
|---|---|---|
| 平板上用手写笔圈一段，直接问 AI / 总结这段 | ✅ 能 | **已完成并测过**，见 §2 |
| 所有版本的译文排版跟原文一模一样（双栏论文的译文也分左右栏） | ✅ 能 | **已完成并测过**，见 §3（桌面端 + 平板端都有「对照」开关） |
| 对照视图里公式/表格要**正常显示**（不是 LaTeX 源码） | ✅ 能 | **已完成并测过**：整块公式贴 PDF 原图、表格出真表、行内公式走 KaTeX（§3.11） |

> 第二件事**不是**"把整页译文换个 CSS 排一下"就能做到的 ——
> 整页译文是**流式**的，它压根不知道"第几段对应原文哪个位置"。
> 要同位渲染，抽取层必须先给出段落坐标。这一步本轮完成（§3.2）。

---

## 1. 为什么"沉浸式翻译那种排版"在 MicroBench 里不是顺手能做

沉浸式翻译的 PDF 双语：它拿的是 **PDF 的文字层坐标**，每段译文贴在原段落的位置上。
它用的是**机翻**（DeepL/Google）—— 用户原话："他们那个翻译不是大模型的翻译吧，
就应该是机翻，感觉就是很粗糙"。也就是说：**排版能抄，翻译质量不能抄**。

MicroBench 现在的译文是**整页一次翻译**（`app.py` 的 `/api/paper/translate_page`），
产物是一整段 Markdown 字符串。右栏译文栏是「左 PDF 页 / 右译文流」两栏
（`index.html` 的 `#pdf-pages-list` / `#pdf-translations-list`），
§4.3 早就记过：**滚动联动是治标不是治本**，译文是流式的，跟 PDF 版面不可能逐像素对齐。

所以要"排版一模一样"，必须做三件事：

1. **抽取层给出段落坐标**（每段：bbox + 属于哪一栏）—— ✅ 本轮完成
2. **翻译变成段落级**（每段一段译文，且能对上号）—— ✅ 本轮完成
3. **前端按原文的分栏重排译文**（左栏段落进左栏、右栏进右栏）—— ✅ 本轮完成

⚠ 第 3 条**不是**"绝对定位叠在 canvas 上"—— 那条路是死的，见 §3.7。

---

## 2. 平板端「圈选问 AI」（已完成）

### 2.1 改之前是什么样（为什么"白圈"）

平板端本来就有手写层（`.tb-pdf-ink`，归一化坐标存储，跟批注共享同一个文件，
换设备、换缩放都不会错位），也有「✍ 带上我的手写问 AI」按钮。
但那个按钮发出去的是**整页图**（`captureInkPage()`：整页压到 1200px 宽）——
圈的那一小块在整页里又小又糊，模型基本看不清。**圈了等于没圈。**

### 2.2 现在是什么样

| 用户动作 | 结果 |
|---|---|
| 开「手写」→ 用笔圈一块 → 松手 | 圈的上方浮出操作条：**✍ 问这块 / 总结这块 / 清除** |
| 点「✍ 问这块」 | 裁出圈的那一块（带笔迹 + 高亮框）→ 打开 AI 面板 → 输入框聚焦 → 提问自动带上这块图 |
| 点「总结这块」 | 直接发问："请总结我圈出来的这部分内容：它在讲什么、关键结论是什么、有没有值得警惕的前提。" |
| 点「清除」 | 清掉本页笔迹（不动批注） |
| 走 AI 面板里的「✍ 带上我的手写问 AI」 | **现在也优先发圈选区域**（没有圈选才退回整页图） |

### 2.3 关键实现点（改这块之前先读）

- **裁图用源画布自己的像素密度**：
  原文 canvas 与笔迹 canvas 的**像素尺寸不一定相同**（笔迹层按自己的
  `getBoundingClientRect()` 量出来），两段各用各的尺寸换算 ——
  假设 1:1 就会让圈线跑到别处。
- **放大有上限 2.5×**：放大**不增加细节**，只让糊更糊。目标是"看得清"，不是"越大越好"。
- **浮条必须挂在 `.tb-pdf-orig` 里**（不是 `.tb-pdf-page`）：
  宽屏是「原文 + 译文」并排，**页卡的原点跟原文画布不是同一个** ——
  挂错容器，按钮会跑到译文那一栏上面去。
  （这条用 headless Edge 真渲染验过，见 §5）
- **浮条要盖在手写层上面**：手写层 `.is-on` 是 `pointer-events:auto` 且 `inset:0`，
  铺满整页。浮条 DOM 顺序排在它**之后** + `z-index:40` → 点它先命中它，
  不会被当成"又画一笔"。
- **带图提问时不再塞 4000 字笔记正文**：用户圈的是 PDF 上的一块，
  模型该看的是那张图；掺一大段无关正文只会把注意力拉走（还白烧 token）。

### 2.4 改动的文件

| 文件 | 改了什么 |
|---|---|
| `microbench/static/tablet/app.js` | `inkBBox` / `captureInkRegion` / `currentInkRegion` / `showRegionBar` / `askRegion` / `summarizeRegion` / `clearRegionInk`；`runAI` 支持 `opts.question` 与 `state.pendingRegionImage` |
| `microbench/static/tablet/styles.css` | `.tb-pdf-region` 浮条样式 |
| `microbench/_test_tablet_region_ask.js` | 静态断言 34 条 |
| `microbench/_test_tablet_region_render.py` | **headless Edge 真渲染**护栏 8 条 |
| `tests/test_tablet_frontend.py` / `tests/test_pdf_layout_guardrails.py` | 把上面两套挂进 pytest |

---

## 3. 全版本「译文按原文排版」（方案 + 已完成的地基）

### 3.1 最终形态长什么样

- PDF 页照旧渲染；
- 每个原文段落的位置上，**叠**一段中文译文（字号按原文字号缩放，可切换
  「原文 / 双语 / 只译文」）；
- 双栏论文：左栏段落的译文回左栏，右栏的回右栏；
- 公式块的位置贴**公式截图**（已有的 `⟦FORMULA:x0,y0,x1,y1⟧` 机制）；
- 表格块的位置贴**多模态抄出来的 Markdown 表格**（已有的 `⟦TABLE:…⟧` 机制）。

### 3.2 阶段 0/1（本轮已完成）：抽取层给出段落坐标 ✅

新增 `microbench/paper_text.py`：

```
_group_lines_into_blocks(lines, col)   → [{kind, col, split, bbox, text}]
extract_page_blocks(pdf_path, page_no) → [{i, kind, col, bbox, text}]
```

- `bbox` = `[x0, top, x1, bottom]`，**页面绝对坐标（pt，左上角原点）**
  —— 与 `⟦FORMULA:…⟧` / `⟦TABLE:…⟧` 标记**同一套坐标系**，前端可直接拿来定位；
- `col` = `"L"` 左栏 / `"R"` 右栏 / `"S"` 整宽或单栏；
- `kind` = `body` / `formula` / `table`。

**关键事实：行级 bbox 早就算好了**（`_crop_lines` 返回 `x0/x1/top/bottom`），
只是在函数返回时被丢掉 —— 本轮把它捡回来，**没有二次解析 PDF**。

**实测结果**（JACERS 综述第 3 页，真双栏）：10 块，左右栏分得干干净净，
公式块单独认出，见 `logs/blockdiag/*.png`（蓝=左栏，绿=右栏，橙=整宽）。

**已经能用的入口**：
- `paper_text.extract_page_blocks(pdf_path, page_no)` —— 直接调
- `GET /api/paper/page_blocks?relative_path=…&page=3` —— 前端用这个
  （只读、路径走 `_safe_path` 闸门；抽取丢线程池，不占事件循环）

### 3.3 段落切分的判据（实测出来的，不是猜的）

| 判据 | 值 | 为什么 |
|---|---|---|
| 段落起点 = 首行缩进 | ≥ 3pt | IEEE 风格靠缩进分段 |
| 段落起点 = 行间距 | ≥ 5pt | Elsevier 风格靠空行分段 |
| 两个信号取「或」 | — | 单用缩进漏掉 Elsevier，单用行距漏掉 IEEE |
| 栏左边界 | 各行 x0 的**中位数** | 用 min 会被异常行带偏（实测有 268pt 的假缩进） |
| 缩进 > 40pt 的行 | 视为**行尾碎片**，并进当前段落 | 上标 "2 4" 的 x0 在行尾（缩进 225pt），不处理会把一段话切成两半，且两块 y 重叠 |
| 又窄又高的行（h>40 且 h>2w） | **竖排/水印**，单独成块 | 期刊页边的下载水印高 568pt → 并进段落会把整段的 bbox 撑成巨型竖条 |
| 上标/下标独立成行 | 用 `_mark_attached_lines` 并回父行 | 实测 JACERS 第 2 页左栏 77 行里 14 行是这种孤儿行 |
| 公式块 | `_formula_spans` | 已有判据，别重写 |

**单栏论文**（抽不到栏缝）：整页作一栏切段落（`col="S"`）。
⚠ 裁剪范围是**整页**而不是从 `y_split` 起 —— 单栏页上的 `y_split`
是「双栏正文起点」，在纯图页上它落在**图注下方**（实测 ACS 大图页
`y_split=720.8`，图注在 640~700）→ 从 y_split 起裁会把整段图注丢掉。

**已知局限**（下一阶段要处理）：
- 整页是图、只有底部一条两栏文字的页面（ACS 大图页）：底部的左右两栏
  会被并成**一个宽块**（该页的栏缝太短，投票法探不到）→ 译文位置会横跨两栏。
- 章节标题（"(3) Ceramic and Terfenol-D…"）目前和它下面的正文**同块**
  → 对照模式下标题的译文会跟正文挤在一起。
  → 下一阶段用 `HEADING_NUMERIC_RE` / `HEADING_ROMAN_RE` 把标题单独切出来。

### 3.4 阶段 2：只读叠加层（下一步，风险中）

给桌面端 / 平板端各加一个**默认关闭**的「对照模式」开关：
在 PDF canvas 上叠一层绝对定位的段落框（`left = x0*scale`, `top = top*scale`）。

- 坐标直接可用：pdfplumber 的 `top` 与 PDF.js viewport 的 y **同为自上而下**
  → 不用翻转 y（与 `_pdf_clip_png` 同一坐标系，已在公式裁图上验证过）；
- 先只画**原文段落框**（不含译文），肉眼验收"左栏框在左栏、右栏框在右栏"；
- 这一步**不碰翻译**，零数据风险。

### 3.5 阶段 3：段落级翻译 + 回填（风险高，但有现成套路）

**协议照抄 `⟦TABLE_n⟧`**（`app.py` 的 `_prepare_tables`）：整页一次翻译，
但要求模型按段输出带编号的译文，例如：

```
⟦B7⟧这一段的中文译文⟦/B7⟧
```

- 调用次数**不变**（还是每页一次）→ 不撞 RPM 限流；
- 服务端按编号回填 → 漏段有现成兜底（`⟦TABLE_n⟧` 那套"漏了追加到末尾"）；
- 前端按 `i` 找到块 → 把译文贴到 `bbox` 上。

⚠ **三条硬约束（我踩过 / 差点踩）**：

1. **不要动 `EXTRACTOR_VERSION`**（`paper_text.py`）。
   它一动 → **用户全部译文作废重翻**，而且跨设备同步会波及另一台。
   段落级译文必须**独立存储 + 独立版本闸门**（新字段 `blocks_version`，
   或者单独一个 `bilingual/<key>.json`）。
   → 本轮加段落块**没有动它**：A/B 比对 **77 页逐字一致**（`tests/test_page_blocks.py`），
   所以旧译文全部保留。
2. **不要往 `pdf_cache` 的键里加"段落块版本"维度**（`pdf_cache.py` 明确警告）：
   会让同一份 PDF 被两个调用点算出不同键 → 缓存**永远命中不了**（比撞键更糟）。
   段落块并进缓存时，只动 `EXTRACT_IMPL_REV`（2→3）：只作废磁盘缓存（27 篇 ≈ 72s），
   **译文不丢**。
3. **前端不能按字符偏移对齐原文↔译文**：
   `_mark_broken_formulas` 是文本级后处理，会**删掉**公式碎片行
   → 段落块里的原文比页级文本多几行 → 按偏移对齐必然错位。
   → 只能靠 `⟦B7⟧` 编号协议。

### 3.7 阶段 2/3/4 **已落地**（2026-11-12 第二轮）

这一节记的是**实际怎么做的**，比上面的计划更准（计划里"叠在 canvas 上"
那条路已经被否掉了，理由见下）。

**① 后端**
- `microbench/bilingual.py`（新）：段落级翻译的协议 / 解析 / 兜底 / 版本闸门；
- `POST /api/paper/translate_blocks`：一次 LLM 调用翻整页，按 `⟦T7⟧` 编号回填；
- `GET /api/paper/page_blocks`：多返回一个 `blocks_version`（前端比对用）；
- 快照每页多两个字段：`blocks`（`{i,kind,col,bbox,translation}`）+
  `blocks_version`（**独立**于 `EXTRACTOR_VERSION`）。

**② 桌面端**（`microbench/static/index.html`）
- 工具栏多一个「🔀 对照」按钮（`#pdf-bilingual-toggle`）；
- `renderBilingualPage()`：整宽块（标题/跨栏表）在上，
  下面 **左栏 / 右栏并排**，块内是「原文小字 + 译文」；
- 对照模式下**翻译队列改走** `translate_blocks`（不额外花钱，
  服务端同时把段落译文拼成整页译文给右栏用）；
- 缩放重绘会把段落数据一起带走（`prevPageState`）—— 不然缩放一次就得重翻一遍。

**③ 平板端**（`microbench/static/tablet/app.js` + `styles.css`）
- 布局多一档「对照」（`LAYOUTS` 里加 `["bilingual","对照"]`）；
- `renderBilingualInto()`：同样左栏/右栏分列（窄屏上下堆叠，但顺序与原文一致）；
- 段落数据从**同步过来的快照**里读（`pages[].blocks`）——
  平板访问不到本地后端，这是唯一通道。

**④ 验收**
- `tests/test_bilingual.py`：28 条（协议 / 版本闸门 / 快照往返 / 前端接线）；
- `microbench/_test_desktop_bilingual_render.py`：17 条真渲染断言；
- `microbench/_test_tablet_bilingual_render.py`：12 条真渲染断言；
- 两个渲染护栏都**从源文件里抽真函数**去跑（不是抄一遍）。

### 3.8 ⚠ 为什么**不能**把译文叠回 canvas（这一条最值钱）

第一反应是"按 bbox 绝对定位贴回 PDF canvas"—— **这条路是死的**：

- PDF 页在阅读器里是**按栏宽自适应**渲染的（612pt 的页塞进 ~420px 宽的栏
  → 缩放倍率约 **0.69**）→ 原文正文只有 **~6px 高**；
- 译文贴回原位也是 6px → **根本读不了**；
- 译文比原文长 → 盒子还会压到下一个段落上。

→ 对照视图改成「**按原文的分栏重排**」：左栏段落进左栏、右栏段落进右栏，
  字号恢复正常。代价：**图片/图表不在文本层 → 对照视图里看不到**
  （切回「原文」视图即可）。这个取舍要跟用户讲清楚。

### 3.9 章节标题要和正文**分成两块**

对照模式是「一个原文段落 → 一个译文框」。标题和下面的正文挤在一块 →
译文框会把标题译文和第一段正文译文糊成一坨。

判据（在**块层**切，页级文本一个字都不变 → 不需要动 `EXTRACTOR_VERSION`）：
编号前缀 + 短 + 不以句末标点结尾 + **词数 ≤ 8** + 无点线 + 无 `et al` +
**最多两行**。全库 25 篇 / 261 页扫过：误伤从 193 降到 137，
剩下的全是参考文献条目和图内坐标轴标签（本来就是独立块，不影响文字）。

### 3.10 阶段 5（可选）：并进抽取缓存

`extract_page_blocks` 现在是**独立抽取**（自己开一次 pdfplumber，单页 64~332ms）。
并进 `pdf_cache` 后零额外成本：
- `extract_text_from_pdf` 的 page dict 加 `"blocks"`；
- `pdf_cache.write_cache` 原样 dump → **零改动即持久化**；
- `_valid_pages` 只校验 `p["text"]` 是 str → 新代码要容忍没有 `blocks` 的旧缓存。

### 3.11 平板端在**本地**裁公式原图（`getViewport` 的 offset 方案）

对照视图里「整块公式」在桌面端是 `GET /api/paper/formula_image` 服务端裁的
（PyMuPDF 按 `⟦FORMULA:x0,y0,x1,y1⟧` 坐标裁 PNG），**平板端调不到这个接口**
（平板三处都拦着访问不到 `/api/...` 的 PDF/图片通道）→ 只能**本地裁**。

做法：PDF.js 3.11 的 `PageViewport`（`rotation=0` 时）：

```
transform = [s, 0, 0, -s, 0, H*s]
bbox（PDF 点，相对 CropBox 左上角）→ 用户空间：
    user_x = view[0] + bbox_x
    user_y = view[3] - bbox_y
裁法：
    page.getViewport({ scale: 4, offsetX: -user_x, offsetY: -user_y })
    → 画布尺寸 = bbox 宽高 × scale（≈288 DPI，与桌面端同倍率）
```

**必须用 `getViewport({offsetX, offsetY})`，不能用 `render({transform:[1,0,0,1,-x,-y]})`**：
后者会把整页画到小画布外（裁出空白），而且 canvas 小于 viewport 时
`render().promise` **永不 resolve** → 页面永远转圈（§6 坑 #18）。

坐标是 **PDF 点**，要经 `viewport.transform` 换算到**画布像素**；
`page.rotate != 0` 的旋转页坐标系对不上 → **直接不裁**（宁可不裁也不裁错）。

裁好的图按 `page + 公式序号` 缓存进 `pdfState.formulaShots`，
译文栏里用 `.tb-formula-shot` 占位（带 `data-formula-page` / `data-formula-key`），
裁完由 `paintFormulaShots` 回填 `<img>`；裁失败则退回「【公式未能完整提取…】」提示。

**护栏**：`microbench/_test_tablet_formula_shot.py` —— 平板本地裁的图 vs
电脑端 PyMuPDF 裁的图，**逐像素比对**（尺寸 / 暗像素中心 / 掩膜 IoU）。
实测 JACERS 第 3 页：两边都是 **894×104**，中心偏差 Δx=0.0 / Δy=0.5，IoU=0.833。

### 3.12 模型**听话但会忘写闭标记** → 解析器要能兜

段落协议要求模型回 `⟦T0⟧…⟦/T0⟧`。实测 JACERS 第 2 页：模型**编号全写对**
（T0/T1/T2/T4/T5…/T13，连跳号都对），但**一个 `⟦/Tn⟧` 都没写** →
旧正则一段都解析不到 → 整页 502「模型没有按段落编号返回译文」。

> 这是**模型听话但少写一个符号**，不是模型乱来。把它判死 → 用户整页没译文。

改成**两段式解析**（`microbench/bilingual.py: parse_block_translations`）：

1. **严格**：有闭标记就按 `⟦T0⟧…⟦/T0⟧` 配对；
2. **兜底**：严格模式解析不出来时，**按下一个开标记切段**
   （段内若有闭标记就截到闭标记为止）。

段数仍然对不上时**照样 502** —— 宁可整页没有译文，也不要把译文贴到错的段落上。

> ⚠ `_BLOCK_CLOSE_RE` 必须写成 `r"⟦/T\d{1,3}⟧"`（**带 `T`**）。
> 漏掉 `T` → 闭标记截不断 → 兜底切段也会错位。

**行折也要一起处理**：块文本用**空格**拼行（抽取层，`paper_text.py`），
解析后再把模型输出里的**单个换行转成空格**（`_flatten_inline_newlines`，
双换行保留）—— 两处缺一不可，否则译文里下标会被拆成单独一行。

### 3.13 遗留三项的处理（2026-11-13）

| 遗留 | 结论 | 做法 |
|---|---|---|
| ① 纯图页底部两栏被并成宽块 | **当前语料库里已不复现**（ACS 大图页的 L/R 分栏是对的）；根因路径仍在，留了**块层兜底**（加固后**全库 429 页一次都不触发**） | 见 §3.13.1 |
| ② 对照视图里看不到图片/图表 | **已修**（两端都有「🖼 原页图」开关） | 见 §3.13.2 |
| ③ 表格脚注原文乱码 | **已修**（不是上游问题，是「PDF 里没有空格字符」） | 见 §3.13.3 |

**本轮验收数字**（2026-11-13 实测）：

| 项 | 结果 |
|---|---|
| 页级文本 A/B（红线） | **一致 77 / 不一致 0** → `EXTRACTOR_VERSION` 保持 **5**，用户译文一句都不作废 |
| 段落块 A/B（全库） | 一致 **416** 页 / 变了 **13** 页 —— **13 页块数全部不变**，只变可读性（空格修复） |
| 空格修复不变式（全库） | 修复 **22** 行 / **字符被改动 0** 行 |
| 全量 pytest | **806 passed / 1 skipped / 12 deselected** |
| 五套渲染护栏 | 25 / 18 / 6 / 8 / 36 —— **全 PASS** |
| 交付验收 | `_verify_delivery.py` 全部通过；APK 签名证书指纹**未变**（可直接覆盖安装） |

#### 3.13.1 单栏页底部两栏的块层兜底（`_find_bottom_column_split`）

大图页的两栏正文条太短 → 逐带投票（`_vote_column_gap` 要求 ≥3 票）探不到 →
整页被判成单栏 → 底部两栏被并成一个宽块。
兜底：单栏分支里**只针对块层**再找一次底部栏缝，认出来就把那一段拆成 L/R。

⚠⚠ **护栏比判据更重要**：「底部中间有条缝」**不等于**「底部是两栏正文」——
单栏论文底部一张**宽表格**也有缝（那是表格的列分隔线）。
第一版按「有缝 + 词够多」去拆 → **3 页被误伤**（表格行被劈成两半）；
第二版加了「够词的每一带都像正文」（`_looks_like_body_text`）→
仍然误伤这 3 页，因为**「词数不够的带不参与否决」这条逃生口让所有带都逃过了否决**
（实测三页的带全是「4~7 / 7~20」这种左右极不均衡的分布）。

→ **最终版三道硬约束**（缺一不可）：

| # | 约束 | 挡的是谁 | 实测 |
|---|---|---|---|
| ① | 投票的带必须**左右各 ≥8 个词**（`_band_words_both_sides`） | 宽表格的行（词摊在整页上，某一头只有几个词） | 三页全部被挡 |
| ② | 栏缝必须**跨 ≥3 个带** | 宽表格每行「最宽空隙」位置随机，最多偶然撞 2 次 | 三页的最高票都只有 2 |
| ③ | 分栏起点以下、**够词的每一带都必须像正文** | 表格行（词短、数字多、没有英文单词） | 兜底 |

护栏：`tests/test_block_layer_fixes.py` 里用**三个真实样本页**
（`Review of Magnetoelectric Sensors` 第 11 页 /
`Magnetoelectric Magnetic Field Sensors A Review` 第 16 页 /
`Current Status of Magnetoelectric Composite Thin Thick Films` 第 6 页）
做回归 —— 这三页曾被误伤过，以后再改判定逻辑会立刻红。

⚠ 只动块层：页级文本走 plain 分支（整页）→ 一个字都不动 → `EXTRACTOR_VERSION` 不动。

#### 3.13.2 「🖼 原页图」开关（对照模式里同时显示原页）

图片/图表**不在 PDF 文本层里** → 译文重排后页面上只剩文字。
**不要**想着把图抽出来插进译文栏（矢量图抽不准，且与「重排」的前提冲突）。

正解：**原页 canvas 不藏了**。
桌面端 `microbench/static/index.html`：工具栏加「🖼 原页图」→
`_showBilingualVisibility` 在 `bilingualOrig` 打开时不再藏 canvas，
页卡加 `.bilingual-orig` → **并排还是上下堆叠由 JS 按实际宽度决定**（见 §3.13.4）。
平板端 `microbench/static/tablet/app.js` + `styles.css`：
页卡加 `.show-orig` → `.lay-bilingual.show-orig .tb-pdf-orig{display:block}`。
零额外请求（canvas 本来就渲染好了）。

护栏：桌面 `_test_desktop_bilingual_render.py`（25 条，量真实坐标：
原页图 left=233 / 译文 left=767，横向不重叠）、
平板 `_test_tablet_bilingual_render.py`（18 条，量 `getComputedStyle(display)`）。

#### 3.13.3 表格脚注的「粘行」：PDF 里没有空格字符

实测 JACERS 综述第 2 页的表格脚注原文是
`Whered 31andd 33arepiezoelectricconstants;e,dielectricconstant;…`。

**根因不是「上游 PDF 有问题」**：那行字符是**逐个精确摆放**的
（字母之间间隙 = 0），只有词与词之间有 1.33~1.48pt 的间隙；
pdfplumber 用**固定** `x_tolerance=1.5` 切词 → 词边界被判成「字母间距」→ 粘住。

修法（**只在块层**）：`_crop_lines` 改成 `return_chars=True` 拿原始字符，
`_heal_line_spaces` 按位置间隙重新切词；阈值**自适应**（把这一行所有正的间隙
当候选，每个都切一遍，用「切出来的常见英文词有多少」打分，取分最高的）。

修完：`Where d 31 and d 33 are piezoelectric constants; e, dielectric constant; …`

⚠ 页级文本走 `_lines_to_text`（用 `line["text"]`）→ **一个字都不变**。

##### 3.13.3.1 ⚠ 连字（U+FB01 `ﬁ`）会让空格修复「凭空多出一个 fi」

第一版修完，实测又抓到 4 行坏词：
`coefficients` → `coeffificients`、`Influence` → `Inflluence`、
`films` → `fifilms`、`electric-field` → `electric-fifield-`。

**根因**：`pdfplumber` 的 `extract_text_lines(return_chars=True)` 在展开连字
（U+FB01 `ﬁ` → "fi"）时，会把连字字符在 `chars` 里**重复登记一次**
（x0/x1 **完全相同**），而 `line["text"]` 只有一份。
实测 `Magnetoelectric Composites` 第 9 页：`page.chars` 里 'ﬁ' 有 **113** 个，
`line['chars']` 里变成 **132** 个 → 拿这份 chars 重新切词，每个连字都变成 "fi"+"fi"。

→ 两道修法：
1. 切词前**去重**（`_dedupe_chars`）：`x0 + x1 + 字符内容` 三者全同 → 同一个字形登记了两次。
2. **硬不变式**（`_no_ws` 比对）：修复结果去掉所有空白后，必须与原行去掉所有空白后
   **逐字相同**；对不上就**整行弃修**。
   → 这一条把「字符丢失/多出」全部挡在外面，以后阈值算法怎么改都不会越界。

护栏：`microbench/_check_heal_invariant.py`（全库扫，输出「修复了 N 行 / 字符被改动 M 行」）
+ `tests/test_block_layer_fixes.py`（合成样本 + 真实样本）。
**当前实测：全库 22 行被修复，字符被改动 0 行。**

> ⚠ 顺带记一条**假阴性**踩坑：不变式脚本第一版写成
> 「`with pdfplumber.open(...)` 外面遍历 `doc.pages`」→ 文档一关，page 对象失效，
> `page.crop()` 抛异常被 `except` 吞掉 → 脚本报「修复了 0 行」，实际明明修了 18 行。
> **凡是把 `except Exception: continue` 写在循环里，都要同时打印一个「跳过了几页」计数**。

#### 3.13.4 ⚠「并排还是堆叠」不能看视口宽 —— 真机实测抓出的两个布局缺陷

§3.13.2 的开关上线后，五套渲染护栏全 PASS，
但 2026-11-13 用**真实前端 + 真实数据**跑一遍（桌面端 Playwright 打开
`http://127.0.0.1:5000/`，平板端拦截 `appassets.androidplatform.net`），
**抓出两个护栏一个都测不到的缺陷**：

| 端 | 症状 | 根因 |
|---|---|---|
| 桌面端 | 对照译文列只剩 **43px**（挤成一条竖线）；同一篇里第 1 页全宽、第 2 页 43px，滚起来忽宽忽窄 | 门槛写的是 `@media (min-width:900px)`（**视口**宽），但真正约束是**阅读器栏**宽（1600px 窗口里只有 ~790px，而 canvas 就 733px）；且当时**逐页各算各的** |
| 平板端 | 对照译文栏宽 **6349px**，页卡被撑到 6940px → 横向溢出屏幕，译文在屏幕外没法看 | 译文栏写的是 `width:max-content` —— 一整段中文「不换行」的宽度 |

**修法（两端统一）**：

1. **并排与否由 JS 按「容器可用宽 − 原页宽」决定**（类名驱动），不够就**退回上下堆叠**；
   桌面 `_applyBilingualOrigLayout`（阈值 380px）、平板 `applyOrigSideLayout`（阈值 320px）。
2. **所有页卡统一判断**（取最大 canvas 宽度）→ 杜绝一页宽一页窄。
3. **译文栏宽度一律给确定值**（JS 量出来写进 CSS 变量），**绝不用 `max-content`**。
4. 量宽度要量 **canvas**，不能量 `.tb-pdf-orig` 容器（容器是 flex 子项，列布局下会被拉满页卡宽
   → 可用宽度算成 0 → 永远并排不了）；平板 `.tb-pdf-orig` 加 `align-self:flex-start`
   → 否则 `.tb-pdf-anno-layer` / `.tb-pdf-ink`（`inset:0`）跟着变宽 →
   **批注高亮和手写笔迹整体错位**。

**实测结果**（同一篇 JACERS 综述）：

| 端 | 原页 | 对照译文 | 判定 |
|---|---|---|---|
| 桌面端（1600px 视口 / 阅读器栏 737px） | 733px | **737px 满栏** | 退回上下堆叠（栏宽不够并排）→ 8 页**全部一致** |
| 平板端（1180px 视口） | 581px | **562px** | **并排**，同排不重叠，页面无横向溢出 |

护栏（这一轮新增的断言）：
- 桌面 `_test_desktop_bilingual_render.py`：**31 条**（新增「并排时译文列 ≥380px」
  「不够宽时退回堆叠」「堆叠时译文列是整栏宽」）。
- 平板 `_test_tablet_bilingual_render.py`：**30 条**（新增第二个探针，专门验
  「并排/堆叠判定 + 译文栏不超容器」；原来的探针**只测译文分栏，根本没渲染页卡布局**）。
- 圈选护栏 `_test_tablet_region_render.py`：**12 条**（改成跑**真代码**决定布局，
  「浮条不压译文栏」改成**矩形不相交**判据 → 与并排/堆叠无关）。

> ⚠ **教训**：护栏全绿 ≠ 达成目标。改**布局类**代码，
> 必须用真实前端 + 真实数据跑一遍（`_live_ui_test.py` / `_live_tablet_test.py`）。
> 另外：探针要带上真实样式表的全局重置（`*{box-sizing:border-box}`），
> 少了它量出来的宽度虚高 32px → 断言全错；跑真代码时**先定尺寸再算布局**
> （canvas 没设尺寸时量到的是默认值）。

---

## 4. 平板端要"跟电脑端对等"这件事的现状

平板端 **只读不写译文**（译文是单向从电脑端同步过来的）：
- 平板读 `<sync_root>/snapshots/<key>.json` 的 `pages[].translation`；
- 自己的副本在 IndexedDB（键 `trans_v{ver}_{safePaperKey}`）；
- **段落译文**（对照模式）走同一个文件：`pages[].blocks` + `blocks_version`，
  本地副本键 `blocks_v{ver}_{safePaperKey}`（**与整页译文分开存**，
  两者的版本闸门不同，混在一个键里会互相作废）。

所以「对照模式」也是**两端各写各的前端**（`static/index.html` 与
`static/tablet/app.js` 是两份独立前端，桌面的修复一行都不过去）。

---

## 5. 验收方式

```powershell
# 平板圈选：静态断言 + headless Edge 真渲染
node microbench/_test_tablet_region_ask.js
python microbench/_test_tablet_region_render.py

# 段落块：判据 + 真实双栏论文 + 「页级文本一个字都没变」
python -m pytest tests/test_page_blocks.py -q

# 段落级翻译：协议 / 版本闸门 / 快照往返 / 前端接线
python -m pytest tests/test_bilingual.py -q

# 对照模式的**真渲染**护栏（抽真代码 → headless Edge → 量坐标）
python microbench/_test_desktop_bilingual_render.py
python microbench/_test_tablet_bilingual_render.py

# 平板本地裁的公式图 vs 电脑端 PyMuPDF 裁的（逐像素比对）
python microbench/_test_tablet_formula_shot.py

# ⚠ 改抽取层后**必须**跑这个：拿 git HEAD 的旧版模块做 A/B，
#   逐字比对页级文本 —— 不一致就别想动 EXTRACTOR_VERSION
python microbench/_check_text_unchanged.py

# ⚠ 改了**块层**还要跑这个：逐页比对段落块（col/kind/bbox/text），
#   确认「只影响该影响的页」
python microbench/_check_blocks_diff.py

# ⚠ 改了空格修复还要跑这个：验证「只加空格、不改字符」的不变式
#   （输出「修复了 N 行 / 字符被改动 M 行」，M 必须恒为 0）
python microbench/_check_heal_invariant.py

# 看修复前后长什么样（生成取证图 logs/blockdiag/heal-evidence.png）
python microbench/_shot_heal_evidence.py

# 全量（⚠ 必须带 -m "not slow"，不带会卡在 arxiv 网络测试上）
python -m pytest tests -q -m "not slow"

# 段落块的诊断 + 可视化（看图最直观）
python microbench/_diag_page_blocks.py --pdf "<某篇双栏 PDF>" --pages 3

# 章节标题切分的全库诊断（看误伤有多少）
python microbench/_diag_headings.py

# 渲染一张「对照模式」真实效果图（真 CSS + 真函数 + 真 API 数据）
python microbench/_shot_bilingual.py --page 3
# → logs/blockdiag/bilingual-*.png

# 平板端「对照模式」真实效果图（含本地裁的公式原图）
python microbench/_shot_tablet_bilingual.py
# → logs/blockdiag/tablet-bilingual-*.png

# 交付验收：APK 里有没有这一轮的功能（改前端后必跑）
python microbench/_verify_delivery.py
```

真渲染护栏（`_test_tablet_region_render.py`）实测过两条关键结论：
浮条在手写层**上面**（`elementFromPoint` 命中的是按钮，不是手写层）、
浮条在原文层**坐标系**里（宽屏并排时没有跑到译文栏上）。

---

## 6. 坑清单（改动前必读）

| # | 坑 | 后果 |
|---|---|---|
| 1 | 平板 `app.js` 里有 **1 个 NUL 字节** → ripgrep 判为二进制直接跳过 | 用 `grep` 工具在这个文件里找不到任何东西（看着像"没有代码"）。用 `Select-String` 或 `read` 工具 |
| 2 | 浮条挂 `.tb-pdf-page` 而不是 `.tb-pdf-orig` | 宽屏并排时按钮跑到译文栏上 |
| 3 | 浮条被手写层盖住（手写层 `pointer-events:auto` 铺满整页） | 点"问这块"变成"又画一笔"，用户以为没反应 |
| 4 | 假设原文 canvas 与笔迹 canvas 像素尺寸相同 | 圈线与裁图错位 |
| 5 | 上标碎片（缩进 225pt）当段落起点 | 一段话被切成两半，且两块 y 重叠 |
| 6 | 用 `min(x0)` 估栏左边界 | 异常行把边界带偏，所有行都变成"缩进" |
| 7 | 段落级译文并进主快照却不加版本闸门 | 旧译文被当成段落级译文 → 错位且无人发现 |
| 8 | 改 `EXTRACTOR_VERSION` | 用户全部译文作废、跨设备重翻（AGENTS.md §4.12② 的老坑） |
| 9 | 单栏页从 `y_split` 起裁 | 整段图注丢失（纯图页的最常见内容就是图注） |
| 10 | 竖排水印并进段落 | 整段 bbox 被撑成 568pt 高的竖条，译文无处安放 |
| 11 | 抽取层改动后**不复核页级文本** | 改了正文却不知道 → 旧译文错位且无人发现（用 `_check_text_unchanged.py` 做 A/B） |
| 12 | 把译文**绝对定位贴回 canvas** | PDF 页按栏宽自适应（倍率 0.69）→ 原文正文只有 ~6px，译文贴回去也读不了；而且译文更长会压到下一段 |
| 13 | 公式/表格块也送进模型 | 公式碎片被模型"补全"成不存在的公式；表格数值整行错位（错公式比没公式危险得多） |
| 14 | 模型无视编号标记、段数还对不上时**照样贴上去** | 译文贴到错的段落上 —— 比没译文更糟。必须整页放弃（502） |
| 15 | 章节标题判据放宽（词数 ≤ 9 / 不限行数） | 正文句子与标题折行被吞进标题，译文糊成一坨 |
| 16 | 渲染护栏里**抄一遍**真代码 | 改了真代码它照样绿 → 护栏零保护力。要从源文件里抽真函数 |
| 17 | 探针用 `str.format()` 拼 HTML，字面 `{`/`}` 没转义 | `Single '}' encountered in format string`，探针直接跑不起来 |
| 18 | 平板用 `render({transform:[1,0,0,1,-x,-y]})` + 小画布裁公式 | ① 整页画到画布外 → 裁出空白；② canvas 小于 viewport 时 `render().promise` **不 resolve** → 页面永远转圈。**必须用 `getViewport({offsetX,offsetY})`**（§3.11） |
| 19 | 段落协议**要求闭标记** `⟦/Tn⟧` | 模型听话但**常忘写闭标记**（实测 JACERS 第 2 页 12 段一个闭标记都没写）→ 整页 502。解析器要能按下一个开标记切段（§3.12） |
| 20 | 段落块文本用 `\n` 拼行 | PDF 行折进到模型输入 → 模型照着回车 → 译文里下标被拆成单独一行。块文本要用**空格**拼 |
| 21 | 表格块在带抽取**结束之后**才 append | 表题与表体被拆到两头 → 对照视图里表题和表体中间隔着别的内容。跨栏桶要按 `bbox.top` 排 |
| 22 | 标题续行只卡「长度 ≤ 70 字符」 | 正文**第一行**（60 字符）被当标题折行吞进去 → 标题译文后面粘着一句正文。续行还要卡**词数**（≤ 6） |
| 23 | 抽真代码时丢掉 `async` 前缀 | 非 async 函数里 `await` = SyntaxError → 整页脚本不执行，浏览器里啥也不发生（像"卡住"，实则连报错都没有） |
| 24 | 抽真代码时漏抽依赖的常量/辅助函数 | `ReferenceError` → 平板 `renderMarkdown` 走 `<pre>` 兜底 → 表格不渲染、行内公式显示成 `$...$` 源码（看着像功能没做） |
| 25 | 用「底部中间有条缝」判断「底部是两栏正文」 | 单栏论文底部的**宽表格**也有缝（那是列分隔线）→ 表格行被从中间劈开 → 原文与译文全错位（实测 3 页被误伤）。必须再加**三道**约束：投票的带左右各 ≥8 词 / 栏缝跨 ≥3 个带 / 够词的每一带都「像正文」 |
| 26 | 用固定 `x_tolerance` 切词 | PDF 的文本层可能**根本没有空格字符**（字符逐个精确摆放，词边界只靠位置间隙）→ 词粘成一坨。空格宽度随字号变（7pt≈1.4pt、10pt≈2pt）→ 固定容差必然一边切错、另一边粘住 |
| 27 | 只按 x 间隙自己切词 | 会漏掉「基线/字号变了就断开」这条规则（上下标 `d31` 该拆成 `d 31`）。要**复用 pdfplumber 自己的切词器**（`pdfplumber.utils.extract_words`） |
| 28 | 想把图片/图表从 PDF 抽出来插进译文栏 | 矢量图根本没有位图可抽，且与「重排」的前提冲突。正解：**原页 canvas 不藏了** → 宽屏左原页图右对照译文、窄屏上下堆叠（零额外请求） |
| 29 | 拿 `chars` 重切词前**不检查它和原文是否一致** | pdfplumber 展开连字（U+FB01 `ﬁ`）时会把连字字符在 `chars` 里**重复登记一次**（x0/x1 相同，`line["text"]` 只有一份）→ 切出 `coeffificients` / `fifilms` 这种**凭空多一个 fi** 的坏词。→ 切词前按「x0+x1+字符」去重，并加**硬不变式**：结果去空白后必须与原文逐字相同，否则整行弃修 |
| 30 | 护栏里写「词数不够就不否决」这种**逃生口** | 当**所有**候选都从这个口溜过去时，护栏等于没写（实测 3 页照样被误伤）。→ 护栏必须用**真实误伤样本**做回归，不能只写合成样本 |
| 31 | 在 `with pdfplumber.open(...)` **外面**遍历 `doc.pages` | 文档一关 page 对象就失效，`page.crop()` 抛异常被 `except` 吞掉 → 脚本报「修复了 0 行」，实际明明修了 18 行（**假阴性**）。→ `except Exception: continue` 旁边必须打印「跳过了几页」 |
| 32 | 用 `@media (min-width:…)` 判断「并排还是堆叠」 | 那是**视口**宽，真正约束是**容器**（阅读器栏 / 滚动容器）宽。实测：1600px 窗口里阅读器栏只有 ~790px 而 canvas 733px → 并排后译文列只剩 **43px**（挤成一条竖线）。→ 并排与否由 JS 按「容器可用宽 − 原页宽」决定，类名驱动；不够就**退回上下堆叠** |
| 33 | 译文栏用 `width:max-content` | 一整段中文「不换行」的宽度 → 实测 **6349px**，页卡被撑到 6940px → 横向溢出屏幕，译文在屏幕外没法看。→ 宽度一律给**确定值**（JS 量出来写进 CSS 变量） |
| 34 | 量宽度时量 flex **容器**而不是 canvas | 容器是 flex 子项，列布局下会被拉满页卡宽（实测 1180px，而 canvas 只有 581px）→ 可用宽度算成 0 → 永远并排不了。平板 `.tb-pdf-orig` 还要加 `align-self:flex-start`，否则 `inset:0` 的注解层/手写层跟着变宽 → **笔迹和高亮整体错位** |
| 35 | 只跑渲染护栏就宣布「修好了」 | 护栏全绿 ≠ 达成目标：这一轮五套护栏全 PASS，真机实测仍抓出 32~34 三个缺陷（老护栏**压根没渲染页卡布局**）。→ 改布局类代码必须用**真实前端 + 真实数据**跑一遍（`_live_ui_test.py` / `_live_tablet_test.py`）。另：探针要带 `*{box-sizing:border-box}`（否则宽度虚高 32px），跑真代码时**先定尺寸再算布局** |

---

## 7. 下一位接手者要做的（按优先级）

1. **把 `blocks` 并进 `pdf_cache`**（`EXTRACT_IMPL_REV` 2→3），
   消掉 `extract_page_blocks` 的二次解析（现在单页 64~332ms）。
   ⚠ 只动 `EXTRACT_IMPL_REV`（只作废磁盘缓存 ≈72s），**译文不丢**；
   **不要**动 `EXTRACTOR_VERSION`（那会让用户全部译文作废重翻）。
2. ~~**修 §3.3 末尾那条已知局限**：纯图页（ACS 大图页）底部的左右两栏会被并成
   一个宽块~~ → **2026-11-13 已做**（§3.13.1）：块层加了 `_find_bottom_column_split`
   兜底，并加固到**全库 429 页一次都不触发**（既不误伤、也不误修）。
   ⚠ 想收紧或放宽这条判定，先看 §3.13.1 的三道约束表 + `tests/test_block_layer_fixes.py`
   里那**三个真实误伤样本**。
3. ~~**对照视图里看不到图片/图表**~~ → **2026-11-13 已做**（§3.13.2）：
   两端都加了「🖼 原页图」开关（宽屏左原页图右对照译文、窄屏上下堆叠）。
4. 两台设备**各自**做前端（桌面的改动不过去平板），各自测。
