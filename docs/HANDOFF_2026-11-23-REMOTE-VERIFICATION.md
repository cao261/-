# HANDOFF · 2026-11-23 · 远端改动可用性验证（在 25353 台执行）

> **给 25353 那台设备的 agent / 用户**：这份只讲一件事 ——
> **把 `origin/main` 拉下来，确认 2026-11-22 那一轮的修复在你这台机器上真的可用。**
>
> 背景与完整审查记录见 [`HANDOFF_2026-11-22-INDEPENDENT-AUDIT.md`](HANDOFF_2026-11-22-INDEPENDENT-AUDIT.md)；
> 坑记在 `AGENTS.md` §4.129~4.132。

---

## 0. 先说清楚：为什么不是「我在你那边跑」

两台设备之间**没有会话通道**（`session send` 只能投本机会话），
而 **25353 没有绑定到 30461 这台桌面**（`session list --source ssh` 与 `--source rc` 都是空的）。
→ 我**没法直接在你那台上执行命令**。

所以本轮做了两件事：

1. **在 30461 这台，用「从 GitHub 干净克隆」代替「在你那台上拉」**：
   把 `origin/main` 克隆到临时目录 → 在克隆树里复跑本轮全部护栏 + 起服务实测。
   → **证明的是「远端代码本身是好的」**（这正是「远端改动可用」的核心）。
2. **给你一份可直接照抄的复跑清单**（本文 §2、§3），
   在 25353 那台跑一遍 → 证明「远端代码在你那台的环境里也是好的」。

---

## 1. 远端现在的状态（30461 实测）

| 项 | 值 |
|---|---|
| 远端地址 | `https://github.com/cao261/-.git` |
| `origin/main` | `965e4b9`（本轮追加提交后见 §4） |
| 干净克隆 HEAD | `965e4b9`（与本地 `origin/main` **完全一致**） |
| 本轮 18 个改动文件 | 本地 vs 克隆树 **SHA256 逐个一致，0 处不一致** |
| 克隆树 `git status` | 干净 |

---

## 2. 在 25353 那台拉取（**两条路选一条**）

### 路 A：让它自己的自动同步去做（**推荐，零操作**）

`microbench/auto_sync.py` 每 2 小时自动 `fetch + merge + push`。
→ **什么都不用做**，等它跑完就行（本轮修了它「读不到 `origin/main` 却报『已是最新』」
的静默失效问题，见 `AGENTS.md` §4.131 —— 所以现在它报成功就是真同步了）。

想立刻确认，在 25353 那台看它的日志里有没有这一行：

```
action=merged（或 up_to_date）  ← 且 remote 读得到 origin/main
```

### 路 B：手动拉（想立刻生效时）

```powershell
cd 'E:\zaiwu\个人工作台'          # 25353 上的仓库路径，按实际改
git fetch origin
git log --oneline -3 origin/main  # 确认拿到的是 §4 那一笔
git pull --rebase origin main
```

⚠ 25353 本地如果有**自己**的改动，先 `git status` 看一眼；
`--rebase` 遇到冲突就 `git add -A && git rebase --continue`。
两边改的文件一般不重叠（30461 改后端/抽取层/导出层，25353 改阅读器提示词那一片）。

### ⚠ 拉完**必须重启服务**

后端改动（`app.py` / `paper_text.py` / `export_offline.py`）**不重启不生效**；
前端（`microbench/static/index.html`）是 `no-store`，刷新浏览器即可。

---

## 3. 在 25353 那台复跑（复制粘贴即可）

### 3.1 本轮新增 / 扩大的 5 套护栏

```powershell
cd 'E:\zaiwu\个人工作台'
python microbench/_test_qa_refine_payload.py   # 期望：PASS 14 FAIL 0，变异捕获 7/7
python microbench/_test_readpos_resume.py      # 期望：PASS 24 FAIL 0，变异捕获 13/13
python microbench/_test_tablet_export.py       # 期望：PASS 48 FAIL 0
python -m pytest tests/test_blocking_endpoints_off_event_loop.py -q   # 期望：7 passed
python -m pytest tests/test_files_meta_cas.py -q                     # 期望：3 passed
python -m pytest tests/test_qa_scope_router.py -q                   # 期望：8 passed
python -m pytest tests/test_audit_2026_10_27_dataplane.py -k origin_main -q  # 期望：1 passed
python -m pytest tests/test_source_hygiene.py -q   # 期望：678 passed（行尾/坏字符/源码卫生）
```

### 3.2 全量

```powershell
python -m pytest tests -q -m "not slow"
```

**期望：1654 passed / 1 skipped / 12 deselected**（`pytest.ini` 已默认排除 slow，不用再带 `-m`）。

### 3.3 真机端到端（一条命令）

仓库里带了个自检脚本，**两台设备都能直接跑**：

```powershell
cd 'E:\zaiwu\个人工作台'
python microbench/_verify_remote.py --port 5000            # ① git 对齐 ② 每篇论文 outline
python microbench/_verify_remote.py --port 5000 --guards   # ③ 再把 5 套护栏跑一遍
```

它逐项给结论（不是「跑完了」，是「过没过」）：

```
[1] git：本地与 origin/main 对齐了吗
  PASS  本地 HEAD  [965e4b9]
  PASS  HEAD == origin/main  [一致]
  PASS  工作区干净
[2] 服务（127.0.0.1:5000）：每篇论文的 outline 都要 200
        200  01_Literature/.../2015_Control_of_Magnetic_Relaxation.pdf  (sections=2)
        ...
  PASS  outline 全通（27/27 篇）
```

**期望：27 篇全部 200**（修复前有 **11 篇必 500**）。
退出码 0 = 全过；1 = 有项目没过。

---

## 4. 本轮追加的那一笔（远端会比 §1 的 `965e4b9` 更新）

提交标题：`test(远端验证): 修「缺本地数据就误报红」的两条护栏 + 清掉 7 处 U+FFFD 坏字符`

改动只有三类，**都不碰业务逻辑**：

1. **`tests/test_llm_thinking_params.py`** — DeepSeek 那条护栏自带假 key
   （原来靠 `.env` 的 `LLM_API_KEY` 兜底 → 干净 clone / 没配 key 的机器必红）。
2. **`microbench/_test_tablet_hardening.js`** — 判据从「目录在不在」改成
   「有没有 `.json` 快照文件」；没有就 **SKIP**（原来 `logs/_pdf_snapshots` 目录空着就判红）。
3. **7 处 U+FFFD 坏字符**（`README.md` / `llm.py` / `app.js` / `styles.css` /
   `DavClient.java` / 两个 JS 测试）—— 全是**注释和文档**里的乱码，不影响任何行为。

> ⚠ **有 4 处 U+FFFD 是故意留着的**：`microbench/replaced_pdf.py` 与
> `tests/test_replaced_pdf.py` 里的「为避免混淆�/T18⟧」是**刻意构造的坏字符样本**
> —— 抽取层偶尔吐半截标记、`⟦` 变成坏字符，清洗逻辑要**认得它**才能清掉。
> **别"顺手修好"**，修了会让 `clean_translation` 的清洗失效。坑记在 `AGENTS.md` §4.132。

---

## 5. 克隆树复跑时踩到的环境事实（**这些本地的东西不入库**）

干净 clone 之后，下面三样是**空的 / 不存在**的 —— 它们都被 `.gitignore` 排除，
是**每台机器自己的**东西（不是 bug）：

| 东西 | 在哪 | 影响 |
|---|---|---|
| vault 内容 | `00_Dashboard/` `01_Literature/` `02_Reproduction/` `03_Knowledge/` `04_Weekly_Reports/` | 没有它 → 导出器报「没有找到任何笔记」；护栏大量 skip |
| 密钥 | `.env`（根目录） | 没有它 → DeepSeek/Agnes 自定义模型「没有配置 API key」 |
| 译文快照 | `logs/_pdf_snapshots/*.json` | 没有它 → 平板 hardening 套件的 [6][8] 两节 **SKIP**（2026-11-23 已改成跳过而不是判红） |
| KaTeX | `microbench/_vendor/node_modules/` | 没有它 → 导出器报「缺 KaTeX 资源」，**导出测试会失败** |

→ **25353 那台本来就有这些**（它一直在用），所以复跑应当是全绿。
→ 如果哪台机器报「缺 KaTeX」，先 `cd microbench/_vendor && npm install`。

---

## 6. 本轮**没做**的事（别当漏做）

- **平板 HTML / APK 没重打**（用户 2026-11-23 拍板 hold）：
  旧的 keystore `android/microbench-tablet.jks` **已丢失** → 重打 = **新证书**
  → 平板必须**卸载重装** → **本地笔记/批注会丢**。
  签名证书 SHA-256 是 `ac50efaf39045a110f60e9922848f0ad61cf689dc59c3ebd2b5de2731e964a55`
  （见 `android/SIGNING.md`）。
- **没动坚果云**：本轮全程只写本机 / 仓库，没有碰共享目录的数据。
- 平板端那 6 项能力差距仍是**主动搁置**（2026-11-18 用户拍板）。
