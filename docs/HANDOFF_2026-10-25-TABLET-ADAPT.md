# 交接说明：平板（APK）适配侧需要知道的（2026-10-25 下午这轮）

> **怎么用这份文件**：贴给平板那台设备上的 agent，或让它读仓库里的这个文件。
> 对方 `git pull` 就能拿到本文件。
>
> 桌面端（25353）这轮做了三件事，**但平板端是另一套代码**，
> 桌面的改动**不会**自动到平板 —— 下面逐条说清哪些要抄、哪些不用抄、哪些抄不了。

---

## 0. 先说架构事实（决定了什么能抄）

| | 桌面端 | 平板端 |
|---|---|---|
| 前端 | `microbench/static/index.html`（单文件，~9000 行） | **`microbench/static/tablet/app.js`（独立实现，130KB）** |
| 怎么调模型 | 走本机服务端 `microbench/app.py` | **直连 LLM API**（`cfg.base` / `cfg.key` / `cfg.model`） |
| 抽取原文 | 服务端 pdfplumber | **前端 PDF.js**（`getTextContent`） |
| 同步 | 坚果云（服务端） | **坚果云 WebDAV**（原生桥） |
| 批注 | 原文坐标框 + 译文文字高亮 | **只画原文坐标框**（`renderAnnoInto`, app.js:2347） |
| 翻译粒度 | 整页 / 整章 | **选段翻译**（先选中文字） |

**最关键的一条**：桌面的 `index.html` 改动**一行都不会**自动到平板。
平板要自己实现。

---

## 1. 桌面这轮做了什么（供对照，不要照抄）

| # | 桌面的改动 | 平板要不要抄 |
|---|---|---|
| ① | 批注重译后不丢：标「⚠ 位置失效·笔记已保留」 | **不用抄** —— 平板批注只画原文坐标框（`a.rects`），不靠引文文本定位，重译动不了它 |
| ② | 标注列表按当前页定位（抽屉滚到本页第一条） | 不用抄（平板没有"译文里包高亮"这回事） |
| ③ | 「AI 活动」可视化：统一登记 + 每 500ms 刷新「已用时+阶段」+ 面板 + 真取消 | **建议抄最轻的一层**（见 §2 ①） |
| ④ | 换模型：`.env` 配 `LLM_CUSTOM_N_*` → 下拉框里出现；主模型 429 自动降级；限流按模型配 | **要抄降级这一条**（见 §2 ②） |
| ⑤ | 解不出的数学符号（× ≈ ≥）裁图认字 | **大概率不用抄**（见 §2 ③） |

---

## 2. 平板侧真正该做的三件事（按性价比排序）

### ① 等待期加"已用时"（小，30 分钟）

平板的 `aiAppendAnswer()` 给了一个 status 行，结束时写
`"完成 · model"` / `"上游只返回了思考过程…"` / `"失败：…"`（app.js:1851–1900），
**但等待期间没有已用时** → 用户还是会觉得"卡住了"（用户的原话：
"他有可能等了半天然后什么也没输出"）。

最小改法（照抄桌面的思路，不必搬整套面板）：

```js
// 发请求之前
const t0 = Date.now();
const timer = setInterval(() => {
  const s = Math.floor((Date.now() - t0) / 1000);
  status.textContent = `⏳ 思考中… · ${s}s`;
  if (s > 60) status.textContent = `⏳ 还在想… · ${s}s（可能模型在长思考，可点停止或换模型）`;
}, 1000);
// finally 里 clearInterval(timer)
```

桌面的完整版（任务登记处 + 浮层面板 + 取消）在
`index.html` 的「AI 活动登记处」那一段（搜 `_aiTaskStart`）。
平板屏幕窄，面板要做竖屏适配，**建议先只做上面这个轻量版**。

> 平板**已经有**停止按钮（`btn-ai-stop`，app.js:1853/1856 `aiAbort`）——
> 这部分不用重做。

### ② 429 自动换模型（★ 平板最该做的一条，1 小时）

平板**天然就有两个模型配置**：
- `cfg.modelT` —— 翻译用的快模型（默认 MiniMax-M3）
- `cfg.model` —— 解读/答疑用的推理模型（默认 MiniMax-M3.1-Flash）

这正好对应桌面端的"主模型 / 降级备用模型"。桌面的做法是
`llm.py` 的 `DualModelManager`：主模型 429 → 立刻降级到备用 →
每 15 分钟探测主模型 → 额度恢复自动升回。

平板可以照搬**最简版**：翻译时收到 429 → 自动换另一个模型重试一次 →
在 status 里说清楚「主模型额度用完，已换 XXX 重试」。

同时要**按模型限流**：桌面实测过 Agnes（免费版）只有 11 RPM，
付费模型（MiniMax / DeepSeek）30 RPM 够用。平板直连同样会打爆限额，
每分钟请求数要按模型卡住。

### ③ 符号 × ≈ ≥：**先实测，大概率不用做**

桌面端确实有这个问题（用户看到"×"变"?"），根因是
**pdfplumber 解不出 TeX 数学字体**（没有 ToUnicode 映射 → `(cid:3)`）。

但**平板走的是 PDF.js**。实测同一处公式：

- pdfplumber（桌面）：`(cid:1)(cid:2)(cid:3)…` → 解不出
- **PDF.js（平板）：`a_ME ¼ E=H`** → **解得出来**

所以平板上那些符号**多半是好的**。

**要做的只有一件事：先在平板上实测一眼**。找一篇有 "500 × larger" 的论文
（`01_Literature/04_Survey_综述/.../2008-JACERS-综述-*.pdf`），
选中那段，看选中内容里 "×" 是不是正常的。
- 正常 → **这块不用做**，白干
- 是空白/缺字符 → 那才需要处理，而且**只能做占位符版**：
  在送进模型的文本里用 `[?]` 标出来（和服务端一个道理），
  别让模型自己脑补。彻底修（裁图+多模态认字）成本高，不建议在平板做。

### ④ 重新导出 + 旧译文会被清空（必做，别当成 bug）

- 桌面端 `paper_text.EXTRACTOR_VERSION` 从 **4 提到 5**（符号锚点）→
  **旧的译文快照一律作废**
- 平板导出器 `export_offline.py` 是**动态读**这个版本号的
  （`current_extractor_version()`，export_offline.py:980）→
  **两端自动一致**，不用手改任何常量 ✅
- 但后果是：平板第一次导出会**没有译文**（旧快照被跳过）→ 需要重新翻一次。
  **这是预期行为，不是 bug**（AGENTS.md §4.18 讲的就是这个闸门）
- 交付物**只由构建脚本产出**：
  ```
  pwsh android/build_apk.ps1
  ```
  → 内部 `export_offline.py --tablet` 导出一次 → `Copy-Item` 到仓库根，
  两份**必须逐字节相同**。**别手工改 APK 里的 HTML**（AGENTS.md §4.17 的老教训）。

---

## 3. 不能照搬的（服务端专属，做了也白做）

| 桌面的能力 | 平板为什么用不上 |
|---|---|
| 符号裁图认字（`app.py` `_resolve_page_glyphs`） | 平板不经过服务端；它用 PDF.js（见 §2 ③） |
| 按模型限流表（`llm.py` `limiter_for_config`） | 平板直连，限流得自己在 `app.js` 里实现 |
| 主/备自动降级 + 15 分钟探测升回（`DualModelManager`） | 同上；平板用 `cfg.modelT` / `cfg.model` 自己实现最简版（§2 ②） |
| 快照版本闸门（`app.py` `extractor_version`） | 导出器已动态读版本 ✅；但**平板自己的本地快照**也要跟上 |
| 符号词典 `logs/glyph_dict.json` | 在 vault 的 logs/ 下，**不在坚果云同步根里** → 平板拿不到（除非把词典也放进同步根） |

---

## 4. 自检清单

```powershell
# 1) 拉代码
git pull --rebase origin main

# 2) 离线测试（桌面侧的护栏，平板代码改动也要让它保持绿）
E:\ruanjian\python.exe -m pytest tests -q -m "not network"     # 期望 305 passed / 5 skipped
node microbench\_test_reader_markdown.js
node microbench\_test_reader_annotations.js
node microbench\_test_reader_ai_activity.js

# 平板自己的护栏**已经收录在 pytest 里**，不用单独跑：
#   tests/test_tablet_frontend.py     → 自动跑下面三个 node 套件
#   tests/test_tablet_snapshot_version.py → 管版本闸门（导出只烤当前版本的译文）
#     - microbench/_test_tablet_renderer.js   渲染器（Markdown / 公式 / 消毒 / hash）
#     - microbench/_test_tablet_ai.js          AI 面板（发送后清空输入栏 / 多轮上下文 / Enter）
#     - microbench/_test_tablet_hardening.js  数据安全（种子不覆盖本地 / XSS / 嵌套公式）
# 想单独跑：node microbench\_test_tablet_ai.js

# 3) 重新构建 APK（交付物只能由脚本产出）
pwsh android/build_apk.ps1

# 4) 装到平板上，人工过一遍
#    - 选中一段文字 → 翻译 → 等待期能看到"· 12s"这样的已用时
#    - 主模型额度用完时 → 自动换另一个模型，且说清楚换了谁
#    - 选一篇有 "500 × larger" 的论文 → 选中那段，看 "×" 是不是正常的
#    - 批注在重译后仍然在（平板批注画在原文坐标上，不受译文变化影响）
```

> 改了 `app.js` 之后，**先跑 pytest**（它会自动跑平板的三个 node 套件），
> 再 `pwsh android/build_apk.ps1`。
> 交付物（仓库根那份与 APK 内嵌那份）必须**逐字节相同** ——
> 构建脚本会自动保证，**别手工改其中一份**（AGENTS.md §4.17 的老教训）。

---

## 5. 一句话总结

**桌面的三件事里，只有"等待期看得见"和"429 自动换模型"值得平板抄，
而且平板已经有停止按钮和两个模型配置，抄起来比桌面还简单；
符号那块平板大概率本来就是好的，先花两分钟实测再决定。**
