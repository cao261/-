# 交接：「像看小说一样」的阅读进度续读（2026-11-20）

> 这一轮的主线是用户提的一个功能诉求：
>
> **「我这论文都很长嘛，我不想每次打开都要重新翻好久翻到我目前看的部分，
> 需要你能做到像看小说一样，这次看到这里了，关掉应用，下次打开也还是这里」**
>
> 坑记在 `AGENTS.md §4.124~4.127`。
>
> **范围（用户 2026-11-20 拍板）**：
> - ✅ **只做电脑端**，平板端（`microbench/static/tablet/app.js`，另一套独立实现）**本轮不动**。
> - ✅ **只要本机自动续读**：进度存 **localStorage**，**不碰坚果云共享目录**
>   （不写 `progress/` 同步通道 → 另一台设备 25353 完全不受影响）。

---

## 0. 一句话结论

项目里**早就有**一套进度机制，但整条是残的 —— 这次不是新建，而是**把它修好并接上「自动」**：

| # | 残在哪 | 改后 |
|---|---|---|
| ① | 采集只认**右栏译文栏** → 用户看左栏 PDF 原文时 `page` 恒为 **0** | 两栏都取，**原文栏优先** |
| ② | 恢复只找 `pdf-trans-block-N`（译文块）→ 原文模式**跳不过去** | 先找原文页块、再找译文块；找不到重试 |
| ③ | 只在**手动点按钮**时才走 → 「关掉应用」什么都不会发生 | 滚动防抖自动采集 + 关窗/关应用前再存一次 |
| ④ | `_persistReaderState()` 写死 `params.set("page", "1")`（注释自认「never wired up」） | 写真实当前页 |
| ⑤ | **页码之外什么都不记** → 只能回到页首，不是上次那一行 | 记「页码 + **页内位置**（带符号 delta + 比例）」，误差 ≤ 3px |

**没动的**：后端一行没改（`app.py` 没碰 → **不用重启服务**）。

---

## 1. 取证：为什么进度必须存 localStorage

三条路逐个排除：

| 方案 | 结论 | 依据 |
|---|---|---|
| URL / hash | ❌ **活不过应用重启** | `desktop.py` 每次启动的 URL 是 `http://127.0.0.1:5000/?v=<随机 hex>` → **hash 不会被保留** |
| 后端 `progress` 通道 | ❌ **会写坚果云共享目录** | `/api/sync` push 会写 `_sync_dir("progress")/<safe>.json` → 影响另一台设备 25353（用户已拍板不碰） |
| **localStorage** | ✅ | `desktop.py` 显式 `private_mode=False` + `storage_path=PROFILE_DIR` → **跨次启动保留**；浏览器 `--app` 兜底模式同理 |

→ 键：`microbench_readpos_v1` → `{ "path:<vault 相对路径>": 记录 }`，最多 200 篇。

---

## 2. 改了什么（全部在 `microbench/static/index.html`，纯前端）

### 2.1 新增「阅读进度续读」模块

放在「状态持久化」小节之前（`_persistReaderState` 会用到它，且 `const` 不能在 TDZ 里被用）。

| 函数 | 作用 |
|---|---|
| `_readPosStore()` / `_writeReadPosStore()` | 读写 localStorage；超 200 篇按 `updated_at` 砍最旧 |
| `_readPosKeyForPath()` / `_readPosKey()` | 主键 = `path:<vault 相对路径>`，退化时用 `pid:<paperId>` |
| `_readPosAnchor(pane)` | 取一栏的「当前页 + **带符号 delta** + 比例 + scrollTop」 |
| `_captureReadPos()` | 采集两栏 → 落盘 |
| `_scheduleSaveReadPos()` | 滚动防抖 **400ms** |
| `_restoreReadPosAnchor()` | 滚回「某页 + 页内位置」，**返回有没有精确落位** |
| `_applyReadPos()` | 两栏一起放回 |
| `_readPosReapply()` | 译文回填 / 快照恢复后重新贴回（用户自己滚过的那栏不动） |
| `_initReadPosTracking()` | 两栏挂 `scroll` 监听 + `beforeunload` / `pagehide` 兜底 |
| `_restoreSectionReadPos()` | 章节视图：回到上次打开的那一节 |

两栏的 DOM 不同但算法一样 → 抽成一张表 `_READPOS_PANES`：

```js
pdf:   { scrollerId: "pdf-viewer-container",     listId: "pdf-pages-list",
         blockSel: ".pdf-page-block",             idPrefix: "pdf-page-block-" }
trans: { scrollerId: "pdf-translations-container", listId: "pdf-translations-list",
         blockSel: ".pdf-translation-block",      idPrefix: "pdf-trans-block-" }
```

### 2.2 页内位置的数学（**这是本轮最容易做错的地方**）

`.pdf-split-pane` 有 `padding: var(--s-5) var(--s-6) var(--s-12)` →
停在第 1 页页首时，页顶相对容器顶是 **+24**（页顶在容器顶**下面**），滚进页里才是负数。

- ❌ 只记「滚进了多少 px」（`max(0, -delta)`）→ **padding 那一段被吃掉** → 恢复时整页差一个 padding 高（实测 **24px**）。
- ✅ 记**带符号**的 `delta = 页顶 - 容器顶`：
  `目标 scrollTop = 当前 scrollTop + (页顶 - 容器顶) - delta`
- ✅ 再记**比例** `ratio = |delta| / 页高`：**缩放变了以后 px 偏移指的不是同一个位置**，
  比例才是「这一页读到了百分之几」→ 缩放变了按比例换算。

### 2.3 采集时机

- 两栏 `scroll` → 防抖 400ms 落盘；
- `closeReaderModal()`（点 × 关阅读器）；
- `beforeunload` / `pagehide`（直接杀应用）。

### 2.4 恢复时机（`loadPdfDualColumn`）

- **渲染循环里尽早回位**：目标页一渲染出来就试一次
  （`scrollTop` 不能超过 `scrollHeight - clientHeight`，内容不够时会被夹住 → 返回 false，等下一页再试）；
- **全部渲染完再精确回位一次** → 弹「📖 已回到上次读到的位置（第 N 页）」；
- 缩放重绘（`keepTranslations=true`）时**不查存储**，直接用重绘前从活 DOM 抓的位置。

### 2.5 顺手修好跨设备那条路（**不自动写坚果云**）

- `_readingProgress()`：改成两栏都取、**原文栏优先**（原来只看译文栏 → 原文模式下推出去 `page=1`，译文没翻时是 `page=0`）；
- `_applyReadingProgress()`：改成**先找原文页块、再找译文块**，找不到重试 10 次（页块可能还没渲染出来）。

> ⚠ 后端 `progress` 通道只保留 `_PROGRESS_CORE_FIELDS`
> （`paper_id, page, mode, zoom, scroll_top, updated_at`）→ **页内位置传不过去**，
> 跨设备只能回到页首。这是后端白名单决定的，本轮**没动后端**。

---

## 3. 护栏

### 3.1 `microbench/_test_readpos_resume.py`（**真实渲染**，行为级）

- 把 index.html 里的**真 JS**（15 个函数 + 9 条状态声明）抽出来，
  喂进一个探针页面（两栏真滚动容器 + 真页块 + 真 padding），
  用 headless Edge **真渲染、真滚动**后量坐标。
- **16 条断言**：采集页码 / 采集页内位置 / 采集比例 / 两栏分别记 / 落盘 /
  精确落位 / **页内位置 ±2px** / 右栏精确回位 / 缩放变了按比例换算 /
  `_readingProgress` 认原文栏 / `_applyReadingProgress` 能跳原文页 / 存储上限 200 篇 …
- **变异自检 9/9 全被抓住**（含「采集丢页内位置」「取错页」「恢复少减 delta」
  「永远定位第 1 页」「只回原文栏」「缩放不换算」「不落盘」「跨设备只认译文栏」「跳页只找译文块」）。
- 已挂进 `tests/test_pdf_layout_guardrails.py` 的 `PY_SUITES`，
  并登记进 `tests/test_guard_efficacy.py` 的 `required` 清单。

> ⚠ **没有新增「读源码文本当护栏」**（元护栏上限仍是 131）。

### 3.2 真机端到端（浏览器）：`microbench/_live_readpos_test.py`（桌面真应用）

真应用 `http://127.0.0.1:5000/` + 真论文（JACERS 综述 8 页）+ 真 PDF.js：

```
✓ 进度真的落进了 localStorage   [path:01_Literature/04_Survey_综述/...]
✓ 记的是**左栏原文**的第 5 页   [记到 page=5 delta=-100]
✓ 页内位置也记下来了（100px）   [offset=100 ratio=0.101]
→ 刷新页面（模拟关掉应用再打开）
✓ 刷新后左栏确实滚起来了         [scrollTop=4203]
✓ 刷新后回到了**同一行**（页内误差 ≤ 3px）  [期望 100 实际 102]
✓ 用户还能正常滚动（续读没把滚动条锁住）
✓ 页面没有 JS 报错
失败 0 项
```

（2.25px 的残差 = 网页字体加载带来的亚像素级位移，视觉上无感。）

⚠ 实测脚本**拦截了 `/api/paper/translate_page`** —— 不烧用户 LLM 额度；
也**没碰 `/api/sync*`** —— 不写坚果云共享目录。

### 3.3 **桌面端真应用**验收（`microbench/_live_readpos_test.py`）

浏览器页面刷新 ≠ 关掉应用。这一轮补了**真应用**的验收
（`desktop.py` → pywebview → Edge WebView2）：

| 与浏览器的差别 | 真应用 |
|---|---|
| localStorage 位置 | `logs/webview_profile`（WebView2 用户目录） |
| 启动 URL | `?v=<随机 hex>`，**没有 `open_pdf`** → 必须从「仓库论文库」下拉框里点开 |
| 服务生命周期 | 应用自己起、自己停（跟你双击快捷方式一致） |

驱动方式：`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222`
→ Playwright `connect_over_cdp` 接进**真窗口**操作（不另开浏览器）。

实测结果（**0 失败项**）：

```
① 第一次启动真应用（--open 直开这篇）→ 滚到第 5 页页内 100px
   ✓ 进度写进了真应用的 localStorage   [page=5 delta=-100 ratio=0.13]
② 向应用窗口发 WM_CLOSE（模拟点 X 关窗）
   ✓ 应用进程确实退出了                [5.0s 后退出]
③ 第二次启动真应用（**不带 --open**）→ 在「仓库论文库」下拉框里选中这篇 → onchange
   ✓ 下拉框里能选中这篇论文并触发打开   [PICKED]
   ✓ 重开后左栏确实滚起来了              [scrollTop=3307]
   ✓ 重开后回到**同一行**（页内误差 ≤ 3px）[期望 100 实际 100]
   ✓ 重开后进度记录仍在（没被清掉）
```

**像素级佐证**：把「关闭前」的画面整体平移 N 个设备像素后与「重开后」比对 ——
**最佳位移 = 0 设备像素、像素差异率 0.000%** → 两张截图**完全一致**，就是同一屏同一行。

⚠ 脚本**只杀属于本应用 profile 的 WebView2 进程** ——
这台机器上还有 **Windows 自己的** WebView2 组件（通知中心/小组件），
一刀切 `taskkill /IM msedgewebview2.exe` 会把系统组件一起打死。

---

## 4. 坑（详见 `AGENTS.md §4.124~4.127`）

1. **§4.124** 进度必须存在**活得过应用重启**的地方（hash 不行、共享目录要用户点头）。
2. **§4.125** 页内位置要用**带符号 delta** + **比例**；别提前 `max(0, x)` / `abs(x)`。
3. **§4.126** 「自己滚的」与「用户滚的」要分清（**别用计数 +1/-1**，值没变时不触发事件）；
   译文异步回填会改布局 → 位置会漂 → 回填后要重新贴回。
4. **§4.127** 同步一个字段前，先确认它在**产生它的那一侧**真的有值（左栏/右栏独立滚动）。

---

## 5. 下一台设备（25353）要做什么

1. `git pull` 拿到本轮（**前端改动**，不用重启服务，刷新页面即生效）；
2. **本机进度不会自动同步过去**（本轮只做本机）——
   要跨设备就手动点菜单里的「推送 · 阅读进度」（这条路的采集已修好，但**只能回到页首**，
   因为后端 `progress` 通道只保留白名单字段）；
3. 平板端**本轮没做**续读（用户拍板），要补的话在 `microbench/static/tablet/app.js`
   里另写一套采集 + 恢复（平板的 PDF 阅读器是独立的实现）。

---

## 6. 复现命令

```powershell
# 护栏（真渲染 + 变异自检）
python microbench/_test_readpos_resume.py

# 全量
python -m pytest tests -q -m "not slow"
python -m microbench._test_regressions

# 真机端到端（浏览器 + 桌面真应用，脚本在仓库里）
python microbench/_live_readpos_test.py
```

> ⚠ 这条脚本会**真的起一个应用窗口**并**关掉它**，别在用户正在用的时候跑。
```
