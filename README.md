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
├── requirements.txt            # Python 轻量依赖
└── start_workbench.bat         # Windows 一键启动脚本
```

---

## 💡 研途建议（给西交大电子科学大四研究生的建议）
1. **先对齐 Baseline，再谈创新**：拿到开源代码或论文，先用作者的参数跑出第一个图，确认误差小于 5%，再在复现分支上做改进；
2. **所有环境严格隔离**：严禁在全局 `base` 环境中直接 `pip install`，每个复现工程单独 `conda create -n repro_xxx`；
3. **微记录代替突击准备**：每天用 Web 控制台或 Obsidian 记录 1~2 条实验进展或避坑经验，每周五导出周报只需 1 分钟。
