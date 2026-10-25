# 平板工作台 —— 2026-10-25 第二轮：译文同步 + 阅读器重做 + 手写

> 第一轮：[`HANDOFF_TABLET_2026-10-25.md`](HANDOFF_TABLET_2026-10-25.md)（APK 首版）
> 审计轮：[`AUDIT_2026-10-25.md`](AUDIT_2026-10-25.md)（6 阻断 + 11 重要）
>
> 这一轮是**按用户实际使用反馈**做的：
> 「你要把译文给我同步过来，不然的话，你只是能看个 PDF，那我直接在本地打开
> 这不一样吗？我要的是这个平板的作用基本上能达到和在电脑上的作用是一样的。」
>
> **状态：VERDICT = PASS**（自动化全绿；真机边界见 §5）

---

## 1. 用户提的问题 → 对应改法

| 用户说的 | 根因 | 改法 |
|---|---|---|
| **「翻页做不到，就只能看第一眼」** | `.tb-pdf-body` 是 `display:flex` **横排** —— 页往右排，窄屏只看得到一两页；而且 flex 居中溢出的经典 bug 让左半边**永远滚不到** | 改成**竖排连续**，每页一张「页卡」（页卡内左原文、右译文）；翻页 = 滚到该页 |
| **「放大之后也划不动 / 只显示一部分」** | 固定 `scale=1.15`，整页比屏幕宽 | 按容器宽度算**适合宽度**的比例（`pdfFitScale`），缩放改成独立的倍数；**重画后保留滚动锚点**（原来每次缩放都被弹回第一页） |
| **「你要把译文给我同步过来」** | 桌面端的译文快照存在 `logs/_pdf_snapshots/`，在**同步根**（`我的坚果云/MicroBench同步`）里 —— 那是 vault 根的**兄弟目录**，平板原来的 WebDAV 客户端**根本读不到** | ① 原生侧加「同步根」读写通道；② 平板按与电脑端**逐字一致**的 `paper_id` 算出快照文件名去取；③ 导出时把译文**烤进单文件**（没连云盘也有） |
| **「我还能用平板儿直接手写」** | 平板上没有笔迹层 | 原文上叠一层手写 canvas（笔/手指都能画），笔迹用**归一化坐标**存，**跟批注存在同一个文件**里 → 跟电脑端共享同一份 |
| **「能同步标注」** | 批注也在同步根里，平板读不到 | 同一套同步根通道 → 批注能在 PDF 上画成高亮框 |

---

## 2. 同步根：这是这轮的关键

```
我的坚果云/                    ← WebDAV 根
├── MicroBench工作台/          ← vault 根：笔记 + 原文 PDF（平板原来只能读这个）
└── MicroBench同步/            ← 同步根：译文快照 + 批注（两台设备共享）
    ├── snapshots/<paper_id>.json
    ├── annotations/<paper_id>.json
    └── progress/<paper_id>.json
```

**同步根不在 vault 里** —— 这就是平板「只能看 PDF、看不到译文」的根因。

### 2.1 原生侧

| 文件 | 改动 |
|---|---|
| `Prefs.java` | 新增 `davSyncRoot` / `setDavSyncRoot` / `syncClient()` |
| `DavClient.java` | 新增 `detectSyncRoot(vaultRoot)` —— 同步根是 vault 根的**兄弟目录**，按名字 `MicroBench同步` 找，并验证里面有 `snapshots/` 或 `annotations/` |
| `DavPathHandler.java` | 新增路由 `/dav/sync/list/*`、`/dav/sync/file/*` |
| `DavBridge.java` | 新增 `putSync(path, text, cbId)` —— 写同步根（手写标注用） |
| `SettingsActivity.java` | 「测试并保存」时**顺带探测同步根**并一起存；状态条会显示「同步目录在「…」（N 篇译文快照）」 |

### 2.2 页面侧

| 函数 | 作用 |
|---|---|
| `syncUrl(kind, path)` | 同步根的 URL（不经过 vaultRoot） |
| `paperIdFor(relPath)` | **复刻**桌面端的 `paper_id`：文件名(去.pdf, 截70) + `_` + djb2(完整相对路径) |
| `safePaperKey(pid)` | 同步目录里的文件名 = `paper_id` 去掉非「字母/数字/_/-」的字符（**中文要保留** —— Python 的 `isalnum()` 对中文是 True） |
| `loadTranslation(pdfId)` | 取译文：① 本地缓存 → ② 云盘同步根 → ③ 导出版里烤进来的 |
| `loadAnnotations(pdfId)` | 取批注 |
| `saveInk()` | 手写笔迹写回同步根（先读现有批注 → 合并 → 写回，不能整份覆盖） |

**paper_id 的三方对齐已实测**（3 篇真实快照全部对上）：

```
tablet=2008-JACERS-综述-MagnetoelectriclaminatecompositesAnoverview_pgghcp
pc    =2008-JACERS-综述-MagnetoelectriclaminatecompositesAnoverview_pgghcp  ✓
tablet=2025_AgenticTCAD_A_LLM-based_Multi-_1dawd47                        ✓
tablet=2026_Sensors__Actuators_A_Physical_382_2025_116174_1j6nqf7          ✓
```

---

## 3. 阅读器重做

### 3.1 布局

```
.tb-pdf-body          竖排连续滚动（原来：flex 横排）
└── .tb-pdf-page      一页一张卡
    ├── .tb-pdf-orig  原文（canvas + 文本层 + 批注高亮层 + 手写层）
    └── .tb-pdf-trans 译文（Markdown 渲染，公式走 KaTeX）
```

- **窄屏（竖屏）**：译文在原文**下面**
- **宽屏（≥900px，横屏）**：自动变成**左右双栏**，跟电脑端一样
- 原文按容器宽度**适合宽度**缩放，整页不再超出屏幕

### 3.2 顶栏新增

| 按钮 | 作用 |
|---|---|
| **译** | 显示/隐藏译文栏 |
| **手写** | 开关手写（在原文上直接画） |
| **存** | 把笔迹存到坚果云（跟电脑端共享） |
| − / ＋ | 缩放（重画后**保留滚动位置**） |

### 3.3 手写

- 笔迹用**归一化坐标**（0~1）存 → 换设备、换缩放比例都不会错位
- 存成批注条目（`source: "ink"`，带 `strokes`）→ **跟电脑端共享同一份文件**
- 后端 `AnnotationItem` 加了 `strokes` 字段（合并逻辑天然保留它）

---

## 4. 导出器：把译文烤进单文件

```python
microbench/export_offline.py :: collect_translations()
```

把 `logs/_pdf_snapshots/*.json` 里的译文按**同步目录里的文件名**索引，烤进
`__DATA__.trans`：

```json
{ "2008-JACERS-综述-..._pgghcp": { "1": "第 1 页的译文…", … } }
```

**这样平板即使还没连坚果云，打开论文也能看到译文** —— 不再是「只能看个 PDF」。

实测：3 篇论文 / 24 页译文烤进单文件，HTML 2.47 → 2.58 MB。

---

## 5. 验收

### 5.1 自动化（11 个套件全绿）

| 套件 | 结果 |
|---|---|
| `pytest tests -m "not slow"` | 205 passed |
| `microbench/_test_regressions` | 40 passed |
| `microbench/_test_lan` | 39 passed |
| `microbench/_test_tablet_sync.py` | 32 passed |
| `microbench/_test_tablet_renderer.js` | 38 passed |
| `microbench/_test_tablet_ai.js` | 24 passed |
| `microbench/_test_tablet_hardening.js` | **58 passed**（+13） |
| `microbench/_test_tablet_export.py` | 41 passed |
| `microbench/_test_offline_export.py` | 17 passed |
| `android/_test_dav.py` | 35 passed |
| `android/_test_apk_contract.py` | **79 passed**（+11） |

新增的护栏（改回去就会红）：
- `.tb-pdf-body` 不能再是 flex 横排、不能再有 `justify-content:center`
- 平板算出的 `paper_id` 必须等于电脑端的快照文件名（拿真实快照对拍）
- 译文里的公式必须渲染成 KaTeX 且不残留 `$`（拿真实译文对拍，24 页）
- 同步根的读写路由、探测、写桥必须都在

### 5.2 浏览器实测

- 主界面、文献库（24 篇原文，大小与关联笔记都对）渲染正常
- 译文已确认烤进单文件（`__DATA__.trans` 有 3 篇 / 24 页）

### 5.3 仍需真机实测

1. **同步根探测**：这台机器的坚果云里到底是不是 `我的坚果云/MicroBench同步`
   （`.env` 的 `ANNOTATIONS_DIR` 是这么写的，但要确认 WebDAV 侧同名）
2. **翻译栏的显示效果**：竖屏堆叠 / 横屏双栏的实际观感
3. **手写**：触控笔和手指的手感、笔迹同步回电脑端
4. **大 PDF** 打开耗时与内存
5. 坚果云 WebDAV 的实际响应细节

**平板上的验证顺序**：

1. 覆盖安装 → 打开
2. 点「配置坚果云」→ 填邮箱 + 应用密码 → 「测试并保存」
   → 状态条应出现「同步目录在「我的坚果云/MicroBench同步」（N 篇译文快照）」
   - **如果没出现「同步目录」** ⇒ 探测失败，告诉我，我改。
3. 点「📄 文献库」→ 打开那篇 **2008-JACERS-综述**（有译文的）
4. 应看到：原文 + 译文（宽屏左右双栏，竖屏上下）；顶栏有「译 / 手写 / 存」
5. 翻页试试（‹ ›）→ 缩放试试（− ＋）→ 都能动
6. 点「手写」→ 在原文上画几笔 → 点「存」→ 提示「已存到坚果云」

---

## 6. 相关文件

| 文件 | 作用 |
|---|---|
| `android/java/.../Prefs.java` | 同步根的位置 + 客户端 |
| `android/java/.../DavClient.java` | `detectSyncRoot()` |
| `android/java/.../DavPathHandler.java` | `/dav/sync/*` 路由 |
| `android/java/.../DavBridge.java` | `putSync()` |
| `android/java/.../SettingsActivity.java` | 探测 + 保存同步根 |
| `microbench/static/tablet/app.js` | 阅读器重做、译文、手写 |
| `microbench/static/tablet/styles.css` | 竖排布局、译文栏、批注层、手写层 |
| `microbench/static/tablet/index.html` | 顶栏新增「译 / 手写 / 存」 |
| `microbench/export_offline.py` | `collect_translations()` 烤译文 |
| `microbench/app.py` | `AnnotationItem.strokes` 字段 |
