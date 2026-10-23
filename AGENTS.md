# MicroBench 项目记忆（AGENTS.md）

> **这份文件给任何接手这个项目的 agent 看。** 它不描述功能（功能看 README.md），
> 它记录的是**这个项目怎么被做对、以及踩过哪些坑**。
>
> 另有两份技术契约，改代码前必读：
> - [`docs/READER_CONTRACT.md`](docs/READER_CONTRACT.md) —— 阅读器：页内顺序、公式、批注
> - [`docs/SYNC_CONTRACT.md`](docs/SYNC_CONTRACT.md) —— 跨设备同步：红线清单，改动即破坏

---

## 0. 一分钟定位

| 项 | 值 |
|---|---|
| 是什么 | Obsidian vault + FastAPI 本地服务（科研文献精读工作台） |
| 仓库根 | `E:\zaiwu\个人工作台`（**它同时是 vault 根**） |
| 服务 | `python -m uvicorn microbench.app:app --host 127.0.0.1 --port 5000` |
| 启动 | 双击 `start_workbench.bat`（= 桌面快捷方式「MicroBench 工作台」） |
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

#### `.obsidian` 的取舍：界面状态不要跨设备共享

`workspace.json` 是 Obsidian 的**界面状态**（开了哪些面板、编辑器布局），
两台都装 Obsidian 时会互相覆盖（谁后打开谁赢），布局也会串。
→ **各留各的**：搬 vault 时 `.obsidian` 可以带进云端（"换位置设置也跟着走"），
但**另一台设备不要给它建 junction**，让它用自己的本地目录。
（2026-10-23 实测：30461 这台共享 `.obsidian`，25353 那台不共享。）

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
