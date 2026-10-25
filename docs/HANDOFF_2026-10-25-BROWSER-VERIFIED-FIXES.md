# 交接说明（2026-10-25 浏览器实测轮）—— 给另一台设备（30461）的 agent

> 这轮不是加功能，是**用真人级浏览器点击把已有功能跑了一遍**，结果抓出 5 个真 bug
> （其中 2 个是既有的阻断级 bug，已经很久了）。
> 本文件说清：改了什么、为什么、你要不要跟着改、怎么自检。

---

## 0. 先看这个：这轮你最该知道的三件事

1. **桌面端 PDF 双栏模式下，整条工具条是消失的**（缩放 / 全篇重译 / 导出 / 同步 /
   模型下拉框全都不见了），而且**章节视图叠在 PDF 上面**。
   这是个既有 bug，根因是 `switchReaderMode()` 用位置型选择器定位章节视图区。
   → 桌面端已修（见 §2）。**如果你那边的平板端有类似"用位置找元素"的代码，一并查。**

2. **AI 活动可视化（徽标 + 浮层 + 取消按钮）在 PDF 模式下完全看不见**，
   因为徽标挂在会被隐藏的 topbar 里。已修（见 §3）。

3. **上游 429 的报错以前直接把英文机器话甩给用户**
   （「流式响应异常: LLMError: HTTP 429: {...}」）。已改成中文可读提示（见 §4）。

---

## 1. 这轮改了什么（三个提交）

| commit | 内容 | 要不要跟着改 |
|---|---|---|
| `5ffb81f` | 修 PDF 模式工具条整条消失 + 章节视图叠 PDF（既有阻断 bug） | **平板端要查同类问题** |
| `e64e2fa` | AGENTS.md 记 §4.23（浏览器实测的工具坑） | 建议读一遍 |
| `1aaf38a` | 429 人话化 + QA 标题竖排 + 进度徽标与模型徽标分离 | 平板端**要改 429 文案** |

---

## 2. `5ffb81f`：位置型选择器把"结构"变成了隐式契约

### 根因

```js
// 原来的写法
const sectionView = document.querySelector("#reader-modal > div > div:nth-child(2)");
```

`:nth-child(2)` 指的是**父元素下的第 2 个子元素**（不是"第 2 个匹配的元素"）。
而 `#reader-modal` 的子元素顺序是：

```
#reader-modal
  └─ .reader-shell
       ├─ [1] .reader-topbar      ← nth-child(2) 选中的其实是这个
       ├─ [2] .reader-body        ← 真正的章节视图区
       └─ [3] #reader-pdf-view
```

所以切到 PDF 模式时 `sectionView.style.display = "none"` 藏掉的是 **topbar**：

- PDF 模式的整条工具条（缩放条 / 全篇重译 / 导出 / 同步 / 模型下拉框）
  **全部消失** —— 它们都在 topbar 里；
- 真正的章节视图区 `.reader-body` **从来没被藏过** → 章节视图盖在 PDF 上面。

### 修法

```js
// 用 id 定位，与子元素顺序彻底解耦
const sectionView = document.getElementById("reader-body");
```

并给容器补 id 锚点：`<div class="reader-body" id="reader-body">`

### 平板端要查什么

平板端 `static/tablet/app.js` 是独立前端，**不会**受这个 bug 影响（它没有 modal/视图切换那套）。
但请检查平板端有没有**同类模式**：

- 用 `nth-child` / `children[N]` 定位元素（加一个元素就会静默指错）；
- 用 `parentElement` / `closest()` 依赖某个元素所在的容器。

### 桌面端实测证据（Edge + CDP，真人级鼠标事件）

```
PDF 模式：reader-body display:none / pdf-view 1576×1004 block / topbar 1576×104 flex
章节模式：reader-body 1576×1040 grid / pdf-view none / topbar 1576×68 flex
工具条元素（修复前全是 0 尺寸）：
  缩放条 579×32 / 模型下拉框 143×23 / 导出 70×24 / 同步 79×36 / 全篇重译 90×24
```

---

## 3. `5ffb81f` 连带：AI 活动可视化的可见性

**现象**：DOM 里有任务行、徽标文本还在更新（"AI 运行中 10s"），但**用户看不见**。

**根因**：徽标 `#ai-activity-pill` 挂在 `.reader-topbar-actions` 里，
而 PDF 双栏模式下 topbar 被隐藏（就是 §2 那个 bug）→ 徽标被连带隐藏 →
`getBoundingClientRect()` 宽高都是 0 → 用户点不开浮层 → **「✕ 取消」永远够不着**。

**最终修法**（经过独立审查又改了一次，见 §3.1）：

1. `#ai-panel` 留在**浮动区块**（child 3 之后，`position: fixed`）
   → 不受 topbar 隐藏影响，也不占 flex 位；
2. **徽标放回 `.reader-topbar-actions`**（正常流里，`position: static`）
   → 位置自然、不压任何控件；
3. **有任务时浮层自动展开一次**（按任务 id 记录，任务全部结束后清空）
   → 徽标只是入口，用户不会想到去点它；"正在跑 + 跑了多久 + 能取消"
   是用户此刻最需要的信息。

### 3.1 独立审查抓到的坑（**别再犯**）

**第一次的修法是错的**：把徽标做成 `position: fixed; top:56px; right:16px`。
独立审查用 CDP 实测发现：

- PDF 模式下 topbar 会**折行**到 ~104px（缩放条 / 导出 / 同步 / 批量翻译排到第二行），
  「关闭」按钮正好落在 56~92px → **被徽标压住 61×28px**；
- `document.elementsFromPoint(关闭按钮中心)` 返回
  `["SPAN", "#ai-activity-pill", "SPAN", "BUTTON.btn-pill", …]`
  → 徽标排在按钮之前，**真人点击点不动**（1440/1576/1920 三种宽度全中招）。

**教训**：`position: fixed` 的浮层放在屏幕某个角落时，
**必须实测那个位置有没有别的东西**（topbar 折行后的实际高度、按钮区范围），
不能按"topbar 大概 50px"拍脑袋。

**另一个坑**：AI 浮层 z-index 1400 > QA 面板 1060 → 浮层盖住 QA 面板标题栏
（模型徽标 / 最小化），而 QA 面板要靠标题栏拖动 → 既点不动也拖不动。
→ `.reader-qa-panel` z-index 提到 **1460**。

**平板端要不要改？** 平板端有独立的 AI 停止按钮（`btn-ai-stop` / `aiAbort`），
**不受影响**。但如果平板端以后加"AI 活动"浮层，注意同样的坑：
**别把入口挂在"按场景会整块隐藏"的容器里**，也**别用 fixed 浮层压住别的按钮**。

---

## 3.2 审查抓到的其它几条（都已修）

| # | 问题 | 修法 |
|---|---|---|
| 🟠 | 3 处错误出口漏了人话化：`read_table`、`QA 路由`、`summarize_paper`（后者**内部吞掉** LLMError，app.py 根本看不到） | 统一 `llm.friendly_error_text(e) if isinstance(e, llm.LLMError) else str(e)` |
| 🟠 | `friendly_error_text` 往 stderr 打原始报文**没脱敏** | 打印前先过 `_redact_secrets` |
| 🟡 | 自动展开的复位语义与注释不符（PDF 队列连续翻译时，手动关一次就整个队列不再弹） | 改成**按任务 id** 记录，每个新任务展开一次，全部结束后清空 |
| 🟡 | `if (panel.classList)` 是为迁就 node 测试桩加的死代码 | 删掉（桩已补 classList；桩坏了就该让测试红） |

**护栏本身也有假通过**（审查指出，已改）：
- `_code_only()` 用正则剥 `//` 会被字符串里的 `"http://x"` 骗过
  → 改成真正的注释剥离器（只取 `<script>` 块 + 自己识别字符串/注释）；
- app.py 出口检查的块边界正则被**块内空行**截断（9 个分支只匹配到 1 个）
  → 改成按行号切块，并覆盖多行写法。

---

## 4. `1aaf38a`：429 报错人话化（**平板端建议照做**）

### 之前用户看到什么

```
流式响应异常: LLMError: HTTP 429: {"error":{"message":"***.RateLimitError: ...
```

一层没意义的"流式响应异常"前缀 + 一层重复的 "LLMError:" 类名 + 一坨英文 JSON。

### 现在

```
请求太频繁（HTTP 429 · 限流）：刚发出的请求超过了该模型的每分钟上限。等几十秒再试，或换一个模型/拉长间隔。
模型额度用完了（HTTP 429 · 配额耗尽）：套餐/余额额度已用尽，现在重试也一样。等额度恢复再试，或在 .env 里配一个你自己的模型。
```

**配额耗尽**和**速率限制**分开说 —— 前者只能等额度恢复，后者等几十秒就行，
提示说错用户就会一直干等。

### 桌面端怎么做的（平板端要抄的部分）

`microbench/llm.py`：

```python
def friendly_error_text(exc: BaseException) -> str:
    """把内部异常翻译成**给用户看**的中文。"""
    msg = str(exc)
    m = _HTTP_CODE_RE.search(msg)          # r"HTTP\s+(\d{3})\s*[:：]\s*(.*)"
    if not m:
        return msg                         # 已经是中文的，原样返回
    code, raw = int(m.group(1)), m.group(2)
    print(f"[MicroBench] 上游返回 HTTP {code}: {raw[:400]}", file=sys.stderr)  # 原文只进日志
    friendly = _friendly_http_error(code, msg)
    prefix = msg[:m.start()].strip().rstrip(":：").strip()
    return f"{prefix}：{friendly}" if prefix else friendly
```

**关键约束（平板端照抄时别踩）**：

- 翻译只发生在**给人看的那一层**。内部判定（要不要降格、TRUNCATED 标记、
  配额 vs 限流）**必须继续吃原始报文** —— 所以 `is_quota_exhausted_error()`
  吃的仍然是原文，不是这句中文。
- 配额判定用**整条消息**（`msg`）而不是只有响应体：像
  「降格模型调用失败: HTTP 429: Too Many Requests (原主模型报错: HTTP 429: Token Plan limit exceeded)」
  这种，主模型那半句才是"额度用完"的证据，只看响应体会误判成限流。
- 原始报文写 stderr 时**先脱敏**（桌面端有 `_redact_secrets`，别把 API key 写进日志）。

### 还有一处：去掉重复包装

```python
except asyncio.CancelledError:
    raise
except LLMError:          # ← 新增
    raise                 # 本函数自己抛的 LLMError 原样上抛
except httpx.ConnectError as e:
    ...
except Exception as e:
    raise LLMError(f"流式响应异常: {type(e).__name__}: {e}")
```

以前 `_stream_model` 自己抛的 `LLMError`（HTTP 429）会被下面那个 `except Exception`
**再包一层** → 用户看到「流式响应异常: LLMError: HTTP 429: ...」。

⚠️ `except LLMError: raise` 必须排在 `except Exception` **之前**；
`asyncio.CancelledError` 在 Python 3.8+ 是 `BaseException` 的子类（不是 `Exception`），
所以**取消功能不受这个新分支影响** —— 但顺序别搞乱。

---

## 5. `1aaf38a`：另外两个小修（顺手抄）

### QA 面板的问题标题被挤成竖排

标题 `flex:1; min-width:0` + 徽标容器不允许换行 → 面板一窄，徽标占满宽度，
标题被压到 0 宽 → 中文逐字换行成竖排（每字一行）。

```css
/* 标题 */
flex: 1 1 240px; min-width: 180px; line-height: 1.5; word-break: break-word;
/* 徽标容器 */
flex-wrap: wrap; min-width: 0;
```

### 进度徽标与模型徽标必须分开

进度写进模型徽标 → 每 500ms 刷一次就把模型名吃掉，收尾后没人恢复 →
徽标永远停在"还在想"。现在进度单独一个 `data-qa-stage-badge`，
收尾走**独立的 onEnd 回调**（✅ 完成 / ✕ 已取消 / ❌ 失败）。

---

## 6. 平板端自检清单

1. **429 的报错**：现在用户看到什么？是不是英文原文？是的话照 §4 改。
2. **AI 停止按钮**：平板端已有 `btn-ai-stop` / `aiAbort`，确认它**真的能停**
   （本轮桌面端实测：点取消 → 状态变 `cancelled` → 浮层显示「🚫 已取消」）。
3. **有没有"整块隐藏"的容器**：如果你在某个容器里加了新入口，确认所有场景下都可见。
4. **有没有位置型选择器**（`nth-child` / `children[N]`）：有的话改成 id。
5. **符号（× ≈ ≥）**：桌面的抽取层解不出来（pdfplumber），平板用 PDF.js **能解出来**
   （`a_ME ¼ E=H`）。所以平板的符号**大概率没问题** —— 别照抄桌面的裁图认字那套。

---

## 8. 安全修复（**平板端要自查**）

这一轮还跑了一次独立安全审计，查出 3 个高危 + 2 个中危。桌面端已修，
**平板端是独立实现，等价问题很可能也在**。

### 8.1 密钥脱敏的盲区（实测 24 种形态，原实现只挡住 3 种）

桌面端原来的 `_redact_secrets` 只挡 `sk-` 前缀 / `Bearer xxx` /
不带引号的 `api_key=xxx`。**JSON 形态完全漏**：
`{"api_key":"<hex>"}` 里关键词和分隔符之间隔了一个引号 → 正则不匹配 →
**key 明文进 `logs/desktop.log`**。

补齐的形态：JSON/驼峰变体（`apiKey`）、`access_token`/`refresh_token`、
裸 `Authorization`（不只认 `Bearer `）、`Basic` 认证、
URL userinfo（`user:pass@host`）、PEM 私钥块、`set-cookie`。

**顺序坑**：`Basic` 的正则必须排在「关键词 + 分隔符」那条**前面** ——
否则后者先把 `Basic` 当值替换掉，后面的 base64 载荷漏出。

**平板端要查**：平板前端把 API key 放哪？有没有写进 `localStorage`
（能被同源脚本读走）？错误提示里会不会带出 key？

### 8.2 错误出口和日志出口要一起管

桌面端原来只洗白了日志，**界面那条路没洗** → 带凭据的 `base_url`
（`https://user:sk-xxx@gw.internal`）在 URL 畸形时会连着 key 一起
进异常消息 → 直接送到浏览器。两条路都要过同一道闸。

### 8.3 离线导出 HTML 的存储型 XSS

导出器把 vault 里的 `.md` 转成**一个独立 HTML 文件**给用户双击打开，
而 `.md` 里可能含**外部论文原文**（双语精读卡片原样写入）。
Python-Markdown 不是消毒器 → `<script>` / `onclick` / `javascript:` 全保留
→ 恶意论文 → 导出物变活代码（`file://` 下能读同目录文件）。

桌面端加了白名单消毒器（不引第三方库：跨设备同步，一方少装包就起不来）。
**平板端的单文件导出（`export_tablet`）也要查一遍**。

### 8.4 GET 带写副作用

`GET /api/paper/download_pdf` 会建目录 + 写 PDF 文件，而 CSRF 守卫
只查非安全方法 → 恶意网页一个 `<img src="http://127.0.0.1:5000/...">`
就能让用户机器凭空多出文件（`<img>` 不带 Origin，守卫直接放行）。
已改成 POST。**平板端有没有同类 GET 写接口？**

### 8.5 内部路径泄露

`OSError` 的消息里带本机绝对路径（`E:\...\logs\xxx.json`），
`str(e)` 原样送到浏览器 = 内部路径泄露。桌面端统一加了 `_safe_err()`
（脱敏 + 抹绝对路径 + 限长）。

---

## 9. 降格提示要说真话（用户会当场质疑）

用户原话：「M3 额度没耗尽啊，你这肯定又有什么问题出现了」。

查下来 M3 额度**确实没问题**（直连 200、12 并发全 200），但界面的降格
toast **写死**了「主模型额度用完了」，不管真实原因是什么都这么说。

**降格有三种完全不同的原因**：

| kind | 真实原因 | 处置 |
|---|---|---|
| `quota` | 5 小时滚动窗口的额度真的用完 | 等窗口恢复 |
| `rate_limit` | 每分钟请求数超限 | 等几十秒 |
| `no_output` | 连上了但这次没吐出正文 | 跟额度无关 |

**两个判定漏洞**（这才是"额度够却显示降格"的机制性原因）：
1. 流式路径用"消息里有没有 429/quota"这种**宽松判定**，
   把网络/协议错误也当成配额耗尽 → 全局降格 15 分钟；
2. `no_usable_output`（这次没吐出正文）也触发全局降格 →
   一次任务的问题让后续所有请求都改走备用模型。

**教训**：凡是会改变全局行为的状态，都要带**分类 + 人话原因**给界面；
一个写死的错误提示比没有提示更糟（用户会去排查一个不存在的问题）。

---

## 10. 这轮用到的浏览器实测方法（值得抄）
桌面端这一轮是用 **Edge + Chrome DevTools Protocol** 做的真人级点击
（内置 Browser 工具在这个页面上 click 落不下去，细节见 `AGENTS.md` §4.23）。

平板端如果也要跑"像真人一样点一遍"，注意：

- 平板端是**独立前端**（`static/tablet/app.js`），不共用桌面的 `index.html`，
  所以**桌面端的修复一行都不过去**，平板端要自己改、自己测。
- 桌面端这轮抓到的 5 个 bug 里，有 3 个是**只有真点击才能发现**的
  （DOM 里有内容但看不见、位置型选择器指错、徽标吃掉模型名）。
  静态读代码看不出来 —— **必须真点**。
