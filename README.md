# MicroBench | 微电子与半导体科研复现工作台

> 面向西安交通大学电子科学与技术专业（微电子 / 集成电路设计 / EDA / 半导体器件与TCAD物理仿真）定制的个人学术工作台。

---

## 🚀 极速上手使用指南

本工作台采用**“双模协同”**架构，既是标准的 **Obsidian 双链知识库**，也是一个**本地可运行的自动化科研辅助工具**。

### 方式零：桌面快捷方式（最快，30 秒上手）
本机已配置好两个桌面快捷方式，**双击即可，无需进目录**：

| 快捷方式 | 作用 |
| --- | --- |
| **MicroBench 工作台** | 自动启动本地服务 → 等就绪 → 自动打开浏览器到 `http://127.0.0.1:5000` |
| **MicroBench 项目目录** | 直接打开项目文件夹 |

- 服务端跑在独立的 `MicroBench Server` 控制台窗口里，**日志实时可见**；
- 停止服务：切到该窗口按 `Ctrl+C`，或直接关闭该窗口；
- 重复双击不会启动多个实例——脚本会先探测服务是否已存在，已存在就直接开浏览器。

### 方式一：直接在 Obsidian 中打开使用
1. 打开 **Obsidian** 软件（需先安装，本机目前**未安装**）；
2. 点击 **“打开文件夹作为库 (Open folder as vault)”**；
3. 选择本工作台根目录：`E:\zaiwu\个人工作台`；
4. 即可在 Obsidian 中直接查阅、编辑文献精读卡片、双向链接和 LaTeX 物理公式渲染。
   - 主看板入口：[[00_Dashboard/主工作台看板]]
   - 顶会与学术资源：[[00_Dashboard/常用微电子科研资源]]
   - 复现标准规范：[[02_Reproduction/00_复现标准Checklist]]
   - 避坑排错日记：[[03_Knowledge/踩坑与排错日记]]

> 注：`00_Dashboard/`、`01_Literature/` 等笔记目录属于本地私有数据，已被 `.gitignore` 排除，
> 因此 **GitHub 上看不到这些目录**，克隆仓库 ≠ 恢复笔记。

---

### 方式二：双击运行本地 Web 辅助控制台 (推荐)
双击运行根目录下的 **`start_workbench.bat`**（桌面快捷方式「MicroBench 工作台」执行的就是它）：
1. 脚本会自动切到项目根目录、校验依赖、启动本地服务；
2. **等服务真正就绪后**再自动打开浏览器到 **`http://127.0.0.1:5000`**，不会撞上"连接被拒绝"；
3. 若 5000 端口已被本工作台占用，则直接开浏览器，不会重复起进程；
4. **核心功能**：
   - 📖 **文献一键抓取与建档**：输入 arXiv ID 或 DOI（如 `2303.15982` 或 `10.1038/nature14539`），一键自动获取标题、作者、摘要并生成结构化 Markdown 精读卡片；
   - 📂 **从仓库论文库直接开读**：下拉框一键列出 `01_Literature/**/01_论文原文_PDF/` 里的论文，选中即进入双栏精读（**不复制文件**）；也可用深链 `http://127.0.0.1:5000/?open_pdf=<vault 相对路径>` 直接打开某篇；
   - 🧪 **复现工程脚手架生成**：输入项目名称，自动生成包含 `configs/`、`scripts/`、`data/`、`plots/` 与基准对齐 `README.md` 的标准化工程；
   - 🛠️ **科研避坑速记**：随时沉淀遇到的 TCAD 收敛发散、EDA 库依赖、CUDA 与 Linux 环境报错；
   - 📊 **一键生成组会周报**：自动汇总近 7 天的工作进展，生成规范 Markdown 周报草稿。

> **模板从哪来**：`_Templates/` 属于你的 Obsidian vault（被 gitignore），但 `microbench/templates/`
> 里随代码带了一份**同款副本**。vault 里没有 → 自动用包内副本（全新克隆不会 500）；
> vault 里有 → 用你的版本，服务启动时只补缺失的，**绝不覆盖你改过的模板**。

> **AI 功能需要配置模型 Key**：复制根目录 `.env.example` 为 `.env`（本机已复制好），
> 填入 `LLM_API_KEY`（主模型）或 `AGNES_API_KEY`（备用模型）后重启服务即可启用文献解读与翻译。

### 🤖 模型选择与「直译」参数

实测口径：2015 IEEE 双栏论文**整页**（2627 字符，pdfplumber 抽取），
走真实 HTTP 接口，统计端到端耗时与译文完整性。

| 模型 | 思考模式 | 整页直译实测 |
| --- | --- | --- |
| **MiniMax-M3** | 可关（`thinking.type=disabled` 支持） | ✅ **10~12s**，译文完整、无思考泄漏、无中英混杂 —— 直译首选 |
| MiniMax-M3.1-Flash | **强制 adaptive，不可关闭**（传 `disabled` 直接 400） | ⚠️ 45s；推理会把输出预算吃光，预算给小了正文被截断（代码已自动加大预算并检测截断） |
| MiniMax-M2.7 | 传 `disabled` 会被**静默忽略** | ⚠️ 13~25s，译文完整但 content 里混着思考文本；部分账号已不再提供该模型 |
| Agnes 3.0 Flash（备用） | 无思考 | ✅ 兜底可用，20 RPM 限速 |

> **为什么「慢 + 乱」**：MiniMax 的**思考 token 与正文共用同一个 `max_tokens`
> 预算池**。预算给小了，推理先吃光，正文就断在半句，而前端仍显示「已翻译」。
> M3.1-Flash 强制思考，最容易踩这个坑。

推荐配置（`.env`）—— **读论文和做推理解耦**：

```ini
LLM_BASE_URL=https://api.minimaxi.com/v1  # 国内站；国际站用 api.minimax.io
LLM_MODEL_NAME=MiniMax-M3                 # 主模型：解读 / 答疑
LLM_TRANSLATION_MODEL=MiniMax-M3          # 翻译专用：批量直译
LLM_TRANSLATION_DISABLE_THINKING=1
LLM_TRANSLATION_DEADLINE=180               # 单次翻译请求的总时限（秒）
```

- `LLM_TRANSLATION_MODEL` 留空 = 翻译跟随主模型。
- `LLM_TRANSLATION_DISABLE_THINKING=1` 只作用于翻译类请求；问答/解读仍保留模型的推理能力。
- 若把翻译模型换成 M3.1-Flash，代码会自动忽略 `disabled` 参数（模型会拒绝）、
  自动加大输出预算、并在正文被截断时**报错而不是把残缺译文当成成功**。
- **总时限**：单次翻译请求最多跑 `LLM_TRANSLATION_DEADLINE` 秒（默认 180，可调 30~900）。
  这个时限覆盖整条链路——分块之间的等待、模型重试、截断后抬预算重试，都算在里面。
  超时的行为**不是报错**，而是返回**已经翻完的部分**并标 `partial`，前端会显示
  「已翻译 N/M 段 · 超出时限」，再点一次翻译可以接着翻。
- 翻译结果按 (章节, 文本哈希, 模型) 缓存。换模型或改提示词后缓存键会变，
  不会命中旧结果；但**只改代码不换模型**时，旧译文仍在内存缓存里，
  重启服务即可清空。

### 🔍 翻译完整性：为什么「有的翻译有的不翻译」被治好了

模型「整段漏译」是提示词管不住的行为，所以校验放在**服务端**：

1. 提示词要求**逐句翻译**，并点名「页眉版权行 / 作者单位 / 章节标题 / 图表说明 / 编辑收稿信息」必须译；
2. 译完扫描译文，找出**成段没翻译的英文**（判据：连续 ≥8 个英文词且含小写实词，
   避开人名机构名与图表引用的误报）；
3. 检出漏译就带着**漏译清单**重译一次；重译失败保留第一版，不让整个请求失败；
4. 结果里带 `untranslated_runs` 计数（正常为 0）。

配套的两个细节：

- **「重新翻译」必须真的重译**：翻译端点带 `force` 参数跳过读缓存。
  没有它时，用户点了「重新翻译」服务端直接命中旧缓存返回同一份坏译文，
  看起来像按钮没生效。
- **翻译用 `temperature=0`**：同一段原文每次都该得到同一份译文，否则措辞一变，
  混着未译的元信息，看着就像「时翻时不翻」。

---

## 📱 平板版（iPad / Android 平板）

工作台有**两个**平板入口，取决于平板在不在你的电脑旁边：

| | 实时版（LAN） | 离线阅读版 |
|---|---|---|
| **启动方式** | 双击 `start_workbench_lan.bat` | 双击 `start_offline_sync.bat` |
| **需要电脑开着** | ✅ 必须 | ❌ 不需要 |
| **需要同一局域网** | ✅ 必须（同一个 Wi-Fi） | ❌ 不需要，文件带在平板上 |
| **能做什么** | 全功能：抓论文、AI 解读/翻译、建卡、生成复现工程、周报 | 只读：看全部笔记与公式、全文搜索 |
| **拿到最新内容** | 实时 | 需重新导出（见下） |

### 实时版（电脑在旁边时用）

1. 双击 **`start_workbench_lan.bat`**，窗口里会打印：
   ```
   [推荐] 一键配对 (自动填入口令):
      http://192.168.1.101:5000/?k=XXXXXXXXXXXX
   ```
2. 平板连**同一个 Wi-Fi**，用 Safari 打开上面那行带 `?k=` 的地址 → 自动完成配对，浏览器会记住口令，之后直接开 `http://192.168.1.101:5000/` 即可；
3. **iPad**：分享按钮 → 添加到主屏幕，即可全屏运行（已配置 PWA manifest + standalone 模式）；
   **Android**：Chrome 菜单 → 添加到主屏幕；
4. 页面已针对平板适配：触控目标 ≥44pt、安全区避让、竖屏单栏、阅读栏宽限制。

> **关于口令**：工作台开到局域网后，同一个 Wi-Fi 下任何设备只要知道地址就能访问——能读你的全部笔记、写入文件、消耗你的 API 额度。所以这里加了一道**一次性口令**：
> - 口令由 `logs/lan_token.txt` 持久保存，**重启不变**，平板只需配对一次；
> - 配对后存进 **HttpOnly + SameSite=Lax** 的 Cookie，页面脚本读不到，跨站请求也不会带上；
> - 本机（`127.0.0.1`）访问**完全不受影响**，不需要口令，桌面用法与以前一模一样；
> - 端口不对外时，口令存在也不会有人能连上——两道锁是独立的。

> **连不上怎么办**：99% 是 Windows 防火墙。启动脚本会检测并提示放行命令（需管理员身份）：
> ```
> netsh advfirewall firewall add rule name="MicroBench 5000" dir=in action=allow protocol=TCP localport=5000 profile=private
> ```

### 离线阅读版（没带电脑时用）

把整个 vault 烘焙成**一个自包含的 HTML 文件**（`MicroBench_离线阅读版.html`）：

- **零外部依赖**——KaTeX 公式在导出时就渲染成静态 HTML，字体以 base64 内嵌，没有一个网络请求，飞机上、地铁上、图书馆都能开；
- 公式渲染规则与在线版**完全一致**（同一套 `$$`/`$` 提取逻辑）；
- 目录树导航 + 全文搜索 + 深色模式 + 触控适配；
- 1.3 MB 一个文件，AirDrop / 微信文件传输 / U 盘都能拷。

**保持内容最新**（这是离线版唯一需要操心的点）：

```bash
# 手动导出一次
python microbench/export_offline.py

# 或者常驻监视,笔记一改就自动重导 (每 60 秒检查)
python microbench/export_offline.py --watch

# 输出到云盘目录 —— 平板用云盘 App 直接打开,永远是最新的
python microbench/export_offline.py "D:/OneDrive/MicroBench.html"
```

双击 **`start_offline_sync.bat`** 更省事：把一个云盘文件夹拖到它上面，它会先导一次然后常驻监视，之后你只管写笔记。

> **能不能在平板上编辑？** 离线版是只读的。想在平板上**读 + 改**，把整个 vault 放进 OneDrive / 坚果云 / Obsidian Sync，用 iPad 版 Obsidian 打开——Obsidian 原生支持 LaTeX 公式和双链，体验比这个 HTML 还好。代价是需要处理同步冲突。

---

## 📁 目录结构与规范说明

```text
个人工作台/
├── 00_Dashboard/               # 工作台主看板、科研资源与常用入口
│   ├── 主工作台看板.md          # 任务追踪与重点聚焦看板
│   └── 常用微电子科研资源.md    # IEDM/ISSCC/DAC/开源PDK/物理常数速查
│
├── 01_Literature/              # 结构化文献库（按学科方向归类）
│   ├── 01_Device_TCAD_器件仿真/ # GAA-FET, FinFET, 2D晶体管, 宽禁带器件
│   ├── 02_Circuit_EDA_电路与算法/ # 布局布线, STA时序分析, 模拟/数字IC
│   ├── 03_Materials_Physics_材料物性/ # 二维材料, 铁电材料, 氧化物半导体
│   └── 04_Survey_综述/          # 权威前沿综述与教程
│
├── 02_Reproduction/            # 论文与实验复现工程库
│   ├── 00_复现标准Checklist.md  # 避开复现黑洞的黄金准则
│   ├── repro_cfet_sub3nm/      # 示例 TCAD 仿真复现工程
│   └── repro_openroad_sky130/  # 示例 EDA 算法/芯片后端复现工程
│
├── 03_Knowledge/               # 硬核技术与踩坑积累
│   └── 踩坑与排错日记.md        # 记录环境冲突、License报错与求解不收敛解决方案
│
├── 04_Weekly_Reports/          # 历次组会周报沉淀归档
│
├── _Templates/                 # Obsidian 专业模版库 (本地私有, 服务启动时自动补齐)
│   ├── 01_Literature_Note_Template.md      # 微电子专属精读卡片模版
│   ├── 02_TCAD_Reproduction_Template.md    # TCAD 物理仿真复现模版
│   ├── 03_EDA_Circuit_Reproduction_Template.md # IC设计与EDA复现模版
│   ├── 04_Weekly_Report_Template.md        # 面向导师汇报的周报模版
│   └── 05_Troubleshooting_Template.md      # 避坑排错日记模版
│
├── microbench/                 # 本地 Web 工作台源码 (FastAPI + 极简前端)
│   ├── app.py                  # 后端 API + 鉴权/跨站守卫
│   ├── lan.py                  # 局域网模式: 口令与 IP 解析
│   ├── export_offline.py       # 导出单文件离线阅读版
│   ├── translation.py          # 翻译管线: 分块 / 拼接去重 / LaTeX 清洗 / 漏译检出
│   ├── templates/              # 随代码走的模版副本 (vault 缺模板时的兜底)
│   ├── static/
│   │   ├── index.html          # 主界面 (单文件前端: 工作台 + PDF 双栏阅读器 + AI 答疑)
│   │   ├── pair.html           # 平板配对口令页
│   │   ├── manifest.webmanifest# PWA 清单
│   │   └── vendor/             # 本地化的 PDF.js / KaTeX（离线可用，见下方说明）
│   └── _test_lan.py            # 局域网模式回归测试
├── assets/                     # 桌面快捷方式图标 (microbench.ico)
├── requirements.txt            # Python 轻量依赖
├── .env                        # 模型 API Key 配置（已 gitignore，勿提交）
├── start_workbench.bat         # Windows 一键启动脚本 (仅本机，桌面快捷方式指向此文件)
├── start_workbench_lan.bat     # Windows 一键启动脚本 (平板 / 局域网)
├── start_offline_sync.bat      # 离线阅读版同步器 (可导出到云盘)
└── MicroBench_离线阅读版.html   # 离线快照 (单文件, 平板可离线阅读)
```

### 🌐 前端依赖已本地化（无需外网）

PDF 双栏阅读器用的 PDF.js、公式渲染用的 KaTeX 原本从 `cdn.jsdelivr.net` 加载，
该域名在部分网络环境下不可达，会导致阅读器直接报「PDF.js 未加载」。

现在两者都随仓库分发在 `microbench/static/vendor/`，由 FastAPI 的 `/static/vendor`
挂载提供（只挂 vendor 子树，其余静态文件仍走白名单路由）：

```text
microbench/static/vendor/
├── pdfjs/    pdf.min.js / pdf.worker.min.js / pdf_viewer.css / images/
└── katex/    katex.min.js / katex.min.css / fonts/ (woff2 + woff + ttf)
```

重新生成或升级版本（vendor 文件已随仓库提交，通常无需手动操作）：

```bash
npm install katex@0.16.11 pdfjs-dist@3.11.174 --prefix microbench/_vendor --registry=https://registry.npmmirror.com
# 然后把 node_modules/katex/dist → microbench/static/vendor/katex/
#      node_modules/pdfjs-dist/build|web → microbench/static/vendor/pdfjs/
```

`index.html` 采用「本地优先 + CDN 兜底」：本地文件缺失（全新克隆未执行上述脚本）
时仍会回退到 CDN，不会因为目录变动再次出现空白阅读器。

---

## 🧪 开发与自检

```bash
pip install -r requirements.txt          # 含 pytest / markdown 等全部依赖

python -m pytest tests -v                # 端点 / 安全 / 翻译鲁棒性 (161 项)
python -m microbench._test_regressions    # 回归守卫 (40 项)
python -m microbench._test_lan            # 局域网模式守卫 (39 项)
python -m microbench._test_offline_export # 离线导出 (需先生成离线 HTML)
```

> 测试用的 vault 是**临时目录**（`tests/conftest.py` 的 `sandbox` fixture），
> 不会写到你的真实 vault；真实 vault 只被只读用例碰。

---

## 💡 研途建议（给西交大电子科学大四研究生的建议）
1. **先对齐 Baseline，再谈创新**：拿到开源代码或论文，先用作者的参数跑出第一个图，确认误差小于 5%，再在复现分支上做改进；
2. **所有环境严格隔离**：严禁在全局 `base` 环境中直接 `pip install`，每个复现工程单独 `conda create -n repro_xxx`；
3. **微记录代替突击准备**：每天用 Web 控制台或 Obsidian 记录 1~2 条实验进展或避坑经验，每周五导出周报只需 1 分钟。
