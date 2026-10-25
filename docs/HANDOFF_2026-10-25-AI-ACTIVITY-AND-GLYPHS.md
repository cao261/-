# 交接说明：2026-10-25 下午这轮改动（给 30461 那台设备的 agent）

> **怎么用这份文件**：把它整份贴给那台设备上的 agent（或让它读仓库里的这个文件）。
> 两台设备之间没有会话通道，交接一律走仓库：对方 `git pull` 就能拿到。
> 本文件是**某个时点的快照**，以 §6 的"自检清单"为准，别把它当成长期状态。

---

## 0. 一句话

这一轮在 25353 这台设备上做了三件事，每件都实测过、都有回归护栏、都已 push：
**① 批注重译不丢 + 列表按当前页定位；② AI 活动可视化（进度/阶段/取消）；
③ 解不出的数学符号（× ≈ ≥）不再是 "?"，改成裁图认字 + 词典缓存。**
另外还把"换模型"这条路打通了：能接你自己的 OpenAI 兼容 API 模型，主模型额度耗尽自动降级。

---

## 1. 这轮动了哪些文件（对方 pull 之后要一起看的）

| 文件 | 改了什么 | 为什么对方也要看 |
|---|---|---|
| `microbench/static/index.html` | ① 批注 orphan 判定 + 抽屉按页定位 ② 「AI 活动」登记处 + 面板 ③ 选区归属兜底 | **单文件前端**，对方如果也改过 index.html，合并时要留意 |
| `microbench/app.py` | ① 抄表返回值校验 ② 表格读图跟随所选模型 ③ 答疑三个入口接受 `model` ④ 符号认字接入 | API 契约变了（见 §3） |
| `microbench/llm.py` | ① 自定义模型配置解析 ② 限流按模型配 ③ `chat/chat_async/chat_stream_async` 支持自定义模型 + 连不上自动退回 | **降级/限流逻辑是全局的**，对方改 llm.py 要一起看 |
| `microbench/paper_text.py` | ① 孤立符号打锚点标记 ② 行匹配放宽 ③ plain 兜底路径也走行级抽取 ④ EXTRACTOR_VERSION 4→5 | **抽取层，最敏感**（见 §4 的提醒） |
| `microbench/glyphs.py`（新） | 符号词典 + 裁图认字 | 词典文件 `logs/glyph_dict.json` 两台设备共用 |
| `tests/test_reader_annotations.py`（新） | 批注护栏 5 项 | |
| `tests/test_reader_ai_activity.js`（新） | AI 活动护栏 25 项（node 跑 index.html 真实源码） | |
| `tests/test_reader_ai_activity.py`（新） | AI 活动护栏 5 项 | |
| `tests/test_reader_glyphs.py`（新） | 符号护栏 19 项 | |
| `docs/LLM_CONTRACT.md`（新） | 模型层契约（能力矩阵 / 配置 / 限流分档 / 已知缺口） | **对方要接自己的模型就看这份** |
| `docs/READER_CONTRACT.md` | 新增 §6「解不出的数学符号」 | |
| `AGENTS.md` | 新增 §4.20/§4.21/§4.22 三节（模型层 / AI 活动 / 符号认字），章节号重排过 | **合并后 4.15/4.16 撞过号**，对方的 4.15~4.19 未动，我的挪到 4.20~4.22 |
| `.env`（**不入库**） | 加了自定义模型的注释模板 | 对方要接自己的模型，照着模板填 |

---

## 2. 三件事分别是什么、对方要不要动

### ① 批注重译不丢 + 列表按当前页定位（用户原话："这个记录要有""不要每次都从第一页开始"）

- 重译后引文对不上 → **不删记录**，只标「⚠ 位置失效（该页重译后原文变了）· 笔记已保留」
- PDF 侧标注（有 rects）**永远不算失效**（高亮画在 PDF 画布上，不受重译影响）
- 抽屉打开时滚到**当前页**的第一条；本页没有就落下一页的第一条
- 顺带修了一个真 bug：拖选时**松手位置在文字外**（很常见）→ 页码丢失 →
  批注被存成"章节视图"，用户在自己看的那页上找不到它。改成按**选区本身**兜底
  （`_resolveSelectionOrigin`）

**对方要不要动**：不用。纯前端 + 后端已改好。

### ② AI 活动可视化（用户原话："他有可能等了半天然后什么也没输出"）

- 所有 AI 请求（按页翻译 / 章节翻译 / 划词答疑）统一登记
- 每 500ms 刷新「已用时 + 阶段」；阶段是**前端按耗时推断**的（文案里写明了）
- 工具栏「🤖 AI 活动」徽标 + 浮层面板，每行一个任务，带 **✕ 取消**（真 abort）
- 慢任务 60s 变黄、150s 变红；用户自己取消的**不显示成"失败"**

**对方要不要动**：不用。如果对方也在改 index.html，注意 `askLLMStream` 是**三个问答入口的公共出口**，改它要保证 `_aiTaskStart` / `_aiTaskEnd` 仍然成对。

### ③ 解不出的数学符号（用户原话："有×的，它都显示的是个？"）

- 根因：TeX 数学字体在 PDF 里没有 ToUnicode 映射 → pdfplumber 吐 `(cid:3)` →
  抽取层换成 `[?]`（**故意的**：防止模型把 cid 猜成 `\approx`/`\times` 编造公式）
- 现在：孤立符号打锚点标记 → 服务端裁图让多模态认字 → 存进 `logs/glyph_dict.json`
  → 之后查表（零成本、跨论文复用）
- **认不出就退回 `[?]`，绝不猜**；模型给的字符必须过白名单才进词典

**对方要不要动**：不用，但**要注意 `logs/glyph_dict.json` 是两台设备共用的**
（坚果云同步）→ 对方那边识别出的新符号会同步过来，直接命中。

---

## 3. API 契约的变化（对方如果要调这几个接口）

| 接口 | 变化 |
|---|---|
| `POST /api/paper/translate_page` | 响应多两个字段：`model`（**实际**用的模型）、`model_fell_back`（是否退回过） |
| `POST /api/paper/read_table` | 返回值会校验"像不像 Markdown 表格"，不像 → 502（而不是把一句说明当表格） |
| `POST /api/paper/explain_selection_stream`<br>`POST /api/paper/explain_stream`<br>`POST /api/paper/cross_qa_stream` | 都接受新的 `model` 字段（`""`=跟随默认 / `"agnes"`=备用 / `"custom:xxx"`=自定义） |
| `GET /api/llm/translation_models` | 选项里会多出**自定义模型**（来自 .env） |
| `GET /api/llm/status` | 多出 `custom_models`（含每个模型的 RPM 档） |
| 翻译缓存键 | 含模型名 → 换模型必然 miss（换模型=真重译），不串 |

---

## 4. 对方最容易踩的坑（都是这轮真实踩过的）

1. **EXTRACTOR_VERSION 现在是 5**。抽取算法改了（符号打标记）→ **旧快照会作废重翻**，
   这是**预期行为**，不是 bug。对方那边如果也改了抽取层，版本号要一起 +1，
   两边必须**逐字一致**（§4.18 那个快照版本闸门就是管这个的）。
2. **PyMuPDF 的 Rect 是左上角原点**（和 pdfplumber 一样），**不是** PDF 原生的左下角。
   按左下角换算会裁到旁边的文字（这轮实测裁出过 "va"/"se"，模型把乱图认成了 ν）。
3. **多模态调用要关思考**（`disable_thinking=True`）：主模型是推理型，思考会把
   token 预算吃光（实测 max_tokens=300 → 输出被截断、0 字符）。
4. **表格读图要跟随用户选的模型**：`_prepare_tables` 必须把 `model` 传下去。
   这轮之前**只有独立接口 `/api/paper/read_table` 传了，整页翻译那条主路径漏了** →
   手动选 Agnes 翻译时表格还偷跑 M3（既不一致又烧 M3 额度）。已补。
5. **三个 AI 入口的登记/收尾必须成对**：漏收尾 = 面板里永远挂着一个"还在跑"的任务。
6. **限流是按模型配的**（不再是"是不是免费版"二分档）：Agnes 严格档 11 RPM / 6 秒，
   付费模型（含自定义）默认 30 RPM / 2 秒。对方接自己的模型时注意别打爆对方限流。

---

## 5. 对方要接自己的 API 模型，怎么填

在**对方的** `.env` 里加（照抄模板，注释已在文件里）：

```env
LLM_CUSTOM_1_NAME=我的模型
LLM_CUSTOM_1_BASE_URL=https://api.deepseek.com/v1
LLM_CUSTOM_1_MODEL=deepseek-chat
LLM_CUSTOM_1_API_KEY=sk-xxxxxx
LLM_CUSTOM_1_RPM=30
```

填完**重启工作台**，模型就会出现在阅读器右上角的下拉框里，选中即用于
翻译 / 抄表 / 划词答疑。

**两个硬前提**（这轮实测出来的）：
- **OpenAI 兼容**（`POST {base_url}/chat/completions`）
- **支持图片输入（vision）** —— 表格靠多模态读图；纯文本模型抄不出表格
  （会走"表格未能读出，请对照 PDF 原文"的兜底，不会瞎编）

---

## 6. 对方 pull 之后的自检清单

```powershell
# 1) 拉代码（两台设备都在 main 上，冲突时先 pull --rebase）
git pull --rebase origin main

# 2) 离线测试必须全绿
E:\ruanjian\python.exe -m pytest tests -q -m "not network"     # 期望 301 passed / 5 skipped
node microbench\_test_reader_markdown.js                          # 15
node microbench\_test_reader_annotations.js                       # 29
node microbench\_test_reader_ai_activity.js                       # 25

# 3) 服务起来 + 前端拿到新代码
#    （改了 index.html 必须**重启桌面应用**，webview 只在启动时拉一次首页）
E:\ruanjian\pythonw.exe E:\zaiwu\个人工作台\desktop.py

# 4) 三处功能各点一次
#    - 打开一篇论文 → 翻译页签应显示「✅ 已翻译 · 模型名」
#    - 划词答疑 → 工具栏「🤖 AI 活动」应显示任务数与已用时
#    - 翻译一篇有 × ≈ ≥ 的论文（如 01_Literature\04_Survey_综述\...\2008-JACERS-综述-*.pdf
#      第 1 页）→ 译文里应是 "α_ME ≈ 20 mV/cm-Oe" 而不是 "α_ME [?] 20"
```

---

## 7. 本轮的实测数据（供对方对照，不用重跑）

- **能力矩阵**（JACERS 综述 p2，表格数值与论文逐格核对）：
  M3 翻译 15.2s / 抄表 3–6s；**M3.1-Flash 翻译 324s 撞线报 504**（只能答疑不能翻译）；
  Agnes 翻译 104s / 抄表 25–35s（数值仍准）
- **符号认字**：JACERS p1 的 ≈、≥、× 全部认对；首次 3.9s，之后走词典 0.0s
- **全库体检**：391 页，残留 `(cid:` 0 页，标记数与未解出字符数 0 处错配；
  长度体检的可疑页与改动前**逐字一致**（都是"参考文献页/表格页"本来就短）
- **AI 活动**：划词答疑实测「等待模型首个回答 3s」→「模型还在想 36s」→「✅ 完成 1m18s」

---

## 8. 还没做、留给以后的事（别当成遗漏）

- 自定义模型**只支持 OpenAI 兼容**协议；本地 Ollama / vLLM 的原生接口没做
- 符号词典是"形状 → 符号"，理论上不同字体里**形状相同**的符号会共用一条（≈ 与 ≃
  会被认成同一个）—— 精度够用，但不严格
- 章节视图与按页翻译各有一套符号认字入口（同一份词典），还没合并成一次调用
