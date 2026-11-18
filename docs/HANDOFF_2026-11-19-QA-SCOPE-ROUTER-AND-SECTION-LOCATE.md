# 交接：scope router 重写 + 「划正文定位到哪个小节」纠错（2026-11-19）

> 这一轮的主线是用户那句：**「确定 ai 回答问题这块切实有效，上下文截取送给 ai 的准确性」**。
> 平板端本轮**不动**（用户 2026-11-18 已拍板）。
> 坑记在 `AGENTS.md §4.114~4.115`。

---

## 0. 一句话结论

这一轮做了两件事，都用**真数据**验证过：

1. **scope router 重写**：后端提示词里**根本没有 `section` 档**（前端 `_SCOPE_CAPS` 早就有），
   所以「这一节讲了什么」被判成 mid（当前页）→ 小节跨页时**后半页漏掉**。
   判据也从「问题看着多复杂」改成「**证据在论文里的位置**」。
2. **修一个真 bug（真机实测抓到）**：同一句正文出现在多个小节里时，
   旧判据「取正文最长的」→ **永远定位到最长那一节** → AI 总结的是**别人那一节**。
   改成「**页码过滤 → 版面几何 → 定不了就不猜**」三层。

---

## 1. scope router 重写（§4.114）

### 1.1 审计发现（`logs/qa_router/{before,after,after2,after3}/`，12 道真实选题）

| 症状 | 根因 |
|---|---|
| 「这一节讲了什么」→ 判 mid | **router 提示词里没有 `section` 档** —— 前端 `_SCOPE_CAPS` 早就有，后端没有 |
| 是/否问题也给 6000 字 | 提示词写着「若问题开放、不能确定 → **选更大一档**」 |
| 徽标说 60000 字，实际发 50000 | router 给的 budget **超该档上限**（full 给了 60000 > cap 50000） |

### 1.2 第一版改法**矫枉过正**（值得抄的一个教训）

改成「吃不准就选**小**档」→ 「公式推导」「能不能用在我自己的器件上」被压到 `term`(1200)
→ **证据不够**（这两个问题本来就要整节）。

→ **「拿不准就升档 / 拿不准就降档」都是拍脑袋**。定稿判据改成按**证据位置**：

| 问题长什么样 | 证据在哪 | 档 |
|---|---|---|
| 「这个词是什么意思」 | 只在选区里 | `term` (2000) |
| 「这一节/这一段讲了什么」 | 需要**跨页** | `section` (16000) |
| 「前后两节对比一下」 | 需要**多个小节** | `wide` (20000) |
| 「全文的整体脉络」 | 整篇 | `full` (50000) |
| 「能不能搬到我自己的器件上」 | 要的是**这一节的完整内容** | `section`（**不是**因为「问题开放」就升档） |

### 1.3 实测

12 题**与期望一致 10/12**，budget 超 cap **0**。
「总结本节/本节方法」正确落 `section`(12000)、「简单是与否」落 `term`(1200)、
「公式推导」落 `mid`(6000)、「跨节对比」落 `wide`(16000)。

### 1.4 改了什么

**后端 `microbench/app.py`**
- `QA_SCOPE_ROUTER_PROMPT` 重写（5 档，含 `section`；判据按证据位置；明确写「
  **不要为了保险选更大一档**」）；
- 新增模块级 `QA_SCOPE_MODES`（白名单）/ `QA_SCOPE_CAPS`（各档上限）；
- `POST /api/paper/classify_qa_scope`：`mode` 白名单认 `section`；
  **budget 按该档 cap 夹住**（router 说 60000 → 实发 50000）；
- `POST /api/paper/explain_selection_stream` 的 `scope_caps` 改为引用 `QA_SCOPE_CAPS`。

**前端 `microbench/static/index.html`**
- 白名单提成具名常量 `_QA_ROUTER_MODES`，`_routeQuestionScope` 用它；
- `_SCOPE_CAPS` 保留（值与后端逐字一致）。

### 1.5 ⚠ 前后端白名单是**契约**，必须有自动比对的护栏

不一致的后果是**静默**的：后端返回 `section` → 前端判「非法」→ 抛错 →
**静默退回关键词启发式**（用户只看到徽标写「关键词 fallback」，不知道发生过什么）。

→ 抽成**具名常量**（后端 `QA_SCOPE_MODES`/`QA_SCOPE_CAPS`，
前端 `_QA_ROUTER_MODES`/`_SCOPE_CAPS`）；
护栏用**运行时值跨端比对**（node 护栏把前端常量吐出来给 pytest 比），
**不是 grep 源码**（grep 挡不住「值一样但 key 拼错」和「两处各写各的」）。

---

## 2. 「划正文定位到哪个小节」纠错（§4.115）

### 2.1 真 bug

JACERS 综述里「**magnetostrictive one**」这一句**同时**出现在三个小节里：

| 小节 | 页范围 | 正文字数 |
|---|---|---|
| I. Introduction | p1–p1 | 3758 |
| II. Experimental Procedure | p1–p2 | 1015 |
| (3) Resonance Enhancement | p7–p7 | 2492 |

旧判据（`_findQaSectionBySelection`）：「多个小节都包含时取**正文最长**的那个（最具体）」
→ **永远选 I. Introduction**。
→ 用户在**第 2 页**划的是 II. Experimental Procedure 里那句 → AI 却在总结
**I. Introduction** → 「这一节讲了什么」答的是**别人那一节**。

> 「取最长」看着像「取最具体的」，其实是「取最大的」—— **长 ≠ 具体**。
> 这跟 §4.6 是同一条：**给错内容比不给更糟**。

### 2.2 改成三层（按位置，不按体量）

1. **候选集**：选区文本是哪些小节正文的一部分；
2. **页码过滤**：候选里**覆盖划词那一页**的才可能是答案
   （页码来自 `_readerSelState.pageNum`，划词时就有）；
3. **版面几何**：还剩多个 → 选区落在**哪个段落块**
   （服务端 `page_blocks` 的 bbox，页面绝对 pt）→ 那个块属于哪个小节；
4. **定不了就返回 null**（落回 `mid` 档给当前页），**不猜**。

### 2.3 几何判定要三样东西

| 要什么 | 从哪来 | 备注 |
|---|---|---|
| 选区位置 | `_readerSelState.rects`（**归一化**矩形） | 划词时就有，原来只用来画批注高亮 |
| 页面物理尺寸 | `renderPdfPage` 里把 `viewport0.width/height`（pt）存进 `_pdfState.pages[i].ptW/ptH` | 归一化矩形 × 物理尺寸 = 页面 pt |
| 段落块 | `ensurePageBlocks(pageNum)` → `/api/paper/page_blocks` | **现成端点**，抽取层同一套 pdfplumber |

块 → 小节的映射：取块文本**前 30 个归一化字符**去候选小节正文里找，
**必须唯一命中**（不唯一 → 认不出）。

### 2.4 实测（真浏览器，同一句、三个页）

| 划的是 | 修前定位 | 修后定位 |
|---|---|---|
| 第 1 页那句 | I. Introduction | **I. Introduction** ✓ |
| 第 2 页那句 | I. Introduction ✗ | **II. Experimental Procedure** ✓ |
| 第 7 页那句 | I. Introduction ✗ | **(3) Resonance Enhancement** ✓ |

第 2 页那次的答案里，模型自己说对了：
> 「你划选的 "magnetostrictive one" 只是公式(2)后变量解释句里
> "the index m designates the magnetostrictive one" 的一部分」

---

## 3. 改了哪些文件

| 文件 | 改了什么 |
|---|---|
| `microbench/app.py` | `QA_SCOPE_ROUTER_PROMPT` 重写；新增 `QA_SCOPE_MODES` / `QA_SCOPE_CAPS`；router 端点白名单 + budget 夹紧；答疑端点 `scope_caps` 引用常量 |
| `microbench/static/index.html` | `_QA_ROUTER_MODES`；`_findQaSectionBySelection` 改三层定位（新增 `_qaSectionPayload` / `_qaSectionCoversPage` / `_qaSelectionPointPt` / `_qaBlockAtPoint` / `_qaSectionByGeometry`）；`renderPdfPage` 返回 `ptW/ptH`；`submitReaderQA` 传页码 + 选区矩形 |
| `microbench/_test_qa_section_scope.js` | 24 → **36 条断言**；变异 11 → **15 条**；新增 `SCOPE_CAPS_JSON` / `ROUTER_MODES_JSON` 输出供跨端比对 |
| `microbench/_test_qa_formula_frontend.js` | 切片补齐新依赖（§4.109 同款坑）+ `ensurePageBlocks` 桩 |
| `microbench/_test_regressions.py` | 「各档上限」那 4 条**源码文本护栏**改成**行为级**（真打端点 + 拦下真正发给模型的消息数字数）+ 变异自检（`term` 改 9999 → 红 ✓）→ 40 → **46 passed** |
| `microbench/app.py` | `force_scope` 字段说明补上 `section` 档（原来只写 term/mid/wide/full） |

⚠ **顺带踩到一个很隐蔽的坑（§4.116）**：变异脚本把 `term` 从 `2000` 改成 `9999`
（同长度）又**在同一秒内**还原 → 源文件 mtime 与 size 都没变 →
`__pycache__` 的字节码被判**仍然有效** → 之后 import 到的**是变异版**，
而源码里明明是 2000。**任何「改文件 → 跑 → 改回来」的脚本，
还原后必须 `os.utime` + 删 `.pyc` + import 一次校验运行值。**
| `tests/test_qa_scope_router.py`（新） | 7 条：router 认 section / budget 按 cap 夹 / 非法 mode 拒 / 提示词列 5 档 / **前后端白名单与上限逐字一致**（运行时值比对） |
| `AGENTS.md` | §4.114~4.115 + 顶部索引 |

---

## 4. 复现命令

```powershell
# 服务（改过 app.py 必须重启）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*uvicorn*microbench.app*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process -FilePath (Get-Command python).Source `
  -ArgumentList '-m','uvicorn','microbench.app:app','--host','127.0.0.1','--port','5000','--log-level','warning' `
  -WorkingDirectory 'D:\Desktop\杂物\琐事\个人工作台' -WindowStyle Hidden

# 护栏
node microbench/_test_qa_section_scope.js      # 36 PASS / 变异 15-15
node microbench/_test_qa_formula_frontend.js   # 8 PASS / 变异 6-6
python -m pytest tests -q -m "not slow"
python -m microbench._test_regressions          # 46 passed
```

真机脚本（临时目录 `C:\Users\30461\.minimax\tmp\`，**不在仓库里**）：
`live_qa_page_section.py <页码>` —— 在指定页划「magnetostrictive one」并核对落地小节；
`qa_span_probe.py` —— 探针：这句话在 PDF.js 文本层里的页/坐标；
`qa_router_audit2.py` —— router 审计（12 道真实选题）。

---

## 5. 给接手的人的三条提醒

1. **消歧要用「位置」，不要用「体量」**。字数最长 ≠ 最具体（§4.115）。
2. **定不了就老实返回「不知道」**，让上层退回更粗的上下文；
   宁可给当前页，也不要给**另一节**（§4.6 / §4.115）。
3. **前后端共享的白名单 = 契约**，必须有一条**自动比对**的护栏，
   靠人记着「两边都要改」迟早会漏（§4.114）。

---

## 6. 本轮**没做**的（别当成漏做）

| 项 | 原因 |
|---|---|
| 平板端「选中标题 → 小节上下文」与 6 项能力差距 | **用户 2026-11-18 拍板「平板端本轮先不动」** → 主动搁置 |
| 跨设备同步端到端验证 | `/api/sync*` 会写坚果云共享目录、影响 25353 → 本轮跳过 |
| files 通道验证 | 本机 `SYNC_PDF_BODY=0` |
| 平板真机验证 | 本机无平板设备 |
| 25353 上的提示词改动对齐 | 对方**没推 GitHub**、**无绑定会话** → 本机看不到，将来合并要人工对齐 |
