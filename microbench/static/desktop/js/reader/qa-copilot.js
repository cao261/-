import { showToast, postJson, escapeHtml, escapeJsString, escapeJsAttr, formatMarkdownLite, renderMathIn } from "../core/utils.js";
import { _readerState, _llmStatus, _qaModel, _qaModelChoicesLoaded, _loadQAModelPref, loadQAModelChoices, markQaModelChoicesLoaded, fetchModelStatus } from "../state/store.js";
import { askLLMStream, _aiFormatMs } from "./ai-activity.js";
import { _pdfPaintCanvas } from "./canvas-manager.js";
import { _pdfState, _pdfTransModel, _readerMode, getPdfState, ensurePageBlocks } from "./pdf-reader.js";
import { _collectSelectionRects, _currentSelectionQuote } from "./annotations.js";
import { showKeywordPickerModal, _persistLocalState, showVaultSaveModal, loadStats } from "../views/dashboard.js";

    // ---------------------------------------------------------------------------
    let _readerSelState = {
      text: "",
      source: "general", // "pdf" | "translation" | "original" | "general"
      pageNum: null,
      sectionIdx: null,
    };
    let _lastAnswerText = "";
    let _lastQAItem = null;
    let _sessionQAList = [];
    let _currentQAPreset = "通俗解释";

    // 判断选区属于哪一侧（PDF 原文 / 译文 / 章节视图）以及第几页。
    //
    // 关键：**不能只看鼠标松开的位置**。
    // 拖选一整行、拖到行尾、拖进右边距时，mouseup 的 e.target 常常是
    // .page-text 或面板留白 —— 不在任何块里 → 旧逻辑判成
    // source="general"、pageNum=null → 批注落到"章节视图"（第 0 页），
    // 用户在自己正在看的那一页上根本找不到这条批注。
    // 所以：先用 e.target，判不出来就用选区 range 的公共祖先兜底。
    //
    // 返回 { source, pageNum, sectionIdx }；判不出来返回 null。
    function _resolveSelectionOrigin(targetEl, range, activeSection) {
      const pick = (el) => {
        if (!el || !el.closest) return null;
        const pdfBlock = el.closest(".pdf-page-block");
        if (pdfBlock) {
          const m = pdfBlock.id.match(/\d+/);
          return {
            source: "pdf",
            pageNum: m ? parseInt(m[0], 10) : null,
            sectionIdx: null,
          };
        }
        if (el.closest("#pdf-translations-list") || el.closest(".pdf-translation-block")) {
          const transBlock = el.closest(".pdf-translation-block");
          let pageNum = null;
          if (transBlock) {
            const m = transBlock.id.match(/\d+/);
            if (m) pageNum = parseInt(m[0], 10);
          }
          return { source: "translation", pageNum, sectionIdx: null };
        }
        if (el.closest("#reader-original")) {
          return { source: "original", pageNum: null, sectionIdx: activeSection };
        }
        if (el.closest("#reader-translation")) {
          return { source: "translation", pageNum: null, sectionIdx: activeSection };
        }
        return null;
      };

      let info = pick(targetEl);
      if (!info && range) {
        const node = range.commonAncestorContainer;
        const el = node && (node.nodeType === 1 ? node : node.parentElement);
        info = pick(el);
      }
      return info;
    }

    function initReaderSelectionListener() {
      document.addEventListener("mouseup", (e) => {
        // If selection happened inside textarea or input, ignore
        if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;

        // Don't show bubble if clicking inside the QA panel controls
        const qaPanel = document.getElementById("reader-qa-panel");
        if (qaPanel && qaPanel.contains(e.target) && !e.target.closest("#qa-answer-container")) {
          return;
        }

        // 气泡 / 批注编辑器上的点击**不能**重新采集划选状态：
        // 点「✍ 标注笔记」时 e.target 落在气泡上（不在译文块里），
        // 若继续往下走会把 _readerSelState 重置成 source="general"、pageNum=null，
        // 保存时发出 page=0 → 后端 422，批注静默丢失。
        const bubbleEl = document.getElementById("reader-selection-bubble");
        if (bubbleEl && bubbleEl.contains(e.target)) return;
        const annoEditorEl = document.getElementById("anno-editor");
        if (annoEditorEl && annoEditorEl.contains(e.target)) return;

        const sel = window.getSelection();
        // 用 _currentSelectionQuote() 而不是 sel.toString()：
        // 后者会把看不见的 KaTeX MathML 副本（重复字母 + 换行）一起带出来，
        // 引文存下来就对不上，重开时高亮不回来。
        const text = _currentSelectionQuote();
        const bubble = document.getElementById("reader-selection-bubble");
        const floatingTag = document.getElementById("reader-floating-sel-tag");

        // Verify selection is within reader modal
        const readerModal = document.getElementById("reader-modal");
        if (!readerModal || getComputedStyle(readerModal).display === "none") {
          if (bubble) bubble.style.display = "none";
          return;
        }

        if (text && text.length >= 2) {
          let source = "general";
          let pageNum = null;
          let sectionIdx = null;

          // 选区落在哪：优先按**鼠标松开的位置**判断，位置在文字外面时
          // 回退到**选区本身**（range 的公共祖先）。
          // 为什么必须回退：拖选一整行时很容易把鼠标松在行尾 / 右边距上，
          // 那时 e.target 不是文字元素（可能是 .page-text 或面板留白），
          // 判定链全部落空 → source="general"、pageNum=null →
          // 批注被存成"章节视图"（第 0 页），用户在 PDF 页上根本找不到它。
          const range = sel.rangeCount ? sel.getRangeAt(0) : null;
          const origin = _resolveSelectionOrigin(
            e.target, range, _readerState ? _readerState.activeSection : null);
          if (origin) {
            source = origin.source;
            pageNum = origin.pageNum;
            sectionIdx = origin.sectionIdx;
          }

          _readerSelState = { text, source, pageNum, sectionIdx, rects: [] };
          // 在 PDF 原文侧划选时，把高亮矩形（归一化）一起存下来
          if (source === "pdf") {
            try { _readerSelState.rects = _collectSelectionRects(); } catch (e) {}
          }

          if (floatingTag) {
            floatingTag.style.display = "inline-block";
            floatingTag.textContent = `${text.length}字`;
          }

          // Show floating bubble
          if (bubble && sel.rangeCount > 0) {
            try {
              const range = sel.getRangeAt(0);
              const rect = range.getBoundingClientRect();
              if (rect.width > 0 || rect.height > 0) {
                const x = Math.min(window.innerWidth - 180, Math.max(12, rect.left + (rect.width / 2) - 80));
                const y = Math.max(12, rect.top - 42);
                bubble.style.left = `${x}px`;
                bubble.style.top = `${y}px`;
                bubble.style.display = "flex";
                const countEl = document.getElementById("reader-sel-bubble-count");
                if (countEl) countEl.textContent = `(${text.length > 12 ? text.slice(0, 10) + '…' : text})`;
              }
            } catch (err) {}
          }

          // If QA panel is already visible, update the quote preview in real-time
          if (qaPanel && qaPanel.style.display === "flex") {
            updateQAQuotedSnippet();
          }
        } else {
          // Fade out bubble if selection cleared
          setTimeout(() => {
            const currentSel = window.getSelection() ? window.getSelection().toString().trim() : "";
            if (!currentSel && bubble) bubble.style.display = "none";
          }, 150);
        }
      });
    }

    function toggleReaderQAPanel() {
      const panel = document.getElementById("reader-qa-panel");
      if (!panel) return;
      if (panel.style.display === "flex") {
        panel.style.display = "none";
      } else {
        openReaderQAPanel();
      }
    }

    function minimizeReaderQAPanel() {
      const panel = document.getElementById("reader-qa-panel");
      if (!panel) return;
      const collapsed = panel.classList.toggle("qa-collapsed");
      if (collapsed) {
        // 收起时先记住当前位置/尺寸（展开时还原），并把面板移到当前所在
        // 那一侧的边缘 —— 拖到左边就贴左边，不要永远贴右边。
        const r = panel.getBoundingClientRect();
        panel.dataset.restoreLeft = String(Math.round(r.left));
        panel.dataset.restoreTop = String(Math.round(r.top));
        panel.dataset.restoreWidth = String(Math.round(r.width));
        panel.dataset.restoreHeight = String(Math.round(r.height));

        panel.style.left = _qaClampPanelX(panel, r.left) + "px";
        panel.style.top = _qaClampPanelY(panel, r.top) + "px";
        panel.style.right = "auto";
        panel.style.bottom = "auto";
        // 贴着窗口边缘：靠右就 right=0，靠左就 left=0
        if (r.left + r.width > window.innerWidth - 60) {
          panel.style.left = "auto";
          panel.style.right = "0px";
        }
      } else {
        // 展开：还原之前的位置与尺寸
        const lw = parseInt(panel.dataset.restoreWidth || "", 10);
        const lh = parseInt(panel.dataset.restoreHeight || "", 10);
        if (lw) panel.style.width = lw + "px";
        if (lh) panel.style.height = lh + "px";
        const ll = parseInt(panel.dataset.restoreLeft || "", 10);
        const lt = parseInt(panel.dataset.restoreTop || "", 10);
        // ⚠ 还原位置也要钳到屏幕内 —— 收起期间窗口可能变小/变了分辨率
        //   （旧代码直接还原 → 面板可能整个在屏幕外，X 键够不着）。
        panel.style.left = (isNaN(ll) ? 0 : _qaClampPanelX(panel, ll)) + "px";
        panel.style.top = (isNaN(lt) ? 0 : _qaClampPanelY(panel, lt)) + "px";
        panel.style.right = "auto";
        panel.style.bottom = "auto";
      }
    }

    // 收起状态下点标题栏也能展开（竖条上不方便够到小按钮）
    function _qaCollapsedHeadClick(e) {
      const panel = document.getElementById("reader-qa-panel");
      if (!panel || !panel.classList.contains("qa-collapsed")) return;
      if (e.target.closest("button")) return;
      // ⚠ 刚拖动完的那一下 click 必须忽略（2026-11-19）——
      //   收起的竖条被拖动会顺带触发标题栏的 click → 面板被**误展开**。
      //   真机实测：collapsed=True 拖一下 → collapsed=False ❌
      if (_qaGeo.suppressClick) { _qaGeo.suppressClick = false; return; }
      minimizeReaderQAPanel();
    }

    function openReaderQAPanel(presetQ = null) {
      const panel = document.getElementById("reader-qa-panel");
      if (!panel) return;
      panel.style.display = "flex";
      panel.classList.remove("minimized");
      panel.classList.remove("qa-collapsed");

      // Hide bubble if showing
      const bubble = document.getElementById("reader-selection-bubble");
      if (bubble) bubble.style.display = "none";

      updateQAQuotedSnippet();

      // 加载答疑模型选项（与翻译的下拉框独立）。
      // 只在第一次打开时拉；之后用记住的选择。
      if (!_qaModelChoicesLoaded) {
        markQaModelChoicesLoaded();
        _loadQAModelPref();
        loadQAModelChoices();
      }

      const modelPill = document.getElementById("qa-panel-model-pill");
      if (modelPill && _llmStatus) {
        // 徽标要显示**答疑实际用的模型**（用户自己选的那个），
        // 不能写死"主模型" —— 否则用户切了模型却看不到变化。
        const sel = document.getElementById("qa-model-select");
        const opt = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
        if (opt && sel.value) {
          modelPill.textContent = opt.textContent;
          modelPill.style.color = "";
        } else {
          modelPill.textContent = _llmStatus.is_degraded ? "🛡️ 降格保护" : "🤖 主模型";
          modelPill.style.color = _llmStatus.is_degraded ? "var(--accent-terracotta)" : "var(--accent-olive)";
        }
      }

      if (presetQ) {
        document.getElementById("qa-question-input").value = presetQ;
        submitReaderQA();
      } else {
        document.getElementById("qa-question-input").focus();
      }
      initReaderQAPanelDrag();
    }

    // ===========================================================================

    // ===========================================================================
    // 划词答疑面板：拖动 / 缩放 / 位置记忆
    // ===========================================================================
    // 用户在 27 寸屏上嫌它"只在角落里一小块、只能看一部分"。现在：
    //   - 拖标题栏移动（位置写进 localStorage，下次打开还在原位）
    //   - 右下角原生手柄缩放（尺寸同样记住）
    const _qaGeo = { drag: null, suppressClick: false };

    function _qaPanelStorageKey() { return "microbench.qaPanel.geometry"; }

    // ⚠ 面板必须**整个**留在视口里（2026-11-19 用户报「X 键在某些比例下会消失」）。
    //   旧实现按固定像素钳制（拖动用 60px、还原用 80px），可面板本身有 460px 宽
    //   → 面板大半个在屏幕外 → 右上角的「×」够不着。
    //   更糟的是这个越界位置**会被存进 localStorage**（saveReaderQAPanelGeometry
    //   直接存 r.left）→ 下次打开面板还在屏幕外，**X 键永久消失**。
    //   真机实测：拖到最右 → 面板左缘 1540 / 右缘 2000（屏宽 1600）→ X 键在 1983 ❌
    function _qaClampPanelX(panel, x) {
      const vw = window.innerWidth;
      // ⚠ 用 offsetWidth（布局尺寸）而不是 getBoundingClientRect().width ——
      //   后者会被入场动画的 scale(0.98) 缩小 → 钳制口径偏小 → 面板右边超出屏幕。
      const w = panel.offsetWidth || panel.getBoundingClientRect().width || 460;
      const maxX = Math.max(0, vw - w);
      return Math.max(0, Math.min(Math.round(x), maxX));
    }
    function _qaClampPanelY(panel, y) {
      const vh = window.innerHeight;
      const h = panel.offsetHeight || panel.getBoundingClientRect().height || 600;
      // 面板比视口还高时（可缩到 220 起步、也可拉得很高）→ 贴顶，不做上移。
      const maxY = Math.max(0, vh - Math.min(h, vh));
      return Math.max(0, Math.min(Math.round(y), maxY));
    }

    function saveReaderQAPanelGeometry() {
      const panel = document.getElementById("reader-qa-panel");
      if (!panel) return;
      try {
        const r = panel.getBoundingClientRect();
        // ⚠ 落盘前先钳到屏幕内 —— 否则一次「拖歪了」会把越界位置永久存下来，
        //   之后每次打开面板都缺一块（X 键永远够不着）。
        const x = _qaClampPanelX(panel, r.left);
        const y = _qaClampPanelY(panel, r.top);
        localStorage.setItem(_qaPanelStorageKey(), JSON.stringify({
          left: x, top: y,
          width: Math.round(r.width), height: Math.round(r.height),
        }));
        // 顺手把面板拉回屏幕内（拖歪了就地纠正）
        if (x !== Math.round(r.left) || y !== Math.round(r.top)) {
          panel.style.left = x + "px";
          panel.style.top = y + "px";
        }
      } catch (e) { /* localStorage 不可用（隐私模式）时静默降级 */ }
    }

    function restoreReaderQAPanelGeometry() {
      const panel = document.getElementById("reader-qa-panel");
      if (!panel) return;
      let g = null;
      try { g = JSON.parse(localStorage.getItem(_qaPanelStorageKey()) || "null"); } catch (e) { g = null; }
      if (!g) return;
      if (g.width)  panel.style.width  = Math.max(340, Math.min(g.width,  window.innerWidth - 24)) + "px";
      if (g.height) panel.style.height = Math.max(220, Math.min(g.height, window.innerHeight - 24)) + "px";
      if (typeof g.left === "number" && typeof g.top === "number") {
        // ⚠ 钳制要按**面板实际尺寸**算（宽高要先设好再量）——
        //   旧实现按固定 80px 钳，面板 460px 宽 → 还原后大半个在屏幕外。
        const x = _qaClampPanelX(panel, g.left);
        const y = _qaClampPanelY(panel, g.top);
        panel.style.right = "auto"; panel.style.bottom = "auto";
        panel.style.left = x + "px"; panel.style.top = y + "px";
      }
    }

    function initReaderQAPanelDrag() {
      const panel = document.getElementById("reader-qa-panel");
      if (!panel) return;
      const head = panel.querySelector(".qa-panel-head");
      if (!head || head.dataset.dragBound === "1") return;
      head.dataset.dragBound = "1";

      head.addEventListener("mousedown", (e) => {
        if (e.button !== 0) return;
        if (e.target.closest("button, input, textarea, select")) return;
        const r = panel.getBoundingClientRect();
        _qaGeo.drag = { sx: e.clientX, sy: e.clientY, ox: r.left, oy: r.top, moved: false };
        // 从 right/bottom 锚定切成 left/top 锚定：之后位置完全由我们说了算
        panel.style.right = "auto";
        panel.style.bottom = "auto";
        panel.style.left = r.left + "px";
        panel.style.top = r.top + "px";
        panel.classList.add("dragging");
        e.preventDefault();
      });

      document.addEventListener("mousemove", (e) => {
        const d = _qaGeo.drag;
        if (!d) return;
        const dx = e.clientX - d.sx, dy = e.clientY - d.sy;
        // 超过 3px 才算「拖动」——否则手抖一下也会被当成拖动
        if (!d.moved && (Math.abs(dx) > 3 || Math.abs(dy) > 3)) d.moved = true;
        const x = _qaClampPanelX(panel, d.ox + dx);
        const y = _qaClampPanelY(panel, d.oy + dy);
        panel.style.left = x + "px";
        panel.style.top = y + "px";
      });

      document.addEventListener("mouseup", () => {
        if (!_qaGeo.drag) return;
        const moved = _qaGeo.drag.moved;
        _qaGeo.drag = null;
        panel.classList.remove("dragging");
        // ⚠ 拖过的那一下 click 要忽略 —— 收起的竖条被拖动会顺带触发
        //   标题栏的 click → 面板被**误展开**（用户报「拖动有时候会出现问题」）。
        //   真机实测：collapsed=True 拖一下 → collapsed=False ❌
        _qaGeo.suppressClick = !!moved;
        saveReaderQAPanelGeometry();
      });
      // CSS resize 拖完也要落盘（鼠标在面板内松开）
      panel.addEventListener("mouseup", saveReaderQAPanelGeometry);
      restoreReaderQAPanelGeometry();
    }


    function updateQAQuotedSnippet() {
      const textEl = document.getElementById("qa-quoted-text");
      const srcTag = document.getElementById("qa-quoted-source-tag");
      const lenTag = document.getElementById("qa-quoted-len-tag");
      const clearBtn = document.getElementById("qa-clear-quote-btn");

      if (!_readerSelState.text) {
        if (textEl) {
          textEl.innerHTML = '<span style="color:var(--text-subtle);">(在左侧 PDF 或右侧译文中划选文字，将自动同步到此；也可直接提问)</span>';
          textEl.style.fontStyle = "normal";
        }
        if (srcTag) {
          srcTag.textContent = "无划选";
          srcTag.style.background = "var(--bg-canvas-subtle)";
          srcTag.style.color = "var(--text-muted)";
        }
        if (lenTag) lenTag.textContent = "";
        if (clearBtn) clearBtn.style.display = "none";
        return;
      }

      if (textEl) {
        textEl.textContent = _readerSelState.text;
        textEl.style.fontStyle = "italic";
      }
      if (srcTag) {
        const srcLabel = _readerSelState.source === "pdf" ? "📄 来自 PDF" :
                         _readerSelState.source === "translation" ? "📝 来自译文" :
                         _readerSelState.source === "original" ? "📑 来自原文" : "📌 选中文本";
        srcTag.textContent = srcLabel;
        srcTag.style.background = "var(--accent-terracotta-subtle)";
        srcTag.style.color = "var(--accent-terracotta)";
      }
      if (lenTag) lenTag.textContent = `· ${_readerSelState.text.length} 字`;
      if (clearBtn) clearBtn.style.display = "inline-block";
    }

    function clearQAQuotedText() {
      _readerSelState.text = "";
      _readerSelState.source = "general";
      _readerSelState.pageNum = null;
      _readerSelState.sectionIdx = null;
      const floatingTag = document.getElementById("reader-floating-sel-tag");
      if (floatingTag) floatingTag.style.display = "none";
      updateQAQuotedSnippet();
    }

    // ---------------------------------------------------------------
    // Question-scope router (two-stage):
    //
    //   Stage 1 (this function): a small LLM call reads the selection
    //   + question and decides how much context to attach. Returns
    //   {mode, budget, reason, source}. On any failure we silently fall
    //   back to the keyword heuristic so the main QA flow is never
    //   blocked. Caller (submitReaderQA) passes the result to stage 2.
    //
    //   Stage 2 (the existing /explain_selection_stream endpoint) gets
    //   the curated surroundingContext built from the routed scope.
    //
    // Budget we pay for this: ~150-250 input tokens + 80-200 output
    // tokens at the router, max ~12s latency. Saves the much larger
    // cost of always-attaching-a-whole-chapter for trivial lookups,
    // AND catches open-ended questions like "这种方法在我自己的工艺
    // 节点上能用吗" that no keyword heuristic could route correctly.
    //
    // preset is passed as a hint (not an override) so the LLM can
    // still up-route when the question demands more context than the
    // preset suggests. The user can still force the mode by hitting
    // the preset button again — and the toast lets them see why we
    // chose what we chose.
    // ---------------------------------------------------------------
    // -----------------------------------------------------------------------
    // 「选中一个标题 → 总结这一小节」（2026-11-18）
    //
    // 用户原话：「我一般的使用场景是选中一个标题，让 ai 先帮我总结一下
    // 这一小节的内容」。
    //
    // 选中的是**标题** —— 标题本身没有信息量（就一行 "II. Experimental
    // Procedure"），要回答「总结这一节」，上下文必须换成**这一节的完整正文**。
    //
    // 三个关键决定：
    //   ① 大纲由**服务端**算（/api/paper/outline）—— 前端拿到的是 PDF.js 的
    //      扁平化文本（`items.map(it => it.str).join(" ")`，没有行结构、
    //      左右栏交错），根本认不出「哪一行是标题」；
    //   ② 命中标题就**跳过 LLM 路由**（省一次 3~8s 往返，而且更确定：
    //      「总结这一节」不需要模型去猜该给多少上下文）；
    //   ③ 上下文给「本节中文译文 + 本节英文原文 + 本节公式高清图」——
    //      中文是用户真正在读的东西，英文是权威表述，公式必须给原图。
    // -----------------------------------------------------------------------

    const _QA_SECTION_BUDGET = 16000;   // 必须与后端 scope_caps["section"] 一致

    // 大纲缓存（同一篇论文只拉一次；服务端也按 mtime 缓存）
    let _qaOutline = { path: null, sections: [], ready: false, loading: false, promise: null };
    // 当前命中的标题（构建上下文时用；_refineQA 也要用）
    let _qaHeadingHit = null;
    // 划的是**正文**时，定位它落在哪个小节里（router 判 section 档时用）
    let _qaSectionBySelection = null;

    async function fetchQaOutline(relativePath) {
      if (!relativePath) return null;
      if (_qaOutline.path === relativePath) {
        if (_qaOutline.ready) return _qaOutline;
        if (_qaOutline.promise) return _qaOutline.promise;
      }
      _qaOutline = { path: relativePath, sections: [], ready: false,
                     loading: true, promise: null };
      _qaOutline.promise = (async () => {
        try {
          const res = await fetch("/api/paper/outline?relative_path=" +
                                  encodeURIComponent(relativePath));
          if (!res.ok) throw new Error("HTTP " + res.status);
          const data = await res.json();
          if (_qaOutline.path !== relativePath) return null;  // 期间换论文了
          _qaOutline.sections = Array.isArray(data.sections) ? data.sections : [];
          _qaOutline.ready = true;
          return _qaOutline;
        } catch (e) {
          console.warn("[qa-outline] 大纲拉取失败:", e && e.message);
          _qaOutline.ready = false;
          return null;
        } finally {
          if (_qaOutline.path === relativePath) {
            _qaOutline.loading = false;
            _qaOutline.promise = null;
          }
        }
      })();
      return _qaOutline.promise;
    }

    // 大纲还没算完就等一下（最多 6 秒）——
    // 用户刚打开论文就划词提问时，服务端首次算大纲要 ~2s（8 页），
    // 等它算完才能认得出「用户选的是标题」。
    async function _ensureQaOutlineReady() {
      const p = _qaOutline && _qaOutline.promise;
      if (!p) return;
      try {
        await Promise.race([p, new Promise(r => setTimeout(r, 6000))]);
      } catch (e) { /* 大纲失败就当没有，走普通上下文 */ }
    }

    // 标题归一化：去掉所有空白与标点，只留下字母/数字/汉字。
    // 这样 "II. Experimental Procedure" / "II Experimental Procedure"
    // / "ii. experimental procedure" 归一化后完全一致。
    function _normQaHeading(s) {
      return String(s == null ? "" : s)
        .replace(/[\s\u3000]+/g, "")
        .replace(/[.,;:!?'"“”‘’()\[\]{}<>《》、，。；：·…—\-]/g, "")
        .toLowerCase();
    }

    // 当前这篇论文的**小节大纲**（服务端算的）。
    //
    // ⚠ 必须校验「这份大纲属于当前这篇论文」（§4.110）：
    //   大纲是按论文路径缓存的，而 `_qaOutline` 是**模块级单例**——
    //   换一篇论文、但还没触发新的 loadPdfDualColumn() 时，
    //   `_qaOutline.sections` 里还是**上一篇**的小节
    //   → 拿它当候选 = 跨论文串台（AI 总结的是上一节）。
    function _qaOutlineSections() {
      const secs = (_qaOutline && _qaOutline.sections) || [];
      if (!secs.length) return [];
      const cur = (_pdfState && _pdfState.relativePath)
               || (_readerState && _readerState.pdfPath) || "";
      if (!_qaOutline.path || !cur || _qaOutline.path !== cur) return [];
      return secs;
    }

    // 候选标题：**小节大纲优先**，章节视图的语义章节兜底。
    //
    // ⚠ 判据是「**这份大纲属于当前这篇论文**」，不是「sections 非空」
    //   （§4.110）：PDF 模式下 `_readerState.sections` 依然留着章节视图那份
    //   （`openVaultPdf()` 填的），而对综述类论文它常常**只有「Full Text」一桶**
    //   → 按「非空」判断会把候选锁死成 ["Full Text"]
    //   → 用户在 PDF 原文里划「II. Experimental Procedure」永远命中不了
    //   （2026-11-18 Playwright 真浏览器实测抓到的就是这个）。
    //
    //   为什么大纲优先：章节视图的标题是**语义桶**（Abstract/Introduction/Methods，
    //   甚至整篇只有一桶），而大纲的标题是 PDF 里**真实的标题**
    //   （「II. Experimental Procedure」）—— 后者才是用户说的「这一小节」。
    //   `_matchQaHeading()` 取**归一化后最长**的命中 → 大纲里的真实标题优先。
    function _qaHeadingCandidates() {
      const out = [];
      for (const s of _qaOutlineSections()) {
        out.push({
          title: s.title || "",
          level: s.level || 1,
          page_start: s.page_start, page_end: s.page_end,
          text: s.text || "",
          formulas: Array.isArray(s.formulas) ? s.formulas : [],
          source: "pdf",
        });
      }
      const secs = (_readerState && _readerState.sections) || [];
      secs.forEach((s, i) => {
        out.push({
          title: s.title || "",
          level: 1,
          page_start: s.page_start, page_end: s.page_end,
          text: s.text || "",
          formulas: [],
          source: "section",
          index: i,
        });
      });
      return out;
    }

    // 用户选中的这一段，是不是某个小节的标题？
    //
    // ⚠ 误判的代价不对称（§4.6 同款思路）：把正文当成标题 → 上下文锚到
    //   **别的节** → AI 总结的是别人那一节，用户一眼就看出不对；
    //   漏判的代价只是「这次退回普通上下文」，还是能答。
    // → 所以判定收紧：标题本身要够长（≥6 个归一化字符），
    //   选区不能比标题长太多（≤1.6 倍），只选一部分也要够长（≥一半）。
    function _matchQaHeading(selectionText) {
      const sel = _normQaHeading(selectionText);
      if (!sel || sel.length < 3 || sel.length > 100) return null;
      let best = null;
      let bestLen = 0;
      for (const sec of _qaHeadingCandidates()) {
        const t = _normQaHeading(sec.title);
        if (!t) continue;
        let hit = false;
        if (sel === t) {
          hit = true;
        } else if (t.length >= 6 && sel.length >= 4 &&
                   t.includes(sel) && sel.length / t.length >= 0.5) {
          hit = true;   // 只选了标题的一部分
        } else if (t.length >= 6 && sel.includes(t) &&
                   sel.length <= t.length * 1.6) {
          hit = true;   // 选了标题（可能多带了标点/空白）
        }
        if (hit && t.length > bestLen) { best = sec; bestLen = t.length; }
      }
      return best;
    }

    // 小节 → 统一的小节对象（前端内部用）
    function _qaSectionPayload(sec) {
      if (!sec) return null;
      return {
        title: sec.title || "",
        level: sec.level || 1,
        page_start: sec.page_start, page_end: sec.page_end,
        text: sec.text || "",
        formulas: Array.isArray(sec.formulas) ? sec.formulas : [],
        source: "pdf",
      };
    }

    // 这个小节覆盖第几页？（页码 1-based，和 PDF 页号、大纲 page_start 同一套）
    function _qaSectionCoversPage(sec, pageNum) {
      const ps = sec && sec.page_start;
      const pe = sec && sec.page_end;
      if (typeof ps !== "number" || !pageNum) return false;
      const end = (typeof pe === "number") ? pe : ps;
      return ps <= pageNum && pageNum <= end;
    }

    // 选区落在页面上的**物理位置**（pt）——
    // 靠划选时记下的归一化矩形 + 页面物理尺寸换算（§4.97：文本层要设 scale-factor，
    // 这里用的是同一套渲染坐标，所以归一化矩形和页面尺寸是一对）。
    function _qaSelectionPointPt(pageNum, selRects) {
      if (!pageNum || !Array.isArray(selRects) || !selRects.length) return null;
      const pages = (_pdfState && _pdfState.pages) || [];
      const pg = pages.find(p => p && p.num === pageNum);
      if (!pg || !pg.ptW || !pg.ptH) return null;
      const r = selRects[0];
      if (!r) return null;
      const x = Number(r.x) || 0, y = Number(r.y) || 0;
      const w = Number(r.w) || 0, h = Number(r.h) || 0;
      return { x: (x + w / 2) * pg.ptW, y: (y + h / 2) * pg.ptH };
    }

    // 找到包含这个点的**段落块**（服务端 page_blocks 的几何：bbox 是页面绝对 pt）
    function _qaBlockAtPoint(blocks, px, py) {
      let near = null, nearDist = Infinity;
      for (const b of (blocks || [])) {
        const bb = b && b.bbox;
        if (!Array.isArray(bb) || bb.length < 4) continue;
        const x0 = Number(bb[0]), y0 = Number(bb[1]);
        const x1 = Number(bb[2]), y1 = Number(bb[3]);
        if (!isFinite(x0) || !isFinite(y0) || !isFinite(x1) || !isFinite(y1)) continue;
        if (px >= x0 && px <= x1 && py >= y0 && py <= y1) return b;   // 点在块内
        const dx = Math.max(x0 - px, 0, px - x1);
        const dy = Math.max(y0 - py, 0, py - y1);
        const d = dx * dx + dy * dy;
        if (d < nearDist) { nearDist = d; near = b; }
      }
      // 点不在任何块里 → 只有「贴着某块」（12pt 内）才认，
      // 否则宁可认不出（§4.6：给错内容比不给更糟）
      if (near && nearDist <= 144) return near;
      return null;
    }

    // 同一页里多个小节都含这句 → 用**版面几何**判：
    //   选区落在哪个段落块 → 那个块属于哪个小节 → 就是答案。
    async function _qaSectionByGeometry(pool, pageNum, selRects) {
      if (!Array.isArray(pool) || pool.length < 2) return null;
      const pt = _qaSelectionPointPt(pageNum, selRects);
      if (!pt) return null;
      let blocks = null;
      try { blocks = await ensurePageBlocks(pageNum); } catch (e) { blocks = null; }
      if (!blocks || !blocks.length) return null;
      const blk = _qaBlockAtPoint(blocks, pt.x, pt.y);
      if (!blk) return null;
      // 块文本的前 30 个归一化字符 → 去候选小节正文里找
      const key = _normQaHeading(blk.text || "").slice(0, 30);
      if (!key) return null;
      const hit = pool.filter(sec =>
        _normQaHeading(sec.text || "").includes(key));
      if (hit.length !== 1) return null;   // 指认不唯一 → 不猜
      return hit[0];
    }

    // 划的**正文**落在哪个小节里？
    //
    // ⚠ 为什么需要（2026-11-19）：router 会把「这一节讲了什么」判成 section 档，
    //   但用户划的往往是**正文**而不是标题 → `_matchQaHeading()` 命不中
    //   → 旧写法 section 档就返回**空上下文**。
    //   → 这里用「选区文本是不是这个小节正文的一部分」来反查所属小节。
    //
    // ⚠ 判据为什么是**页码**而不是「谁最长」（2026-11-19 真机实测抓到的错）：
    //   「magnetostrictive one」这句同时出现在
    //     I. Introduction(p1, 3758 字) / II. Experimental Procedure(p1-2, 1015 字)
    //     / (3) Resonance Enhancement(p7, 2492 字) 三个小节里
    //   → 旧写法「取正文最长的」= 永远选 I. Introduction
    //   → 但用户是在**第 2 页**划的这句 → 正确答案应该是 II. Experimental Procedure
    //   → AI 总结的是别人那一节，用户一眼就看出不对（§4.6 同款：给错内容比不给更糟）。
    //
    // 现在是四层：
    //   ① 选区文本命中哪些小节（正文包含）→ 候选集；
    //   ② **页码过滤**：候选里覆盖「划词那一页」的才可能是答案；
    //   ③ 还剩多个 → **版面几何**：选区落在哪个段落块，那个块属于哪个小节；
    //   ④ 还是定不了 → **返回 null**（落回 mid 档给当前页），不猜。
    async function _findQaSectionBySelection(selectionText, pageNum, selRects) {
      const sel = _normQaHeading(selectionText);
      if (!sel || sel.length < 8) return null;
      const cands = _qaOutlineSections().filter(sec => {
        const body = _normQaHeading(sec.text || "");
        return !!body && body.includes(sel);
      });
      if (!cands.length) return null;
      let pool = cands;
      if (typeof pageNum === "number" && pageNum > 0) {
        const onPage = cands.filter(sec => _qaSectionCoversPage(sec, pageNum));
        if (onPage.length) pool = onPage;
      }
      if (pool.length === 1) return _qaSectionPayload(pool[0]);
      if (pool.length > 1) {
        const geo = await _qaSectionByGeometry(pool, pageNum, selRects);
        if (geo) return _qaSectionPayload(geo);
        return null;      // 定不了 → 不给错的小节
      }
      return null;
    }

    // 超预算时**头尾都保留**（不是只留头部）——
    // 小节的结尾常常是结论/收束，只留头部等于把结论丢了。
    //
    // ⚠ 2026-11-19：省略标记**必须算进预算**。
    //   旧写法标记是「额外」加的 → 产物比 budget 长几十个字
    //   → 服务端再按 scope 上限裁一刀时，**恰好砍掉我们特意保留的尾部**
    //     （小节的结论就在尾部）—— 前端白保留。
    function _clipQaContext(text, budget) {
      const s = String(text == null ? "" : text);
      if (!budget || s.length <= budget) return s;
      const MARK_RESERVE = 40;   // 省略标记大约 30 字，留够余量
      const room = Math.max(0, budget - MARK_RESERVE);
      const head = Math.floor(room * 0.65);
      const tail = Math.max(0, room - head);
      const cut = s.length - head - tail;
      return s.slice(0, head)
        + "\n…（中间约 " + cut.toLocaleString() + " 字已省略）…\n"
        + (tail > 0 ? s.slice(s.length - tail) : "");
    }

    // 译文里的 ⟦FORMULA:…⟧ / ⟦TABLE:…⟧ 标记对模型没有意义（坐标它也用不上），
    // 换成中文占位，别让一堆坐标噪声占掉上下文预算。
    function _stripQaMarkers(text) {
      return String(text == null ? "" : text)
        .replace(/⟦FORMULA:[^⟧\n]*⟧/g, "【公式】")
        .replace(/⟦TABLE:[^⟧\n]*⟧/g, "【表格】");
    }

    // 本小节的中文译文（章节视图 = 本节译文；PDF 模式 = 本节所在页的整页译文）
    function _sectionChineseText(hit) {
      if (!hit) return "";
      if (hit.source === "section" && typeof hit.index === "number") {
        const t = (_readerState.translations || [])[hit.index];
        if (t) return _stripQaMarkers(t);
        // 这一节还没翻过 → 别急着返回空，继续往下找 PDF 页译文
      }
      const pages = (_pdfState && _pdfState.pages) || [];
      const chunks = [];
      const from = hit.page_start || 0;
      const to = hit.page_end || 0;
      for (let p = from; p <= to && p > 0; p++) {
        const pg = pages.find(x => x.num === p);
        if (pg && pg.translation) {
          // ⚠ 译文是**整页**翻的（译文按页存，不按小节存）→ 里面可能还有
          //   本小节之外的段落。**如实标注**，否则模型会把摘要/引言当成本节内容
          //   一起总结（2026-11-19）。
          chunks.push("[第 " + p + " 页中文译文（整页翻译，含本页其他段落）]\n"
            + _stripQaMarkers(pg.translation));
        }
      }
      if (chunks.length) return chunks.join("\n\n");
      // 章节视图里没有 PDF 页译文（`_pdfState.pages` 是空的）→
      // 退到**章节视图自己那一节**的译文：用户正在读的就是它，
      // 中文译文是用户真正在读的那份（§4.110）。
      const secs = (_readerState && _readerState.sections) || [];
      const trs = _readerState.translations || {};
      let idx = null;
      if (_readerSelState && _readerSelState.sectionIdx !== null &&
          _readerSelState.sectionIdx !== undefined) {
        idx = _readerSelState.sectionIdx;
      } else if (hit.page_start) {
        // 没有 sectionIdx → 找页码区间覆盖本小节的那一节
        for (let i = 0; i < secs.length; i++) {
          const s = secs[i] || {};
          const sFrom = s.page_start || 0;
          const sTo = s.page_end || sFrom;
          if (sFrom && hit.page_start >= sFrom && hit.page_start <= sTo) { idx = i; break; }
        }
      }
      if (idx !== null && idx !== undefined && trs[idx]) {
        return _stripQaMarkers(trs[idx]);
      }
      return "";
    }

    // -----------------------------------------------------------------------
    // 上下文 = **分段结构**（2026-11-19）
    //
    // 为什么不再拼成一坨字符串：
    //   ① 「可视化」要能说清「这次送了什么」—— 只有分段才说得清；
    //   ② 用户划的那句必须在上下文里、而且**要在最前面**
    //      （实测：section 档里它落在 95.4% 处 → 模型第一眼看不到用户关心的句子）；
    //   ③ 各档的**来源**不同（真小节 / 整页译文 / PDF.js 扁平文本），
    //      必须分别标注，否则模型会把「整页译文」当成本节内容（§4.113）。
    //
    // 一段上下文：{ label, kind, text }
    //   kind: selection（用户划选） / section（小节英文原文） /
    //         translation（中文译文） / page（整页文本） / note（提示）
    // -----------------------------------------------------------------------
    function _qaCtxPart(label, kind, text) {
      return {
        label: label == null ? "" : String(label),
        kind: kind || "note",
        text: text == null ? "" : String(text),
      };
    }

    function _qaCtxJoin(parts) {
      return (parts || []).map(p => {
        if (!p) return "";
        if (!p.text) return p.label || "";
        return p.label ? (p.label + "\n" + p.text) : p.text;
      }).filter(Boolean).join("\n\n");
    }

    // 分段各自裁剪，**总和不超过 budget**（要给标签和分隔符留位置，
    // 否则外面那层全局裁剪会再砍一刀，把小节结尾的结论砍掉 —— §4.107）。
    function _qaCtxParts(parts, budget) {
      const list = (parts || []).filter(Boolean);
      const cap = Math.max(400, (budget || 8000) - 600);
      let total = 0;
      for (const p of list) total += (p.text ? p.text.length : 0);
      if (total <= cap) return list;
      const k = cap / total;
      return list.map(p => {
        if (!p || !p.text) return p;
        const keep = Math.max(0, Math.floor(p.text.length * k));
        if (keep >= p.text.length) return p;
        return Object.assign({}, p, { text: _clipQaContext(p.text, keep) });
      });
    }

    // 小节上下文（分段）：标题 + 中文译文（有就带）+ 英文原文
    function _buildSectionContextParts(hit, budget) {
      if (!hit) return [];
      const cap = budget || _QA_SECTION_BUDGET;
      const parts = [];
      const pageDesc = (hit.page_start && hit.page_end)
        ? "（第 " + hit.page_start + "-" + hit.page_end + " 页）" : "";
      parts.push(_qaCtxPart("【小节】" + (hit.title || "") + pageDesc, "section", ""));
      const zh = _sectionChineseText(hit);
      const en = hit.text || "";
      if (zh) {
        const zhBudget = Math.floor(cap * 0.4);
        parts.push(_qaCtxPart("【本小节中文译文（机器翻译，供快速理解）】", "translation",
                              _clipQaContext(zh, zhBudget)));
        parts.push(_qaCtxPart("【本小节英文原文（权威表述；公式位置为【公式N】占位，"
                              + "对应的公式高清原图见附件）】", "section",
                              _clipQaContext(en, Math.max(0, cap - zhBudget))));
      } else {
        parts.push(_qaCtxPart("【本小节英文原文（公式位置为【公式N】占位，"
                              + "对应的公式高清原图见附件）】", "section",
                              _clipQaContext(en, cap)));
      }
      return parts;
    }

    function _buildSectionContext(hit, budget) {
      return _qaCtxJoin(_buildSectionContextParts(hit, budget));
    }

    // 本小节涉及哪些页（上限 12 页，别把整篇都塞进去）
    function _sectionPageList(hit) {
      if (!hit || !hit.page_start) return [];
      const from = hit.page_start;
      const to = Math.max(from, hit.page_end || from);
      const out = [];
      for (let p = from; p <= to && out.length < 12; p++) out.push(p);
      return out;
    }

    // 本小节的公式坐标 → [页码, x0, y0, x1, y1]
    function _sectionFormulaRects(hit) {
      if (!hit || !Array.isArray(hit.formulas)) return [];
      const out = [];
      for (const f of hit.formulas) {
        if (!f || !Array.isArray(f.rect) || f.rect.length !== 4) continue;
        const page = Number(f.page);
        if (!page || page < 1) continue;
        out.push([page, f.rect[0], f.rect[1], f.rect[2], f.rect[3]]);
        if (out.length >= 16) break;
      }
      return out;
    }

    // -----------------------------------------------------------------------
    // Scope caps — MUST mirror the backend (app.py `scope_caps`).
    // The server truncates surrounding_context to these limits before it
    // reaches the LLM, so the client has to apply the same numbers: the
    // badge must report what is really sent, and the request field itself
    // (max_length=50000) must never be exceeded.
    //   term=2K · mid=8K · section=16K · wide=20K · full=50K
    // -----------------------------------------------------------------------
    const _SCOPE_CAPS = { term: 2000, mid: 8000, section: 16000, wide: 20000, full: 50000 };
    // ⚠ router 返回的档位白名单 —— 必须与后端 `QA_SCOPE_MODES` **逐字一致**
    //   （2026-11-19）。少一个 → router 返回的档位被判「非法」→ 抛错 →
    //   **静默**退回关键词启发式 → 用户看不出发生了什么。
    const _QA_ROUTER_MODES = ["term", "mid", "section", "wide", "full"];
    // Backend field limit for surrounding_context is 50000 — leave headroom.
    const _MAX_CONTEXT_CHARS = 48000;
    // Hard ceiling for any client-side scope budget. The router may answer
    // 80000; building that much context would 422 on the 50K field.
    const _MAX_SCOPE_BUDGET = 45000;

    async function _routeQuestionScope(question, preset, selectedText) {
      // Fast path: explicit "术语速查" preset → always term (user
      // explicitly asked for term-level). Skip the LLM round-trip.
      if (preset === "术语速查") {
        return {
          mode: "term", budget: 800, label: "紧邻上下文 (~800 字)",
          source: "preset", reason: "preset=术语速查(强制)",
        };
      }

      // Otherwise: ask the LLM. With a 12s timeout so the user isn't
      // left waiting if the model is sluggish.
      try {
        const ac = new AbortController();
        const timer = setTimeout(() => ac.abort(), 12000);
        let r;
        try {
          r = await fetch("/api/paper/classify_qa_scope", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              selected_text: (selectedText || "(未划选)").slice(0, 2000),
              question: (question || "").slice(0, 1000),
              preset: preset || "",
            }),
            signal: ac.signal,
          });
        } finally {
          clearTimeout(timer);
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        // ⚠ 白名单必须与后端 `QA_SCOPE_MODES` **逐字一致**（2026-11-19）。
        //   少一个 → router 返回的档位在这里被判「非法」→ 抛错 →
        //   静默退回关键词启发式 → 用户看到的是「关键词 fallback」而不是路由结果。
        if (!data || !_QA_ROUTER_MODES.includes(data.mode)) {
          throw new Error("router 返回 mode 非法");
        }
        // The router can answer 80000; clamp it here or the context we
        // build would blow past the backend's 50000-char field limit.
        const budget = Math.min(Number(data.budget) || 8000, _MAX_SCOPE_BUDGET);
        const label = ({
          term: "紧邻上下文",
          mid:  "当前页/当前小节",
          section: "整个小节",
          wide: "摘要+当前部分+结论",
          full: "全文摘要",
        })[data.mode] + ` (~${budget.toLocaleString()} 字)`;
        return {
          mode: data.mode, budget, label,
          source: "ai", reason: data.reason || "(无)",
        };
      } catch (err) {
        // Router unavailable (network, timeout, LLM down, bad JSON) —
        // fall back to keyword heuristic. We log so the diagnostic
        // panel shows what happened, but we never block the user.
        console.warn("[qa-scope router] fallback to keyword:", err && err.message);
        const kw = _keywordClassify(question) || {
          mode: "mid", budget: 8000, label: "章节上下文 (~8K 字)",
        };
        return { ...kw, source: "keyword", reason: "LLM 路由失败,关键词兜底" };
      }
    }

    // Pure-keyword fallback (used when the LLM router is unavailable).
    // Returns null when no specific keyword matches — caller falls back
    // to mid (default).
    function _keywordClassify(question) {
      const q = (question || "").trim();
      const ql = q.toLowerCase();
      if (/(术语|缩写|全称|符号|定义|意思|含义|代表什么|是什么|缩写名|英文|缩写词)/.test(q)) {
        return { mode: "term", budget: 600, label: "紧邻上下文 (~600 字)" };
      }
      if (/(创新点?|主要贡献|贡献所在|创新性|创新之处|创新贡献|novel|contribution|有什么创新|相比.*?优势|相比.*?改进)/.test(ql + q)) {
        return { mode: "wide", budget: 20000, label: "摘要+当前章节+结论" };
      }
      if (/(整篇|全文|整体|主旨|总结一下|概括|主要思想|核心观点|大纲|structure|overview|summarize|summary)/.test(ql + q)) {
        return { mode: "full", budget: _MAX_SCOPE_BUDGET, label: "全文摘要 (~45K 字)" };
      }
      if (/(实现方法|怎么实现|如何实现|实验方法|流程|对比.*?性能|评测|仿真|复现|具体怎么做|用了什么方法|算法)/.test(q)) {
        return { mode: "wide", budget: 14000, label: "摘要+当前章节+结论" };
      }
      return null; // signal "no specific hit"
    }

    // Locate the user's selection inside the source's full text so we can
    // take a tight window around it (term mode) instead of handing over the
    // whole page. Returns "" if we can't find a unique anchor — caller
    // falls back to head-truncation in that case.
    function _sliceAroundSelection(fullText, selection, budget) {
      if (!fullText) return "";
      if (!selection || selection.length < 4) return fullText.slice(0, budget);
      const idx = fullText.indexOf(selection);
      if (idx < 0) {
        // Selection text may have been normalized (whitespace, smart quotes).
        // Try a fuzzy prefix match: take the first 24 chars of selection.
        const anchor = selection.slice(0, 24);
        const aidx = fullText.indexOf(anchor);
        if (aidx < 0) return fullText.slice(0, budget);
        const half = Math.floor(budget / 2);
        const start = Math.max(0, aidx - half);
        const end = Math.min(fullText.length, aidx + budget - half);
        return fullText.slice(start, end);
      }
      const half = Math.floor(budget / 2);
      const start = Math.max(0, idx - half);
      const end = Math.min(fullText.length, idx + selection.length + half);
      return fullText.slice(start, end);
    }

    // ---------------------------------------------------------------
    // Refinement loop: when the user is unsatisfied with the answer,
    // they can ⬆️ promote scope (see more paper), ⬇️ demote it (focus
    // tighter), or ✏️ write free-form feedback that gets threaded into
    // both the router prompt (so the LLM re-decides scope) AND the
    // answer prompt (so the LLM knows what to fix).
    //
    // We don't re-run submitReaderQA — that would tear down the panel
    // and lose scroll position. Instead we update the existing
    // stream-target in place and stream the new answer right on top.
    // ---------------------------------------------------------------
    const _SCOPE_LADDER = [
      { mode: "term", budget: 800,  label: "紧邻上下文 (~800 字)" },
      { mode: "mid",  budget: 8000, label: "章节上下文 (~8K 字)" },
      // 「小节全文」这一档：选中标题时自动命中（2026-11-18）。
      // 放在 mid 与 wide 之间，⬆️ 升级 = 看得更宽，⬇️ 收窄 = 回到页面级。
      { mode: "section", budget: _QA_SECTION_BUDGET, label: "小节全文 (~16K 字)" },
      { mode: "wide", budget: 20000, label: "摘要+当前章节+结论 (~20K 字)" },
      { mode: "full", budget: _MAX_SCOPE_BUDGET, label: "全文摘要 (~45K 字)" },
    ];

    async function _refineQA(answerBody, direction, feedbackText) {
      const question = answerBody.dataset.question || "";
      const priorAnswer = answerBody.dataset.priorAnswer || "";
      const priorMode = answerBody.dataset.priorScopeMode || "mid";
      // ⚠ 用**提问当时的快照**，不是现在的实时选区（2026-11-22 独立审查实测复现）：
      //   `_readerSelState` 会被**任何一次带选区的 mouseup** 改写 →
      //   用户问完又划了别的段落，再点「⬆️ 多看点上下文」→
      //   模型拿到的是「旧问题 + 新选区」→ 答偏。
      const snapPageNum = answerBody.dataset.pageNum
        ? parseInt(answerBody.dataset.pageNum, 10) : null;
      const snapPdfPath = answerBody.dataset.pdfPath || "";
      const snapSelText = answerBody.dataset.selText || "";
      const snapSourceType = answerBody.dataset.sourceType || "general";

      // 1. Resolve the new scope.
      let newScope;
      if (direction === "up" || direction === "down") {
        const idx = Math.max(0, _SCOPE_LADDER.findIndex(s => s.mode === priorMode));
        const nextIdx = direction === "up"
          ? Math.min(idx + 1, _SCOPE_LADDER.length - 1)
          : Math.max(idx - 1, 0);
        newScope = _SCOPE_LADDER[nextIdx];
      } else if (direction === "feedback") {
        // Let the LLM re-decide scope with the user's free-form feedback
        // appended as a hint. We deliberately route the *augmented*
        // question through stage 1 so the model can re-evaluate.
        const augmented = `${question}\n\n[用户反馈,用于本次重新决策上下文范围] ${feedbackText}`;
        newScope = await _routeQuestionScope(
          augmented, _currentQAPreset, _readerSelState.text || ""
        );
      } else {
        return; // unknown direction
      }

      // 2. Build context for the new scope.
      const ctxBundle = _buildScopeContextBundle(newScope);
      const surroundingContext = ctxBundle.text;
      // ⚠ 上下文**可视化**：让用户看见「这一轮模型到底看到了什么」
      //   （上下文对不对，只有看得见才能判断 —— 用户 2026-11-19 原话）。
      _renderQaContextBox(answerBody, ctxBundle.parts, surroundingContext);

      // ⚠ 2026-11-18：提示里的「用户选中的是一个小节标题 → 下面是这一小节的完整正文」
      //   **只有在上下文真的是这一小节时才成立**。
      //   用户点 ⬆️/⬇️ 把范围调到别的档（wide/full/mid）后，上下文就换成整页/全文了
      //   → 这时还报 selection_kind="heading" = **对模型说假话** → 它会以为
      //   整页都是这一节 → 总结跑偏。
      //   → 所以「是不是标题」要跟**实际用的 scope** 对齐。
      const headingScoped = (newScope.mode === "section") && !!_qaHeadingHit;

      // 3. Replace the prior answer with a streaming placeholder so the
      //    user can see "AI 正在重新回答…" right where the old answer was.
      const streamTarget = answerBody.querySelector("[data-stream-target]");
      const scopeBadge = answerBody.querySelector("[data-qa-scope-badge]");
      const modelBadge = answerBody.querySelector("[data-qa-model-badge]");
      if (streamTarget) {
        streamTarget.textContent = "⏳ AI 重新回答中...";
        streamTarget.parentElement.scrollTop = streamTarget.parentElement.scrollHeight;
      }
      if (scopeBadge) {
        const actualChars = (surroundingContext || "").length;
        scopeBadge.textContent = `🔁 重新回答 · ${newScope.label} · 实际 ${actualChars.toLocaleString()} 字`;
      }
      if (modelBadge) modelBadge.textContent = "⏳ 解答中...";
      // 实时状态单独一个徽标（不能写进模型徽标：进度会把它覆盖掉）
      const _qaStageBadge = answerBody.querySelector("[data-qa-stage-badge]");

      // 4. Re-issue the streaming call. payload carries the prior answer
      //    + feedback so the LLM can avoid repeating the same content
      //    and steer toward what the user actually wants.
      const submitBtn = document.getElementById("qa-submit-btn");
      // 浏览器那张截图只有当**当前选区还在同一页**时才发得出去
      //   （否则截到的是用户后来划的别处 → 图文不符）。
      // ⚠ 2026-11-25：截图是**异步**的（位图可能被释放 → 要先画回来），
      //   所以必须先算出来再进 payload 对象字面量。
      const _refinePageImage = (snapPageNum && (_readerSelState.pageNum === snapPageNum))
        ? await _captureSelectionPageImage() : null;
      await askLLMStream(submitBtn, "/api/paper/explain_selection_stream", {
        // Backend caps: selected_text 15000 / question 1000 / paper_title 300
        // / feedback 2000 / prior_answer 8000.
        selected_text: (snapSelText || "(用户未划选特定片段,提问全篇整体背景)").slice(0, 15000),
        question: (question || "").slice(0, 1000),
        paper_title: (_readerState.title || "").slice(0, 300),
        surrounding_context: surroundingContext,
        source_type: snapSourceType,
        // ⚠⚠ 重新回答也必须带 **pdf 路径 + 页码 + 公式坐标**（2026-11-22 修）：
        //   缺了它们 → 服务端解析不出 PDF → 一张图都拿不到 → 连「附件说明」
        //   都不生成 → 而上下文里的【公式N】是**裸占位** → 模型只见占位符
        //   → **瞎编公式**（AGENTS §4.6：错公式比没公式危险得多）。
        //   服务端拿到 pdf_relative_path + page_num 会按 PDF 原始坐标
        //   **重渲染一张清晰整页图**（比浏览器画布清楚得多）。
        pdf_relative_path: snapPdfPath || null,
        page_num: snapPageNum || null,
        // 浏览器那张截图只有当**当前选区还在同一页**时才发得出去
        //   （否则截到的是用户后来划的别处 → 图文不符）。
        page_image: _refinePageImage,
        // 该页公式的坐标 → 服务端按坐标裁**高清公式原图**给模型
        formula_rects: _collectPageFormulaRects(snapPageNum),
        feedback: (feedbackText || "").slice(0, 2000),
        prior_answer: (priorAnswer || "").slice(0, 8000),
        force_scope: newScope.mode,
        // 命中标题时，重新回答也要带着「本小节」的定位信息
        // （否则服务端会以为用户划的是半句话，提示里没有小节约束）。
        // ⚠ 只有**上下文确实是这一小节**时才报 heading（见上面的 headingScoped）。
        selection_kind: headingScoped ? "heading" : "body",
        heading_title: headingScoped ? (_qaHeadingHit.title || "") : "",
        section_pages: headingScoped ? _sectionPageList(_qaHeadingHit) : [],
        formula_rects_with_page: headingScoped ? _sectionFormulaRects(_qaHeadingHit) : [],
        // 用**答疑面板**自己选中的模型回答（与翻译用的模型互不影响）：
        // '' = 跟随默认 / 'agnes' = 备用 / 'deepseek-flash' = DeepSeek /
        // 'custom:xxx' = 用户自己的 API 模型
        model: _qaModel || "",
      }, {
        label: "划词答疑",
        onStage: (stage, t) => {
          if (!_qaStageBadge) return;
          const el = t.startedAt ? (Date.now() - t.startedAt) : 0;
          const danger = el > 90000;
          _qaStageBadge.innerHTML = `<span class="ai-inline-status" data-warn="${danger ? "1" : "0"}">`
            + `<span class="ai-beat"></span>${escapeHtml(stage)} · ${_aiFormatMs(el)}</span>`;
        },
        onEnd: (state) => {
          if (!_qaStageBadge) return;
          _qaStageBadge.textContent = state === "done" ? "✅ 完成"
            : state === "cancelled" ? "✕ 已取消" : "❌ 失败";
        },
        onMeta: (meta) => {
          if (modelBadge && meta && meta.model) {
            modelBadge.innerHTML = `🤖 ${escapeHtml(meta.model)}${meta.is_degraded ? ' (降格)' : ''}`;
          }
        },
        onChunk: (chunk, full) => {
          if (streamTarget) {
            streamTarget.textContent = full;
            streamTarget.parentElement.scrollTop = streamTarget.parentElement.scrollHeight;
          }
        },
        onSuccess: (full, meta) => {
          if (streamTarget) {
            streamTarget.innerHTML = formatMarkdownLite(full);
            if (typeof renderMathIn === "function") renderMathIn(streamTarget);
          }
          // Update state so the next refinement starts from this answer.
          answerBody.dataset.priorAnswer = full;
          answerBody.dataset.priorScope = newScope.label;
          answerBody.dataset.priorScopeMode = newScope.mode;
          if (typeof renderMathIn === "function") renderMathIn(answerBody);
        },
        onError: (err) => {
          if (streamTarget) {
            streamTarget.innerHTML = `<span style="color:var(--accent-terracotta);">❌ 重新回答失败: ${escapeHtml(err.message || String(err))}</span>`;
          }
        },
      });
    }

    // -----------------------------------------------------------------------
    // 上下文 = **分段结构**（2026-11-19）
    //
    // ⚠ 为什么 wide/full 不再用章节视图（2026-11-19 真机实测抓到的）：
    //   PDF 模式下 `_readerState.sections` 常常只有「Full Text」**一桶**
    //   （§4.110 记过这个残留）→ wide 给的是 `[Section Full Text (摘要/引言)]`
    //   一整篇**没有小节结构**的文字；full 给的是 `[Full Text]` 一桶 43,767 字。
    //   而且那一桶是 **PDF.js 扁平文本**（左右栏交错 + 公式碎片，如 "k 50.94.20"）。
    //   → 改用**真小节大纲**（服务端按论文阅读顺序算的，公式是【公式N】占位）。
    //
    // ⚠ 为什么必须分段（而不是拼成一坨）：
    //   ① 用户要能**看见**这次送了什么 → 只有分段才说得清；
    //   ② 用户划的那句必须在上下文里、而且**要在最前面**
    //      （实测 section 档里它落在 95.4% 处 → 模型第一眼看不到用户关心的句子）；
    //   ③ 各档的**来源**不同（真小节 / 整页译文 / PDF.js 扁平文本），
    //      必须分别标注，否则模型会把「整页译文」当成本节内容（§4.113）。
    // -----------------------------------------------------------------------

    // 锚点：当前页（PDF.js 扁平文本）或章节视图当前那一节
    function _qaAnchorText() {
      let text = "";
      let label = "";
      if (_readerSelState.pageNum && _pdfState && _pdfState.pages) {
        const pObj = _pdfState.pages.find(p => p.num === _readerSelState.pageNum);
        if (pObj) { text = pObj.text || ""; label = "[Page " + _readerSelState.pageNum + "]"; }
      }
      if (!text && _readerSelState.sectionIdx !== null && _readerState.sections) {
        const sObj = _readerState.sections[_readerSelState.sectionIdx];
        if (sObj) { text = sObj.text || ""; label = "[Section " + (sObj.title || "") + "]"; }
      }
      if (!text && _readerState.sections && _readerState.sections[_readerState.activeSection]) {
        const sObj = _readerState.sections[_readerState.activeSection];
        text = sObj.text || ""; label = "[Section " + (sObj.title || "") + "]";
      }
      return { text: text, label: label };
    }

    // 小节的页码描述（第 1-2 页）
    function _qaPageDesc(sec) {
      if (!sec || !sec.page_start) return "";
      const from = sec.page_start;
      const to = Math.max(from, sec.page_end || from);
      return "（第 " + from + "-" + to + " 页）";
    }

    // 覆盖某一页的**真小节**（服务端大纲；没有大纲 → 空数组）
    function _qaSectionsForPage(pageNum) {
      if (typeof pageNum !== "number" || pageNum <= 0) return [];
      return _qaOutlineSections().filter(s => _qaSectionCoversPage(s, pageNum));
    }

    // 「能拿到选区的那段正文」—— 优先**真小节**（按论文阅读顺序、公式是占位），
    //   没有小节才退回 PDF.js 扁平页文本（左右栏交错）。
    function _qaSearchableText() {
      const hit = _qaSectionBySelection;
      if (hit && hit.text) return hit.text;
      const onPage = _qaSectionsForPage(_readerSelState.pageNum);
      if (onPage.length) {
        return onPage.map(s => s.text || "").join("\n\n");
      }
      return _qaAnchorText().text;
    }

    // 某个小节所属的「家族」= 它所在的**顶层节 + 该顶层节的所有子节**。
    //   （大纲是扁平的：level 1 = "II. Experimental Procedure"，
    //     level 2 = "(1) Individual Phases…" …）
    //   → 「wide」档给这一族 = 真正把「这一大节」给全，而不是给一桶全文。
    function _qaSectionFamily(outline, cur) {
      if (!cur || !outline || !outline.length) return cur ? [cur] : [];
      let i = outline.indexOf(cur);
      if (i < 0) {
        // ⚠ `_qaSectionBySelection` 是 `_qaSectionPayload()` 造的**副本**，
        //   不是大纲里那一条 → indexOf 找不到 → 按「标题 + 起始页」认回去。
        i = outline.findIndex(s => (s.title || "") === (cur.title || "")
                                && s.page_start === cur.page_start);
      }
      if (i < 0) return [cur];
      let top = i;
      while (top > 0 && (outline[top].level || 1) > 1) top -= 1;
      const out = [];
      for (let k = top; k < outline.length; k++) {
        const s = outline[k];
        if (k > top && (s.level || 1) <= 1) break;
        out.push(s);
      }
      return out.length ? out : [cur];
    }

    // Build context for the chosen scope —— **分段**。
    function _buildScopeContextParts(scope) {
      const mode = (scope && scope.mode) || "mid";
      const budget = (scope && scope.budget) || (_SCOPE_CAPS[mode] || 8000);
      const selText = _readerSelState.text || "";

      // ---- section：本小节完整正文 ----
      if (mode === "section") {
        // 「选中了一个标题」→ 上下文 = **本小节的完整正文**（2026-11-18）。
        // 命中标题时由 submitReaderQA 直接给这个 scope（不走 LLM 路由）。
        const hit = _qaHeadingHit || _qaSectionBySelection;
        if (hit) return _qaCtxParts(_buildSectionContextParts(hit, budget), budget);
        // ⚠ 2026-11-19：**绝不能返回空上下文**。
        //   router 现在会判 section 档（「这一节讲了什么」），
        //   而用户划的可能是**正文**而不是标题 → 命不中小节 →
        //   旧写法直接 return "" → 模型只拿到划的那一句 → 答得很差。
        //   → 落回 mid（当前页 / 当前页涉及的小节）。
        return _buildScopeContextParts({ ...scope, mode: "mid" });
      }

      // ---- term：选区前后紧邻 ----
      if (mode === "term") {
        // Tight window around the selection (≈ budget chars). Don't
        // include the section header here — definitions are usually
        // resolved by the surrounding sentence, not the heading.
        // ⚠ 切窗的**素材**优先用真小节（按论文阅读顺序），
        //   退而才用 PDF.js 扁平页文本（左右栏交错、公式是碎片）。
        const anchor = _qaAnchorText();
        const tight = _sliceAroundSelection(_qaSearchableText(), selText, budget);
        if (tight) {
          return _qaCtxParts([
            _qaCtxPart("【选区前后紧邻片段（" + (anchor.label || "当前页") + "）】",
                       "selection", tight),
          ], budget);
        }
        return _qaCtxParts([
          _qaCtxPart("【注意】未在 " + (anchor.label || "当前页")
                     + " 的文本中找到用户划选片段，改用该页开头。", "note", ""),
          _qaCtxPart("【" + (anchor.label || "当前页") + " 开头】", "page",
                     (anchor.text || "").slice(0, budget)),
        ], budget);
      }

      // ---- mid：选区所在小节（**真小节**，按论文阅读顺序）→ 退到当前页 ----
      if (mode === "mid") {
        const hit = _qaSectionBySelection;
        const onPage = _qaSectionsForPage(_readerSelState.pageNum);
        if (hit || onPage.length) {
          const secs = hit ? [hit] : onPage;
          const body = secs.map(s =>
            "【小节】" + (s.title || "") + _qaPageDesc(s) + "\n" + (s.text || "")
          ).join("\n\n");
          const parts = [
            _qaCtxPart(hit
              ? "【选区所在小节】" + (hit.title || "") + _qaPageDesc(hit)
              : "【当前页涉及的小节（按论文阅读顺序）】", "section", body),
          ];
          // 用户真正在读的是**中文译文** → 带上（如实标注它是整页翻译）
          const zh = _sectionChineseText(secs[0]);
          if (zh) {
            parts.push(_qaCtxPart("【本小节所在页的中文译文（整页翻译，含本页其他段落）】",
                                  "translation", zh));
          }
          return _qaCtxParts(parts, budget);
        }
        // 没有大纲 → 退回 PDF.js 扁平页文本（**如实标注**它的形态）
        const anchor = _qaAnchorText();
        if (!anchor.text) return [];
        return _qaCtxParts([
          _qaCtxPart("【" + (anchor.label || "当前页")
                     + " 原文（PDF.js 扁平文本，左右栏交错；公式为碎片）】",
                     "page", anchor.text),
        ], budget);
      }

      // ---- wide：摘要 + 选区所在的**整个大节** + 结论（**真小节大纲**）----
      if (mode === "wide") {
        const outline = _qaOutlineSections();
        if (outline.length >= 2) {
          const cur = _qaSectionBySelection
            || _qaSectionsForPage(_readerSelState.pageNum)[0]
            || outline[0];
          const picks = [];
          const push = (sec, role) => {
            if (!sec || picks.some(p => p.sec === sec)) return;
            picks.push({ sec: sec, role: role });
          };
          push(outline[0], "摘要/前言");
          // ⚠ 「wide」要真的**宽**：给当前**顶层小节连同它的所有子节**
          //   （例：选区在 "II. Experimental Procedure" → 连 (1)(2)(3)(4) 一起给）
          //   → 这才是「这一大节」，而不是一桶没结构的全文。
          const family = _qaSectionFamily(outline, cur);
          family.forEach((s, i) => push(s, i === 0 ? "选区所在大节" : "同大节 · 子节"));
          push(outline[outline.length - 1], "结论/末节");
          return _qaCtxParts(picks.map(p =>
            _qaCtxPart("【" + p.role + "】" + (p.sec.title || "") + _qaPageDesc(p.sec),
                       "section", p.sec.text || "")
          ), budget);
        }
        return _buildScopeContextLegacy(scope, "wide");
      }

      // ---- full：全篇**按小节**给（不是「Full Text 一桶」）----
      const outline = _qaOutlineSections();
      if (outline.length) {
        return _qaCtxParts(outline.map(s =>
          _qaCtxPart("【小节】" + (s.title || "") + _qaPageDesc(s),
                     "section", s.text || "")
        ), budget);
      }
      return _buildScopeContextLegacy(scope, "full");
    }

    // 没有真小节大纲时的老路子。⚠ PDF 模式下章节视图常常只有「Full Text」一桶
    //   → 这里明确按「按页码顺序拼接全文」处理，并如实标注文本形态。
    function _buildScopeContextLegacy(scope, mode) {
      const budget = scope.budget || (_SCOPE_CAPS[mode] || 8000);
      const selText = _readerSelState.text || "";
      const sections = (_readerState && _readerState.sections) || [];
      const oneBucket = sections.length === 1;   // 「Full Text 一桶」→ 不可用
      if (!sections.length || oneBucket) {
        const allText = (_pdfState && _pdfState.pages || [])
          .map(p => "[Page " + p.num + "]\n" + (p.text || "")).join("\n\n");
        const sliced = _sliceAroundSelection(allText, selText, budget)
          || allText.slice(0, budget);
        return _qaCtxParts([
          _qaCtxPart("【无小节索引：已按页码顺序拼接全文"
                     + "（PDF.js 扁平文本，左右栏交错；公式为碎片）】",
                     "page", sliced),
        ], budget);
      }
      if (mode === "wide") {
        const chunks = [];
        const per = Math.max(2000, Math.floor(budget / 3));
        if (sections[0] && (sections[0].text || "").length > 200) {
          chunks.push(_qaCtxPart("[Section " + (sections[0].title || "1") + " (摘要/引言)]",
                                 "section", (sections[0].text || "").slice(0, per)));
        }
        const activeIdx = _readerSelState.sectionIdx !== null
          ? _readerSelState.sectionIdx : _readerState.activeSection;
        if (activeIdx != null && sections[activeIdx] && activeIdx !== 0) {
          chunks.push(_qaCtxPart("[当前选中: "
                                 + (sections[activeIdx].title || "Section " + (activeIdx + 1)) + "]",
                                 "section", (sections[activeIdx].text || "").slice(0, per)));
        }
        if (sections.length > 1) {
          const last = sections[sections.length - 1];
          if (last !== sections[0] && (last.text || "").length > 200) {
            chunks.push(_qaCtxPart("[末节: " + (last.title || sections.length) + "] (结论)",
                                   "section", (last.text || "").slice(0, per)));
          }
        }
        return _qaCtxParts(chunks, budget);
      }
      // full
      const per = Math.max(1500, Math.floor(budget / Math.min(sections.length, 8)));
      return _qaCtxParts(sections.slice(0, 8).map((sec, i) =>
        _qaCtxPart("[" + (sec.title || "Section " + (i + 1)) + "]", "section",
                   (sec.text || "").slice(0, per))
      ), budget);
    }

    // 旧的字符串入口（护栏与外部调用都走它）
    function _buildScopeContextRaw(scope) {
      return _qaCtxJoin(_buildScopeContextParts(scope));
    }

    // -----------------------------------------------------------------------
    // 「本次送给 AI 的上下文」**可视化**（2026-11-19）
    //
    // 用户原话：「继续做给 ai 上下文的优化和可视化，确保正常划线问问题，
    //            ai 都能得到合适的上下文」。
    //   → 上下文对不对，只有**看得见**才能判断。
    //   → 折叠面板放在徽标行下面、答案上面：一眼看到「模型这次看到了什么」。
    // -----------------------------------------------------------------------
    function _renderQaContextBox(answerBody, parts, text) {
      if (!answerBody || !document.createElement) return;
      let box = answerBody.querySelector("[data-qa-context-box]");
      if (!box) {
        box = document.createElement("details");
        box.setAttribute("data-qa-context-box", "1");
        box.style.cssText = "margin:0 0 12px; padding:0; border:1px solid var(--card-border-subtle);"
          + " border-radius:6px; background:var(--bg-canvas-subtle);";
        box.innerHTML = '<summary data-qa-context-summary style="cursor:pointer; padding:6px 10px;'
          + ' font-size:11px; color:var(--text-muted); user-select:none;"></summary>'
          + '<div data-qa-context-parts style="padding:2px 10px 8px; display:flex;'
          + ' flex-direction:column; gap:3px;"></div>'
          + '<pre data-qa-context-text style="margin:0; padding:8px 10px; max-height:300px;'
          + ' overflow:auto; white-space:pre-wrap; word-break:break-word;'
          + ' font-size:11px; line-height:1.6; color:var(--text-main);'
          + ' border-top:1px solid var(--card-border-subtle);"></pre>';
        const stream = answerBody.querySelector("[data-stream-target]");
        if (stream && stream.parentElement) stream.parentElement.insertBefore(box, stream);
        else answerBody.appendChild(box);
      }
      const total = (text || "").length;
      const list = (parts || []).filter(p => p && (p.text || p.label));
      const summary = box.querySelector("[data-qa-context-summary]");
      if (summary) {
        summary.textContent = "🔍 本次送给 AI 的上下文 · " + total.toLocaleString()
          + " 字 · " + list.length + " 段（点开看模型到底看到了什么）";
      }
      const listEl = box.querySelector("[data-qa-context-parts]");
      if (listEl) {
        listEl.innerHTML = "";
        for (const p of list) {
          const row = document.createElement("div");
          row.style.cssText = "font-size:10.5px; color:var(--text-muted); line-height:1.5;";
          row.textContent = "· " + (p.label || p.kind)
            + (p.text ? "（" + p.text.length.toLocaleString() + " 字）" : "");
          listEl.appendChild(row);
        }
      }
      const pre = box.querySelector("[data-qa-context-text]");
      // ⚠ 用 textContent 不用 innerHTML —— 上下文是论文正文，必须原样且安全。
      if (pre) pre.textContent = text || "";
    }

    // 构造上下文（分段 + 最终文本）
    //   ⚠ 「用户划选的那句」必须**在最前面**：实测 section 档里它落在 95.4% 处
    //   → 模型第一眼看不到用户关心的那句 → 容易答偏。
    function _buildScopeContextBundle(scope) {
      const mode = (scope && scope.mode) || "mid";
      let parts = _buildScopeContextParts(scope);
      const selText = String(_readerSelState.text || "").trim();
      const isHeading = !!_qaHeadingHit;
      if (selText && !isHeading) {
        const nSel = _normQaHeading(selText);
        const nJoined = _normQaHeading(_qaCtxJoin(parts));
        const at = nSel ? nJoined.indexOf(nSel) : -1;
        const already = at >= 0;
        const rel = already ? at / Math.max(1, nJoined.length) : 1;
        // 已经在前 35% → 不重复；否则**前置**一份（几十字，成本极低）
        if (!already || rel > 0.35) {
          parts = [_qaCtxPart("【用户划选的片段（请围绕它回答；完整上下文见下）】",
                              "selection", selText)].concat(parts);
        }
      }
      const raw = _qaCtxJoin(parts);
      const cap = _SCOPE_CAPS[mode] || 8000;
      const limit = Math.min(cap, _MAX_CONTEXT_CHARS);
      const text = _clipQaContext(raw, limit);
      return { parts: parts, text: text };
    }

    // Same as _buildScopeContextRaw, but clamped to what the backend will
    // actually keep: the server truncates the context by scope
    // (scope_caps) and the request field itself is capped at 50000 chars.
    // Every call site goes through this wrapper, so the "实际 N 字" badge
    // always reports the length that is really sent (and the request can
    // never 422 on surrounding_context).
    function _buildScopeContext(scope) {
      // ⚠ 超预算时**头尾都保留**（原来只留头部 → 长章节的结论段直接丢了）。
      return _buildScopeContextBundle(scope).text;
    }

    function triggerQAPreset(type) {
      _currentQAPreset = type;
      _persistLocalState();
      let q = "";
      if (type === "通俗解释") {
        q = "请用通俗易懂的中文深入解释这段内容的核心含义、学术背景与研究动机。";
      } else if (type === "物理机制") {
        q = "这段内容涉及什么半导体物理机制、能带/输运理论或器件模型？请结合公式与物理图景详细推导剖析。";
      } else if (type === "工程启发") {
        q = "这段内容在实际TCAD仿真建模、DTCO器件优化或实验复现中有何指导意义与启发？";
      } else if (type === "术语速查") {
        q = "请梳理这段内容中出现的关键专业术语、缩写与公式符号，给出全称与物理意义。";
      }
      document.getElementById("qa-question-input").value = q;
      submitReaderQA();
    }

    function handleQAInputKeydown(e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submitReaderQA();
      }
    }

    // -------------------------------------------------------------------
    // 截取「所选片段所在页面」的图片 —— 多模态答疑兜底
    // -------------------------------------------------------------------
    // 用户原话（2026-10-23）："minimaxm3 是多模态模型，如果表或者图不好
    // 文字化给他的话，完全可以截图给它，让它回答问题"。
    //
    // 为什么需要：表格和曲线图被 pdfplumber 抽出来只是碎片（"2 4 3" 这种
    // 上下标、坐标轴刻度），模型看到一堆没意义的字符，自然答不上来。
    //
    // 实现：直接用已经渲染好的 PDF.js 画布裁剪 —— 不额外请求服务端，
    // 也没有编码/分辨率的往返损耗。选区矩形 (_readerSelState.rects) 是
    // 相对页面画布的归一化坐标，直接换算成像素即可。
    async function _captureSelectionPageImage(selOverride) {
      try {
        // ⚠ 2026-11-25 审计 P1：允许传入**选区快照**。
        //   答疑流程要 await 大纲就绪 + 路由（数秒），期间用户可能重新划选
        //   → 这里再读全局 `_readerSelState` 就会截到**新选区**的那一页，
        //   与送给模型的文本对不上。
        const sel = selOverride || _readerSelState || {};
        const pageNum = sel.pageNum;
        if (!pageNum) return null;
        const block = document.getElementById(`pdf-page-block-${pageNum}`);
        if (!block) return null;
        const canvas = block.querySelector("canvas.pdf-page-canvas");
        if (!canvas) return null;
        // ⚠ 2026-11-25：整页位图是**按需渲染**的（滚远了会把位图释放掉，
        //   见 renderPdfPage 里的说明）→ 截图前发现画布是空的，
        //   必须**先把这一页画回来**，否则截图直接返回 null →
        //   模型少一张整页图（公式/图表全靠它）。
        if (!canvas.width || !canvas.height) {
          try { await _pdfPaintCanvas(block); } catch (e) { /* 画不回来就算了 */ }
          if (!canvas.width || !canvas.height) return null;
        }

        // 选区矩形（归一化）→ 包围盒；没有选区就发整页
        let box = null;
        const rects = (sel.rects || []).filter(
          r => r && r.w > 0 && r.h > 0
        );
        if (rects.length) {
          let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
          rects.forEach(r => {
            x0 = Math.min(x0, r.x);
            y0 = Math.min(y0, r.y);
            x1 = Math.max(x1, r.x + r.w);
            y1 = Math.max(y1, r.y + r.h);
          });
          // 往外扩一点：表格/图注常常比划选范围大
          const padX = Math.max(0.02, (x1 - x0) * 0.12);
          const padY = Math.max(0.02, (y1 - y0) * 0.12);
          x0 = Math.max(0, x0 - padX);
          y0 = Math.max(0, y0 - padY);
          x1 = Math.min(1, x1 + padX);
          y1 = Math.min(1, y1 + padY);
          box = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
        }

        // 输出尺寸上限：太长 base64 会把请求撑爆，缩到 ~1400px 宽足够看清
        const MAX_W = 1400;
        const sw = box ? box.w * canvas.width : canvas.width;
        const sh = box ? box.h * canvas.height : canvas.height;
        const scale = Math.min(1, MAX_W / Math.max(1, sw));
        const ow = Math.max(1, Math.round(sw * scale));
        const oh = Math.max(1, Math.round(sh * scale));

        const out = document.createElement("canvas");
        out.width = ow;
        out.height = oh;
        const ctx = out.getContext("2d");
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, ow, oh);
        if (box) {
          ctx.drawImage(
            canvas,
            Math.round(box.x * canvas.width),
            Math.round(box.y * canvas.height),
            Math.round(box.w * canvas.width),
            Math.round(box.h * canvas.height),
            0, 0, ow, oh
          );
        } else {
          ctx.drawImage(canvas, 0, 0, ow, oh);
        }
        const dataUrl = out.toDataURL("image/png");
        if (!dataUrl || dataUrl.length < 200) return null;
        return dataUrl;   // "data:image/png;base64,...."
      } catch (e) {
        return null;   // 截图失败不该挡住答疑
      }
    }

    /**
     * 收集当前页的**公式坐标**（PDF 点，左上角原点）。
     *
     * 为什么需要（2026-11-17 用户实测）：
     *   「划译文的词句问问题，因为公式是直接截图放在译文里的，
     *     ai 看上下文的时候完全看不到公式推导，只能猜」。
     *
     * 公式在译文里是**从 PDF 原页裁下来的图片**，文本上下文里只有乱序碎片；
     * 而整页截图上的公式又只有几十像素高（整页图会被多模态模型降采样，
     * 一条 223pt 宽的公式在整页图上只有 ~18px 高）→ 模型照样读不出来。
     * → 唯一能让模型看清的办法：把公式**按坐标单独裁成高清图**一起给它。
     *
     * 坐标从哪来：译文原文里的 ⟦FORMULA:x0,y0,x1,y1⟧ 标记
     *   （抽取层定位到公式时写进去的，模型照抄不改数字）。
     *   · 整页译文      → pg.translation
     *   · 对照模式段落译文 → pg.blockTranslations（公式块由后端直接填标记）
     */
    function _collectPageFormulaRects(pageNum) {
      const out = [];
      if (!pageNum) return out;
      const RE = /⟦FORMULA:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)⟧/g;
      const seen = new Set();
      const push = (txt) => {
        if (!txt) return;
        RE.lastIndex = 0;
        let m;
        while ((m = RE.exec(txt)) !== null) {
          const key = m[1] + "," + m[2] + "," + m[3] + "," + m[4];
          if (seen.has(key)) continue;
          seen.add(key);
          out.push([parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3]), parseFloat(m[4])]);
        }
      };
      try {
        const pg = (_pdfState && _pdfState.pages || []).find(p => p.num === pageNum);
        if (pg) {
          push(pg.translation);
          const bt = pg.blockTranslations || {};
          Object.keys(bt).forEach(k => push(bt[k]));
        }
      } catch (e) { /* 拿不到就只发整页图，不影响答疑 */ }
      // 上限 16：再多的公式对一次问答也没意义（服务端还会再卡一道）
      return out.slice(0, 16);
    }

    async function submitReaderQA() {
      const qInput = document.getElementById("qa-question-input");
      const question = (qInput ? qInput.value.trim() : "");
      if (!question) {
        showToast("请输入您想问的问题", true);
        return;
      }
      // 发出去就把输入框清空（和常见 AI 软件一样）：
      // 问完一题想接着问下一题，不用先手动把上一个问题删掉。
      if (qInput) {
        qInput.value = "";
        try { qInput.focus(); } catch (e) { /* 面板收起时忽略 */ }
      }

      const welcome = document.getElementById("qa-welcome-placeholder");
      const answerBody = document.getElementById("qa-answer-body");
      const submitBtn = document.getElementById("qa-submit-btn");

      if (welcome) welcome.style.display = "none";
      if (answerBody) {
        answerBody.style.display = "block";
        // Streaming-friendly shell: question header + scope badge + model
        // badge placeholder + streaming answer body + action buttons row.
        // The scope badge shows the user what context budget we used; they
        // can override by picking a different preset (or rewrite the
        // question to add keywords like "创新点" / "术语" / "整体").
        answerBody.innerHTML = `
          <div style="margin-bottom:12px; padding-bottom:8px; border-bottom:1px solid var(--card-border-subtle); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">
            <div style="font-weight:600; font-size:13px; color:var(--text-main); flex:1 1 240px; min-width:180px; line-height:1.5; word-break:break-word;">❓ ${escapeHtml(question)}</div>
            <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; min-width:0;">
              <span data-qa-scope-badge style="font-size:10.5px; padding:2px 8px; border-radius:10px; background:var(--bg-canvas-subtle); color:var(--text-muted); border:1px solid var(--card-border);">⏳ 上下文测算中</span>
              <span data-qa-model-badge style="font-size:11px; padding:2px 8px; border-radius:10px; background:var(--bg-canvas-subtle); color:var(--text-muted); border:1px solid var(--card-border);">⏳ 解答中...</span>
              <span data-qa-stage-badge style="font-size:10.5px; padding:2px 8px; border-radius:10px; background:transparent; color:var(--text-muted); border:1px solid transparent;"></span>
            </div>
          </div>
          <div data-stream-target style="font-size:13px; line-height:1.65; color:var(--text-main); margin-bottom:14px; white-space:pre-wrap; min-height:1.4em;">⏳ AI 正在深度剖析所选内容与半导体物理机制...</div>
          <div data-qa-actions style="display:flex; gap:8px; align-items:center; justify-content:flex-end; padding-top:8px; border-top:1px solid var(--card-border-subtle); flex-wrap:wrap;"></div>
        `;
      }

      // Adaptive scope: stage 1 lets the LLM itself judge how much context
      // is needed. The user explicitly asked for this — keyword heuristics
      // can't catch open-ended questions like "这个方法在我自己的工艺节点
      // 上能用吗" (looks like mid but actually needs wide). A small router
      // call (~200 tokens, ~3-8s) reads the selection + question and
      // returns {mode, budget, reason}; if it fails (network, timeout, LLM
      // down) we silently fall back to the keyword heuristic so the main
      // QA flow is never blocked.
      //
      // ⚠ 例外（2026-11-18）：**选中的是标题**时直接走「小节全文」，
      //   不问路由 —— ① 省一次 3~8s 往返；② 「总结这一节」该给多少上下文
      //   是确定的（就是这一节），让模型去猜反而可能猜错。
      // ⚠ 2026-11-25 审计 P1：**选区快照必须在任何 await 之前取**。
      //   下面要 await 大纲就绪（数秒）+ 选区定位 + 路由（3~8s），
      //   期间用户重新划一次选区 → `_readerSelState` 被改写 →
      //   「问的是 A、答的却是 B」，而且 `_lastQAItem` 与「重新回答」
      //   记下的也是**新选区** → 用户越修越乱。
      // → 这里一次性冻结选区，后面全部用快照。
      const _selSnapshot = {
        text: (_readerSelState && _readerSelState.text) || "",
        source: (_readerSelState && _readerSelState.source) || "general",
        pageNum: (_readerSelState && _readerSelState.pageNum != null)
                  ? _readerSelState.pageNum : null,
        sectionIdx: (_readerSelState && _readerSelState.sectionIdx != null)
                    ? _readerSelState.sectionIdx : null,
        rects: ((_readerSelState && _readerSelState.rects) || []).slice(),
      };
      await _ensureQaOutlineReady();
      const headingHit = _matchQaHeading(_selSnapshot.text);
      _qaHeadingHit = headingHit;
      // ⚠ 划的是**正文**时，也可能落在某个小节里（router 判 section 档时会用）
      //   页码 + 选区矩形是**位置证据**：同一句话可能在多个小节里出现
      //   （实测「magnetostrictive one」同时在 3 个小节里），
      //   只看文本会挑错小节 → AI 总结的是别人那一节（§4.6）。
      _qaSectionBySelection = headingHit ? null :
        await _findQaSectionBySelection(
          _selSnapshot.text,
          _selSnapshot.pageNum,
          _selSnapshot.rects || []);
      let scope;
      if (headingHit) {
        scope = {
          mode: "section",
          budget: _QA_SECTION_BUDGET,
          label: "小节全文 · " + (headingHit.title || ""),
          source: "heading",
          reason: "选中的内容是章节标题 → 上下文换成本小节完整正文",
        };
      } else {
        scope = await _routeQuestionScope(question, _currentQAPreset, _selSnapshot.text);
      }
      const ctxBundle = _buildScopeContextBundle(scope);
      const surroundingContext = ctxBundle.text;
      // ⚠ 上下文**可视化**：折叠面板显示「这次送了什么、每一段多少字、原文长什么样」
      _renderQaContextBox(answerBody, ctxBundle.parts, surroundingContext);
      // Surface the chosen scope to the user so the trade-off is visible.
      // They can always click a different preset to override the heuristic.
      const scopeBadge = answerBody && answerBody.querySelector("[data-qa-scope-badge]");
      if (scopeBadge) {
        const actualChars = (surroundingContext || "").length;
        const sourceTag = scope.source === "ai"
          ? "🧭 AI 路由"
          : (scope.source === "preset" ? "⚙️ preset"
            : (scope.source === "heading" ? "🎯 标题命中" : "🔑 关键词 fallback"));
        scopeBadge.textContent = `${sourceTag} · ${scope.label} · 实际 ${actualChars.toLocaleString()} 字`;
        scopeBadge.title =
          `mode=${scope.mode} · 上限 ${scope.budget.toLocaleString()}\n` +
          `依据: ${scope.reason || "(无)"}\n` +
          `换 preset 可强制覆盖路由结果`;
      }

      // 多模态兜底：把所选页面截图一起发给模型（表格/曲线图不好文字化时，
      // 光靠抽出来的碎片文本是答不出来的）。截图失败不影响正常答疑。
      //
      // ⚠ 2026-11-17 修：门禁从「source === 'pdf'」放宽成「**知道页码就截**」。
      //   原来只有从 PDF 原文侧划选才带截图；
      //   用户在**译文栏**划词提问（source='translation'）→ 不带截图 →
      //   而公式在译文里是**图片**（从原页裁下来的）→ 上下文里一个字都没有 →
      //   模型只能猜「公式推导」（用户原话：「ai 看上下文的时候完全看不到公式推导，只能猜」）。
      //   实测（Playwright 拦截请求）：译文栏划选时 has_page_image=false。
      //   → 现在译文栏划选也带上**那一页的图**（服务端会按 PDF 坐标重渲染一张清晰的）。
      let pageImage = null;
      if (_selSnapshot.pageNum) {
        pageImage = await _captureSelectionPageImage(_selSnapshot);
      }

      const payload = {
        // Backend caps: selected_text 15000 / question 1000 / paper_title 300.
        selected_text: (_selSnapshot.text || "(用户未划选特定片段,提问全篇整体背景)").slice(0, 15000),
        question: (question || "").slice(0, 1000),
        paper_title: (_readerState.title || "").slice(0, 300),
        surrounding_context: surroundingContext,
        source_type: _selSnapshot.source || "general",
        page_image: pageImage,
        pdf_relative_path: (_pdfState && _pdfState.relativePath) || null,
        // ⚠ 页码：服务端据此按 PDF 原始坐标**重渲染一张清晰整页图**给模型
        //   （浏览器那张是 PDF.js 画布，按栏宽渲染 → 整页只有 ~785px 宽，
        //     公式在上面只有几十像素，多模态模型读不出来）。
        page_num: _selSnapshot.pageNum || null,
        // ⚠ 该页公式的坐标（PDF 点）→ 服务端按坐标裁**高清公式原图**给模型。
        //   整页图只给布局上下文：一条 223pt 宽的公式在整页图上只有 ~18px 高，
        //   模型读不出来（用户原话：公式推导完全看不到，只能猜）。
        formula_rects: _collectPageFormulaRects(_selSnapshot.pageNum),
        // Tell the server which scope we used. Without it the backend
        // falls back to the mid cap (8000) and silently truncates the
        // wide (20K) / full (50K) contexts.
        force_scope: scope.mode,
        // ---- 「选中的是标题」专用（2026-11-18）----
        // 后端据此在提示里明确告诉模型「用户选中的是本小节标题，
        // 下面给的是本小节完整正文」，并且按 section 档截断上下文。
        selection_kind: headingHit ? "heading" : "body",
        heading_title: headingHit ? (headingHit.title || "") : "",
        section_pages: headingHit ? _sectionPageList(headingHit) : [],
        // 本小节**所有页**的公式坐标（[页码,x0,y0,x1,y1]）——
        // 小节常常跨页，只给选中页的公式图，跨页那一半的推导模型看不到。
        formula_rects_with_page: headingHit ? _sectionFormulaRects(headingHit) : [],
        // 用**答疑面板**自己选中的模型回答（与翻译用的模型互不影响）：
        // '' = 跟随默认 / 'agnes' = 备用 / 'deepseek-flash' = DeepSeek /
        // 'custom:xxx' = 用户自己的 API 模型
        model: _qaModel || "",
      };

      await askLLMStream(submitBtn, "/api/paper/explain_selection_stream", payload, {
        label: "划词答疑",
        onStage: (stage, t) => {
          // 模型在思考时（首个 token 之前）也能看到"等了多久、卡在哪一步"。
          // ⚠️ 必须写进**独立的**进度徽标，不能写进模型徽标 ——
          //    写同一个元素的话，进度每 500ms 刷一次就把模型名吃掉了，
          //    而且收尾后没人恢复 → 徽标永远停在"还在想"（2026-10-25 浏览器实测）。
          const badge = answerBody.querySelector("[data-qa-stage-badge]");
          if (!badge) return;
          const el = t.startedAt ? (Date.now() - t.startedAt) : 0;
          const danger = el > 90000;
          badge.innerHTML = `<span class="ai-inline-status" data-warn="${danger ? "1" : "0"}">`
            + `<span class="ai-beat"></span>${escapeHtml(stage)} · ${_aiFormatMs(el)}</span>`;
        },
        onEnd: (state) => {
          const badge = answerBody.querySelector("[data-qa-stage-badge]");
          if (!badge) return;
          badge.textContent = state === "done" ? "✅ 完成"
            : state === "cancelled" ? "✕ 已取消"
            : "❌ 失败";
        },
        onMeta: (meta) => {
          const badge = answerBody.querySelector("[data-qa-model-badge]");
          if (badge && meta && meta.model) {
            badge.innerHTML = `🤖 ${escapeHtml(meta.model)}${meta.is_degraded ? ' (降格)' : ''}`;
          }
        },
        onChunk: (chunk, full) => {
          const streamTarget = answerBody.querySelector("[data-stream-target]");
          if (streamTarget) {
            streamTarget.textContent = full;
            streamTarget.parentElement.scrollTop = streamTarget.parentElement.scrollHeight;
          }
        },
        onSuccess: (full, meta) => {
          _lastAnswerText = full;
          _lastQAItem = {
            title: _readerState.title || "未命名论文",
            question: question,
            selection: _selSnapshot.text || "",
            // ⚠ 2026-11-25 审计 P1：原来**漏了 page 字段** ——
            //   存进会话历史/避坑库的答疑卡片没有页码，回头找不到出处。
            page: _selSnapshot.pageNum,
            answer: full,
            mode: _currentQAPreset || "explain",
            topic_category: (document.getElementById("paper-category") && document.getElementById("paper-category").value) ||
                            "01_Device_TCAD_器件仿真",
            timestamp: new Date().toISOString()
          };
          _sessionQAList.push(_lastQAItem);
          _persistLocalState();

          const streamTarget = answerBody.querySelector("[data-stream-target]");
          if (streamTarget) {
            streamTarget.innerHTML = formatMarkdownLite(full);
          }
          // Stash the full prior answer + question on the answerBody so the
          // "refine" buttons (⬆️ / ⬇️ / ✏️ 反馈) can re-issue with feedback
          // threading. Without this the refine loop would have no memory of
          // what the user just saw.
          answerBody.dataset.question = question;
          answerBody.dataset.priorAnswer = full;
          answerBody.dataset.priorModel = (meta && meta.model) || "";
          answerBody.dataset.priorScope = scope.label;
          answerBody.dataset.priorScopeMode = scope.mode;
          // ⚠ 存**提问当时**的快照（2026-11-22 修）：
          //   「重新回答」只读这里，**不读实时选区** ——
          //   否则用户问完又划了别的段落，模型会拿到「旧问题 + 新选区」。
          answerBody.dataset.pageNum = String(_selSnapshot.pageNum || "");
          answerBody.dataset.pdfPath = (_pdfState && _pdfState.relativePath) || "";
          answerBody.dataset.selText = _selSnapshot.text || "";
          answerBody.dataset.sourceType = _selSnapshot.source || "general";

          // Render action buttons AFTER successful streaming. Order matters:
          //   1. Refinement (close to the answer it refines)
          //   2. Save / export (further action)
          const actions = answerBody.querySelector("[data-qa-actions]");
          if (actions) {
            actions.innerHTML = `
              <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; width:100%;">
                <span style="font-size:10.5px; color:var(--text-subtle); margin-right:2px;">回答不满意?</span>
                <button class="btn-pill-outline" style="font-size:10.5px; padding:2px 8px;" data-refine="up" title="下一轮把上下文范围升一档 (mid→wide→full),让 AI 看到更多论文内容">⬆️ 多看点上下文</button>
                <button class="btn-pill-outline" style="font-size:10.5px; padding:2px 8px;" data-refine="down" title="下一轮把上下文范围降一档 (full→wide→mid→term),让 AI 聚焦更紧">⬇️ 少看上下文</button>
                <button class="btn-pill-outline" style="font-size:10.5px; padding:2px 8px;" data-refine="feedback" title="自己写一段反馈(答得太浅/偏题/要更技术细节...),把这条说明作为下一轮的 hint">✏️ 带反馈重答</button>
                <span style="flex:1;"></span>
                <button class="btn-pill-outline" style="font-size:11px; padding:3px 10px; color:#2E7D32; border-color:#81C784; background:#F1F8E9;" onclick="exportCurrentQAToObsidian()" title="将本条答疑保存到 Obsidian 04_AI划词答疑 文件夹">💾 导出本条答疑到 Obsidian</button>
                <button class="btn-pill-outline" style="font-size:11px; padding:3px 10px;" onclick="copyLastQAAnswer()">📋 复制解答</button>
                <button class="btn-pill-outline" style="font-size:11px; padding:3px 10px;" onclick="saveQAToTroubleshoot('${escapeJsAttr(question.slice(0, 30))}')">➕ 存入避坑库</button>
              </div>
              <div data-refine-feedback hidden style="margin-top:8px; padding:10px; background:var(--bg-canvas-subtle); border-radius:6px; border:1px solid var(--card-border-subtle);">
                <div style="font-size:11px; color:var(--text-muted); margin-bottom:6px;">告诉 AI 你对刚才的回答哪里不满意,例如 "答得太笼统,要具体公式"、"忽略了我问的 X 角度"、"引用一下 §3 的实验结果" 等:</div>
                <textarea data-refine-feedback-input style="width:100%; min-height:60px; padding:6px 10px; font-size:12px; border:1px solid var(--card-border); border-radius:6px; background:var(--bg-canvas); resize:vertical;" placeholder="例如: 解释得太空泛,要列出公式推导和数据支撑"></textarea>
                <div style="display:flex; gap:6px; margin-top:6px; justify-content:flex-end;">
                  <button class="btn-pill-outline" style="font-size:11px; padding:2px 10px;" data-refine-feedback-cancel>取消</button>
                  <button class="btn-pill-outline" style="font-size:11px; padding:2px 10px; color:var(--accent-olive); border-color:var(--accent-olive);" data-refine-feedback-confirm>🚀 提交反馈并重答</button>
                </div>
              </div>
            `;
            // Wire refinement handlers (inline so we keep closure over answerBody)
            const refineUp = actions.querySelector('[data-refine="up"]');
            const refineDown = actions.querySelector('[data-refine="down"]');
            const refineFb = actions.querySelector('[data-refine="feedback"]');
            const fbPanel = actions.querySelector("[data-refine-feedback]");
            const fbInput = actions.querySelector("[data-refine-feedback-input]");
            const fbCancel = actions.querySelector("[data-refine-feedback-cancel]");
            const fbConfirm = actions.querySelector("[data-refine-feedback-confirm]");
            if (refineUp) refineUp.onclick = () => _refineQA(answerBody, "up");
            if (refineDown) refineDown.onclick = () => _refineQA(answerBody, "down");
            if (refineFb) refineFb.onclick = () => {
              if (fbPanel) {
                fbPanel.hidden = false;
                if (fbInput) fbInput.focus();
              }
            };
            if (fbCancel) fbCancel.onclick = () => {
              if (fbPanel) fbPanel.hidden = true;
              if (fbInput) fbInput.value = "";
            };
            if (fbConfirm) fbConfirm.onclick = () => {
              const fbText = (fbInput && fbInput.value || "").trim();
              if (!fbText) {
                showToast("请先输入反馈内容", true);
                return;
              }
              _refineQA(answerBody, "feedback", fbText);
            };
          }

          if (typeof renderMathIn === "function") renderMathIn(answerBody);
          fetchModelStatus();
        },
        onError: (err) => {
          const streamTarget = answerBody.querySelector("[data-stream-target]");
          if (streamTarget) {
            streamTarget.innerHTML = `
              <div style="padding:14px; color:var(--accent-terracotta);">
                <div style="font-weight:600; margin-bottom:4px;">❌ 答疑失败</div>
                <div style="font-size:12px;">${escapeHtml(err.message || String(err))}</div>
                <button class="btn-pill-outline" style="font-size:11px; padding:3px 8px; margin-top:8px;" onclick="submitReaderQA()">🔄 重试提问</button>
              </div>
            `;
          }
        },
        onAbort: () => {
          const streamTarget = answerBody.querySelector("[data-stream-target]");
          if (streamTarget) {
            streamTarget.innerHTML = `
              <div style="padding:14px; color:var(--text-muted);">
                <div style="font-weight:600; margin-bottom:4px;">✕ 已取消本次解答</div>
                <div style="font-size:12px;">输入下一个问题继续。</div>
              </div>
            `;
          }
        }
      });
    }

    async function exportCurrentQAToObsidian() {
      if (!_lastQAItem || !_lastQAItem.answer) {
        showToast("暂无可导出的答疑内容", true);
        return;
      }
      const topic = (document.getElementById("paper-category") && document.getElementById("paper-category").value) ||
                    "01_Device_TCAD_器件仿真";
      const yearVal = document.getElementById("paper-year") ? parseInt(document.getElementById("paper-year").value, 10) : 2024;
      // Ask user where to save via the modal (default = 04_AI划词答疑/QA_<ts>_<slug>.md)
      const qSlug = (_lastQAItem.question || "QA").replace(/[\\/:*?"<>|]/g, "_").slice(0, 30);
      const defaultFn = `QA_${yearVal || 2024}_${qSlug}`;
      const dest = await showVaultSaveModal({
        topic: topic,
        kind: "literature_cards",
        defaultFilename: defaultFn,
      });
      if (!dest) return;
      showToast("正在导出答疑卡片到 Obsidian...");
      try {
        const payload = {
          title: _lastQAItem.title || _readerState.title || "未命名论文",
          question: _lastQAItem.question,
          selection: _lastQAItem.selection,
          answer: _lastQAItem.answer,
          mode: _lastQAItem.mode || "explain",
          topic_category: topic,
          year: isNaN(yearVal) ? 2024 : yearVal,
          subfolder: dest.subfolder || "",
          filename_override: dest.filename || "",
        };
        const data = await postJson("/api/paper/export_qa", payload);
        showToast(`✅ 答疑已归档: [${data.folder}/${data.file_name}]`, false, 5000);
        loadStats();
      } catch (err) {
        showToast("导出答疑失败: " + err.message, true);
      }
    }

    function copyLastQAAnswer() {
      if (!_lastAnswerText) return;
      navigator.clipboard.writeText(_lastAnswerText).then(() => {
        showToast("✅ 解答已复制到剪贴板");
      }).catch(() => {
        showToast("复制失败，请手动选择复制", true);
      });
    }

    function saveQAToTroubleshoot(keyword) {
      if (!_lastAnswerText) return;
      showKeywordPickerModal({
        defaultKeyword: keyword || (_lastQAItem && _lastQAItem.question ? _lastQAItem.question.slice(0, 30) : ""),
        defaultSolution: _lastAnswerText,
        defaultTool: "论文精读答疑",
      }).then((result) => {
        if (!result) return; // cancelled
        // ⚠⚠ 2026-11-17 全面审查 D1（确证缺陷，实测 422）：
        //   原来这里发的是 `raw_error: ""` / `root_cause: ""`，
        //   而后端 TroubleshootRequest 这两个字段都是 `min_length=1`
        //   → **必然 422**，「➕ 存入避坑库」一键保存从来就没成功过；
        //   而且报错被写成「API 暂不可用」，把排查方向带偏（用户会去查网络）。
        //   现在填**真内容**：原始问题（含引文/页码/出处）→ 错误现场，
        //   解答的关键结论 → 根本原因。
        const qa = _lastQAItem || {};
        const answerText = String(result.solution || _lastAnswerText || "");
        const rawError = [
          "提问：" + String(qa.question || result.keyword || "").trim(),
          qa.selection ? "引文：" + String(qa.selection).trim() : "",
          qa.page ? "页码：第 " + qa.page + " 页" : "",
          qa.title ? "出处：" + qa.title : "",
        ].filter(Boolean).join("\n").slice(0, 10000);
        const rootCause = ("结论（来自 AI 解答）：\n" + answerText.trim())
          .slice(0, 5000);
        // ⚠ 每个必填字段都要有兜底：用户把关键词清空 → 又是一次 422。
        const payload = {
          tool_name: String(result.tool || "").trim() || "论文精读答疑",
          category: String(result.category || "").trim() || "TCAD仿真收敛",
          error_keyword: String(result.keyword || "").trim()
            || String(qa.question || "").trim().slice(0, 30) || "论文精读答疑",
          raw_error: rawError || "论文精读答疑记录",
          root_cause: rootCause || "见解决方案",
          solution: answerText.slice(0, 5000) || "（无解答内容）",
        };
        // Direct one-click save to 03_Knowledge/踩坑与排错日记.md
        fetch("/api/troubleshoot/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }).then(async (r) => {
          let data = null;
          try { data = await r.json(); } catch (e) { data = null; }
          if (r.ok && data && data.status === "success") {
            showToast("✅ 已存入避坑知识库", false, 4000);
            loadStats();
          } else {
            // Fallback to legacy pre-fill if backend rejects
            const kwEl = document.getElementById("tb-keyword");
            const solEl = document.getElementById("tb-solution");
            if (kwEl) kwEl.value = result.keyword;
            if (solEl) solEl.value = result.solution;
            // ⚠ 别把「请求体不合法」说成「API 暂不可用」—— 方向完全不一样。
            const why = (data && data.detail) ? data.detail : ("HTTP " + r.status);
            const head = (r.status === 422)
              ? "请求参数不合法"
              : (r.status >= 500 ? "服务端出错" : "保存失败");
            showToast(head + ", 已预填到右侧避坑表单(请手动保存): " + why, true);
          }
        }).catch(() => {
          // Network failure → fall back to pre-fill UX
          const kwEl = document.getElementById("tb-keyword");
          const solEl = document.getElementById("tb-solution");
          if (kwEl) kwEl.value = result.keyword;
          if (solEl) solEl.value = result.solution;
          showToast("网络异常, 已预填到右侧避坑表单(请手动保存)", true);
        });
      });
    }


function applyRestoredQaPrefs(s) {
  if (!s) return;
  if (Array.isArray(s.sessionQAList)) _sessionQAList = s.sessionQAList;
  if (typeof s.currentQAPreset === "string") _currentQAPreset = s.currentQAPreset;
  if (typeof s.lastAnswerText === "string") _lastAnswerText = s.lastAnswerText;
}
function getQaPrefsSnapshot() {
  return {
    sessionQAList: (_sessionQAList || []).slice(-50),
    currentQAPreset: _currentQAPreset || "通俗解释",
    lastAnswerText: _lastAnswerText || "",
  };
}
function getSessionQaList() { return _sessionQAList || []; }
function getReaderSelState() { return _readerSelState; }

export {
  _readerSelState, _qaGeo, _QA_SECTION_BUDGET, _SCOPE_CAPS, _QA_ROUTER_MODES, _MAX_CONTEXT_CHARS, _MAX_SCOPE_BUDGET, _SCOPE_LADDER,
  _resolveSelectionOrigin, initReaderSelectionListener, toggleReaderQAPanel, minimizeReaderQAPanel, _qaCollapsedHeadClick, openReaderQAPanel,
  _qaPanelStorageKey, _qaClampPanelX, _qaClampPanelY, saveReaderQAPanelGeometry, restoreReaderQAPanelGeometry, initReaderQAPanelDrag,
  updateQAQuotedSnippet, clearQAQuotedText, fetchQaOutline, _ensureQaOutlineReady, _normQaHeading, _qaOutlineSections,
  _qaHeadingCandidates, _matchQaHeading, _qaSectionPayload, _qaSectionCoversPage, _qaSelectionPointPt, _qaBlockAtPoint,
  _qaSectionByGeometry, _findQaSectionBySelection, _clipQaContext, _stripQaMarkers, _sectionChineseText,
  _qaCtxPart, _qaCtxJoin, _qaCtxParts, _buildSectionContextParts, _buildSectionContext, _sectionPageList, _sectionFormulaRects,
  _routeQuestionScope, _keywordClassify, _sliceAroundSelection, _refineQA, _qaAnchorText, _qaPageDesc, _qaSectionsForPage,
  _qaSearchableText, _qaSectionFamily, _buildScopeContextParts, _buildScopeContextLegacy, _buildScopeContextRaw,
  _renderQaContextBox, _buildScopeContextBundle, _buildScopeContext, triggerQAPreset, handleQAInputKeydown,
  _captureSelectionPageImage, _collectPageFormulaRects, submitReaderQA, exportCurrentQAToObsidian, copyLastQAAnswer, saveQAToTroubleshoot,
  applyRestoredQaPrefs, getQaPrefsSnapshot, getSessionQaList, getReaderSelState,
};
