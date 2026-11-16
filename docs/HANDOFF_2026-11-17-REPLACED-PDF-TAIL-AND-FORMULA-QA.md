# 交接：2026-11-17 —— 「中译替换版」最后几页没替换 + 划译文提问看不到公式

> 这份文档记的是**用户 2026-11-17 一次报上来的两个缺陷**。
> 两个缺陷看起来不相干，实际是**同一个模式**：
> 「拿到的数据比需要的少一截，但系统**静默地**按手头的东西往下走」。
>
> 坑记在 `AGENTS.md` **§4.96 / §4.97**（本文件是那两个坑的展开）。
> 改动文件：`microbench/app.py`、`microbench/static/index.html`、
> `tests/test_qa_formula_images.py`、`tests/test_replaced_pdf.py`、
> `microbench/_test_qa_formula_frontend.js`、`microbench/_test_replaced_pdf_frontend.js`。
>
> ⚠ 对方设备 `git pull` 即生效，**不用各自改一遍**。
> ⚠ 本轮**没有动** `EXTRACTOR_VERSION` / `BLOCK_TRANSLATION_VERSION` → **用户已有的译文全部不作废**。

---

## 0. 一分钟结论

| 缺陷 | 用户原话 | 根因 | 修法 |
|---|---|---|---|
| 替换版最后几页没替换 | 「我论文最后几页都没替换」 | 那几页在**普通模式**翻的（只有整页译文，**没有段落坐标**）；替换版只认段落级 | 端点如实报告 `pages_without_blocks` + 前端**构建前自动按段落补翻** |
| 划译文提问看不到公式 | 「ai 看上下文的时候完全看不到公式推导，只能猜」 | ① 译文栏划选的截图门禁写死 `source === "pdf"` → **整页图压根没发**；② 就算发了，PDF.js 画布按栏宽渲染 → 公式只有 **~18px 高**，物理上读不出来 | 服务端**按坐标裁高清公式图**（scale=4 → 894×40px）一起发给模型 |

---

## 1. 缺陷一：「中译替换版」最后几页没被替换

### 1.1 取证

用户报的论文是 JACERS 综述（`2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf`）。
查它的快照 `logs/_pdf_snapshots/2008-JACERS-综述-Magnetoelectric laminate composites An overview_pgghcp.json`：

| 页 | `blocks` | `blocks_version` | 整页译文 |
|---|---|---|---|
| 1 | 18 | 1 | 有 |
| 2 | 16 | 1 | 有 |
| 3 | 11 | 1 | 有 |
| 4 | 34 | 1 | 有 |
| **5** | **0** | **None** | 有 |
| **6** | **0** | **None** | 有 |
| **7** | **0** | **None** | 有 |
| **8** | **0** | **None** | 有 |

### 1.2 根因

替换版要把中文**原地**贴回原页 → 必须有每个段落的**坐标**（`blocks`）。
「整页译文」（**普通模式**翻的）**没有坐标** → 那些页只能**原样保留英文**。

→ 这不是渲染 bug，是**两个模式产出的数据结构不一样**：
普通模式产出「一整段译文」，对照模式产出「段落 + 坐标」。
端点之前**静默降级**（只生成能替换的页，不吭声）→ 用户以为全替换了。

### 1.3 修法

**后端**（`microbench/app.py` 的 `api_build_replaced_pdf`）：

- 解析快照时收集 `pages_without_blocks` = 「有整页译文但没有 blocks」的页；
- 响应里加 `"pages_without_blocks": sorted(...)`；
- 409 文案改说清「要用对照模式翻」。

**前端**（`microbench/static/index.html`）：

- `_pagesMissingParagraphTranslations()` → 读快照（替换版端点读的就是它）
  → **一次请求拿到真相**，不用逐页去抽段落几何；
- `_ensureParagraphTranslations()` → 缺 blocks 的页逐页
  `translateBlocksForPage(n, true)` → **复用对照模式那套机器**，
  有逐页进度反馈，**不产生 >60s 的长单请求**；
- 翻完 `savePdfSnapshot({silent:true})` → blocks 随快照持久化 → **下次构建不用再翻**；
- **≥2 页先 `confirm()`**：告诉用户会调模型 N 次（约 N×15 秒、消耗额度）；
  用户拒绝就只生成能替换的部分（不硬来）；
- `buildReplacedPdf()` 在发构建请求**之前**先 `await _ensureParagraphTranslations()`；
- 构建成功的 toast 里如实写「⚠ 第 N 页没有段落级译文，保留英文」。

### 1.4 为什么不是「后端自动翻」

后端自动翻 = 一次 >60s 的长请求：
前端 SSE 读不到逐页进度 → 用户以为卡死；失败时无法逐页重试；
还要在后端再实现一套「翻页 + 存快照」的编排。
**复用已有机器 + 前端编排**才是对的。

### 1.5 护栏

- `tests/test_replaced_pdf.py::test_api_reports_the_pages_that_cannot_be_replaced`
  —— 真打端点 → 断言 `pages_without_blocks == [2]` 且 `pages_replaced == 1`；
- `microbench/_test_replaced_pdf_frontend.js` 新增 4 条**行为断言**：
  缺 blocks → 构建前先补翻 / 全都有 → 不重翻 / ≥2 页先问用户 / 用户拒绝 → 不翻也不发构建请求；
  + 4 个变异自检（把补翻删掉 / 判据改坏 / 补翻完不存快照 / 不问用户）→ **4/4 全被抓**。

---

## 2. 缺陷二：划译文提问时 AI 看不到公式

### 2.1 两层根因（都实测过）

**① 根本没发图**

译文栏划选走 `source_type="translation"`，而前端的截图门禁写的是：

```js
if (_readerSelState.source === "pdf") { pageImage = _captureSelectionPageImage(); }
```

→ 译文栏划选 **不满足**这个条件 → 整页截图压根没进请求体。
Playwright 拦截请求实测：

```json
{"source_type": "translation", "has_page_image": false, "page_image_len": 0,
 "ctx_len": 5290, "sel_len": 30}
```

**② 发了也看不清**

浏览器那张是 **PDF.js 画布**，画布按**栏宽**渲染（双栏模式下整页只有 ~785px 宽）
→ 一条 223pt 宽的公式在整页图上只有 **~18px 高** → 多模态模型**读不出来**。

⚠ **第 ② 条是本轮最关键的认知**：
**「把整页图发给模型」不等于「模型看得见公式」**。
整页图回答的是「这一页长什么样」，回答不了「这条公式具体是什么」。

### 2.2 修法（两段式，缺一不可）

| 附件 | 作用 | 来源 |
|---|---|---|
| 整页缩略图 | 布局上下文（公式/图表在哪一块） | 服务端 `paper_text.render_page_png`（zoom=2.0，自带 max_pixels 护栏） |
| **公式高清原图** | **模型唯一能看清公式的地方** | 服务端按坐标 `_pdf_clip_png(..., scale=4.0)` 裁 |

实测（JACERS 综述真实页面，公式坐标 223×10~30pt）：

| | 整页图 | 公式裁图（scale=4） |
|---|---|---|
| 223pt 宽的公式 | ~400×**18**px | **894×40**px |
| 单张 PNG | — | 4~12KB |

→ 裁图不是「多发点东西」，是**唯一**能让模型读出公式的途径；
体积也完全可接受（8 张公式图 ≈ 100KB）。

### 2.3 坐标从哪来

译文原文里的 `⟦FORMULA:x0,y0,x1,y1⟧` 标记
（抽取层定位到公式时写进去、提示词要求模型照抄不改数字）。

前端新增 `_collectPageFormulaRects(pageNum)`：

- 从 `pg.translation`（**整页译文**）抽；
- 从 `pg.blockTranslations`（**对照模式的段落译文**）抽；
- **去重**（整页译文和段落译文里同一条公式会重复出现）；
- 上限 16 条。

两个来源都要读：普通模式翻的页只有 `pg.translation`，
对照模式翻的页只有 `pg.blockTranslations` —— **只读一个就有一半的页抽不到**。

### 2.4 三层防御（每层都可能被绕过，所以都要有）

1. **前端**：没有页码 / 没有该页 → 返回空数组，不抛异常；
2. **服务端**：`_sanitize_formula_rects()` 逐条校验
   （NaN / ±∞ / 反向 / 非四元组 / 非数字全丢）。

   ⚠ **不能只靠下游裁图函数兜底**：Python 里 `min(nan, 612)` 返回 nan →
   `cx1 - cx0` 是 nan → `nan <= 0` 为 **False** → **不会抛异常**，
   会带着 nan 一路走进 PyMuPDF → 要么炸在里面，要么裁出一张空白图冒充公式
   （更糟：模型会拿它当依据）。

3. **提示词**：明说「**一律以这些高清公式图为准**」
   「**不要根据上下文补全或猜测公式**」「看不清就明说『图中看不清』」。

   仓库既有红线：**错公式比没公式危险得多**（用户会照着它去算）。

### 2.5 护栏

- `tests/test_qa_formula_images.py`（10 条，**行为级**：真打端点，
  拦下**真正发给模型**的 messages）：

  | 用例 | 盯什么 |
  |---|---|
  | `test_qa_sends_the_page_image_plus_one_crop_per_formula` | 模型收到 1 张整页图 + N 张公式图 |
  | `test_formula_crop_is_far_bigger_than_the_same_formula_on_the_page_image` | **核心**：裁图必须明显比整页图里清楚 |
  | `test_prompt_tells_the_model_to_trust_the_high_res_formula_images` | 提示词红线（以图为准 / 不许猜） |
  | `test_sanitizer_drops_every_kind_of_broken_rect` | 校验层逐条细测 |
  | `test_sanitizer_caps_the_number_of_rects` | 上限 16 |
  | `test_qa_without_formula_rects_still_works` | 反向用例：没坐标也不能挂 |
  | `test_non_numeric_formula_rects_are_rejected_by_the_schema` | pydantic 模型层 422 |
  | `test_broken_formula_rects_do_not_break_the_qa` | 畸形坐标不成额外图片 |
  | `test_formula_rects_outside_the_pdf_are_dropped` | 越界坐标不裁（空白图冒充公式） |
  | `test_qa_formula_frontend_node_guardrail_passes` | 挂 node 套件 |

  **变异自检已做**（5 个变异体：不裁图 / 倍率 4→1 / 裁了不送 / 删提示 / 不校验）
  → **5/5 全被抓**。

- `microbench/_test_qa_formula_frontend.js`（8 条 + **6/6 变异自检**）：
  从 `index.html` 切出 `_collectPageFormulaRects` 与 `submitReaderQA` **真跑**，
  断言**请求里真的带上了 formula_rects**、页码也带上了。

---

## 3. 顺带修的第三件事：5 个渲染护栏**集体失效**（环境问题，不是代码问题）

全量 pytest 跑出 5 条红：

```
FAILED tests/test_pdf_layout_guardrails.py::test_pdf_layout_guardrail_passes[
  microbench/_test_tablet_pdf_layout.py]
FAILED ... [microbench/_test_desktop_ink_render.py]
FAILED ... [microbench/_test_tablet_region_render.py]
FAILED ... [microbench/_test_desktop_bilingual_render.py]
FAILED ... [microbench/_test_tablet_bilingual_render.py]
→ 全部报：渲染探针失败：没拿到探针结果（DOM 长度 0）
```

**先证伪再改代码**：`git stash` 掉这轮改的 `index.html` → **依然红** → 与改动无关。

**真根因**：本机（30461）的**系统 Edge 157 headless 根本启动不了**：

```
msedge.exe --headless=new --disable-gpu --no-sandbox --dump-dom 平凡.html
→ stdout 长度 0；连 --user-data-dir 目录都建不出来（进程压根没起）
```

而 **Playwright 自带的 Chromium 是好的**。

**危害比「测试红」大得多**：这 5 条护栏护的正是
「对照模式真的左右分栏」「手写笔迹真的画出来」「圈选区域真的换算成坐标」
「平板 PDF 版式真的没被挤爆」…… → **护栏集体失效 = 这些行为改坏了也没人知道**。

**修法**（新增共享模块 `microbench/_headless_browser.py`）：

- `dump_dom()`：**系统浏览器优先 → 拿不到输出就回退** Playwright 的 Chromium；
  两个都没有才抛错（**护栏必须红，不许静默跳过**）；
- `find_browser()` 也走回退链 —— 否则各探针 `main()` 里那句
  「找不到浏览器 → SKIP 整个护栏」会让系统浏览器一缺，**护栏静默失效**；
- `--headless` 与 `--headless=new` 都试（Chromium <112 只有前者；
  132+ 旧 headless 已移除，`--headless` 自动指向新版）；
- 回退**不依赖 playwright 这个 pip 包**（只扫它的浏览器缓存目录）
  → **对方设备没装 playwright 时，行为跟今天完全一样**。

修完 5 条护栏全绿（31 / 13 / 12 / 30 / 13 条断言），
**顺带证明了本轮对 `index.html` 的改动没有破坏任何渲染行为**。

### 3.1 顺带修的第四件事：真机护栏把**用户报的 bug 钉成了「正确行为」**

`microbench/_live_replaced_pdf_test.py` 在修完缺陷后变红，两条：

```
FAIL  输出页数 = 原文页数 + 替换页数（8 → 12）
FAIL  原文页与替换页交替出现  →  英中英中英中英中英中英中英中英中 ≠ 英中英中英中英中英英英英
```

**看着像「我改坏了」，其实不是** —— 那两条期望值是**写死的**：

```python
expect = ("英中" * 4) + ("英" * 4)     # ← 正是用户报的「最后几页都没替换」
check("输出页数 = 原文页数 + 替换页数（8 → 12）", doc.page_count == 12)
```

→ **护栏把 bug 当成正确行为钉住了**。缺陷修好后，它当然红。

另外它还有第二个毛病：靠 `glob("*.pdf")` 取**任意一个** PDF，
同目录里有别的论文的产物时就会读到**别人的文件**。

**修法**（断言的是**不变量**，不是某个具体数字）：

1. 期望值改成**从 toast 里解析**：
   `已生成 N 页（M 原文 + K 替换）` → 期望 `M + K` 页、序列 `"英中"*K + "英"*(M-K)`；
   → 无论哪几页没翻，判据都成立，且**不再把某个具体状态当成标准**；
2. 取**最新修改**的那个 PDF（= 刚刚构建出来的那个）。

修完真机护栏全绿：**16 页（8 原文 + 8 替换）、严格交替、8 个替换页都有中文、图表保留**。

⚠ **这条值得抄**：护栏里**写死的期望值**一旦对应的行为变好，就会变成「不许修」。
凡是把「当前的坏状态」写进期望值的，都该改成**从实际输出推导不变量**。

---

## 4. 本轮刻意**没做**的事（留给下一轮，别以为是漏了）

1. **没动 `EXTRACTOR_VERSION` / `BLOCK_TRANSLATION_VERSION`**
   → 用户已有的全部译文**不作废**，不需要重翻。
2. **没改 `load_pack` 的返回契约**
   → 它是数据安全关键路径，留作独立一轮的窄缝修复，别和这两条混在一起。
3. **没在服务端做「缺段落就自动翻」**
   → 理由见 §1.4。
4. **没把公式裁图做成「按选区邻近度排序」**
   → 目前的做法是「该页的公式全给」（一页通常 2~6 条）。
   将来若要「只给选区附近的那几条」，需要前端把选区映射回 PDF 坐标
   （对照模式下 `pg.blocks` 里有 bbox，普通模式下没有）—— 是独立一轮的活。

---

## 5. 复现与自检清单

```powershell
# 服务（改过 app.py 必须重启）
Start-Process -FilePath (Get-Command python).Source `
  -ArgumentList '-m','uvicorn','microbench.app:app','--host','127.0.0.1','--port','5000','--log-level','warning' `
  -WorkingDirectory 'D:\Desktop\杂物\琐事\个人工作台' -WindowStyle Hidden

# 护栏
python -m pytest tests/test_qa_formula_images.py -q     # 10 passed
python -m pytest tests/test_replaced_pdf.py -q           # 22 passed
node microbench/_test_qa_formula_frontend.js             # PASS 8 / 变异 6-6
node microbench/_test_replaced_pdf_frontend.js           # PASS 13 / 变异 16-16
python -m microbench._test_regressions                   # 40 passed
```

⚠ 本机**没有 Android SDK** → `tests/test_apk_delivery.py::test_translations_are_baked_in`
仍是那条**改动前就红**的用例（APK 与 `MicroBench_平板工作台.html` 都是 gitignore 的
本地产物，不进仓库，对另一台设备无影响）。

---

## 6. 给对方设备的一句话

**这两个缺陷的共同教训**：拿到的数据比需要的少一截时，
**要报错/要如实报告**，不要静默按手头的东西往下走。
「静默降级」在两个地方各制造了一次「用户以为成功了，其实没成」。

对照 `AGENTS.md` §4.16（护栏全绿 ≠ 达成目标）：
这轮的护栏**全部是行为级**的，而且**每个都带变异自检** ——
改坏行为就红，不改坏行为别想蒙过去。
