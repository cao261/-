# 性能优化：电脑端「打开略微卡顿」的取证与修复（2026-10-27）

> **用户原话**："我发现在电脑上打开略微有些卡顿"
>
> 这份文档记录的是**怎么定位**的（不是猜的），
> 以及**哪些路试过但没用**（省得下一个人再试一遍）。

---

## 0. 结论先行

用户感受到的卡顿有三个来源，**都实测钉死**：

| # | 现象 | 根因 | 修复 | 实测效果 |
|---|---|---|---|---|
| ① | **打开一篇论文要等 2.5~9.7 秒** | pdfplumber 每次都重新解析整篇 PDF | 加磁盘缓存（键含版本） | **2496ms → 50ms（快 50 倍）** |
| ② | 首屏慢、请求排成两批 | `async def` 端点里调**同步磁盘扫** → 阻塞事件循环 | 改成同步 `def`（丢线程池） | 6 个请求**全部并行**，重复请求清零 |
| ③ | 滚动时掉帧 | 滚动监听器**没节流**，每个事件都写 DOM | rAF 节流 + 状态没变就不写 | 帧 max **391ms → 23ms** |

---

## 1. 取证方法（可复用）

### ① 首屏：CDP + Timing API

用 Edge 的 `--headless=new` + CDP 拿真实数据，不靠读代码猜：

- `performance.getEntriesByType('navigation')` → domInteractive / loadEvent
- `performance.getEntriesByType('paint')` → 首屏绘制时刻
- **在页面脚本之前注入 `fetch` 包装器** → 拿到每个请求的**发起时刻**和**耗时**
  → 这一步是关键：能看出「串行瀑布」还是「并行」
- `PerformanceObserver({entryTypes:['longtask']})` → 主线程长任务
- `Profiler.start/stop`（CDP）→ 按函数排 self time，**找出真正的热点**

### ② 后端：分段打点

写一个临时脚本，按阶段计时（`read_bytes` / `extract_text_from_pdf` /
`_resolve_document_glyphs` / `split_into_sections`），
比在生产代码里塞 print 干净。

---

## 2. 根因一：PDF 抽取（最主要）

### 现象

打开一篇论文 → `/api/paper/open_vault_pdf` **2471ms**，
其中 **90% 是 `extract_text_from_pdf`**。

全库 27 篇 PDF 抽一遍：

    合计 72224 ms，平均 2675 ms/篇，最慢 9680 ms（36 页那篇）

### 试过但**没用**的三条路（都实测过）

| 思路 | 实测 | 为什么不行 |
|---|---|---|
| **线程池并行抽页** | 4 workers **0.97x**，8 workers **0.74x** | GIL + pdfplumber 内部锁，越多越慢 |
| **换 PyMuPDF 抽文本** | 快 **20.6x**（52ms vs 1075ms），字符量只差 -0.8% | ❌ 会破坏双栏顺序（§4.5 红线）+ **丢符号标记**（fitz 的 glyphs 全 0，`⟦G:n⟧` 会丢） |
| **减少 pdfplumber 的调用次数** | `extract_text` + `extract_words` 共用同一份 chars 缓存 | 本来就没重复解析，慢的是首次解析本身 |

> **教训**：**先量"瓶颈在哪一层"，再决定优化方向**。
> 这次如果直接换 PyMuPDF，会换来"双栏顺序错乱 + 符号丢失"两个更严重的问题
> —— 性能提升的代价是**正确性**，那是负优化。

### 采纳的方案：磁盘缓存

`microbench/pdf_cache.py`。实测 **第二遍 82ms（842x）**，体积 2.5MB。

**缓存键必须包含三样**，缺一个就会缓存出脏数据：

1. PDF 的 `(mtime_ns, size)` —— 文件改了必须失效
2. `EXTRACTOR_VERSION` —— 抽取算法改了必须失效
   （跟译文快照的版本闸门同款思路，§4.18）
3. **符号词典的 `(mtime_ns, size)`** —— 词典认出了新符号，
   文本里 `⟦G:n⟧` 对应的真实字符就变了 → 必须重解

**放哪**：vault 的 `logs/pdf_extract_cache/` —— **不在坚果云同步根里**。

> **为什么缓存不进同步根**：同步根是两台设备的**数据面**。
> 缓存写坏 → 两台设备的正文对不上，而且极难排查。
> 缓存是**性能**优化，不是数据 → 各自缓存、各自变快，互不影响。

**缓存坏了不能挡住功能**：读失败当未命中，写失败只记日志。

护栏：`tests/test_pdf_cache.py`（12 条）。

---

## 3. 根因二：首屏的串行瀑布 + 重复请求

### 实测到的两个问题

**① 重复请求**：`/api/vault/literature` 被请求 **2 次**（52ms 那次 + 273ms 那次）。

根因：`loadStats()` 末尾 `await loadRecentFeed()`，
而首屏的 `_bootDashboard` **又自己调了一次** → 打两次。

**② 串行瀑布**：

    第 1 批：/api/paper/folders        51ms 发出 → 161ms 才回来
             /api/vault/literature    52ms 发出 → 160ms 才回来
    第 2 批：/api/stats              200ms 才开始   ← 被第 1 批挡住了
             /api/llm/status         201ms
             ...

根因：这几个端点是 `async def`，但里面直接调**同步的磁盘扫描**
（`get_vault_stats()` / `root.rglob("*.pdf")` / `base.iterdir()`）
→ **阻塞事件循环** → 并发请求被排成串行。

### 修法

| 端点 | 改法 |
|---|---|
| `/api/stats` | 同步 `def`（本来就是）—— 但内部是同步磁盘扫，**在 async 里就阻塞** |
| `/api/vault/literature` | `async def` → **同步 `def`** |
| `/api/paper/list_vault_pdfs` | `async def` → **同步 `def`** |
| `/api/paper/folders` | `async def` → **同步 `def`** |

> 同步 `def` 的端点，FastAPI 会丢进 **anyio 线程池**执行 → 不冻结事件循环。
> 这跟 `api_fetch_paper` 用同步 `def` 是**同一个理由**（那里有注释说明）。

**同时**：`loadStats()` 不再拉 feed（feed 归 `_bootDashboard` 管；
需要刷新的地方——比如删笔记后——自己显式调 `loadRecentFeed()`）。

### 效果

    请求数        7 → 6（重复请求清零）
    并行度        两批串行 → 全部同时（27~30ms 一起发出）
    loadEvent     234ms → 175ms

---

## 4. 根因三：滚动掉帧

### 实测

CDP 的 CPU Profiler 显示，`onScroll` 是首屏**唯一**的热点函数：

    onScroll @ :2595    236 samples (7%)   ← 远超第二名
    滚动帧 p90 = 96ms，max = 391ms

它挂在 `window` 的 `scroll` 上（`{passive: true}`，但**没节流**），
快速滚动时每个事件都写一次 `header.setAttribute(...)` → 强制样式重算。

### 修法

- **rAF 节流**：一帧最多写一次
- **状态没变就不写**：`scrolled !== lastState` 才动 DOM

行为完全不变（还是 `scrollY > 8` 就切状态），只是不再每个事件都写。

### 效果

    滚动帧 max     391ms → 23ms
    滚动帧 p90      96ms → 23ms
    CPU 热点榜      onScroll 直接消失

---

## 5. 还没动的（有意留下）

| 项 | 为什么不动 |
|---|---|
| `index.html` 是 **423KB 单文件**，首屏 JS 解析编译约 148ms | 拆文件要动几百处引用，风险远大于收益；而且本地服务 148ms 不明显 |
| 字体从 Google Fonts 异步加载 | 已经是 `media="print"` 非阻塞方式 |
| KaTeX / PDF.js | 已经懒加载（不进首屏） |
| `_real_output_stats()` 扫同步根 | 有 15 秒缓存，且首屏只调一次 |

---

## 6. 验收

```powershell
# 1) 拉代码
git pull --rebase origin main

# 2) 后端改动要重启服务才生效
#    （前端是 no-store，刷新即生效）

# 3) 全量测试
python -m pytest tests -q -m "not slow"

# 4) 人工确认
#    - 打开一篇论文 → 应该**立刻**出正文（原来是 2.5~9.7 秒）
#    - 反复打开同一篇 → 第二次几乎瞬时
#    - 滚动页面 → 不再掉帧
#    - 首屏看板 → 文献流和数字都正常出现
```

---

## 7. 一句话总结

**用户说的「略微卡顿」，实测是三个可量化的真问题**（PDF 重抽 2.7 秒/篇、
首屏请求串行、滚动不节流），**不是一个模糊的"感觉慢"**。

> **通用纪律**：
> - **先定位根因，不要猜** —— 用户说"卡"时，正确反应是**加探针 + 实测**，
>   不是先猜一个原因就去改。
> - **优化前先量"瓶颈在哪一层"** —— 这次三条路（并行/换库/减调用）
>   全试过全证伪，才找到唯一有效的方向。
> - **性能优化不能拿正确性换** —— 换 PyMuPDF 快 20 倍，
>   但会破坏双栏顺序和符号标记 → 那是负优化。
