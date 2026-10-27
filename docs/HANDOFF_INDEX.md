# 交接文档索引（对方设备 / 平板设备都先看这份）

> **你不需要读全部 5 份** —— 按你负责的部分挑。
> 所有文档都在 GitHub 仓库里，`git pull` 就能拿到（`auto_sync.py` 每 2 小时也会自动同步）。
> ⚠️ 交接走仓库，**没有会话通道**（`session send` 只能投本机会话）→ 改完记得推 GitHub。

## 阅读顺序（按你的角色选）

### 谁都要看（本轮新增的三份）
0. **[`HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md`](HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md)** ← **最新，先看这份**
   合并了对方（25353）`main` 的第二轮审计（6 提交），解决 **25 处语义冲突**，
   并复核加固平板端。**两台设备各自审计 → 结论撞了三点**（缓存键的词典维度 /
   限流器基准 / 审计报告同名），这轮用**代码事实**判清楚并让两边都对。
   含：**为什么「加维度防撞键」反而引入更隐蔽的故障**、
   **两条「护栏比代码差」的字符串断言护栏**（对方只是换了个写法就假红）、
   平板端 12 项逐条判（哪些要抄、哪些抄不了、哪些已一致）。
   测试 **740 passed / 1 skipped**；APK 已重打，两份交付物逐字节相同。
1. **[`AUDIT_2026-10-27.md`](AUDIT_2026-10-27.md)** ← **第四轮全量审计（最新）**
   安全 + 性能 + 代码质量一起审：四路并行只读审查 + **215 次真实 HTTP 实测**（含局域网远端视角）。
   本轮抓到 **2 条 CRITICAL**（都直接打断用户主线工作流）：
   ① **批注重放自激循环** → 译文栏划词选区每 80ms 被清空 → **划词答疑拿不到引文**；
   ② **平板 WebView 离源后仍持原生桥** → 笔记里一个外链就能让恶意页面
      删/写用户坚果云里的 vault（并拿平板当 HTTPS 代理）。
   另有 **12 条 HIGH**：`/docs` `/openapi.json` 局域网无口令（71KB 完整 API 目录）、
   **限流器并发失效** → 误判配额耗尽 → 全局降格 15 分钟、
   **按页翻译在事件循环上阻塞 64~332ms/页**、**Pillow 40+ 个已知 CVE** 等。
   ⚠ **修复全在轮新分支 `feat/audit-2026-10-27`**（基于 `feat/zen-focus-mode`）。
   ⚠ **2026-10-28 更新**：该分支已合并对方 `main`（第二轮审计）。
   → 另有两份别混的：`AUDIT_2026-10-27-REMOTE-R2.md`（对方第二轮记录）、
     `AUDIT_2026-10-27-REMOTE.md`（审对方提交的报告）。
1. **[`HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md`](HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md)** ← 专注模式
   桌面端**专注模式**（小屏极简阅读）：屏幕上只留「论文页 + 译文页」，
   顶栏工具条 / 章节导航 / AI 提问区 / 划词悬浮钮默认收起，需要时从顶部滑出。
   ⚠ **在分支 `feat/zen-focus-mode` 上，`main` 没动** —— 不想要就 `git checkout main`。
   浏览器实测抓到 4 个真 bug；独立审查又抓到 **1 高 + 3 中 + 5 低**，
   全部已修（其中高危那条是「护栏对核心修复零覆盖」，见 §4.5）。
   改之前必读 `AGENTS.md` §4.48 / §4.49 / §4.50 / §4.51。
2. **[`HANDOFF_2026-10-27-PERF-OPTIMIZATION.md`](HANDOFF_2026-10-27-PERF-OPTIMIZATION.md)**
   电脑端「打开略微卡顿」的**取证与修复**：打开论文 2496ms → 50ms（PDF 抽取缓存）、
   首屏请求串行 → 全并行（async 端点里的同步磁盘扫）、滚动帧 max 391ms → 23ms（节流）。
   含**三条试过但证伪的路**（线程池并行 0.97x / 换 PyMuPDF 快 20 倍但破坏双栏顺序）。
3. **[`HANDOFF_2026-10-27-TABLET-QA-BUDGET.md`](HANDOFF_2026-10-27-TABLET-QA-BUDGET.md)**
   平板端补齐桌面端 `2216afa` 那轮答疑修复的其余根因（预算 / 重试 / 序号守卫 /
   `reasoning_effort` / 能力边界），并修了独立审查揪出的 6 个缺陷。

### 我负责桌面端（另一台电脑的 agent）
1. **[`HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md`](HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md)** ← **最新，先看这份**
   专注模式（功能尝试，**在分支 `feat/zen-focus-mode`，`main` 没动**）。
   改之前必读 `AGENTS.md` §4.48 / §4.49 / §4.50 / §4.51
   （本轮四个坑：flex fit-content 收缩 / 隐藏→可见时窗口没变 /
   「点了没反应」也是 bug / **变异要改「被调方」**）。
2. **[`HANDOFF_2026-10-25-GLYPH-PERF.md`](HANDOFF_2026-10-25-GLYPH-PERF.md)**
   「打开/上传论文卡 8 分钟」的取证与修复：符号认字从**逐页串行**改成**整篇批量**，
   词典主键从**形状指纹**换成**语义键**（字体基名|字符码），打开路径**不调模型**
   （5.5s 打开）。含剩余待办（裁图带邻居 → 部分符号认不出）。
3. **[`HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md`](HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md)**
   浏览器实测（真人级点击）抓出的 5 个真 bug + 安全审计的 5 个问题 + 降格提示改造。
   含：PDF 模式整条工具条消失、AI 活动「取消」够不着、429 报错说人话、
   密钥脱敏盲区、离线导出 XSS。
4. [`HANDOFF_2026-10-25-AI-ACTIVITY-AND-GLYPHS.md`](HANDOFF_2026-10-25-AI-ACTIVITY-AND-GLYPHS.md)
   AI 活动可视化（徽标/浮层/取消）+ 符号认字（× ≈ ≥）的实现要点。
   ⚠️ 其中「符号认字」那节的**形状指纹方案已被上面第 2 份取代**（误判率 26~41%）。
5. [`LLM_CONTRACT.md`](LLM_CONTRACT.md) —— 改模型层前必读（换模型/降级/速率/表格读图）。
6. [`READER_CONTRACT.md`](READER_CONTRACT.md) —— 改阅读器前必读（页内顺序/公式/批注）。

### 我负责平板端（Android/iPad 那台）
1. **[`HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md`](HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md)** ← **最新，先看这份**
   合并对方 `main` 后**平板端逐条适配**：对方那轮 12 处修复**全在桌面端**
   （平板文件零改动）→ 逐条判哪些要抄（**只有 1 处**：裸 `DOMContentLoaded` 加固）、
   哪些抄不了、哪些**两端已一致**（网关白名单、paper_id、密钥脱敏、XSS 消毒、
   答疑预算/序号守卫）。含 paper_id 的**真 JS 引擎对拍**结果。
2. [`HANDOFF_2026-10-27-TABLET-QA-BUDGET.md`](HANDOFF_2026-10-27-TABLET-QA-BUDGET.md)
   平板端补齐桌面端 `2216afa` 那轮答疑修复的**其余根因**：
   思考预算按「思考+正文」算（16000）、空正文自动抬预算重试、
   请求序号守卫（重答不死锁）、`reasoning_effort=low`、摘要提示词补能力边界。
   ⚠ 平板的 `reasoning_effort` 判定**只看网关地址不看模型名**（故意的差别，文档里说了为什么）。
   ⚠ **2026-10-28**：桌面端已改成**同一套精确主机名白名单**，两端一致，
   且有护栏 `test_desktop_and_tablet_use_the_same_host_whitelist` 持续钉住。
3. [`HANDOFF_2026-10-25-TABLET-DAV-FIX.md`](HANDOFF_2026-10-25-TABLET-DAV-FIX.md)
   平板端「同步 0 篇 / 24 篇 PDF 全 404」的**根因与修法**：
   vault 前缀是空的，而界面只看账号密码，所以显示「已连接」。
   **空结果必须当成错误** —— 这条最值得抄。
4. [`HANDOFF_2026-10-25-TABLET-ADAPT.md`](HANDOFF_2026-10-25-TABLET-ADAPT.md)
   桌面端这轮做的哪些事**平板端要抄、哪些抄不了、哪些不用抄**。
5. [`HANDOFF_TABLET_2026-10-25.md`](HANDOFF_TABLET_2026-10-25.md) +
   [`HANDOFF_TABLET_2026-10-25-B.md`](HANDOFF_TABLET_2026-10-25-B.md) —— 平板端此前的两轮交接。
6. [`AUDIT_2026-10-25.md`](AUDIT_2026-10-25.md) —— 第二轮全面审计报告。
7. **平板端必须自查的两件事**（在 `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` §8）：
   - 离线导出 HTML 的 XSS 面（平板走**另一套** JS 消毒器，桌面端的修复不过去）；
   - 密钥脱敏的盲区（JSON 形态的 key、裸 `Authorization`、URL userinfo…）。
   → **这两条已在 30461 上修完**（见 `HANDOFF_2026-10-25-TABLET-DAV-FIX.md`
     同期的提交与 AGENTS.md §4.29）；**2026-10-28 复核确认仍然在位**。

### 跨设备同步（谁都要看）
- [`SYNC_CONTRACT.md`](SYNC_CONTRACT.md) —— **红线清单，改动即破坏**。
- **[`AUDIT_2026-10-26.md`](AUDIT_2026-10-26.md)** —— ⚠ **这台设备上最重要的两条**：
  1. **H-1：pytest 会写进共享的坚果云同步目录**（已实测复现）。
     `tests/conftest.py` 的沙箱只挡了 `BASE_DIR`，漏了**同步根**，
     而 `auto_sync.py` 每 2 小时自动跑一次 pytest → **这台设备也会往共享目录写测试产物**。
     修复已在本 commit 里（`tests/conftest.py` 的 `_pin_sync_root` autouse fixture），
     `git pull` 之后自动生效 —— **不要自己再改一遍**。
  2. **M-1：`/api/vault/file` 的作用域从「整个仓库」收窄到「vault 内容目录」**
     （`generator.VAULT_CONTENT_DIRS`）。这是**行为变更**：
     原来能读 `logs/` `docs/` 根目录 `.md`，现在 400。
     桌面端前端只用 `01_Literature/...`，不受影响；
     **但如果这台设备上有自写脚本在读 vault 根目录的 .md，需要自己改路径。**

---

## 文档状态表

| 文档 | 日期 | 解决什么问题 | 状态 |
|---|---|---|---|
| `HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md` | 10-28 | **同步 + 平板适配**：合并对方 main 的第二轮审计（25 处冲突）、三点结论撞车的代码事实判据、平板端逐条适配清单 | ✅ 最新 |
| `AUDIT_2026-10-27.md` | 10-27 | **第四轮全量审计**（安全+性能+代码质量）：2 CRITICAL / 12 HIGH，含批注重放自激、局域网 docs 暴露、限流器并发失效、Pillow CVE | ✅ 最新（**分支 `feat/audit-2026-10-27`，已合并对方 main**） |
| `AUDIT_2026-10-27-REMOTE-R2.md` | 10-27 | **对方设备（25353）第二轮审计记录**：已改 / 故意没改 / 未审到 | ✅ 最新（远端轮） |
| `AUDIT_2026-10-27-REMOTE.md` | 10-27 | 审「对方设备（30461）提交」的报告 —— 与上面两份**不是一回事** | ✅ 有效 |
| `HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md` | 10-27 | **桌面端专注模式**：屏幕只留「论文页 + 译文页」，其余收起；浏览器实测抓到 4 个真 bug | ✅ 最新（**在分支 `feat/zen-focus-mode`，main 未动**） |
| `HANDOFF_2026-10-27-PERF-OPTIMIZATION.md` | 10-27 | **电脑端性能**：打开论文 2.5~9.7s → 50ms、首屏并行化、滚动节流 | ✅ 最新（性能） |
| `HANDOFF_2026-10-27-TABLET-QA-BUDGET.md` | 10-27 | **平板端**：补齐答疑「只思考不回答」的预算 / 重试 / 序号守卫 / 思考强度 | ✅ 最新（平板端） |
| `AUDIT_2026-10-26.md` | 10-26 | 第三轮审计（后端/模型层/前端）+ **P0 五处已修** | ✅ 有效 |
| `HANDOFF_2026-10-25-GLYPH-PERF.md` | 10-25 深夜 | **桌面端**：打开/上传论文卡 8 分钟（逐页串行认字 → 整篇批量 + 语义键词典） | ✅ 最新（桌面端） |
| `HANDOFF_2026-10-25-TABLET-DAV-FIX.md` | 10-25 深夜 | **平板端**：同步 0 篇 / PDF 全 404 的根因与修法 | ✅ 最新（平板端） |
| `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` | 10-25 晚 | 浏览器实测抓出的 5 个 bug + 安全 5 项 + 降格分类 | ✅ 有效 |
| `HANDOFF_2026-10-25-AI-ACTIVITY-AND-GLYPHS.md` | 10-25 | AI 活动可视化、符号认字 | ✅ 有效 |
| `HANDOFF_2026-10-25-TABLET-ADAPT.md` | 10-25 | 桌面端 → 平板端的移植清单 | ✅ 有效 |
| `HANDOFF_TABLET_2026-10-25.md` | 10-25 | 平板端第一轮交接 | ⚠️ 部分已被上面那份覆盖 |
| `HANDOFF_TABLET_2026-10-25-B.md` | 10-25 | 平板端第二轮交接 | ✅ 有效 |
| `HANDOFF_2026-10-23.md` | 10-23 | 最早的跨设备交接 | ⚠️ **已过时**，只作历史参考 |
| `AUDIT_2026-10-25.md` | 10-25 | 第二轮全面审计 | ✅ 参考 |
| `LLM_CONTRACT.md` / `READER_CONTRACT.md` / `SYNC_CONTRACT.md` | — | 技术契约（长期有效） | ✅ 必读 |

---

## ⚠️ 两条硬规矩（每台设备都适用）

1. **交接只在仓库里走** —— 改完推 GitHub；对方 `git pull` 或等 `auto_sync.py`
   （每 2 小时一次，AGENTS.md §4.19 有用法）。两台设备之间**没有会话通道**。
2. **不要各自改一遍同一处** —— 桌面端 `microbench/static/index.html` 和
   平板端 `microbench/static/tablet/app.js` 是**两份独立前端**，
   桌面的修复**一行都不过去**；桌面端这边改了阅读器行为，平板端要自己同步改、自己测。

## 想快速知道"我这轮要干什么"

- **两台设备都要看** → **`HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md`**（最新）
  —— 合并对方 `main` 后的三点结论撞车怎么判的、平板端逐条适配清单、
  以及**「加维度防撞键」反而引入更隐蔽故障**这个坑。
- **桌面端 agent** → 再读 `AUDIT_2026-10-26.md` 的 §0（修复状态）+ §10（结论与下一步），
  再读 `HANDOFF_2026-10-25-GLYPH-PERF.md`（含剩余待办），
  以及 `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` 的
  §0（三个最该知道的）、§2（PDF 模式工具条消失）、§3（AI 活动可见性）、
  §4（429 人话化）、§8（安全自查）、§9（降格要说真话）。
- **平板端 agent** → 读 `HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md` 的 §4
  （逐条判 + APK 交付）+ `HANDOFF_2026-10-25-TABLET-ADAPT.md` 全文 +
  `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` 的 §8（安全自查清单）。
- **两台设备都要看** → `AUDIT_2026-10-26.md` 的 §3.1（H-1 测试碰真数据）
  —— **那台设备上 `auto_sync.py` 同样在每 2 小时往共享目录写测试产物**，
  `git pull` 本次提交即自动修好，**不要各自改一遍**。

### ⚠ 2026-10-28 合并后的三条变更（对方设备注意）

1. **`pdf_cache` 的缓存键变了**（去掉词典维度、加入 `EXTRACT_IMPL_REV`）
   → 本机磁盘缓存会**整体重建一次**（27 篇 ≈ 72s，一次性）。
   **译文快照不受影响**（`EXTRACTOR_VERSION` 仍是 5，没 +1）。
2. **`/api/sync` 从 `async def` 改成同步 `def`**（避免独占事件循环）
   → 加东西时注意它现在是**同步函数**，里面不能写 `await`。
3. **`cache_key(pdf, dict)` 的调用形式已作废** → 统一成 `cache_key(pdf)`。
