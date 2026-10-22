# MicroBench | 微电子与半导体科研复现工作台

> 面向西安交通大学电子科学与技术专业（微电子 / 集成电路设计 / EDA / 半导体器件与TCAD物理仿真）定制的个人学术工作台。

---

## 🚀 极速上手使用指南

本工作台采用**“双模协同”**架构，既是标准的 **Obsidian 双链知识库**，也是一个**本地可运行的自动化科研辅助工具**。

### 方式一：直接在 Obsidian 中打开使用
1. 打开 **Obsidian** 软件；
2. 点击 **“打开文件夹作为库 (Open folder as vault)”**；
3. 选择本工作台根目录：`d:\Desktop\杂物\琐事\个人工作台`；
4. 即可在 Obsidian 中直接查阅、编辑文献精读卡片、双向链接和 LaTeX 物理公式渲染。
   - 主看板入口：[[00_Dashboard/主工作台看板]]
   - 顶会与学术资源：[[00_Dashboard/常用微电子科研资源]]
   - 复现标准规范：[[02_Reproduction/00_复现标准Checklist]]
   - 避坑排错日记：[[03_Knowledge/踩坑与排错日记]]

---

### 方式二：双击运行本地 Web 辅助控制台 (推荐)
双击运行根目录下的 **`start_workbench.bat`**：
1. 脚本将自动检测环境并启动本地服务；
2. 默认浏览器将自动打开 **`http://127.0.0.1:5000`**；
3. **核心功能**：
   - 📖 **文献一键抓取与建档**：输入 arXiv ID 或 DOI（如 `2303.15982` 或 `10.1038/nature14539`），一键自动获取标题、作者、摘要并生成结构化 Markdown 精读卡片；
   - 🧪 **复现工程脚手架生成**：输入项目名称，自动生成包含 `configs/`、`scripts/`、`data/`、`plots/` 与基准对齐 `README.md` 的标准化工程；
   - 🛠️ **科研避坑速记**：随时沉淀遇到的 TCAD 收敛发散、EDA 库依赖、CUDA 与 Linux 环境报错；
   - 📊 **一键生成组会周报**：自动汇总近 7 天的工作进展，生成规范 Markdown 周报草稿。

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
├── _Templates/                 # Obsidian 专业模版库
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
│   ├── static/
│   │   ├── index.html          # 主界面 (单文件前端)
│   │   ├── pair.html           # 平板配对口令页
│   │   └── manifest.webmanifest# PWA 清单
│   └── _test_lan.py            # 局域网模式回归测试
├── requirements.txt            # Python 轻量依赖
├── start_workbench.bat         # Windows 一键启动脚本 (仅本机)
├── start_workbench_lan.bat     # Windows 一键启动脚本 (平板 / 局域网)
├── start_offline_sync.bat      # 离线阅读版同步器 (可导出到云盘)
└── MicroBench_离线阅读版.html   # 离线快照 (单文件, 平板可离线阅读)
```

---

## 💡 研途建议（给西交大电子科学大四研究生的建议）
1. **先对齐 Baseline，再谈创新**：拿到开源代码或论文，先用作者的参数跑出第一个图，确认误差小于 5%，再在复现分支上做改进；
2. **所有环境严格隔离**：严禁在全局 `base` 环境中直接 `pip install`，每个复现工程单独 `conda create -n repro_xxx`；
3. **微记录代替突击准备**：每天用 Web 控制台或 Obsidian 记录 1~2 条实验进展或避坑经验，每周五导出周报只需 1 分钟。
