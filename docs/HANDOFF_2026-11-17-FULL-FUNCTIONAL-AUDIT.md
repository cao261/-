# 交接 · 2026-11-17 全面功能审查（附三份子审计结论）

> 用户要求：「进行一次全面的功能审查就是确定每一个都能正常运行再给我」。
> 这一轮**没有新功能**，全部是「把功能逐个验一遍 + 把验出来的坏东西修掉」。

---

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| 全量 pytest | **910 passed / 1 failed（改动前就红）/ 1 skipped** |
| Node 护栏 | 桌面 3 套 + 平板 6 套，**全绿** |
| 真机脚本 | 替换版 / 划词公式图 / 总检查，**全绿** |
| 三份子审计 | 同步与数据安全、桌面端前后端契约、平板端功能对等性 |
| **确认并修掉的缺陷** | **12 条**（含 3 条 P1、4 条 P0/P1 级） |
| **修完仍存在的能力差距** | 平板端 6 项（P0-2/P0-3/P1-5/P1-6/P2-7~12），**本轮未修**（见 §4） |
| 新增护栏 | 3 个行为级套件（22 条断言 + 8 个变异自检） |

⚠ **唯一还红的一条**：`tests/test_apk_delivery.py::test_translations_are_baked_in`
—— 改动前就红（缺两份快照的译文烤录）。本轮**已重建 APK**，见 §3.3。

---

## 1. 审查方法（可复现）

### 1.1 套件总表（34 套）

初跑 30 PASS / 3 FAIL / 1 缺失 → 4 处全是**调用姿势错**，改正后全绿：

| 套件 | 正确姿势 | 结果 |
|---|---|---|
| `microbench/_test_regressions.py` | `python -m microbench._test_regressions`（**必须 `-m`**） | 40 passed |
| `microbench/_test_lan.py` | `python -m microbench._test_lan`（**必须 `-m`**） | 39 PASS |
| `microbench/_live_check.py` | `python microbench/_live_check.py "<pdf 相对路径>" 2`（**要两个参数**） | 正常输出 |
| `microbench/_test_tablet_sync.js` | **该文件不存在**（历史上误记） | — |

### 1.2 端点冒烟

62 个端点 × 63 条路由：`OK 28 / 4xx 30 / SKIP 5`，**无 5xx、无超时、无连接失败**。
冒烟产生的 vault 垃圾（测试卡片、QA 文件、周报、快照）**当场清理干净**（复查已清）。

### 1.3 三份子审计（verifier，只读）

| 审计 | 范围 | 结论 |
|---|---|---|
| 同步与数据安全 | 路径安全 / 墓碑 owner / 批注并发 / files 通道 | FAIL（2×P1、2×P2） |
| 桌面端 ↔ 后端契约 | 37 个调用点 × 路径/方法/查询参数/请求体/响应字段 | FAIL（1 条必现 422 + 2 条边界） |
| 平板端功能对等性 | 两端逐功能比对 | **FAIL**（3×P0、3×P1、6×P2） |

---

## 2. 确认并修掉的缺陷（12 条）

### 2.1 跨设备并发写：同一类缺陷重复 4 次（P1-高 ×2、P1-中、P2-中）

| # | 位置 | 症状 | 修法 |
|---|---|---|---|
| 1 | `api_pdf_annotations_save` | 另一台设备刚写的批注**静默消失**（两边都返 200） | 指纹取在**读之前** + 撞满 3 轮**放弃**（409）→ `_cas_update_json` |
| 2 | `api_pdf_annotations_delete` | 删除一条批注时把对方新加的一起抹掉（端点**完全没有 CAS**） | 接进同一个 CAS；主路径用严格读 |
| 3 | `api_pdf_snapshot_save` | 对方新翻的页被整份盖掉 → 用户要重翻 = **重烧 token** | 进程内锁内再套一层**跨设备 CAS**（原来只有 `threading.Lock`，挡不住另一台设备） |
| 4 | `_sync_pull_files` | 还原中的 PDF 是**半截**的 → 坚果云可能把半截 PDF 上行 | 新增 `_atomic_copy_file()`（mkstemp → `os.replace`），与推送端对齐 |

⚠ 根因：**乐观 CAS 的指纹取在读之后**。指纹要标识「我们即将读到的那一版」；
取晚了 → 写前比对必然通过 → 用旧基线盖回去。

### 2.2 联动删除会删掉发起端自己那份（P2-中）

`propagate_delete` 只写 `propagate/deleted`，**不记录发起方** → 执行端分不清
「对方要求删」和「我自己要删」→ **发起端自己那份也被删**，且云端实体同时释放 → 不可恢复。
→ 记录 `propagate_by`；只有发起方不是我时才删本地副本；本机版本与发布不一致 → 报冲突不删。

### 2.3 「➕ 存入避坑库」一键保存 **100% 失败**（P0，桌面端契约审计发现）

前端发 `raw_error: ""` / `root_cause: ""`，后端 `TroubleshootRequest` 两字段都是 `min_length=1`
→ **必然 422**；且报错被写成「API 暂不可用」，把排查方向带偏。
→ 填**真内容**（问题 + 引文 + 页码 → 错误现场；解答结论 → 根本原因），每个必填字段留兜底，
报错把 422 与服务端故障分开。

### 2.4 平板端划词提问看不到公式（P0，与桌面 11-17 同一缺陷）

平板**划词路径一张图都不发**（桌面 11-17 修好了，平板没跟上，而且更严重 —— 连整页图都没有）。
→ 复用平板**已有**的 scale=4 高清裁图能力：划词时（趁 PDF 还开着）抓好
「整页原文图 + 该页高清公式图」，多图一起发；提示词加「一律以高清公式图为准 /
不要根据上下文补全或猜测 / 看不清就说看不清」（与桌面 `app.py` 同口径）。

### 2.5 平板端 PDF 文本层没设 `--scale-factor`（P1）

pdf.js 3.11 的文本层 CSS 用 `calc(var(--scale-factor) * W px)` 算 span 宽高，
**这个变量必须由调用方设**（实测 `pdf.min.js` 里只有 `var()` 的用法，没有 `setProperty`）。
桌面 `index.html:6004` 设了，**平板漏了** → span 塌成 0 宽 → **文本层与画布错位**
→ **平板上划词会选到错的字**，批注高亮框也落在错位置。
→ 平板补设；顺带把 `pdfFitScale()` 的 NaN 兜住（容器宽度拿不到时会让几何整个崩掉）。

### 2.6 平板端段落译文缺版本闸门（P1）

平板把快照里的 `blocks` 原样收下、**从不比对 `blocks_version`** →
段落切分算法一改，旧段落译文按旧坐标贴到新段落上 → **左右栏对调、译文串行**。
（桌面端靠 `/api/paper/page_blocks` 比对；平板访问不到后端 → 只能把版本**烤进包里**。）
→ `export_offline.py` 烤 `blocks_ver`（与 `trans_ver` 同一套路）→ 平板读到时比对，
对不上就**丢弃段落**（整页译文不受影响），并在界面上告诉用户。

### 2.7 端点边界不一致 2 条（低）

| 端点 | 问题 | 修法 |
|---|---|---|
| `/api/paper/find_pdf` | `title` 为空 → 422 被 catch 吞成「没找到」 | 前端空标题就不发请求 |
| `/api/paper/explain_selection_stream`、`/api/paper/cross_qa_stream` | 后端 `min_length=2`，前端只判空 → 单字符提问 422 | 前端判长度 |

### 2.8 源码卫生 3 条

| 问题 | 影响 | 修法 |
|---|---|---|
| `microbench/static/tablet/app.js` 里有 **1 个 NUL 字节** | **ripgrep 把整个文件当二进制跳过** → 任何基于 grep 的检索/审计都搜不到 → 差点把「平板没实现划词带图」当成事实 | 换成空格；新增全仓 NUL/BOM/行尾护栏 |
| `microbench/_test_regressions.py` 带 BOM | 首行可能被当乱码 | 去 BOM |
| `start_offline_sync.bat` / `start_tablet_sync.bat` 是 LF | `.bat` 必须 CRLF（cmd.exe 按 LF 解析会错位，AGENTS §2.6） | 转 CRLF |

---

## 3. 本轮新增/变更的产物

### 3.1 代码

| 文件 | 改了什么 |
|---|---|
| `microbench/app.py` | `_cas_update_json`、`_atomic_copy_file`；批注保存/删除、快照保存、files 还原、propagate_delete 全部改走它们 |
| `microbench/static/index.html` | 批注保存 409 重试；避坑库一键保存填真内容 + 报错说实话；find_pdf / 两个提问端点的边界守卫 |
| `microbench/static/tablet/app.js` | 划词带图（整页图 + 高清公式图）；文本层 `--scale-factor`；`pdfFitScale` NaN 兜底；段落版本闸门；NUL 字节清除 |
| `microbench/export_offline.py` | 烤 `blocks_ver` 给平板 |

### 3.2 护栏（全部行为级 + 变异自检）

| 文件 | 断言 | 变异自检 |
|---|---|---|
| `tests/test_sync_cas_2026_11_17.py` | 7 | 6/6 被抓 |
| `microbench/_test_tablet_qa_images.js` | 23 | 5/5 被抓 |
| `microbench/_test_troubleshoot_save_frontend.js` | 12 | 3/3 被抓 |
| `tests/test_troubleshoot_save_frontend.py` | 3 | — |
| `tests/test_source_hygiene.py` | 600（按文件参数化） | — |

⚠ 元护栏上限 `_MAX_SOURCE_TEXT_GUARDS = 131` **没有调大** —— 本轮新增的全是行为级。

### 3.3 APK 重建（✅ 本机能重建）

本机**有** Android SDK（`D:\Android\Sdk`，build-tools 35.0.1）+ JDK + 签名密钥
→ `pwsh android/build_apk.ps1` **构建成功**：
`MicroBench-平板工作台.apk`（1.04 MB，已签名，交付验收 5/5 通过）。
已核对 APK 内 `assets/index.html` 含
`pendingSelectionImages` / `--scale-factor` / `blocks_ver` / `QA_IMAGE_RULE`，且 **0 个 NUL 字节**。

⚠ `tests/test_apk_delivery.py::test_translations_are_baked_in` 仍红：
缺两份快照（等效电路综述、2015 Control of Magnetic Relaxation）的译文烤录 ——
**改动前就红**，与本轮无关；根因是那两份快照的 `extractor_version` 不是当前版本。

---

## 4. 修完仍存在的能力差距（**本轮未修，需拍板**）

平板端对等性审计的结论是 **FAIL**。以下 6 项**不是缺陷，是功能没做/没对齐**：

| # | 项 | 现状 | 影响 |
|---|---|---|---|
| P0-2 | 平板端没有「中译替换版 PDF」 | 全目录 0 命中 | 平板做不出「原文页+中文替换页」 |
| P0-3 | **平板完全不能翻译 PDF 页** | 平板的译文只能靠桌面同步过来 | 从没在电脑上翻过的论文，平板永远没译文 |
| P1-5 | 平板批注写不了备注/颜色，**不能编辑、不能删除、没有列表** | `note: ""`、`color: "yellow"` 写死 | 违反 AGENTS §1.8「少做哪个都要先问用户」 |
| P1-6 | 平板答疑缺：预设问题 / 带反馈重答 / 导出到 Obsidian / 自动 scope / 跨节问答 / 上下文档位 | 全缺 | 答疑成果沉淀不下来 |
| P2-7~12 | 章节视图、专注模式、双语笔记导出、快照管理、per-paper 重译、工作台外围功能 | 全缺 | 平板定位是「阅读器」，桌面是「工作台」 |

⚠ **P0-3 是根**：平板要自己翻页，就得在平板端重做**段落抽取层**
（双栏顺序 + 公式保护在 Python 侧），那是**另一个项目级工程**，不是本轮能塞进去的。

---

## 5. 本机不可验证的（不装作验过了）

| 项 | 原因 |
|---|---|
| 真实跨设备同步 | `/api/sync*` 会写坚果云共享目录（影响另一台设备 25353）→ **本轮跳过** |
| files 通道端到端 | 本机 `.env` 是 `SYNC_PDF_BODY=0` → 通道休眠；两条缺陷用**隔离复现**验证（见护栏） |
| 平板真机 | 无平板设备；平板结论来自**代码 + 构建产物静态比对**（APK 内 app.js 与源文件逐字一致） |
| 划词准确度（桌面/平板像素级） | 自造探针的拖选判据有缺陷（挑到了标题 span），**结论作废**，未重做 |

---

## 6. 交接清单

- [x] 12 条缺陷修完 + 行为级护栏（变异自检全过）
- [x] `AGENTS.md` §4.99~4.103（五条新坑记）+ 顶部索引
- [x] `docs/SYNC_CONTRACT.md` 三处与代码不符的地方更正（§4 / §4.1 / §5 / §8）
- [x] APK 重建并核对
- [ ] **待用户拍板**：平板端那 6 项能力差距要不要开下一轮（§4）
- [ ] **待用户拍板**：`tests/test_apk_delivery.py` 那条既有红项要不要一并修（重翻两篇 = 烧 token）
- [ ] 另一台设备（25353）`git pull` 后**必须重启服务**（改过 `app.py`）
- [ ] 平板端更新：把新的 `MicroBench-平板工作台.apk` 装到平板上
