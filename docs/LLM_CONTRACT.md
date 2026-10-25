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
| **MiniMax-M3** | ✅ 15.2s / 2918 字 | ✅ 3–6s，`−90｜−175｜−109｜｜~700｜16.5` 与论文一致 | 直译首选 |
| **MiniMax-M3.1-Flash** | ❌ 324s 撞线 → HTTP 504 | ✅ 21–42s，还会主动把表头译成中文 | **只能答疑，不能翻译**（强制思考吃光 token 预算） |
| **Agnes 3.0 Flash** | ✅ 104s / 2773 字（慢 7 倍但完整） | ✅ 25–35s，数值与论文一致 | 额度用完时的免费降级 |
| 自己的 API 模型 | 看是否 OpenAI 兼容 | **看是否支持图片输入** | 纯文本模型抄不出表格 |

> ⚠️ 抄表测的是**生产用的表格区域坐标**（`28,517,536,609` / `28,662,536,754`，
> 来自抽取层 `_find_table_regions`）。拿手写的猜的坐标去测会得到
> "截图里没有表格"的回答 —— 那是**答对了**，那个区域本来就不是表。
> 早期曾据此误判「Agnes 读不了图」，用生产坐标复测后结论相反。

---

## 3. 已经有的换模型基础设施（别重复造）

| 能力 | 在哪 | 怎么做的 |
|---|---|---|
| 界面换模型 | `app.py` `TRANSLATION_MODEL_CHOICES` + `GET /api/llm/translation_models` | 服务端下发选项，前端下拉框 |
| 换模型=真重译 | 翻译缓存键里带模型名 | 换模型必然 cache miss，不会命中旧译文 |
| 额度耗尽自动降级 | `llm.py` `DualModelManager` | 主模型返回 429 → 立刻降级到 Agnes；每 15 分钟探测主模型，恢复自动升回 |
| 答疑跟随降级 | 答疑走 `llm.chat` 默认配置 | **用户不用管**：主模型降级时答疑自动走备用模型 |
| 慢模型放宽时限 | `app.py` `_translation_deadline_seconds` | `agnes` → 时限 ×2.5；换模型 → ×1.8（否则慢模型必然 504） |
| 不支持 `thinking` 参数的模型 | `llm.py` `_THINKING_DISABLE_UNSUPPORTED` | 撞过 400 就去掉参数重试一次，**并记住这个模型**，之后不再发 |

---

## 4. 加"自己的 API 模型"：已开好的 3 个口子

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
把内置 5 项和自定义项拼在一起下发（`GET /api/llm/translation_models`）。

> 前提：**OpenAI 兼容**（`POST {base_url}/chat/completions`）+ **支持图片输入**
> （表格/公式靠多模态读图；纯文本模型抄不出表格）。

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
   降级时弹一次提示（主模型额度用完 → 已自动降级到免费备用模型），
   模型下拉框的"跟随当前设置"会标出"（已降格到备用模型）"。

---

## 6. 改这块时必须跑

```bash
python -m pytest tests -q
node microbench/_test_reader_markdown.js
```

- `tests/test_reader_formula_table.py` —— 提示词里的表格/公式契约
- `tests/test_reader_table_vision.py` —— 表格多模态读图契约
- `tests/test_reader_annotations.py` —— 批注契约
- `tests/test_llm_models.py` —— **模型层契约**（自定义模型解析 / 限流分档 /
  抄表校验 / 表格跟随模型 / 答疑带模型）

**换模型相关的手工验证**（没有自动化护栏，必须实测）：

1. 用 `POST /api/paper/read_table` 指定模型，读一张真表，比对数值
2. 用 `POST /api/paper/translate_page` 指定模型，翻译一页，看长度与耗时，
   以及返回的 `model` 是不是**实际**用的那个
3. 换模型后缓存要 miss（同一页重新翻一遍）
4. 自定义模型填一个**不可达**的地址 → 验证"连不上自动退回"仍然出译文
