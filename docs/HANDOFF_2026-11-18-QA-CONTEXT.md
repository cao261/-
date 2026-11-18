# 交接 · 2026-11-18 AI 答疑上下文改造（选中标题 → 总结这一小节）

> 用户原话：
> 「同步远程对 ai 解答功能的修改，并且优化现在给 ai 的上下文，确保每次能够正常解决问题，
> 　给的上下文内容长短合适，然后公式什么的也要正常能给，
> 　还有就是我一般的使用场景是选中一个标题，让 ai 先帮我总结一下这一小节的内容，
> 　要注意这个上下文的给法，使其能做到给我合适的阅读学习帮助」
>
> **「同步远程修改」部分的结论：远端没有新东西可拉。**
> `git fetch --all --prune` + `git ls-remote` 实测：
> 远端 `main` = `feat/audit-2026-10-27` = `9f01308`，**与本地 `main` 逐字一致**；
> `feat/zen-focus-mode` = `58760b5`，是 `main` 的**祖先**（`main` 上没有它缺的东西）。
> 另一台设备（25353）**没有绑定会话**（`mavis session list --source rc` / `--source ssh` 均空）
> → 它的本地未推送改动只能靠它自己推到 GitHub，本机看不到。
> → **本轮的重点全部落在「优化给 AI 的上下文」上。**

---

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| 远端同步 | **无新提交**（远端 = 本地 `9f01308`）；25353 无绑定会话 |
| 新增能力 | 「**选中一个标题 → 用本小节完整正文当上下文**」 |
| 新增端点 | `GET /api/paper/outline`（小节大纲，按 mtime 缓存） |
| 新增抽取函数 | `paper_text.extract_outline()`（标题 + 本节正文 + 本节公式坐标） |
| 新增 scope 档 | `section` = 16K（前端 `_SCOPE_CAPS` 与后端 `scope_caps` **逐字一致**） |
| 新增护栏 | `tests/test_qa_section_context.py`（17 条，真打端点 + 真抽 PDF）<br>`microbench/_test_qa_section_scope.js`（16 条断言 + **8/8 变异自检**） |
| 真实论文实测 | JACERS 综述 8 页 → **13 个小节，13/13 全中，0 误判**；耗时 1.9s（服务端缓存） |
| 平板端 | **本轮未做**（平板访问不到桌面后端，见 §5） |

---

## 1. 改之前是什么样（为什么必须改）

在 PDF 双栏阅读器里划选一个标题（"II. Experimental Procedure"）问「总结这一节」：

| 环节 | 改之前 | 问题 |
|---|---|---|
| 上下文锚点 | 固定是「选中页」的整页文本（`[Page 3]` 开头） | 跟「用户选的是标题」毫无关系 → 没有「这一节」的边界 |
| 长度 | `mid` 档 8K，**纯头部截断** | 这一节往往在整页靠后 → **这一节的后半段直接没了** |
| 公式 | 只给**选中页**的公式图 | 小节跨页 → 后半页的推导模型看不到 → 又去猜（§4.97 那个坑） |
| 提示词 | 「用户划选片段」 | 服务端不知道用户选的是标题 → 模型以为用户划了半句话 → 去猜、去编 |

---

## 2. 现在的链路

```
用户在 PDF 里划选一个标题
        │
        ▼
前端 _matchQaHeading(选区)                     ← 归一化后跟大纲标题匹配
        │ 命中
        ├──► scope = {mode:"section", budget:16000}
        │    （**跳过 LLM 路由**：省一次 3~8s 往返，且「总结这一节」该给多少上下文是确定的）
        │
        ├──► _buildSectionContext()
        │    ├─ 【小节】II. Experimental Procedure（第 1-2 页）
        │    ├─ 【本小节中文译文（机器翻译，供快速理解）】  ← 40% 预算
        │    └─ 【本小节英文原文（权威表述）】              ← 60% 预算
        │
        └──► payload: selection_kind=heading / heading_title / section_pages
                     / formula_rects_with_page=[页码,x0,y0,x1,y1]
                                    │
                                    ▼
                          服务端 /api/paper/explain_selection_stream
                            ├─ scope_caps["section"] = 16000（上限）
                            ├─ 提示写明「用户选中的是本小节标题，下面给的是本小节完整正文」
                            ├─ 上下文标签换成【本小节完整正文】
                            └─ 按页裁公式高清图（scale=4），标签带页号，
                               并说明「正文里的【公式N】与这些图按顺序一一对应」
```

### 2.1 大纲从哪来（为什么必须在服务端）

| 候选来源 | 能不能用 | 原因 |
|---|---|---|
| 前端 PDF.js 文本 | ✗ | `items.map(it => it.str).join(" ")` —— **没有行结构、左右栏交错**，认不出哪一行是标题 |
| 段落块层 `extract_page_blocks()` | ✗ | **会漏行**：JACERS 第 1 页的 "I. Introduction" / "II. Experimental Procedure" 在块层里**根本不存在**（被水平带裁没了）→ 见 AGENTS.md §4.105 |
| **页级文本 `_extract_page_text()`** | ✓ | 本来就按阅读顺序拼好，标题是独立的一行，公式标记（⟦FORMULA:…⟧）也在里面 |

### 2.2 标题识别（保守优先）

`_outline_heading_title()` 六道闸（详见 AGENTS.md §4.106）：
编号式开头 / 首字大写 / 至少一个小写字母 / 词数 ≤ 12 / 不含作者名与 `et al` /
**不含小写动词**（挡掉 `3 Measurements were performed on the samples.`）。

**实测（JACERS 综述 8 页）**：13 个真实小节 **13/13 全中，0 误判**；
论文末尾的参考文献段被识别出来并**整段排除**（书目有 10023 字，进了上下文纯浪费）。

---

## 3. 关键文件与行号（main 分支）

| 文件 | 位置 | 说明 |
|---|---|---|
| `microbench/paper_text.py` | `extract_outline()` / `_outline_heading_title()` | 小节大纲抽取 |
| `microbench/app.py` | `GET /api/paper/outline` | 大纲端点（`_OUTLINE_CACHE` 按 mtime 缓存，最多 6 篇） |
| `microbench/app.py` | `ExplainSelectionRequest` | 新增 `selection_kind / heading_title / section_pages / formula_rects_with_page` |
| `microbench/app.py` | `_sanitize_formula_rects_with_page()` | 坐标校验（客户端可控入参） |
| `microbench/app.py` | `_render_formula_crops_by_page_b64()` | 跨页公式裁图（一次 open，裁全部） |
| `microbench/app.py` | `api_explain_selection_stream` | `scope_caps["section"]=16000` + 标题场景提示 + 附件标签带页号 |
| `microbench/static/index.html` | `_matchQaHeading()` / `_buildSectionContext()` / `_clipQaContext()` | 标题命中 + 小节上下文 + 头尾截断 |
| `microbench/static/index.html` | `submitReaderQA()` | 命中标题 → 跳过路由；载荷加 4 个字段 |
| `microbench/static/index.html` | `_refineQA()` | 重新回答也带上标题定位信息 |
| `microbench/static/index.html` | `loadPdfDualColumn()` | 打开 PDF 时后台预取大纲 |

---

## 4. 复现与验收

```powershell
# 后端护栏（17 条：真抽 PDF + 真打端点）
python -m pytest tests/test_qa_section_context.py -q

# 前端行为级护栏（16 条断言 + 8/8 变异自检）
node microbench/_test_qa_section_scope.js

# 真论文的大纲（不依赖 LLM）
python -c "import sys;sys.path.insert(0,'.');from microbench.paper_text import extract_outline;from pathlib import Path;print(extract_outline(Path(r'01_Literature/04_Survey_综述/01_论文原文_PDF/2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf'))['sections'])"

# 端到端（服务重启后）
Invoke-RestMethod 'http://127.0.0.1:5000/api/paper/outline?relative_path=01_Literature/04_Survey_综述/01_论文原文_PDF/2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf'
```

---

## 5. 没做的事（明确的差距）

| # | 事项 | 为什么没做 | 怎么补 |
|---|---|---|---|
| 1 | **平板端没有「选中标题 → 小节上下文」** | 平板**访问不到桌面后端**（无 baseURL / CSP connect-src / 原生桥白名单三处拦着），而大纲必须由服务端从 PDF 算 | 在 `export_offline.py` 导出时把大纲**烤进平板包**（跟 `trans_seed` 一样），平板端再做同样的标题匹配 |
| 2 | 标题**折行**时标题文本不全（"…Epitaxial Thin" / 下一行 "Layers: Future Directions"） | 合并折行要靠字体信息（同一字体），本页没有可靠信号 | 用词级 `fontname` 判定续行；匹配用**包含关系**，所以不影响命中，只是标题显示短一截 |
| 3 | 段落块层**漏行**（两个标题在对照模式里没有位置） | 修块层要 `EXTRACTOR_VERSION` +1 → **全部已翻段落作废重翻**＝烧钱 | 单独排期；本轮只记录（AGENTS.md §4.105） |
| 4 | 平板端原有 6 项能力差距（P0-2/P0-3/P1-5/P1-6/P2-7~12） | 上一轮已列出，**等用户拍板** | 见 `docs/HANDOFF_2026-11-17-FULL-FUNCTIONAL-AUDIT.md` §4 |
| 5 | `tests/test_apk_delivery.py::test_translations_are_baked_in` 仍红 | 缺两份快照的译文烤录，补它要重翻＝烧 token | 等用户拍板 |

---

## 6. 给下一位接手的人

1. **改上下文相关代码时，务必同步改 `_SCOPE_CAPS`（前端）与 `scope_caps`（后端）**，
   两处数字必须**逐字一致** —— 徽标显示的字数要等于真发出去的字数。
2. **命中标题时不要又去问 LLM 路由**：路由可能把上下文降级成「紧邻 800 字」。
3. **公式坐标一定要带页码**（`formula_rects_with_page`）：小节是跨页的。
4. 上下文截断一律走 `_clipQaContext()`（头尾都留 + 明示省略），
   **别再写 `slice(0, budget)`** —— 小节的结论在结尾。
5. 想动 `paper_text.py` 的抽取行为 → 先看 `EXTRACTOR_VERSION` 的规矩（§4.99 之前的章节）：
   改抽取 = 旧译文作废 = 用户重翻 = 烧钱。
