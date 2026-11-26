import { showToast, escapeHtml, formatMarkdownLite, renderMathIn } from "../core/utils.js";
import { _readerState, _qaModel } from "../state/store.js";
import { _pdfTransModel } from "./pdf-reader.js";

      // ---------------------------------------------------------------
      // Cancellable LLM ask: tool that wraps fetch + AbortController,
      // swaps the button label to "解答中... ✕ 取消", and exposes a
      // window.cancelActiveAsk() so any cancel handler can fire it.
      //   onSuccess(data) — request completed (200 OK)
      //   onError(err)    — request failed (non-AbortError)
      //   onAbort()       — user cancelled mid-flight (AbortError)
      // ---------------------------------------------------------------
      let _activeAsk = null; // {controller, btn, originalHTML}
      // 答疑请求的递增序号：用于判断"我还是不是当前这次请求"。
      // 新请求会中止旧请求，旧请求的回调靠它识别自己已过期（2026-10-26）。
      let _qaRequestSeq = 0;
      function cancelActiveAsk() {
        if (_activeAsk && _activeAsk.controller) {
          try { _activeAsk.controller.abort(); } catch {}
          showToast("已取消 AI 请求");
        }
      }
      // ⚠ 按钮的「原始标签」**只认第一次**（2026-11-19 用户报「键错掉了就没了」）。
      //   旧实现每次请求都 `const originalHTML = btn.innerHTML` ——
      //   第二次请求捕获到的是**上一轮注入的「⏳ 解答中… ✕ 取消」**，
      //   跑完再 `btn.innerHTML = originalHTML` 恢复成它 →
      //   按钮**永久停在「⏳ 解答中…」**，用户再也发不出下一问。
      //   （真机实测：同一按钮连问两次 → 标签停在「⏳ 解答中... ✕ 取消」❌）
      //   → 把原始标签存在 dataset 上，整条生命周期只记一次。
      function _qaAskOriginalHTML(btn) {
        if (!btn) return "";
        if (!btn.dataset.askOriginalHTML) btn.dataset.askOriginalHTML = btn.innerHTML;
        return btn.dataset.askOriginalHTML;
      }
      // 把按钮复位成它的原始标签（并解除禁用 / 去掉注入的取消键）。
      function _qaResetAskButton(rec) {
        const btn = rec && rec.btn;
        if (!btn) return;
        btn.disabled = false;
        const orig = btn.dataset.askOriginalHTML || rec.originalHTML || btn.innerHTML;
        btn.innerHTML = orig;
        try { delete btn.dataset.askOriginalHTML; }
        catch (e) { btn.removeAttribute("data-ask-original-html"); }
      }
      // DELETED 2026-09-24: askLLMCancelable (48 lines) + window.askLLMCancelable
      // export — 100% dead code. askLLMStream (below) is the only path.
      // cancelActiveAsk stays here because it's wired into askLLMStream's
      // cancel button.
      window.cancelActiveAsk = cancelActiveAsk;

      // ===================================================================
      // AI 活动登记处 —— 让"AI 正在干什么"变得可见
      // ===================================================================
      // 用户原话（2026-10-25）："他有可能等了半天然后什么也没输出"。
      // 一个不动的转圈，用户分不清"在跑"还是"卡了"；等 210 秒才报超时更糟。
      //
      // 做法：所有 AI 请求（翻译 / 章节翻译 / 划词答疑 / 抄表）都登记到这里，
      // 每 500ms 刷新一次"已用时 + 阶段"，并在工具栏挂一个可点开的面板，
      // 每个任务都能直接取消。慢到一定程度就变色预警，而不是让人干等。
      const _AI_TASKS = new Map();      // id -> task（正在跑的）
      const _AI_TASK_HISTORY = [];      // 已结束的（最近 12 条）
      let _aiTaskSeq = 0;
      let _aiTickTimer = null;
      // 浮层是否已经为这些任务自动展开过（每个任务 id 一条；任务全部结束后清空）
      const _aiPanelOpenedFor = new Set();

      function _aiFormatMs(ms) {
        const s = Math.max(0, Math.floor(ms / 1000));
        if (s < 60) return s + "s";
        const m = Math.floor(s / 60);
        return m + "m" + String(s % 60).padStart(2, "0") + "s";
      }

      // 阶段阈值：慢 = 提示，变红 = 很可能卡住/模型在思考
      const _AI_WARN_MS = 60000;
      const _AI_DANGER_MS = 150000;

      function _aiTaskStart(opts) {
        opts = opts || {};
        const id = "ait" + (++_aiTaskSeq);
        const task = {
          id: id,
          kind: opts.kind || "ai",
          label: opts.label || "AI 任务",
          model: opts.model || "",
          stage: opts.stage || "已提交",
          startedAt: Date.now(),
          endedAt: 0,
          state: "running",
          error: "",
          controller: opts.controller || null,
          onStage: opts.onStage || null,   // 运行中：只更新"进度"显示
          onEnd: opts.onEnd || null,       // 收尾：把"进度"显示改成最终状态
        };
        _AI_TASKS.set(id, task);
        _aiTick();
        _renderAiActivity();
        return id;
      }

      function _aiTaskStage(id, stage) {
        const t = _AI_TASKS.get(id);
        if (!t) return;
        t.stage = stage;
        if (t.onStage) { try { t.onStage(stage, t); } catch (e) { /* 忽略 */ } }
        _renderAiActivity();
      }

      function _aiTaskEnd(id, state, err) {
        const t = _AI_TASKS.get(id);
        if (!t) return;
        t.state = state || "done";
        t.error = err || "";
        t.endedAt = Date.now();
        _AI_TASKS.delete(id);
        _AI_TASK_HISTORY.unshift(t);
        if (_AI_TASK_HISTORY.length > 12) _AI_TASK_HISTORY.length = 12;
        // 刻意**不**在这里调 t.onStage：收尾不是"阶段更新"。
        // 之前调了 → translateSection 的 onStage 把 #reader-translation 整个
        // 覆盖成 "done · 0s" → 翻完章节译文消失（独立审查抓到的阻断 bug）。
        //
        // 收尾要走**另一个回调** onEnd：它只负责"把进度显示改成最终状态"
        // （✅ 完成 / ❌ 失败 / ✕ 已取消），不许碰正文区。
        if (t.onEnd) { try { t.onEnd(t.state, t); } catch (e) { /* 忽略 */ } }
        _renderAiActivity();
      }

      function _aiTaskCancel(id) {
        const t = _AI_TASKS.get(id);
        if (!t) return;
        try {
          if (t.controller) t.controller.abort();
        } catch (e) { /* 忽略 */ }
        showToast("已取消：" + t.label);
      }

      // 心跳：每 500ms 刷新计时与阶段显示
      function _aiTick() {
        if (_aiTickTimer) return;
        _aiTickTimer = setInterval(() => {
          if (_AI_TASKS.size === 0) {
            clearInterval(_aiTickTimer);
            _aiTickTimer = null;
            _renderAiActivity();
            return;
          }
          _renderAiActivity();
          // 阶段随时间推进（前端推断：服务端只在结束时才回话）
          _AI_TASKS.forEach((t) => {
            const el = t.startedAt ? (Date.now() - t.startedAt) : 0;
            if (t.kind === "translate" || t.kind === "section") {
              if (el < 4000) _aiTaskStage(t.id, "提交请求");
              else if (el < 25000) _aiTaskStage(t.id, "等待模型响应（含读表）");
              else if (el < _AI_WARN_MS) _aiTaskStage(t.id, "模型处理中（可能在读表格 / 思考）");
              else _aiTaskStage(t.id, "很慢：可能在读表格或模型陷入长思考");
            } else if (t.kind === "qa") {
              // ⚠️ 文案要给出**下一步能做什么**，不能只说"还在想"
              // （用户原话 2026-10-26："他显然就不会再输出了"）。
              // 长思考是 M3.1-Flash 的常态（实测首字 17~27s），
              // 但超过一分钟还一个字没有，就该告诉用户可以换模型/重问了。
              if (el < 8000) _aiTaskStage(t.id, "等待模型首个回答");
              else if (el < 45000) _aiTaskStage(t.id, "模型还在想（尚未产出内容）");
              else if (el < _AI_WARN_MS) _aiTaskStage(t.id, "模型思考较久（尚未产出内容）· 可点取消换模型");
              else _aiTaskStage(t.id, "很慢：模型可能陷入长思考 · 建议取消并换模型重试");
            }
          });
        }, 500);
      }

      function _aiTaskIcon(kind) {
        if (kind === "translate") return "📄";
        if (kind === "section") return "📚";
        if (kind === "qa") return "💬";
        if (kind === "table") return "📊";
        return "🤖";
      }

      function _renderAiActivity() {
        const pill = document.getElementById("ai-activity-pill");
        const panel = document.getElementById("ai-panel");
        if (!pill || !panel) return;
        const running = Array.from(_AI_TASKS.values());
        const n = running.length;

        // 有任务时**自动展开**浮层。
        // 为什么必须自动展开：徽标只是入口，用户不会想到去点它；
        // 而"正在跑 + 跑了多久 + 能取消"是用户此刻最需要的信息
        // （用户原话："他有可能等了半天然后什么也没输出"）。
        // 语义：**每个新任务展开一次**（记录已展开的任务 id）。
        // 之前用"是否有任务"的标志位 → PDF 队列连续翻译时，用户手动关一次，
        // 整个队列期间都不再自动弹（与注释不符，独立审查抓到的）。
        if (n > 0 && !running.every((t) => _aiPanelOpenedFor.has(t.id))) {
          panel.classList.add("open");
          running.forEach((t) => _aiPanelOpenedFor.add(t.id));
        }
        // 任务全部结束后清空记录 —— 下一批任务还能自动展开
        if (n === 0 && _aiPanelOpenedFor.size) {
          _aiPanelOpenedFor.clear();
        }

        // 工具栏上的徽标
        pill.dataset.active = n > 0 ? "1" : "0";
        const oldest = running.reduce((acc, t) => Math.max(acc, Date.now() - t.startedAt), 0);
        const warn = running.some(t => (Date.now() - t.startedAt) > _AI_DANGER_MS);
        pill.dataset.warn = warn ? "1" : "0";
        if (n > 0) {
          pill.innerHTML = `<span class="ai-beat"></span><span>AI 运行中 ${n}</span>`
            + `<span style="opacity:.7">${_aiFormatMs(oldest)}</span>`;
        } else {
          pill.innerHTML = `<span style="opacity:.6">🤖 AI 活动</span>`;
        }

        // 面板
        const body = panel.querySelector(".ai-panel-body");
        if (!body) return;
        const rows = [];
        running.forEach((t) => {
          const el = Date.now() - t.startedAt;
          const isWarn = el > _AI_WARN_MS;
          const isDanger = el > _AI_DANGER_MS;
          rows.push(`
            <div class="ai-task-row" data-state="running">
              <div class="ai-task-top">
                <span class="ai-task-title">${_aiTaskIcon(t.kind)} ${escapeHtml(t.label)}</span>
                <span class="ai-task-model">${escapeHtml(t.model || "跟随默认")}</span>
                <span class="ai-task-elapsed">${_aiFormatMs(el)}</span>
              </div>
              <div class="ai-task-stage" data-warn="${isDanger ? "1" : "0"}">
                <span class="ai-spinner"></span>
                <span>${escapeHtml(t.stage)}</span>
              </div>
              ${isWarn ? `<div class="ai-task-err">已经 ${_aiFormatMs(el)} 没动静了 —— 可能是模型在长思考或卡住，可以取消或换个模型重试</div>` : ""}
              <div class="ai-task-actions">
                <button class="btn-pill-outline" style="font-size:10.5px; padding:2px 9px;"
                        onclick="_aiTaskCancel('${t.id}')">✕ 取消</button>
              </div>
            </div>`);
        });
        _AI_TASK_HISTORY.slice(0, 5).forEach((t) => {
          const el = (t.endedAt || Date.now()) - t.startedAt;
          rows.push(`
            <div class="ai-task-row" data-state="${escapeHtml(t.state)}">
              <div class="ai-task-top">
                <span class="ai-task-title">${_aiTaskIcon(t.kind)} ${escapeHtml(t.label)}</span>
                <span class="ai-task-model">${escapeHtml(t.model || "")}</span>
                <span class="ai-task-elapsed">${_aiFormatMs(el)}</span>
              </div>
              <div class="ai-task-stage">
                <span>${t.state === "done" ? "✅ 完成" : t.state === "cancelled" ? "🚫 已取消" : "❌ 失败"}</span>
              </div>
              ${t.error ? `<div class="ai-task-err">${escapeHtml(String(t.error).slice(0, 160))}</div>` : ""}
            </div>`);
        });
        body.innerHTML = rows.length
          ? rows.join("")
          : `<div class="ai-empty">当前没有 AI 任务。<br>翻页翻译、划词答疑都会在这里显示进度、耗时和取消按钮。</div>`;
      }

      function toggleAiPanel() {
        const panel = document.getElementById("ai-panel");
        if (!panel) return;
        panel.classList.toggle("open");
        _renderAiActivity();
      }
      window.toggleAiPanel = toggleAiPanel;
      window._aiTaskStart = _aiTaskStart;
      window._aiTaskStage = _aiTaskStage;
      window._aiTaskEnd = _aiTaskEnd;
      window._aiTaskCancel = _aiTaskCancel;
      window._aiFormatMs = _aiFormatMs;

      // ---------------------------------------------------------------
      // Streaming variant of the cancellable ask.
      // Reads SSE chunks via fetch + ReadableStream so the caller can
      // render text progressively ("typing" effect) instead of waiting
      // for the full response.
      //
      //   onChunk(text, fullText) — each chunk arrived; fullText = accumulated
      //   onSuccess(fullText)     — [DONE] received
      //   onError(err)            — fetch failed / SSE error event
      //   onAbort()               — user cancelled mid-flight
      //
      // Returns the accumulated full text on success, null on cancel/error.
      // Shares the _activeAsk single-flight guard with askLLMCancelable
      // (so a user can't kick off two LLM panels at once).
      // ---------------------------------------------------------------
      async function askLLMStream(btn, url, payload, options) {
        options = options || {};
        if (_activeAsk) {
          // ⚠️ 用户原话（2026-10-26）："我给他反馈之后他会显示就是已有AI在
          // 回答中这个提问失败……但是他显然就不会再输出了"。
          //
          // 也就是说：**用户想重答时，旧请求还在跑**（模型陷在长思考里，
          // 一直不吐正文），于是单飞守卫直接把新请求拒了 —— 用户被卡死：
          // 既拿不到旧答案，也发不出新问题。
          //
          // 现在改成「**替换**」而不是「拒绝」：新请求接管，把旧的中止掉。
          // 旧请求的 abort 会走它自己的 catch → onAbort/onError 回调，
          // 但那时它已经不是当前面板的主人了（见 _qaRequestSeq 守卫）。
          const old = _activeAsk;
          _activeAsk = null;
          try { if (old && old.controller) old.controller.abort(); } catch (e) { /* 忽略 */ }
          // ⚠ 旧请求的按钮必须在**这里**立即复位（2026-11-19）：
          //   旧请求自己的 finally 会因为序号守卫（_isCurrentAsk() 为 false）
          //   跳过恢复 → 那个按钮**永久卡在「⏳ 解答中…」**。
          //   真机实测：章节视图「问 AI」被答疑面板顶掉后，那个按钮再也点不动。
          _qaResetAskButton(old);
          showToast("已中止上一次 AI 请求，开始新的提问");
        }
        if (!btn) {
          // No button to host the spinner/cancel control — the request is
          // not sent, so the caller's spinner must be cleared explicitly.
          if (options.onError) options.onError(new Error("未找到提问按钮, 无法发起请求"));
          return null;
        }
        const originalHTML = _qaAskOriginalHTML(btn);
        const controller = new AbortController();
        _activeAsk = { controller, btn, originalHTML };
        // 必须禁用：不禁用的话用户再点一次会走上面「替换」分支，
        // 把自己**第一次**请求 abort 掉并弹「已中止上一次请求」——
        // 用户看到的是"我点了一下，它说取消了一次请求"，像是程序崩了。
        btn.disabled = true;
        // ⚠ 注入的「✕ 取消」必须自己把 pointer-events 打开：
        //   父按钮 disabled → `.btn-pill[disabled] { pointer-events: none }`
        //   → 取消键继承 none → **点不动**（用户反馈「只能干等」）。
        //   pointer-events 可以被后代重新打开。
        btn.innerHTML = `<span>⏳ 解答中...</span> <button type="button" data-cancel-ask style="margin-left:6px; padding:1px 8px; font-size:11px; background:var(--accent-terracotta); color:#fff; border:0; border-radius:6px; cursor:pointer; pointer-events:auto;">✕ 取消</button>`;
        const cancelBtn = btn.querySelector("[data-cancel-ask]");
        if (cancelBtn) cancelBtn.onclick = cancelActiveAsk;

        // ⚠️ 请求序号守卫（2026-10-26）：
        // 新的请求会 abort 掉旧的（见上面的"替换"逻辑），但旧请求的
        // catch → onError/onAbort 回调**仍会执行**（AbortError 走的是
        // onAbort 分支）。如果不加序号守卫，旧请求的回调会把**新**请求
        // 正在写的答案区覆盖成"已取消"，用户看到的就是新问题刚开跑就被
        // 自己的旧请求擦掉了。
        // → 每次发请求领一个递增序号；回调只在"我还是当前主人"时才生效。
        const _askSeq = ++_qaRequestSeq;
        const _isCurrentAsk = () => _qaRequestSeq === _askSeq;

        // 登记到「AI 活动」：答疑最容易出现"等半天没输出"（模型在思考），
        // 这里给出实时已用时 + 阶段，面板里也能直接取消。
        const _aiQaTaskId = _aiTaskStart({
          kind: "qa",
          label: options.label || "AI 解答",
          // ⚠ 2026-11-25 审计 P1：这里原来登记的是 **_pdfTransModel（翻译模型）**
          //   —— 答疑与翻译是**两个独立**的模型选择（`_qaModel` vs `_pdfTransModel`）。
          //   → 用户选了「答疑用 Agnes 3.0 Flash」，AI 活动面板却显示翻译模型，
          //     看起来像「选模型没生效」；排查方向直接被带偏。
          model: (_qaModel || "") || "跟随默认",
          controller: controller,
          onStage: (stage, t) => {
            if (options.onStage) { try { options.onStage(stage, t); } catch (e) { /* 忽略 */ } }
          },
        });
        let _aiQaGotText = false;

        let accumulated = "";
        let meta = null;
        try {
          const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
            signal: controller.signal,
          });
          if (!res.ok) {
            // Try to extract FastAPI error detail from JSON body
            let detail = `HTTP ${res.status}`;
            try {
              const errBody = await res.json();
              if (errBody && errBody.detail) detail = String(errBody.detail);
            } catch {}
            throw new Error(detail);
          }
          if (!res.body) throw new Error("响应无 body (SSE 不可用)");

          const reader = res.body.getReader();
          const decoder = new TextDecoder("utf-8");
          let buffer = "";
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            // SSE events separated by \n\n
            const events = buffer.split("\n\n");
            buffer = events.pop(); // keep last incomplete chunk in buffer
            for (const evt of events) {
              const line = evt.trim();
              if (!line.startsWith("data: ")) continue;
              const data = line.slice(6);
              if (data === "[DONE]") {
                // ⚠️ 空正文不能当成成功（2026-10-26 浏览器实测）：
                // 上游可能正常发 [DONE] 但一个可见字符都没吐（思考烧光预算）。
                // 这时若照常调 onSuccess("")，界面上就是一个空答案 +
                // "✅ 完成"，用户看到的就是「显示在回答、实际没有回答」。
                // → 明确报错，并给出可操作的下一步。
                if (!accumulated.trim()) {
                  const emptyErr = new Error(
                    "模型没有产出任何内容（只输出了思考过程，正文为空）。" +
                    "换个模型再试，或把问题问得更具体一点。"
                  );
                  _aiTaskEnd(_aiQaTaskId, "error", emptyErr.message);
                  if (options.onError) options.onError(emptyErr);
                  return null;
                }
                if (options.onSuccess) options.onSuccess(accumulated, meta);
                _aiTaskEnd(_aiQaTaskId, "done");
                return accumulated;
              }
              let obj;
              try { obj = JSON.parse(data); }
              catch { continue; } // skip malformed
              if (obj.cancelled) {
                if (_isCurrentAsk() && options.onAbort) options.onAbort();
                _aiTaskEnd(_aiQaTaskId, "cancelled");
                return null;
              }
              if (obj.error) {
                throw new Error(String(obj.error));
              }
              if (obj.meta) {
                meta = obj.meta;
                if (options.onMeta) options.onMeta(meta);
              }
              if (obj.text) {
                if (!_aiQaGotText) {
                  _aiQaGotText = true;
                  _aiTaskStage(_aiQaTaskId, "正在生成回答");
                }
                accumulated += obj.text;
                if (options.onChunk) options.onChunk(obj.text, accumulated);
              }
            }
          }
          // Stream ended without [DONE] — treat as soft complete
          if (!accumulated.trim()) {
            const emptyErr = new Error(
              "模型没有产出任何内容（连接结束但正文为空）。换个模型再试。"
            );
            _aiTaskEnd(_aiQaTaskId, "error", emptyErr.message);
            if (options.onError) options.onError(emptyErr);
            return null;
          }
          if (options.onSuccess) options.onSuccess(accumulated, meta);
          _aiTaskEnd(_aiQaTaskId, "done");
          return accumulated;
        } catch (err) {
          if (err && err.name === "AbortError") {
            // ⚠️ 只有"我还是当前主人"时才通知界面。
            // 否则：这个请求是被**新请求**替换掉的，它的中止回调会把
            // 新请求正在写的答案区覆盖成"已取消"。
            if (_isCurrentAsk() && options.onAbort) options.onAbort();
            _aiTaskEnd(_aiQaTaskId, "cancelled");
            return null;
          }
          _aiTaskEnd(_aiQaTaskId, "error", err && (err.message || String(err)));
          if (options.onError) options.onError(err);
          return null;
        } finally {
          // ⚠️ 只有当前主人才能清 _activeAsk —— 被替换掉的旧请求收尾时
          // 若把 _activeAsk 清了，会把**新**请求的登记一起抹掉
          // （新请求就再也取消不掉了）。
          if (_isCurrentAsk()) {
            _activeAsk = null;
            // ⚠ 用 dataset 里存的**原始标签**复位 —— 不能再用
            //   `btn.innerHTML = originalHTML`（那可能是上一轮注入的流式标签
            //   「⏳ 解答中… ✕ 取消」→ 按钮永久停在这个状态）。
            _qaResetAskButton({ btn: btn, originalHTML: originalHTML });
          }
        }
      }
      window.askLLMStream = askLLMStream;


    function _buildExplainPayload(question) {
      const active = _readerState.activeSection;
      return {
        // Backend caps: question 500 / sections max 100 / paper_title 300.
        question: (question || "").slice(0, 500),
        sections: (_readerState.sections || []).map(s => ({
          section: s.section, title: s.title, number: s.number || "",
          text: s.text, page_start: s.page_start, page_end: s.page_end,
        })),
        // 用户当前所在的那一节 → 必须出现在上下文里
        focus_index: (active !== null && active !== undefined) ? active : null,
        paper_title: (_readerState.title || "").slice(0, 300),
        model: _pdfTransModel || "",
      };
    }

    async function askPaperQuestion() {
      const qEl = document.getElementById("ask-paper-question");
      const question = (qEl ? qEl.value.trim() : "");
      // ⚠ 2026-11-17 全面审查 S2：后端 ExplainRequest.question 是
      //   `min_length=2`，前端只判空 → 单字符提问会拿到 422，
      //   SSE 面板弹出原始校验错误（用户看不懂）。
      if (question.length < 2) {
        showToast(question ? "问题至少写两个字" : "请输入问题", true);
        return;
      }
      // 发出即清空：问下一题不用先手动删掉上一个问题
      if (qEl) {
        qEl.value = "";
        try { qEl.focus(); } catch (e) { /* 忽略 */ }
      }
      const askBtn = document.querySelector('[onclick="askPaperQuestion()"]');
      const el = document.getElementById("ask-paper-result");
      el.style.display = "block";
      el.innerHTML = `
        <div class="reader-section-header">
          <div class="reader-section-title">🤖 ${escapeHtml(question)}</div>
        </div>
        <div class="reader-section-body" data-stream-target style="white-space:pre-wrap; min-height:1.4em;">⏳ AI 解读中...</div>`;
      const streamTarget = el.querySelector("[data-stream-target]");
      await askLLMStream(askBtn, "/api/paper/explain_stream", _buildExplainPayload(question), {
        onChunk: (chunk, full) => {
          // Live typing effect: append raw text (no markdown formatting
          // mid-stream — too expensive and janky)
          streamTarget.textContent = full;
          // Auto-scroll if container scrolls
          streamTarget.parentElement.scrollTop = streamTarget.parentElement.scrollHeight;
        },
        onSuccess: (full) => {
          // Format the final accumulated text once [DONE] arrives
          streamTarget.innerHTML = formatMarkdownLite(full);
          renderMathIn(el);
        },
        onError: (err) => {
          streamTarget.innerHTML = `<div style="color:var(--accent-terracotta);">❌ ${escapeHtml(err.message)}</div>`;
        },
        onAbort: () => {
          streamTarget.innerHTML = `<div style="color:var(--text-muted);">✕ 已取消本次解读</div>`;
        }
      });
    }

    function presetQuestion(q) {
      document.getElementById("ask-paper-question").value = q;
      askPaperQuestion();
    }

    async function crossSectionQA() {
      const qEl = document.getElementById("cross-qa-question");
      const question = (qEl ? qEl.value.trim() : "");
      // ⚠ 同 S2：后端 CrossQARequest.question 同样是 `min_length=2`。
      if (question.length < 2) {
        showToast(question ? "问题至少写两个字" : "请输入跨章节问题", true);
        return;
      }
      if (_readerState.sections.length < 2) {
        showToast("跨章节问答需要至少 2 个章节 (本论文章节不足)", true);
        return;
      }
      // 同样：发出去就清空输入框，问下一题不用先手动删。
      if (qEl) {
        qEl.value = "";
        try { qEl.focus(); } catch (e) { /* 忽略 */ }
      }
      // Collect selected sections (default: all non-references sections)
      const checked = Array.from(document.querySelectorAll(".cross-qa-section-cb:checked"))
        .map(cb => parseInt(cb.dataset.idx, 10));
      // ⚠️ 不要再 `.slice(0, 20)`（2026-11-19，25353 台）：长论文的后半部分
      // 会被**静默丢弃**，用户勾到的那一节可能根本不在请求里。
      // 后端上限已放宽到 100（该限的是每节字符预算，不是节数）。
      const MAX_SECTIONS = 100;
      const cappedSections = _readerState.sections.slice(0, MAX_SECTIONS);
      const sections = (checked.length >= 2
        ? checked.filter(i => Number.isInteger(i) && i >= 0 && i < cappedSections.length)
        : null);
      const el = document.getElementById("cross-qa-result");
      el.style.display = "block";
      el.innerHTML = `
        <div class="reader-section-header">
          <div class="reader-section-title">🔗 跨章节综合 · ${escapeHtml(question)}</div>
          <div class="reader-section-meta" data-cross-qa-meta style="color:var(--text-muted);">⏳ 综合多章节分析中...</div>
        </div>
        <div class="reader-section-body" data-stream-target style="white-space:pre-wrap; min-height:1.4em;"></div>`;
      const streamTarget = el.querySelector("[data-stream-target]");
      const metaTarget = el.querySelector("[data-cross-qa-meta]");
      const askBtn = document.querySelector('[onclick="crossSectionQA()"]');
      await askLLMStream(askBtn, "/api/paper/cross_qa_stream", {
        // Backend caps: question 500 / sections max 20 / paper_title 300.
        question: (question || "").slice(0, 500),
        sections: cappedSections.map(s => ({ section: s.section, title: s.title, text: s.text })),
        selected_indices: sections,
        paper_title: (_readerState.title || "").slice(0, 300),
        model: _pdfTransModel || "",
      }, {
        onChunk: (chunk, full) => {
          streamTarget.textContent = full;
          streamTarget.parentElement.scrollTop = streamTarget.parentElement.scrollHeight;
        },
        onSuccess: (full) => {
          // cross_qa_stream doesn't return sections_used (it's SSE-only);
          // re-derive the meta line locally
          const usedList = (sections || cappedSections.map((_, i) => i))
            .map(i => `<span class="cross-qa-tag">${escapeHtml(cappedSections[i].section)}</span>`).join("");
          const sectionCount = sections ? sections.length : cappedSections.length;
          metaTarget.innerHTML = `综合 ${sectionCount} 个章节${usedList ? ` · ${usedList}` : ""}`;
          streamTarget.innerHTML = formatMarkdownLite(full);
          renderMathIn(el);
        },
        onError: (err) => {
          streamTarget.innerHTML = `<div style="color:var(--accent-terracotta);">❌ ${escapeHtml(err.message)}</div>`;
        },
        onAbort: () => {
          streamTarget.innerHTML = `<div style="color:var(--text-muted);">✕ 已取消本次跨章节问答</div>`;
        }
      });
    }

    function presetCrossQA(q) {
      document.getElementById("cross-qa-question").value = q;
      crossSectionQA();
    }

    function renderCrossQASectionPicker() {
      const el = document.getElementById("cross-qa-sections");
      if (!el) return;
      const sections = _readerState.sections;
      if (!sections || sections.length < 2) {
        el.innerHTML = `<div style="color:var(--text-subtle); font-size:12px; padding:8px;">本论文章节数 < 2, 无法跨章节问答</div>`;
        return;
      }
      // ⚠ 保留用户的勾选状态（2026-10-27）。
      //
      //   这个选择器在每次 renderReader() 时都会重建 —— 而 renderReader()
      //   在**切换章节**时也会跑 → 用户取消勾选几个章节后随便点一下别的章节，
      //   勾选就回到默认值了，之前的选择白做。
      //
      //   做法：重建前先把当前勾选的 data-idx 读出来，重建后套回去。
      //   首次渲染（页面里还没有这些 checkbox）时用默认规则。
      const oldBoxes = el.querySelectorAll("input.cross-qa-section-cb");
      const prevChecked = new Set();
      oldBoxes.forEach(cb => { if (cb.checked) prevChecked.add(cb.dataset.idx); });
      const hasPrev = oldBoxes.length > 0;

      el.innerHTML = sections.map((s, i) => {
        const checked = hasPrev
          ? prevChecked.has(String(i))
          : (s.section !== "references" && s.section !== "full_text");
        return `<label style="display:flex; align-items:center; gap:6px; padding:4px 0; font-size:12px; color:var(--text-soft); cursor:pointer;">
          <input type="checkbox" class="cross-qa-section-cb" data-idx="${i}" ${checked ? "checked" : ""} style="accent-color: var(--accent-olive);">
          <span>${escapeHtml(s.section)}</span>
          <span style="color:var(--text-subtle); margin-left:auto; font-size:10px;">${(s.text || "").length}字</span>
        </label>`;
      }).join("");
    }

    // ---------------------------------------------------------------------------
    // AI Q&A Copilot / 划词答疑

export {
  _activeAsk, _qaRequestSeq, cancelActiveAsk, _qaAskOriginalHTML, _qaResetAskButton,
  _aiFormatMs, _aiTaskStart, _aiTaskStage, _aiTaskEnd, _aiTaskCancel, toggleAiPanel, askLLMStream,
  _buildExplainPayload, askPaperQuestion, presetQuestion, crossSectionQA, presetCrossQA, renderCrossQASectionPicker,
};
