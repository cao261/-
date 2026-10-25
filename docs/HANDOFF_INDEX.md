# 交接文档索引（对方设备 / 平板设备都先看这份）

> **你不需要读全部 5 份** —— 按你负责的部分挑。
> 所有文档都在 GitHub 仓库里，`git pull` 就能拿到（`auto_sync.py` 每 2 小时也会自动同步）。
> ⚠️ 交接走仓库，**没有会话通道**（`session send` 只能投本机会话）→ 改完记得推 GitHub。

## 阅读顺序（按你的角色选）

### 我负责桌面端（另一台电脑的 agent）
1. **[`HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md`](HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md)** ← **最新、最重要**
   浏览器实测（真人级点击）抓出的 5 个真 bug + 安全审计的 5 个问题 + 降格提示改造。
   含：PDF 模式整条工具条消失、AI 活动「取消」够不着、429 报错说人话、
   密钥脱敏盲区、离线导出 XSS。
2. [`HANDOFF_2026-10-25-AI-ACTIVITY-AND-GLYPHS.md`](HANDOFF_2026-10-25-AI-ACTIVITY-AND-GLYPHS.md)
   AI 活动可视化（徽标/浮层/取消）+ 符号认字（× ≈ ≥）的实现要点。
3. [`LLM_CONTRACT.md`](LLM_CONTRACT.md) —— 改模型层前必读（换模型/降级/速率/表格读图）。
4. [`READER_CONTRACT.md`](READER_CONTRACT.md) —— 改阅读器前必读（页内顺序/公式/批注）。

### 我负责平板端（Android/iPad 那台）
1. **[`HANDOFF_2026-10-25-TABLET-ADAPT.md`](HANDOFF_2026-10-25-TABLET-ADAPT.md)** ← **先看这份**
   桌面端这轮做的哪些事**平板端要抄、哪些抄不了、哪些不用抄**。
2. [`HANDOFF_TABLET_2026-10-25.md`](HANDOFF_TABLET_2026-10-25.md) +
   [`HANDOFF_TABLET_2026-10-25-B.md`](HANDOFF_TABLET_2026-10-25-B.md) —— 平板端此前的两轮交接。
3. [`AUDIT_2026-10-25.md`](AUDIT_2026-10-25.md) —— 第二轮全面审计报告。
4. **平板端必须自查的两件事**（在 `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` §8）：
   - 离线导出 HTML 的 XSS 面（平板走**另一套** JS 消毒器，桌面端的修复不过去）；
   - 密钥脱敏的盲区（JSON 形态的 key、裸 `Authorization`、URL userinfo…）。

### 跨设备同步（谁都要看）
- [`SYNC_CONTRACT.md`](SYNC_CONTRACT.md) —— **红线清单，改动即破坏**。

---

## 文档状态表

| 文档 | 日期 | 解决什么问题 | 状态 |
|---|---|---|---|
| `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` | 10-25 晚 | 浏览器实测抓出的 5 个 bug + 安全 5 项 + 降格分类 | ✅ **当前最新** |
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

- 桌面端 agent → 读 `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` 的
  §0（三个最该知道的）、§2（PDF 模式工具条消失）、§3（AI 活动可见性）、
  §4（429 人话化）、§8（安全自查）、§9（降格要说真话）。
- 平板端 agent → 读 `HANDOFF_2026-10-25-TABLET-ADAPT.md` 全文 +
  `HANDOFF_2026-10-25-BROWSER-VERIFIED-FIXES.md` 的 §8（安全自查清单）。
