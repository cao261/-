# 平板端 · 手写与缩放修复（2026-10-28 用户实测）

> **用户原话**（2026-10-28）：
>
> 「实话实说，这个平板功能目前的这个功能已经很好了，但是这个手写这方面好像没有。
> 没有任何。作用。而且你这个加减就是这个缩放它还是一样。变大了之后就只能显示一部分就是动不了了。」
>
> **本轮把这两句话逐字拆开查了**，找到 **3 个真缺陷**（不是 1 个），
> 全部修完，并用 **headless Edge 真渲染**验证过（旧写法确实坏、现在确实好）。

---

## 0. 一句话

**手写「没作用」和缩放「动不了」是两个独立缺陷，但根因同源：
都在「触屏手势」和「版面尺寸」这两层，而这两层**从来没有护栏**。**

平板的 PDF 阅读器此前**没有任何自动化测试**（node 套件里只有渲染器/AI/安全三套），
所以这类问题能一路活到用户手上。

---

## 1. 缺陷 ①：缩放放大后「只能显示一部分，动不了」

### 根因（CSS 层面，实测复现）

`.tb-pdf-page` 原来是：

```css
.tb-pdf-page { ... overflow:hidden; max-width:100%; ... }
```

放大时原文 canvas 按 `zoom` 变宽（`viewport.width` 变大），
但**页卡被 `max-width:100%` 夹住** → `overflow:hidden` 把超出部分**裁掉**
→ `.tb-pdf-body` 的 `scrollWidth` 没变 → **横向滚不动** → 右半边永远看不到。

### 实测对照（headless Edge，同一份页面只换 CSS）

| | 旧 CSS | 新 CSS |
|---|---|---|
| 页卡宽度 | **585px**（内容 1800px 被裁成 1/3） | **1800px** |
| 原文实际可见宽度 | 585px（**被裁**） | 1800px |
| 滚动容器 `scrollWidth` | 605 → **滚不到右半边** | 1820 → **完整可滚** |
| 页卡 `overflow` | `hidden` | `visible` |

### 修法

```css
.tb-pdf-page {
  margin:0 0 14px; margin-left:auto; margin-right:auto;
  width:max-content; min-width:100%;      /* 跟着内容走 → 放大后自然超宽 */
  display:flex; flex-direction:column;
}
```

- 去掉 `max-width:100%` 与 `overflow:hidden`（圆角裁切不需要它）。
- 用 `width:max-content` 让页卡跟着原文 canvas 走。
- 居中改用 `margin-left/right:auto`（超宽时 auto 解析成 0 → 内容靠左 → 正好是要看的那侧）。
- 宽屏双栏时译文栏给**确定宽度**（`flex:0 0 auto; width:max-content`），
  否则会被 `max-content` 吃掉挤成一条。

---

## 2. 缺陷 ②：手写「没有任何作用」（三个原因叠加）

### 原因 A：触屏手势被浏览器抢走 ⭐ 主因

```css
.tb-pdf-ink.is-on { pointer-events:auto; cursor:crosshair; }   /* 原来只有这个 */
```

`pointer-events` 对了，但**缺 `touch-action:none`**：

手指按住画布拖动 → 浏览器判定为**滚动手势** → `pointermove` 被 `pointercancel` 打断
→ 这一笔只攒到 1~2 个点 → 被当废笔丢掉 → **画不出任何东西**。

```css
.tb-pdf-ink.is-on {
  pointer-events:auto; cursor:crosshair;
  touch-action:none;                        /* ⭐ 关键 */
  -webkit-user-select:none; user-select:none;
  -webkit-tap-highlight-color:transparent;
}
```

> 划词是**点击**触发的（不是拖动），所以 `touch-action:none` 不影响划词。

### 原因 B：手指稍一动就出边界 → 半笔被丢掉

原来监听 `pointerleave` 收笔。手指按住稍微一动就可能移出层边界
→ 提前收笔 → 那一笔只剩 1~2 个点 → 丢掉。

```js
// ⚠ 删掉 pointerleave 收笔
layer.addEventListener("pointerdown", (e) => {
  ...
  try { layer.setPointerCapture(e.pointerId); } catch (err) {}   // ⭐ 锁住这一笔
});
layer.addEventListener("pointerup", end);
layer.addEventListener("pointercancel", end);
// 画到层外面也继续画（capture 兜住）
```

同时收笔门槛从 `>= 4` 放宽到 `>= 2`（点一下也算一笔）。

### 原因 C：画布尺寸量到 0 → 笔迹画在 1 像素的画布上

```js
// 旧：r.width 为 0 时被 `|| 1` 兜成 1×1 → 画了等于没画
cv.width = Math.max(1, Math.floor((r.width || 1) * dpr));

// 新：量不到尺寸就跳过，等布局好了再量
if (!r.width || !r.height) return false;
cv.width = Math.max(1, Math.floor(r.width * dpr));
```

再加 `ResizeObserver` 跟着层尺寸变化重测并重画（放大后手写层尺寸会变，
只在装的时候量一次不够）。

---

## 3. 缺陷 ③：平板画了，电脑上**一个字都看不见**

平板把笔迹存进批注的 `strokes` 字段（后端 `AnnotationItem` 模型**有**这个字段），
但**桌面端 `drawAnnoOverlays()` 只画 `rects`（划词高亮）→ 完全忽略 `strokes`**。

→ 平板上画的东西同步到电脑后，电脑端**一个字都不显示**。

### 修法（桌面端 `index.html`）

用 SVG polyline 画（不干扰原文 canvas、不用处理 dpr）：

```js
const NS = "http://www.w3.org/2000/svg";
const svg = document.createElementNS(NS, "svg");
svg.setAttribute("class", "anno-ink-svg");
svg.setAttribute("preserveAspectRatio", "none");
svg.setAttribute("viewBox", "0 0 1000 1000");
(_annoState.items || []).forEach(a => {
  (a.strokes || []).forEach(s => {
    const sgPage = (s && s.page != null) ? parseInt(s.page, 10) : NaN;
    const target = isNaN(sgPage) ? parseInt(a.page, 10) : sgPage;   // ⭐ 见下
    if (target !== pageNum) return;
    ...
  });
});
```

### ⚠⚠ 页码归属：一个很容易踩的坑

平板存的手写批注长这样：

```json
{ "page": 0, "source": "ink",
  "strokes": [ { "page": 3, "pts": [x,y,x,y,...] } ] }
```

**批注的 `page` 是 0**（一条手写批注可以跨多页），
**真实页码在每条笔迹的 `strokes[].page` 上**。

→ 桌面端如果按 `a.page === pageNum` 过滤，**永远匹配不到**（我第一版就写错了）。
→ 必须按 `strokes[].page` 判断。

---

## 4. 新增的护栏（这层以前**完全没有**）

| 护栏 | 类型 | 条数 | 覆盖 |
|---|---|---|---|
| `microbench/_test_tablet_pdf_layout.py` | **真实渲染**（headless Edge） | 8 | 放大后不裁切 / 能横向滚 / `touch-action` / 手写层尺寸 / 画布尺寸 |
| `microbench/_test_desktop_ink_render.py` | **真实渲染**（headless Edge） | 7 | 笔迹 SVG 出现 / 坐标映射 / 页码归属 / 旧数据形态兼容 |
| `microbench/_test_tablet_pdf_ink.js` | 静态断言 | 22 | 缩放重画 / 手写层手势 / 笔迹存取 / 桌面端渲染 |
| `tests/test_pdf_layout_guardrails.py` | pytest 接入 | 2 | 把上面两套**挂进 pytest** |

### 为什么要「真实渲染」而不是静态断言

这两个都是**视觉/交互**问题 —— 读代码看不出来。我第一遍就差点判错
（以为「层能收到事件就够了」）。

**真实渲染**才回答得了「旧 CSS 到底坏没坏」：

```
旧 CSS: page=585 body=620  scrollWidth=605  → 坏的
新 CSS: page=1800 body=620 scrollWidth=1820 → 好的
```

### 破坏性验证（4/4 全部被护栏抓到）

| 破坏动作 | 护栏反应 |
|---|---|
| CSS 改回 `max-width:100% + overflow:hidden` | ✅ 4 条变红 |
| 去掉 `touch-action:none` | ✅ 变红 |
| 把 `pointerleave` 收笔加回来 | ✅ 变红 |
| 画布尺寸兜底改回 `\|\| 1` | ✅ 2 条变红 |

> ⚠ 中途踩到一次护栏自身的 bug：断言用**字符串匹配 CSS 规则**，
> 而规则里带着大段中文注释（注释里提到 `max-width:100%` 是在解释**为什么不能用**）
> → 护栏误判成「还在用」。
> → 现在断言前先**剥掉注释**。
> 这是 §4.62「字符串断言是格式的敌人」的又一次应验。

---

## 5. 顺手修的构建脚本问题（APK 构建偶发失败）

`android/_add_dex.py` 的 `tmp.replace(apk)` 在 Windows 上偶发
`PermissionError(WinError 5)` —— 杀毒/索引服务在文件刚写完的瞬间持有句柄。

→ 改成「先删目标再 replace + 最多 6 次退避重试」。

---

## 6. 验收

```powershell
python -m pytest tests -q -m "not slow"
node microbench/_test_tablet_pdf_ink.js
python microbench/_test_tablet_pdf_layout.py
python microbench/_test_desktop_ink_render.py
pwsh android/build_apk.ps1
```

---

## 7. 装到平板后请人工过一遍（我没法替你按手指）

1. 打开一篇论文 → 点「✍ 手写」→ **在原文上画一笔**
   → 笔迹要**立刻出现**（不是松手才出现）
2. 画一笔**长一点、拐个弯**的 → 要能连成一条，不是断成几段
3. 点「＋」放大 2~3 倍
   → **要能左右拖动看右半边**（这是修的核心）
4. 放大后继续画 → 笔迹要**跟着放大后的页面**走，不错位
5. 点「存」→ 提示保存成功
6. **在电脑上**打开同一篇论文 → **要能看到手写笔迹**（红笔）
7. 重新打开应用 → 笔迹要**还在**

> 第 6 条是本轮新修的（原来同步到电脑上一个字都看不见）。

---

## 8. 相关文档

- [`docs/HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md`](HANDOFF_2026-10-28-SYNC-AND-TABLET-ADAPT.md) —— 本轮同步合并
- [`docs/HANDOFF_2026-10-27-TABLET-QA-BUDGET.md`](HANDOFF_2026-10-27-TABLET-QA-BUDGET.md) —— 平板端答疑预算
- AGENTS.md §4.62 —— 字符串断言型护栏是格式的敌人
