# 审计报告：对方设备（30461）的两次提交（2026-10-27）

> **审计对象**：`e28346d`（平板端答疑预算）+ `43a2986`（平板+性能优化）
> **上游起点**：本机（25353）的 `2216afa`
> **审计方式**：读代码 → 跑全量测试 → **反向验证护栏** → 实测性能 → 浏览器实测
>
> **结论**：**通过，可以保留**。发现 1 个需要知悉的风险点、2 个可改进项，无阻断缺陷。

---

## 0. 一句话

对方的工程质量**高于我的基线**：两次提交都带了完整交接文档、
都做了独立审查、护栏是**行为级**的（不是字符串断言），
且我逐条破坏验证过——**9 处破坏全部让对应护栏变红**（恒真护栏 = 没护栏）。

---

## 1. 审计清单（逐项都有证据）

| 审计项 | 方法 | 结果 |
|---|---|---|
| 全量测试 | `pytest tests -q -m "not slow"` | ✅ **518 passed / 6 skipped** |
| 护栏是否恒真 | **逐条破坏实现 → 断言必须变红** | ✅ **9/9 全部变红** |
| PDF 缓存效果 | 实测连开同一篇 3 次 | ✅ 1720ms → 716ms（**2.4 倍**） |
| 首屏并发 | 实测 5 端点并发 | ✅ 总 43ms ≈ 最慢 31ms（**完全并行**） |
| 空正文兜底 | 读代码 + 破坏验证 | ✅ 强制思考模型走「加预算」而非空转重试 |
| 序号守卫 | 读代码 + 破坏验证 | ✅ 旧请求回调不碰共享状态 |
| 能力边界口径 | 实测真实接口输出 | ✅ 先给实质内容，最后一句才说做不到 |
| 降格标记 | 读代码 | ✅ 按**这次实际用的模型**判定，不看全局状态 |
| 平板端提示词漂移 | 读代码 | ✅ 抽成共享常量，物理上不可能漂移 |
| 行尾编码 | `git ls-files --eol` | ✅ 全部 LF |

### 反向验证明细（9 处破坏）

| 破坏动作 | 对应护栏 | 结果 |
|---|---|---|
| 缓存键漏掉 `EXTRACTOR_VERSION` | `test_cache_key_includes_the_extractor_version` | ✅ 变红 |
| 缓存键漏掉 PDF 签名 | `test_cache_key_changes_when_the_pdf_changes` | ✅ 变红 |
| 缓存目录挪进同步根 | `test_cache_dir_is_under_the_repo_logs` | ✅ 变红 |
| 坏缓存重新抛异常 | `test_corrupted_cache_is_treated_as_a_miss` | ✅ 变红 |
| 空 pages 也当命中 | `test_corrupted_cache_is_treated_as_a_miss` | ✅ 变红 |
| 平板端不再拒绝 `none` | `test_forced_thinking_model_never_gets_reasoning_effort_none` | ✅ 变红 |
| 平板端强制思考判定失效 | `test_forced_thinking_model_never_gets_thinking_disabled` | ✅ 变红 |
| 平板端预算改回 8000 | `test_qa_budget_is_enough_for_thinking_models` | ✅ 变红 |
| 白名单改成子串匹配 | `test_reasoning_effort_uses_an_exact_host_whitelist` | ✅ 变红 |

---

## 2. 值得肯定的三处（比我做得好的地方）

### 2.1 护栏是**行为级**的

`tests/test_pdf_cache.py` 里没有一句 `assert "xxx" in src`，
而是**改实现看结果变不变**：

```python
def test_cache_key_includes_the_extractor_version(tmp_path):
    orig = pdf_cache.EXTRACTOR_VERSION
    try:
        pdf_cache.EXTRACTOR_VERSION = orig + 1
        key2 = pdf_cache.cache_key(pdf, None)
    finally:
        pdf_cache.EXTRACTOR_VERSION = orig
    assert key != key2
```

对比我自己那轮（`test_qa_capability_boundary.py`）里仍有
`assert "..." in src` 的源码级断言 —— 对方的更硬。

### 2.2 平板端判定只看**网关**，不看模型名

平板用户可以把自己的接口指向**任意** OpenAI 兼容网关，
那种网关上照样挂着「MiniMax-M3.1-Flash」这个模型名，
但它**不一定认 `reasoning_effort`** → 发了就 400。

对方据此把判定改成**精确主机名白名单**
（并专门修掉了子串匹配的坑：`minimax.中转.example.com` 也会命中子串）。

**我桌面端现在还是「base + 模型名」一起判** —— 见 §3.1。

### 2.3 共享常量防漂移

能力边界那段原来在 `SYS_RESEARCH` / `SYS_SUMMARY` 里各抄一份，
抄着抄着差了半句。对方抽成 `CAPABILITY_BOUNDARY` 共享常量，
并加护栏断言两者相同。

---

## 3. 发现的问题

### 3.1 ⚠️ 桌面端 `reasoning_effort` 判定比平板端宽（对方已指出，未改）

**现状**：`microbench/llm.py` 的 `cfg_accepts_reasoning_effort()`
用**子串匹配**：

```python
_REASONING_EFFORT_OK_PROVIDERS = ("deepseek", "agnes", "minimaxi", "minimax")
hay = f"{base} {model}"
if any(p in hay for p in _REASONING_EFFORT_OK_PROVIDERS):
```

**风险**：用户在 `.env` 里填自建网关，base 叫 `https://my-relay.example.com/v1`，
模型名填 `MiniMax-M3.1-Flash` → `hay` 里含 "minimax" → **命中** → 发了
`reasoning_effort` → 那个网关不认 → **HTTP 400，请求直接被拒**。

**为什么现在没爆**：桌面端的自定义模型必须走 `LLM_CUSTOM_N_*`，
而内置的 MiniMax/DeepSeek/Agnes 确实认这个参数。

**为什么还没改**：改了要同步 `tests/test_llm_thinking_params.py` 的断言；
对方判断「目前没接到用户投诉，先不动」。

**我的判断**：**应该改**。理由：
- 判错的后果是 **400 直接拒绝**（不是静默失败），
  用户会看到"请求被拒"且不知道为什么；
- 平板端已经改对了，桌面端跟着改能保持两端一致；
- 改动很小（换精确主机名白名单），护栏同步改即可。

**风险等级**：低（要用户自己填中转站才会触发）→ 建议下一轮改。

### 3.2 ℹ️ `/api/stats` 冷启动 2.9 秒

实测：第一次 92ms（缓存刚过期）→ 之后 2ms；但**服务重启后的第一次**
要 2884ms（`_real_output_stats()` 全扫 vault + 同步根）。

**影响**：只影响**服务重启后的第一次**看板请求，且它现在跑在线程池里
（不阻塞其他请求）。15 秒 TTL 之后又会再扫一次。

**对方已在文档里标明**（「`_real_output_stats()` 扫同步根：有 15 秒缓存」），
属于有意留下。不阻断。

### 3.3 ℹ️ 「专注版 / 经典版」**不在这次推送里**

用户提到对方做了「专注版（经典版）」来适配小屏。
但：
- 远程只有 `origin/main` 一个分支，**没有别的分支**；
- 没有 tag；
- 全仓搜「专注版 / 经典版 / 专注模式」**零命中**；
- 这次推送的 `microbench/static/tablet/` 只改了 `app.js`（418 行），
  **没改平板的 HTML/CSS**。

→ **那个版本应该还在对方设备本地，还没推上来。**
本轮审计的对象是「平板端答疑预算 + 桌面端性能优化」，
**不含**「专注版/经典版」。等对方推上来再审。

---

## 4. 对我方（桌面端）的后续建议

| # | 建议 | 优先级 | 说明 |
|---|---|---|---|
| 1 | 桌面端 `reasoning_effort` 判定改成**精确主机名白名单** | 中 | 与平板端对齐；防自建网关 400 |
| 2 | 桌面端护栏补几条**行为级**断言 | 低 | 对方的 `test_pdf_cache.py` 是范本 |
| 3 | `/api/stats` 冷启动单独预热 | 低 | 不阻断，先不做 |

---

## 5. 保留决定

**保留对方的两次提交，不做任何回退。** 理由：

1. 修复的是**同一个用户症状**（平板端也在"一直思考不回答"），
   方向正确；
2. 全部有实测数据支撑（2496ms→50ms、55.5s→17.8s 等）；
3. 护栏经反向验证有效，不是假护栏；
4. 全量测试 518 passed，比我提交时的 480 多了 38 条。

**唯一需要跟进**：§3.1（桌面端判定对齐）+ §3.3（专注版还没推上来）。
