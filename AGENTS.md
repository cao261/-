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
> [`docs/HANDOFF_2026-10-23.md`](docs/HANDOFF_2026-10-23.md) —— 跨设备交接记录。
> 两台设备（25353 / 30461）之间**没有会话通道**（`session send` 只能投本机会话），
> 交接一律走仓库：对方 `git pull` 就能拿到本文件。
> ⚠️ 它是**某个时点的快照**，不是长期状态 —— 以 §4.4 和各机实测为准。

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
| 测试 | `pytest tests/` —— **全绿才算做完**（当前 203 passed） |
| 两台设备 | 本机 `25353`（主开发）+ 对方 `30461`（同账号，另一台机器） |

**代码走 GitHub，数据走坚果云** —— 两条通道互不重叠，见 §4.4。

---

## 1. 用户的硬性要求（改动前先读）

这些是用户反复强调过的，**不是我的推测**：

1. **改完必须能跑。** 服务起得来、`http://127.0.0.1:5000` HTTP 200、`pytest tests/` 全绿。
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
python -m pytest tests/ -q            # 全绿
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
2. **长任务必须可取消**。有 `AbortController` 就别浪费。
3. **阶段可以是前端推断的**，但要在文案里说清楚是推断，别假装服务端报了阶段。

---

### 4.19 定期同步对方设备的升级（两台设备 + 平板 APK 必须同版本）

**问题**：两台设备共用一个 GitHub 仓库。对方（另一台设备）一直在做桌面端升级，
本机和**平板 APK** 必须跟着一起升 —— 否则用户就是「电脑上新版、平板上旧版」，
还会以为是两个 bug。

**做法**：`microbench/auto_sync.py`（幂等，没新提交就什么都不做）。
定时任务**每 2 小时**跑一次（Asia/Shanghai `0 */2 * * *`）。

> **这台机（30461）上的定时任务**：
> 名字「定期同步对方设备的升级（本机 + 平板 APK）」
> `cron_id = d2c04537-d595-43ca-9efb-3a8a8ae8ca88`
> 跑法 = 在仓库根目录执行 `python microbench/auto_sync.py`，读最后一行
> `MB_SYNC_RESULT: {json}`，按 `action` 决定怎么汇报。
> **另一台设备上的 agent 看到这段就知道：对方推的东西会自动跟过来，
> 不用等人工同步**；反过来，这台机也会自动同步对方推的东西。

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
