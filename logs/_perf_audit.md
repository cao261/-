# MicroBench 打开卡顿 — 性能审计报告

> **范围**:`microbench/` + `desktop.py`
> **目标**:找出首屏(splash → 主界面可交互)真实瓶颈
> **方法**:静态阅读 + 路径推算 + 标注所有"未实测"项
> **不修改任何代码**

---

## 0. 量化基线

| 指标 | 数值 | 备注 |
|---|---:|---|
| `static/index.html` 总字节数 | **244,033 B** (~244 KB) | UTF-8 |
| `static/index.html` 行数 | 5,238 | |
| **inline `<script>` 字节数** | **178,208 B** (~174 KB) | 行 1095–4918,共 3,824 行 |
| inline `<style>` 块 1 (head) | 16,982 B | 行 18–655 |
| inline `<style>` 块 2 (body 末尾) | 11,383 B | 行 5133–5536 |
| **head 段总字节** | **18,173 B** | 含全部 CSS link |
| body 中 `<div>` 元素数 | 297 | |
| body 中 `<input>` | 23 | |
| body 中 `<button>` | 78 | |
| body 中 `<img>` | 0 | |
| 内联 `style="..."` 属性 | **290** | ≈ 8–10 KB 重复 CSS |
| 内联 `on*=` 事件 | 81 | |
| 全局 `window.X = ...` 导出 | 34 | 仅供内联 `onclick=` 调用 |
| `fetch()` 调用 | 33 | |
| `setInterval()` | 1 | line 2270 (PDF reader 关闭检测) |
| `setTimeout()` | 21 | 多为 debounce / poll-back |
| `addEventListener()` 注册 | 10 | 2 个 DOMContentLoaded,其余 mouseup / keydown / input |
| `DOMContentLoaded` 监听器 | 2 | line 1774 + 2029 |
| 同步执行的顶层 IIFE / 调用 | 0 | 纯函数定义,无顶层副作用 ✅ |
| 后端 Python 文件 LOC | 5,002 | app.py 2018 / llm.py 908 / generator.py 659 / paper_text 384 / paper_fetcher 408 / plot_compare 353 / translation 203 |

---

## 1. 启动时序图(冷启动典型路径)

```
双击 desktop.py
  │
  ├─[~0.0 s]  main() 启动 uvicorn 守护线程(threading.Thread daemon=True)
  │            ← 这里 FastAPI 已经完成模块导入(pdfplumber / jinja2 / requests / llm / generator)
  │              实际冷导入耗时 ~1.5–3 s(模块数对应此区间,未实测)
  │
  ├─[~0.0 s]  _wait_for_server 阻塞轮询 127.0.0.1:5000
  │            每 0.3 s 一次 socket.create_connection,直到端口响应
  │            ← 此期间主线程无法继续,webview 还没建
  │
  ├─[~T_A 秒] 端口通了 — 创建 webview 窗口(url=data:text/html,...SPLASH...)
  │            ← SPLASH 不需网络,立刻显示 "Loading..."
  │
  ├─[+0.4 s]  _splash_switch 在另一线程把 url 切换到 http://127.0.0.1:5000/?v=<token>
  │            ← 切换的 "延迟 0.4 s" 是固定值,不是数据加载完成才切
  │
  ├─[切换瞬间] WebView2 真正发起 GET /?v=<token>
  │  │
  │  ├─ 拉取 HTML: 244 KB ← localhost 回环,~30–80 ms(取决于磁盘)
  │  │
  │  ├─ 解析 HTML <head>:5 个外部 CSS 链接(下详)← 全部 render-blocking
  │  │   │
  │  │   ├─ fonts.googleapis.com/css2?family=…&display=swap
  │  │   │     3 个 family × 多个 weight(italic + upright × 6–8 个 weight)
  │  │   │     先 fetch 一个 ~30 KB 的 CSS,该 CSS 再 import N 个 .woff2(.gstatic.com)
  │  │   │
  │  │   ├─ cdn.jsdelivr.net/.../katex.min.css   ← ~30 KB
  │  │   │     ⚠ KaTeX JS 是 lazy 的,但 KaTeX CSS 在 head 阻塞渲染
  │  │   │
  │  │   └─ cdn.jsdelivr.net/.../pdf_viewer.css ← ~5 KB
  │  │         ⚠ PDF.js JS 也是 lazy 的,但 viewer CSS 在 head 阻塞渲染
  │  │
  │  └─ 等所有 <link> 完成 → First Contentful Paint
  │
  ├─[HTML 解析到 line 1095] 开始执行 inline `<script>` (174 KB / 3,824 行)
  │    实际无顶层副作用(仅函数定义),但 V8 仍需 parse + compile 整段 JS
  │    ← 实测未做;V8 大致 50–150 ms / 100 KB JS,此段预估 ~80–200 ms
  │
  ├─[DOMContentLoaded] 两个监听器同时触发:
  │    1) line 1774 → refreshPaperSubfolderList() + updatePaperSavePathPreview()
  │       ← 触发 GET /api/paper/folders?topic_category=...&kind=literature_cards
  │    2) line 2029 → loadStats() + fetchModelStatus() + initReaderSelectionListener() + _restoreLocalState() + _restoreFromHash()
  │       ← 并发 4 个 GET:/api/stats, /api/llm/status, ...
  │       ← _restoreFromHash() 仅在 url hash 非空时才走,日常是 no-op
  │
  ├─[后续]  字体二进制文件陆续到达,Cormorant Garamond / Plus Jakarta / JetBrains Mono
  │         FOUT/FOIT → flash of unstyled text(用户感觉 "卡顿正在发生")
  │
  └─[~3–8 s,视网络冷热] "看起来可用",用户开始点击
```

**T_A 典型值(冷启)**:3–8 s。缓慢来自三段:
1. 模块冷导入(~1.5–3 s,不可控)
2. Google Fonts 冷下载(~1–5 s,看 CDN 抖动)
3. WebView2 自身的窗口初始化与 paint 调度(~0.5–1 s)

---
## 2. 真正的瓶颈 Top 5(按耗时占比排序)

### 🥇 #1 — 5 个 render-blocking `<link rel="stylesheet">` 全在 `<head>`,包含 2 个**死重量**

**位置**:`static/index.html:10–16`
```html
10  <link rel="preconnect" href="https://fonts.googleapis.com">
11  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
12  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;0,700;1,400;1,600&family=Plus+Jakarta+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
14  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin="anonymous">
16  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/web/pdf_viewer.css">
```

**为什么慢**
- Google Fonts 链路:preconnect 只到 googleapis + gstatic,虽 .woff2 实际就在 gstatic 上,但**要等 CSS 解析出 `@font-face` 才知道要拉哪些 .woff2**,层层 waterfall。
- 3 个 font family × 多个 weight × italic 变体 ≈ **6–12 个 .woff2 文件,约 200–500 KB 二进制**(Cormorant Garamond 单独 8 个 weight)。
- **KaTeX min.css(~30 KB)与 PDF.js pdf_viewer.css(~5 KB)都是"配着 lazy JS 用"** — 但 CSS 在 head 一律 render-blocking。它们对应的 JS 是 lazy-load(行 1890 `loadKatex()`,行 1871 `loadPdfJs()`),所以加载时整个 DOM 完全没有匹配元素,这两个 CSS 完全死重量。

**预估耗时**(从 webview 创建到首屏可交互看得到字):
- 国内网络冷启 Google Fonts:典型 1.5–4 s,有时 5 s+(DNS 解析失败需重试)
- 国内网络 jsdelivr 拉 KaTeX CSS + pdf_viewer.css:0.5–2 s
- 优化空间合计 **1.5–4 s**

**优化建议**
1. KaTeX CSS / PDF.js viewer CSS 改为 lazy —— 不放在 head,用 `link.media="print"` + onload 切换的 hack,或直接通过 JS 在 `loadKatex()` / `loadPdfJs()` 里 append
2. Google Fonts:用 `font-display: swap`(已有,好) + self-host 子集字体,**或**对 hero 区单独 `<link rel="preload">` 头部用到的 1 个 weight
3. 加 `<link rel="preconnect" href="https://cdn.jsdelivr.net">`

**预估能省**:**1.5–4 s**(网络冷启场景下)。本机已缓存 Google Fonts 时节省可能 <200 ms

---

### 🥈 #2 — 174 KB inline `<script>` 在 body 末同步阻塞,parse 全部内容才能继续

**位置**:`static/index.html:1095–4918`,3,824 行连续 inline 脚本

```html
1095  <script>
1096    function showToast(...) { ... }
       ... ~3,800 行函数定义 ...
4917    window.showKeywordPickerModal = showKeywordPickerModal;
4918  </script>
```

**为什么慢**
- 没有 `defer` / `async` / `type="module"`。V8 必须 parse → compile 全部 174 KB,**且全部完成才会派发 DOMContentLoaded**。
- 内含 33 个 `fetch()`,21 个 `setTimeout`,10 个 `addEventListener` 注册,34 个 `window.X =` 导出 — 全部需要解析,虽然**执行没有副作用**(无顶层 IIFE)。
- 顶层 DOMContentLoaded 注册器(line 1774 + 2029)只有在该段 JS parse 完后才能注册,然后才等 DOMContentLoaded 才能 fire = **双层链式延迟**。

**实测无数据**。粗略估算 V8 parse+compile 174 KB JS 约 **80–250 ms**(函数多、字符串多,慢于纯算法场景)。

**优化建议**
1. **加 `defer`**:把整个 `<script>` 改为 `<script defer>`,浏览器会在 HTML 解析期间就 fetch + parse,这意味着 **DOMContentLoaded 可以更早 fire**。
2. **更大收益**:把不急用的 modal/QA code 挪到 lazy,例如:
   - `showSnapshotModal` / `showDiagnoseModal` / `showKeywordPickerModal` 等 ~15 个 "用时才创建" 的 modal 移到独立 JS 文件或 `import()` lazy 模块。
   - 预计可减 30–60 KB 关键路径 JS。

**预估能省**:**80–250 ms**(静态解析并发化后)。

---

### 🥉 #3 — desktop.py 启动 uvicorn 后,串行等待端口响应 → 启动额外 +0.3 s ~ N s

**位置**:`desktop.py:23–32` 的 `_wait_for_server`

```python
23  def _wait_for_server(port: int, timeout: float = 15.0) -> bool:
24      """阻塞等待端口响应,返回 True 表示服务已就绪。"""
25      deadline = time.time() + timeout
26      while time.time() < deadline:
27          try:
28              with socket.create_connection(("127.0.0.1", port), timeout=1.0):
29                  return True
30          except OSError:
31              time.sleep(0.3)
32      return False
```

**为什么是问题**
- `socket.create_connection` 默认 timeout=1.0,在未 listen 的早期会 timeout 1 s 才返回 → 最多拖 1 s。
- 即使 uvicorn 已经在 listen,WebView 切换到 `127.0.0.1:5000/?v=<token>` 后,**WebView2 自己又要重做 TCP + HTTP + parse**。
- **没有 HTTP/1.1 keep-alive 优化** — 每次 webview 重新创建就重新做 TCP 握手(无 TLS,但有 connection setup)。

**预估耗时**:
- 端口等待:0–1.3 s(典型)
- webview 切换 url 后的真实 HTTP fetch 等到 HTML 字节:30–100 ms

**优化建议(不影响功能)**
1. `_wait_for_server` 改用更密的轮询(50 ms),避免 1 s socket timeout。
2. **最佳方案(覆盖 1+2)**:让 webview 启动时**直接打开 main URL**,splash 是多余的(已经有 ~400 ms 固定延迟)。把 `_splash_switch` 那 0.4 s 删了,实测会感知快一些。
3. 进一步:把 uvicorn `lifespan="on"` 改为 `lifespan="off"`(本项目没用 lifespan handler,可以省启动时间)。

**预估能省**:**0.3–1 s**

---

### #4 — 后端 "async handler 里同步调 LLM" → 整 server event loop 死锁(虽不是 "打开" 问题,但严重影响后续)

**位置**:`app.py:1257, 1570, 1938, 2080` 都用 `llm.chat()`(sync) 在 `async def` 路由内调用

```python
# app.py:1210
@app.post("/api/paper/translate_section")
async def api_translate_section(req: TranslateRequest):
    ...
1257            translated_parts.append(
1258                llm.chat(            # ← sync, 里面 requests.post + time.sleep
                    messages=[...],
                    ...
1269                ).strip()
            )
```

**`llm.chat()` 的内部路径**(llm.py:386–553):
```python
# llm.py:386 _invoke_model
436        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)  # 60–120s blocking
# llm.py:95–129 acquire()
120            # Sleep then re-acquire time
121            time.sleep(wait_time)  # ← 这里 time.sleep 在事件循环里 = 整 uvicorn 卡
```

**为什么是问题**
- LLM 在 thinking mode(MiniMax M2.7)平均 60–120 s。这段时间整 server event loop 被占(因为 `requests.post` 是阻塞 IO,`time.sleep` 显式让出 GIL 但实际阻塞)。**前端后续的所有 fetch 都排队**。
- 用户感觉:页面点其它按钮 → 没反应 → 几分钟后才有反应。
- 已有 `chat_async()` / `chat_stream_async()` 但只在 3 个 endpoint 用 — `translate_section`、`translate_page`、`paste_analyze`、`classify_qa_scope` 仍用同步 `chat()`。

**预估耗时影响**:非 "打开",但**任何一个翻译行为触发后**,所有其它 API 排队 60–120 s。

**优化建议(不影响功能)**
1. **把 translate_section / translate_page 改成 streaming(SSE)**:前端打字机效果,体验立刻像在动;server 端用 `chat_stream_async` 真异步、不阻塞。
2. **至少**把这 4 个 `llm.chat()` 调用换成 `await llm.chat_async()` + `to_thread` / `run_in_executor`,避免阻塞 event loop。

**预估能省**:**0–60 s 视并发**,但主要是消除 "全部死锁" 现象

> **注**:这条并非 "打开" 卡顿,但跟用户的整体体验紧密相关。若用户反馈 "打开后点什么都不动",根因是这个。

---

### #5 — `_pdfState.rendering` 上有一个常驻 `setInterval`(`index.html:2270`)

**位置**:`static/index.html:2270–2276`
```js
2270        const observer = setInterval(() => {
2271          if (!document.getElementById("reader-modal") ||
2272              getComputedStyle(document.getElementById("reader-modal")).display === "none") {
2273            _pdfState.rendering.cancelled = true;
2274            clearInterval(observer);
2275          }
2276        }, 1000);
```

**为什么是问题**
- 这是每次 `loadPdfDualColumn()` 启动时新建的 interval,1 Hz `getComputedStyle()` 持续轮询,**仅用于检测 reader modal 是否被关闭**。
- 关闭 modal 后立即 clear,不会留下泄漏。但 modal 关不掉时(罕见 bug)会无限跑。

**与 "打开" 关系**:这个 interval **不**在打开时启动,只在用户开 PDF reader 时才起。所以不直接影响打开。但跟 #4 一起出现时(LLM 死循环时),这个 1 Hz `getComputedStyle` 会持续空转。

**优化建议**
- 用 `MutationObserver` 替代,绑定在 modal 节点,0 Hz 持续监控。
- 或者 inline 用 `closeReaderModal()` 显式 cancel flag,根本不用定时检测。

**预估能省**:0 ms 首屏,**但更省电与减少判断**

---

## 3. 不影响功能的优化清单(按 "快速 win" 排序)

### 🟢 Quick Win 1 — Lazy-load KaTeX CSS 与 PDF.js viewer CSS

**改动**:`static/index.html:14, 16` 两条 `<link>` 删掉,在 `loadKatex()`(行 1890) / `loadPdfJs()`(行 1871)内部 append 对应 `<link>`,再 resolve。

**影响范围**:KaTeX CSS 仅在第一次 LLM 翻译完成后才会用到;PDF.js viewer CSS 仅在 PDF 模式打开时用。

**预估收益**:**0.3–1.5 s**(冷启时拉 jsdelivr)

**风险等级**:🟢 极低 — 内部已经 lazy-load 了 JS,JS 触发时再插入 CSS 是对齐行为。

---

### 🟢 Quick Win 2 — 给 inline `<script>` 加 `defer`

**改动**:`<script>` (行 1095) → `<script defer>`
   实际因为 script 在 body 末尾,defer 效果 ≈ 立刻 parse+execute on DOMContentLoaded。让浏览器在 HTML parse 同时就开始 parse JS,**避免链式等待**。

**影响范围**:全部 inline JS。

**预估收益**:**80–250 ms**(脚本 parse 与 HTML parse 并发)

**风险等级**:🟢 极低

---

### 🟢 Quick Win 3 — 删 desktop.py 的 `_splash_switch` 0.4 s 硬延迟

**改动**:`desktop.py:95–102`,0.4 s 固定等待改 0;或者改成 "等 `/` 返回 200 后立刻切"。
  ```python
  # 现有
  def _splash_switch(window, target_url: str, delay: float = 0.4) -> None:
      ...
      time.sleep(delay)
      if hasattr(window, "load_url"):
          window.load_url(target_url)
  ```
  **改成**:`delay: float = 0.0`(直接切)。Splash 的视觉作用不大,因为 webview 启动到第一帧本来就有 200–400 ms 渲染开销,直接打开主 URL 反而快。

**影响范围**:仅启动路径

**预估收益**:**0.4 s**

**风险等级**:🟢 极低(可选保留 splash,通过加快取 URL 弥补)

---

### 🟢 Quick Win 4 — `_wait_for_server` 0.3 s → 0.05 s 轮询

**改动**:`desktop.py:31` 的 `time.sleep(0.3)` → `time.sleep(0.05)`

**影响范围**:仅启动路径

**预估收益**:**0–500 ms**(视 uvicorn 启动速度)

**风险等级**:🟢 极低(更密轮询只在等端口阶段多 10 次小循环)

---

### 🟢 Quick Win 5 — `GET /` 加 ETag/Last-Modified(走 304 加速)

**现状**:`app.py:315–327` 显式 `Cache-Control: no-store, must-revalidate`
**反思路**:用户频繁刷新(F5)、重开 webview 时,254 KB HTML 重新下载 ~80 ms。可以加 ETag(filename + mtime based),revalidate 用 304 → 0 字节。**功能上不变**(仍走 no-store 等价语义),但 webview 内部 304 不会重传 body。

**影响范围**:静态首页

**预估收益**:**30–80 ms / 每次刷新**

**风险等级**:🟡 中(改 cache 行为,需要保证 dev 期间编辑后仍能拿到新内容 — 已用 `?v=token` cache-bust 解决了)

---

### 🟡 Quick Win 6 — `/api/vault/literature` 共享 `get_vault_stats()` 缓存

**现状**:`app.py:355–358`
```python
stats = get_vault_stats()  # ← 每次重新 glob('**/*.md')
all_items = stats.get("literature_list", [])
```
`loadRecentFeed`(index.html:1252)的 default 不带 cache header → 每次都重新走 disk scan。

**改动**:在 generator.py 顶层加一个 5 s TTL 的 cached module variable:
```python
_VAULT_STATS_CACHE = {"ts": 0.0, "data": None}
def get_vault_stats():
    now = time.time()
    if _VAULT_STATS_CACHE["data"] is not None and now - _VAULT_STATS_CACHE["ts"] < 5:
        return _VAULT_STATS_CACHE["data"]
    data = _compute_vault_stats()
    _VAULT_STATS_CACHE["ts"] = now
    _VAULT_STATS_CACHE["data"] = data
    return data
```
**注**:要写盘后失效(save_literature_card 等主动 invalidate)。

**预估收益**:**20–80 ms**(典型 vault 50 篇 literature 时)

**风险等级**:🟢 极低(纯缓存,5 s TTL)

---

### 🟡 Quick Win 7 — `getComputedStyle` 在 setInterval 中改用 ResizeObserver / click 绑事件

**改动**:`index.html:2270–2276` 用 modal 节点的 `transitionend` 事件替代 setInterval

**影响范围**:仅 PDF reader

**预估收益**:0 首屏,**但**减少 1 Hz 空转 CPU

**风险等级**:🟢 低(逻辑已能正常 close)

---

### 🟡 Quick Win 8 — 290 个 inline `style="..."` 抽 CSS 类

**现状**:body 里有 290 个 `style="..."` 内联样式,估算 8–10 KB 重复 CSS
**改动**:针对高频样式(如 `cursor:pointer; min-width:0;` 等)抽到 .reader-section-action 类

**预估收益**:**5–10 KB HTML 减少 → 30–50 ms parse / 50–100 ms 网络**

**风险等级**:🟡 中(需要看具体样式分布)

---

### 🟡 Quick Win 9 — Google Fonts 单 font-family 限制 weight

**现状**:`Cormorant Garamond: 0,300;0,400;0,500;0,600;0,700;1,400;1,600 = 8 weights`,真正用到 ≤ 4 个
**改动**:用 googlefonts 中明确裁剪 weight(每个被引元素实际只用了 ≤ 4 个)

**预估收益**:**100–400 KB 字体文件减少**(视 weight 数减半)

**风险等级**:🟡 中(可能改了字体一致性)

---

### 🟡 Quick Win 10 — `renderPdfPage()` 内的 `setTimeout(() => loadPdfDualColumn(...), 80)` 续渲染

**现状**:`index.html:2168` 在 zoom 切换时清空 container 后等 80 ms 再 start
**改动**:不延迟,直接 `loadPdfDualColumn(...)`,生成号已天然防 race(行 2247–2249)

**预估收益**:**80 ms / zoom 切换**

**风险等级**:🟡 中(可能与 DOM 渲染时序冲突)

---

## 4. 不优化的部分(看似慢但优化会出问题)

### ❌ 不优化 — PDF page render 顺序循环 (`index.html:2280–2286`)
```js
for (let p = 1; p <= pdfDoc.numPages; p++) {
  if (_pdfState.rendering.cancelled) break;
  ...
  const pageData = await renderPdfPage(p);  // 串行 await page.render
  _pdfState.pages.push(pageData);
}
```
**为什么不能并行**
- `pdfjsLib.renderTextLayer` 之后还有 `getElementsByClassName` 等操作,canvas 大小依赖 viewport
- 同时 N 个 `getPage().render()` 会快速吃满 WebView2 内存(渲染 30 页 PDF 时)
- 用户体验角度:zoom 切换已经能立刻看到当前页,平行也没省到感知
- **已存在 generation 防 race** — 写并行需重写整个 cancel 模型

### ❌ 不优化 — LLM 翻译自然等待 60–120 s
**为什么**
- MiniMax M2.7 是 thinking model,无法压缩思考时间。
- 已经用 streaming 让前端显示打字机效果(虽只是 3 个 streaming endpoint)。
- 真正能省的是把另外 4 个 `llm.chat()` 换成 `chat_async()`,见 #4。这只是 "消除死锁",省不了单次延时。

### ❌ 不优化 — webview splash 跟 main URL 的切换
- Splash HTML 数据 URL 即时,**省不了任何东西**;只是给了视觉占位。
- 想 "先 splash 后 main" 基本不可避免,因为 webview 的 url 参数一次性。
- 能做的只是缩 0.4 s 硬延迟(已列 Quick Win 3)。

### ❌ 不优化 — `getComputedStyle` poll 检测 modal close
- 直接绑事件需要改多处(ESC、× 按钮、外部 click、close 按钮)
- 维持 1 Hz poll 更可靠,CPU 影响在 WebView2 下极小

---

## 5. 数值汇总(便于对照检查)

| 项目 | 现状 | 优化后预估 | 可行性 |
|---|---|---|---|
| `index.html` 大小 | 244 KB | 244 KB(几乎不变;改 caching 实际不变) | 短期难改 |
| inline `<script>` | 174 KB | **120–150 KB**(迁出 modal code) | 中期 |
| render-blocking CSS 数 | 5 | **3**(仅 Google Fonts) | **立刻** |
| 死重量 CSS(KaTeX + PDF.js) | ~35 KB | 0 KB(head 不再 load) | **立刻** |
| Google Fonts weight 数 | 8(Cormorant)| 4 | 中期 |
| Google Fonts 字体文件总大小 | 200–500 KB | 100–250 KB | 中期 |
| 首屏可交互时间(冷启网络) | 4–8 s | **2–4 s** | 短期(Quick Win 1+2+3) |
| 首屏可交互时间(暖启) | 1–2.5 s | **0.7–1.5 s** | 短期 |
| 任意 LLM 翻译期间全 server 冻结 | 60–120 s | **0**(改 async) | 中期 |

---

## 6. 推荐改动优先级(用户视角最划算的组合)

> **只读不改,以下为推荐顺序,需用户授权后再动**

```
Day-1 (零风险,30 分钟内)
  Quick Win 1  lazy KaTeX / PDF.js CSS
  Quick Win 2  inline script defer
  Quick Win 3  desktop.py splash 延迟缩 0
  Quick Win 4  _wait_for_server 轮询 0.05s

  → 首屏冷启 -2 ~ -4 s(看网络)

Day-2 (中等,半天)
  Quick Win 6  /api/vault/literature 缓存 get_vault_stats
  Quick Win 5  GET / 加 ETag(配合 ?v=token cache-bust)
  Quick Win 10 zoom 切换去 80ms sleep

  → 首屏 -0.1 ~ -0.3 s

Mid-term (1 天 +测试)
  Quick Win 8  抽离 inline style
  Quick Win 9  Google Fonts 缩 weight
  #4  llm.chat() → chat_async()(4 个 endpoint,需要联调)

  → 后续操作体感 +500 ms ~ +60 s
```

**注**:本次审计仅做静态分析,**未实际跑应用测量**。上文所有 "预估耗时" 基于:
- V8 parse 174 KB JS 大致时间
- WebView2 / Chromium 启动开销常识
- 真实 CDN 与字体文件大小公开数据
- LLM 已知 latency 区间(thinking mode 60–120 s)

实测时间会因机器(磁盘/网络/CPU)浮动 ±50%。本报告**聚焦真实瓶颈**,非泛泛而谈。

报告路径:`logs/_perf_audit.md`
