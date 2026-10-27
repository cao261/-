# MicroBench 项目记忆（AGENTS.md）

> **这份文件给任何接手这个项目的 agent 看。** 它不描述功能（功能看 README.md），
> 它记录的是**这个项目怎么被做对、以及踩过哪些坑**。
>
> 另有两份技术契约，改代码前必读：
> - [`docs/READER_CONTRACT.md`](docs/READER_CONTRACT.md) —— 阅读器：页内顺序、公式、批注
> - [`docs/SYNC_CONTRACT.md`](docs/SYNC_CONTRACT.md) —— 跨设备同步：红线清单，改动即破坏
> - [`docs/LLM_CONTRACT.md`](docs/LLM_CONTRACT.md) —— 模型层：换模型/降级/速率/表格读图
>
> **另一台设备上的 agent 先看这个**：
> [`docs/HANDOFF_INDEX.md`](docs/HANDOFF_INDEX.md) —— **交接索引**（按你负责的设备选文档）。
> 两台设备（25353 / 30461）之间**没有会话通道**（`session send` 只能投本机会话），
> 交接一律走仓库：对方 `git pull` 就能拿到（`auto_sync.py` 每 2 小时也会自动同步，§4.19）。
>
> ⚠️ **别只读 `docs/` 目录**：交接文档有 6 份，散在目录里很容易漏读。
> 索引里按「桌面端 / 平板端 / 同步契约」分好了组，也标了哪份已过时。
> **当前最新且最重要**的是
> [`docs/HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md`](docs/HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md)
> （桌面端**专注模式**：屏幕只留「论文页 + 译文页」——
> ⚠ **在分支 `feat/zen-focus-mode` 上，`main` 没动**，不想要就 `git checkout main`）。
> 性能那轮是
> [`docs/HANDOFF_2026-10-27-PERF-OPTIMIZATION.md`](docs/HANDOFF_2026-10-27-PERF-OPTIMIZATION.md)
> （打开论文 2496ms → 50ms，坑记在 §4.42）。
> ⚠️ 交接文档是**某个时点的快照**，不是长期状态 —— 以 §4.4 和各机实测为准。

---

## 0. 一分钟定位

| 项 | 值 |
|---|---|
| 是什么 | Obsidian vault + FastAPI 本地服务（科研文献精读工作台） |
| 仓库根 | `E:\zaiwu\个人工作台`（**它同时是 vault 根**） |
| 服务 | `python -m uvicorn microbench.app:app --host 127.0.0.1 --port 5000` |
| 启动 | **桌面应用**：双击 `MicroBench Desktop.bat`（= 桌面快捷方式「MicroBench 工作台」）→ 独立原生窗口，不走浏览器<br>**网页版**：双击 `start_workbench.bat`（= 快捷方式「MicroBench 网页版」）→ 服务 + 默认浏览器 |
| 前端 | **单文件** `microbench/static/index.html`（CSS+JS 全在里面，~7900 行） |
| 后端 | `microbench/app.py`（API 全部在一个文件里，~4000 行） |
| 抽取层 | `microbench/paper_text.py`（双栏顺序、公式保护） |
| 模型层 | `microbench/llm.py`（MiniMax-M3 主 + Agnes 3.0 Flash 备） |
| 同步层 | `microbench/app.py` 的 `/api/sync` + 坚果云 `我的坚果云\MicroBench同步` |
| 测试 | `pytest tests -m "not slow"` —— **全绿才算做完**（当前 396 passed / 6 skipped）<br>⚠ **必须带 `-m "not slow"`**：不带会连上 arxiv 下载整篇 PDF，卡死在网络测试上 |
| 两台设备 | 本机 `25353`（主开发）+ 对方 `30461`（同账号，另一台机器） |

**代码走 GitHub，数据走坚果云** —— 两条通道互不重叠，见 §4.4。

---

## 1. 用户的硬性要求（改动前先读）

这些是用户反复强调过的，**不是我的推测**：

1. **改完必须能跑。** 服务起得来、`http://127.0.0.1:5000` HTTP 200、
   `pytest tests -m "not slow"` 全绿。
   后端改动要**重启服务**才生效（前端是 `no-store`，刷新即生效）。
2. **要深度，不要浅层。** 用户原话：「我不希望你浅薄地想，我希望你给切实可行、
   落地确实能赚到钱的东西。」→ 交付要**可执行的成品 + 明确结论**，不是方向性建议。
3. **先定位根因，不要猜。** 用户说「看板怎么都是零」时，正确反应是**查代码 + 加探针 + 实测**，
   不是先猜一个原因就去改。§6 是症状→根因的速查表。
4. **路径/文件名/目录结构一个字都不能动。** `01_Literature` 下的任何文件名变化都会让
   `paper_id` 哈希变 → 另一台设备的批注/快照/进度**立刻对不上**。这是红线。
5. **中文回复、中文界面、中文注释。**
6. **不要来回手动对齐。** 用户多次表达「能不能两边只改一遍」→ 能自动化的都要自动化
   （一键对齐脚本、同步通道、云端项目）。
7. **改之前先想影响面。** 任何改动都会同时影响另一台设备，必须两台同时改或做数据迁移。
8. **「APK 只是打包形式」**（2026-10-25 用户原话）—— 平板版的功能要
   **跟电脑端对等**，不是「手机上能跑的简化版」。
   具体到平板工作台：笔记/批注/译文**本地优先**（断网可读可写），
   云盘只是同步目标；翻译、标注、手写、问答一个都不能少。
   少做哪个都要先问用户，别自作主张砍。

---

## 2. 开发环境与操作规范（Windows + PowerShell）

### 2.1 用 PowerShell 7，别用 5.1

```powershell
& 'C:\Users\25353\AppData\Local\Programs\PowerShell\7\pwsh.exe' -NoProfile -File xxx.ps1
```

**为什么必须换**：agent 的 bash 工具绑定的是 Windows PowerShell 5.1，它按系统 ANSI(GBK)
读**无 BOM 的 UTF-8 脚本** → 脚本里的中文注释/字符串被截断成乱码 → 报
「字符串缺少终止符」「意外的标记」这类 ParserError。**看着像命令写错了，其实是编码问题。**
PS 7 默认 UTF-8 无 BOM，同样脚本 exit 0、输出正常。

### 2.2 Python 脚本一律「写文件再跑」

```powershell
# ✗ 不要这样：PowerShell 会把脚本里的单引号/引号吃掉
python -c "import sqlite3; c.execute('SELECT ...')"

# ✓ 这样：write 工具写 .py，再 python 跑
python C:\Users\25353\.minimax\tmp\script.py
```

**本项目至少踩了 4 次**同样的坑（PowerShell 引号转义把 Python 代码截断）。

### 2.3 bash 工具里 `cd` 之后不能写/删

安全策略会拦：`write/delete targets cannot be verified safely after a command working
directory change`。→ 用 `git -C <绝对路径>` / 全绝对路径，**不要先 cd**。

### 2.4 删除文件必须走回收站

```powershell
& 'C:\Users\25353\.minimax\bin\mavis-trash.cmd' -- "<path1>" "<path2>"
```

⚠️ PowerShell 里 `rm` 是 `Remove-Item` 的**别名**，不是 mavis-trash，会被安全层拦。

### 2.5 服务要 detached 启动

```powershell
Start-Process -FilePath 'E:\ruanjian\python.exe' `
  -ArgumentList '-m','uvicorn','microbench.app:app','--host','127.0.0.1','--port','5000','--log-level','warning' `
  -WorkingDirectory 'E:\zaiwu\个人工作台' -WindowStyle Hidden
```

用 bash 工具的后台任务方式启动服务会被 **1 小时上限**掐掉。

### 2.6 行尾与编码

- `.gitattributes`：**`* text=auto eol=lf`** + `*.bat text eol=crlf`
- `.py` / `.js` / `.html` 全 LF
- **`.bat` 必须 CRLF** —— cmd.exe 按 LF 解析会错位（症状：`'rt' is not recognized`）
- `.env` / 文档 UTF-8 无 BOM

### 2.7 GitHub 推送

代理 `127.0.0.1:65532` 常不通 → **先清代理再推**，并留重试：

```powershell
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''
git -c http.proxy= -c https.proxy= push
```

对方那台设备也会 push → 推送被拒时先 `git pull --rebase origin main`（通常无冲突，
两边改的文件不同）。重试脚本见 `C:\Users\25353\.minimax\tmp\push_retry.py`。

### 2.8 提交前自查

```powershell
git status --short                    # 工作区必须干净（除了有意提交的文件）
git ls-files --eol <改过的文件>        # 确认 i/lf w/lf
python -m pytest tests -q -m "not slow"   # 全绿（⚠ 漏了 -m 会卡在网络测试上）
```

---

## 3. 架构速览：改哪里

| 想改什么 | 动哪儿 |
|---|---|
| 页面抽文字 / 双栏顺序 / 公式保护 | `microbench/paper_text.py` |
| 翻译提示词 / 清洗 / 漏译校验 | `microbench/translation.py` |
| 模型选择 / 降级 / 思考模式 | `microbench/llm.py` |
| API / 同步 / 批注 / 快照 | `microbench/app.py` |
| 阅读器 UI / 首页看板 | `microbench/static/index.html`（**单文件**，注意别改崩） |
| 路径安全 / 卡片模板 / vault 统计 | `microbench/generator.py` |
| 抓论文（arXiv/DOI/Crossref） | `microbench/paper_fetcher.py` + `microbench/netutil.py` |
| 离线/平板工作台导出 | `microbench/export_offline.py` |
| 桌面独立窗口（应用形态） | `desktop.py` + `MicroBench Desktop.bat` + `install_shortcuts.ps1` |
| vault 搬进坚果云 | `microbench/move_vault_to_cloud.py` |
| 平板变更包合并 | `microbench/merge_changes.py` |

---

## 4. 踩过的坑 · 按域分类

### 4.1 前端初始化：整个初始化可能一次都没跑过

**症状**：首页看板四个数字全是 0；文献流面板永远停在「正在同步…���；模型状态不显示。

**根因**：`loadStats()` 挂在 `window.addEventListener("DOMContentLoaded", ...)` 上。
但用户平时是带着 `?open_pdf=` / `#paper=` **直达打开**某篇论文的 ——
那时 `DOMContentLoaded` **早就触发过了**（`document.readyState` 已是 `"interactive"`），
监听器再注册就是死的，永远等不到 → **整个初始化（看板统计 + 文献 feed + 模型状态）一次都没跑**。

```js
// ✗ 会死
window.addEventListener("DOMContentLoaded", boot);

// ✓
if (document.readyState === "loading") {
  window.addEventListener("DOMContentLoaded", boot);
} else {
  boot();                      // 事件早已过去，监听器是死的
}
```

**附带教训**：原来 `loadStats()` 里 `await loadRecentFeed()` 排在取数**之前**，
feed 一抛错就把数字一起带走（10 处调用点全中：每次保存文献/建工程/记踩坑后看板都不刷新）。
→ **统计优先，附属品降级**：先拿数字，feed / Obsidian 状态失败只 `console.warn`。

### 4.2 统计口径数错了东西

看板原来数的是「用工具生成的结构化产物」（`.md` 笔记卡片、复现工程目录、踩坑日记、周报），
可用户实际的主线是「**翻论文 → 翻译 → 划词 → 批注**」——这条线上的产出
（论文原文数、已译页数、批注条数）**一个都没被计入**，所以即使初始化正常也是 0。

→ 新增 `_real_output_stats()`：`pdf_total` / `note_total` / `translated_pages` /
`annotation_count`（后两项取自同步目录 → 两台设备共享同一份数字）。
看板四格改为：**论文典藏 / 精读译文 / 划词批注 / 复现工程**，旧计数挪到悬停提示。

> **教训**：统计口径要对齐用户的真实工作流，不是对齐代码里"顺手生成的东西"。

### 4.3 滚动联动是治标，不是治本

曾把左右两栏做成「按页对齐」的滚动联动（左栏滚到第 N 页 → 右栏跟到第 N 页译文），
想解决「右边对不上左边」。

**用户反馈**：「它不是译文滚动原文也跟着滚，但这个页码却有一些错位，我感觉不是很需要这个功能。」

**为什么该删**：译文是**流式**的（整页一次翻译、按段落铺开），跟 PDF 的整页版面
本来就不可能逐像素对齐，强行联动反而制造跳动。
真正让两栏「对得上」的是**页内文本顺序**（§4.3 契约），不是滚动同步。

> **教训**：现象在 UI 层、根因常在数据流上游。**先往上游找**。

### 4.4 路径安全：junction 会击穿 `resolve()`

**症状**：把 vault 搬进坚果云（junction）后，`serve_pdf` 全报
`400 非法路径: 解析后位于 base 目录之外`，vault 列表 500，**PDF 一个都打不开**。

**根因**：`generator._safe_path()` 用 `resolve()` 做包含性检查，而 `resolve()` **穿透 junction**：

```
base_resolved = E:\zaiwu\个人工作台                       (REPO 本身不是 junction)
resolved     = C:\...\我的坚果云\...\01_Literature\x.pdf  (junction 被穿透)
relative_to(base_resolved) → ValueError → 400
```

**修法**：改用**逻辑路径** `os.path.abspath`（不 resolve）做包含性检查 ——
abspath 同样把 `..` 归一化，遍历攻击照拦；绝对路径/换盘符也拦得住。
唯一放宽的是「指向 vault 外的符号链接/junction 可被读到」，而建 junction 需要对
vault 目录有写权限，那已在可信边界内。

> ⚠️ 同一个洞在**桌面端云端交接**里也有：`inspectSessionHandoffPaths` 用
> `realpath(file)` + `pathWithin(workspaceRoot, physicalPath)` → junction 里的文件
> 会被判 `outside_workspace` 而**拒绝上传云端**。
> 所以「vault 搬坚果云」与「转成 MiniMax Code 云端项目」**互斥**。

#### 部署配置：两台设备怎么配

| `.env` 键 | 值 | 含义 |
|---|---|---|
| `ANNOTATIONS_DIR` | `<Nutstore>\<id>\我的坚果云\MicroBench同步` | 同步根（两台必须一致） |
| `SYNC_PDF_BODY` | `1` / `0` | 是否经同步通道传 PDF 本体 |

- **`SYNC_PDF_BODY=1`（默认）**：PDF 走 files 通道（增量 + 墓碑 + 自动补齐）。
  适用于「vault 留在本地、只同步数据」。
- **`SYNC_PDF_BODY=0`**：files 通道跳过 PDF 发布/还原/墓碑。
  适用于「vault 整体搬进坚果云 + junction」——那时两台读的是**同一份**物理文件，
  再传一份只会让坚果云多占一倍空间（76MB → 152MB）。
  关闭时**连墓碑也不发**：同一份文件被删，两台天然都看不到。

> 搬完 vault 后，坚果云里 files/*.pdf 记得用 `POST /api/sync/files/release {"all":true}`
> 清掉（保留清单），否则会留一份 76MB 的冗余副本。

#### junction 要建哪几个目录

vault 搬进坚果云后，**两台设备各自**在原路径建 junction，指向
`<Nutstore>\<id>\我的坚果云\MicroBench工作台\<同名目录>`。

**要共享的（笔记 / 工程 / 数据）**：

```
00_Dashboard   01_Literature   02_Reproduction   03_Knowledge
04_Weekly_Reports   _Templates   assets
```

`.obsidian`：**两台都装 Obsidian 就共享**（同一套配置，插件和外观不用各配一遍）。
代价只有 `workspace.json`（窗口布局）谁后打开谁赢 —— 比各留各的省事。

> ⚠️ **两台建的目录名必须一致** —— 不一致的话坚果云会各传各的，不会合并。
> 实测：两台各自建好同名 junction 后，写一边 → 另一边 1 秒内可见（同一份物理文件，
> 不是两份各同步各的）；删一边 → 另一边一起消失。

> ⚠️ **搬之前先停服务**：文件被占用时跨盘移动会在 delete 阶段失败
> （copy 成功 delete 失败 → 留下两份）。真遇到了跑 `move_vault_to_cloud.py --undo` 还原。

#### 搬 vault 到坚果云：三步的正确顺序（2026-10-23 两台设备实测）

`move_vault_to_cloud.py --apply` 的逻辑是「**目标已存在 → 跳过**」。
所以只要云端已经有同名目录，它就**不会建 junction** —— 那台设备的 vault
仍然是本地独立副本，两边各传各的，永远不会合并。

正确顺序：

1. **先合并**：把本地 vault 里云端没有的文件复制进云端目标目录，逐文件 sha256 校验。
   实测对方设备先搬过一次，云端 `01_Literature` 只有 24 个 PDF，**缺全部 24 个笔记 .md**
   （精读笔记 / 文献速览卡片 / 划词答疑 —— 用户真正的产出），`_Templates` 也是旧版。
   直接 `--apply` 会跳过 `01_Literature` → 那 24 篇笔记永远上不了云。
2. **再搬**：`--apply` 只处理云端还没有的目录
   （`00_Dashboard` / `02_Reproduction` / `03_Knowledge` / `04_Weekly_Reports` / `.obsidian`）。
3. **最后换 junction**：把 8 个本地目录改名到备份区（同盘 rename，秒级、不复制数据），
   再 `mklink /J` 指向云端副本。校验 realpath 对得上 + 文件数一致；
   回滚 = 删 junction + 改名回去。

> ⚠️ 跨盘 move 是 copy+delete：文件被占用会在 delete 阶段失败、留下两份。
> 搬之前先停服务，Obsidian 也要关（它锁 `.obsidian`）。
> 两台设备必须各自都做这三步，且云端路径一致 —— 否则就是两份互不相干的 vault。

#### 模板冲突：vault 的 `_Templates` 是用户地盘

vault 里的 `_Templates/` 被 gitignore，是**用户可自由改写**的；
`microbench/templates/` 是随代码走的兜底副本（`ensure_vault_templates()` 只在缺失时补，
**绝不覆盖**）。两台设备各改各的 → 合并时同名文件必冲突。
→ 判据：和 `microbench/templates/` 一致的是「标准骨架」，
带用户上下文（学校 / 课题组 / 在研方向）的那份才是「个人版」→ **个人版优先**，
标准版留档。

#### `.obsidian`：共享（最终决定）

`.obsidian` **两台都建 junction 共享** —— 同一套配置，插件和外观不用各配一遍，
省事。代价只有 `workspace.json`（窗口布局）**谁后打开谁赢**，可以接受。
要建哪几个目录、怎么建，见上面「junction 要建哪几个目录」。
（2026-10-23 定为共享，**推翻了此前「各留各的」的写法**；30461 这台已是共享，
25353 那台还差一个 `.obsidian` 的 junction。）

#### `.env` 的部署开关会漏进测试进程

`microbench/llm.py` 在 import 时 `load_dotenv(..., override=False)` →
`.env` 里的 `SYNC_PDF_BODY=0` 会进 `os.environ` → files 通道的 11 个测试全红
（报「通道被关」，看着像代码坏了，其实是环境泄漏）。
→ `tests/conftest.py` 用 autouse fixture 把 `SYNC_PDF_BODY` 钉成 `1`，
测试固定跑默认行为；关闭模式由 `test_pdf_body_can_be_disabled` 显式 monkeypatch。
**任何按部署需要写进 `.env` 的开关，都要在这里钉住。**

### 4.5 抽取层：双栏顺序（详见 READER_CONTRACT）

两种直觉做法**都错**：

| 做法 | 后果 |
|---|---|
| 整栏裁剪（crop 左半 + crop 右半） | 跨栏表格被栏缝劈成两半，碎片夹在正文中间，表标题被甩到最后 |
| 逐带交替拼接（左1→右1→左2→右2） | 译文左右横跳，阅读阻碍大 |

**正解**：按带分组（判据是「栏缝位置有没有空隙」，不是「最长空隙在哪」）→
双栏带归左右栏、跨栏带归整宽块 → 跨栏块 + 紧邻表体合成一个单元整块保留 →
最终顺序 = **左栏整段 → 右栏整段 → 跨栏单元**。

### 4.6 碎公式会被模型「自信地编造」

公式在抽取结果里是好几行（分子一行、分母一行），`=` 和 `[?]` 不在同一行 →
按单行判据失效 → 模型看到碎片会自己补全出一个**原文里根本不存在的公式**。

**错公式比没公式危险得多**（用户会照着它去算）。

→ 抽取层把「连续公式碎片行」整段标成 `⟦FORMULA⟧` → 提示词要求原样输出该标记 →
后处理换成「【公式未能完整提取，请对照左侧 PDF 原文】」。

### 4.7 跨设备同步：owner 与墓碑

- **墓碑只对「自己删的」生效** → 清单里必须有 `owner`（发布者 device_id）。
  没有 owner 会出这种事故：A 有 1、2 篇，B 有 2、3 篇 → B 同步时看到 1 不在自己机器上
  → 误判成「B 把 1 删了」→ A 下次同步把自己那篇删掉。
  → **跨设备场景里，「我这里没有」≠「我删了」。**
- `propagate=false`（默认）= 只从云端释放实体，本机保留；`true` = 对方也删本地。
- **增量**：先比 `(size, mtime)` 快判 → 都没变就零传输；变了才算 sha256 →
  sha256 相同说明内容没变（只更清单），不同才真重传。实测 24 篇 / 76MB：
  首次 183ms，之后 51ms 零传输。
- **paper_id = 文件名(去.pdf, 截70) + "_" + djb2(完整相对路径)的 base36**，
  前后端必须逐字一致（`app.py: _paper_id_for` 复刻了 `index.html` 的算法，
  `tests/test_paper_id_contract.py` 锁死这个契约）。落盘 safe key 用
  `c.isalnum() or c in "_-"` → **中文会被保留**（Python 的 `isalnum()` 对中文是 True），
  别写成 ASCII 过滤。

### 4.8 坚果云：什么位置才真的会上云

| 位置 | 上云？ |
|---|---|
| `%USERPROFILE%\Nutstore\xxx` | ✗ 客户端工作目录，不上云 |
| `Nutstore\<镜像id>\` 下新建顶层文件夹 | ✗ 被忽略（`sync_errors` reason=7、上传量恒为 0） |
| `Nutstore\<镜像id>\我的坚果云\xxx` | ✓ |

镜像 id 读 `%APPDATA%\Nutstore\config\UsersMap.json` 的 `users` 映射。
**验证"真的上云了"**：`%APPDATA%\Nutstore\db1\nutstore.db` →
`sndconfig*.localAbsPath` 是同步根、`sndobject*` 表是已登记的同步对象清单、
`sync_errors` 表应为 0 行。这比看文件存在与否可靠。

> `traffic_history` **不是一张独立的表**，是 `config` 表里的一个 key，值是 JSON：
> `select key, value from config where key = 'traffic_history';`
> 里面有 `totalUploadSize` / `totalDownloadSize` / `latest_upload_file_date`。
> （在 nutstore.db / event.db / db_backup 里按表名找是找不到的，别浪费时间。）

### 4.9 环境类

- **PS 5.1 读 UTF-8 脚本乱码** → §2.1
- **`.bat` 用 LF** → cmd 解析错位（`'rt' is not recognized`）→ §2.6
- **`.gitignore` 的 `/_*.ps1`、`/_*.py`** 会把根目录的有用脚本一起吃掉
  （`_make_shortcuts.ps1`、`_vendor_assets.py` 现在都不在仓库里，对方那台拿不到）。
  → 有用的脚本请放 `microbench/` 下，或换不带下划线的名字。
- **代理兜底不能永久粘住**：原来"代理已死"是全局永久标记 → 用户把代理客户端重新打开后
  请求仍然直连，而 arxiv 直连会 SSL 校验失败。改成 `bypassed_until = now + 300s`，过期自动恢复。
- **代理兜底必须用 `proxies={"http": None, "https": None}`，不能换 `requests.Session`**：
  换 Session 会让测试里对 `requests.post` 的 monkeypatch 失效 → 假响应没被用上 →
  测试会真的发网请求；更糟的是全局"代理已死"标志被某个用例点亮后，后续用例全走另一分支。
- **winget 源会 403 / `source reset` 要管理员** → 装工具时优先找免安装 ZIP
  （本机 PowerShell 7 就是这么装的：官方 SHA256 校验后解压到用户目录，不需要管理员）。

### 4.10 前端的老坑

- `if (!res.ok) return;` 写在声明之前 → 一旦请求失败，后面的函数全是 undefined
  → HTML 的 inline `onchange` 处理器报 ReferenceError。
- 声明写在块里 → 块级作用域，块外调不到 → 这就是为什么代码里有十几处 `window.X = X` 导出。
  **新写代码请直接放顶层，不要塞进裸花括号块。**

### 4.11 桌面应用窗口（pywebview / Edge WebView2）

用户要求「点开就像独立应用，不要在浏览器里」→ 2026-10-24 落地 `desktop.py`（根目录）。
用的是 Windows 自带的 **Edge WebView2**（`pywebview` 驱动），不是 Electron，不打包 100MB 运行时。

| 入口 | 行为 |
|---|---|
| `MicroBench Desktop.bat` | `pythonw desktop.py` —— **无黑窗**；日志写 `logs/desktop.log` |
| `install_shortcuts.ps1` | 装桌面/开始菜单快捷方式（**名字不能带前导下划线**，见 §4.9） |
| `start_workbench.bat` | 网页版：服务 + 默认浏览器（服务跑在独立控制台窗口） |

坑（都踩过）：

- **pywebview 事件回调的形参必须叫 `window`**。`webview/event.py` 的派发规则是
  「0 参 → 无参调用；形参名含 `window` → 传窗口对象；否则按触发时的 args 调用」，
  而 `loaded` / `closing` 的 args 是**空的** → 写成 `def _on_loaded(win)` 会抛
  `TypeError: missing 1 required positional argument`（而且被 pywebview 的
  `logger.exception` 吞掉，只在日志里留一行，窗口照样开着，很难察觉）。
- **`private_mode` 默认是 `True`** → 每次启动 localStorage 全清（主题、Obsidian vault 名、
  阅读器状态、答疑面板几何全丢）。必须 `private_mode=False` + `storage_path`。
- **`pythonw.exe` 下 `sys.stdout` / `sys.stderr` 是 `None`** → uvicorn 的日志处理器
  拿到 None 会静默丢日志。启动时把两个流接管成 `_Tee(原流, 日志文件)`。
- **跨进程置前窗口**：`SetForegroundWindow` 直接调经常无效，要
  `AttachThreadInput(当前线程, 前台窗口线程)` 再 `BringWindowToTop` + `SetForegroundWindow`。
- **单实例**：用 `EnumWindows` 按标题找已有窗口（认「微电子科研工作台」，
  **别认** `MicroBench Server - python -m uvicorn ...` 那个控制台窗口，否则会误杀服务）。
  第二次双击改为激活已有窗口，否则会出现「两个窗口，关一个把服务带走」。
- **端口探测必须校验页面特征串**（`b"MicroBench" in body`），老教训复用（§4.4 / start_workbench.bat）。
  探测请求要带 `ProxyHandler({})`：本机 `HTTP_PROXY` 指向本地代理客户端，代理没开时必 ProxyError。
- **兜底链**：pywebview 不可用 → Edge/Chrome `--app=` 模式（同样无地址栏）；
  这条路线里服务必须**分离进程**（`DETACHED_PROCESS`），否则启动器一退出服务就死。
  再不行就弹 `MessageBoxW` 报错 —— 绝不静默失败。

> ⚠️ 改 `desktop.py` 时的连带影响：它跑的是**同一个** `microbench.app:app`，
> 端口、vault 路径、`.env` 全部一致。窗口关掉 = 它自己起的服务停掉；
> 若服务是 `start_workbench.bat` 起的（它不是本进程起的），窗口关掉**不会**停服务。

### 4.12 阅读器：表格乱 / 栏序错 / 公式没法看（2026-10-24）

用户一次报了三个问题，逐个定位后一起修的。三条的根因**都不在模型**：

**① 表格"很乱 + 占篇幅大"** —— 模型本来就把表格输出成 Markdown 表格
（`| 性能 | BaTiO₃ | … |`），但渲染层 `formatMarkdownLite()` **没有表格支持**
→ 用户看到一堆竖线加换行。
→ 现在渲染成真正的 `<table class="md-table">`（单元格 nowrap + 横向滚动），
提示词也明确要求"表格用 Markdown 表格输出"。

**② "还是会先翻译先出现字的那一列"** —— **代码早就修好了，用户看的是旧快照**。
实测：快照 `saved_at=2026-10-23T14:17`，而栏序修复的 commit 是 10-23 16:36
→ 读者照旧恢复 10-23 14:17 的译文（逐字与用户截图一致）。
→ 快照现在带 `extractor_version`（`paper_text.EXTRACTOR_VERSION`），
加载时版本对不上 → **整份作废重翻**。改了抽取算法就必须 +1。

**③ 公式"分子分母分不清谁是谁"** —— 碎片硬拼 LaTeX 会把分子分母装反，
而**错公式比没公式危险得多**（用户会照着它去算）。
→ **不拼**：抽取层只记公式在页面上的坐标 `⟦FORMULA:x0,y0,x1,y1⟧`，
前端拿坐标去 PDF 原页裁一张图贴进译文 —— 和论文里一模一样，绝不会错。
新增接口 `GET /api/paper/formula_image`（PyMuPDF 按坐标裁 PNG）。

**顺手修的（抽取层）**：

- **裁剪边界切在字上 → 相邻两行被逐字交错成乱码**：
  `cannot be coprocessed…` + `would oxidize the iron…`
  → `cwaonunlodtobxiediczoeptrhoeciersosnedanwditdhraomxiadteicaflelryroree`
  原因：pdfplumber 裁剪会把压在边上的字形**切掉一半**，剩下的 top 被裁到
  边界值 → 跟下一行的字混成同一行。
  → 裁剪时**向外扩 16pt**，再按「行的 top 属于哪一带」过滤（`_crop_lines`）。
- **左栏只剩几个词的带被判成"整宽"** → 整宽裁剪把左右两栏合进同一行
  → 又是逐字交错。判据改成**只看栏缝干不干净**（`_band_kind`）：
  中间空 → 双栏；中间被填满 → 整宽（跨栏表格）。
- **页尾截尾会切坏标记**：标记里有小数点（`⟦TABLE:28.2,494.6,…⟧`），
  而截尾是按"最后一个句末标点"切的 → 标记被切成半截 → 多模态读不到那张表。
  → `_trim_page_tail()` 的切点必须避开标记内部，标记自己的 `⟧` 才是合法切点。

**判据的坑（改抽取层时务必记住）**：

- 「有等号就是公式」会把**正文整行吞进公式图**
  （`f(ω) = … Substituting the inverse` → 译文里这句话消失）。
  → 现在先数**词状 token**（≥3 字母、纯字母、`(cid:N)` 不算）：
  ≥2 个 → 是正文。`eeﬀ ¼ 33 33 (3)` 只有 1 个 → 才是公式。
- 表格行也含大量 `(cid:)` 碎片 → **必须要求等号**，否则整张表变成一张图。
- 公式判据改紧后要跑全库扫描（24 篇 391 页），确认正文词没被吞。

### 4.13 表格：截图交给多模态模型逐格抄（2026-10-25）

用户原话："对于表格的话个别单元他这个都抽错的话你也给我直接上这个多模态读图吧……
我要的是这个阅读的……至少顺序要是很正常的"。

**为什么表格不能交给文本模型**：表格在 PDF 文本层是碎的
（`d (pC/N) (cid:4)90 (cid:4)175 (cid:4)109 B700 16.5` —— 连负号都认不出），
文本模型只能靠上下文猜对齐 → **整行数值错位**（实测 d31 行的 16.5 跑到 d33 行、
7200 变成 B700）。

**现在的做法**（与公式同一套路）：

- 抽取层用**表题**（"Table N." / "表 N"）定位表格区域 → 输出
  `⟦TABLE:x0,y0,x1,y1⟧`（表格碎片不再进正文）；
- 服务端裁图（PyMuPDF，scale=4 ≈ 288 DPI）→ 多模态模型逐格抄 → Markdown；
- 标记换成中性的 `⟦TABLE_n⟧` 送进主翻译，翻译完按位置把表格拼回去；
- 前端渲染成真正的 `<table>`（§4.12①）。

**实测**：JACERS Table I 抄出来是 `−90 | −175 | −109 | | ∼700 | 16.5`
—— 与论文里印的一模一样，连论文自己把 2605 排进表头的排版毛病都照抄了。

**判据的坑**：

- **不能用"数字多"判表格**：公式行也全是数字（`a ¼ 33 31 (2)`），
  按数字判会把公式当表格（实测 eq (2)/(3) 被误判成两张表）→ 改成按
  **"表状行成段"**（词短 + 含数字 + 没有像英文单词的东西）。
- 判"像英文单词"不能只看字母数：**材料名也是长词**（BaTiO3 / Terfenol-D），
  要看**是否以小写为主**。
- 表格区域**只能由"表状行"延续**：表题后面跟一段"短"正文时，区域会一直
  延伸，把那几段正文从正文里删掉（独立审查发现 13 处）。
- 表题**不进区域**：留在正文里由主模型翻译，图里只有表体，两者不重复。
- 主模型偶尔会把 `⟦TABLE_n⟧` 弄丢 → 服务端兜底（漏掉的表格追加到译文末尾）。

**这一轮踩过的三个坑（独立审查揪出来的，2026-10-25）**：

1. **裁剪框越界会抛异常**：`pdfplumber.crop()` 的框超出页面就抛 ValueError。
   为避开切字而外扩 16pt，很容易把框推出页面下沿 → 整组文字全丢
   （实测 24/391 页中招，JACERS 第 4~6 页正文只剩 1/4）。
   → 裁剪框四边都 clamp 到页面内。
2. **静默的 `except Exception: return []` 会把整页正文丢失藏起来** →
   兜底必须留痕（打堆栈到 stderr），否则"译文少了一大截"根本查不出来。
3. **合并同类型带会跨过被排除的表格带** → 整宽裁剪又把表格行捞回正文
   （表格既在图里又在文字里）→ 用哨兵组打断合并链。

### 4.14 阅读器批注：重译后记录不能丢（2026-10-25）

用户原话："如果我在翻译上写了标注，然后它又被重新翻译了，那这块儿标注是不是就直接没了？
……这个记录要有。还有就是我点开哪一页，标记的列表是从那个开始的，不要每次都从第一页开始。"

**「失效」不等于「丢失」** —— 这是两条不同的承诺，缺一条用户就白干了：

| 场景 | 表现 |
|---|---|
| 译文侧标注（无 `rects`）+ 重译后引文对不上 | 标 **⚠ 位置失效**（虚线边框），但引文/笔记/颜色全保留 |
| PDF 侧标注（有 `rects`） | 高亮画在 PDF 画布覆盖层上，**不受重译影响** → 不算失效 |
| 译文又变回原来那句 | `orphan=false`，失效标记**自愈** |

判据在 `applyAnnotationsToPage()`：
`a.orphan = !(a.rects && a.rects.length)` —— 有 PDF 坐标的永远不算失效。

**抽屉定位**（`openAnnoDrawer()`）：列表按页排序后 → 本页第一条 → 本页没有就落
**下一页的第一条** → 再没有就退回第一条（旧写法永远 `querySelector(".anno-item")` = 第一条）。

> ⚠️ 改前端后**桌面应用要重启**：`index.html` 是 `no-store`，但 pywebview 只在
> 启动时拉一次首页，刷新浏览器不等于刷新桌面窗口。

**通用教训**：
1. **"功能没了"和"位置对不上"要用不同的 UI 表达**。直接删除 = 用户白写；
   标成"失效但保留"才不伤信任。
2. **测试要反向验证**：写完 node 断言后 `git stash` 掉改动再跑一遍 ——
   必须变红，否则说明断言恒真、测了个寂寞。
3. **前端单文件里的函数可以被切片出来单测**：`sliceFn()` 切源码 + `new Function` 注入桩
   → 测的是**发布出去的那份代码**，不是抄一份（见 `microbench/_test_reader_annotations.js`）。
4. **别用"鼠标松开的位置"判断选区属于哪一块** —— 拖选一整行时用户常把鼠标松在
   行尾留白上，`e.target` 不是文字元素 → 页码丢失 → 批注被存成"章节视图"，
   用户在自己正在看的那页上找不到它。
   → 用 `_resolveSelectionOrigin(e.target, range, activeSection)`：
   先按 `e.target` 判，判不出来就用 `range.commonAncestorContainer` 兜底。
   （PDF 侧同理：判不出来就收不到 `rects` → 原文高亮也没了。）

### 4.20 模型层：换模型 / 降级 / 自定义 API 模型（2026-10-25）

详见 [`docs/LLM_CONTRACT.md`](docs/LLM_CONTRACT.md)。要点：

- **协议层早就通用**：底层只有一种调用 `POST {base_url}/chat/completions`，
  主模型（minimaxi）和备用（agnes-ai）都是 OpenAI 兼容 → 接自己的模型没有协议障碍
- **提示词不绑死 M3**：翻译/抄表提示词都是纯中文自然语言指令
- **实测能力矩阵**（JACERS p2，表格数值与论文逐一核对）：
  M3 翻译 15s + 抄表 3–6s；**M3.1-Flash 翻译 324s 撞线报 504**（强制思考吃光
  token 预算）→ 只能答疑不能翻译；Agnes 翻译 104s + 抄表 25–35s（数值仍然准）
- **用户自己的 API 模型**：`.env` 里 `LLM_CUSTOM_N_{NAME,BASE_URL,MODEL,API_KEY,RPM}`
  （最多 5 个）→ 出现在模型下拉框，用于翻译/抄表/答疑
- **限流按模型配**：只有免费版 Agnes 吃严格限流（11 RPM / 6s），付费模型
  （MiniMax/DeepSeek/自定义）默认 30 RPM / 2s
- **连不上自动退回**：自定义模型失败 → 主模型 → 备用模型；界面显示**实际**用的模型
  + "（已自动退回）"，不能显示"用户选的那个"（会让人以为换模型没生效）
- ⚠️ **同步 / 异步 / 流式三条路径必须对齐**（2026-10-25 独立审查）：
  `chat()` / `chat_async()` / `chat_stream_async()` 三条路径里
  "把 model_name 覆盖到 primary"这一步**只写在同步路径上** →
  答疑（走流式）选了别的模型，实际发出去的仍是主模型，
  而徽标还显示用户选的那个 → **换模型在答疑入口完全落空，还白烧主模型额度**。
  改任何一条路径都要把另外两条一起看。
- **表格跟随所选模型**：`_prepare_tables` 必须把 model 传下去（之前只有独立接口
  `/api/paper/read_table` 传了，整页翻译那条主路径漏了）
- **抄表返回值要校验**：模型回"截图里没有表格"是**成功返回**不是异常 →
  不校验就会把这段说明当表格拼进译文

> ⚠️ **测模型能力要用生产路径的真实输入**。我曾拿手写的猜的坐标去测抄表，
> 得出"Agnes 读不了图"的**相反结论**；用抽取层真实的表格 rect 复测，
> Agnes 的数值和论文一字不差。拿合成/猜测的输入评估能力，结论会反过来。


### 4.15 安卓平板 APK（手工打包，不走 Gradle）

> 完整审查报告见 [`docs/HANDOFF_TABLET_2026-10-25.md`](docs/HANDOFF_TABLET_2026-10-25.md)
> 和 [`docs/AUDIT_2026-10-25.md`](docs/AUDIT_2026-10-25.md)（第二轮全面审计）。
> 下面是「改回去也不会立刻报错、但功能会静默坏掉」的那几条，护栏在
> `android/_test_apk_contract.py`（104 项）里钉死了。

- **裸 socket 的 TLS 顺序**：`createSocket()`（无参）→ `setSSLParameters(
  endpointIdentificationAlgorithm=HTTPS)` → `connect()`。
  ⚠ 用 `createSocket(host, port)` **是错的** —— 那个版本内部就完成了 TLS 握手，
  这时再设参数来不及，**主机名验证等于没开**，坚果云应用密码会裸奔在中间人面前。
- **`java.util.Base64` 是 API 26 才有的**，而 minSdk 是 24 → Android 7.x 上
  第一次网络请求就 `NoClassDefFoundError`（d8 不拦，只警告）。
  `android.util.Base64` 是 API 8，但 Android 专有、桌面 JVM 跑不了（要对拍测试）。
  → 自己写 20 行，两边都能跑。
- **跨线程改 View = `CalledFromWrongThreadException`**：后台线程里
  `setText()` → 抛异常 → 被 `catch (Exception)` 吞掉 → 判定成「失败」。
  表现是「测试并保存永远失败、凭证存不进去」。**UI 只能在主线程上改。**
- **坚果云会限流**：请求多了返回 `503 BlockedTemporarily`
  「Too many requests are received recently」——
  **这不是配置错了，是请求太频繁**。
  一次完整同步是 50+ 个请求（每个目录一次 PROPFIND、每篇笔记一次 GET），
  点几次「测试并保存」就必然踩到。
  → 必须**节流 + 退避重试 + 少发请求**（按 `getlastmodified` 跳过没变的文件）。
  护栏：`android/_test_dav.py` 里有真的 503 模拟。
- **`HttpURLConnection` 只放行标准方法**，PROPFIND/MKCOL 只能裸 socket；
  裸 socket 默认**不做主机名验证**，要手动开。
- **`URLDecoder` 走 form 编码规则**，`+` 会被解成空格 → 文件名带 `+` 的论文
  永远打不开。解码前先把 `+` 换成 `%2B`。
- **APK 里的 origin 是 `https://appassets.androidplatform.net`** → 外部 API
  不会回这个 origin 的 CORS 头，`fetch` 必被打死。AI 请求要**走原生代理**。

### 4.16 静默坏掉的三类（2026-10-25 全面审计的产出）

> 详细清单见 [`docs/AUDIT_2026-10-25.md`](docs/AUDIT_2026-10-25.md)。
> 这一轮最重要的教训不是某个具体 bug，而是下面这三种**形状**。

1. **「种子/快照/本机副本」覆盖「真正的数据源」**
   - 平板的 `loadSeed` 拿**导出期快照**覆盖本地正文 → 刷新丢改动，
     而且 `dirty` 还在、`baseHash` 却是旧文本的哈希 → **下次 push 把旧文本写回云端**。
   - 同步 push 里那段「兼容旧版本」把本机**陈旧副本**合并回共享目录
     （deleted 传空）→ **已删除的批注被复活**。
   - 译文快照**整份替换** → 对方已翻的页被清空 → 重新烧 token。
   → **通用判据：任何「本机副本 → 真源」的回灌都要能被质疑；合并必须按业务主键
     （page_num / 批注 id），不能整份覆盖，也不能在没有墓碑的情况下做并集。**

2. **测试全绿但功能是坏的**
   - 平板三套 JS 测试**不在 `pytest tests/` 的收集范围**（`pytest microbench/`
     → 0 tests collected）→ 验收信号对页面代码**没有保护力**。
   - 渲染器测试恰好避开了会失败的攻击面 → 38 条全绿，而 XSS 打得通。
   → **凡是「手工跑的脚本」都要挂进 pytest；验收命令收不到的东西 = 没有验收。**

3. **`esc()` 这类「看着像对」的转义函数**
   - 引号转义写成了 `E_QUOT + "quot;"`（应该是 `E_AMP`）→
     产出 `"quot;` 而不是 `&quot;` ⇒ **引号压根没被转义**，
     属性注入照样打得通。上线很久都没被发现。
   → **转义函数要有「用真解析器验证输出」的用例**，不能只测「危险词被拦下」
     （`/on[a-z]+=/` 这种正则分不清「属性名」和「属性值里的文本」，
     会给出假阳性/假阴性）。

### 4.17 这轮自己踩的坑（工具层面的）

| 坑 | 纪律 |
|---|---|
| `node -e "带引号的代码"` → PowerShell 把引号吃掉，报「意外的标记」 | **一律写脚本文件再跑**（§2.2，本项目已踩 5 次） |
| `bash` 里 `cd` 之后写文件 → 被安全层拦 | 用绝对路径，别先 cd（§2.3） |
| `write_text` 在 Windows 上把 LF 写成 CRLF | 「还原文件」要用 `newline=""`，否则整个文件行尾被改 |
| `query kind=editable` 扫到 API Key 字段 → 运行时挂 `requiredNextTool: ask_user` 门禁，**把 bash/read/task_output 一起锁** | 不对含凭证字段的页面做 editable 扫描；`task_output` 带 `offset: 0` 能绕过 |
| `subprocess.run(text=True)` 在中文 Windows 按 GBK 解码 node 输出 → emoji 路径 `UnicodeDecodeError` | 显式 `encoding="utf-8"`（§4.9） |
| 删代码时漏掉了一个变量定义 | 后端回归护栏立刻抓到 `NameError` —— 这就是写护栏的价值 |

---

### 4.18 快照版本闸门：三端必须逐字一致（2026-10-25 合并时挖出来）

> **一句话判据（三处一模一样，别改一处漏一处）**：
> `saved_extractor_version != 当前抽取算法版本` → **整份快照作废**。
> 「**没有** `extractor_version`」**也算对不上** —— 那是旧算法翻出来的。

三处必须一致：

| 端 | 位置 | 行为 |
|---|---|---|
| 桌面端读取 | `app.py: api_pdf_snapshot_load` | 版本不符 → `pages=[]` + `stale_extractor=True`（阅读器照常重翻） |
| 桌面端合并 | `app.py: _merge_snapshot_pages` / `_merge_snapshot_files` | 先过 `_snapshot_extractor_ok()`，对不上就**不合** |
| 导出器 | `export_offline.py: collect_translations()` | 只烤当前版本的译文（旧的**不烤**） |
| 平板端读取 | `static/tablet/app.js: loadTranslation()` | 版本不符 → 返回 null + `state.transStale=true` |

**为什么这是「合并后才暴露」的坑**：对方（10-24）给快照加了 `extractor_version`，
桌面端读取时整份作废重翻；但**磁盘上的旧页还在**，而**按 page_num 合并**的逻辑
（本项目自己的设计，为了两台设备轮流翻同一篇时不清空对方翻过的页）
会把它们**合回来** ⇒ **过期译文被复活**，而读取时的作废逻辑只看最终那一份，
**管不到它**。→ 合并之前必须先看版本。

**「没有版本号」也算旧 —— 这条最容易写漏**：
本机现存 3 份快照（`logs/_pdf_snapshots/`）**全都没有** `extractor_version`
（它们是旧算法翻的，栏序是反的）。
平板端一度写成「有版本号且不等于当前版本才作废」= **把没版本号的旧快照当成好的放行**
⇒ 用户看到的就是错的那一列。**宽松判据 = 没有判据。**

**平板本地缓存也必须带版本闸门**：
译文缓存在 IndexedDB 的 `meta` 里（键 `trans_<paper_key>`），缓存命中时
**根本不会去查云盘**、也就绕过了版本检查 ⇒ APK 一升级（抽取算法变了），
旧译文会**从本地缓存直接返回**。
→ 缓存键改成 `trans_v<版本>_<paper_key>`（`transCacheKey()`），
换版本自动失效，不用手动清缓存。

**教训**：两端各自「都做了版本检查」**不等于**规则一致 ——
一个宽松一个严格，用户看到的结果就取决于**看的是哪一端**。
跨端规则要**逐字对齐**，并且**用同一份行为级测试**锁住（本次：
`tests/test_tablet_snapshot_version.py` 15 项 + `_test_tablet_hardening.js` 第 [13] 节 8 项）。

#### ⚠⚠ 导出版陷阱：`export_offline.py` **以脚本模式**跑时取不到版本

**症状**：代码里明明写了「只烤当前版本的译文」，平板单文件里却还是躺着
**旧的坏译文**（栏序反的），而且 `trans_ver` 是 `None`。

**根因**：`export_offline.py` 两种跑法，`sys.path` 不一样：

| 跑法 | `sys.path[0]` | `from microbench.paper_text import ...` |
|---|---|---|
| `python microbench/export_offline.py`（**脚本模式**） | `microbench/` | ❌ ImportError（仓库根不在 path 里） |
| `python -m microbench.export_offline` | 仓库根 | ✅ |

而 `paper_text.py` 内部用的是**相对导入**（`from .paper_fetcher import ...`），
只能以包形式导入 —— **退化成 `import paper_text` 也不行**。

**后果**：`except Exception: return None` → 取不到版本 → 版本过滤**整个不生效**
→ 旧的坏译文照样烤进平板单文件。**失败得很安静**：没有报错、没有告警、
所有测试都绿（因为测试跑的是 `import microbench.export_offline`，
走的是**包模式**，包模式是好的！）。

**修法**（`export_offline.py` 顶部）：

```python
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:      # ← 脚本模式必须有这一步
    sys.path.insert(0, str(REPO))
```

**配套纪律（三条，缺一不可）**：
1. **取不到版本就 fail closed**：一份译文都不烤 + **往 stderr 喊一声**。
   「宽松放行」在这里等于「把旧算法的坏译文发给用户」。
2. **护栏必须跑在「出问题的那种跑法」上**：
   `tests/test_tablet_snapshot_version.py::test_exporter_as_script_*`
   用 `subprocess` 真的以脚本模式跑一遍导出器 ——
   只在包模式下测，护栏是假的。
3. **交付验收盯产物，不盯代码**：
   `tests/test_apk_delivery.py` 直接拆 APK、读 `assets/index.html` 里的
   种子，断言 `trans_ver == 当前版本` 且**没有一份旧译文的 key**。

**通用教训**：`try: import X / except: return 默认值` 这种写法，
在**默认值本身决定了对错**的地方（默认值=「放行」而不是「拒绝」），
就是把错误藏起来的最佳位置。
要么让失败**响**，要么让它**保守**（fail closed）——
**不能又安静又宽松**。

#### ⚠ 交付物不能被测试顺手改掉

`_test_tablet_export.py` 原来会**重新导出到仓库根目录**那份
`MicroBench_平板工作台.html`（交付物）。于是：
「跑了一下导出测试」→ 交付物被换成新的 → 交付验收拿它跟 APK 里的
`assets/index.html` 逐字节比对 → **必然不一致** → 验收红。
（2026-10-25 被这个绊了两次。）

→ **交付物只由构建脚本产出**（`android/build_apk.ps1` 导出一次，
再 `Copy-Item` 到仓库根目录，保证两份**逐字节相同**）。
测试一律导出到**临时文件**，不许碰交付物。

### 4.21 AI 活动可视化：别让用户面对一个不动的转圈（2026-10-25）

用户原话："他有可能等了半天然后什么也没输出……所以我觉得最好还是做一个真的（可视化）"。

**问题长什么样**：
- 按页翻译只写「⏳ 翻译中…」，**没有计时、没有阶段**，210 秒才报超时；
- 划词答疑在**首个 token 之前**（模型思考期，可能 30–120 秒）完全没动静；
- 全局看不到"现在到底有几个 AI 任务在跑"；
- 翻译过程**没有任何取消入口**。

**做法**（`index.html` 的「AI 活动登记处」）：
- 所有 AI 请求（按页翻译 / 章节翻译 / 划词答疑）统一登记：`_aiTaskStart/Stage/End`
- 每 500ms 刷新「已用时 + 阶段」；阶段按已用时推断（提交 → 等待模型响应（含读表）
  → 模型处理中 → 很慢），**措辞里说明是推断**
- 工具栏徽标「🤖 AI 活动」显示任务数与最长耗时；点开是浮层面板，每行一个任务：
  类型 / 模型 / 已用时 / 阶段 / **✕ 取消**（真 abort 上游请求）
- 页面标签与 QA 徽标也带实时计时 + 心跳点（`.ai-beat`）
- 慢任务阈值：60s 变黄（提示"没动静了"）、150s 变红
- 任务结束进历史（最近 12 条），失败原因留痕

**通用教训**：
1. **"在加载"和"卡住了"用户分不清**。转圈必须带计时与阶段，
   超过阈值要明确说"很慢，可以取消"——否则用户只能干等或以为坏了。
2. **长任务必须可取消**。有 `AbortController` 就别浪费；
   没有 controller 的任务**别显示取消按钮**（只弹一句"已取消"、
   任务还在后台跑完，比没有取消更气人）。
3. **阶段可以是前端推断的**，但要在文案里说清楚是推断，别假装服务端报了阶段。
4. **"收尾"不许再动界面**（2026-10-25 独立审查抓到的阻断 bug）：
   `_aiTaskEnd` 收尾时调了 `onStage` → 章节翻译的 onStage 把
   `#reader-translation` 整个覆盖成 `done · 0s` → **翻完章节译文消失**，
   而 toast 还在说"✅ 翻译完成"。收尾显示归各自的成功/失败分支管。

   > 教训：给"进度显示"挂的回调，**只能写进度**，不能写正文区。
   > 同一个 DOM 节点既是进度容器又是正文容器 → 一旦收尾时多写一次就出事。

---

### 4.19 定期同步对方设备的升级（两台设备 + 平板 APK 必须同版本）

**问题**：两台设备共用一个 GitHub 仓库。对方（另一台设备）一直在做桌面端升级，
本机和**平板 APK** 必须跟着一起升 —— 否则用户就是「电脑上新版、平板上旧版」，
还会以为是两个 bug。

**做法**：`microbench/auto_sync.py`（幂等，没新提交就什么都不做）。

> ⚠ **2026-10-25 起，这台机（30461）上的定时任务已被用户取消**
> （原名「定期同步对方设备的升级（本机 + 平板 APK）」，
> `cron_id = d2c04537-d595-43ca-9efb-3a8a8ae8ca88`，每 2 小时一次）。
> 现在是**手动 / 按需**跑：`python microbench/auto_sync.py`。
> → 对方设备 push 之后，这台机**不会自动跟过来**：
> 要么用户说一声，要么对方明确要求时再跑。
> 脚本本身没变（还是幂等），随时可跑；**另一台设备上的定时任务不受影响**。

> **怎么读它的结果**：
> 在仓库根目录执行 `python microbench/auto_sync.py`，读最后一行
> `MB_SYNC_RESULT: {json}`，按 `action` 决定怎么汇报
> （`updated` / `up_to_date` / `conflict` / `tests_failed` / …）。
> ⚠ **这行必须是最后一行、且必须打得出来** —— 2026-10-25 那轮同步
> 其实**全部成功**（测试绿、APK 好、push 成功），却因为这一行里有
> 一个 GBK 编不出来的字符（`⚠`）抛了 `UnicodeEncodeError` → exit 1
> → 定时任务**误判成失败**。
> → 已修：stdout/stderr 一律 `errors="replace"`，且 JSON 里不放
>   GBK 编不出的字符。**改这个脚本时别把 emoji 放回 `messages` 里。**

```powershell
python microbench/auto_sync.py              # 正常同步
python microbench/auto_sync.py --dry-run    # 只看有没有新提交
python microbench/auto_sync.py --no-push    # 合并+重打 APK，但不推 GitHub
python microbench/auto_sync.py --no-apk     # 只同步代码
```

**流程（每一步失败都停手，绝不推半成品）**：

| 步骤 | 失败时 |
|---|---|
| 工作区不干净 | **跳过**，不碰用户正在改的东西 |
| `git fetch` | 报告，结束 |
| 合并冲突 | `git merge --abort` → 报告冲突文件 → **不推** |
| 测试有红的 | `git merge --abort` → 报告 → **不推**（带病上线比不同步更糟）|
| 重打 APK 失败 | 报告 → **不推** |
| `git push` 失败 | 合并**保留在本机**，报告 |

**只有测试全绿才 push** —— 对方设备直接 pull 这个分支，
推出去的就是「已经验过的」版本。

**代理**（这台机 30461）：
GitHub 访问**需要默认代理** `http://127.0.0.1:65532`；
清代理的 push 有时也能通 —— 脚本按「带代理 → 清代理 → 再带代理」重试。
（AGENTS.md §2.7「先清代理再推」是**另一台机 25353** 的做法，别照抄。）

⚠ **后端改动要重启服务才生效**（§2.5）。脚本**故意不自动重启**——
半夜关掉用户正开着的窗口不合适。报告里会明确提示「需重启工作台窗口」。

#### ⚠ 手动 `git pull` 之后，APK 不会自动重打 —— 但测试会因此变红

`auto_sync.py` 里有「重打 APK」这一步，**手动 pull 没有**。
而 `tests/test_apk_delivery.py::test_translations_are_baked_in` 断言：
「本地 `logs/_pdf_snapshots/` 里**当前抽取版本**的译文必须都烤进单文件」。

→ 只要本机新翻完的译文还没进 APK（单文件里 `trans` 是空的），
  这条测试就红，报「有当前版本的译文，却一份都没烤进单文件」。
  **看着像 bug，其实是 APK 过期**（2026-10-26 实测：APK 是 10-25 23:46 的，
  本机 10-26 11:41 又翻了三篇 → 必红）。

→ **手动 pull 之后必须补一刀**：

```powershell
pwsh -NoProfile -File android/build_apk.ps1     # 导出 → 打包 → 签名 → 交付验收(5 项)
python -m pytest tests/test_apk_delivery.py -q   # 必须 5 passed
```

⚠ **交付物只由构建脚本产出**（§4.18 末段）：`MicroBench_平板工作台.html` / `.apk`
不要手动改，也不要用测试来"顺手重新导出"——两份必须**逐字节相同**。

**盯它**：`logs/auto_sync.log`（脚本 stdout 追加）+ 定时任务自己的运行记录。
脚本最后一行输出 `MB_SYNC_RESULT: {json}`，给 agent 读。

---

### 4.22 解不出的数学符号：× / ≈ / ≥ 显示成 "?"（2026-10-25）

用户原话："只要涉及这个。有×的，它都显示的是个？……这个显示模块，这这一块还是得加强一下。"

**根因不是显示层，是抽取层**：TeX 排版的论文用的数学字体
（`MathematicalPi-Three` / `CMEX10` / `AdvP4C4E74` / `LCIRCLEW10` …）在 PDF 里
**没有完整的 ToUnicode 映射** → pdfplumber 只能吐 `(cid:3)` →
抽取层把它换成 `[?]`（**故意的**：曾把 cid 交给模型，模型把 `(cid:2)` 猜成
`\approx`、`(cid:3)` 猜成 `\times`，编出论文里根本没有的公式）。

**这些字形在 PDF 里是画出来的**（有字形轮廓）→ 裁一小块图让多模态认字，
和表格读图、公式裁图是同一套思路。

**做法**：
1. 抽取层把**孤立**的解不出符号换成带锚点的标记 `⟦G:<下标>⟧`
   （下标 = 该字形在"本页未解出字符表"里的位置，表按 `page.chars` 顺序排）；
   **连续一整串**（公式碎片 / 引用编号 "[12]"）保持原来的整体 `[?]`
   —— 折叠成一块，文本不膨胀、模型也不会被灌几百个标记；
2. 服务端按坐标裁出字形 → 归一化成 16×16 二值位图 → 当作"形状指纹"；
3. 词典（`logs/glyph_dict.json`，坚果云同步、两台设备共用）里有的直接用；
4. 没有的拼成小图 → 多模态认 → **白名单校验** → 存进词典；
5. 认不出来的退回 `[?]`，**绝不猜**。

**关键坑**：
- **PyMuPDF 的 Rect 是左上角原点**（和 pdfplumber 一样），按 PDF 原生的
  左下角去换算会裁到**旁边的文字**（实测裁出 "va" / "se"，模型把乱图认成 ν）。
- **认字必须关思考**：主模型是推理型，思考会把 token 预算吃光
  （实测 max_tokens=300 → 输出被截断、0 字符）。
- **形状指纹不能用精确哈希**：同一个字形在不同字号下，归一化后的采样点
  位置会差几位（实测同一符号 zoom 12 vs 20 差 26 位）→ 词典永远不命中。
  改用**汉明距离容差匹配**（阈值 40；不同符号之间差 75~105 位，安全）。
- **行匹配不能按 top 比**：数学公式的上下标高度差很多，按 top 比一个都
  匹配不上 → 判据改成"落在行的垂直带内"。
- **plain 兜底路径**（单栏数学页分栏失败时走的那条）也得走行级抽取，
  否则没有行级几何 → cid 对不回字形 → 整页被当坏公式丢掉。
- **坏公式判定要逐行做**：`_is_mathish_line` 对超过 200 字符的整段直接返回
  False，而公式块往往就是一大段 → 逐行判才会把公式整段换成 ⟦FORMULA⟧。

**通用教训**：
1. **文本层解不出来 ≠ 图里没有**。先看字形在不在 PDF 里，在就裁图让视觉模型认。
2. **"给不了答案"要分两档**：能恢复的（认字）就恢复；恢复不了的（公式碎片）
   才占位。**不能一个 "[?]" 了事** —— 用户读到的是乱码。
3. **认出来的东西必须过白名单**：词典是最后一道闸，模型说了不该收的字符就丢掉。
4. ⚠️ **但白名单也会把「正确答案」挡在门外**（2026-10-26）：
   括号类 `()[]{}|` 原本**不在**白名单 → 模型认对了也被丢 → 译文里还是 `[?]`。
   → 白名单是"模型说错就丢"的闸门，不该挡正确结果。认字前先核对：
   **常见的数学符号有没有被漏掉**。

### 4.24 打开/上传论文卡 8 分钟：符号认字必须「整篇一次 + 打开不调模型」（2026-10-25 晚）

用户原话："上传本地pdf下面只是显示正在抓取并解析，然后半天没反应，
从论文库里打开也是半天没反应。"

> 详细取证与交接见 [`docs/HANDOFF_2026-10-25-GLYPH-PERF.md`](docs/HANDOFF_2026-10-25-GLYPH-PERF.md)

**三条根因叠加**（都做了实测，不是猜的）：

1. **逐页串行认字**（主因）：`upload_pdf` / `open_vault_pdf` 对**每一页**调一次认字，
   每页一次多模态 HTTP 请求。实测单次 29~48s → 36 页的论文 = 35 次**串行**
   = **8 分钟仍未返回**。
2. **形状指纹词典永远命不中**（还带误判风险）：全库 24 篇 / 6184 个字形实例实测 ——
   同符号跨实例汉明差**中位 61** vs 异符号**中位 74** → 两者重叠 →
   阈值 40 既命不中，又会把 ≈ 认成 ≥（**异符号误判率 26~41%**）。
3. **认字失败触发全局降格**，连累用户正在跑的翻译（日志里满屏 `no_output` 降格）。

**修法**：
- **语义键**（`字体基名|字符码`，去掉 `XXXXXX+` 子集前缀）当词典主键 ——
  全库只有 **101** 个不同语义键（对比 6184 个实例），**76 个跨文档重复**
  → 认一次长期命中，跨论文跨设备复用。
- 形状指纹**降级为二级校验**（`_shape_bits_stable` 等比补白版，阈值 96）——
  形状区分不开不同符号（异符号中位 47），只能挡"极端不像"。
- **整篇一次**解析（`resolve_document_glyphs`）取代逐页调用。
- **打开/上传不调模型**（`recognize=False`）：亚秒级返回，符号认不出显示 `[?]`
  完全可接受；认字挪到**后台补齐任务**（`isolated=True` → `llm.chat_isolated`，
  只打主模型、**不改全局降格状态**）。
- 回填逻辑移进 `finally`：任何异常路径 `⟦G:n⟧` 都不外泄进 prompt（§4.6 红线）。

**实测**：36 页综述 **8 分钟未返回 → 5.5s**。全量测试 393 passed / 6 skipped。

**通用教训**：
1. **"打开一篇论文"是交互动作，不该被"锦上添花"阻塞**。
   符号认不出来显示 `[?]` 可接受，让用户等 8 分钟不可接受。
   → 打开路径只查缓存（零模型调用），认字放后台。
2. **后台子任务不许走会改全局状态的调用路径**。
   符号认字失败 → `chat()` 会 `mark_degraded(kind=no_output)` →
   用户正在跑的翻译被迫改走备用模型 60 秒。
   → 后台任务走 `llm.chat_isolated()`（不改全局状态）。
3. **缓存的键要用「语义键」，不要用「像素指纹」**。
   像素指纹跨渲染差异太大会永远命不中；语义键（字体+字符码）天然稳定。
   判据：拿**真实产物**量"同键的差异" vs "异键的差异"，两者必须能分开。
4. **拿旧缓存推新结论前必须肉眼核对**。
   我用旧形状词典推语义键 → 20 个不同键全映射成 `÷`，
   肉眼一看出错（`MathematicalPi-Three|16` 明明是 `)`）→ 全部回滚。
   **缓存里的旧数据可能本身就是脏的**。
5. **回填/清理逻辑要放在 `finally`**，不是 `try` 尾部 ——
   否则中途异常 → 内部标记外泄 → 模型照抄/瞎编。

### 4.25 符号认字裁图带邻居 + 提示词要给候选清单（2026-10-26）

接着 §4.24 往下做：把"认不出来的符号"从 1226 处降到 303 处（-75%）。

**① 裁图的外扩量（padding）必须小**
窄字形只有 **2~3pt 宽**，`_render_glyph` 原来外扩 ±1.0pt
→ 把**相邻字符**裁进来 → 模型看到的是"ω𝑓"这种混合图 → 认不出/认错。

实测（2008-JAP 综述 46 个语义键，判据=别的字形有多少面积落进裁剪框）：

| padding | 混入邻居的字形数 | 平均混入数 |
|---|---|---|
| 1.0（旧） | 14 / 46 | 0.35 |
| **0.25（现用）** | **9 / 46** | **0.24** |
| 0.10 | 7 / 46 | 0.17 |

→ `_GLYPH_PAD_PT = 0.25`（接近最小混入，又留余量防切字形：
实测 pad=0 时墨迹左溢 ≤0.75pt，是抗锯齿边缘）。

**⚠️ 改了 padding 必须重算词典里的形状基准** —— 渲染变了 → 指纹变了 →
`get_verified()` 会把旧条目全判为"形状对不上"而**全部误杀**。

**② 认字提示词要给「候选符号清单」**
只说"这是数学符号"时，模型对**括号/竖线**特别不自信 → 大量回答 `?`
（实测 22 个语义键 → 22 个全是 `?`）。
给了候选清单后：**认出数 7/71 → 40/71**，括号类全部正确
（`( ) [ ] { } | ⌈ ⌉ ⌊`）。

**⚠️ 清单里的每个符号都必须在白名单里** —— 否则模型选了一个不在白名单的
符号 → `put()` 丢弃 → 那次调用白费（实测模型爱给 `≧`、`⋗`、`┌` 这类）。
有测试钉死这条：`tests/test_glyph_padding.py::test_hint_prompt_symbols_are_all_whitelisted`。

**③ 白名单原先漏了括号类** —— `()[]{}|‖⌈⌉⌊⌋` 与 `≧≪≫` 已补上。

**通用教训**：
1. **给模型一份"候选清单"比空喊"这是什么"有效得多** ——
   尤其是它没把握的类别（括号、竖线）。清单还能顺便**限制输出空间**
   （清单里的都合法 → 认错也不会污染）。
2. **裁图的外扩量要按"最窄的目标"来定**，不是按"看起来清楚"来定。
   窄字形（2~3pt）是极端情况，也是最容易被污染的。
3. **改了渲染参数 → 所有基于渲染的缓存都要重算**。
   否则旧条目全部失效，而且**症状是"认不出来"**，很容易误以为是模型不行。

**附：调试时踩的两个假象**

1. 上游对 data URL 格式错误会返回
   `HTTP 400: invalid param: image data url missing comma separator (2013)`。
   我一度以为"单张图会失败、批量图正常"是生产 bug ——
   其实是我**调试脚本**里手写漏了逗号（`"data:image/png;base64;"`）。
   生产代码是对的。
   → **排查线上问题时，先确认"最小复现脚本"本身是对的**，
   别急着改生产代码。

2. ⚠️ **"每个符号单独发一张图"会大幅降低识别准确率** ——
   实测 31 个符号单独认 → 20 个有答案，但 **18 个都是 `|`**（全是瞎猜）。
   肉眼核对：字形明明是 `\` `+` `e` `k` `①`，全标成了 `|`。
   原因：`_compose_strip([("1", png)])` 生成的图是 320×64，
   字形只占中间一小块 → 模型看到的是"极扁窄条"，分不出细节。
   而**批量**（20 个格子）时每格内容更饱满 → 反而认对了。
   → **认字的图必须给足上下文**：拼图的格子 + 留白都要够。
   单张窄条图不可靠，认出来的结果**必须肉眼核对**再入库。
   （这 20 条错误已全部回滚，词典回到 70 条可信条目。）

### 4.23 浏览器实测：MCP 浏览器工具点不动这个页面，改用 Edge + CDP（2026-10-25）

用户要求「所有功能都要用浏览器当真人走一遍」（重要的事说三遍）。实测下来：

**MCP 内置 Browser 工具在这个页面上 click 不可用**，两个坑：
1. **后台标签页鼠标事件不投递**：`hover` 返回 `hovered:false`、
   `click` 返回 success 但事件根本没到元素（inline onclick 全都不触发，
   console 里连一条报错都没有）。
2. **页面持续重绘 → 稳定性检查永远超时**：PDF.js 渲染 + 译文流式渲染让元素
   的 bounding box 一直在动，`click` 每次耗时恰好 5.5 秒然后放弃。
   ⚠️ `click` 返回 `success: true` **不代表事件真的派发了** —— 必须用
   "点击后 DOM 有没有变"来验证，不能看返回值。

**可靠的替代方案：Edge + CDP（Chrome DevTools Protocol）**，本机 Edge 自带
`--remote-debugging-port`，不需要装 Playwright/Puppeteer（那要下 ~150MB
浏览器，本机网络不稳）：
- `Runtime.evaluate` → 精确执行页面 JS（定位元素、读状态）
- `Input.dispatchMouseEvent` → **真人级鼠标事件**（不是直接调函数）
- `Page.captureScreenshot` → 截图取证
- 关键：**`Target.activateTarget` 激活标签页**，否则后台标签的 rAF/定时器被
  Chrome 节流，页面加载慢到无法接受。
- `websocket-client` 装一下即可（`pip install websocket-client`，几十 KB）。

**自己写 CDP 脚本时踩的三个坑**：
1. **`offsetParent !== null` 不能用来判可见性** —— `position:fixed` 元素的
   `offsetParent` **恒为 null** → 浮动按钮会被误判成"不可见"。
   改用 `getBoundingClientRect()` 宽高 + `getComputedStyle` 的
   display/visibility/opacity。
2. **等 modal 打开要等 `getComputedStyle(#reader-modal).display !== 'none'`**，
   不能只看"内容注入了没有" —— 内容注入 ≠ 可见（内部元素宽高为 0，
   照样点不到）。实测 `open_vault_pdf` 首开要 40+ 秒（要抽取全文）。
3. **`?open_pdf=` 前面不能多写斜杠** —— 写成 `?open_pdf=/01_Literature/...`
   会找不到文件，modal 永远不打开（白等 5 分钟）。

**通用教训**：
1. **自动化工具"点了"不等于"点到了"**。判断点击是否生效要看**状态变化**
   （DOM / 网络请求 / 截图），不能信工具返回的 success。
2. **工具失灵时先怀疑工具，别急着改产品代码**。先做一个对照实验
   （在稳定页面 vs 复杂页面上点同一个按钮），把"应用有 bug"和"工具有 bug"
   分开，否则会去改本来正确的代码。
3. **能自动化的交互一定要自己跑一遍**。本轮就靠真人点击抓出了三个真 bug：
   标题被挤成竖排、进度徽标吃掉模型徽标、429 报错甩英文机器话。

### 4.24 「隐藏整块」会连带藏掉刚加进去的东西（2026-10-25）

**现象**：AI 活动可视化（工具栏徽标 + 浮层面板 + 每任务可取消）功能做完了，
代码也在、DOM 也在、徽标文本还会在更新（"AI 运行中 10s"）—— 但**用户根本
看不见**。

**根因**：`#ai-activity-pill`（徽标）被放在 `.reader-topbar` 里，而
**PDF 双栏模式下 `.reader-topbar` 是 `display:none`**（PDF 模式要全屏分屏，
不需要章节视图那套工具栏）→ 徽标被连带隐藏 → rect 宽高都是 0 →
用户点不开浮层 → **「✕ 取消」永远够不着**。

也就是说：整个"让用户看得见 AI 在干什么、并且能取消"的功能，
**在主使用场景（PDF 双栏）下等于没有**。

**修法**（三处一起）：
1. `#ai-panel` 和 `#ai-activity-pill` 一起移到 `#reader-modal` 顶层
   （与 `.reader-shell` 平级），`position: fixed` → 不受 topbar 隐藏影响；
2. 徽标改成 fixed 定位（`right: var(--s-5); top: 10px`），两种模式都看得见；
3. **有任务时自动展开浮层**（`_aiPanelAutoOpened` 标志位，任务结束后复位）：
   徽标只是入口，用户不会想到去点它；"正在跑 + 跑了多久 + 能取消"是用户
   此刻最需要的信息。

**通用教训**：
1. **新加的元素不要塞进一块"按场景会整块隐藏"的容器里**。要么放外面，
   要么确认所有场景下它都可见。
2. **DOM 里有内容 ≠ 用户看得见**。判据要用**渲染后的尺寸**
   （`getBoundingClientRect().width > 0`），不能只看"元素存在"或
   "innerText 有内容"。
3. **CSS display:none 的祖先会连坐子元素**：子元素自己的
   `getComputedStyle().display` 仍然是 `flex`，但 rect 是 0 —— 只看
   computed display 会误判成"可见"。

### 4.25 用 `nth-child` 定位 = 把"位置"变成了隐式契约（2026-10-25）

**症状**：PDF 双栏模式下，**整条工具条消失**（缩放 / 全篇重译 / 导出 /
同步 / 模型下拉框全都不见了），而且章节视图区**叠在 PDF 上面**；
切回章节视图才看得到工具条。

**根因**：`switchReaderMode()` 原来这样定位章节视图区：

```js
const sectionView = document.querySelector("#reader-modal > div > div:nth-child(2)");
```

但 `nth-child(2)` 那个位置**不是**章节视图区，是 `.reader-topbar`
（child 1）。于是切到 PDF 模式时：

- `sectionView.style.display = "none"` → **藏掉的是 topbar**
  （→ PDF 模式那条工具条整条消失）；
- 真正的章节视图区 `.reader-body` **从来没被藏过**
  → 章节视图继续盖在 PDF 上面。

**修法**：用 id 定位，与子元素顺序彻底解耦。

```js
const sectionView = document.getElementById("reader-body");
```

**为什么这个 bug 藏了这么久**：页面里本来就写着结构契约注释
（"The inner wrapper must keep exactly this child order…"），
但**契约只约束了"别重排"，没约束"别用位置去定位"** ——
顺序一改（哪怕只是加一个浮动元素），指向就悄悄变了。

**通用教训**：
1. **能用 id 定位就别用 `nth-child`**。位置型选择器把"结构"变成隐式契约，
   任何人加一个元素都会静默改变指向。
2. **要定位就用 id，但要给"顺序敏感"的地方留 id 锚点**
   （本例里 `.reader-body` 之前没有 id，只能拿位置凑）。
3. **"某块内容在某个模式下不见了"要先怀疑"谁在藏它"**：
   用 CDP 量一遍祖先链的 `display` + `getBoundingClientRect()`，
   一眼就能看出是谁把谁藏了（本例：topbar 被藏 → 连带藏掉里面的徽标）。

### 4.26 「降格」不能一律说"额度用完了"（2026-10-25）

用户原话：「M3 额度没耗尽啊，你这肯定又有什么问题出现了」。

**查证结论**：M3 直连 200、12 并发全 200、翻译与答疑都正常 —— **额度确实没问题**。
但界面 toast 写死了「主模型额度用完了 → 已自动降级到免费备用模型」，
**不管真实原因是什么都这么说**。用户看到降格徽标却没法判断自己的额度还有没有，
只能怀疑程序在瞎说。

**降格其实有三种完全不同的原因，处置方式也完全不同**：

| kind | 真实原因 | 处置 |
|---|---|---|
| `quota` | 5 小时滚动窗口的额度真的用完 | 等窗口恢复 |
| `rate_limit` | 每分钟请求数超限 | 等几十秒 |
| `no_output` | 主模型连上了但这次没吐出正文（思考烧光预算） | 跟额度无关 |

**两个判定漏洞（这才是"额度明明够却显示降格"的机制性原因）**：

1. **流式路径的宽松分支**：
   ```python
   quota_hit = (is_quota_exhausted_error(status, err_msg) if status is not None
                else ("429" in err_msg or "quota" in err_msg.lower()))
   ```
   `status` 为 None 表示这不是 HTTP 状态码错误（网络/协议/URL 解析），
   却被当成配额耗尽 → **全局降格 15 分钟**。
   → 现在 status 为 None 一律不降格；真 429 一定有 status_code，走精确判定。
2. **`no_usable_output` 也触发 `mark_degraded`**：
   「这次没吐出正文」是**单次任务**的问题，却让**后续所有请求**都改走备用模型。
   → 现在改成 **60 秒短 TTL 降格**（`mark_degraded(..., ttl_seconds=)`，
   到期自动恢复主模型）：
   - 为什么不降格：主模型每次都会被重试 2 次（escalation + 抬预算重试），
     每次白烧一次思考额度（实测：连续 3 次调用，主模型每次 2 次）；
   - 为什么不永久降格：一次任务的毛病压 15 分钟太重。
   - 60 秒刚好：这一波走备用模型，下一波自动回主模型。
   - ⚠️ **只有同步路径 `chat()` 有这个分支**；`chat_async()` /
     `chat_stream_async()` 没有（既有不对称）。

**通用教训**：
1. **状态提示必须说真话**。一个写死的错误提示，比没有提示更糟 ——
   用户会按错误提示去排查一个不存在的问题（这次用户以为程序有 bug）。
2. **"降级"是状态，不是一句话**。凡是会改变全局行为的状态，
   都要带**分类**（kind）和**人话原因**（text）一起传给界面。
3. **判定要精确，别用"消息里有没有某个词"这种宽松办法**。
   有结构化信息（HTTP 状态码）就用结构化信息。

### 4.27 脱敏/消毒的盲区都是"实测"出来的（2026-10-25）

安全审计跑了 **24 种密钥形态**，发现原来的 `_redact_secrets` 只挡住 3 种：
JSON 形态 `{"api_key":"<hex>"}` 因为关键词和分隔符之间隔了一个引号
**完全不匹配** → key 明文进 `logs/desktop.log`。

**补齐的形态**：JSON/驼峰变体、access_token/refresh_token、
裸 Authorization（不只认 `Bearer `）、Basic 认证、
URL userinfo（`user:pass@host`，连没有密码的 `user@host` 也会暴露用户名）、
PEM 私钥块、set-cookie。

**两个顺序坑**：
- `Basic` 正则必须排在「关键词 + 分隔符」那条**前面** ——
  否则后者先把 `Basic` 当值替换掉，后面的 base64 载荷就漏了。
- **错误出口和日志出口要一起管**：只洗白日志、界面那条路没洗 = 照样泄密。
  `friendly_error_text` 在**没有 HTTP 码**时（网络/URL 异常）也必须脱敏，
  因为带凭据的 `base_url` 会在异常消息里露出来。

**离线导出器（`export_offline.py`）也要消毒**：
它是把 vault 里的 `.md` 转成**一个独立 HTML 文件**给用户双击打开，
而 `.md` 里的内容可能来自**外部论文原文**（双语精读卡片原样写入）。
Python-Markdown 不是消毒器 → `<script>`/onclick/`javascript:` 全部保留
→ 恶意论文 → 导出物变活代码（`file://` 下能读同目录文件）。
→ 自研白名单消毒器（不引第三方库：跨设备同步，一方少装包就起不来）。

**通用教训**：
1. **脱敏正则要按"实际会出现的形态"列，而不是按"我以为的形态"列**。
   跑一遍真实语料（24 种）再写规则。
2. **写盘前先问：这份内容会到别人手里吗**（日志 / 导出物 / 前端）。
   三条路都要过同一道闸。

### 4.28 自己写的消毒器也会被绕过（2026-10-25，二轮审查实测）

第一版白名单消毒器（`export_offline.sanitize_html`）自认为够用，
**独立审查用真实 Edge 实测打穿了**：

```
markdown_to_html("<xmp><img src=x onerror=alert(1)></xmp>")
  → '<p><img src=x onerror=alert(1)></p>'      ← 存活
  → 真实 Edge 打开导出物：window.__XSS_FIRED === True（加载即执行）
```

**根因**：Python 3.13 的 `HTMLParser` 把
`xmp/iframe/noembed/noframes` 当 **CDATA 容器**、`textarea/title` 当
**RCDATA 容器** → 进入容器后，里面的内容被当**纯文本**交给 `handle_data`
→ 而我第一版的 `handle_data` 是 `self.out.append(data)`（原样透传）
→ 标签被丢掉（不在白名单）、内容却原样留下 → **绕过全部过滤**。

**修法**（一个改动同时修掉绕过和实体丢失）：
```python
def handle_data(self, data):
    if self.skip_depth: return
    self.out.append(_html_escape_text(data))   # ★ 数据就是文本，必须转义
def handle_entityref(self, name):              # ★ 不实现就会把
    if not self.skip_depth: self.out.append(f"&{name};")   #   AT&T 变成 ATT
def handle_charref(self, name):
    if not self.skip_depth: self.out.append(f"&#{name};")
```
另外把 `xmp/noembed/noframes/plaintext/title` 加进 `_DROP_TAGS`（双保险）。

**通用教训**：
1. **"不在白名单就丢掉标签"不等于安全** —— 解析器可能把内容当**纯文本**
   交给你，而你如果不转义就等于放行。**白名单必须作用在"文本"上**。
2. **解析器差异要实测**：Python `HTMLParser` 与浏览器 HTML5 解析规则不同，
   容器标签（CDATA/RCDATA）是经典差异点。护栏要按"浏览器能不能执行"写，
   不是按"字符串里有没有 `<script>`"写。
3. **验证要端到端**：造**真实结构的导出物** → 真实浏览器打开 → 查
   `window.__XSS_FIRED`。只测 `sanitize_html()` 的返回值会漏掉
   "innerHTML 里事件属性照样执行"这类问题（`<script>` 在 innerHTML 里
   不执行，但 `onerror` 会）。

---

### 4.29 平板端是另一套前端：它的 HTML 转义从上线起就是坏的（2026-10-25）

**背景**：桌面端 `microbench/static/index.html` 与平板端
`microbench/static/tablet/app.js` 是**两份独立前端**（交接文档反复强调
「桌面的修复一行都不过去」）。这轮桌面端做了安全修复（白名单消毒器、
密钥脱敏、429 人话化），**平板端一行都不过去** → 平板必须自己实现、自己测。

**实测挖出两个真 bug**（不是「潜在风险」，是能执行的）：

#### ① `esc()` 的 `<` / `>` 转义从上线起就是坏的

```js
const E_LT = String.fromCharCode(60);      // ← 这就是字符 '<' 本身
const esc = (s) => s
  .replace(/&/g, E_AMP + "amp;")            // ✅ "&" -> "&amp;"
  .replace(/</g, E_LT + "lt;")              // ❌ "<" -> "<lt;"（不是 "&lt;"！）
  .replace(/>/g, E_GT + "gt;")              // ❌ ">" -> ">gt;"
  .replace(/"/g, E_AMP + "quot;");         // ✅
```

`E_LT` 是**字符 `<` 本身** —— `E_LT + "lt;"` 产出的是 `<lt;`，
**等于什么都没转义**。而当时**只修了引号那一处**
（`E_QUOT + "quot;"` → `"quot;"`，那次确实修对了），`<` / `>` 一直漏着。

**后果**：
- 代码块（`<pre><code>`）走的是 `esc()` 而不是消毒器 →
  笔记里的 `<script>` **会真的执行**；
- 消毒器的「不在白名单 → 当普通文本显示」分支写成
  `m.replace(/</g, E_LT)` → **空操作** →
  白名单外的标签（`details/ontoggle`、`marquee/onstart`…）
  原样进 DOM，事件属性照样触发。

**修法**：三个都要用 `E_AMP`（`&`）；并**把 `E_LT/E_GT/E_QUOT` 三个常量删掉** ——
它们是错的写法的**原料**，留着就还会有人拼错。

> **通用教训**：
> 1. **「修好了一个字符类」不等于「转义是对的」** —— 同一个函数里
>    四个替换用了两种前缀，只发现一个拼错就只改一个 → 其余三个继续漏。
>    写转义函数时**一次把四个都统一**，并且给每个都写断言。
> 2. **转义函数要有「反向」测试**：断言 `esc("<") === "&lt;"`，
>    而不是只断言「输出里没有 `<script>`」（`&lt;script&gt;` 里也有 `script`）。
> 3. **码点常量这种「防 HTML 反转义」的写法，本身就是坑** ——
>    `String.fromCharCode(60)` 是 `<`，`+ "lt;"` 拼不出 `&lt;`；
>    真正需要的是**码点 38**（`&`）。**常量越少越安全。**

#### ② 未闭合的标签整段原样保留

消毒器的标签正则要求必须有 `>`：
```js
/<\/?([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g
```
但**浏览器会在 EOF 处隐式闭合标签** ——
`<img src=x onerror=alert(1)`（**没有 `>`**）照样会变成一个带
`onerror` 的 img 元素并触发。正则没匹配上 → 整段原样保留 → **XSS**。

**修法**：结尾改成 `(>|$)`，未闭合的**整段当纯文本**
（只转义开头的 `<`：既堵住执行，又不丢后面的内容）。

> **通用教训**：**正则消毒器要按「浏览器怎么解析」写，不是按「字符串长什么样」写。**
> 判定「这段会不会变成活标签」要模拟浏览器的宽松规则：
> `<` + 字母 → 标签开始；标签没被 `>` 闭合 → 浏览器在 EOF 处闭合它。
> 桌面端 §4.28 那条（CDATA/RCDATA 容器）是同一族问题的另一面。

#### 平板端自查清单（交接文档 §8）—— 逐条结果

| 项 | 平板端 | 结论 |
|---|---|---|
| §4 429 人话化 | ❌ 原来直接甩英文原文 | ✅ 补 `friendlyHttpError()`（限流 vs 配额分开说）|
| §8.1 密钥脱敏 | ⚠ key 存在 **IndexedDB** `meta/ai`（同源脚本读得到）→ 一条能执行的 XSS 就等于密钥外泄 | ✅ 补 `safeErr()`（抹 URL userinfo / key / 绝对路径）|
| §8.2 错误出口也要洗 | ❌ 上游报文直接进 toast | ✅ 全部走 `safeErr()` |
| §8.3 离线导出 XSS | ⚠ 平板**另一套** JS 消毒器，零载荷测试 | ✅ 新增 `_test_tablet_xss.js`（97 条断言）|
| §8.4 GET 带写副作用 | ✅ 原生侧 GET 只读（PROPFIND / GET），写走 JS 桥 | 无需改 |
| §8.5 内部路径泄露 | ✅ 原生异常只带 vault 相对路径，不含凭据 | 已在 JS 侧再抹一道 |

#### 载荷测试怎么写（平板端专用）

`microbench/_test_tablet_xss.js`：
- **切发布出去的源码**（`sliceBlock()`），不 `require` 整个模块 ——
  `require()` 测的是「模块跑起来之后」，代码一坏往往先在 export 上炸掉，
  测试变成「函数不存在」的崩溃，**证明不了断言能抓 bug**；
- 判定用**浏览器式宽松解析**（找出会变成真标签的构造），
  不是 `/<script/i.test(out)`（`&lt;script&gt;` 里也有 `script`）；
- 切片分成**核心段必需 / 护栏段可选** → 旧代码能**干净判红**而不是崩掉；
- **同时断言正常内容没被误伤**：表格 / 代码块 / `class="katex"` /
  `AT&T` 实体 / **vault 里真实在用的那批标签**
  （`br p strong div details b summary h2 h3 h4` ——
  转义修对之后，不在白名单的标签会变成字面文本，
  白名单漏一个就等于**把现有笔记搞花**）。

**反向验证（必做）**：
```powershell
git stash push -- microbench/static/tablet/app.js
node microbench/_test_tablet_xss.js     # 必须变红！
git stash pop
```
实测：旧代码 **PASS 59 / FAIL 30**（exit 1），新代码 **PASS 97 / FAIL 0**。

---

### 4.42 性能：打开论文 2.5~9.7 秒 → 50 毫秒（2026-10-27）

用户原话："我发现在电脑上打开略微有些卡顿"。

**这不是模糊的"感觉慢"** —— 实测（Edge + CDP）钉死了三个可量化的问题：

| # | 现象 | 根因 | 修法 | 效果 |
|---|---|---|---|---|
| ① | 打开一篇论文等 2.5~9.7 秒 | pdfplumber **每次都重解析整篇 PDF** | 磁盘缓存（键含版本） | **2496ms → 50ms** |
| ② | 首屏请求排成两批 | `async def` 里调**同步磁盘扫** → 阻塞事件循环 | 端点改同步 `def` | 6 个请求全并行 |
| ③ | 滚动掉帧 | 滚动监听器**没节流** | rAF 节流 + 状态没变就不写 | 帧 max 391ms → 23ms |

**① 的关键教训：三条路试过全证伪**

| 思路 | 实测 | 为什么不行 |
|---|---|---|
| 线程池并行抽页 | 4 workers **0.97x**，8 workers **0.74x** | GIL + pdfplumber 内部锁 |
| 换 PyMuPDF | 快 **20.6x** | ❌ 破坏双栏顺序（§4.5）+ **丢符号标记** |
| 减少 pdfplumber 调用次数 | 共用同一份 chars 缓存 | 本来就没重复解析 |

→ **性能优化不能拿正确性换**：换 PyMuPDF 是**负优化**。

**缓存键必须含三样**（缺一个就缓存出脏数据）：
PDF 的 `(mtime_ns, size)` + `EXTRACTOR_VERSION` + **符号词典的 `(mtime_ns, size)`**
（词典认出新符号 → 文本里 `⟦G:n⟧` 对应的字符就变了 → 必须重解）。

**缓存放 `logs/`（不在同步根）**：同步根是两台设备的**数据面**，
缓存写坏 → 两台设备正文对不上且极难查。缓存是**性能**优化不是数据 → 各自缓存各自快。

护栏：`tests/test_pdf_cache.py`（12 条）。

**② 同步磁盘扫的端点一律用同步 `def`**（FastAPI 丢线程池，不冻结事件循环）——
`/api/stats` `/api/vault/literature` `/api/paper/list_vault_pdfs` `/api/paper/folders`
（跟 `api_fetch_paper` 同一个理由）。

**③ 滚动监听器必须节流**：原来挂在 `window` 上每个 scroll 事件都写 DOM
→ CPU Profiler 显示它是首屏**唯一**热点（7% 采样）。

> **通用纪律**：
> - **先定位根因，不要猜** —— 用户说"卡"时，正确反应是**加探针 + 实测**。
>   本轮的工具：Edge `--headless=new` + CDP（拿 Navigation Timing / 长任务 /
>   CPU Profiler 热点）+ **在页面脚本之前注入 fetch 包装器**（拿到每个请求的
>   发起时刻 → 能看出串行还是并行）。
> - **优化前先量"瓶颈在哪一层"** —— 三条路全试过全证伪，才找到唯一有效的方向。
> - **同样的"同步磁盘 IO"要一起查** —— 一个端点 async 化时会漏，
>   四个端点同时中招（首屏并发被排成串行）。

### 4.44 平板答疑的原生代理超时比答疑时限还短（2026-10-27 独立审查）

**APK 上 300 秒的答疑超时永远触发不了**：

- `app.js` 的 `AI_QA_TIMEOUT_MS = 300000`（对齐电脑端 `QA_TIMEOUT`）
- 但 `nativeCall()` 原来所有原生调用共用 **150s** 兜底超时
  → 150s 时 nativeCall 先 reject，那个 Error 没有 `.status`
  → 不匹配 400/429 → 直接上屏「原生调用超时（httpPost）」
  → **用户还是什么都没拿到**（正是那轮要消灭的症状）
- Java 侧 `DavBridge.java` 的 `setReadTimeout(120_000)` 更短

→ **原生调用超时必须按方法分别给**：`httpPost` 给 360s（> 答疑时限），
WebDAV 同步保持 150s（同步卡住就该早点失败重试）。

> **通用纪律**：**超时要按「谁在用」分层**，不能一个数打天下。
> AI 请求和文件同步的合理时长差一个数量级 —— 共用一个值，
> 短的那个会把长的那个**静默掐掉**，而且掐掉的还不是最该报错的地方。

### 4.45 平板答疑：429 换模型不能把刚抬上去的预算冲掉（2026-10-27 独立审查）

正文空 → 预算刚从 16000 抬到 **32000** → 重试正好撞 429 → 换模型 →
预算被**赋值覆盖**回 16000 → 若新模型也是强制思考型 → 思考再吃一次预算
→ **又 0 字**。用户白等三倍时间。

→ 429 分支必须用 `Math.max(当前值, 按新模型算的下限)`，不能用赋值。

### 4.46 平板答疑：未知网关不要乱发可选参数（2026-10-27 独立审查）

平板的用户可以把自己的接口指向**任意** OpenAI 兼容网关 ——
那种网关上照样挂着「MiniMax-M3.1-Flash」这个模型名。

| 参数 | 判错的后果 | 修法 |
|---|---|---|
| `thinking:{"type":"disabled"}` | 严格网关直接 **400** | **白名单**：只有实测支持的 provider 才发 |
| `reasoning_effort` | **400** | **精确主机名**白名单（子串匹配会误命中中转站） |

且 400 兜底**不能依赖错误文案**：上游可能说 "unrecognized request argument"，
也可能把参数名写在第 200 字之后 → 判据改成「**请求带了可选参数 + 400**」。

> **通用纪律**：**可选参数（opt-in）要按「最严格的假设」发**：
> 未知网关一律不发；发了被拒了要能**无文案依赖**地自愈。

### 4.47 平板端补齐答疑「只思考不回答」（2026-10-27）

电脑端 `2216afa` 修了答疑的三个根因（§4.37~4.39），
但**平板端是另一套前端**（`microbench/static/tablet/app.js`，**直连模型**，
不走后端提示词、不走 `llm.py`）→ 桌面的修复**一行都不过去**。

上游那次只在平板补了一个提示词（`SYS_RESEARCH`），
**另外三个根因在平板端原样还在**：

| # | 桌面端修的 | 平板端原来 | 平板端现在 |
|---|---|---|---|
| ① | 思考预算按「思考+正文」算 | `max_tokens: 8000` → **正文 0 字** | `AI_QA_MAX_TOKENS = 16000` |
| ② | 空正文自动加预算重试 | 只弹一句提示 → **用户什么都没等到** | 自动重试（抬预算，上限 32000） |
| ③ | 重答不死锁 + 序号守卫 | 无守卫 → 旧请求把**新请求的停止按钮**藏掉 | `aiRequestSeq` + `isCurrent()` |
| ④ | 思考强度 `reasoning_effort` | **完全没这个参数** | `low`（保守判定，见下） |
| ⑤ | 能力边界写进三个提示词 | 只有 `SYS_RESEARCH` 有 | `SYS_SUMMARY` 补齐（逐字一致） |

> **⚠ 平板的 `reasoning_effort` 判定只看网关地址（base），不看模型名**
> —— 这跟 `llm.py`（base + 模型名一起判）有一处**故意的**差别：
> 平板的用户可以把自己的接口指向**任意** OpenAI 兼容网关，
> 那种网关上照样挂着「MiniMax-M3.1-Flash」这个模型名，
> 但它**不一定认 `reasoning_effort`** → 发了就 **HTTP 400**（请求直接被拒）。
> → **网关才是决定「认不认这个参数」的东西，模型名不是。**

**顺带修的隐患**：`aiRequest()` 原来读**全局** `aiAbort.signal`，
而全局变量会被下一次请求换掉 → 旧请求的响应会被当成新请求的结果。
→ signal 改成**由调用方传入**。

**翻译的预算不能照抄答疑的**：
翻译是「关思考的直译型模型」（4000 按正文算），
答疑是「思考型模型」（16000 按思考+正文算）。
平板的翻译模型是用户自己配的 → 配成强制思考模型时**自动抬到 16000**。

护栏：行为级 `microbench/_test_tablet_ai.js`（+22 条，累计 67）
+ 源码级 `tests/test_tablet_qa_budget.py`（18 条，新增）。

> **通用纪律**：
> - **「桌面的修复不过去」这句话要当真** —— 同一个坑要在**同一层所有入口**一起修。
>   上游那轮只抄了提示词，预算和死锁那两条用户照样踩得到。
> - **抄的时候要连「为什么这么判」一起抄** —— 只抄数值不抄判据，
>   下次环境一变（换个网关、换个模型）就会翻车。
> - **两个端对同一机制的判定可以有意不同** —— 只要**理由**写进注释，
>   并且各自有护栏钉住，就不算分歧，算「按各自的部署环境取证」。

### 4.48 `.reader-shell` 会被 flex 收缩成半屏 —— fit-content 的坑（2026-10-27 浏览器实测）

**症状**：专注模式单栏看论文页，PDF 只占左半屏，右半屏全空。

**根因（探针实测，不是猜的）**：

    .reader-shell  cw=616   ← 视口 1280px，它只占了一半
    .pdf-split     gtc="308px 308px"

`#reader-modal` 打开时是 `display:flex`（**行向** flex），
`.reader-shell` 是它的 flex item，`width:auto` → 走 **fit-content 收缩**：
壳的宽度 = 内容的 max-content，**不是视口宽度**。

平时看不出问题：顶栏在文档流里、内容很宽 → 壳被撑满。
**一旦顶栏脱离文档流**（专注模式把顶栏改成 `position:fixed`），
壳里只剩 `.pdf-split` → 壳立刻缩成 fit-content → 半屏。

**修**：`.reader-shell { width:100%; min-width:0 }`（`min-width:0` 同理 ——
否则 min-content 撑大时 flex item 不肯缩）。

> **教训**：改 flex 容器的子项宽度前，先问「它是不是某个 flex item」。
> `width:auto` 在 flex item 上 = fit-content，不是「自适应父宽」。
> 交叉轴上给 `margin:auto` 还会**取消 stretch** → 同样变成 fit-content
> （§4.12 里 `@media (700-1100px) .reader-shell > * { max-width:860px; margin:auto }`
> 就是这一类，专注模式要把它顶掉）。

> **教训 2**：症状「PDF 只占一半」有两个候选根因（fit-content 收缩 / media query 的 max-width）。
> **别按屏幕宽度猜** —— 之前修的是 `700-1100px` 那条 media query，
> 在 1280 屏上**根本不生效**，所以症状一点没变。
> 正确做法是**埋探针量实际宽度**（`getComputedStyle` + `clientWidth`），
> 一测就分清了。

### 4.49 PDF 画布宽度：阅读器从隐藏变可见时，窗口尺寸**一点没变**（2026-10-27 浏览器实测）

**症状**：画布永远 520px 宽（`availWidth = max(260, (listEl.clientWidth || 520) - 4)` 的兜底值）。

**根因**：阅读器打开前是 `display:none` → `#pdf-pages-list` 的 clientWidth = 0
→ 首次渲染走兜底 520。等阅读器**真的显示出来**，
**窗口尺寸一点没变** → `window resize` **根本不触发** → 画布永远停在 520。

**修**（三处一起，缺一不可）：

1. `ResizeObserver` **直接观察 `#pdf-pages-list`** —— 覆盖「隐藏→可见」「单栏→双栏」
   这些 window resize 抓不到的宽度变化；
2. `_pdfRelayoutBusy` **忙标记** —— 重绘期间忽略尺寸回调，
   否则「清空列表 → 高度变化 → 又排一次重绘」会**自激循环**；
3. 渲染结束后用**渲染起始宽度**（`renderW0`）复核一次 ——
   阅读器可能是在渲染**过程中**才变可见的，那一瞬间的观察器回调被忙标记挡掉了。

> **教训**：`|| 520` 这种**兜底默认值**会把「量不到」和「真的是 520」混成同一个值，
> 静默地把 bug 藏起来。量不到的时候要能**发现**（渲染后复核），别只兜底。

### 4.50 专注模式（小屏极简阅读）—— 功能尝试，在分支 `feat/zen-focus-mode`（2026-10-27）

用户诉求：「屏幕不大，所有内容铺在屏幕上很乱、费劲 → 做个极简版本，
只有论文页和译文页，其他功能隐藏，需要时自己打开，滑下来能出现一些功能。
这是功能尝试，审查好当分支推，明天不行要能一键回退。」

交接文档：[`docs/HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md`](docs/HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md)

**设计要点**：

- 工具条**复用既有 `.reader-topbar`**，不另做一套按钮 → 既有功能一行不少，
  也不会和顶栏分叉出两套行为；
- 顶栏在专注模式下是 `position:fixed` + `translateY(-101%)` 滑出，
  **不是 `display:none`** —— 否则再也滑不回来；
- **不给 `.reader-shell` 加 `position:relative`** —— 那会把里面
  `position:absolute` 的浮层（批注编辑器 / 笔记抽屉）的包含块换掉，位置全乱；
- `.zen-handle`（顶部 7px 细把手）放在 `.reader-shell` 的**最后** ——
  插在前面会挤偏「child1=topbar / child2=章节视图 / child3=PDF 视图」的位置型契约；
- 选择器一律 `#reader-modal` 前缀 —— 靠 id 权重压住基础样式里的
  `@media (max-width:1200px) .reader-body` 两列规则；
- 默认关闭 + 偏好持久化（`zenPref`）→ 下次开论文直接进专注。

**这一轮浏览器实测又抓到 3 个真 bug**（都在 §4.48 / §4.49 / 本节）：

| # | 症状 | 根因 | 修法 |
|---|---|---|---|
| 1 | 单栏看论文页，右半屏全空 | `.reader-shell` fit-content 收缩 | §4.48 |
| 2 | 画布停在 520px | 隐藏→可见时窗口没变 | §4.49 |
| 3 | 切「论文页」一片空白 | CSS 无条件 `display:none` | 改成 `:not(.zen-show-orig)` |
| 4 | 「章节」「提问」点了像坏了 | 它们住在章节视图容器里，PDF 模式下整体 `display:none` | 先切章节视图；没章节数据就如实提示 |

> **通用纪律**：**「点了没反应」和「显示成空白」都是 bug，不能算「功能没实现」**。
> 用户分不清「我按错了」和「它坏了」——
> 能切就切，切不了就**说清楚为什么**（弹提示），别给一片空白。

### 4.51 护栏的变异必须改「被调方」，切片必须覆盖被测逻辑（2026-10-27 独立审查）

**教训的来源**：专注模式分支的独立审查（verifier 子代理）用**3 个变异体**
证明，本分支**最核心的修复（PDF 画布重绘链）护栏零覆盖**：

| 变异 | node 护栏 | pytest 护栏 | 后果 |
|---|---|---|---|
| 删掉 `if (!force && …)` 里的 `!force` | **PASS 22 / FAIL 0** | **0 条失败** | force 失效 → 栏宽变化 < 40px 时静默跳过重绘 → 原 bug 复活 |
| 删掉 `if (_pdfIsRelayoutBusy()) return;` | **PASS 22 / FAIL 0** | **0 条失败** | 自激循环保护消失 |
| 删掉渲染后的宽度复核 | **PASS 22 / FAIL 0** | **0 条失败** | 画布永远停在兜底 520px |

**两个根因**：

1. **node 护栏的切片没覆盖被测逻辑本身** ——
   切片只切了「专注模式块」，把重绘链（`_pdfRelayoutIfNeeded` /
   `_pdfIsRelayoutBusy` / `_pdfVerifyRenderedWidth`）**用桩替换掉了**。
   → 桩里当然测不到真逻辑。
2. **变异全都只改「调用方」**（类名字符串、传参），
   **一个都没碰「被调方」**（函数体、守卫、复核）——
   于是「变异捕获 7/7」给出了一种**虚假的覆盖感**：
   最该被守的 4 处代码，恰好是唯一没有变异体的 4 处。

**修法**：

- 切片扩到**被测逻辑的真实所在**（`loadPdfDocument` 之前），
  让重绘链**真代码在跑**（DOM 桩提供可注入的 `clientWidth`）；
- 补 **5 个改「被调方」的变异**（删 `!force` 短路 / 删忙标记守卫 /
  删渲染后复核 / 把 40px 阈值改成 0 / 把切单栏的 force 去掉）→ 现在 **11/11**；
- 把「渲染后复核」抽成**独立可测的小函数** `_pdfVerifyRenderedWidth()`
  —— 埋在 `loadPdfDualColumn` 那个大函数里的逻辑，护栏切片根本够不着。

> **纪律**：
> - **变异要改「被调方」**：只改调用方的变异，证明的是"调用方传对了参数"，
>   **证明不了被调方认这个参数**。
> - **切片要覆盖被测逻辑**：切片边界要在**被测逻辑之外**，
>   否则你测的是一个「长得像但不是它」的东西。
> - **埋在大函数里的逻辑没法被测**：先抽成小函数，再写护栏。

> **顺带**：审查还抓到一个**成本问题** ——
> 进出专注原本会 `force` 全量重绘（每页一次 `await renderPdfPage`），
> **391 页的论文每点一次「专注」就全量重绘一遍**。
> 但进出专注**根本不改变 PDF 栏宽**（`.reader-shell` 恒为 100% 宽，§4.48）→
> `force` 只给「切单栏 / 切论文页」用（那两个确实改变栏宽），
> 其余交给 `ResizeObserver` 按实际宽度差决定。
> 浏览器实测：进出专注前后画布宽度**一模一样**，没有重绘。

> **纪律 2**：**别拿"全局广播事件"当内部调用** ——
> `window.dispatchEvent(new Event("resize"))` 看着方便，
> 但将来任何新增的 resize 监听器都会被误触发。直接调函数。

### 4.41 平板适配：等待期看得见 + 429 自动换模型 + 按模型限流（2026-10-25）

交接文档 [`docs/HANDOFF_2026-10-25-TABLET-ADAPT.md`](docs/HANDOFF_2026-10-25-TABLET-ADAPT.md)
说清了三件「桌面端做了、平板端要自己抄」的事。这台机（30461）就是
**负责平板那侧**的（APK 从这儿打）。

| # | 桌面端做了什么 | 平板要不要抄 | 实际做没做 |
|---|---|---|---|
| ① | 批注重译标「位置失效」 | ❌ 不用（平板批注画在原文坐标上）| 不需要 |
| ② | 标注列表按当前页定位 | ❌ 不用 | 不需要 |
| ③ | AI 活动可视化（已用时/面板/取消）| ✅ **抄最轻的一层** | ✅ 做了「已用时」 |
| ④ | 换模型 + 429 降级 + 按模型限流 | ✅ **抄降级这条** | ✅ 做了 |
| ⑤ | 符号 × ≈ ≥ 裁图认字 | ⚠ 大概率不用（平板走 PDF.js，符号本来就解得出来）| 先实测再决定 |

**① 等待期「已用时」**：平板原来只有一个状态条，结束才更新 →
用户原话「他有可能等了半天然后什么也没输出」。
```js
const t0 = Date.now();
const tick = setInterval(() => {
  if (gotFirstDelta) return;
  const s = Math.floor((Date.now() - t0) / 1000);
  status.textContent = s > 60
    ? "⏳ 还在想… · " + s + "s（可能模型在长思考，可点停止或换模型）"
    : "⏳ 思考中… · " + s + "s";
}, 1000);
// finally 里 clearInterval(tick)
```
**首个增量到达就停表**（`gotFirstDelta`）—— 不然正文已经在滚了还在说「思考中」。

**② 429 自动换模型**：平板**天然就有两个模型配置**
（`cfg.model` 推理 / `cfg.modelT` 翻译快模型）→ 正好是「主/备」。
- 关键：`aiRequest` 抛错时**把 HTTP 状态码挂在 error 上**
  （`err.status = res.status`）——
  靠字符串匹配 `"HTTP 429"` 太脆，人话化文案一改就失效；
- 主模型 429 → **换另一个重试一次** → 只换一次（换完还不行就照实报错，
  别无限重试烧额度）；
- 换模型后**思考开关要重算**（`payload.thinking` 是按主模型定的）；
- 完成的提示要说**实际用的那个**模型名。

**③ 按模型限流**：平板是**直连** LLM API 的（不经过本机服务端），
桌面端 `llm.py` 的限流表**对平板不生效** → 得自己卡：
Agnes（免费版）11 RPM、付费模型 30 RPM。

> **通用教训**：**「桌面的修复不过去」这句话要当真**——
> 两份独立前端里，同一个概念（转义、限流、降级）会各自坏在各自的地方。
> 桌面端那轮安全修复里，**平板端有 3 项完全没覆盖**
> （429 人话化、错误出口脱敏、导出 XSS），
> 而且平板端还额外藏着一个桌面端没有的老 bug（`esc()` 转义）。

---

### 4.31 「接口返回 200」不等于成功：空结果必须当成错误（2026-10-25 平板真机故障）

**故障现场**（用户真机截图）：
- 界面显示「已连接坚果云」「坚果云 WebDAV（真双向）」
- 同步结果是「**已连接 0 篇** · 新增 0 · 更新 0」
- 24 篇原文 PDF **全部 404**（「云盘里没找到这个文件」）
- 文献库还写着「⚠ 还没连接文件夹」
- 用户原话：「连接也是显示正常的…但是它就是同步不了」

**根因**：vault 前缀（「我的坚果云/MicroBench工作台」，**两段**）是空的。
它只有原生设置页的「测试并保存」会写 —— 用户没跑过、或装的是旧版 APK，
就永远是空的。而网页侧的「已连接」**只看账号密码对不对**。

**最阴的一点（以前谁都没想到）**：
前缀错时，PROPFIND **不报 404** —— WebDAV 对「不存在的目录」回的是
**207 Multi-Status + 0 个条目**。
→ 网页看到的是「这个目录是空的」→ 界面显示「已连接 0 篇」，
**一点错都看不出来**。用户只会以为「坚果云还没同步完」，
于是反复重试、反复失败 —— 这就是「卡在第一步，什么都测不了」的原因。

**修法（三层，缺一不可）**：

1. **原生自愈**（`Prefs.resolveRootsIfMissing()` + `DavPathHandler.ensureRoots()`）
   —— vault 前缀没配时**运行时就地探测并存下来**，
   用户不必去翻那个藏得很深的原生设置页。
   探测失败**不置位**（5 分钟后可再试），别让一次网络抖动就永久放弃。
2. **网页不再沉默**（`scanRemote`）：DAV 模式下扫到 **0 个条目**就**报错**
   「云盘里没找到 vault 目录」—— 空结果和成功必须分开。
3. **界面不说谎**：设置页亮出 **vault 的真实位置**；
   文献库改用 `currentBackend()` 判断（DAV 模式下 `state.dirHandle` 永远是 null，
   照旧判断就会一直谎报「还没连接文件夹」）。

**测试**（这次全靠它才发现）：
- `android/_test_dav.py` + `TestDav.java` [14]：拿**真实的 Java 客户端**
  对着 mock WebDAV **复现故障**（列目录返回空 + 下载 404），
  再验证自愈路径（探测 → 设前缀 → 能列能下）。
- `microbench/_test_tablet_dav.js`：网页侧钉住三条护栏。
- `android/_test_apk_contract.py` [20]：钉住原生侧的接线（探测 / 存回 / 只探一次）。

> **通用教训**：
> 1. **「HTTP 200」和「有东西」是两回事** —— 空数组、空列表、
>    0 条目，在 WebDAV / 搜索 / 分页接口里**都是失败**，
>    但很多 API 会用 200 + 空集合来表达「没有」。
>    → **凡是「同步/拉取」的结果是空的，必须当成失败处理**，
>      至少要跟「成功但确实没有」区分开。
> 2. **界面上的一个布尔值会骗人** —— 「已连接」如果只由
>    「账号密码齐了没」推导，那它跟「路径对不对」「能不能读到」毫无关系。
>    → 状态要显示**可核对的细节**（路径、条数），而不只是一个绿灯。
> 3. **静默失败比崩溃更贵** —— 崩溃用户会来问；
>    静默返回空，用户只会自己反复重试，然后说「这东西根本没法用」。
> 4. **配置项缺失要有自愈 + 兜底** —— 凡是「只有某个深层设置页会写」的字段，
>    都得在运行时有探测兜底，否则老用户升级后永远坏着。

### 4.32 兜底要分「没配」和「配错了」两种（2026-10-25 平板真机故障·第二轮）

**上一轮（§4.31）的自愈只兜了一半**：它只在 vault 前缀**是空的**时候探测。
但还有一类**前缀非空、但是错的**：

- 用户在原生设置页**手填错**了；
- 云盘里**改过目录名**（vault 搬家）；
- 用户装的是**探测逻辑有 bug 的旧版 APK**，把错的位置存下来了。

这类情况下 `ensureRoots()` 看到「非空」就直接放行 →
**症状跟空前缀一模一样**（列目录 0 条、PDF 全 404），界面照样显示「已连接 0 篇」。
→ 兜底根本没触发，用户那边还是坏的。

**修法**：兜底要接到「**结果异常**」上，而不是接到「**配置缺失**」上。

```java
// DavPathHandler.java
if (entries.isEmpty()) {                       // 列目录空 → 可能路径错了
    DavClient fixed = tryRedetect(syncRoot);
    if (fixed != null) entries = fixed.list(dir);
}
if (data == null) {                            // 取文件 404 → 可能路径错了
    DavClient fixed = tryRedetect(syncRoot);
    if (fixed != null) data = fixed.get(path);
}
```

**三条配套纪律**：

1. **重探测必须节流**（`PROBE_RETRY_INTERVAL_MS = 5 分钟`）——
   重探测是**递归整个云盘**（好几次 PROPFIND）。
   「每个空目录都探一次」几秒就能把坚果云打到 `503 BlockedTemporarily`。
2. **同步根要一起刷新** —— 同步根（译文快照 / 批注）是 vault 根的**兄弟目录**，
   vault 位置变了它可能跟着变；不刷的话译文还是读不到。
3. **重探到相同位置就放弃**（`v.equals(davVaultRoot())` → 返回 null）——
   位置没变说明不是路径问题，别反复探。

**怎么测「真跑在手机上的那条链路」**：
`DavPathHandler` / `Prefs` 依赖 Android 框架类，普通 JDK 编译不了，
以前只能靠真机点。这次加了三个**桩类**放在 `android/_stub/android/{content,webkit}/`，
让 `android/TestDavPath.java` 能把 `DavPathHandler` **真正跑一遍**（不是模拟）。

> ⚠⚠ **桩类绝对不能挪进 `android/java/`** ——
> `build_apk.ps1` 会把 `android/java` 全编译一遍，桩类跟真 `android.jar` 撞类，
> **APK 直接编不过**。`android/_test_apk_contract.py` [22] 专门钉这一条。

**跨设备脚本别写死本机路径**：`android/_test_dav.py` 原来把 JDK 写死成
「`D:\Desktop\杂物\z\Zotero\jdk`」—— **那是 30461 这台的路径**。
25353 上没有 → javac 失败 → 那个套件红 → **25353 的 auto_sync 永远判定
「测试失败」，再也推不上去**，而且红的原因跟代码无关，最难查。
→ 改成多路查找（`MB_JDK` → `JAVA_HOME` → 常见安装位置 → PATH），
实在找不到就 **SKIP（不算失败）**。

> **通用教训**：
> 1. **兜底的条件要对准「用户看得见的信号」**（结果为空 / 404），
>    而不是「某个配置字段缺失」。配错了和没配是两种故障，症状一样。
> 2. **测试要能反向验证** —— 把修复临时拆掉，测试必须变红。
>    全绿的测试可能只是**根本没测到东西**。
>    （这次把 `tryRedetect` 改成直接 `return null`，
>    `TestDavPath` 立刻 PASS 11 / FAIL 7，红的正是那两条。）
> 3. **「真机上才能测」的代码，也可以用桩类在电脑上测** ——
>    三个几十行的桩，换来一套能进 CI 的回归测试。

### 4.33 按名字猜路径必然撞车：兄弟目录只能靠内容分辨（2026-10-25 平板真机故障·第三轮）

**用户真机截图**（这轮的关键证据）：
> 当前 vault 位置：**我的坚果云/MicroBench同步**

界面把真实路径亮出来了 —— 一眼就看出不对：**那是同步根，不是 vault。**

**真实云盘结构**（本机 Nutstore 实测）：

```
我的坚果云/
  ├── MicroBench工作台/      ← vault（PDF 原文、笔记）
  │     00_Dashboard  01_Literature  02_Reproduction
  │     03_Knowledge  04_Weekly_Reports  _Templates  .obsidian  assets
  └── MicroBench同步/        ← 同步根（译文快照、批注）
        snapshots  annotations  files  progress
```

**两个是同一层的兄弟目录，名字共享 `MicroBench` 前缀。**

老实现只按名字判断（`isLikelyVaultName()`：名字含 "microbench" 或 "工作台" 就算），
而坚果云的 PROPFIND **按名字排序** ——
「同」= U+540C < 「工」= U+5DE5 →
**「MicroBench同步」永远排在「MicroBench工作台」前面 → 永远先被选中。**

→ 这不是偶发，是 **100% 必错**。用户看到的现象全是它的推论：
列目录 0 条、PDF 全 404、界面却显示「已连接」。

**修法：名字只用来"值得一看"，必须验证内容才能认。**

```java
// 名字像的候选 → 验证它里面到底有没有 vault 的标志目录
for (DavEntry e : dirs) {
    if (!isLikelyVaultName(e.name)) continue;
    if (isVaultDir(e.path)) return e.path;     // ← 内容校验
}
```

三条判据要**成对**定义（否则认错方向）：

| 类别 | 标志目录 | 为什么 |
|---|---|---|
| vault | `00_Dashboard` `01_Literature` `02_Reproduction` `03_Knowledge` `04_Weekly_Reports` `_Templates` | 只有 vault 里有 |
| 同步根 | `snapshots` `annotations` | 只有同步根里有 —— **用来排除**，别只判「像 vault」 |

⚠ **同步根里全是 .md**（译文快照就是 .md）→
所以「有没有 .md」**不能**当 vault 的判据；
而且递归进同步根之前必须先按「含 snapshots/annotations」把它挡掉，
否则会返回 `…/MicroBench同步/snapshots` 这种更离谱的路径。

> **通用教训**：
> 1. **「按名字猜路径」在有兄弟目录时必然撞车** ——
>    凡是同一个项目在云盘/文件系统里有多个目录（工作区 vs 缓存 vs 备份），
>    **判据必须是内容**，不是名字。名字只能用来缩小搜索范围。
> 2. **mock 太善良 = 假绿** ——
>    这次的 mock 树里**只有 vault、没有同步根**，而且 PROPFIND 按
>    `FILES` 字典的**插入顺序**返回条目 → 恰好把对的那个排在前面 →
>    测试全绿、真机全坏。
>    → **mock 必须照抄真实结构**：目录层级、兄弟关系、**排序规则**都要一样。
>    这次改完 mock，**旧代码立刻从 48/22 全绿变成 40 PASS / 8 FAIL**，
>    红的正是「探测到同步根」「PDF 404」「列表为空」那几条。
> 3. **「界面把真实状态亮出来」的价值** ——
>    这一轮能一眼看出「vault 位置 = MicroBench同步」，
>    全靠上一轮加的「显示 vault 真实位置」。
>    → 报错时**把内部状态原样显示**，比给一句「同步失败」有用一百倍。

### 4.34 沙箱只挡住 `BASE_DIR` 是不够的：同步根也要钉死（2026-10-26 审计实测）

> 完整报告：[`docs/AUDIT_2026-10-26.md`](docs/AUDIT_2026-10-26.md)

**症状**：跑完 `pytest tests -m "not slow"`，用户**真实的坚果云同步目录**里
`files/demo.json` 的内容被改写了（sha256 变了）。
文件是 `{"paper_id": "demo", ...}` —— 明显是测试产物，但躺在**两台设备共享的云端目录**里。

**根因（三层叠加，少一层就漏）**

1. `microbench/llm.py` 在 import 时 `load_dotenv(..., override=False)`
   → `.env` 里的 `ANNOTATIONS_DIR` 被灌进 `os.environ`。
   `tests/conftest.py` 只钉了 `SYNC_PDF_BODY`，**没钉 `ANNOTATIONS_DIR`**。
2. `app.py` 的 `_sync_root()` 有**三个**分支：
   ① `ANNOTATIONS_DIR` 环境变量
   ② **坚果云云端沙箱自动探测** ← 这个也指向真实云端数据
   ③ vault 内的 `01_Literature/_同步`
   ⇒ **只钉环境变量挡不住 ②**，必须钉**函数本身**。
3. `sandbox` fixture 只 `monkeypatch.setattr(..., "BASE_DIR", tmp_path)`，
   **没管 `_sync_root`** → 整个同步层漏在沙箱外。

**为什么"测试全绿"完全掩盖了它**：测试碰真数据不会报错，
只会在同步目录里多一个文件 / 改一个时间戳。

**放大器**：`microbench/auto_sync.py` 每 2 小时自动跑一次 pytest
→ **无人值守地**反复写。

**修法**（`tests/conftest.py`）：

```python
@pytest.fixture(autouse=True)
def _pin_sync_root(tmp_path, monkeypatch):
    """把同步根钉到临时目录 —— 测试绝不碰用户的跨设备同步数据。"""
    from microbench import app as app_mod
    shared = tmp_path / "_sync_shared"
    shared.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_mod, "_sync_root", lambda: shared)   # ← 函数本身
    monkeypatch.setenv("ANNOTATIONS_DIR", str(shared))            # ← 环境变量
```

**护栏**（`tests/test_audit_2026_10_26.py`）：4 条，
其中最关键的是 `test_sync_root_ignores_nutstore_autodetect` ——
它**把环境变量拿掉**再断言同步根不许指向真实云端，
专门守"只钉环境变量"这种半吊子修法。
（破坏性验证：把 `monkeypatch.setattr` 改回 `pass` → 立刻 2 failed。）

> **通用纪律**：**沙箱要覆盖"所有能指到用户真实数据的位置"，不是覆盖你以为的那一个。**
> 这个项目里 BASE_DIR（vault 根）和 `_sync_root()`（同步根）是**两个**独立入口，
> 只挡一个 = 没挡。写完护栏后**必须做破坏性验证**：把修复改回去，确认测试真的会红。

### 4.35 「读一篇笔记」的端点，作用域必须是 vault 而不是仓库（2026-10-26 审计实测）

**症状**：`GET /api/vault/file?relative_path=logs/lan_token.txt` → **200 + 明文局域网口令**。
同一个端点的 DELETE 还能把它删掉 —— 删掉之后 LAN 访问 fail-closed 关闭，
用户只会觉得「平板突然连不上了」，查起来极费劲。

**根因**：`BASE_DIR` 既是**仓库根**又是 **vault 根**，
但仓库里还住着代码、文档、tests/，以及 `logs/`（存着局域网共享口令）。
`_safe_path` 只保证「不出 BASE_DIR」，**挡不住「读仓库里的任何 .md/.txt」**。

**修法**：`generator.py` 加 `VAULT_CONTENT_DIRS` + `is_vault_content_path()`，
`app.py` 的 GET / DELETE / reveal 三个端点都在 `_safe_path` 之后加这道判定。
判定放在**存在性检查之前** —— 不在范围内的路径一律 400，
不告诉调用方「这个文件存不存在」。

**连带要改的**：已有测试里用了仓库根路径（`"nope/missing.md"`、`"x.bin"`），
口径变了就得跟着改 —— 改成 `01_Literature/...`，**断言的意图保持不变**
（一个测 404、一个测后缀白名单）。

> **通用纪律**：端点的语义决定了它的作用域。「读一篇笔记」≠「读仓库里的文本文件」。
> 凡是 `BASE_DIR` 同时承担多个角色（仓库根 + 数据根）的地方，
> **都要显式列出允许的子树**，别靠"文件类型白名单"兜底。

### 4.36 异常处理里最后一个「凭据形状」的细节：脱敏要盖住 URL 的 userinfo（2026-10-26 审计实测）

**症状（两条，都实测复现）**

1. `probe_primary()` 的 `except Exception as e: return {"message": f"探针网络请求异常: {e}"}`
   → `/api/llm/probe` **原样**返回给浏览器。
   而 `requests` 的 `InvalidURL` 异常消息实测含**完整 URL（含 userinfo）**：
   `Failed to parse: https://user:sk-xxx@gw.internal:notaport/v1/chat/completions`
2. `_safe_base_url()` 的兜底分支 `except Exception: return url`
   → 畸形 URL（端口写成 `notaport`、IPv6 括号不配对）正好命中兜底
   → **带凭据的 base_url 进 `/api/llm/status` 响应体**。

**为什么这两条特别阴**

- 第 1 条：同步调用路径（`llm.chat` → `friendly_error_text`）**是**脱敏的，
  只有探针这条分支漏了 → 读代码时很容易以为"整个 llm.py 都脱敏了"。
- 第 2 条：**兜底分支天然是"出问题的那条路"**，而它偏偏是唯一没防护的那条。
  「URL 畸形」不是罕见输入 —— 手写 base_url 时最常犯的错就是端口写错。

**修法**：异常出口统一 `_mask_paths(_redact_secrets(str(e)))`；
兜底分支**绝不能原样返回** —— 走 `redact_secrets(url)`。

> **通用纪律**：**脱敏要放在"最后一公里"，不能靠下游兜底**。
> 而且**兜底分支要单独审一遍** —— 它是"出错了才走"的路径，
> 正常路径上测不出来（本轮就是靠"把端口写成 notaport"才测出来的）。

### 4.37 模型必须知道自己的能力边界：不然它会"思考 43 秒然后交白卷"（2026-10-26）

用户原话："可能他不能联网，但是他也不告诉我他不能联网搜索……我要的是一个答案
或者我要他不知道他也要回答我的问题，不能就是我等很久之后显示可能 AI 活动里显示
他回答，但是他实际上还不回答……他要有知道自己的能力边界。然后可以回答，
我不知道，或者说，他做不到。这都是可以的，但是不回答的话，这个情况尽量不要用。"

**实测对照**（同一问题、同一模型 M3.1-Flash，只换提示词）：

| 提示词 | 耗时 | 首字 | 正文 |
|---|---|---|---|
| 现状 | 45.0s | **从未产出** | **0 字** |
| 加能力边界条款 | 28.0s | 17.4s | **3175 字** |

**为什么现状会交白卷**：提示词只讲了"怎么回答"，没告诉模型"你没有联网能力"
→ 模型把"上网找图"当成需要长时间推理的任务去思考 → 推理预算吃光 → 正文 0 字。

→ **能力边界必须写进系统提示词**，三个提示词都要写
（`EXPLAIN_SELECTION_SYSTEM_PROMPT` / `EXPLAIN_SYSTEM_PROMPT` /
`CROSS_QA_SYSTEM_PROMPT`）；**平板端是另一份前端**（`tablet/app.js` 的
`SYS_RESEARCH`，直连模型不走后端提示词），要自己写一份。

**配套的机制性修复**（"显示在回答、实际没有回答"的真因）：

1. `llm._stream_model` 的 `[DONE]` 分支里是 `return` → **跳过了循环后的空正文守卫**
   → 上游正常发 `[DONE]` 但正文为空时被当成成功 → 前端渲染空答案 +
   AI 活动面板显示"✅ 完成"。→ 改成 `break`，两条路都落到同一个守卫。
2. 前端 `askLLMStream` 收到空正文时不能调 `onSuccess("")` → 报人话错误。
3. `friendly_error_text` 要拦下 `TRUNCATED_NO_BODY` 内部标记 → 说人话。

**第二个坑：单飞守卫不能"拒绝"新请求**

旧行为：第二次提问撞上还在跑的第一次 → 弹"已有 AI 请求进行中"直接拒掉。
而第一次又永远不结束（陷在长思考里）→ **用户被卡死：既拿不到旧答案，
也发不出新问题**。→ 改成 abort 旧请求 + 新请求接管。

⚠️ abort 之后旧请求的 `catch → onAbort` 回调**仍会执行**，
不处理的话它会把新请求正在写的答案区覆盖成"已取消"
→ 用**请求序号守卫**（`_qaRequestSeq` / `_isCurrentAsk()`）识别过期回调。

**通用教训**：
- **"模型做不到"必须让模型说出来** —— 提示词里写清能力边界，
  模型就会"说我做不到 + 给你能做的那部分"，而不是长时间空转交白卷。
- **提示词写"不能X"时别用 Markdown 加粗**（`你**不能**联网`）——
  匹配和模型理解都可能受影响，写成"你没有联网能力"。
- **空正文守卫要盖住所有断流方式**（`[DONE]` / 断流 / 只吐空白），
  而守卫放在 SSE 循环**之后**才能同时盖住前两者。
- **单飞守卫要"替换"不要"拒绝"** —— 用户主动发起的新请求，
  语义上就是"我要新的"，把旧的作废比把他卡住好。

护栏：`tests/test_qa_capability_boundary.py`（18 条，行为级 + 源码级，
已做反向验证：撤掉修复 → 16 条变红）。

### 4.38 思考型模型的 `max_tokens` 要按「思考 + 正文」算，不是按正文算（2026-10-26）

用户后续反馈："后面问任何问题都显示的是在思考，但是没有回答问题。
就是不管问啥，都变成这样。"

**这一条是 §4.37 的根因，也是本项目最贵的一个坑。**

**根因**：**M3.1-Flash 强制思考，思考 token 与正文 token 共用同一个
`max_tokens` 预算池。** 答疑接口原来给 `max_tokens=4000` / `timeout=120`
→ 8K 上下文时思考就把 4000 吃光 → **正文 0 字**。

实测对照（7K 上下文 + 生产提示词 + 真实模型）：

| max_tokens | timeout | 总耗时 | 首字 | 正文 |
|---|---|---|---|---|
| **4000**（原值） | 120 | 59.9s | **从未产出** | **0 字** → 报"未产出正文" |
| **16000**（现值） | 300 | 86.2s | 70.3s | **4884 字** ✅ |

端到端复测（真实接口 + 用户截图里的场景）：**42.2s / 3071 字**。

→ **翻译给 4000 是给「关思考的直译型模型」用的；答疑给同一个数就必然翻车。**
（翻译路径 `translation_disable_thinking()` 默认开着，答疑默认**不**关。）

**三个答疑入口有三个不同的坑，漏一个就有一个继续犯同样的毛病**：

| 接口 | 原值 | 现值 |
|---|---|---|
| 划词答疑 | 4000 / 120 | `QA_MAX_TOKENS` / `QA_TIMEOUT` |
| 章节解读 | **1200** / 默认 **60** | 同上（最严重的入口） |
| 跨章节问答 | 4000 / 默认 60 | 同上 |

`app.py` 新增 `QA_MAX_TOKENS = 16000` / `QA_TIMEOUT = 300`，三个接口统一引用。

**连带修的另一个坑**：空正文后的「关思考重试」对强制思考模型是**空操作**
—— `should_send_thinking_disabled(M3.1-Flash, True)` 返回 False
→ 参数根本不下发 → 请求与第一次完全相同 → 再等 55 秒还是 0 字
（实测 55s → 111s 才报错）。→ 改成：能关思考才走那条路，
**不能关的（强制思考型）改成「抬高 max_tokens 再试」**。

> **通用纪律**：
> - **同一个预算常量在不同任务里的含义不同** —— 思考型模型要按
>   "思考 + 正文"算，直译型模型按正文算。照抄别的路径的数值必然翻车。
> - **同一个坑要在同一层所有入口一起修** —— 三个接口三个值，
>   只改用户截图里那一个 = 换了个入口继续犯同样的毛病。
> - **时限只该防"真卡死"**，不该砍掉还在正常思考的请求
>   （用户原话："他即使思考再久也行，我只要他能回答问题"）。

护栏：`tests/test_qa_capability_boundary.py`（22 条，含 4 条预算断言；
反向验证：撤掉修复 → 15 条变红）。

### 4.39 思考强度：上游往往有第三档，从它的 400 报错里挖出来（2026-10-26）

用户诉求："思考型模型思考要三十秒到九十秒……你把它给设定成 low，
思考强度设定的低一点。Max token 可以设定高一点，
反正就保证他都能正常运行就行。"

**之前只有"开/关"两档**（`thinking.type=disabled` / 不传），
强制思考模型（M3.1-Flash）被卡在中间：**关不掉**，只能全开。

#### 关键技巧：**上游的 400 报错原文会告诉你它支持什么参数**

MiniMax M3.1-Flash 传 `thinking.type=disabled` 时的完整报错：

```
invalid params, model "MiniMax-M3.1-Flash" requires adaptive thinking;
  thinking.type="disabled" (including reasoning.effort=none) is not allowed
```

→ 它点名了 **`reasoning.effort`** 这个参数！
→ 再试 `reasoning_effort=none` → `reasoning effort none is not allowed`；
  试 `low` / `minimal` → **通过校验**（返 429 = 参数没问题，只是额度用完）。

> **通用纪律**：**别只看"这个参数有没有效果"，还要看"上游认不认这个参数"。**
> 判断方法很便宜：**校验错误(400)发生在配额检查(429)之前** ——
> 所以**没额度也能测出参数是否被接受**（返 429 = 接受，返 400 = 拒绝）。

#### `low` 真的有效（同一 7K 上下文 + 同一模型，DeepSeek Flash）

| reasoning_effort | 耗时 | 正文 | **思考量** |
|---|---|---|---|
| 不传 | 55.5s | 4609 字 | **7572 字** |
| **low** | **17.8s** | 3201 字 | **1052 字**（↓ 86%） |
| none | 13.5s | 3310 字 | **9 字** |

⚠️ 但**短问题上 low 反而更多**（实测思考 73 → 157 字）——
`low` 不是"思考更少"的保证，只是"别过度思考"。

#### 判定规则（`llm.reasoning_effort_for`）

| provider | `none` | `low` |
|---|---|---|
| DeepSeek / Agnes | ✅（思考 73 → 9 字） | ✅ |
| MiniMax M3.1-Flash | ❌ **400** → **自动降级成 low** | ✅ |
| 未知网关 | 不发（保守） | 不发 |

**判错的后果是 HTTP 400（请求直接被拒，不是静默失败）** → 未知网关一律不发。

实现：`app.QA_REASONING_EFFORT = "low"` → 三个答疑接口统一传
`reasoning_effort=QA_REASONING_EFFORT`。

#### 顺带：能力边界声明的口径改了（用户明确要求）

用户："不需要他第一句话就说自己的能力边界。你只需要最后能说一句就行了。
你前面第一句话，每次第一句话说挺麻烦的。"

→ 提示词从"**第一句**就说明"改成"**先给实质内容，最后一句**说明"，
且明确写了"不要把能力边界声明放在开头当第一句"。

护栏：`tests/test_qa_capability_boundary.py`（28 条；
含 `test_forced_thinking_model_never_gets_reasoning_effort_none` 等 5 条新断言）。

---

### 4.52 批注重放会**自激**：MutationObserver 里的 setTimeout 必须防抖（2026-10-27 审计实测）

**症状**：译文栏的文字**选区会被莫名清空** → 划词答疑读不到 `getSelection()` →
用户以为是划词功能坏了。

**根因**（`microbench/static/index.html` 的 `initAnnotationFeatures`）：

```js
const obs = new MutationObserver(() => {
  if (!_annoState.items.length) return;
  setTimeout(reapplyAnnotations, 80);      // ← 没有 clearTimeout！
});
obs.observe(list, { childList: true, subtree: true, characterData: true });
```

`reapplyAnnotations` 自己会改 DOM（`_clearAnnoMarks` 清标记 + `_wrapTextWithMark`
重新包 `<mark>`），而这个 DOM **正在被观察** →

```
改 DOM → 触发 observer → 再排一次 setTimeout → 再改 DOM → …
```

**实测**：只要这篇论文有**一条能匹配上的批注**，3 秒内 `reapplyAnnotations` 被调用
**32 次**（≈10.7 次/秒，永不停止）；译文栏 60 字符的选区在 500ms 内变成 **0**。

**修法**：① `clearTimeout` 真防抖（旧写法是"每次变更都排队"，不是防抖）；
② **用户正在划词时不动 DOM**（`getSelection()` 非折叠就跳过这一轮）——
否则重放照样会把选区洗掉。

> **通用教训**：**观察者回调里如果会改被观察的子树，就必须防抖 + 加"正在交互就跳过"的闸门**。
> 桌面端、平板端、离线导出三套前端都要各自检查一遍。

护栏：`microbench/_test_audit_2026_10_27.js`（node 跑 index.html 里的**真实源码**：
断言"5 次变更 → 只排 1 个定时器"和"划词中不重放"）+ `tests/test_audit_2026_10_27_core.py`。

---

### 4.53 `{ once: true }` 的语义是「**任意**键按一次就摘掉」，不是「这个键按一次」（2026-10-27 审计实测）

**症状**：弹窗（快照 / 诊断）**按 Esc 关不掉**；关了再打开后 Esc **永久失效**。

**根因**：

```js
document.addEventListener("keydown", onEsc, { once: true });
```

`once: true` = "**任意**键按一次就摘掉这个监听器"。所以弹窗开着时先按了 `a` 或 `q`，
Esc 处理器**已经被消耗掉了** → 后面按 Esc 什么都不会发生。
诊断弹窗更糟：监听器只在 `if (!modal)` 里挂一次 → 关掉再打开后**再也挂不上**。

**修法**：统一走 `_wireModalEsc(overlay, closeFn)`（只认 Esc、关掉时摘干净、
并且 `stopPropagation`）。`stopPropagation` 还顺手解决了一个叠加问题：
专注模式的 Esc 会把 Esc「让给」弹窗（`_zenOverlayOpen()`），
而弹窗自己的 Esc 又被别的键吃掉了 → **两边都不响应 = 键盘陷阱**。

> **通用教训**：`{ once: true }` 只适合"下一个特定事件"，
> 想等某个键就用 `removeEventListener` 自己管生命周期。

---

### 4.54 `transform` 藏起来 ≠ 移出焦点序（2026-10-27 审计实测）

**症状**：专注模式下按 Tab 会**跳进一个看不见的工具条**（元素在视口上方 90px，
`getBoundingClientRect().top === -90`）—— 23 个控件进了焦点序。

**根因**：专注模式用 `transform: translateY(-101%)` 把 `.reader-topbar` 移出视口。
**`transform` 不会把元素移出焦点序**（不像 `display:none` / `visibility:hidden`）。

**修法**：收起时加 `visibility: hidden`（同时移出焦点序和可访问性树），
展开时 `visibility: visible`；配 `transition: visibility 0s linear .2s` 让收起
动画走完再隐藏。

> **通用教训**：**"看不见" 和 "不存在" 是两件事**。
> 视觉上藏起来的 UI 必须同时问一句"它还能被 Tab 到吗？还能被读屏念到吗？"

---

### 4.55 局域网上的交互式 API 文档 = 免费送给对方的**攻击面说明书**（2026-10-27 审计实测）

**症状**：`/docs` `/redoc` `/openapi.json` 在局域网上**不需要口令**。

**实测**：从 `192.168.1.100` 无口令 `GET /openapi.json` → **200，71386 字节**，
里面是 **60 条路由 + 全部请求/响应模型**；而同一时刻所有 `/api/*` 都正确返回 401。

**为什么严重**：口令是 LAN 模式**唯一**的防线（vault 里的全部笔记 +
用户的 LLM 额度 + 跨设备同步目录）。把 API 地图免费送出去，
等于把「目标系统有多详细」直接告诉攻击者。

**修法**：`_path_needs_token(path)` 把 `/docs` `/redoc` `/openapi.json`
和 `/api/**` 一并纳入口令保护；`/api/auth/*` 仍然豁免（平板要靠它问"要不要口令"）。

护栏：`tests/test_audit_2026_10_27_core.py::test_lan_openapi_and_docs_require_token`
（同时断言**本机仍然能看**文档 —— 不能把桌面端也堵死）。

---

### 4.56 鉴权要 **fail-closed**：拿不到对端地址 ≠ 本机（2026-10-27 审计）

**症状**：`_client_is_local(request)` 原来是
`not _is_remote_peer(client.host if client else "")` ——
`client` 为 None 时 `host` 是空串 → `_is_remote_peer("")` 返回 False
→ 判定为「本机」→ **跳过口令校验**。

**为什么危险**：判成本机 = 鉴权整个失效。拿不到对端地址时，正确答案是
「我不知道你是谁」→ **当成远端**。

**修法**：`client` 为 None 或 host 为空 → 直接 `return False`（远端）。
Starlette 的 TestClient 填的是 `"testclient"`（不是 IP → 回环分支），
所以测试不受影响；只有"对端信息完全缺失"这一种异常路径会变。

---

### 4.57 限流器的「预留时间」和「实际触发时间」是两回事（2026-10-27 审计实测）

**症状**：并发起 5 个上游请求 → **4 个同时发出**（间隔判据形同虚设）→ 打到上游每分钟上限
→ 被误判成「配额耗尽」→ `mark_degraded` **全局降格 15 分钟**（用户额度其实还有）。

**根因**：`llm.py` 限流器里，间隔判据读的是 `_last_call_time`（**实际触发**时间，
在 sleep **之后**才更新），而**预留**写在 `_last_planned_time` ——
**间隔判据根本没用预留值**。于是 N 个并发者：第 1 个立即发，
其余 N-1 个全部 sleep 一个 `min_interval` 后**同时**发出。

**实测**（真实类，5 线程、min_interval=6.0）：

```
CURRENT   gaps=['6.00','0.00','0.00','0.00']   ← 4/5 同时发出
FIXED     gaps=['6.00','6.00','6.00','6.00']
```

**修法**：间隔判据改用 `_last_planned_time`（**预留**时间），
或预留时同步把 `_last_call_time` 推进到 planned。
**验收标准是「每一次实际触发间隔 ≥ min_interval」**，不是"总耗时没变"。

> **通用教训**：**"我已经占了这个位置"和"我真的用了它"是两个时间戳**。
> 凡是"先排队再执行"的限流/排队逻辑，间隔判据必须读**预留**的那个。

---

### 4.58 事件循环上不能做同步重活（2026-10-27 审计实测）

**症状**：翻页 / 打开论文时**全站卡住**（看板计数、3 个 SSE 答疑面板、LAN 平板一起冻）。

**根因**：`async def` 端点里直接调**同步的**重活，而且**没有 await**：

| 端点 | 同步重活 | 实测 |
|---|---|---|
| `POST /api/paper/translate_page` | `extract_page_text_with_glyphs`（pdfplumber 解析） | **64~332 ms/页**（中位 264） |
| `GET /api/paper/find_pdf` | `iterdir()` + `rglob("*.pdf")` 全量扫 vault | 并发 8 时探针 MAX 78.3ms |
| `GET /api/paper/formula_image` | `_pdf_clip_png`（fitz 渲染） | 跨站可触发、无并发上限 |
| `POST /api/sync` | files 通道 sha256 + 79MB 拷贝 | 事件循环被占满 |

（`01_Literature` 等目录是**坚果云 junction** → 扫描打的是云端 I/O，断链时更慢。）

**修法**：
① 重活一律 `await asyncio.to_thread(...)`；
② 能吃缓存的先吃缓存 —— `pdf_cache` 里的页结构与单页抽取**逐字一致**
（同一个 `_extract_page_text(page, glyphs)`），所以按页翻译可以直接命中缓存；
③ 全量扫盘的端点改成**同步 `def`**（FastAPI 会丢进 anyio 线程池，不冻结事件循环）
—— 与 2026-10-27 那轮已修的 `/api/stats` `/api/vault/literature` `/api/paper/folders` 同理，
**那一轮漏掉了 `find_pdf`**。

> **通用教训**：`async def` **不等于**"不阻塞"。
> 只要函数体里有一句**同步的重活**，整个事件循环就在那一刻停住。
> 判据：这段代码会不会读盘 / 解析 PDF / 算 sha256 / 发同步 HTTP？
> 会 → 要么 `to_thread`，要么把端点改成同步 `def`。

---

### 4.59 **GET 端点不该建目录**（2026-10-27 审计实测）

**症状**：`GET /api/pdf/annotations/load` 会在**坚果云同步根**里建 `annotations/` 目录
（还会被坚果云上传）。

**为什么能跨站触发**：GET 属**安全方法** → `csrf_origin_guard` **只管非安全方法**
→ 跨站一个 `<img>` 就能让服务端建目录。

**修法**：目录创建从**读路径**移到「lifespan 启动时建一次」+「**写**路径显式建」
（`_ensure_snapshot_dir()` / `_ensure_annotations_dir()`）。

护栏：`test_get_endpoints_do_not_create_directories`（启动后删掉目录，
GET 之后断言**没被重建**）+ `test_write_paths_still_create_the_directories`
（写路径不能被改没了）。

---

### 4.60 跨设备共享文件的「读-改-写」要用 **CAS**（2026-10-27 审计）

**症状**：两台设备同时标注同一篇论文 → **其中一条批注被整文件覆盖**。

**根因**：批注 JSON 住在**坚果云共享目录**。A 读 → A 合并 → A 写回，
这个窗口里 B 写了一次 → A 的写回把 B 的内容**整个覆盖**。
`_merge_annotation_lists` 只在**内容**层面合并，**救不了整文件覆盖**。

**修法**：乐观 CAS —— 写之前再取一次磁盘指纹 `(st_mtime_ns, st_size)`，
和读完时取的那份比对；**变了**说明另一台设备刚写过 → 重新读 + 重新合并再来一轮
（最多 3 轮）。窗口从「整个同步往返」缩到「一次文件读」。

> **通用教训**：**只要文件在两台设备之间共享，"读-改-写"就必须假设中间有人改过**。
> 光在内容层面合并是不够的 —— 合并要建立在"我合并的是最新版本"这个前提上。

---

### 4.61 依赖 CVE 会随版本漂移：**审计报告不是永久有效的**（2026-10-27 审计实测）

**事实**：2026-10-26 那一轮审计的结论是「`pip-audit` → No known vulnerabilities found」。
**一天后**（2026-10-27）同一台机器上：

```
Pillow 12.2.0   → 40+ 个已知 CVE（CVE-2026-96636/96637/96638…、
                   CVE-2026-112390~112406、GHSA-78g4-xqwv-q3g8…）
                   **全部在 13.0.0 修复**
requests 2.32.3 → 2 个（PYSEC-2026-1872 → 2.32.4；PYSEC-2026-2275 → 2.33.0）
```

**为什么这次是真的**：MicroBench 会用 Pillow 打开**用户上传的 PDF 渲染出来的图**
和**图表截图**（`glyphs.py` / `plot_compare.py`）→ Pillow 是真实攻击面，
一张畸形图片就能打这些 CVE。

**修法**：`requirements.txt` 锁下限（`Pillow>=13.0.0` / `requests>=2.33.0`），
不是锁死版本（锁死会让另一台设备装不上）。

> **通用教训**：
> - 审计报告里「无 CVE」这种结论**会过期**，隔一段时间要重跑；
> - **查依赖 CVE 要用「真实已安装版本」**，不要拿 `requirements.txt` 里的
>   下限去查（那查的是"如果装最低版本会怎样"，不是"现在装的是什么"）；
> - `pip-audit` 的输出**要限定到本项目的依赖闭包** —— 扫整机环境会得到
>   几百条无关的噪音（本机 222 条 / 27 个包，其中绝大多数与本项目无关）。

---

### 4.62 护栏的断言要**够窄**（2026-10-27 审计实测）

**这一轮 18 条修复的破坏性验证里，5 条护栏一开始是「零覆盖」** ——
把修复改回去，护栏**居然还是绿的**。四种典型：

| 坏护栏 | 为什么零覆盖 | 改法 |
|---|---|---|
| 断言 `app.py` 全文含 `asyncio.to_thread(_pdf_clip_png` | 同一个函数还被**抄表**路径用着 → 改一处还剩一处 | 断言**限定在那个函数体内**（`inspect.getsource`） |
| 断言全文含 `asyncio.to_thread(_extract_page_cached` | 那个词的**文档字符串**里也写着它 | 同上 |
| 断言 `hasattr(mod, "_REAL_STATS_LOCK")` | 把锁赋成 `None` 也算"有锁" | 断言它是 `threading.Lock` **实例** |
| 断言 `"onEsc, { once: true }" not in src` | 换个变量名就绕过去了 | 扫**所有** `addEventListener("keydown"` 调用点，逐个看有没有 `once: true` |

**教训（接 §4.51）**：§4.51 说"变异要改**被调方**"；这一轮补上另一半 ——
**"变异"还要让护栏真的能红**。判据：
1. 护栏必须**限定在被测的那段逻辑**里，不能全文 grep；
2. 护栏必须能区分"改对了"和"改成了另一个样子"；
3. **每条护栏都要做一次破坏性验证**（改回去 → 必须红 → 复原 → 必须绿），
   否则它只是一条**看起来像**护栏的断言。

破坏性验证脚本：`C:\Users\30461\.minimax\tmp\audit_20261027\destructive_verify.py`
（18 条变异 → 18/18 护栏变红 → 复原后 29 条全绿）。

---

## 5. 通用教训（从上面提炼的原则）
1. **先定位根因，再改代码。** 拿不准就在可疑位置加 `console.log` 探针 + 用浏览器的
   `query(kind=console)` 看，比盯着代码猜快得多。
2. **现象在 UI 层，根因常在数据流上游。** 「右边对不上左边」的真因是页内文本顺序，
   不是滚动同步。
3. **判定逻辑要抽成生产与测试共用的函数**（如 `_find_vertical_split`）。
   第一版测试自己写死 `y_split=0`，和生产算出的 91.3 不一致 → 测试假失败。
4. **判定「是否交替」要用正文区且长度 ≥ 4 的词**：页眉里的词会出现在文本最前面，
   单字符词（`'c'`）到处都有，用它们判定必误报。
5. **跨设备的多端语义必须带身份/来源标记**（owner / device_id），否则会误删别人的数据。
6. **状态标记不能永久粘住**（代理兜底要能自愈），否则用户恢复条件后行为还是错的。
7. **删除和移动走可恢复路径**（mavis-trash），别做永久删除。
8. **测试用 `tmp_path` 隔离**，不要依赖真实 vault 的内容（vault 会随使用变化）。
9. **交付前在浏览器里实测**，别只看 API 返回 200就说完成了。
10. **知识要落进仓库**（本文档 + `docs/` 两份契约），不能只留在对话里 ——
    换台设备、换个会话就找不回来了。

---

## 6. 诊断手册：症状 → 查哪

| 症状 | 先看 | 常见根因 |
|---|---|---|
| 看板数字全是 0 | console 有没有 `[stats] 看板统计已更新` | `DOMContentLoaded` 没触发（§4.1）／统计口径（§4.2） |
| PDF 打不开、报 400 非法路径 | `serve_pdf` 返回体 | `_safe_path` 的 `resolve()` 穿透 junction（§4.4） |
| PDF 打不开、列表 500 | 服务日志 | 同上 |
| 译文左右横跳 | `microbench/paper_text.py` | 抽取顺序（§4.5） |
| 译文里出现原文没有的公式 | 抽取结果里的 `⟦FORMULA⟧` | 碎公式被模型编造（§4.6） |
| 另一台设备看不到批注/快照 | 两边的 `paper_id` 是否一致 | 路径差一个字符 → 哈希变（§4.7） |
| 同步传了大文件 | `files/<id>.json` 的 size/mtime/sha256 | 增量判定失效（§4.7） |
| 坚果云没上传 | `nutstore.db` 的 `sync_errors` | 位置错了（§4.8） |
| 桌面快捷方式双击报错 | `.bat` 的行尾 | LF（§2.6） |
| PowerShell 脚本报「字符串缺少终止符」 | 脚本编码 | PS 5.1 读 UTF-8（§2.1） |
| 中文脚本在 cmd 里乱码 | `chcp` / 编码 | 同上 |
| 批注点了没反应 / 批注丢了 | mouseup 是否落在气泡上 | 点浮层按钮会触发浮层容器的 mouseup 采集（→ return） |
| 全量测试绿、单跑某个文件却红 | `.env` 的部署开关是否漏进 pytest 进程 | `llm.py` import 时 `load_dotenv(override=False)`；测试进程要隔离 `.env`（`tests/conftest.py` 已钉死） |
| 某个 `@slow`/`@llm` 测试偶发红 | 单跑是否通过 | 真打 MiniMax API，偶发；不是代码问题。pre-commit 钩子用 `-m "not slow"` 已排除 |
| 平板上改的笔记刷新就没了 | `loadSeed` 是不是拿种子覆盖了本地正文 | 种子是导出期快照；本地必须优先（§4.16-1） |
| 平板上改了笔记但云端没变 | 同步按钮分支 / `pushToDir` 有没有被调到 | `syncMode === "dir"` 永远不成立；`pushToDir` 无并发闸门（§4.16-1） |
| 平板上划词没反应 | 划词监听绑在哪个节点上 | 绑在会被 `innerHTML=""` 清掉的 textLayer 上 ⇒ 永久失效（§4.16-1） |
| 平板上笔记整篇空白 | 笔记里是不是有引用块/列表里的公式 | 公式表没跨递归层共享 ⇒ `renderMarkdown` 抛异常（§4.16-1） |
| 删掉的批注又回来了 | 同步 push 里有没有「兼容旧版本」的回灌 | 本机陈旧副本合并回真源、deleted 传空（§4.16-1） |
| 另一台设备译文少了几页 | 快照是不是整份替换 | 必须按 `page_num` 合并（§4.16-1） |
| 平板变更包一条都合不回来 | `merge_changes.safe_target` 用没用 `resolve()` | `resolve()` 穿透 junction ⇒ 全判越界（§4.4 / §4.16-1） |
| 含 emoji 的路径两端 id 对不上 | paper_id 哈希按码点还是码元 | JS 用 UTF-16 码元，Python `ord()` 是码点（§4.7） |
| 表格显示成一堆竖线、占半屏 | 译文里是不是 `\| a \| b \|` | 渲染层没有 Markdown 表格支持（§4.12①）|
| 表格数值整行错位 | 译文里是不是 `⟦TABLE:…⟧` 没被替换 | 表格走文本层会错位 → 应由多模态读图抄（§4.13）|
| 表格整张丢失 | 译文里有没有半截 `⟦TABLE:28.2,` | 页尾截尾把标记切坏了（§4.12）|
| 公式是占位符 / 分子分母装反 | 译文里有没有 `⟦FORMULA:...⟧` | 抽取层给坐标、前端裁 PDF 原图（§4.12③）|
| "翻译顺序还是错的" | 快照的 `extractor_version` | 旧快照是**老算法**翻的，整份作废重翻（§4.12②）|
| 译文里出现逐字交错的乱码行 | 相邻两行被合成一行 | 裁剪边界切在字上，扩边裁剪（§4.12）|
| 重译后批注不见了 | 抽屉条目有没有"位置失效"提示 | 失效≠丢失：笔记必须保留（§4.14）|
| 标注列表每次都从第一页开始 | `openAnnoDrawer()` 的定位分支 | 应滚到**当前页**的第一条（§4.14）|
| 改了前端但桌面窗口没变 | 桌面应用是不是启动在改动之前 | pywebview 只在启动时拉一次首页（§4.14）|
| 平板上译文还是「错的那一列」 | 云盘快照有没有 `extractor_version` | 两端版本判据不一致；「没版本号」也必须算旧（§4.18）|
| 平板升了 APK 后译文没变（还是旧的） | 本地缓存键里有没有版本号 | 缓存命中绕过了版本闸门 → 键要带版本（§4.18）|
| 翻过的译文被翻回旧内容 | 快照合并有没有过版本闸门 | 按 page_num 合并把过期页合回来了（§4.18）|
<<<<<<< HEAD
| 平板「已连接 0 篇」但探测过前缀 | 前缀是不是**非空但错的** | 兜底只接了「空」，没接「错」（§4.32）|
| 平板 vault 位置显示成「…MicroBench同步」 | 探测是按名字还是按内容 | vault 和同步根是兄弟目录，名字会撞车（§4.33）|
| 测试全绿但真机全坏 | mock 的结构排序跟真机一致吗 | mock 太善良 = 假绿（§4.33）|
| 平板云盘同步一进去就被限流 | 自愈有没有节流 | 重探测是递归全盘，5 分钟最多一次（§4.32）|
| 改完 android/ 后 APK 编不过 | 桩类是不是被挪进了 `android/java` | 桩跟真 android.jar 撞类（§4.32）|
| 对方设备的 auto_sync 一直报「测试失败」 | 测试脚本里有没有写死本机路径 | 写死路径 = 对方永远红（§4.32）|
| 打开/上传论文半天没反应 | `logs/desktop.log` 有没有「符号认字」刷屏 | 逐页串行认字（§4.24）|
| 译文里的 × / ≈ 显示成 `[?]` | 词典里有没有 `s:` 开头的语义键条目 | 认字没跑完 / 词典没命中（§4.22 §4.24）|
---

## 7. 关键 commit 索引（改前想回溯就查这里）

| commit | 内容 |
|---|---|
| `b86250f` | `_safe_path` 改逻辑路径判定，junction 不再被判越界 |
| `db3a4c8` | 同步协议新增 `files/` 通道（PDF 本体增量同步 + 墓碑 + 空间管理） |
| `e82a6db` | 双栏页改「左栏整段读完再读右栏」；移除两栏滚动联动 |
| `c5319ab` | 坚果云同步根探测修到真正的云端沙箱（`Nutstore\<id>\我的坚果云`） |
| `7e9b26d` | 锁死 paper_id 契约（前后端算法必须逐字一致） |
| `df009fc` / `06d207e` | 看板统计改真实产出；修 `loadStats` 从未执行 |
| `0a84258` | 跨栏表格堵死栏缝 → 逐带投票找栏缝 |
| `9bf17da` | 行内公式空档（placeholder 宽度 50.6px → 20.6px） |
| `e4bdbae` | 模板随代码走（全新克隆不 500） |
| `b4f1e97` | 行尾归一 LF（`.gitattributes`），冲突从巨型块降到语义块 |
| `6330cfd` | 平板：修「列目录正常但 PDF 全 404」（vault 在云盘里的路径前缀丢了） |
| `103b2e3` | 平板 APK 首轮系统审查：4 阻断 + 12 重要（TLS 主机名验证、Base64 API 26、批注删除复活、快照整份替换、junction 穿透…） |
| 见下方 | 平板全面审计：6 阻断 + 11 重要 + AI 面板重做（发送后清空输入栏） |
| `d1be3b7` | 桌面改成独立应用窗口（pywebview/WebView2），快捷方式不再走浏览器 |
| `d8002df` | 表格渲染成真表格 + 旧快照按抽取版本作废 + 公式改用 PDF 原图裁图 |
| `a6db653` | 表格改走多模态读图（裁图 → M3 逐格抄 → 拼回译文），数值与论文一致 |
| `00b6beb` | 同一页的多张表并行读图（整页翻译 27 秒） |
| `8b4fbfb` | 交付前审查：裁图像素上限、清死代码、标记兜底 |
| `2880b4b` | 阻断级回归：裁剪框越界导致整组正文消失（24/391 页） |
| `75aa297` | 审查后收紧表格识别 + 修缓存顺序/模型选择/表格拼接 |
| `a5e1b82` | 文档：记入独立审查揪出的三个坑（裁剪越界/静默吞异常/合并跨表格带） |
| `9163cda` | 提问发送后自动清空输入框（三处提问入口） |
| `7aa112e` | 批注重译不丢记录（标"位置失效"）+ 标注列表按当前页定位 |
| `70bc138` | 选区归属按选区兜底：松手在文字外不再把批注丢到"章节视图" |
| `6825b44` | 合并对方 13 个提交（桌面阅读器升级 + 独立窗口）+ 修跨端快照版本闸门（§4.18） |
| `模型层` | 自定义 API 模型（.env LLM_CUSTOM_N_*）+ 按模型限流 + 连不上自动退回 + 抄表校验 |
| `AI 活动` | 翻译/答疑的实时进度可视化：已用时 + 阶段 + 取消（§4.21） |
| `符号认字` | 解不出的 × ≈ ≥ 不再是 "?"：裁图认字 + 词典缓存（§4.22） |
---

## 8. 已排除的方案：把项目「转成 MiniMax Code 云端项目」（查清后 pass）

MiniMax Code 侧边栏有「本地 / **云端**」两个 tab，但**云端不是文件夹同步**，
不要再往这个方向想：

- `project_kind` 的数据库约束写死只有 `'default'` / `'workspace'`，**没有"云端项目"这个类型**
- 云端 tab 里放的是**云端会话**（Cloud Session），由会话里的 `/cloud` 交接产生：
  把对话历史 + 你勾选的文件传到 MiniMax 云盘，让**云端 agent 接着跑**
- 它是「把重活委派给云端」的功能，**不是共享工作区**
- 限制：单文件 ≤500MB、总体积 ≤500MB、≤15,000 文件、**只收工作目录内的文件**
  → vault 搬进坚果云（junction）后，里面的文件会被判 `outside_workspace` 而拒传

**结论：两台设备共享工作区的正确做法是「vault 整体进坚果云 + junction」**
（见 §4.4 部署配置），不是 `/cloud`。这条路已 pass，别再反复折腾。
