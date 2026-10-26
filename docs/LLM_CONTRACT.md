# MicroBench 模型层契约（换模型 / 降级 / 速率 / 表格读图）

> 改 `microbench/llm.py`（模型调用）、`microbench/app.py`（翻译 / 抄表 / 答疑）
> 之前先读这里。
>
> 阅读器契约见 [READER_CONTRACT.md](READER_CONTRACT.md)，
> 同步契约见 [SYNC_CONTRACT.md](SYNC_CONTRACT.md)。

---

## 1. 一句话结论：换模型是可行的，协议层早就通用了

底层只有**一种**调用形态：

```
POST {base_url}/chat/completions
{"model": ..., "messages": [...], "max_tokens": ..., "temperature": ...}
```

- 主模型 `https://api.minimaxi.com/v1`（MiniMax-M3）
- 降级备用 `https://apihub.agnes-ai.com/v1`（agnes-3.0-flash）

**两个都是 OpenAI 兼容**。所以"接一个自己的 API 模型"在协议层没有障碍，
只要它是 OpenAI 兼容的。

提示词侧也没有绑死：`TRANSLATION_SYSTEM_PROMPT` / `TABLE_VISION_PROMPT`
都是纯中文自然语言指令 + 输出格式约定（Markdown 表格 / 原样输出 `⟦FORMULA⟧` 标记），
没有任何 MiniMax 专属语法。实测四个模型都照做（见 §2）。

---

## 2. 实测能力矩阵（2026-10-25，JACERS 综述第 2 页）

样本：`01_Literature/04_Survey_综述/01_论文原文_PDF/2008-JACERS-综述-*.pdf` 第 2 页
（页内有 2 张表：Table I 压电相性能、Table II 磁致伸缩相性能）。
表格数值与论文印刷值逐一核对过。

| 模型 | 整页翻译 | 抄表（数值对不对） | 备注 |
|---|---|---|---|
| **MiniMax-M3** | ✅ 7.6~9.6s / 1956 字 | ✅ 3–6s，`−90｜−175｜−109｜｜~700｜16.5` 与论文一致 | 直译首选（**关思考**） |
| **MiniMax-M3.1-Flash** | ❌ 324s 撞线 → HTTP 504 | ✅ 21–42s，还会主动把表头译成中文 | **只能答疑，不能翻译**（强制思考吃光 token 预算） |
| **Agnes 3.0 Flash** | ✅ 24.5s / 1843 字 | ✅ 25–35s，数值与论文一致 | 免费无限，但慢 + ⚠ 严格限流 11 RPM |
| **DeepSeek V4.1 Flash** | ✅ **3.7s** / 905 字 | ✅ **1.9s**，数值与论文逐字一致 | 独立额度，**支持读图**（2026-10-26 加入） |
| **DeepSeek V4 Pro** | ✅ 15.1s / 2575 字 | ❌❌ **会编造整张表** | 推理更强，但**不支持视觉** → 表格路径已强制拦截 |
| 自己的 API 模型 | 看是否 OpenAI 兼容 | **看是否支持图片输入** | 纯文本模型抄不出表格 |

> ⚠️ 抄表测的是**生产用的表格区域坐标**（`28,517,536,609` / `28,662,536,754`，
> 来自抽取层 `_find_table_regions`）。拿手写的猜的坐标去测会得到
> "截图里没有表格"的回答 —— 那是**答对了**，那个区域本来就不是表。
> 早期曾据此误判「Agnes 读不了图」，用生产坐标复测后结论相反。
>
> ⚠️ 上面「整页翻译」一列是**关思考**的结果（生产路径默认）。
> 翻译的 thinking 参数必须**按 provider** 判定 —— 见 §3.1。

### 2.1 DeepSeek 专项（2026-10-26 实测）

**它是独立网关**：MiniMax（`api.minimaxi.com/v1`）和 Agnes（`apihub.agnes-ai.com/v1`）
两边的 `GET /v1/models` 里都**没有** DeepSeek（实测各 9 个模型，均无）→
必须作为独立 provider 接，不能复用任何一边的 key。

**官方 API 当前只暴露两个模型**（`GET https://api.deepseek.com/v1/models` 实测）：

| 模型 id | 视觉 | 实测表现 |
|---|---|---|
| `deepseek-flash` | ✅ 支持图片输入 | 抄表 1.9s，数值与论文印刷值逐字一致 |
| `deepseek-v4-pro` | ❌ **不支持** | **收下截图后编造了整张表** |

> ⚠️ 官方文档原话："deepseek-v4-pro does not support vision"；
> "deepseek-flash accepts JPEG, PNG, GIF and WebP as image_url blocks"。
> 旧的 `deepseek-chat` / `deepseek-reasoner` id 已于 2026-07-24 退役。

**参数兼容性**：`thinking: {"type": "disabled"}` DeepSeek **接受**（实测 200，
`reasoning_content` 长度归 0）→ 翻译时能真的关掉思考，不像 MiniMax-M2.7 那样被静默忽略。
思考内容走独立的 `reasoning_content` 字段，不会混进 `content` → 译文干净。

**v4-pro 编造表格是最危险的一种失败**：它返回的 Markdown 表格**格式完全像真的**
（有表头、有分隔行、数值排布规整），但数值一个都不对。这跟 §4.6
「错公式比没公式危险得多」是同一条红线 —— 用户会照着这张表去算。

→ **处理办法（已实现）**：抄表路径做**视觉能力检查**，
`llm.VISION_CAPABLE_MODELS` 白名单之外（含 v4-pro）的模型**一律不派去抄表**，
自动改用已验证的视觉模型（优先 `deepseek-flash`）。

---

## 3. 已经有的换模型基础设施（别重复造）

| 能力 | 在哪 | 怎么做的 |
|---|---|---|
| 界面换模型（翻译） | `app.py` `TRANSLATION_MODEL_CHOICES` + `GET /api/llm/translation_models` | 服务端下发选项，前端下拉框 |
| 界面换模型（答疑） | `app.py` `QA_MODEL_CHOICES` + `GET /api/llm/qa_models` | **独立**下拉框（2026-10-26 新增） |
| 翻译/答疑各选各的 | 前端 `_pdfTransModel` / `_qaModel` 两个变量 | 共用一个下拉框 → 想换翻译就连带换掉答疑 |
| 答疑默认模型 | `llm.qa_model_name()`（`LLM_QA_MODEL`，默认 M3.1-Flash） | 答疑**不**跟随主模型（主模型是直译型 M3） |
| 换模型=真重译 | 翻译缓存键里带模型名 | 换模型必然 cache miss，不会命中旧译文 |
| 额度耗尽自动降级 | `llm.py` `DualModelManager` | 主模型返回 429 → 立刻降级到 Agnes；每 15 分钟探测主模型，恢复自动升回 |
| 慢模型放宽时限 | `app.py` `_translation_deadline_seconds` | `agnes` → 时限 ×2.5；换模型 → ×1.8（否则慢模型必然 504） |
| 不支持 `thinking` 参数的模型 | `llm.py` `should_send_thinking_disabled` | **按 provider** 判定（见 §3.1） |

### 3.1 thinking 参数必须**按 provider** 判定（2026-10-26 实测）

⚠️ **不能一刀切**。实测四个模型对 `thinking={"type":"disabled"}` 的反应完全不同，
而且其中两种是**静默失败**（HTTP 200、看起来成功、内容是空的）：

| 模型 | 传 disabled | 不传 | 正确做法 |
|---|---|---|---|
| MiniMax-M3 | 7.6s / 2033 字 ✅ 干净 | 32.5s / 混入思考文本 | **传** |
| MiniMax-M3.1-Flash | **HTTP 400 直接拒** | 正常（强制思考） | **不能传** |
| Agnes 3.0 Flash | **98.6s / 正文 0 字** ❌ | 27.9s / 2092 字 ✅ | **不能传** |
| DeepSeek Flash | 3.9s / 906 字 ✅ 干净 | 20.9s / **正文 0 字** ❌ | **必须传** |

两个必须记住的坑：

1. **Agnes 传 disabled 会静默吃光预算**：不报错、返回 200，但
   `reasoning_content` 有 9072 字符、`content` 一字没有、`max_tokens` 4000
   全被思考吃掉 → 用户等 98 秒拿到空白。
   原代码对所有模型统一发这个参数 → **选 Agnes 翻译必然白等**。
2. **DeepSeek 不传反而空**：它默认开着思考（reasoning 8293 字符、content 0 字）。

→ 实现：`llm.should_send_thinking_disabled(cfg, disable_thinking)`，
判据走 **provider**（模型名会变，provider 不会）。
护栏：`tests/test_llm_thinking_params.py`（16 条，含行为级断言：看发出去的 URL 和 payload）。

### 3.2 "agnes" 是**备用模型别名**，不是模型名（2026-10-26 实测 bug）

`model_name="agnes"` 曾被当成"主模型网关下的另一个模型名" →
primary 被改成 `model="agnes"` → 拿着 MiniMax 的 key 去打 →
**HTTP 400 invalid params, unknown model 'agnes'**（用户选 Agnes 翻译时必现）。

→ 修法：命中 fallback 配置时走 `force_fallback` 链路，**不改** primary。
三条调用路径（`chat` / `chat_async` / `chat_stream_async`）都要处理。

### 3.3 翻译与答疑的模型清单是**两套**

| 场景 | 诉求 | 默认 | 选项 |
|---|---|---|---|
| 翻译 + 抄表 | 快 + 完整（**关思考**） | MiniMax-M3 | M3 / Agnes Flash / DeepSeek Flash（+ 思考型标注⚠） |
| 划词答疑 | 会推导（**要思考**） | MiniMax-M3.1-Flash | M3.1-Flash / DeepSeek Flash / Agnes Flash |

> 翻译的 `disable_thinking` 走 `translation_disable_thinking()`（默认开）；
> 答疑按所选模型给：M3.1-Flash 不关（强制思考）、DeepSeek 关（否则正文空）、
> Agnes 不关（否则正文空）。

---

## 4. 加"自己的 API 模型"：已开好的 3 个口子

### 4.0 DeepSeek 已经是内置 provider（2026-10-26）

DeepSeek **不用填 .env 就能用** —— 阅读器右上角模型下拉框里直接有：

- `DeepSeek V4.1 Flash（默认）` —— 支持读图，抄表 1.9s，整页翻译 8.1s
- `DeepSeek V4 Pro` —— 推理更强（答疑 11.2s），不支持读图

它的额度、限流档、base_url、key 都是**独立的**（不占 MiniMax 的 5 小时窗口，
也不占 Agnes 的慢档）。key 在 `.env`：

```env
DEEPSEEK_API_KEY=sk-xxxxxx          # https://platform.deepseek.com/api_keys
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_RPM=30
```

没配 key → 下拉框里照常显示，但标注「⚠ 没配 API key」，不静默失败。

代码位置：`llm.deepseek_model_configs()` / `llm.resolve_model_config()` /
`llm.VISION_CAPABLE_MODELS`。

### 4.1 在 `.env` 里按编号配置（最多 5 个）

```env
LLM_CUSTOM_1_NAME=我的视觉模型
LLM_CUSTOM_1_BASE_URL=https://api.deepseek.com/v1
LLM_CUSTOM_1_MODEL=deepseek-chat
LLM_CUSTOM_1_API_KEY=sk-xxxxxx      # 留空 → 复用 LLM_API_KEY
LLM_CUSTOM_1_RPM=30                 # 每分钟请求上限
```

**重启工作台**后，它就会出现在阅读器右上角的模型下拉框里，选中即用于
**翻译 / 抄表 / 划词答疑**三处。`app.py` `translation_model_choices()`
把内置项、DeepSeek 项和自定义项拼在一起下发（`GET /api/llm/translation_models`）。

> 前提：**OpenAI 兼容**（`POST {base_url}/chat/completions`）+ **支持图片输入**
> （表格/公式靠多模态读图；纯文本模型抄不出表格）。
>
> ⚠️ 自定义模型**不在** `llm.VISION_CAPABLE_MODELS` 白名单里 → 抄表时会
> **自动改用**已验证的视觉模型，不会用你的模型去读图（防编造表格）。
> 想要自己的模型直接抄表，得先把它加进白名单并实测过数值。

### 4.2 限流按模型配（不再是 primary/agnes 二分档）

- 免费版 **Agnes** 仍是严格档：11 RPM / 6 秒间隔（`agnes_limiter`）
- 主模型（MiniMax）宽松档：30 RPM / 2 秒间隔（`primary_limiter`）
- **自定义模型按它自己的 `LLM_CUSTOM_N_RPM` 单独一档**（默认 30 RPM / 2 秒间隔
  —— 付费模型不吃严格限流；对方限流更紧时把 RPM 调小）

实现：`llm.limiter_for_config(cfg)` 按配置取限流器，同档共用一个 limiter。

### 4.3 翻译路径的表格读图跟随所选的模型

- `POST /api/paper/read_table`（独立接口）传 model ✅
- `app.py` `_prepare_tables()`（整页翻译走的那条）也传 model ✅
  （2026-10-25 补上：之前只改了独立接口，主路径漏了 → 手动选 Agnes 翻译时
  表格还偷跑 M3，既不一致又烧 M3 的额度）

### 4.4 连不上 → 自动退回

选了自定义模型但它连不上 / 报错时，**自动退回**主模型 → 备用模型链路，
不会把用户卡死（`llm.chat` 的自定义分支失败后继续往下走主/备链路，
并往 stderr 打一行留痕）。

**界面必须显示"实际"用的模型**：翻译接口返回 `model`（实际）+ `model_fell_back`
（是否退回过），前端据此显示"✅ 已翻译 · MiniMax-M3（已自动退回）"并弹提示。
显示"用户选的那个"会让用户以为"换模型没生效"。

流式答疑（`chat_stream_async`）同理：自定义模型失败且**还没吐出内容**时退回；
已经吐了一半就不换（否则两段答案会粘在一起）。

---

## 5. 已知缺口（2026-10-25 记录）

1. ~~**抄表返回值没有校验**~~ → **已修**（2026-10-25）：
   `_looks_like_markdown_table()` 判返回值里有没有竖线；没有 → 判失败 →
   走已有的兜底「【表格未能读出，请对照左侧 PDF 原文】」。
   模型回一句"截图里没有表格"不会再被当表格拼进译文。
2. ~~**降级状态对表格的影响没有提示**~~ → **已修**：
   降格时弹一次提示（主模型额度用完 → 已自动降级到免费备用模型），
   模型下拉框的"跟随当前设置"会标出"（已降格到备用模型）"。
3. ~~**无视觉模型会被派去抄表**~~ → **已修**（2026-10-26）：
   `deepseek-v4-pro` 实测会**编造整张表**（格式像真的、数值全错）。
   现在抄表路径做视觉能力检查，白名单外的模型自动换成已验证的视觉模型。
   护栏：`tests/test_llm_deepseek.py::test_read_table_vision_rejects_non_vision_model`
   （反向验证过：拆掉守卫 → 这条立刻变红）。
4. **自定义模型默认不参与抄表**（2026-10-26 记录）：
   用户自己的模型不在视觉白名单里 → 抄表会改用 `deepseek-flash`。
   这是**刻意**的保守设计（宁可读不出，不要假表）。若用户确认自己的模型
   读图准确，可把它加进 `llm.VISION_CAPABLE_MODELS` 并**实测数值**后再改。

---

## 5B. 思考型模型的预算：思考与正文共用同一个池（2026-10-26 实测）

### 5B.1 现象

用户反馈："后面问任何问题都显示的是在思考，但是没有回答问题。就是不管问啥，
都变成这样。"

界面表现 = 进度徽标一直停在「模型还在想（未产出内容）」，
AI 活动面板计时器一直走，**正文一个字都没有**。

### 5B.2 根因（实测钉死）

**M3.1-Flash 强制思考，思考 token 与正文 token 共用同一个 `max_tokens` 预算池。**

答疑接口原来给 `max_tokens=4000` / `timeout=120`。
实测（7K 字上下文 + 真实生产提示词 + 真实模型）：

| max_tokens | timeout | 总耗时 | 首字 | 正文 |
|---|---|---|---|---|
| **4000**（原值） | 120 | 59.9s | **从未产出** | **0 字** → 报"未产出正文" |
| **16000**（现值） | 300 | 86.2s | 70.3s | **4884 字** ✅ |

端到端复测（真实接口 + 用户截图里的场景）：**42.2s / 3071 字**，含公式与对比表。

→ 4000 的预算被思考吃光 → 正文 0 字。
→ **给足预算就正常了**（用户原话："他即使思考再久也行，我只要他能回答问题"）。

### 5B.3 三处一起改（漏一处就有一个入口继续"只思考不回答"）

`app.py` 新增两个常量，三个答疑接口**全部**改用：

```python
QA_MAX_TOKENS = 16000
QA_TIMEOUT = 300
```

| 接口 | 原值 | 现值 |
|---|---|---|
| `/api/paper/explain_selection_stream`（划词答疑） | 4000 / 120 | `QA_MAX_TOKENS` / `QA_TIMEOUT` |
| `/api/paper/explain_stream`（章节解读） | **1200** / 默认 **60** | `QA_MAX_TOKENS` / `QA_TIMEOUT` |
| `/api/paper/cross_qa_stream`（跨章节问答） | 4000 / 默认 60 | `QA_MAX_TOKENS` / `QA_TIMEOUT` |

⚠️ 章节解读原来只有 **1200 tokens + 60 秒**，是最严重的那个入口。

### 5B.4 ⚠️ 空正文后的「关思考重试」对强制思考模型是**空操作**

`chat_stream_async` 里空正文会重试一次（`disable_thinking=True`）。
但 `should_send_thinking_disabled(M3.1-Flash, True)` 返回 **False**
→ 参数**根本不会下发** → 发出去的请求与第一次**完全相同**
→ 再等 55 秒，还是 0 字（实测 55s → 111s 才报错）。

→ 修法：先判断这个模型**到底能不能关思考**：
- 能关 → 走原来的"关思考重试"（请求真的变了，值得重试）
- **不能关（强制思考型）→ 改成「抬高 max_tokens 再试」**（思考与正文共用池，
  给足预算就能出正文）

护栏：`tests/test_qa_capability_boundary.py::test_empty_body_retry_is_not_a_noop_for_thinking_models`

### 5B.5 思考强度：`reasoning_effort`（2026-10-26 实测）

用户诉求："思考型模型思考要三十秒到九十秒……你把它给设定成 low，
思考强度设定的低一点。Max token 可以设定高一点，
反正就保证他都能正常运行就行。"

之前只有"开/关"两档（`thinking.type=disabled` / 不传），
强制思考模型被卡在中间：**关不掉**，只能全开。

#### 上游认这个参数（MiniMax 400 报错原文点名了它）

```
invalid params, model "MiniMax-M3.1-Flash" requires adaptive thinking;
  reasoning effort none is not allowed (2013)

invalid params, model "MiniMax-M3.1-Flash" requires adaptive thinking;
  thinking.type="disabled" (including reasoning.effort=none) is not allowed
```

→ `reasoning_effort` 是**上游真实支持的参数**，只是 `none` 被强制思考模型拒。

#### `low` 真的能压低思考量（同一 7K 上下文 + 同一模型）

DeepSeek Flash（独立额度，2026-10-26 实测）：

| reasoning_effort | 耗时 | 正文 | **思考量** |
|---|---|---|---|
| 不传 | 55.5s | 4609 字 | **7572 字** |
| **low** | **17.8s** | 3201 字 | **1052 字**（↓ 86%） |
| none | 13.5s | 3310 字 | **9 字** |

→ 思考量降到 1/7，耗时从 55 秒降到 18 秒。

⚠️ 但**短问题上 low 反而更多**（实测思考 73 → 157 字）——
`low` 不是"思考更少"的保证，只是"别过度思考"。
真正要快，靠 `none`（但强制思考模型不给用）。

#### 判定规则（`llm.reasoning_effort_for`）

| provider | `none` | `low` |
|---|---|---|
| DeepSeek / Agnes | ✅ 可用（思考 73 → 9 字） | ✅ |
| MiniMax M3.1-Flash | ❌ **400** → 自动降级成 `low` | ✅（实测通过校验） |
| 未知网关 | 不发（保守） | 不发 |

**判错的后果是 HTTP 400（请求直接被拒）**，所以未知网关一律不发。

### 5B.6 通用教训

- **思考型模型的 `max_tokens` 要按"思考 + 正文"算，不是按正文算**。
  翻译给 4000 是给直译型模型（关思考）用的；答疑给同一个数就必然翻车。
- **时限只该防"真卡死"，不该砍掉还在正常思考的请求**。
- **同一个坑要在同一层所有入口一起修** —— 三个接口三个不同的值，
  漏一个就有一个入口继续犯同样的毛病（这次章节解读最严重：1200/60）。
- **"开/关"两档不够用时要找第三档** —— 上游往往支持
  `reasoning_effort` 这种中间档；**从上游的 400 报错原文里能挖出来**
  （MiniMax 那句 "including reasoning.effort=none" 就是这么发现的）。

---

## 6. 改这块时必须跑

```bash
python -m pytest tests -q -m "not slow"
node microbench/_test_reader_markdown.js
```

> ⚠️ **必须带 `-m "not slow"`**：不带会连上 arxiv 下载整篇 PDF，卡死在网络测试上。

- `tests/test_reader_formula_table.py` —— 提示词里的表格/公式契约
- `tests/test_reader_table_vision.py` —— 表格多模态读图契约
- `tests/test_reader_annotations.py` —— 批注契约
- `tests/test_llm_models.py` —— **模型层契约**（自定义模型解析 / 限流分档 /
  抄表校验 / 表格跟随模型 / 答疑带模型）
- `tests/test_llm_deepseek.py` —— **DeepSeek provider 契约**
  （配置解析 / 独立限流档 / **视觉能力守卫** / 界面清单 / 缓存键）
- `tests/test_llm_thinking_params.py` —— **thinking 参数按 provider 判定**
  （Agnes 静默吃光预算 / DeepSeek 不发就空 / M3.1-Flash 撞 400 /
  "agnes" 别名不是模型名 / 只有 Agnes 严格限流）
  —— 这两条新文件都做过**反向验证**（拆掉修复 → 对应断言立刻变红）。

**换模型相关的手工验证**（没有自动化护栏，必须实测）：

1. 用 `POST /api/paper/read_table` 指定模型，读一张真表，比对数值
2. 用 `POST /api/paper/translate_page` 指定模型，翻译一页，看长度与耗时，
   以及返回的 `model` 是不是**实际**用的那个
3. 换模型后缓存要 miss（同一页重新翻一遍）
4. 自定义模型填一个**不可达**的地址 → 验证"连不上自动退回"仍然出译文
5. **选一个不支持视觉的模型**（如 `deepseek-v4-pro`）跑 1 → 表格数值必须
   仍然正确（说明被自动换成视觉模型了），不能出现编造的表

> ⚠️ 评估任何模型能不能抄表，**输入必须取自生产链路的真实产物**
> （`paper_text.extract_page_text_from_path` 出来的真实坐标），
> 拿手写猜的坐标去测会得到"这个区域没有表格"的回答 —— 那是**答对了**。

