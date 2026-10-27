# 同步与平板适配交接（2026-10-28）

> **本轮做了什么**：把对方设备（25353）的 `main` 分支**合并进本机分支**，
> 解决 25 处语义冲突，并**复核 + 加固平板端（APK）**。
>
> - 本机分支：`feat/audit-2026-10-27`（第四轮全量审计）
> - 合入来源：`origin/main`（对方第二轮审计，6 个提交）
> - 合并结果：**740 passed / 1 skipped**（合并前 673 passed）
> - 交付物：`MicroBench-平板工作台.apk`（1.0 MB）+ 仓库根 `MicroBench_平板工作台.html`
>   —— 两份**逐字节相同**（sha256 `4df443d0…d9c01`）

---

## 0. 一句话

**两台设备各审各的，结论在三个点上撞了车 —— 这轮合并的价值不在「合上」，
在于把撞车的三点用代码事实判清楚，然后让两边都对。**

撞车的三点：

| # | 撞车点 | 本机第四轮审计的结论 | 对方第二轮审计的结论 | 最终按谁 | 判据 |
|---|---|---|---|---|---|
| ① | 缓存键要不要含符号词典 | **要**（含路径，防撞键） | **不要**（抽取层不读词典） | **对方** | 代码事实：`extract_text_from_pdf(pdf_bytes)` 签名里没有字典参数，`paper_text` 不 import 也不读词典 |
| ② | 限流器间隔用哪个基准 | 预留时间 `_last_planned_time` | 实际触发 `_last_call_time` | **两者取并集**（取更严的） | 两边护栏各 1 条，都要绿 |
| ③ | 审计报告文件名 | `docs/AUDIT_2026-10-27.md`（本机第四轮） | 同名（对方第二轮） | **拆成两份** | add/add 冲突，两份都该留 |

---

## 1. ① 缓存键：为什么最终采纳「不含词典维度」

### 代码事实（不是推测）

```python
# microbench/paper_text.py
def extract_text_from_pdf(pdf_bytes: bytes) -> list[dict]:
```

- 签名里**没有字典参数**；整个 `paper_text` 模块**不 import 也不读**符号词典。
- 文本里的 `⟦G:<下标>⟧` 是**页内 glyphs 列表的下标**，不是词典序号。
- 把标记换成真实字符是后面 `_resolve_document_glyphs()` 干的，
  而**那时缓存早写完了**。

→ 缓存里存的永远是**抽取原始输出**，词典维度对它**没有任何影响**。

### 本机那个修法为什么反而更糟

本机第四轮审计加的是「词典的 (mtime,size) **+ 路径**」，本意是防
「没有词典」与「词典文件不存在」撞键（两者都算 `[0,0]`）。

但**撞键的前提不存在**（词典维度根本不影响缓存内容），
而这个修法引入了一个**真实的新故障**：

```
不同调用点传不同参数 → 算出不同的键 → 一份写、一份读
→ 缓存永远命中不了 → 842x 加速完全失效
→ 用户每次都等 2.7~9.7 秒重抽（比撞键更糟）
```

合并后工作区里就出现过这个症状：
`api_open_vault_pdf` 写的是 `cache_key(pdf_path)`，
`_extract_page_cached` 读的是 `read_cache(pdf_path, GLYPH_DICT_PATH)` → **键不同**。

### 最终方案

- `cache_key(pdf_path)` / `read_cache(pdf_path)` / `write_cache(pdf_path, pages, key)`
  —— **单一签名**，读写两侧必然一致。
- 键里放**两个**闸门，各管各的：

| 闸门 | 管什么 | 动了的代价 |
|---|---|---|
| `EXTRACTOR_VERSION`（paper_text） | 数据面：抽取**有效性**变了 → 旧译文快照整份作废 | 用户把全部译文重翻（跨设备）|
| `EXTRACT_IMPL_REV`（pdf_cache） | 性能面：抽取**实现**变了 → 磁盘缓存作废重抽 | 本机 27 篇重抽一次 ≈ 72s，各付各的 |

- 护栏从「词典维度进键」改成**行为级**的：
  `test_cache_key_takes_no_dictionary_argument`（签名不许再有第二个参数）+
  `test_cache_read_and_write_use_the_same_key`（扫 `app.py` 全部调用点，
  任何一处传词典参数就红）+ `test_cache_key_ignores_the_glyph_dictionary_entirely`。

> **教训**：「加一个维度防撞键」之前先问**这个维度到底影响不影响缓存内容**。
> 不影响的话，加它不是修 bug，是**引入一个更隐蔽的故障**。

---

## 2. ② 限流器：两个基准取并集

两边修的是**同一个**并发缺陷（`min_interval` 在并发下失效 → 齐射 → 打到上游 429
→ 被误判成「配额耗尽」→ 全局降格 15 分钟），但判据不同：

- 本机：间隔按**预留时间** `_last_planned_time` 算（+ 末尾「欠账补睡」机制）
- 对方：间隔按**实际触发时间** `_last_call_time` 算 + `_next_allowed` 预占检查

**最终实现**：两个基准都算，**取更严的那个**：

```python
since_planned = now - self._last_planned_time
since_call    = now - self._last_call_time
since_last    = min(since_planned, since_call)
```

本机的「欠账补睡」机制保留（更强：保证**实际**触发间隔也 ≥ min_interval）。

两边的护栏都必须绿：

| 护栏 | 来源 |
|---|---|
| `tests/test_llm_limiter.py`（4 条，真线程 + 真实小间隔） | 对方 |
| `tests/test_audit_2026_10_27_llm.py::test_min_interval_holds_under_concurrency` | 本机 |

---

## 3. ③ 审计报告：两份都留，拆成两个文件

add/add 冲突（同名文件两边都新建）——**两份是不同设备的不同轮次，内容不重叠**：

| 文件 | 内容 |
|---|---|
| `docs/AUDIT_2026-10-27.md` | 本机（30461）第四轮全量审计：4 CRITICAL / 17 HIGH / 25 MEDIUM |
| `docs/AUDIT_2026-10-27-REMOTE-R2.md` | 对方（25353）第二轮审计记录：已改 / 故意没改 / 未审到 |
| `docs/AUDIT_2026-10-27-REMOTE.md` | 「审对方设备提交」的报告（第三份，别和上面两份混） |

---

## 4. 平板端（APK）：这轮做了什么、没做什么

### 4.1 关键事实：**对方这轮完全没动平板端**

```
git diff 43a2986 origin/main -- microbench/static/tablet/ android/ microbench/export_offline.py
→ 零改动
```

对方的 12 处修复全在**后端 + 桌面端前端**（`app.py` / `llm.py` /
`paper_text.py` / `pdf_cache.py` / `static/index.html`）。

→ **桌面端的修复一行都不会自动到平板**（`docs/HANDOFF_2026-10-25-TABLET-ADAPT.md`
§1 的架构事实），所以下面逐条判。

### 4.2 逐条判：哪些要抄

| 对方这轮的改动 | 平板要不要抄 | 判据 |
|---|---|---|
| 前端 `getJson`/`postJson` 封装（17 处「先 json 后判 ok」） | **不用抄** | 平板的请求走**原生桥**（`backend.readBlob()` / `nativeCall`），不走 `fetch`。且平板端 22 处 `innerHTML` 全部已是「先判 `r.ok` 再解析」 |
| 弹窗 Esc `{once:true}` 泄漏 | **不用抄** | 平板端只有 1 处 `once:true`，用于 abort 监听（正确用法） |
| 裸 `DOMContentLoaded` | **要抄**（本轮已做） | 见 §4.3 |
| 跨章节问答勾选状态 | **不用抄** | 平板端没有跨章节问答功能 |
| 阅读器键盘关闭 / 诊断面板 escapeHtml | **不用抄** | 平板端没有这两个界面 |
| 写操作按钮防重 | **不用抄** | 平板端已有（AI 面板 `_busy` + 序号守卫） |
| 密钥脱敏盲区 | **不用抄** | 平板端 `safeErr()` 已有完整实现（userinfo / 裸 Bearer / JSON 形态 key / 绝对路径） |
| 离线导出 XSS | **不用抄** | 平板端自带 `sanitizeHtml()`，97 条探针全拦住 |
| `reasoning_effort` 精确主机名白名单 | **两端已一致** | 平板端 `AI_REASONING_EFFORT_OK_HOSTS` 与桌面端 `_REASONING_EFFORT_OK_HOSTS` **完全相同**，且对方新加的护栏 `test_desktop_and_tablet_use_the_same_host_whitelist` 会持续钉住这件事 |

### 4.3 本轮实际改的平板端：1 处

**`microbench/static/tablet/app.js` 的 `DOMContentLoaded` 加固**：

```js
// 旧：裸监听
document.addEventListener("DOMContentLoaded", () => { boot().catch(...) });

// 新：判 readyState，已就绪就直接跑
function _bootSafely() { boot().catch(...); }
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _bootSafely);
} else {
  _bootSafely();
}
```

**为什么现在还改**：平板端脚本在 `</body>` 前同步加载 → 正常路径下
`DOMContentLoaded` 还没触发 → 监听器**是有效的**。
但**不能靠这个前提活着**：一旦有人把脚本挪到 `<head>`、或改成
defer/async 动态注入，`readyState` 就已经是 `"interactive"` →
监听器再注册就是**死的** → `boot()` 一次都没跑 → 平板停在
「壳子渲染出来了但空空如也」，且**没有任何报错**（正是用户看到的白屏）。

→ 这正是 AGENTS.md §4.1「看板全是 0」的同型缺陷。
本机桌面端已经修过（`index.html` 的 `_wirePaperFormInputs`），
平板端这轮补齐。

### 4.4 平板端**确认完好**的部分（没被合并弄坏）

| 项 | 位置 | 状态 |
|---|---|---|
| C-2 WebView 离源仍持原生桥 | `MainActivity.java` `shouldOverrideUrlLoading` | ✅ 在 |
| H-7 原生桥回包带 status | `app.js` `err.status = Number(p.status)` | ✅ 在 |
| H-8 AI 代理与云盘写入分池 | `DavBridge.java` `POOL_AI` / `POOL_DAV` | ✅ 在 |
| H-9 设置同步根 ≠ vault 根 | `SettingsActivity.java` | ✅ 在 |
| H-10 批注删除墓碑 | `app.js`（43 处 `deleted`） | ✅ 在 |
| 答疑预算 / 空正文重试 / 序号守卫 | `app.js`（16000 / 32000 / `aiRequestSeq`） | ✅ 在 |
| paper_id 与桌面端逐字一致 | `app.js` `paperIdFor` vs `paperid.py` | ✅ 已实测对拍 |

### 4.5 paper_id 一致性：实测对拍（不是看代码）

新模块 `microbench/paperid.py`（对方新建）统一了**三处**实现
（`app.py` / `merge_changes.py` / 桌面前端）。
平板端是**独立 JS 复刻**，必须仍逐字一致 —— 用真 JS 引擎跑 6 个用例对拍：

```
OK  01_Literature\04_Survey_综述\2008-JACERS-综述.pdf
OK  01_Literature\a.pdf
OK  01_Literature/论文 with 中文.pdf
OK  01_Literature/emoji_😀_paper.pdf      ← 代理对：码元 vs 码点
OK  01_Literature/xxxx...(90 chars).pdf   ← 超长 base 截 70
OK  01_Literature/edge.pdf
```

### 4.6 平板端 APK 交付

```
pwsh android/build_apk.ps1
→ APK: MicroBench-平板工作台.apk (1.0 MB)
→ 交付验收 5 passed
→ APK 内 assets/index.html 与仓库根那份 sha256 完全相同
```

---

## 5. 合并时改掉的两个「护栏比代码差」的护栏

合并时发现两条护栏**要求的是比现状更差/更脆的实现形态**，
已按「护栏要断言**行为**而不是字面量」改写：

| 护栏 | 原来断言 | 为什么不合理 | 改成 |
|---|---|---|---|
| `test_formula_image_clip_runs_in_thread` | 源码里有字面量 `"asyncio.to_thread(_pdf_clip_png"`（**单行**） | 对方把同一个调用**换行**写了 → 语义完全相同但字符串不匹配 → **假红** | 去掉空白后比对 + 反向断言「没有裸调用」 |
| `test_sync_files_channel_runs_in_thread` | 源码里有 `"asyncio.to_thread(_sync_push_files)"` | 对方把整个端点改成**同步 def**（FastAPI 自动丢 anyio 线程池）→ `to_thread` 反而**多余**（线程池里再开线程）→ 护栏在要求一个更差的形态 | 断言**行为**：① 端点不是 `async def` ② 重活调用仍被调用到 |

> **教训**：字符串断言型护栏是**格式的敌人**。改个换行就假红，
> 而真正该守的性质（重活不堵事件循环）反而没被守住。
> 判据应该问「**这个性质成立吗**」，不是问「**这行字还在吗**」。

---

## 6. 对方设备下一步

```powershell
# 1) 拉代码
git pull --rebase origin feat/audit-2026-10-27   # 或 checkout main 后 pull

# 2) 全量测试（平板的 node 套件会自动被 pytest 跑一遍）
python -m pytest tests -q -m "not slow"     # 期望 740 passed / 1 skipped

# 3) 重打 APK（交付物只能由构建脚本产出）
pwsh android/build_apk.ps1

# 4) 装到平板人工过一遍
#    - 打开应用（不是白屏，boot 正常）
#    - 划词问一句需要推理的问题 → 4~6 秒出正文
#    - 翻译一段话 → 译文完整
```

### ⚠ 三条注意

1. **合并改了 `pdf_cache` 的缓存键**（去掉词典维度、加入 `EXTRACT_IMPL_REV`）
   → 磁盘缓存会**整体重建一次**（27 篇 ≈ 72s，一次性）。
   **译文快照不受影响**（`EXTRACTOR_VERSION` 仍是 5，没 +1）。
2. **`/api/sync` 端点从 `async def` 改成了同步 `def`** → 同步时不会卡事件循环了。
   但如果你在 `app.py` 里给 `api_sync` 加过东西，注意它现在是**同步函数**。
3. **手册里的 `cache_key(pdf, dict)` 调用形式已作废** → 统一成 `cache_key(pdf)`。

---

## 7. 合并中值得记住的三件事

1. **两台设备各自审计 → 结论必然撞车**。撞车不是灾难，是让两边都对的最好机会。
   判据只有一个：**代码事实**（签名、import、调用点），不是「谁审计得更晚」。
2. **「加一个维度防撞键」要先问：这个维度影响不影响缓存内容**。
   不影响的话，加它不是修 bug，是引入一个更隐蔽的故障（本轮就撞上了）。
3. **字符串断言型护栏是格式的敌人**。本轮两条假红都是「对方只是换了个写法」。

---

## 8. 相关文档

- [`docs/AUDIT_2026-10-27.md`](AUDIT_2026-10-27.md) —— 本机第四轮全量审计
- [`docs/AUDIT_2026-10-27-REMOTE-R2.md`](AUDIT_2026-10-27-REMOTE-R2.md) —— 对方第二轮审计
- [`docs/AUDIT_2026-10-27-REMOTE.md`](AUDIT_2026-10-27-REMOTE.md) —— 审对方提交的报告
- [`docs/HANDOFF_2026-10-27-TABLET-QA-BUDGET.md`](HANDOFF_2026-10-27-TABLET-QA-BUDGET.md) —— 平板端答疑预算
- [`docs/HANDOFF_2026-10-25-TABLET-ADAPT.md`](HANDOFF_2026-10-25-TABLET-ADAPT.md) —— 平板移植清单
- [`docs/SYNC_CONTRACT.md`](SYNC_CONTRACT.md) —— 跨设备同步红线
