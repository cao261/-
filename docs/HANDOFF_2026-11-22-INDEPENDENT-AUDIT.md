# 独立代码审查 + 升级（2026-11-22）

> 这一轮是**从头重做一遍审查**，不是接着上一轮。
> 基线：`pytest tests -q -m "not slow"` → **1629 passed / 1 skipped**（全绿）。
> 终版：见文末「验收」。共修 **14 项**，每项配**行为级**护栏 + **变异自检**。
>
> ⚠ 本轮**只改本机**，没有推 GitHub；25353 台看不到这些改动。
> ⚠ 平板端**只改了构建脚本**，没有重打 APK/HTML（见「需要拍板」）。

---

## 0. 一句话结论

四路并行只读审查（后端 API+同步 / 阅读器前端 / 模型层+抽取层 / 平板端）共 32 条发现，
逐条取证后**修了 14 项**（3 个 P0、6 个 P1、5 个 P2），
每项都配了「改坏代码必须变红」的行为级护栏；
另有 9 条**已核实无问题**、9 条**已知但本轮不改**（附理由）。

---

## 1. P0：功能直接不可用（3 项）

### 1.1 `extract_outline` 必崩 —— vault 27 篇里 11 篇必 500
- **位置**：`microbench/paper_text.py:508-554`
- **根因**：`References` 分支只 `break` 了**行**循环，**外层页循环继续** →
  `cur["last_page"]` 对 `None` 赋值 → `TypeError`。
- **影响**：选中标题 → 「小节全文」档 → 整条功能报错（JACERS 综述等 11 篇）。
- **修法**：`stop_pages` 标志跳出页循环 + `if cur is None` 兜底。
- **证据**：真机 `GET /api/paper/outline` **27/27 全 200**（修前 11 篇 500）。

### 1.2 续读位置被 null 覆盖 —— 关应用即丢进度
- **位置**：`microbench/static/index.html` `_captureReadPos`
- **根因**：栏 `display:none` 时 anchor 恒为 `null`，
  `beforeunload`/`pagehide` 触发的采集把**刚存好的位置整条覆盖成 null**
  → 下次打开静默不回位（§4.96 同型复发）。
- **修法**：**合并**——新的采不到就沿用旧的，绝不许把好数据抹掉。
- **护栏**：`microbench/_test_readpos_resume.py`（真渲染，**24 断言**，变异 **13/13** 全被抓住）。

### 1.3 导出产物报废 —— 平板工作台/离线阅读版整段挂掉
- **位置**：`microbench/export_offline.py`
- **根因**：数据块只转义 `</`，但载荷里出现 `<!--` **紧跟 `<`** 时，
  HTML 解析器进入 *script data double escape start* 状态 →
  **数据块的 `</script>` 永不闭合** → 后面 app.js/KaTeX/PDF.js 全被当脚本文本吞掉。
  **Edge + Chromium 双引擎实测一致**（只有 `<!--<` 触发，`<!-- ` 空格安全）。
- **修法**：新增 `_escape_json_for_script()`：`<` → `\u003c`
  （JSON 合法转义，`JSON.parse` 解回来还是 `<`，**数据一个字节都不变**），两处调用点已换。
- **护栏**：`microbench/_test_tablet_export.py`（**48/48**，含真浏览器跑 4 条恶意载荷）。

---

## 2. P1：会静默丢数据 / 冻结服务（6 项）

### 2.1 共享批注被改名隔离
`app.py` `_read_json_or_quarantine` 在文件损坏时会把**共享目录**里的文件改名成
`.corrupt-*` —— 违反本文件自己的 6629 那条规则（拉取路径遵守、保存/删除路径违反）。
**修法**：新增 `_path_is_shared()`，把规则**集中进 reader 内部**（保住既有并发注入点）。

### 2.2 `freed_bytes` 虚报
`api_sync_files_release` 原本在 `unlink()` **之前**累加字节，失败被 `continue` 吞掉 →
界面照样报「腾出 X MB」→ 用户据此删本地副本而云端还在。
**修法**：删除成功后才累加 + 收集 `errors` + `status:"partial"`；前端会提示失败篇数。

### 2.3 7 个 async 端点阻塞事件循环
`api_vault_file` / `api_vault_delete_file` / `api_vault_reveal` /
`api_pdf_snapshot_delete` / `api_pdf_annotations_save` / `api_pdf_annotations_delete`
**+ `api_upload_pdf`（40MB 写盘，落点可能在坚果云 junction 里）**。
**修法**：前 6 个改同步 `def`（沿用项目自己的 `api_pdf_snapshot_save` 模式）；
上传的写盘丢进 `await asyncio.to_thread(...)`。
**护栏**：`tests/test_blocking_endpoints_off_event_loop.py`（**7/7**，
判据是**线程身份**不是源码里有几个 `async def`；变异自检：改回 `async def` 精准变红）。

### 2.4 files/ 共享清单无 CAS —— 跨设备并发写会丢数据
违反 §4.1 乐观 CAS 红线。
**修法**：新增 `_cas_update_file_meta()` + 6 个补丁构造器
（`_patch_owner/_touch/_publish/_tombstone/_release/_propagate_delete`）；
6 个调用点全改走 CAS 合并；`_patch_publish` **整组保留**对方设备发起的
「联动删除三件套」(deleted+propagate+propagate_by)。
**护栏**：`tests/test_files_meta_cas.py`（3/3 + 变异）。

### 2.5 「重新回答」缺 pdf 路径 —— 模型只能瞎编公式 ⭐
- **位置**：`index.html` `_refineQA`（⬆️/⬇️/✏️ 三条路径共用）
- **根因①**：payload 里**一个图相关的字段都没带**（无 `pdf_relative_path`/`page_num`/`formula_rects`）
  → 服务端解析不出 PDF → **一张图都拿不到** → 连「附件说明」都不生成
  → 而上下文里的【公式N】是**裸占位** → 模型只见占位符 → **瞎编公式**（§4.6）。
- **根因②**：用的是**实时选区**而不是**提问当时的快照**
  → 用户问完又划了别的段落再点「⬆️」→ 模型拿到「旧问题 + 新选区」→ 答偏。
- **修法**：补齐三个字段；改用 `answerBody.dataset` 里的**提问快照**。
- **护栏**：`microbench/_test_qa_refine_payload.py`（**真 Edge 渲染，14/14**，变异 **7/7**）
  —— 探针**真跑**「输入问题 → 点发送 → 改掉实时选区 → 真点「⬆️ 多看点上下文」」。
- **真机端到端证据**（JACERS 综述，第 1 页）：
  | 载荷 | 模型表现 |
  |---|---|
  | 带 pdf 路径+页码+公式坐标（修复后） | 读出原文公式 `α_ME = E/H (1)`，并点名「附件 2 的高清公式图」 |
  | 完全不带（= 修复前） | 「你划选的这段文字里**没有任何公式**」，请用户重划 → 逼问就会编 |

---

## 3. P2：小但确实有病（5 项）

### 3.1 缩放 / 重排丢阅读位置（注释里写了意图，实现没做到）
- **位置**：`index.html` `setReaderZoom` / `_pdfRelayoutIfNeeded` / `loadPdfDualColumn`
- **根因**：`loadPdfDualColumn` 里有注释「先把『当前看到哪儿』抓下来 —— 下面马上要把
  两栏的 DOM 全清空」，**但真正清 DOM 的是调用方**（在调它**之前**就 `innerHTML=""`）
  → 被调方再去读活锚点时 DOM 已空 → **恒为 null** → 整段是**死代码**
  → 位置只能回落到 **400ms 防抖**的旧值 → 「滚动完立刻缩放」必退回旧位置。
- **修法**：清空前先落盘，且**必须在改 `_pdfZoom` 之前**（记录里带 zoom，
  比例换算靠它判断「缩放变了没有」，§4.126）；
  另把「回位目标怎么算」抽成真函数 `_readPosTargetFor`，好让护栏能直接调用。
- **护栏**：并入 `_test_readpos_resume.py`（**24 断言**，变异 **13/13**）。

### 3.2 router 对模型说「4 档」，实际 5 档
`app.py` `api_classify_qa_scope` 的 user 消息写死「4 档」——
那是加 `section` 档之前的**陈旧文案**（system prompt 里早就是 5 档）。
**修法**：改成 `len(QA_SCOPE_MODES)`，以后加档不会忘改文案。
**护栏**：`tests/test_qa_scope_router.py` 新增一条（拦下**真发出去的请求**看内容）。

### 3.3 auto_sync 读不到 origin/main 却报「已是最新」——同步静默失效
- **位置**：`microbench/auto_sync.py`
- **根因**：`if not remote or remote == local: → up_to_date`，
  而 `remote` 来自 `git rev-parse origin/main` —— **这个命令失败时 remote 是空串**
  → 报「没有新提交，无需同步」+ `ok=True`。
  → **同步其实根本没在工作**，对方设备的提交永远合不过来，日志却说一切正常。
- **修法**：「读不到」与「没变化」分成两条：读不到 → `action="remote_ref_missing"` + `ok=False`。
- **护栏**：`tests/test_audit_2026_10_27_dataplane.py` 新增一条
  —— 用**真 git 仓**搭出「远端默认分支叫 master（没有 main）」的场景，真跑 `auto_sync.main()`。

### 3.4 我自己踩的坑：CRLF 行尾（已被项目自己的护栏抓到）
用 Python 脚本改源码时，Windows 上 `open(path,"w")` 的 `newline=None`
会把每个 `\n` 翻译成 `\r\n` → 整篇变成 CRLF → `tests/test_source_hygiene.py` 变红。
（`git diff` 看不出来，因为 `.gitattributes` 会在 `git add` 时归一化。）
**已归一化**，并把这条写进 `AGENTS.md §4.129`。

### 3.5 两处 U+FFFD 坏字符
`paper_text.py`（本轮引入的）与 `export_offline.py`（历史遗留，HEAD 里就有）。
**已修**。全仓其余 U+FFFD 都在**注释**或**故意**的位置（`replaced_pdf.py` 那处就是在剥掉半截标记），不动。

---

## 4. 新增 / 扩大的护栏（都挂在 pytest 里）

| 护栏 | 断言数 | 变异捕获 | 挂载点 |
|---|---|---|---|
| `microbench/_test_qa_refine_payload.py`（新） | 14 | 7/7 | `tests/test_pdf_layout_guardrails.py` |
| `microbench/_test_readpos_resume.py`（扩） | 24 | 13/13 | 同上 |
| `microbench/_test_tablet_export.py`（扩） | 48 | — | 同上 |
| `tests/test_blocking_endpoints_off_event_loop.py`（新） | 7 | ✅ | pytest 直收 |
| `tests/test_files_meta_cas.py`（新） | 3 | ✅ | pytest 直收 |
| `tests/test_qa_scope_router.py`（扩） | +1 | — | pytest 直收 |
| `tests/test_audit_2026_10_27_dataplane.py`（扩） | +1 | — | pytest 直收 |

`tests/test_guard_efficacy.py` 的「行为级套件清单」已补登这 3 个新文件。

---

## 5. 已知但**本轮不改**（附理由）

| 项 | 为什么不动 |
|---|---|
| 平板直写云盘笔记/批注无 CAS | 要 DAV ETag / 原生桥，改动大；且用户 2026-11-18 已拍板「平板端本轮不动」 |
| `generator.py` 笔记卡片非原子写（705/781/1060/1161） | 单篇笔记被截断只影响那一篇；周报已经是原子写（930-933） |
| 章节视图翻译报「请求的模型」而非实际模型 | LLM_CONTRACT §4.4；改它会牵动整个模型层 |
| section 档公式编号与附件图对不上 | 需要连带改附件编号约定 |
| 平板 `safeErr` 脱敏关键词覆盖不足、URL userinfo 无密码不脱敏 | 平板端本轮不动 |
| 平板 FSA 分支空结果当成功 | 同上 |
| 元护栏上限正则（`_MAX_SOURCE_TEXT_GUARDS=131`）可被变量名绕过（≥8 条已绕过） | 修正则会让计数立刻超限而变红；正确做法是**逐条换成行为级**再把上限调小 —— 工程量大，单独排一轮 |

---

## 6. 需要拍板 / 下次构建自动带上

### ① 已交付的平板产物落后于源码
- `MicroBench_平板工作台.html`（11-18 构建）内联的 app.js 比
  `microbench/static/tablet/app.js` **旧**：缺 2026-11-19 的
  **放松版提示词**（"你是资深学术导师…"）和 **`_headTailText`**（上下文头尾都留）。
  实测差异：内联 app.js 34 行。
- 而且它数据块里还有 **3497 个裸 `<`** → 正好是 1.3 那个报废条件的暴露面。
- **构建脚本已修好**（实测新构建：裸 `<` = 0、`\u003c` = 3497、
  `JSON.parse` 结果一模一样（36 笔记 / 5 译文）、内联 app.js 与源文件**逐字一致**）。
- ⚠ 但 `tests/test_apk_delivery.py` 要求 **APK 里的 `assets/index.html` 与根目录那份逐字节相同**
  → 重打 HTML 就必须同时重打 APK（`android/build_apk.ps1`，脚本自己保证两者一致）。
- 🔴 **2026-11-23 用户已拍板：先不重打，等签名密钥的事定下来。**
  原因（实测）：**生成现有 APK 的那把私钥 `android/microbench-tablet.jks` 已经不在本机**
  → 脚本首次构建会自动生成**新密钥** → 新 APK 与平板上已装的那版签名不同
  → Android **不允许覆盖安装** → 平板必须先卸载重装 → **本地笔记/批注会丢**。
  证书指纹与判断方法见 **[`android/SIGNING.md`](../android/SIGNING.md)**。
  → 下次正常构建（`auto_sync.py` 第 6 步 / `android/build_apk.ps1`）会自动带上代码侧的修复。

### ② AGENTS.md 新增三条坑
`§4.129` Python 脚本改源码 → CRLF；`§4.130` 注释里的意图 ≠ 实现；
`§4.131` 「查不到」≠「没变化」。

---

## 7. 验收

```
python -m pytest tests -q -m "not slow"     # 1642 passed / 1 skipped / 12 deselected（11m30s，全绿）
python microbench/_test_readpos_resume.py   # 24 PASS / 0 FAIL，变异 13/13
python microbench/_test_qa_refine_payload.py# 14 PASS / 0 FAIL，变异 7/7
python microbench/_test_tablet_export.py    # 48 PASS / 0 FAIL
```

（基线是 **1629 passed / 1 skipped** → 现在 **1642 passed / 1 skipped**，
+13 条全是这一轮新增的行为级护栏。）

真机（`http://127.0.0.1:5000`，用最终代码重启后复验）：
- `GET /api/paper/outline` × 27 篇 → **27/27 HTTP 200**
- `POST /api/paper/explain_selection_stream`（带图 vs 不带图）→ 见 §2.5 的对比表
