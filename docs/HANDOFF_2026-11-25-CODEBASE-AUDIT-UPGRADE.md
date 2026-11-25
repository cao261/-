# 交接文档 · 2026-11-25 全代码库深度审计 + 全量升级

> 这份文档给**另一台设备（25353）**的 agent 看，也给以后接手的 agent 看。
> 状态：代码在本机改完并验证；**未推 GitHub**（推之前先看 §6）。

---

## 0. 一句话

用户给了一份「全代码库深度审计与全量升级蓝图」，本轮把它**落成了可执行的改动**：
P0 修 5 条、P1 修 12 条、新增 2 套行为级护栏、测试提速一半、
并**解除了平板/APK 的冻结**（实测私钥没丢，可覆盖升级）。

---

## 1. 本轮改了什么（按域）

### 1.1 后端（`microbench/app.py` / `translation.py`）

| 缺陷 | 修法 | 位置 |
|---|---|---|
| **P0** 多模态抄的表格永远贴不回原位 | `sanitize_translation` 不再提前销毁 `⟦TABLE_n⟧`；新增 `translation.apply_table_map(text, table_map)`，**清洗 → 回填**两段路径（主路径 + 漏译重试路径）都走它 | `translation.py` `_normalize_formula_markers` / `apply_table_map`；`app.py` `api_translate_page` / `api_translate_section` |
| **P0** 快照推入缺乐观 CAS | 走 `_cas_update_json`（指纹取在读之前，撞满 3 轮放弃） | `app.py` 快照推入路径 |
| **P0** `_vault_pdfs` 把「中译替换版」生成物当论文原文 | 收窄到 `generator.VAULT_CONTENT_DIRS`，并排除 `_REPLACED_DIR_NAME` | `app.py` `_vault_pdfs` |
| P1 替换版版本闸门只看**第一页** | 改成**逐页**校验 `blocks_version`，混版快照直接 409 | `app.py` `api_build_replaced_pdf` |
| P1 翻译缓存批量查询用**前端空串模型名**查缓存 | 先 `_resolve_translation_model()` 归一化再查 | `app.py` `api_translation_cache_batch_lookup` |
| P1 `async def` 端点里同步做重活 | `api_build_replaced_pdf` 改 `def`（由 FastAPI 线程池执行） | `app.py` |

### 1.2 前端（`microbench/static/index.html`）

| 缺陷 | 修法 |
|---|---|
| **P0** Toast 被阅读器整块盖住 | `:root` 加 **z-index 阶梯 token**；`#toast` → `var(--z-toast)`（2400） |
| **P0** 夜间模式深色卡片隐形（1.07:1） | 夜间重设 `--dark-text` / `--dark-text-muted`；卡片里的 label / placeholder 也改用 `--dark-text-muted` |
| P1 `await r.json()` 在 `r.ok` 之前 | 两处都改走 `_readJsonSafe` + `_httpErrorMessage` |
| P1 失败的页被算进「已完成」 | 拆 `blockTranslated`（只在成功时置位）/ `blockFailed`（队列跳过） |
| P1 编辑已有批注被当前选区覆盖坐标 | 编辑时**沿用原批注**坐标，只有新建才取当前选区 |
| P1 表格里的 `\|` 把公式劈开 | 切单元格前把公式/行内代码占位换成无 `\|` 的 token，切完换回 |
| P1 答疑登记成**翻译**模型 | 改用 `_qaModel`（答疑模型与翻译模型本来就是分开的） |
| P1 答疑选区竞态 | 函数开头冻结 `_selSnapshot`，全程只用快照 |
| P1 缩放无防抖 | 缩放重绘走 220ms 防抖（连点只重绘一次） |
| P1 队列 timer 从不清理 | 暂停时 `clearTimeout` |
| P1 一次 Esc 关掉整个阅读器 | **统一 Esc 栈**（`_escRegister/_escBind/_escUnbind` + 单一 document 处理器 + `stopImmediatePropagation`） |
| P1 画布显存 880MB | 整页位图**按需渲染 + 远端释放**（IntersectionObserver），关窗/缩放时取消 RenderTask |

### 1.2.1 画布那一条：**真机端到端**才测出护栏抓不到的洞

第一版实现是「画布创建时按尺寸分配 → 不画也占显存」——
护栏看「这一页画了没有」，位图尺寸照样 733×948，**显存一点没省**。
`<canvas width=733 height=948>` **一创建就分配 backing store**（≈2.7MB/页，
DPR=2 时 11MB），不是「画了才分配」。

修法：画布创建时 `width/height` **先归零**（不分配显存），需要画时由
`_pdfPaintCanvas` 按 `renderViewport` 还原；CSS 尺寸走 `style.width/height`
→ 版面/滚动/续读回位全部不受影响。

**真机脚本**：`microbench/_live_canvas_release_test.py`
（真服务 `127.0.0.1:5000` + 真 PDF.js + 36 页的 JAP 综述）：
① 页块 36 个全在 → ② 第 1 页有位图 → ③ 远处页 `canvas.width === 0`
→ ④ 滚过去自动画回来 → ⑤ 文本层还在（划词/批注不受影响）→ ⑥ 无 JS 报错。
**6/6 全绿**（坑记 **§4.145**）。

### 1.3 测试与工具链

- `microbench/_headless_browser.py`：**优先 Playwright Chromium**（系统 Edge headless
  单次卡 16s，Chromium 只要 0.46~0.9s），并缓存「可用 (exe, flag)」组合。
  → **全量测试 11.5 分钟 → 6m15s**。
- 新增 `microbench/_test_reader_overlay_render.py`：真实浏览器量
  **Toast 层级 / 夜间对比度 / Esc 栈 / 缩放防抖**，4 条变异全部被抓住。
- 新增 `tests/test_vault_pdf_scope.py`：vault 论文清单的口径。
- `microbench/_test_reader_markdown.js`：补「公式里的竖线」用例 + 变异自检（20 passed）。
- 修了三个被我改动波及的旧探针（桩件缺 `_qaModel` / `_pdfZoomReloadTimer` /
  变异目标串要跟着真代码走）—— **改真代码时记得连护栏一起改**。

---

## 1.4 ⚠ 提交后的补修：**两处「护栏绿了但用户那边还是坏的」**

> 这一节是本轮**最有价值的部分** —— 两笔 commit 落库后，真机复查发现的。
> 两条都是同一个病根：**判据/断言停在了用户不走的路径上**。

### ① P0「生成物当论文原文」只修了一半（坑记 §4.146）

真机一查 `/api/paper/list_vault_pdfs`：**27 篇里混着 2 篇中译替换版**。

**根因**：第一版只改了 `_vault_pdfs()`（**同步通道**用的清单），
而前端「📂 从仓库论文库打开」走的是 `api_list_vault_pdfs` ——
那个端点**自己 `rglob` 一遍**，压根没调那个排除。
第一版护栏 `tests/test_vault_pdf_scope.py` 也**只测 `_vault_pdfs()`**
→ 守的是一条用户不走的路径 → 绿得毫无意义。

顺带查出**第三处**也没排除：`api_find_pdf`（按标题找本地 PDF）
→ 搜「JACERS 综述」能直接搜出替换版。

**修法**：
1. 判据**收敛成单一函数** `app._is_replaced_output_pdf(parts)`；
   常量归到 `generator.VAULT_REPLACED_PDF_DIR`（谁要判都从这一处取，
   `services/vault_index.py` 里那个写字面量的也一并改掉）；
2. **三个扫描点全部走它**：`_vault_pdfs()` / `api_list_vault_pdfs` / `api_find_pdf`；
3. 护栏改成**真打端点**（`TestClient` 打上面两个端点）+ **变异自检**
   （把判据改成永远失效 → 必须变红）。

### ② P0-1 的端点级护栏第一版**太松**（坑记 §4.147）

给「多模态抄的表有没有贴回译文」补端点级护栏时，第一版断言只写
「译文里含 `| A | B |`」→ **把真代码改坏后它依然绿**。

**根因**：`apply_table_map` 有个兜底分支 —— 标记不见了就把表格
**追加到译文末尾**（「绝不丢内容」）。所以占位符被销毁时，
表**确实在译文里**，只是**没贴回原位**（被甩到最后）——
这正是 P0 的真实症状（「永远贴不回原位」），而「在不在」的断言抓不到。

**修法**：判据要卡**位置** + 断言兜底文案没出现：
```python
assert translation.index("译文前") < translation.index("| A | B |") < translation.index("译文后")
assert "未能读出" not in translation
```

### 变异自检怎么做的（两次都是真·产品代码变异）

不是「在测试里 monkeypatch 掉判据」，而是**真的把产品代码改坏**，
确认护栏变红，再改回来：
- ① `if False and _is_replaced_output_pdf(...)` → 2 条端点断言变红；
- ② 把 `⟦TABLE(?!_\d+⟧)` 的负向前瞻去掉（回到旧行为）→ 2 条变红。

---

## 2. 平板 / APK：冻结解除（重要）

**旧结论是错的。** 2026-11-23 写的「私钥已丢失、不敢重打 APK」不成立：

```
keytool -list -v android/microbench-tablet.jks
  → SHA256: AC:50:EF:AF:39:04:5A:11:0F:60:E9:92:28:48:F0:AD:61:CF:68:9D:C5:9C:3E:BD:2B:5D:E2:73:1E:96:4A:55
apksigner verify --print-certs MicroBench-平板工作台.apk
  → SHA-256 digest: ac50efaf39045a110f60e9922848f0ad61cf689dc59c3ebd2b5de2731e964a55
```

**逐字相同** → 重打出来的 APK **可以覆盖安装**，平板上的本地笔记/批注**不会丢**。

- 旧 APK 是 **11-18** 打的，而平板端源码 **11-23 改过**（`app.js` / `styles.css`）
  → 旧 APK **已经过时**（缺 11-23 的平板端修复）。
- 本轮已重打：`MicroBench_平板工作台.html` + `MicroBench-平板工作台.apk`
  （同一把 `.jks`，构建脚本末尾自带 `tests/test_apk_delivery.py` 验收，5 passed）。
- `android/SIGNING.md` 已**重写**：更正事实 + 写清旧结论为什么错 + 给「构建前必查」命令。

⚠ **给 25353 台**：那个设备上**没有** `.jks`（被 gitignore），
**不要**在那台机器上跑 `android/build_apk.ps1` —— 脚本会**自动生成一把新密钥**
→ 证书变了 → 平板必须卸载重装 → **本地笔记全丢**。要打 APK 请在有 `.jks` 的机器上打。

---

## 3. 本轮**没有**做的（明确留白，别当漏做）

1. **前端模块化重构**（`microbench/static/desktop/` 多视图架构）—— 用户拍板
   **分阶段**：先修 P0/P1，再逐模块抽离。本轮只做了缺陷修复。
2. **平板端 6 项能力差距**（自主翻页 / 替换版 / 批注编辑 / 答疑附加能力 / 章节视图…）
   —— 仍是「功能没做」，不是缺陷。
3. **导出器烤入段落级 blocks** —— 平板的段落译文仍从云盘快照取；
   离线首开时对照模式没有段落坐标（下一轮）。
4. **`microbench/` 下 48 个一次性诊断脚本**没有清理（清理要动测试引用，风险大于收益）。
5. **没有推 GitHub**（见 §6）。

---

## 4. 验证结果（本机，30461 台）

- 全量基线（Gemini 改动后、进我改动前）：**1691 passed / 1 skipped / 6m15s**。
- 我的改动后：**1742 passed / 1 skipped / 12 deselected / 5m08s**（全绿）。
  （比基线多 51 条 = 本轮新增护栏 + 参数化行尾护栏覆盖了新文件。）
- 回归护栏 `python -m microbench._test_regressions`：**46 passed / 0 failed**。
- 新护栏单独跑：`_test_reader_overlay_render.py` 全绿 + 4/4 变异被抓住；
  `_test_reader_markdown.js` 20 passed；
  `test_vault_pdf_scope.py` **7 passed**（真打端点 + 变异自检）；
  `test_upgrade_pipeline_guards.py` **18 passed**（含 P0-1 端点级护栏）。
- **两次真·产品代码变异**（不是 monkeypatch 判据，是真的把产品代码改坏）：
  ① 三个扫描点的排除改成 `if False and ...` → 2 条端点断言变红；
  ② 去掉 `_normalize_formula_markers` 的负向前瞻 → 2 条变红。
- **真机端到端**：`_live_canvas_release_test.py` 6/6（真服务 + 真 PDF.js + 36 页论文）。
- `tests/test_apk_delivery.py`：5 passed（APK 与仓库根 HTML 逐字节一致 + 证书一致）。
- **被我的改动波及、又修回来的旧护栏 4 个**：
  `_test_qa_panel_render.py`（缺 `_qaModel` 桩件）、
  `_test_readpos_resume.py`（缺 `_pdfZoomReloadTimer` 声明 + 画布清理桩件）、
  `_test_qa_refine_payload.py`（变异目标串要跟着真代码走）、
  `_test_qa_formula_frontend.js` / `_test_qa_section_scope.js`（同上）、
  `_test_reader_annotations.js`（抽屉要登记 Esc 栈 → 补桩件）。
  → **改真代码时，护栏要一起改**（§5 的提醒第 1 条）。

---

## 5. 给下一位 agent 的提醒

1. **改 `index.html` 里被旧探针抽走的函数时，护栏会红** ——
   `_test_qa_panel_render.py` / `_test_readpos_resume.py` / `_test_qa_refine_payload.py`
   会**把真代码抽出来跑**，我的改动就让其中两个报 `_qaModel is not defined`
   / `_pdfZoomReloadTimer is not defined`。修法：给探针补桩件 + 更新变异目标串。
2. **`_escStack` 的出栈由栈自己 `pop()`** —— 关闭函数里再调 `_escUnbind` 是幂等的，
   但**别把 `pop()` 删掉**（删了就会卡住一个已关闭的浮层 → Esc 失灵）。
3. **z-index 只准用 token**（`--z-toast` / `--z-reader` / `--z-modal` / `--z-float`），
   别再写裸数字（§4.138）。
4. `EXTRACTOR_VERSION` **仍然是 5**，**不要动**；段落级改动才评估
   `BLOCK_TRANSLATION_VERSION`（动了会作废另一台设备的段落快照）。
5. ⚠ **「什么算论文原文」只准写一遍** —— 用 `app._is_replaced_output_pdf()`。
   再写第二遍就是下一轮的 §4.146（坑记里记着两次漏网的现场）。
6. ⚠ 断言「某个东西在结果里」之前，先看代码里**有没有兜底分支**
   —— 有兜底时它一定会被塞进去，判据必须卡**位置**（§4.147）。

---

## 6. 交接与同步

- 本轮改动**在本机**（30461 台），**尚未推 GitHub**。
- 推送前先跑：`python -m pytest tests -q`（仓库根 `pytest.ini` 已默认排除 slow）。
- 推的时候按 AGENTS.md §2.7：**先清代理**再推
  （`$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; git -c http.proxy= -c https.proxy= push`）。
- 推完把 commit 号回填到 `AGENTS.md` §7「关键 commit 索引」。
- **平板端那台**：25353 上**不要**重打 APK（见 §2 的警告）。

---

## 7. 关键文件清单（本轮改动）

| 文件 | 改了什么 |
|---|---|
| `microbench/static/index.html` | P0 ×2 + P1 ×9（z-index 阶梯、夜间对比度、Esc 栈、选区快照、防抖、画布按需渲染…） |
| `microbench/app.py` | `_vault_pdfs` 收窄、逐页版本闸门、缓存模型键、替换版端点改同步 |
| `microbench/translation.py` | `apply_table_map` + 表格占位符不再被提前销毁 |
| `microbench/_headless_browser.py` | Chromium 优先 + 缓存可用组合（测试提速一半） |
| `microbench/_test_reader_overlay_render.py` | **新增**：真实浏览器浮层护栏 |
| `microbench/_live_canvas_release_test.py` | **新增**：真机端到端（画布按需渲染/显存释放） |
| `microbench/_test_reader_markdown.js` | 公式里的竖线用例 + 变异自检 |
| `tests/test_vault_pdf_scope.py` | **新增**：vault 论文清单口径（后升级为**真打端点** + 变异自检） |
| `tests/test_upgrade_pipeline_guards.py` | 新增 P0-1 的**端点级**护栏（表必须**贴回原位**，不是「在译文里」） |
| `microbench/generator.py` | `VAULT_REPLACED_PDF_DIR` 常量（生成物目录名的**单一来源**） |
| `microbench/services/vault_index.py` | 索引扫描改用共享常量（不再写字面量） |
| `android/SIGNING.md` | 更正「私钥丢失」的错误前提 + 构建前必查 |
| `AGENTS.md` | §4.138~4.147 坑记 + 顶部摘要 |
