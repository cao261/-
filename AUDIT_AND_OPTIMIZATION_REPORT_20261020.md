# MicroBench 全库审计 · 优化 · 安全加固报告

- **项目**：`D:\Desktop\杂物\琐事\个人工作台`（MicroBench 微电子科研工作台，FastAPI + 原生 JS + Obsidian 双链库）
- **基线 commit**：`369c60c`（工作区另有未提交改动，本轮全部纳入修复并提交）
- **审计范围**：`microbench/app.py`、`llm.py`、`generator.py`、`paper_fetcher.py`、`paper_text.py`、`plot_compare.py`、`translation.py`、`desktop.py`、`static/index.html`、`test_bench.py`、`_test_regressions.py`、`requirements.txt`
- **运行环境**：Windows / Python 3.12.10 / FastAPI 0.138.0 / uvicorn 0.49.0 / httpx 0.28.1 / Pillow 12.2 / numpy 2.1.3
- **日期**：2026-10-20

---

## 0. 结论

| 维度 | 修复前 | 修复后 |
|---|---|---|
| API 端点 | 41 | 41（全部逐个实测通过） |
| 自动化测试 | 0（pytest 套件不存在，存量 `test_bench.py` 自 2026-09-24 起长期红灯） | **130 项 pytest**（118 离线 + 12 网络/LLM）+ 39 项回归守卫 + 存量套件转绿 |
| 严重/高危缺陷 | 12+ | 0 |
| 中危缺陷 | 14 | 0 |
| 低危/清理项 | 11+ | 0 |
| 已知功能不可用 | 翻译（2 端点）、图表提取（1 端点）、加载更多分页、划词答疑 scope 路由、周报统计、避坑计数器 | 全部恢复 |

**交付状态：可运行。** 服务可启动，41 个端点全部实测通过，前端实机验证零控制台错误，vault 零污染。

---

## 1. 阶段一：优化点识别与实施

### 1.1 性能优化（均经前后实测）

| 位置 | 问题 | 优化 | 实测 |
|---|---|---|---|
| `plot_compare.py:44` | 逐像素纯 Python 双趟循环 + `[[False]*w]*h` 掩码（4K 图 700MB 内存） | Pillow C 通道运算做阈值化 + numpy 向量化聚类/中位数；无 numpy 时回退纯 Pillow | 4000×3000：**3.8s → 0.199s**（19×）；输出**逐点完全一致** |
| `plot_compare.py:103` | Pillow 只在 2× 阈值报错，1~2× 之间仅警告 | 新增 `MAX_IMAGE_PIXELS = 40MP` 硬上限 | 49MP 图片被明确拒绝 |
| `paper_text.py:256` | `len("".join(parts))` 写在循环里 → O(n²) | 运行时偏移累加 | 1000 页：1145ms → 137ms |
| `generator.py:102` | 每次渲染新建 `jinja2.Environment`（占渲染耗时 85%） | 模块级单例 `_JINJA_ENV` | 1.42ms → 0.21ms/次，输出字节级一致 |
| `llm.py:671` | 每次调用新建 `httpx.AsyncClient`（无连接池/TLS 复用） | 按事件循环缓存的池化客户端（WeakKeyDictionary） | 每次 SSE 省一次 TLS 握手（~100-300ms） |
| `llm.py:95` | 限流器**持锁** `time.sleep`，`stats()` 被排队阻塞 | 锁内预留槽位、锁外睡眠 | `stats()` 从阻塞数秒变为即时返回 |
| `generator.py:159` | `search_vault` 在关键词循环内重复正则扫描全文 | 标题正则提到循环外 | O(关键词×正文) → O(正文) |
| `app.py:417` | `/api/stats` 序列化全量文献列表 | 只返回计数器 | 23 篇 6.3KB → 仅 4 个计数 |

### 1.2 功能补充

- **`force_scope` 真正生效**：服务端按档位截断上下文（term 2K / mid 8K / wide 20K / full 50K）。此前服务端**固定截断 10K**，导致两级路由（router）分配的 60-80K 预算被完全丢弃——"全文摘要"档位实际只给模型看了 1/6。
- **周报真正按 7 天聚合**：此前只取"最近修改的 3 篇"、无日期过滤，且一句话总结是硬编码的"本周推进了核心器件仿真网格收敛"，即使本周零产出也照写。现在按窗口过滤并据实生成总结。
- **文献流服务端分页打通**：前端此前 `offset` 硬编码为 0，"加载更多"永远重拉第一页。
- **避坑计数器修正**：正则只匹配旧版 `### 坑 01：` 标题格式，实际写的是 `### 坑记录：…` → 仪表盘显示 4，真实 76。
- **复现工程重复创建不再静默覆盖 README**。
- **思考模型空响应自适应**：M2.7 在 `max_tokens` 不足时会把预算全花在 `<think>` 上并返回**空串**（实测 200 tokens → 空、800 → 正常，且阈值随题目难度漂移）。现在识别空内容并自动加倍预算重试一次。

---

## 2. 阶段二：全库代码审计

三路并行深度审计（llm.py / generator+fetcher 家族 / 前端契约），逐条复核当前工作树。

### 2.1 BLOCKER

| ID | 问题 | 位置 | 修复 |
|---|---|---|---|
| B-1 | `paper_text.py` 缺模块级 `import requests`，`_download_pdf_bytes` 抛 `NameError` → **所有 DOI→OA 全文抓取全挂**（502） | `paper_text.py` | 补模块级 import（该回归由本轮加固引入，已被端点测试捕获） |
| B-2 | `asyncio.to_thread(llm.chat, ...).strip()` —— `.strip()` 绑到**协程对象**而非返回值 → **翻译功能两个端点 100% 不可用**（`'coroutine' object has no attribute 'strip'`） | `app.py` ×2 | 改为 `(await asyncio.to_thread(...)).strip()` |

### 2.2 HIGH

| ID | 问题 | 修复 |
|---|---|---|
| H-1 | `api_plot_extract` 内部用 `file.read()`，而参数名是 `image` → **端点必崩** | 改回 `image.read()` 并加流式大小上限 |
| H-2 | 流式传输错误（`httpx.ReadError` 等）不是 `ConnectError`/`TimeoutException` 子类 → 裸抛 → SSE 无终止事件 → 前端把**半截答案当完整答案**渲染 | 捕获 `httpx.HTTPError` 全族并包装为 `LLMError` |
| H-3 | `content: null` 被当作**成功**返回 → `summarize_paper` 抛 TypeError（500）、`answer_with_context` 返回 `null`（静默错误答案） | 空内容判定为失败并触发预算重试 |
| H-4 | 重试放大：单次请求最坏 **7 次上游调用 / 12.5 分钟**；3 段翻译 = 21 次调用/分钟，**自己打爆自己要防的 RPM 限流** | 引入全局 `MAX_UPSTREAM_CALLS_PER_REQUEST = 6` 预算 |
| H-5 | `paper_fetcher` 的 doi.org 重定向链与 IEEE 抓取**无 SSRF 防护**，可让服务端探测内网/云元数据 | 守卫下移到 `paper_fetcher`，逐跳手写重定向并逐跳校验 |
| H-6 | 前端 `escapeHtml`（HTML 转义）被用来拼 **JS 字符串字面量** → `'` 变 `&#39;` → 内联处理器语法错误（按钮全废）；且 `x');alert(1);//` 是合法文件名 → **任意 JS 执行** | 新增 `escapeJsAttr`（HTML 属性层 + JS 字面量层双层转义） |
| H-7 | `sec.title`/`sec.section`（来自 PDF 标题解析）**未经转义直接进 innerHTML** → 恶意 PDF 可注入脚本 | 补 `escapeHtml` |
| H-8 | 导出双语笔记时用 `page_start`（页码）去索引**章节序号**数组 → 摘要块会被填成章引言的译文 | 分离 ordinal / page 两套索引空间 |

### 2.3 MEDIUM / LOW（全部修复）

`force_scope` 静默失效 · 429 "rate limit" 误判为额度耗尽导致反复降格 · 异步/流式路径缺失主模型限流 · SSE 路径无自动探针（降格后永远留在 Agnes）· 探针阻塞事件循环 · 限流器统计出现负数"距上次调用" · 客户端缓存泄漏事件循环 · `base_url` 内嵌凭据被吐给浏览器 · 上游错误体回显 · `<think>` 块原样流给前端 · YAML frontmatter 被含引号/换行的标题破坏 · 显式 `null` 可选字段触发 500 · 目标目录缺失时写盘 500 且泄露绝对路径 · `search_vault` 全量重扫 · 踩坑日记读改写竞态 · 踩坑日记日期硬编码导致清不掉 · 周报每周新增文件 · `get_vault_stats` 共享可变缓存对象 · 临时文件泄漏 / 无界内存下载 / 响应未关闭 · 抽样文件被测试套件按文件名子串误删 · `sanitize_filename` 保留 Windows 会剥离的尾点 · 死代码与重复分支 · `_Templates/05_*` 模板漂移 · `plot_compare.__all__` 里的 `if False else`。

---

## 3. 阶段三：安全测试

`tests/test_security.py` —— **77 项安全断言**，全部通过。

### A. 路径遍历（45 项）
9 种载荷（`../`、反斜杠、绝对路径、URL 编码、混合分隔符、`..` 本身）× 5 个文件类端点（`serve_pdf` / `vault/file` GET+DELETE / `pdf/info` / `vault/reveal`）全部拦截。另测：`project_name`、`filename_override`、`subfolder`、`snapshot paper_id` 越界。

### B. SSRF（4 项）
15 个内网载荷（回环 / IPv6 回环 / IPv4-mapped IPv6 / RFC1918 / 云元数据 169.254.169.254 / 十进制与十六进制 IP 写法 / `file://` / `ftp://` / `gopher://` / `0.0.0.0`）全部拒绝。
**实证**：本地起真实 HTTP 监听器，`/api/paper/fetch_full` 指向它 → 服务端**一次都没访问**。

### C. CSRF（7 项）
新增 Origin/Referer 守卫：跨站 POST / DELETE / LLM 探针 → 403；同源、无 Origin、`Origin: null`、跨站 Referer 均按预期处理；GET 不受影响。
> CORS 只管"能不能读"，管不了"能不能发"。任意外部网页可用隐藏 form 让本机服务替它干活：写周报、弹 explorer 窗口、**用用户自己的 API key 打 LLM 探针**。

### D. 资源耗尽（4 项）
40MB PDF / 10MB 图片 / 5000 页快照 / 20000 点曲线 / 49MP 图片，全部被前置拦截（流式计数，不再先吃进内存再判断）。

### E. 凭据泄露（3 项）
`/api/llm/status` 响应体中不含 `sk-`、`Bearer`；`base_url` 中的 `user:pass@` 被剥离；错误文本中的 key 形态被脱敏。

### F. 注入与校验（10 项）
YAML frontmatter 注入（标题含 `"` 与换行 → **真实注入出 `status:` / `injected:` 键**）→ 已修复；代码围栏截断 → 改用 4 反引号；白名单强制；422 错误信息改为可读字符串（原先是对象数组，前端显示 `[object Object]`）。

---

## 4. 阶段四：逐端点可运行性验证

`tests/test_endpoints.py` —— **53 项端点测试**（41 离线 + 12 网络/LLM）。

| 分组 | 端点 | 结果 |
|---|---|---|
| 静态/统计 | `GET /`、`/api/stats`、`/api/vault/literature`（含分页推进验证） | ✅ |
| 文献卡片 | `/api/paper/save`（含 null 字段、引号标题）、`/api/vault/file` GET/DELETE | ✅ |
| 目录/文件 | `/api/paper/folders`、`/api/paper/find_pdf`、`/api/paper/serve_pdf`、`/api/pdf/info` | ✅ |
| 上传/解析 | `/api/paper/upload_pdf`、`/api/paper/split_text`、`/api/paper/paste_analyze` | ✅ |
| 复现/知识 | `/api/reproduction/create`（含重复创建/遍历/非法类型）、`/api/troubleshoot/add`、`/api/weekly/generate` | ✅ |
| 导出 | `/api/paper/export_bilingual`、`/api/paper/export_qa` | ✅ |
| 图表数字化 | `/api/plot/extract`、`/api/plot/calibrate`、`/api/plot/compare`（含全零曲线判定） | ✅ |
| 快照 | `/api/pdf/snapshot/` save/list/load/delete（含重复页、空 hash、超大列表） | ✅ |
| LLM 状态/缓存 | `/api/llm/status`、`/api/translation/cache_stats`、`/api/paper/translation_cache_batch_lookup` | ✅ |
| 网络实测 | `/api/paper/fetch`（arXiv）、`/api/paper/bibtex`、`/api/paper/fetch_full`、`/api/paper/download_pdf` | ✅ 真实联网 |
| LLM 实测 | `/api/paper/summarize`、`/api/ask`、`/api/paper/classify_qa_scope`、`/api/paper/translate_section`、`/api/paper/translate_page`、3 个 SSE 端点 | ✅ 真实调用上游模型 |

**实机验证**（内置浏览器，http://127.0.0.1:5000）：仪表盘正常渲染（23 文献 / 3 复现 / 76 避坑 / 5 周报）、**控制台零错误**、网络零失败请求、"加载更多"实测 5→10 条、剩余计数 13→8 正确递减。

---

## 5. 测试体系重建

| 文件 | 作用 |
|---|---|
| `tests/conftest.py` | `sandbox` fixture 把 `generator.BASE_DIR` 与 `app.BASE_DIR` 双双重定向到临时目录 → **写路径测试绝不碰真实 vault** |
| `tests/test_endpoints.py` | 53 项逐端点可运行性测试 |
| `tests/test_security.py` | 77 项安全断言 |
| `microbench/_test_regressions.py` | 39 项回归守卫（扩充至 6 组） |
| `microbench/test_bench.py` | 存量套件：修复 3 组指向已删除端点的陈旧测试（自 2026-09-24 起长期红灯）；清理逻辑从**文件名子串匹配**改为**内容标记**；运行结束后也清理，做到 vault 零污染 |

> **存量套件的删除风险**：`test_bench.py` 原先按文件名子串（`MoS2` / `QA_` / `SmokeTest` / `BilingualTest`）删除 `01_Literature/**/*.md`。用户研究方向正是 2D 材料（MoS2 / HZO / 2D-FET），**一旦保存标题含 "MoS2" 的真实笔记，跑一次测试就会永久删除**。现改为内容标记 `MBTESTARTIFACT` 判定，并实证：跑完前后 vault 哈希对比，真实笔记 `2024_新型_{MoS2}HZO_异质结_2D-FET_—_物理建模.md` **未被删除**。

---

## 6. 交付物

```
microbench/app.py            + CSRF 守卫 + 可读 422 + 类型化快照 + 阻塞端点改线程池
microbench/llm.py            + 限流语义修正 + 降级不拼接 + 空内容重试 + 重试预算 + 凭据脱敏
microbench/generator.py      + 原子写 + 目录自建 + YAML 注入防护 + 并发写锁 + Jinja 单例
microbench/paper_fetcher.py  + SSRF 守卫下移 + 重定向逐跳校验
microbench/paper_text.py     + 流式限量下载 + 临时文件 finally 清理 + O(n) 边界计算
microbench/plot_compare.py   + 向量化数字化 + 像素上限
microbench/static/index.html + 18 项前端缺陷修复（含 2 个 XSS）+ 4 个额外发现
microbench/test_bench.py     + 陈旧测试转绿 + 内容标记清理 + 零污染
microbench/_test_regressions.py  扩充至 39 项
tests/                       新建：53 项端点测试 + 77 项安全测试
requirements.txt             + httpx / send2trash / pywebview / numpy / pytest 全部声明
```

### 已知残留（不影响可用）

1. `/api/plot/*` 三个端点后端完备但**前端无入口**——功能存在却无法从 UI 触达。
2. 10 个后端路由前端未调用（`/api/ask`、`/api/paper/summarize`、`/api/paper/bibtex` 等），属既有设计，非本次引入。
3. 快照的 `text_hash` 在非安全上下文（LAN IP 访问）用非加密 djb2 兜底，与 SHA-256 存档不可互认（服务端缓存仍吸收 LLM 成本）。
4. 限流锁为进程内锁；第二个服务实例仍可能交错（单用户桌面场景不触发）。
5. 审计过程消耗了少量 LLM 配额（探针/翻译/摘要实测）。
