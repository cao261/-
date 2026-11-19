# 交接：答疑浮层的「键消失 / 拖动 / X 键」四条缺陷（2026-11-19）

> 这一轮的主线是用户附截图报的一组 UI 缺陷：
> **「ai 解答有时候会出现这种情况，就非常麻烦，因为那个错掉的键就没了，
> 虽然能随意缩放，但拖动有时候会出现问题，上面的 x 键也在某些比例下会消失」**
>
> 平板端本轮**不动**（用户 2026-11-18 已拍板）。
> 坑记在 `AGENTS.md §4.120~4.123`。

---

## 0. 一句话结论

用户报的是**三个症状**，实际是**四条独立根因**，全部修掉并有真机 + 真实渲染护栏盯着：

| # | 症状 | 根因 | 实测（改前 → 改后） |
|---|---|---|---|
| ① | **「错掉的键就没了」**（按钮一片空白） | 夜色主题 `.btn-pill` = 奶油底 + **纯白字**（`--text-light` 恒为白） | 对比度 **1.17:1** → **15.77:1** |
| ② | **「错掉的键就没了」**（按钮停在「⏳ 解答中…」） | 第二次请求把**上一轮注入的流式标签**当成原始标签；被别的入口顶掉的按钮因序号守卫**没人复位** | 标签永久错乱 → 复位为「发送提问」 |
| ③ | **「x 键会消失」** | 拖动/还原的横向钳制用**固定 60/80px**（面板 460px 宽 → 大半个在屏外），且**越界位置被持久化** | 面板右缘 2000（屏宽 1600）→ **1600** |
| ④ | **「拖动有时候会出现问题」** | 拖动结束浏览器补的 `click` 触发了「点竖条展开」 | 拖一下就弹开 → **仍是收起态** |

顺带修：**「✕ 取消」键点不动**（父按钮 `disabled` 的 `pointer-events:none` 传染给子按钮）。

---

## 1. 取证（不是猜）

### 1.1 像素分析用户截图

`tmp/analyze_btn_pixels.py` + `tmp/zoom_shot.py`：

```
按钮底色 ≈ #F1EDE2      ← 正好 = 夜色主题的 --fg-strong（#F0EDE6）
放大后：奶油底 + 白字 —— 四个字几乎看不见
```

### 1.2 真机量坐标

`tmp/live_qa_panel_bugs.py` / `tmp/live_qa_xkey.py`：

| 假设 | 实测 | 结论 |
|---|---|---|
| 面板太窄 → X 键被裁 | 宽 340→700，标题栏溢出 **0px** | ❌ 不是宽度问题 |
| 浏览器缩放比例 → X 键被裁 | 90%~150% 全部正常 | ❌ 不是缩放问题 |
| **拖到右边 → X 键出屏** | 面板左缘 1540 / 右缘 **2000**（屏宽 1600），X 键在 1983 | ✅ **就是它** |
| 从 localStorage 还原到靠右 | 面板 460 宽，左缘钳到 `innerWidth-80`=1520 → **仍有 380px 在屏外** | ✅ 而且**永久** |

### 1.3 按钮状态的实证

`tmp/live_qa_btn_stuck.py`：

```
同一按钮连问两次 → 跑完后 label='⏳ 解答中... ✕ 取消' disabled=False   ❌
章节视图按钮被答疑面板顶掉 → label='⏳ 解答中... ✕ 取消' disabled=True   ❌
```

---

## 2. 修了什么（全部在 `microbench/static/index.html`）

### ① 主按钮文字色 → `var(--surface-canvas)`

```css
.btn-pill {
  background: var(--fg-strong);
  color: var(--surface-canvas);   /* 改前是 var(--text-light) —— 恒为纯白 */
}
```
同族的 toast / 悬浮钮 / AI 活动条本来就是 `var(--surface-canvas)` —— 主按钮是唯一例外。

### ② 按钮的原始标签只认一次 + 顶掉旧请求时立刻复位

新增 `_qaAskOriginalHTML(btn)` / `_qaResetAskButton(rec)`：
- 原始标签存 `btn.dataset.askOriginalHTML`（整条生命周期只记一次）；
- `askLLMStream` 的「替换」分支里 **立刻** `_qaResetAskButton(old)`
  （否则旧请求的 `finally` 会因序号守卫跳过恢复 → 那个按钮永久卡死）；
- `finally` 里用 `_qaResetAskButton()` 而不是 `btn.innerHTML = originalHTML`；
- 注入的取消键自己写 `pointer-events:auto`。

### ③ 位置钳制按实际尺寸算 + 落盘前钳

新增 `_qaClampPanelX(panel, x)` / `_qaClampPanelY(panel, y)`：
- 上限 = `innerWidth - 面板实际宽度`（不是固定 60/80px）；
- 用 `offsetWidth`/`offsetHeight`（**布局尺寸**）——
  `getBoundingClientRect()` 会被入场动画 `scale(0.98)` 缩小 → 口径偏小；
- `saveReaderQAPanelGeometry` **落盘前先钳** → 越界位置永远不会被存下来。

### ④ 拖动结束的那一下 click 要抑制

`_qaGeo.drag` 记 `moved`（位移 > 3px 才算拖动）→ `mouseup` 时
`_qaGeo.suppressClick = !!moved` → `_qaCollapsedHeadClick` 看到这个标记就忽略这一次 click。

---

## 3. 护栏

### 新增 `microbench/_test_qa_panel_render.py`（**真实渲染**）

- 从 `index.html` 抽**真 CSS**（整个 `<style>` 块）+ **真 DOM**（`#reader-qa-panel` 整块）
  + **真 JS**（15 个函数）→ 拼一个探针页 → headless 浏览器**真渲染**；
- **两个主题各跑一遍页面**（⚠ 运行时改 `data-theme` 后 Chrome 不会重解析
  依赖该自定义属性的 `background-color` → 必须在加载时定死）；
- 18 条断言：对比度（两主题）/ 拖到最右 X 键仍在屏内 / 落盘几何在屏内 /
  收起竖条拖动不误展开 / 单击仍能展开 / 连问两次按钮复位 /
  被别的入口顶掉的按钮也复位 / 「✕ 取消」键真能点到；
- **变异自检 7/7 全被抓住**（含一条「整体回退到旧实现」的三点同时变异）。

已挂进 `tests/test_pdf_layout_guardrails.py` 的 `PY_SUITES` → 跟其他渲染护栏一起进 pytest。

```powershell
python microbench/_test_qa_panel_render.py    # 18 PASS + 变异 7/7，约 19 秒
```

### 真机脚本（临时，不在仓库）

```powershell
python 'C:\Users\30461\.minimax\tmp\verify_qa_panel_fix2.py'   # 0 失败项
```

> ⚠ **Playwright 路由队列是串行的**：一条「挂住」的请求不返回，
> 后面的请求根本发不出去 → 真机探针**测不了「请求重叠」那个场景**。
> 那条由浏览器护栏用**页面级 `fetch` 桩**覆盖（确定性、无网络）。

---

## 4. 复现命令

```powershell
# 护栏
python microbench/_test_qa_panel_render.py
python -m pytest tests -q -m "not slow"

# 真机
python 'C:\Users\30461\.minimax\tmp\verify_qa_panel_fix2.py'
python 'C:\Users\30461\.minimax\tmp\analyze_btn_pixels.py'   # 用户截图的像素取证
```

---

## 5. 下一台设备要注意的

1. **本轮只改前端** → 不需要重启服务（刷新即生效）。
2. **`.btn-pill` 的文字色如果有人改回 `--text-light`** → 夜色主题立刻退回 1.17:1
   → 护栏的对比度断言会红。
3. **`askLLMStream` 是 4 个入口共用的**（章节视图 / 跨章节 / 首次提问 / 带反馈重答）——
   改它的时候要想到**别的入口的按钮**（§4.121）。
4. **护栏的变异目标串必须唯一**（§4.119）—— 护栏里已加「出现次数 != 1 直接判失败」。
5. **平板端本轮没做** —— 用户 2026-11-18 已拍板，下一轮别再当"应该补"重复提。
