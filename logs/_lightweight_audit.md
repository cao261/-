# MicroBench 轻量化审计报告

**审计日期**: 2026-09-24
**审计对象**: `D:\Desktop\杂物\琐事\个人工作台\microbench\` (Python + 单文件 HTML)
**审计目标**: 找出可削减/移除/合并的代码
**审计方式**: 只读,基于 grep 反向索引 + 行号引用

---

## 1. 总体数字

### 1.1 各文件 LOC

| 文件 | 行数 | 字节 |
|---|---|---|
| `static/index.html` | **5238** | ~254 KB |
| `test_bench.py` | **1854** | ~86 KB |
| `app.py` | **2018** | ~96 KB |
| `llm.py` | 908 | ~41 KB |
| `generator.py` | 659 | ~31 KB |
| `paper_fetcher.py` | 408 | ~17 KB |
| `paper_text.py` | 384 | ~17 KB |
| `plot_compare.py` | 353 | ~14 KB |
| `translation.py` | 203 | ~9 KB |
| `__init__.py` | 1 | 21 B |
| **合计** | **~12 026** | ~570 KB |

### 1.2 估算可削减行数

| 类别 | 行数 |
|---|---|
| A. 死代码(路由+函数+helpers) | **~340 行** |
| B. 重复代码(可合并/模板化) | **~160 行** |
| C. 不必要依赖 | 无 requirements.txt 文件;`send2trash` 是可选 fallback |
| D. 过度抽象 / 简化实现 | **~30 行** (inline import + 一些 helper) |
| **总计可削减** | **~530 行 (~4.4%)** |

⚠️ **关键现实**: 524 KB 中的 254 KB 来自 `static/index.html`,但前端 CSS+HTML 模板+DOM 是必要的(整个 UI 在里面),可削减空间集中在 **JS 函数/重复模板/死路由 + 死 Python 函数**。

---

## 2. 死代码清单

### 2.1 死路由(后端定义,前端从不调用)

> 对照方法:列出 `app.py` 中所有 `@app.{get,post,delete}(...)`,与 `static/index.html` 中 `fetch("/api/...")` 交叉对照。

**A. 整段非流式 LLM endpoint (3 个,共 ~216 行) — 100% 死代码**

前端在 line 3921 / 3977 / 4409 / 4621 **只调用 `_stream` 版本**,从未调用非流式版本:

| 死路由 | 行号 | 行数 | 替代 |
|---|---|---|---|
| `POST /api/paper/explain` | `app.py:1329-1397` | 69 | `…_stream` |
| `POST /api/paper/cross_qa` | `app.py:1400-1486` | 87 | `…_stream` |
| `POST /api/paper/explain_selection` | `app.py:1633-1691` | 59 | `…_stream` |

**连带**:`_disconnect_watcher(request, cancel_event)` helper (line 92-103, 12 行) — 只被这 3 个死 endpoint 用到 (line 1374 / 1453 / 1661),函数定义和注释可以一起删除。

**B. 完全未暴露给前端的路由 (9 个,共 ~250 行)**

| 死路由 | 行号 | 行数 | 用途推测 |
|---|---|---|---|
| `GET /api/paper/download_pdf` | `app.py:472-536` | 65 | arXiv PDF 自动下载(可能预留) |
| `GET /api/paper/bibtex` | `app.py:876-895` | 20 | BibTeX 导出(预留) |
| `POST /api/paper/summarize` | `app.py:956-976` | 21 | 单论文摘要(RAG 用) |
| `POST /api/ask` | `app.py:978-1040` | 63 | vault RAG 问答(预留) |
| `POST /api/plot/extract` | `app.py:1047-1070` | 24 | 图像曲线提取(预留) |
| `POST /api/plot/calibrate` | `app.py:1072-1084` | 13 | 像素坐标→真实坐标(预留) |
| `POST /api/plot/compare` | `app.py:1086-1116` | 31 | 论文 vs 复现数据对比(预留) |
| `GET /api/translation/cache_stats` | `app.py:1888-1892` | 5 | debug 用,文档化 |
| `POST /api/paper/paste_analyze` | `app.py:2033-2095` | 63 | 粘贴文本一步式(预留) |

> **验证方法**: `grep "/api/paper/download_pdf\|/api/paper/bibtex\|/api/paper/summarize\|/api/ask\|/api/plot\|/api/translation/cache_stats\|/api/paper/paste_analyze" static/index.html` → 0 命中。

### 2.2 死函数(JS / Python)

**A. 前端 `askLLMCancelable` (~48 行) — 100% 死代码**

- 定义: `index.html:1325-1372`
- 引用: `index.html:1374` (仅 `window.askLLMCancelable = …`,无任何调用方)
- 全文搜索: `grep -n "askLLMCancelable\(" index.html` → 0 命中
- 整个 `askLLMCancelable` 和 `window.askLLMCancelable = askLLMCancelable` 都可删除
- 唯一保留: `cancelActiveAsk` (line 1319) 仍被 `askLLMStream` 用作 cancel 回调(必须保留)

**B. 前端 `renderMarkdown` (4 行) — 100% 死代码**

```js
// index.html:3585-3589
// Convenience: apply formatMarkdownLite + renderMathIn in one go
function renderMarkdown(el, rawText) {
  el.innerHTML = formatMarkdownLite(rawText);
  renderMathIn(el);
}
```

- 全文搜索: `grep "renderMarkdown\b" index.html` → 仅定义点,无任何调用

**C. 前端 `_refreshSnapshotBadge` (2 行) — 死函数(stub)**

```js
// index.html:2747-2748
async function _refreshSnapshotBadge() { /* future: show count badge in TopNav */ }
```

- 仅在 `index.html:2581` 通过 `typeof _refreshSnapshotBadge === "function"` 防御性调用,函数体是空 stub
- 这 2 行 + 防御性 typeof 都可以删

**D. 前端 `_listSubfoldersForKind(kind)` (12 行) — 100% 死代码**

```js
// index.html:1519-1530
async function _listSubfoldersForKind(kind) {
    const r = await fetch(`/api/paper/folders?topic_category=...&kind=...`);
    ...
}
```

- `refreshPaperSubfolderList()` (line 1723) 内部 inline 调 fetch,不通过这个 helper
- 全文搜索: `grep "_listSubfoldersForKind" index.html` → 仅定义点

**E. 测试函数未挂载到 `run_all()` (~7 个,共 ~390 行)**

- `test_bench.py:2094-2097` 的 `run_all()` 末尾是 `test_explain_endpoint_returns_499_on_cancel()` 调用
- 下面这些定义了但 **从未在 `run_all()` 中被调用**:

| 未挂载测试 | 行号 | 行数 |
|---|---|---|
| `test_chat_stream_async_basic` | `test_bench.py:1855-1916` | 62 |
| `test_chat_stream_async_cancel_mid_stream` | `test_bench.py:1917-1989` | 73 |
| `test_explain_stream_endpoint_returns_sse` | `test_bench.py:1990-2052` | 63 |
| `test_paper_serve_pdf_endpoint_validates_relative_path` | `test_bench.py:1303-1336` | 34 |
| `test_vault_file_endpoint_serves_markdown` | `test_bench.py:1337-1382` | 46 |
| `test_llm_chat_propagates_max_retries_to_primary` | `test_bench.py:1270-1290` | 21 |
| `test_llm_retry_exception_classes_include_ssl_and_chunked` | `test_bench.py:1291-1302` | 12 |

> 这与死路由相关 — `serve_pdf`/`vault/file` 已被前端调用,但这些 endpoint 的测试缺失说明回归保护没起作用。**修复建议**:把测试加进 `run_all()`,或者直接删除(若不打算测)。

### 2.3 死 import / inline 重复 import

**A. 后端 `app.py` 顶层缺 import,函数内 inline import 重复**

```python
# app.py:422
from .generator import ALLOWED_TOPIC_CATEGORIES, _safe_path
# app.py:515
from .generator import sanitize_filename, _safe_path
# app.py:831
from .generator import sanitize_filename, _safe_path
# app.py:1154
from .generator import sanitize_filename, _safe_path
# app.py:1230
from .translation import chunk_section_text, get_cache
# app.py:1425
from .translation import build_cross_section_context
# app.py:1783
from .translation import build_cross_section_context
# app.py:1891
from .translation import get_cache
# app.py:1909
from .translation import chunk_section_text, get_cache
# app.py:1977
from .translation import get_cache
```

`from .translation import chunk_section_text, get_cache` 出现 2 次,`from .translation import build_cross_section_context` 出现 2 次。**移到顶部 import 不影响行为,减少 ~10 行 inline import。**

**B. `import re as _re` (line 1596) — 仅用一次**

```python
# app.py:1596
import re as _re
cleaned = _re.sub(r"<think>[\s\S]*?</think>\s*", "", cleaned).strip()
```

`re` 没在文件其他地方用过(只有 line 1596),可以放进文件顶部 import。

**C. `__import__("re")` (line 494) — 极丑陋**

```python
# app.py:493-495
is_arxiv = "arxiv" in identifier.lower() or bool(
    __import__("re").search(r"^\d{4}\.\d{4,5}", identifier.strip())
)
```

`re` 在 line 740 已经 import 过(`import re`),这里又 `__import__("re")` 是冗余的怪异写法。删除后用上面已导入的 `re`。

---

## 3. 重复代码清单

### 3.1 3 个流式 LLM endpoint 共享大量样板 (3 段 ~120 行)

`api_explain_stream` / `api_cross_qa_stream` / `api_explain_selection_stream` 共享:

```python
# 模板结构 (重复 3 次,每次 ~15 行)
if not llm.is_configured():
    raise HTTPException(...)

async def event_gen():
    try:
        chunk_iter = llm.chat_stream_async(
            messages=[
                {"role": "system", "content": <SOME_PROMPT>},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=<X>,
            temperature=0.3,
        )
        async for chunk in _stream_llm_event_gen(chunk_iter, ...):
            yield chunk
    except asyncio.CancelledError:
        return
    except llm.LLMError as e:
        yield f"data: {json.dumps({"error": str(e)}, ensure_ascii=False)}\n\n"

return StreamingResponse(event_gen(), media_type="text/event-stream")
```

可以抽出一个 `_stream_endpoint(req, system_prompt, user_msg, max_tokens=...)` helper,把 3 个 endpoint 缩减到 ~25 行各。

### 3.2 `api_translate_section` 和 `api_translate_page` 重复 (~80 行)

两个 endpoint (`app.py:1210` 和 `app.py:1895`) 都有相同的 chunk→loop→cache 模式:

```python
from .translation import chunk_section_text, get_cache
cache = get_cache()
chunks = chunk_section_text(<TEXT>)
translated_parts = []
for idx, chunk in enumerate(chunks):
    if len(chunks) > 1:
        user_msg = f"...第 {idx+1}/{len(chunks)} 段: ..."
    else:
        user_msg = f"... {TEXT} ..."
    translated_parts.append(
        llm.chat(messages=[...], max_tokens=1500, temperature=0.2, timeout=...)
    )
translation = "\n\n".join(translated_parts).strip()
cache.put(<KEY>, <TEXT>, <MODEL>, result)
return result
```

**可抽成 `_chunked_translate(text, cache_key, system_prompt, build_user_msg, model)` helper**,两个 endpoint 各缩到 ~25 行。

### 3.3 前端 `askLLMStream` 和 `askLLMCancelable` 共享 cancel-button HTML (重复 1 次)

```js
// index.html:1335 (askLLMCancelable)
btn.innerHTML = `<span>⏳ 解答中...</span> <button type="button" data-cancel-ask ...>✕ 取消</button>`;
// index.html:1401 (askLLMStream)
btn.innerHTML = `<span>⏳ 解答中...</span> <button type="button" data-cancel-ask ...>✕ 取消</button>`;
```

**但 askLLMCancelable 是死代码**,删了就剩一份,无需抽 helper。

### 3.4 多个 modal 创建/关闭样板 (showSnapshotModal / showDiagnoseModal / showVaultSaveModal / showKeywordPickerModal)

- `showSnapshotModal` (line 2637-2742) ~106 行
- `showDiagnoseModal` (line 2787-2872) ~85 行
- `showVaultSaveModal` (line 1533-1625) ~92 行
- `showKeywordPickerModal` (line 4852-4916) ~64 行
- 都共享 `_wireModalDismissals(overlay, closeFn)` helper (line 1489) — 这个 helper 已经存在,**不要再合并,是有意分开**(每个 modal 的内容/字段不同,合并会更糟)。

---

## 4. 简化建议(按"低成本 + 高收益"排序)

### 🏆 Top 3 建议

#### ✅ #1:删除 3 个非流式 LLM endpoint + `_disconnect_watcher`

- **影响范围**: `app.py` (lines 92-103, 1329-1397, 1400-1486, 1633-1691)
- **预估行数减少**: **228 行** (12 + 69 + 87 + 59)
- **风险等级**: 🟢 **低**
  - 前端从未调用,删除后无任何功能影响
  - `_disconnect_watcher` 仅被这 3 个 endpoint 使用,一起带走
- **保留**: 流式版本 + `_stream_llm_event_gen` (line 106) 保留
- **验证方法**: `git grep "fetch.*paper/explain\|/paper/cross_qa\|/paper/explain_selection\"" -- "*.html"` 应只匹配 `…_stream`

#### ✅ #2:删除 9 个未被前端调用的路由

- **影响范围**: `app.py` (lines 472-536, 876-895, 956-976, 978-1040, 1047-1116, 1888-1892, 2033-2095)
- **预估行数减少**: **~242 行**
- **风险等级**: 🟢 **低**(若确认是"预留"功能,不是临时停用)
- **建议保留**:`/api/paper/upload_pdf` 和 `/api/paper/serve_pdf` — 它们被前端调(line 2233, 3294)
- **决策标准**: 路由如果不在 `static/index.html` 的 `fetch("/api/...")` 列表里,且未在 `test_bench.py` 中作为"已通过"项,即可删除
- **验证方法**: 路由列表 → grep 前端 fetch 列表 → diff

#### ✅ #3:删除前端 4 个死函数 (`askLLMCancelable` + `renderMarkdown` + `_refreshSnapshotBadge` + `_listSubfoldersForKind`)

- **影响范围**: `static/index.html`
- **预估行数减少**: **~66 行** (48 + 4 + 2 + 12)
- **风险等级**: 🟢 **低**
  - `askLLMCancelable` 无任何调用方,直接删
  - `renderMarkdown` 仅在注释上方定义,无调用方
  - `_refreshSnapshotBadge` 函数体是空 stub,只在 `typeof` 防御性检查中被引用 — 可一并删 line 2581 的 typeof 检查
  - `_listSubfoldersForKind` 无调用方,`refreshPaperSubfolderList` 走 inline fetch
- **保留**:`cancelActiveAsk` (line 1319) 仍被 `askLLMStream` 用作 cancel 回调

### 🥈 可选建议

#### ☐ #4:抽 `_stream_llm_endpoint(...)` helper 合并 3 个流式 endpoint

- **影响范围**: `app.py` lines 1706-1885
- **预估行数减少**: ~80 行 (3×27 → 1×27 + 3×8 调用)
- **风险等级**: 🟡 **中**(参数化 prompt/timeout/event_gen 的差异要小心)
- **可选**: 如果计划长期维护 3 个独立 stream,合并收益不大;如果 2 个中任意 1 个是死代码(本审计中都不是),则无需合并

#### ☐ #5:抽 `_chunked_translate(...)` helper 合并 `translate_section` + `translate_page`

- **影响范围**: `app.py` lines 1210-1292 和 1895-1966
- **预估行数减少**: ~40 行
- **风险等级**: 🟡 **中**(两个 endpoint 的 cache key 形状不同、user_msg 模板不同、返回字段不同)

#### ☐ #6:把 inline import 移到 `app.py` 顶部

- **影响范围**: `app.py` lines 422, 515, 831, 1154, 1230, 1425, 1596, 1783, 1891, 1909, 1977
- **预估行数减少**: ~10 行 + 显著提高可读性
- **风险等级**: 🟢 **低**(纯重构)

#### ☐ #7:删除 7 个未挂载的测试函数(或挂载到 run_all())

- **影响范围**: `test_bench.py` lines 1270-1290, 1291-1302, 1303-1336, 1337-1382, 1855-1916, 1917-1989, 1990-2052
- **预估行数减少**: ~311 行
- **风险等级**: 🟡 **中**(决策:这些是"未来要加进 run_all()"还是"死代码"?如果项目活跃,保留并挂载;否则删除)
- **建议**: 把 `test_chat_stream_async_*` 和 `test_explain_stream_endpoint_returns_sse` 加进 `run_all()`(对应活跃的流式 endpoint),删除其他 4 个未挂载的

---

## 5. 不要动的部分(看起来重复,但合并会出问题)

### 5.1 4 个 modal (`showSnapshotModal` / `showDiagnoseModal` / `showVaultSaveModal` / `showKeywordPickerModal`)

虽然都使用 `_wireModalDismissals` 共用 ESC/click-out 关闭逻辑,**每个 modal 的字段、按钮、数据加载都完全不同**,强行合并会让代码更难读。当前抽象层级合理。

### 5.2 `askLLMStream` 和 `askLLMCancelable`

虽然名字相似,但 `askLLMCancelable` 是死代码。**不要合并** — 直接删 `askLLMCancelable` 即可,`askLLMStream` 是唯一的活路径。

### 5.3 4 个 `_snapshot` 相关 endpoint (`snapshot/list` / `load` / `save` / `delete`)

虽然都操作 snapshot 文件,但**操作类型完全不同**(list/load/save/delete),合并会损失语义清晰度。当前粒度合适。

### 5.4 `chat` / `chat_async` / `chat_stream_async` 三个 LLM 函数

虽然都是"调 LLM",但:
- `chat` — 同步、单次、用于短 prompt(翻译/RAG)
- `chat_async` — 异步、可取消、用于非流式 LLM endpoint (虽 endpoint 死了,但函数本身可被未来用)
- `chat_stream_async` — 异步、流式、用于流式 endpoint

**三者签名/重试逻辑/错误处理都不同**,不能合并。

### 5.5 `save_literature_card` / `save_bilingual_reading_card` / `save_qa_card` (generator.py)

虽然都是"生成 .md + 写到 vault",但:
- 路径不同 (`03_文献速览卡片` / `02_双语精读笔记` / `04_AI划词答疑`)
- 模板内容不同(单卡片/双语/答疑)
- filename 规则不同

**共用 helper**: `_safe_path`, `_render_template`, `sanitize_filename` 已经被抽出,不需要再合并。

### 5.6 `_diagnose` 系列 (`_recordDiag` / `_clearDiagLog` / `_diagSummary` / `_renderDiagnoseTable`)

都是诊断页内部 helper,虽然有些只是单行函数,但**它们绑定同一个数据结构 `_diagLog`**,合并会破坏职责分离。

### 5.7 `_qa-panel` 单一 modal

只有一个 `#reader-qa-panel`(`grep "reader-qa-panel" index.html` → 仅 1 个 `<div id="reader-qa-panel">`),不是"多个"。**审计提示中的"多个 qa panel"假设不成立**,无需处理。

---

## 6. 快速摘要

| 类别 | 项目 | 行数 | 风险 | 优先级 |
|---|---|---|---|---|
| 死路由 | 3 个非流式 LLM endpoint + 9 个未用路由 | ~470 | 🟢 低 | 🔥 高 |
| 死函数 | `askLLMCancelable` + `renderMarkdown` + `_refreshSnapshotBadge` + `_listSubfoldersForKind` | ~66 | 🟢 低 | 🔥 高 |
| 死 helper | `_disconnect_watcher` (随 3 个 endpoint 一起) | 12 | 🟢 低 | 🔥 高 |
| 重复 | 3 个流式 endpoint 模板 | ~80 | 🟡 中 | ☐ 中 |
| 重复 | `translate_section` / `translate_page` 重复 | ~40 | 🟡 中 | ☐ 中 |
| 重构 | inline import 提到顶部 | ~10 | 🟢 低 | ☐ 中 |
| 测试 | 7 个未挂载测试 | ~311 | 🟡 中 | ☐ 低 |
| **Top 3 高优先级** | 死路由 + 死函数 + `_disconnect_watcher` | **~548 行** | 🟢 | **必做** |
| **全部建议** | 上述 7 项总和 | ~989 行 | - | - |

**最终建议:实施 Top 3 优先级建议,可削减 ~548 行 (~4.6% 总 LOC),风险低、收益高。可选建议根据未来维护需要再决定。**
