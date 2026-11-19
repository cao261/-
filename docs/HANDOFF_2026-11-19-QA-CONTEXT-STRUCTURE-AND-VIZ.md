# 交接：AI 上下文的「分段结构」+ 可视化面板（2026-11-19）

> 这一轮的主线是用户那句：**「继续做给 ai 上下文的优化和可视化，
> 确保正常划线问问题，ai 都能得到合适的上下文」**。
> 平板端本轮**不动**（用户 2026-11-18 已拍板）。
> 坑记在 `AGENTS.md §4.117~4.119`。

---

## 0. 一句话结论

这一轮把**送给 AI 的上下文**从「一坨字符串」改成了「**分段结构**」，并加了一个
**用户能点开看的可视化面板**。

实测效果（同一篇论文、同一条划线）：

| 档 | 改前 | 改后 |
|---|---|---|
| `section` | 5310 字，用户划的那句落在 **95.4%** 处 | 5358 字，那句在 **0.5%**（前置到第一段） |
| `wide` | 6694 字，**「Full Text 一桶」**，无小节结构 | **2910 字，3 个真小节**（摘要 + 选区所在小节 + 结论） |
| `full` | 43767 字，**「Full Text 一桶」**，尾部是参考文献 | **33109 字，13 个真小节**，尾部是**真结论** |
| `mid` | 纯头部截断 → 划在页面后半部时**选区句可能根本不在上下文里** | 真小节正文 + 中文译文；选区句**必在**上下文里 |

---

## 1. 先取证，别先改：逐档体检「实际送出去的是什么」

写了个探针 `tmp/qa_ctx_probe.py`：**抓真实请求载荷**（Playwright 拦截
`POST /api/paper/explain_selection_stream`），并用前端调试面板的
`[data-refine="up"/"down"]` 切档，逐档看**真正发出去的字符串**。

```powershell
python 'C:\Users\30461\.minimax\tmp\qa_ctx_probe.py'
```

**四个真问题**（详见 §0 表）。最刺眼的是 `section` 档：
用户划的那句落在 **95.4%** —— 模型读完 5000 字才看到用户问的那半句话。

---

## 2. 根因：上下文是「一坨字符串」

原来的上下文是 `join("\n\n")` 之后就**没了结构**的字符串 →
- 没法标注每段的**来源与形态**（真小节 / 整页译文 / PDF.js 扁平文本）；
- 没法把用户关心的那一句**提前**；
- 没法给用户**看**。

---

## 3. 修法：上下文改成「分段结构」

全部在**前端 `microbench/static/index.html`**（本轮**没动后端** → 服务不用重启）。

### 3.1 三个基础件

| 函数 | 作用 |
|---|---|
| `_qaCtxPart(label, kind, text)` | 一段：标签 + 类型 + 正文 |
| `_qaCtxJoin(parts)` | 拼成最终字符串（`label\ntext`，段间空行） |
| `_qaCtxParts(parts, budget)` | **分段各自裁剪**，总和 `budget - 600` |

⚠ **`budget - 600` 是给标签和分隔符留的位置** ——
不然外层那层全局裁剪会再砍一刀，**把小节结尾的结论砍掉**（§4.107）。

### 3.2 各档的新语义

| 档 | 上下文内容 |
|---|---|
| `section` | 本小节（中文译文 40% + 英文 60%）；**未命中就落回 `mid`，绝不交空上下文** |
| `term` | 用 `_qaSearchableText()`（真小节优先）切窗 |
| `mid` | 真小节正文（按论文阅读顺序）＋ 用户正在读的**中文译文**（标注「整页翻译」）；<br>没有大纲才退回 PDF.js 扁平页文本，并**如实标注形态** |
| `wide` | 摘要/前言 ＋ **选区所在整个大节（顶层节 + 它的所有子节）** ＋ 结论/末节 |
| `full` | **全部**真小节（不再只前 8 节、不再一桶） |

### 3.3 新的取材工具

- `_qaAnchorText()` → 当前选区 + 它在哪（页/小节）；
- `_qaPageDesc(sec)` → 「（第 1-2 页）」；
- `_qaSectionsForPage(pageNum)` → 这一页涉及的真小节；
- `_qaSearchableText()` → 可切窗的全文（**真小节优先**，无则 PDF.js 扁平文本）；
- `_qaSectionFamily(outline, cur)` → 顶层节 + 它的所有子节。

### 3.4 选区句前置

`_buildScopeContextBundle(scope)`：

```
已在上下文的前 35% 内 → 不重复
否则 → 前置一份（几十字，成本极低）
```

标签：**「【用户划选的片段（请围绕它回答；完整上下文见下）】」**，`kind:"selection"`。

> 这条是「确保划线问问题」最强的保证：
> **无论哪一档，用户划的那句一定在上下文里。**

---

## 4. 可视化面板

`_renderQaContextBox(answerBody, parts, text)` → 在徽标行下、答案上插一个
折叠面板 `<details data-qa-context-box>`：

1. **摘要**：「🔍 本次送给 AI 的上下文 · 5,358 字 · 4 段（点开看模型到底看到了什么）」
2. **分段清单**：每段「标签 + 字数」
3. **实际发出的原文**：`<pre>` 限高 300px 滚动，**用 `textContent` 不用 `innerHTML`**

⭐ **面板渲染的就是 `parts`/`text` 本身**（不是另算一份）→
**面板里看到的就是发出去的**，不会「面板说一套、发出去另一套」。

真机复验（`tmp/live_qa_ctx_viz.py`）：

```
徽标：🧭 AI 路由 · 整个小节 (~12,000 字) · 实际 5,358 字
面板：<DETAILS> 存在
摘要：🔍 本次送给 AI 的上下文 · 5,358 字 · 4 段（点开看模型到底看到了什么）
分段清单：
   · 【用户划选的片段（请围绕它回答；完整上下文见下）】（20 字）
   · 【小节】II. Experimental Procedure（第 1-2 页）
   · 【本小节中文译文（机器翻译，供快速理解）】（4,188 字）
   · 【本小节英文原文（权威表述；公式位置为【公式N】占位，对应的公式高清原图见附件）】（1,015 字）
实际发出的上下文：scope=section  字数=5358  选区句位置=第 0.5% 处
```

截图：`logs/blockdiag/qa_context_viz.png`。

---

## 5. 顺带修的两个真 bug（§4.118）

### 5.1 `_qaSectionFamily` 的大节永远取不到

`outline.indexOf(cur)` —— 但 `cur` 是 `_qaSectionBySelection` 造的**副本**
（来自 `_qaSectionPayload()`）→ `indexOf` **永远返回 -1**。
→ 改成按「**标题 + 起始页**」认回去。

### 5.2 `mid`/`wide` 档盖不住 PDF.js 扁平文本

`_qaSectionsForPage` / `_qaSectionBySelection` 的结果被**扁平页文本**盖住 →
明明有真小节却用了扁平文本（左右栏交错、公式碎片 `k 50.94.20`）。
→ 真小节优先。

### 5.3 通用纪律

- **消歧/取材一律用「位置」（页码、坐标、偏移），不用「体量」（长度）** ——
  长 ≠ 具体，一段长正文往往横跨好几个小节。
- **消歧失败要老实返回「不知道」**，让上层退回更粗的上下文 ——
  **宁可给当前页，也不给另一节**。
- **有更精确的来源时，不要用更粗糙的来源**。

---

## 6. 护栏

### 6.1 `microbench/_test_qa_section_scope.js`

24 → **46 条断言**；变异 11 → **19 条（19/19 全被抓住）**。

新增断言覆盖：
- `wide` 用真小节（不是 Full Text 一桶）
- `full` 给**全部**小节
- `mid` 用真小节
- 无大纲时**如实标注**形态
- **选区句前置**
- **所有档位选区句必在上下文里**
- 可视化面板内容正确
- `wide` 给**整个大节**（`OUTLINE_TREE` fixture）
- `mid` 带中文译文
- `term` 用真小节切窗

### 6.2 `microbench/_test_qa_formula_frontend.js`

- 切片补齐 13 个新依赖（§4.109 的坑：本轮又踩了两次）；
- 加 `sliceBalanced`；
- DOM 桩加 `createElement` / `appendChild`；
- 8 PASS / 变异 6-6。

### 6.3 ⚠ 变异目标串必须**唯一**（§4.119）

想变异 `mid` 分支，脚本写 `_qaSearchableText()` → `_qaAnchorText()`，
但这个串在源码里出现 **2 次**（`term` 和 `mid` 分支），
JS `String.replace` **只替换第一处** → 改的是 `term` → 护栏红了但是**假阳性**，
`mid` 的缺陷根本没测出来。

→ 变异脚本新增前置校验：**目标串出现 >1 次 → 直接判失败**。

---

## 7. 复现命令

```powershell
# 服务（本轮只改前端，no-store，刷新即生效；没重启也 OK）
# 护栏
node microbench/_test_qa_section_scope.js      # 46 PASS / 变异 19-19
node microbench/_test_qa_formula_frontend.js   # 8 PASS / 变异 6-6
python -m pytest tests -q -m "not slow"        # 全量
python -m microbench._test_regressions

# 真机
python 'C:\Users\30461\.minimax\tmp\live_qa_ctx_viz.py'   # 可视化面板 + 上下文
python 'C:\Users\30461\.minimax\tmp\qa_ctx_probe.py'      # 逐档体检
```

测试论文：`01_Literature/04_Survey_综述/01_论文原文_PDF/2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf`（8 页，13 节）。

---

## 8. 下一台设备要注意的

1. **本轮只改前端** → 合并时不会和后端冲突（但 `app.py` 上轮改过 router，注意别回退）。
2. **25353 上的提示词改动本机看不到**（没推 GitHub、无绑定会话）→ 将来合并要人工对齐。
3. **平板端本轮没动** —— 用户 2026-11-18 已拍板「平板端本轮先不动」，
   **下一轮别再当"应该补"重复提**。
4. **`_SCOPE_CAPS` 前后端必须逐字一致**（`QA_SCOPE_CAPS` ↔ `_SCOPE_CAPS`）——
   护栏是**运行时值跨端比对**，不是 grep 源码。改了一边必须改另一边。
