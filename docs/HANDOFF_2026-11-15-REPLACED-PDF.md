# 交接：中译替换版 PDF（2026-11-15）

> **这份文件给另一台设备（25353 / 30461）的 agent 看。**
> 关联：`AGENTS.md` §4.90（坑位）、§4.89（专注模式，本次同批改动）。
> 护栏：`tests/test_replaced_pdf.py`（20 条）、真机脚本 `microbench/_live_replaced_pdf_test.py`。

---

## 1. 用户诉求（原文）

> 「直接就是原文上直接把翻译打上去……这篇论文一共十页，然后我翻译完之后就变成二十页。
> 第一页就是原来的那个文章，第二页就是原来的那个字儿直接削去……然后第三页又是原来的第二页……
> 然后原本的图表还都保持不变。」

（用户明确说：这个功能**跟专注模式没关系**，是独立功能。）

**一句话**：把译文**直接打到原文页上**，输出一份「原文页 + 中文替换页」交替的新 PDF。

---

## 2. 用户怎么用

1. 打开一篇论文 → PDF 双栏视图 → 工具条点 **「🧾 中译替换版」**。
2. 3 秒左右生成 → 自动在阅读器里打开生成物。
3. 产物路径：
   `01_Literature/<主题>/05_译文替换版_PDF/<原文件名>_中译替换版.pdf`

**前提**：这篇论文得先用过「🔀 对照」并**保存快照**（段落级译文 + 坐标都在快照里）。
没有快照 → 端点返回 409（提示先做对照）。

**页面构成**：第 1 页 = 原文，第 2 页 = 第 1 页的中文替换版，第 3 页 = 原文第 2 页……
**没翻过的页只有原文页**（实测 JACERS 只翻了前 4 页 → 12 页输出 = 8 原文 + 4 替换）。

---

## 3. 实测数据（JACERS 综述，8 页原文）

| 指标 | 值 |
|---|---|
| 输出页数 | 12（8 原文 + 4 替换） |
| 段落替换 | 68 段 |
| 放不下保留英文 | 4 段（竖排水印窄条 1、图表轴标签 3） |
| 写入被拒 | 0 |
| 耗时 | 2.9~3.5 s |
| 文件大小 | ~1.0 MB（`subset_fonts()` 后） |
| 中文可检索 | ✓「材料」「理论」「复合材料」「磁致伸缩」全搜得到 |
| 豆腐块 | 0 |
| LaTeX 残留 | 0（`\` / `$` / 花括号均为 0） |
| 图表 | 替换页与原文页图片数一致（1/1、2/2、1/1） |

---

## 4. 代码地图

| 文件 | 作用 |
|---|---|
| `microbench/replaced_pdf.py`（新） | 构建器核心：`Block` / `BuildStats` / `CjkFont` / `clean_translation` / `fix_missing_glyphs` / `build_replaced_pdf` |
| `microbench/app.py` | `POST /api/paper/build_replaced_pdf` + `ReplacedPdfRequest` + `_default_replaced_out()`（在 `api_page_blocks` 之后） |
| `microbench/static/index.html` | 工具条按钮 `#pdf-replaced-toggle` + `buildReplacedPdf()` + `openPdfDirect()` + `_applyReplacedPdfView()` + `.pdf-split-single` |
| `tests/test_replaced_pdf.py`（新） | 20 条护栏 |
| `microbench/_live_replaced_pdf_test.py`（新） | 真机 Playwright 全链路（12 项断言） |

**关键坐标契约**：块 `bbox = [x0, top, x1, bottom]`（pt，左上角原点），
与 `⟦FORMULA⟧`/`⟦TABLE⟧` 标记、`/api/paper/formula_image` 裁图**同一坐标系**
（pdfplumber 与 PyMuPDF 都是左上角原点）。

**版本闸门**：端点比对快照的 `blocks_version` 与 `bilingual.BLOCK_TRANSLATION_VERSION`，
不一致 → 409（段落切分变了，旧译文坐标会错位）。

---

## 5. 坑（8 条，全部写进 AGENTS.md §4.90）

1. **字体要能「原样取回」** —— Noto Sans SC VF 把「料/理/墨」写成 U+F9BE（兼容表意文字），
   渲染正常但**搜不到「材料」** → `_font_roundtrips()` 自动拒用不合格字体。
2. **涂改范围要跟原页真实的行** —— 块 bbox 只反映首行起点 → 每行最左 2~3 个字母残留
   成一条竖排 'ert' 'in' 'ot'（JACERS 第 2 页右栏实测）。
3. **「藏掉右栏」≠「PDF 变宽」** —— `.pdf-split` 是两列 grid → 要加 `.pdf-split-single`。
4. **替换版不能进翻译队列**（12 页白花钱），右栏旧译文也要收起。
5. **雅黑没有上下标数字字形** → `Cr₂O₃` 会变 `CrO`，必须兜底。
6. **LaTeX 不能直接删**（`\alpha` → α）。
7. **放不下的块保留英文，绝不截断**。
8. **公式/表格块不替换**（图表保持原样）。

---

## 6. 验收清单（换设备后照着跑）

```powershell
# 1) 护栏
python -m pytest tests/test_replaced_pdf.py -q          # 期望 20 passed
python -m pytest tests -q -m "not slow"                 # 全绿（注意 -m）

# 2) 服务
Start-Process -FilePath (Get-Command python).Source -ArgumentList `
  '-m','uvicorn','microbench.app:app','--host','127.0.0.1','--port','5000','--log-level','warning' `
  -WorkingDirectory '<仓库根>' -WindowStyle Hidden

# 3) 真机全链路（Playwright）
python microbench/_live_replaced_pdf_test.py             # 期望「全部通过」12 项

# 4) 肉眼核对（务必看图，别只看断言）
#    输出页：原文页有英文、替换页有中文、公式/图表/封面渐变图都在、
#    右栏没有残留英文竖条、没有豆腐块。
```

### 已知红灯（**不是本次引入**）

`tests/test_apk_delivery.py::test_translations_are_baked_in` —— 红。
原因：仓库里**预构建的 APK 单文件**里只烤进了旧的 JACERS 译文，
而磁盘上已有更新的快照（`2008-等效电路综述-..._1uvepj7`，11-14 用户自己翻的）
没被烤进去 → 「当前版本的译文必须都烤进单文件」这条断言不成立。
**改动前的原始树上用 `git stash` 复跑同样红**（与本次无关）。
重新构建 APK 后即恢复。

⚠ **别在快照目录里留「中译替换版」的快照** —— 它会让这条护栏多红一条。
现在的实现已经挡住：替换版视图下**不启动翻译队列**、**不自动/手动存快照**。

---

## 7. 已知限制

- **依赖快照**：没做过「🔀 对照」的论文生成不了（409）。
- **只替换 body/heading**：公式与表格整块保留原样（用户要求如此）。
- **放不下的块保留英文**：竖排水印、个别脚注、图表轴标签这类窄条天然放不下。
- **字号折算**：中文 = 原文号 × 0.85（基线对齐），下限 0.62 倍且不低于 6pt。
- **段落里若含 LaTeX**：已做符号映射，但复杂矩阵仍可能以文本形式出现（公式块不替换）。
- **输出目录固定** `05_译文替换版_PDF/`（与 `01_论文原文_PDF/` 平级），**不覆盖源 PDF**。

---

## 8. 未提交

本轮改动**尚未 commit / push**（设备 30461，分支 `feat/audit-2026-10-27`）：

```
AGENTS.md
docs/HANDOFF_2026-11-15-REPLACED-PDF.md   （本文件）
microbench/app.py
microbench/static/index.html
microbench/replaced_pdf.py                 （新）
microbench/_live_replaced_pdf_test.py      （新）
tests/test_replaced_pdf.py                 （新）
```

同批还有专注模式的改动（§4.89）：`docs/HANDOFF_2026-10-27-ZEN-FOCUS-MODE.md`、
`microbench/_test_zen_mode.js`、`microbench/_live_zen_bar_test.py`、`tests/test_zen_mode.py`。

**两台设备都改过了** → 提交前先 `git pull --rebase origin main`（§2.7）。
