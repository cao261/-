# 交接：合并云端 `origin/main` + 独立审计（2026-11-15）

> **对方设备（25353）先看这份。**
> 改动全在分支 **`feat/audit-2026-10-27`**（本机 30461）。
> `git pull` 之后自动生效，**不要各自改一遍**。

## 0. 一句话

把云端 `origin/main` 的 3 笔提交合并到本机分支，**逐笔独立审计**，
抓出 **1 个阻断级缺陷**（整条事件循环会被冻住）并修掉，
同时把本机自己那批「读源码文本当护栏」的假护栏**全部换成行为级**。

---

## 1. 合并了什么

`origin/main` 比基线 `f9b820a` 多 3 笔（合计 34 文件 / +3021 / -227）：

| 提交 | 内容 |
|---|---|
| `79152b8` | 2026-11-09 / 11-10 两批审计修复：快照原子写 + 写锁、损坏隔离、CORS IPv6 公网放行、读请求 Origin 校验、同步错误上报 |
| `03ac4ad` | merge |
| `f3bf52a` | 隔离读区分「内容损坏」与「瞬时占用」（只有真损坏才改名隔离，OSError 一个字节都不动） |

**合并方式**：`git merge`（不是 rebase），零冲突，自动合并。
合并后逐项核对：本地那 11 个中译替换版 / 专注模式的关键符号全在，
`AGENTS.md` §4.89 / §4.90 都在。

**红线复核（合并必查的四条）**：
- `EXTRACTOR_VERSION` 两侧都是 **5** → 旧译文不作废 ✓
- `paper_id` 算法（`microbench/paperid.py` + `index.html`）这 3 笔**零改动** ✓
- 墓碑 / owner 语义**一行未动**（`api_sync` 只在外围加错误收集）✓
- `microbench/llm.py` 只在 `__all__` 加了一个名字，**零行为改动** ✓

---

## 2. 审计结论（对方那批代码质量很高，但藏了一个 P0）

### 2.1 【阻断】`async def` 端点里等 `threading.Lock` → 冻结整条事件循环

**位置**：`microbench/app.py` 的 `api_pdf_snapshot_save`（合并后在 4871 附近）

**机制**：FastAPI 只把 **`def` 端点**丢进线程池；**`async def` 端点跑在事件循环线程上**。
云端给快照的「读旧值 → 合并 → 写回」加了 `threading.Lock`（**方向完全正确**），
但端点当时是 `async def` → `lock.acquire()` **阻塞**在事件循环线程上。

等锁的那一头是 `/api/sync`（线程池线程），它的临界区里要写**坚果云共享目录** → 耗时不可控。

**实测**（合并后的代码）：

```
CONTROL  async endpoint latency, no contention :     15.0 ms
DURING   async endpoint latency, lock held     :   1547.0 ms
AFTER    async endpoint latency, released      :     16.0 ms
VERDICT: EVENT LOOP BLOCKED
```

期间一个**完全无关**的 `GET /api/pdf/snapshot/list` 从 15ms 变成 1547ms
→ SSE 翻译流、看板轮询全停。

**修法（本机已改）**：端点改成 **`def`**（同步端点）→ FastAPI 自动丢线程池
→ 等锁只占住一个线程池线程。

**护栏（本机新增）**：`tests/test_snapshot_save_event_loop.py`
—— 真发两个并发请求，量「锁被占住时**无关**请求的耗时」（阈值 1.5s）。
**变异自检已做**：把端点改回 `async def` → 护栏红（实测报「无关请求耗时 7815ms」）。

> ⚠ **这条对你那台也生效** —— `git pull` 之后就不用再单独修了。
> ⚠ 如果你以后给别的端点加 `threading.Lock`，记着：**端点得是 `def`**。
> 坑记在 `AGENTS.md` §4.91。

### 2.2 【中】KaTeX 闸门把 `npm install` 变成了测试套件的硬前置

`microbench/_vendor/node_modules/` **被 .gitignore 忽略** → 干净 clone（和没跑过
`npm install` 的设备）**没有** KaTeX；而导出器会**主动拒绝**导出残废版
（2026-11-09 审计 P1-4）→ 导出类用例在那台机器上**必然红**。

**本机处置**：`tests/test_tablet_snapshot_version.py` 的导出用例加
**显式 skipif + 写明原因**（`requires_katex`），不拿「跳过」冒充「通过」。

> ⚠ **你要自查的一件事**：你那台 `microbench/_vendor/node_modules/katex/dist/katex.min.css`
> 在不在？在 → 照常跑；不在 → `cd microbench/_vendor && npm install`，
> 否则那条用例会**跳过**（不是红）。
> 坑记在 `AGENTS.md` §4.93。

### 2.3 【中】`merge_changes.py` 的 watch 循环没有轮次级 try/except

`microbench/merge_changes.py:343` 的 `merge_path(...)` 裸调：
`_atomic_write_text` 里的 `os.replace` 遇到 Obsidian / 杀软占用会抛 `OSError`
→ **整个「平板变更包合并」守护进程退出**。
同型缺陷 `export_offline.py` 那边对方**已经补了** try/except → 修一半。

**本轮没改**（不在这次合并范围内，且不碰用户数据）。
建议单独一轮修：跟 `export_offline.py` 的写法对齐即可。

### 2.4 【低】`move_vault_to_cloud.py` 的回滚不校验

`rollback_moved` 只打「已搬回原路径」，**不做 realpath + 文件数一致校验**
（AGENTS §4.4 明确要求过）；junction 建失败分支只打印一个示例命令且不回滚。
影响面有限（该脚本已标「归档，保留作回滚工具」），但它是 vault 搬迁的唯一回滚入口。

### 2.5 【低·既有，不是这 3 笔引入的】隔离文件留在共享目录

`app.py` 的 `_quarantine_corrupt` 会 `os.replace` 改名；批注路径用的就是它，
而批注目录 = **坚果云共享目录** → 一台设备隔离损坏文件，另一台设备的
`<paper_id>.json` 就消失了。基线 `f9b820a` 已有此行为（已核对 `git show`）。

**本轮没改**（跨设备语义，要两台一起设计）。已记在审计结论里。

---

## 3. 本轮另一件事：把「读源码文本当护栏」全换成行为级

合并后元护栏 `tests/test_guard_efficacy.py::test_source_text_guards_do_not_grow` **变红**
（154 > 132）—— 涨的是**本机自己**加的 22 条（`test_replaced_pdf.py` 19 + `test_zen_mode.py` 3）。

**没有靠调大上限绕过**，全部换成行为级：

| 原来（读源码文本） | 现在（行为级） |
|---|---|
| 「模块里有 `build_replaced_pdf` / `Block` / `BuildStats`」 | `test_module_exposes_the_builder_api`（真 import + 真调用） |
| 「涂改时 `images=NONE` / `graphics=NONE`」 | `test_redaction_only_removes_text_keeps_figures`（真造带**位图+矢量图**的 PDF → 构建 → 比对图片数/矢量图数/文字） |
| 「端点存在 + 版本闸门」 | 三个真打端点的用例（200 / 409×2 / 404×2） |
| 「不许覆盖源 PDF」 | `test_api_never_overwrites_the_source_pdf`（真打端点 → 400 + 源文件**逐字节**没变） |
| 「前端按钮接线 + 替换版视图」 | **新** `microbench/_test_replaced_pdf_frontend.js`：从 index.html **切出真实函数**在最小 DOM 桩里跑，9 条行为断言 + **12 个变异自检** |
| 「滚轮不唤出工具条」「指针到顶才唤出」 | 交给**已有**的 `_test_zen_mode.js`（3 条行为断言 + 16 个变异自检）+ pytest 里新增「那 3 条用例确实在套件里」的覆盖断言 |

**上限 132 → 131**（顺带删掉云端那条 `_zenScrollEl` 断言 —— 它守的机制已被用户
2026-11-14「滚轮一律不唤出工具条」的要求整体删除，留着就是守一个不存在的行为）。

⚠ **仍存在的债**：元护栏的正则只认 `in src|source|app_src|source_code|js_src`
这几个**变量名** → 把新假护栏的变量改叫 `in text` 就能绕过。
要真堵住得把剩下那批逐条换成行为级，属于独立的一轮。
坑记在 `AGENTS.md` §4.92。

---

## 4. 你要做的

1. `git pull`（分支 `feat/audit-2026-10-27`）→ 事件循环 P0 的修复自动生效。
2. 自查 KaTeX 资源在不在（§2.2）→ 不在就 `npm install`。
3. **不用**再单独修 `api_pdf_snapshot_save`（§2.1）。
4. 跑一遍 `python -m pytest tests -q`（`pytest.ini` 已默认 `-m "not slow"`）确认绿。
5. 合并纪律见 `AGENTS.md` §4.94（`merge-tree` 先试 → `merge` 不 rebase → 核功能标记 → 跑真机）。

## 5. 值得直接抄的做法（对方那批代码里的）

1. **原子写模板**（`app.py` 的 `_atomic_write_text`）：`mkstemp` + `os.replace`
   + Windows `PermissionError` 6 次退避重试，**每一处都写了为什么**。
2. **「读不到 ≠ 读坏了」三分类**（`_read_json_or_quarantine`）：内容坏 → 改名隔离；
   瞬时 I/O 故障 → **一个字节都不动**；刚好不存在 → 当成「没有旧值」。
3. **元护栏 AST 化**：用 `ast` 而不是数引号判断恒真断言 → 注释里写 `or True` 不会误伤。
4. **确定性并发测试**：用 `threading.Event` **钉住**先后顺序，不靠 sleep 碰概率。
5. **反向用例的写法**：每条护栏都配一条「把实现改坏就必须红」的用例。
6. **`pytest.ini` 默认 `-m "not slow"`**：把「必须带 -m」从人肉纪律变成默认值。
