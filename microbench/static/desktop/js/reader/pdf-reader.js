import {
  showToast, _readJsonSafe, _httpErrorMessage, getJson, postJson, loadPdfJs, _sha256Hex16,
  escapeHtml, escapeJsString, escapeJsAttr, _unrenderMathPlaceholders, formatMarkdownLite, renderMathIn,
} from "../core/utils.js";
import { _escBind, _escUnbind, _escOverlayOpen, _wireModalEsc } from "../core/overlay-esc.js";
import { _readerState, _llmStatus, fetchModelStatus, updateModelBadges } from "../state/store.js";
import {
  _READPOS_PANES, _readPosAnchor, _captureReadPos, _restoreReadPosAnchor, _applyReadPos,
  _readPosTargetFor, _readPosReapply, _initReadPosTracking, _restoreSectionReadPos,
  beginReadPosSession, markReadPosRestored, _readPosCurrent, _readPosRestored, _readPosUserMoved,
} from "../state/readpos.js";
import { _pdfPageNearViewport, _pdfObserveCanvas, _pdfPaintCanvas, _pdfCancelAllCanvasRenders } from "./canvas-manager.js";
import { _aiTaskStart, _aiTaskStage, _aiTaskEnd, _aiFormatMs, renderCrossQASectionPicker } from "./ai-activity.js";
import {
  _annoState, loadAnnotationsForPaper, applyAnnotationsToPage, reapplyAnnotations, refreshAnnoBadges,
  openAnnotationEditor, openAnnoDrawer, drawAnnoOverlays, drawAllAnnoOverlays, refreshPdfPageAnnoBadges,
} from "./annotations.js";
import { fetchQaOutline, getSessionQaList } from "./qa-copilot.js";
import { _persistReaderState, _persistLocalState, _restoreTranslationsFromCache, showVaultSaveModal, loadStats } from "../views/dashboard.js";

    // Esc 关闭阅读器的监听句柄（见 openReaderModal）
    let _readerEscHandler = null;

    function openReaderModal() {
      document.getElementById("reader-modal").style.display = "flex";
      document.body.style.overflow = "hidden";
      // Esc 关闭阅读器（2026-10-27）。
      //
      //   之前只能点右上角 × 关窗 —— 全屏阅读器却不能按键盘关，
      //   是很不自然的交互（§7.1）。
      //   ⚠ 输入框里按 Esc **不能**关窗（用户在输入框里想取消编辑，
      //     结果整个阅读器被关掉 = 内容全丢）。
      if (!_readerEscHandler) {
        _readerEscHandler = (e) => {
          if (e.key !== "Escape") return;
          // ⚠ 2026-11-25 审计 P1：有浮层开着 → **让给浮层**。
          //   一次 Esc 只能干一件事：先关最上面那个弹窗/抽屉/编辑器，
          //   而不是连整个阅读器一起关掉（内容全丢）。
          if (_escOverlayOpen()) return;
          const t = e.target;
          const tag = (t && t.tagName || "").toLowerCase();
          if (tag === "input" || tag === "textarea" || tag === "select"
              || (t && t.isContentEditable)) return;
          e.stopPropagation();
          closeReaderModal();
        };
        document.addEventListener("keydown", _readerEscHandler);
      }
      fetchModelStatus();
      // 章节视图：回到上次打开的那一节（PDF 视图的回位在 loadPdfDualColumn 里做，
      // 因为那边要等页块渲染出来才能算准位置）。
      try { _restoreSectionReadPos(); } catch (e) {}
      // 续读采集：两栏的滚动监听（开机就挂上，别等第一次打开 PDF 才挂）
      try { _initReadPosTracking(); } catch (e) {}
      // 专注模式偏好：上次开着 → 这次直接进专注（不用再点一次「专注」按钮）
      if (_zenPref && !_zenMode) {
        setTimeout(() => { if (!_zenMode) enterZenMode({ silent: true }); }, 0);
      }
    }
    function closeReaderModal() {
      // 关窗前把「读到哪儿」存下来（用户可能直接点 X 关掉，
      // 靠滚动防抖存的那份最多差 400ms，这里再存一次兜底）。
      try { _captureReadPos(); } catch (e) {}
      document.getElementById("reader-modal").style.display = "none";
      document.body.style.overflow = "";
      if (_readerEscHandler) {
        document.removeEventListener("keydown", _readerEscHandler);
        _readerEscHandler = null;
      }
      // Clear URL hash so refresh doesn't auto-reopen
      history.replaceState(null, "", window.location.pathname + window.location.search);
      // Stop PDF.js rendering if active
      //
      // `rendering` 上只有 `cancelled` 标志，**没有 cancel() 方法** ——
      // 原来这里调 `rendering.cancel()`，每次关窗都抛 TypeError 并被
      // catch 吃掉，真正的取消全靠一个每秒轮询 getComputedStyle 的
      // interval 兜底（既慢又每秒强制一次样式计算）。现在直接置标志。
      if (_pdfState && _pdfState.rendering) {
        _pdfState.rendering.cancelled = true;
      }
      // ⚠ 2026-11-25 审计 P1：关窗时**释放画布显存**。
      //   原来关掉阅读器后，几十张整页位图还挂在 DOM 上（~880MB）
      //   → 开着别的论文/网页就一直占着，标签页迟早被撑爆。
      //   → 掐掉在飞的 RenderTask + 归零所有画布。
      _pdfCancelAllCanvasRenders();
      // Hide Copilot elements
      const bubble = document.getElementById("reader-selection-bubble");
      if (bubble) bubble.style.display = "none";
      const qaPanel = document.getElementById("reader-qa-panel");
      if (qaPanel) qaPanel.style.display = "none";
    }

    // ---------------------------------------------------------------------------
    // PDF dual-column reader
    // ---------------------------------------------------------------------------
    let _readerMode = "section"; // "section" | "pdf"
    let _pdfZoom = 1.0; // user-controlled PDF zoom multiplier (PDF dual-column view)
    // 对照模式（译文按原文排版）的偏好开关 —— 跨会话记住
    let _bilingualPref = false;
    // 对照模式里**同时显示原页图**的偏好开关。
    // 为什么需要：图片/图表不在 PDF 文本层里 → 译文重排后**看不到图**
    //   （用户原话："对照视图里看不到图片/图表"）。
    // 开着这个开关 → 原页 canvas 不再被藏起来（宽屏左右并排、窄屏上下堆叠）。
    let _bilingualOrigPref = false;
    let _pdfState = null; // { paperId, paperTitle, relativePath, pdfDoc, pages: [{num, canvas, text, translated}], rendering }
    // 当前打开的是不是「中译替换版」—— 那份 PDF 里已经有中文了，
    // 不能让翻译队列再翻一遍（§ 见 openPdfDirect 的 opts.replaced）。
    let _pdfReplacedView = false;
    // 生成替换版时用的**源**（paper_id + 路径）。
    // 为什么单独记：生成后阅读器打开的是**替换版**，`_pdfState` 也就变成了它；
    //   再点一次按钮若还用 `_pdfState` → 会去找「替换版的快照」→ 必然 409。
    let _pdfReplacedSource = null;

    function switchReaderMode(mode, opts = {}) {
      _readerMode = mode;
      _persistLocalState();
      _persistReaderState();
      // ⚠️ 必须用 id 定位，不能用 "#reader-modal > div > div:nth-child(2)"。
      //    那个位置**不是**章节视图区，而是 .reader-topbar（child 1）：
      //    切到 PDF 模式时会把 topbar 藏掉，而章节视图区（reader-body）
      //    继续盖在 PDF 上面 —— 后果是 PDF 模式下的缩放条 / 全篇重译 /
      //    导出 / 同步 / 模型下拉框**整条工具条都不见了**（它们都在 topbar 里），
      //    而且章节视图和 PDF 叠在一起（2026-10-25 浏览器实测抓到）。
      const sectionView = document.getElementById("reader-body");
      const pdfView = document.getElementById("reader-pdf-view");
      const btnSection = document.getElementById("reader-mode-section");
      const btnPdf = document.getElementById("reader-mode-pdf");
      const zoomBar = document.getElementById("pdf-zoom-bar");
      if (mode === "pdf") {
        sectionView.style.display = "none";
        pdfView.style.display = "block";
        btnSection.classList.remove("active");
        btnPdf.classList.add("active");
        // Hide translate-all button (only useful in section mode)
        document.getElementById("translate-all-btn").style.display = "none";
        if (zoomBar) zoomBar.style.display = "flex";
        // opts.skipLoad lets the caller drive the load itself (openFromSnapshot
        // passes a snapshot) instead of racing a second concurrent load.
        if (!opts.skipLoad) loadPdfDualColumn();
      } else {
        sectionView.style.display = "";
        pdfView.style.display = "none";
        btnSection.classList.add("active");
        btnPdf.classList.remove("active");
        document.getElementById("translate-all-btn").style.display = "";
        if (zoomBar) zoomBar.style.display = "none";
      }
    }

    // ⚠ 2026-11-25 审计 P1：缩放重绘的定时器（防抖用）。
    //   原来缩放**完全没有防抖** —— 连点「放大」时每次点击都排一次重绘
    //   （各自 80ms 后启动），多个重绘**并发**跑：各自清空列表、各自重建
    //   _pdfState.pages → 后半篇译文被丢掉、页面来回闪。
    // → 重绘一律走防抖：只保留最后一次点击。
    let _pdfZoomReloadTimer = null;

    /**
     * PDF zoom controls (PDF dual-column view).
     *   setReaderZoom(-1)   → zoom -10%
     *   setReaderZoom(1)    → zoom +10%
     *   setReaderZoom("fit")→ reset to 100%
     * Re-runs loadPdfDualColumn() so each page canvas is re-rendered at new scale.
     */
    function setReaderZoom(deltaOrMode) {
      // ⚠⚠ 缩放前先把「当前看到哪儿」**落盘**（2026-11-22 独立审查修）：
      //   下面马上要把两栏的 DOM 全清空（位置不抓就没了），
      //   而重排时 `loadPdfDualColumn` 是在**清空之后**才去读位置的
      //   （读活 DOM 必然读不到 → 只能回落到 localStorage）
      //   → 而落盘用的是**滚动防抖 400ms** → 「滚动完立刻缩放」会退回旧位置。
      //   ⚠ 这一句必须在**改 `_pdfZoom` 之前**：落盘记录里带着 zoom，
      //     比例换算（§4.126）靠它判断「缩放变了没有」——
      //     若记的是新 zoom，恢复时会被当成「没变缩放」→ 直接套旧 px → 位置偏。
      try { _captureReadPos(); } catch (e) { console.warn("[readpos] 缩放前保存失败", e); }
      if (!_pdfState || !_pdfState.pdfDoc) {
        // First time user toggles zoom before PDF is ready — just update label
        if (deltaOrMode === "fit") _pdfZoom = 1.0;
        else _pdfZoom = Math.max(0.5, Math.min(2.5, (_pdfZoom || 1.0) + (deltaOrMode * 0.1)));
        updateZoomLabel();
        return;
      }
      if (deltaOrMode === "fit") {
        _pdfZoom = 1.0;
      } else {
        _pdfZoom = Math.max(0.5, Math.min(2.5, _pdfZoom + deltaOrMode * 0.1));
      }
      _pdfZoom = Math.round(_pdfZoom * 100) / 100; // 2 decimals
      updateZoomLabel();
      _persistLocalState();
      // Bump generation so any in-flight render loop bails out
      _pdfState.rendering.generation = (_pdfState.rendering.generation || 0) + 1;
      const thisGen = _pdfState.rendering.generation;
      _pdfState.rendering.cancelled = true;
      // Drop duplicate translation blocks (an earlier zoom used to append
      // a second block per page, so the column grew every click).
      _dedupePdfBlocks();
      // Only clear the PDF canvas side — translations don't depend on zoom,
      // so we keep them (and re-use their LLM cache on subsequent fetches).
      // ⚠ 先掐掉在飞的画布渲染（否则旧的 RenderTask 还在往一块已被清空的
      //   画布上画 → 报错 + 白屏 + 显存白占）。
      _pdfCancelAllCanvasRenders();
      document.getElementById("pdf-pages-list").innerHTML = "";
      // Schedule a new render loop after the old one observes its cancel flag
      // ⚠ 2026-11-25 审计 P1：走**防抖**，只保留最后一次点击的那次重绘
      //   （原因见 _pdfZoomReloadTimer 的注释）。
      if (_pdfZoomReloadTimer) clearTimeout(_pdfZoomReloadTimer);
      _pdfZoomReloadTimer = setTimeout(() => {
        _pdfZoomReloadTimer = null;
        loadPdfDualColumn({ keepTranslations: true, generation: thisGen });
      }, 220);
    }
    function updateZoomLabel() {
      const el = document.getElementById("pdf-zoom-label");
      if (el) el.textContent = Math.round((_pdfZoom || 1.0) * 100) + "%";
    }

    // =====================================================================
    // 专注模式（Zen / Focus）
    //
    // 目标：小屏上只留「论文页 + 译文页」，其余界面默认收起。
    //
    // 唤出工具条的方式（哪种都行，总有一个能用）：
    //   - 顶部那条 7px 细把手 —— 点一下（展开后常驻）
    //   - 鼠标移到屏幕最上方 40px（停手 2.6s 自动收起；指针还停在上方就不收）
    //   - 快捷键 Z（切换专注）
    //   - Esc（先收起工具条，再按退出专注）
    //
    // ⚠️ **滚轮滚动一律不唤出**（用户 2026-11-14 明确要求）：
    //    原来「内容区往回上滑 / 滑到最底部」会弹工具条 —— 读书往回看一行，
    //    工具条就跳出来打断阅读。现在工具条**只认鼠标动作**。
    //
    // 工具条 = 原有 .reader-topbar（不另做一套按钮）→ 既有功能一行不少，
    // 也不会和顶栏分叉出两套行为。
    //
    // 两个状态别混：
    //   _zenMode = 当前这个阅读器是不是专注布局（运行时）
    //   _zenPref = 用户偏好（持久化）—— 下次打开论文直接进专注
    // =====================================================================
    let _zenMode = false;
    let _zenPref = false;
    let _zenBarTimer = null;
    let _zenPointerY = -1;              // 最近一次鼠标 Y（判断指针是不是还停在最上面）
    const ZEN_TOP_HOTZONE = 40;         // 鼠标「到了最上面」的判定带（px）
    const ZEN_BAR_AUTO_CLOSE_MS = 2600; // 指针离开顶部后，多久自动收起工具条

    // 上层弹窗（论文快照 / 诊断）开着没有？
    // 它们的 keydown 处理器没有 stopPropagation → 一次 Esc 会两边都响应
    //（弹窗关了 + 专注也退出了）。所以专注的 Esc 要先给它们让路。
    function _zenOverlayOpen() {
      const ids = ["snapshot-modal", "diag-modal"];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el && el.style.display !== "none" && el.style.display !== "") return true;
      }
      return false;
    }

    function _zenModal() { return document.getElementById("reader-modal"); }
    function isZenMode() { return !!_zenMode; }
    // 只有「阅读器开着 + 处于专注」才算激活 —— 关闭阅读器后不该还有交互
    function _zenActive() {
      const modal = _zenModal();
      return !!(modal && _zenMode && modal.style.display !== "none");
    }

    // 布局变了 → PDF 画布可能要按新栏宽重绘。
    //
    // ⚠️ force 必须**按调用点**给，不能一律 true：
    //    - 进出专注（顶栏在文档流 ↔ fixed 浮层）：`.reader-shell` 宽度恒为 100%
    //      （§4.48），顶栏进出**不改变 PDF 栏宽** → 不需要重绘。
    //      一律 force=true 会让 391 页的论文每点一次「专注」就全量重绘一遍
    //      （每页一次 await renderPdfPage）→ 明显卡顿。
    //    - 切单栏 / 切论文页：`.pdf-split` 的列数变了 → 栏宽大变 → 必须重绘。
    //
    // 不用 `window.dispatchEvent(new Event("resize"))` 当内部调用 ——
    // 那是全局广播，将来任何新增的 resize 监听器都会被误触发。
    function _zenRelayoutPdf(force) {
      try {
        _pdfRelayoutIfNeeded(!!force);
      } catch (e) { /* 忽略 */ }
    }

    function _zenSyncLabels() {
      const modal = _zenModal();
      const single = !!(modal && modal.classList.contains("zen-single"));
      const btn = document.getElementById("zen-single-btn");
      if (btn) btn.textContent = single ? "双栏" : "单栏";
      const titleEl = document.getElementById("zen-handle-title");
      const readerTitle = document.getElementById("reader-title");
      if (titleEl) titleEl.textContent = readerTitle ? (readerTitle.textContent || "") : "";
    }

    function _zenOpenBar(sticky) {
      const modal = _zenModal();
      if (!modal || !_zenMode) return;
      modal.classList.add("zen-bar-open");
      clearTimeout(_zenBarTimer);
      _zenBarTimer = null;
      _zenSyncLabels();
      if (!sticky) _zenBarTimer = setTimeout(_zenAutoClose, ZEN_BAR_AUTO_CLOSE_MS);
    }

    function _zenCloseBar() {
      const modal = _zenModal();
      if (!modal || !_zenMode) return;
      modal.classList.remove("zen-bar-open");
      clearTimeout(_zenBarTimer);
      _zenBarTimer = null;
    }

    // 自动收起的守门：指针**还停在屏幕最上面**（= 用户还扶着工具条）就别收 ——
    // 收了用户还得再扫一次鼠标，纯添堵。
    function _zenAutoClose() {
      _zenBarTimer = null;
      if (_zenPointerY >= 0 && _zenPointerY <= ZEN_TOP_HOTZONE) return;
      _zenCloseBar();
    }

    // 鼠标是不是「到了最上面」（屏幕顶部判定带内）。
    // 这是**唯一**能自动唤出工具条的鼠标手势。
    function _zenPointerAtTop(e) {
      return !!e && typeof e.clientY === "number" && e.clientY <= ZEN_TOP_HOTZONE;
    }

    function enterZenMode(opts = {}) {
      const modal = _zenModal();
      if (!modal) return;
      const firstTime = !_zenMode;
      _zenMode = true;
      _zenPref = true;
      modal.classList.add("zen");
      if (opts.single) modal.classList.add("zen-single");
      if (opts.showOrig) modal.classList.add("zen-show-orig");
      _persistLocalState();
      _zenSyncLabels();
      if (firstTime && !opts.silent) {
        showToast("专注模式：鼠标移到屏幕最上面展开工具条 · Z 切换 · Esc 退出", false, 3600);
      }
      // 顶栏脱离文档流 → 论文页变宽 → PDF 必须重绘（否则画布和栏宽对不上）
      // ⚠️ force=false：进出专注**不改变 PDF 栏宽**（壳恒为 100% 宽），
      //    真正的重绘交给 ResizeObserver 按实际宽度差决定 —— 见 _zenRelayoutPdf。
      setTimeout(function () { _zenRelayoutPdf(false); }, 260);
    }

    function exitZenMode() {
      const modal = _zenModal();
      if (!modal) return;
      _zenMode = false;
      _zenPref = false;
      modal.classList.remove("zen", "zen-bar-open", "zen-single",
                            "zen-show-orig", "zen-nav-open", "zen-aux-open");
      clearTimeout(_zenBarTimer);
      _zenBarTimer = null;
      _persistLocalState();
      _zenSyncLabels();
      // 退出专注：顶栏回到文档流，PDF 栏宽不变 → force=false（见 _zenRelayoutPdf）
      setTimeout(function () { _zenRelayoutPdf(false); }, 260);
    }

    function toggleZenMode() {
      if (_zenMode) exitZenMode(); else enterZenMode();
    }

    // ⚠️ 章节导航（.reader-nav）和 AI 提问区（.reader-aux）都住在
    //    「章节视图」容器 #reader-body 里，而 PDF 双栏模式下那个容器
    //    整体是 display:none —— 这时候点「章节」「提问」**什么也不会发生**，
    //    点了像按钮坏了。所以先切到章节视图再展开对应面板。
    //
    //    但章节视图的**内容**来自「全文拆分章节」（_readerState.sections），
    //    只有走过 openVaultPdf / 拉取全文 的论文才有；
    //    仅靠地址栏恢复的本地 PDF 没有章节数据 → 切过去是一片空白，
    //    用户会以为坏了 → 这种情况直接说清楚，不切。
    function _zenHasSections() {
      return !!(typeof _readerState !== "undefined" && _readerState &&
                Array.isArray(_readerState.sections) && _readerState.sections.length);
    }

    // 返回 true = 已切到章节视图（调用方接着展开面板）
    // 返回 false = 切不过去（已弹提示说明原因，或本来就在章节视图）
    function _zenEnsureSectionView() {
      if (_readerMode === "section") return false;
      if (!_zenHasSections()) {
        showToast("这篇还没有章节数据 —— 需要先「拉取全文」拆分章节", true, 3600);
        return false;
      }
      switchReaderMode("section");
      return true;
    }

    function toggleZenNav() {
      const modal = _zenModal();
      if (!modal) return;
      // ⚠ HIGH-3（2026-10-27 审计，实测复现）：窄屏（<860px）下 CSS 里
      //   `#reader-modal.zen.zen-nav-open .reader-nav { display: none; }`
      //   会把刚打开的导航又关掉 → 按钮点了**什么都没发生**：
      //   实测 display=none、元素 0×0、也没有任何提示 → 用户以为按钮坏了。
      //   专注模式本来就是给小屏用的，所以这里必须**如实说明**而不是白点。
      if (window.matchMedia && window.matchMedia("(max-width: 860px)").matches) {
        showToast("小屏放不下章节导航 —— 专注模式请用「提问」，或退出专注看目录", true, 3600);
        return;
      }
      if (_zenEnsureSectionView()) {
        modal.classList.add("zen-nav-open");
        showToast("已切到章节视图（章节导航只在章节视图里有）", false, 2600);
        return;
      }
      if (_readerMode !== "section") return;  // 切不过去就别动开关
      // ⚠️ 已经在章节视图了，但**这篇没有章节数据** → 导航是空的，
      //    打开只会多出一条 200px 的空列（用户看着像坏了）→ 如实提示。
      if (!_zenHasSections()) {
        showToast("这篇还没有章节数据 —— 章节导航需要先「拉取全文」拆分章节", true, 3600);
        return;
      }
      modal.classList.toggle("zen-nav-open");
    }

    function toggleZenAux() {
      const modal = _zenModal();
      if (!modal) return;
      if (_zenEnsureSectionView()) {
        modal.classList.add("zen-aux-open");
        showToast("已切到章节视图（提问区只在章节视图里有）", false, 2600);
        return;
      }
      if (_readerMode !== "section") return;  // 切不过去就别动开关
      modal.classList.toggle("zen-aux-open");
    }

    function toggleZenSingle() {
      const modal = _zenModal();
      if (!modal) return;
      modal.classList.toggle("zen-single");
      _zenSyncLabels();
      // 列数变了 → PDF 栏宽大变 → 必须重绘（force=true）
      setTimeout(function () { _zenRelayoutPdf(true); }, 300);
    }

    function zenTogglePane() {
      const modal = _zenModal();
      if (!modal) return;
      modal.classList.toggle("zen-show-orig");
      // 论文页 / 译文页互斥切换 → PDF 栏宽大变 → 必须重绘（force=true）
      setTimeout(function () { _zenRelayoutPdf(true); }, 300);
    }

    /**
     * 专注模式的交互接线（只跑一次，在 _bootDashboard 里调）。
     * 所有监听器都用 document 级 + 运行时判活跃 —— 阅读器本身是常驻 DOM，
     * 但它在关闭时 display:none，不能让这些监听器在首页上乱触发。
     */
    function _initZenMode() {
      const handle = document.getElementById("zen-handle");
      if (handle) {
        // ⚠️ 把手只负责**展开**工具条，不能"再点一下收起"：
        //    工具条打开时把手是 `pointer-events:none` 且被顶栏盖住
        //    （z-index 1199 < 1200）→ 点不到 → 「再点一下收起」是死分支。
        //    收起靠：Esc / 鼠标移出顶栏（1.2s 后自动收起）/ 顶栏的「✕ 退出专注」。
        handle.addEventListener("click", () => {
          if (!_zenActive()) return;
          _zenOpenBar(true);   // 显式点开 → 常驻，不自动收起
        });
      }
      // 鼠标移到屏幕最上方 → 滑出工具条（停手 2.6s 自动收起）
      // ⚠️ 这是**唯一**能自动唤出工具条的鼠标手势（滚轮一律不唤出，见下）
      document.addEventListener("mousemove", (e) => {
        if (!_zenActive()) return;
        if (typeof e.clientY === "number") _zenPointerY = e.clientY;
        if (!_zenPointerAtTop(e)) return;
        // ⚠️ 指针**已经在工具条上**时不要重置定时器 ——
        //    否则光标停在条上抖 1px 就会重挂 2.6s 定时器，
        //    2.6s 后工具条在光标底下自己关掉（mouseover 只在"进入"时触发，补不回来）。
        const onBar = e.target && e.target.closest && e.target.closest(".reader-topbar");
        if (onBar) return;
        _zenOpenBar(false);
      });
      // 指针停在工具条上时不自动收起；离开后再开始计时
      document.addEventListener("mouseover", (e) => {
        if (!_zenActive()) return;
        const bar = e.target && e.target.closest && e.target.closest(".reader-topbar");
        if (bar) { clearTimeout(_zenBarTimer); _zenBarTimer = null; }
      });
      document.addEventListener("mouseout", (e) => {
        if (!_zenActive()) return;
        const bar = e.target && e.target.closest && e.target.closest(".reader-topbar");
        if (bar && !(e.relatedTarget && bar.contains(e.relatedTarget))) {
          clearTimeout(_zenBarTimer);
          _zenBarTimer = setTimeout(_zenCloseBar, 1200);
        }
      });
      // ⚠️ 滚轮**不**唤出工具条（用户 2026-11-14 明确要求）。
      //    原来这里是「内容区往回上滑 / 滑到最底部 → 滑出工具条」：
      //    读书往回看一行，工具条就跳出来打断阅读。
      //    现在工具条只认鼠标动作：指针到屏幕最上方（_zenPointerAtTop）
      //    或点顶部那条细把手。
      //    这里**保留一个空监听**是刻意的 —— 让「滚轮不唤出」这条规则
      //    有代码锚点：真机自检 / 变异自检都盯着它，往里塞 _zenOpenBar 就会被抓到。
      document.addEventListener("scroll", () => {
        /* 刻意留空：滚轮滚动不唤出工具条 */
      }, true);
      // 快捷键：Z 切换专注，Esc 先收工具条、再按退出专注
      document.addEventListener("keydown", (e) => {
        if (!_zenActive()) return;
        const t = e.target;
        const tag = ((t && t.tagName) || "").toLowerCase();
        if (tag === "input" || tag === "textarea" || tag === "select" ||
            (t && t.isContentEditable)) return;
        if (e.key === "Escape") {
          // ⚠️ 上层弹窗（论文快照 / 诊断）开着时，Esc **归它** ——
          //    一次按键不能干两件事（退出专注 + 关弹窗）。
          //    那些弹窗自己的 keydown 处理器没有 stopPropagation，
          //    两边都会响应 → 弹窗关了，专注也退出了。
          if (_zenOverlayOpen()) return;
          const modal = _zenModal();
          if (modal && modal.classList.contains("zen-bar-open")) _zenCloseBar();
          else exitZenMode();
          e.preventDefault();
          return;
        }
        if ((e.key === "z" || e.key === "Z") && !e.ctrlKey && !e.metaKey && !e.altKey) {
          toggleZenMode();
          e.preventDefault();
        }
      });
    }

    // PDF 栏宽变了 -> 按新的栏宽重新渲染 PDF。
    // PDF 宽度是按左栏可用宽度自适应的（100% = 占满整栏），所以栏宽一变
    // 就得重绘，否则页面宽度和栏宽对不上，两侧又会空出来。
    //
    // ⚠️ 只听 window resize 是不够的（浏览器实测抓到的）：
    //    阅读器打开前是 display:none，#pdf-pages-list 的宽度是 0；
    //    第一次渲染时量到 0 → 走兜底 520px；等阅读器真的显示出来，
    //    **窗口尺寸一点没变** → window resize 根本不触发 →
    //    画布永远停在 520px，右半屏全空。
    //    所以要直接观察 PDF 列表容器本身（ResizeObserver），
    //    它在「隐藏 → 显示」「单栏 → 双栏」这些变化上都会报。
    let _pdfResizeTimer = null;
    // ⚠️ 必须是**计数**，不能是布尔 —— loadPdfDualColumn 可以并发（重绘定时器 +
    //    缩放）。用布尔时，先结束的那次渲染会在 finally 里把标记清成 false，
    //    另一场还在跑 → 忙标记提前失效 → 渲染中途的尺寸回调不再被挡。
    let _pdfRelayoutBusyCount = 0;
    function _pdfIsRelayoutBusy() { return _pdfRelayoutBusyCount > 0; }

    function _pdfRelayoutIfNeeded(force) {
      if (_readerMode !== "pdf" || !_pdfState || !_pdfState.pdfDoc) return;
      if (_pdfIsRelayoutBusy()) return;
      const w = document.getElementById("pdf-pages-list")?.clientWidth || 0;
      if (!w) return;               // 阅读器还没显示 / 容器不可见 → 宽度不可信
      const last = _pdfState.lastRenderWidth || 0;
      if (!force && last && Math.abs(w - last) < 40) return; // 变化太小不值得重绘
      clearTimeout(_pdfResizeTimer);
      _pdfResizeTimer = setTimeout(() => {
        if (!_pdfState || !_pdfState.rendering) return;
        const gen = (_pdfState.rendering.generation || 0) + 1;
        _pdfState.rendering.generation = gen;
        _pdfState.rendering.cancelled = true;
        // ⚠ 清空前先落盘（跟 setReaderZoom 同理）：重排时的位置只能从
        //   localStorage 回落到 —— DOM 已经被清空了，活锚点读不到。
        try { _captureReadPos(); } catch (e) { console.warn("[readpos] 重排前保存失败", e); }
        document.getElementById("pdf-pages-list").innerHTML = "";
        loadPdfDualColumn({ keepTranslations: true, generation: gen });
      }, 250);
    }

    window.addEventListener("resize", () => {
      _pdfRelayoutIfNeeded(false);
      // ⚠ 窗口变了 → 「原页图 + 译文」并排放不放得下也变了 → 重算
      //   （放不下就自动从并排退回上下堆叠；放得下就自动并排）。
      try {
        if (_pdfState && _pdfState.bilingual) {
          _applyBilingualOrigLayout(!!_pdfState.bilingualOrig);
        }
      } catch (e) { /* 忽略 */ }
    });

    /**
     * 渲染完成后复核栏宽。
     *
     * 阅读器可能是在**渲染过程中**才从隐藏变可见的 ——
     * 那一瞬间的 ResizeObserver 回调会被「忙」标记挡掉，
     * 不复核的话画布就停在「看不见时量到的兜底宽度 520px」上。
     *
     * 用**渲染起始时**的宽度比，不是最后一页的：
     * 渲染中途变可见时，前几页是旧宽度、后几页是新宽度，
     * 最后一页的宽度会掩盖掉这个事实。
     */
    function _pdfVerifyRenderedWidth(renderW0, listEl) {
      const el = listEl || document.getElementById("pdf-pages-list");
      if (!el) return;
      if (Math.abs(renderW0 - el.clientWidth) > 40) _pdfRelayoutIfNeeded(true);
    }

    /** 观察 PDF 列表容器尺寸 —— 覆盖「阅读器从隐藏变可见」「专注模式切单双栏」
     *  这些 window resize 抓不到的宽度变化。 */
    function _initPdfWidthObserver() {
      const listEl = document.getElementById("pdf-pages-list");
      if (!listEl || typeof ResizeObserver === "undefined") return;
      try {
        const ro = new ResizeObserver(() => _pdfRelayoutIfNeeded(false));
        ro.observe(listEl);
      } catch (e) { /* 观察器不可用就退回 window resize */ }
    }

    /**
     * 载入 PDF 文档，worker 源按「本地 vendor -> CDN」顺序尝试。
     *
     * 本地 worker 随仓库分发（microbench/static/vendor/pdfjs/），正常情况第一档
     * 即成功；只有在 vendor 缺失（全新克隆未执行 vendor 脚本）时才会落到 CDN。
     * 不做预探测的原因：某些环境下 HEAD 探测会被浏览器标记为 ERR_ABORTED 而
     * 产生假阴性，把可用的本地 worker 误判成不可用，反而退回到被墙的 CDN。
     */
    async function loadPdfDocument(pdfUrl) {
      const LOCAL_WORKER = "/static/vendor/pdfjs/pdf.worker.min.js";
      const CDN_WORKER = "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js";
      const candidates = [LOCAL_WORKER, CDN_WORKER];
      let firstErr = null;
      for (const workerSrc of candidates) {
        try {
          pdfjsLib.GlobalWorkerOptions.workerSrc = workerSrc;
          return await pdfjsLib.getDocument({ url: pdfUrl }).promise;
        } catch (e) {
          // 首个错误最有诊断价值（本地路径优先），保留它用于最终提示
          if (!firstErr) firstErr = e;
        }
      }
      throw firstErr;
    }

    /**
     * 这个 PDF 是不是「中译替换版」？
     *
     * ⚠ 判定**跟着文件走**（文件名以 _中译替换版.pdf 结尾），不靠调用方传参：
     *   缩放 / 窗口变化会再次调用 loadPdfDualColumn（重排），
     *   若只靠传参，重排后这个标志会丢 → 右栏又冒出来、又开始翻译（白花钱）。
     *
     * 抽成独立函数是为了**能被护栏直接跑**（node 切片跑真实源码），
     * 免得这条判定只能靠「读源码文本」来守。
     */
    function _isReplacedPdfPath(p) {
      return /_中译替换版\.pdf$/i.test(p || "");
    }

    async function loadPdfDualColumn(opts = {}) {
      const { keepTranslations = false, generation = null, restoreSnapshot = null } = opts;
      // 缩放重绘 / 重排之前，先把「当前看到哪儿」抓下来 ——
      // 下面马上要把两栏的 DOM 全清空，位置不抓就没了。
      // ⚠ 只在 keepTranslations（同一篇的重绘）时用；换论文时这两栏可能
      //   还留着上一篇的页块，拿来当目标就串篇了。
      const _liveLeft = keepTranslations ? _readPosAnchor(_READPOS_PANES.pdf) : null;
      const _liveRight = keepTranslations ? _readPosAnchor(_READPOS_PANES.trans) : null;
      // 「中译替换版」：译文已经打进 PDF 里了 → 不翻第二遍、右栏收起。
      // ⚠ 判定**跟着文件走**（文件名以 _中译替换版.pdf 结尾）：
      //   缩放/窗口变化会再次调用本函数（重排），
      //   若只靠调用方传参，重排后这个标志会丢 → 右栏又冒出来、又开始翻译。
      const replaced = (opts.replaced !== undefined)
        ? !!opts.replaced
        : _isReplacedPdfPath(_readerState.pdfPath);
      _pdfReplacedView = replaced;
      _applyReplacedPdfView(replaced);
      const title = _readerState.title || "";
      const placeholder = document.getElementById("pdf-viewer-placeholder");
      const listEl = document.getElementById("pdf-pages-list");
      const transList = document.getElementById("pdf-translations-list");

      // Try to find local PDF by cached path or title
      let found = null;
      if (_readerState.pdfPath) {
        found = {
          found: true,
          relative_path: _readerState.pdfPath,
          file_name: _readerState.pdfPath.split("/").pop().split("\\").pop(),
          title: title,
        };
      } else {
        // ⚠ 2026-11-17 全面审查 S1：后端 `title` 是 `min_length=1`，
        //   空标题会拿到 422，而下面的 catch 把它吞成「没找到」——
        //   用户看到「未在 vault 中找到匹配的本地 PDF」，排查方向完全错。
        //   → 标题为空就别问了，直接按「没找到」处理。
        const titleQ = String(title || "").trim();
        if (!titleQ) {
          found = { found: false };
        } else {
          try {
            const r = await fetch(`/api/paper/find_pdf?title=${encodeURIComponent(titleQ)}`);
            found = await r.json();
            if (found && found.found) {
              _readerState.pdfPath = found.relative_path;
            }
          } catch (e) {
            found = { found: false };
          }
        }
      }

      if (!found || !found.found) {
        placeholder.style.display = "";
        listEl.innerHTML = "";
        transList.innerHTML = `<div style="padding:24px; color:var(--text-muted); text-align:center;">
          <div style="font-size:36px;">🔍</div>
          <div style="margin-top:12px; font-weight:500;">未在 vault 中找到匹配的本地 PDF。</div>
          <div style="margin-top:6px; font-size:12px; color:var(--text-subtle);">
            标题: ${escapeHtml(title)}<br>
            您可点击下方按钮直接选择电脑上的 PDF 文件进行关联解析，或使用 📥 拉取全文 下载。
          </div>
          <div style="margin-top:16px;">
            <button class="btn-pill-outline" onclick="document.getElementById('reader-pdf-upload-input').click()">
              <span>📤 选择本地 PDF 关联解析</span>
            </button>
          </div>
        </div>`;
        return;
      }

      // 「选中一个标题 → 让 AI 总结这一小节」：后台先把**小节大纲**拉下来。
      // （服务端算，约 2s/8 页；不阻塞阅读，也不阻塞 PDF 渲染。）
      try { fetchQaOutline(found.relative_path); } catch (e) { /* 拉不到就当没有 */ }

      // Load PDF.js document
      placeholder.style.display = "none";
      listEl.innerHTML = `<div class="pdf-translation-loading">📥 正在加载 PDF (${escapeHtml(found.file_name)})…</div>`;
      // Preserve existing translations when only zoom changed; the LLM cache
      // is keyed by (paper_id, page), independent of zoom level.
      if (!keepTranslations) {
        transList.innerHTML = `<div class="pdf-translation-loading">📥 加载中…</div>`;
      }

      try {
        const pdfjsLib = await loadPdfJs();
        const pdfUrl = `/api/paper/serve_pdf?relative_path=${encodeURIComponent(found.relative_path)}`;

        // Reuse existing pdfDoc on zoom to avoid re-downloading / re-parsing
        let pdfDoc;
        if (keepTranslations && _pdfState && _pdfState.pdfDoc) {
          pdfDoc = _pdfState.pdfDoc;
        } else {
          if (_pdfState && _pdfState.pdfDoc && typeof _pdfState.pdfDoc.destroy === "function") {
            try { await _pdfState.pdfDoc.destroy(); } catch (e) {}
          }
          pdfDoc = await loadPdfDocument(pdfUrl);
        }

        // Reset cancelled flag for new render loop. Each loadPdfDualColumn
        // claims a generation number; if a newer toggle supersedes us mid-
        // render, the stale loop bails out via the generation guard below.
        const myGen = (generation !== null)
          ? generation
          : ((_pdfState && _pdfState.rendering && _pdfState.rendering.generation) || 0) + 1;

        // Build a stable, short paperId. The naive `relative_path.replace(/[^a-zA-Z0-9]/g, "_")`
        // keeps full path length (Chinese chars become underscores but not shorter), and Pydantic
        // rejects paper_id > 100 chars. Use basename + 8-char djb2 hash of full path.
        const _paperPath = found.relative_path;
        const _paperBase = _paperPath.split("/").pop().replace(/\.pdf$/i, "").slice(0, 70) || "paper";
        let _paperHash = 5381;
        for (let _i = 0; _i < _paperPath.length; _i++) {
          _paperHash = ((_paperHash << 5) + _paperHash + _paperPath.charCodeAt(_i)) | 0;
        }
        const paperId = `${_paperBase}_${(_paperHash >>> 0).toString(36)}`;
        // Keep the per-page translation state across a zoom re-render:
        // _pdfState is rebuilt from scratch below, so without this the whole
        // translation column would be lost (and every page re-queued for
        // translation) on every zoom click.
        const prevPageState = new Map();
        if (keepTranslations && _pdfState && _pdfState.pages) {
          for (const prev of _pdfState.pages) {
            if (prev && prev.translated && prev.translation) {
              prevPageState.set(prev.num, {
                translation: prev.translation,
                translation_model: prev.translation_model || "",
                translated_at: prev.translated_at || "",
                // 对照模式的段落数据也要带着走 —— 否则缩放一次，
                // 整篇的段落译文就得重翻一遍（钱和命都是）。
                blocks: prev.blocks || null,
                blocksVersion: prev.blocksVersion || 0,
                blockTranslations: prev.blockTranslations || null,
                blockTranslated: !!prev.blockTranslated,
                snapshotBlocks: prev.snapshotBlocks || null,
                snapshotBlocksVersion: prev.snapshotBlocksVersion,
              });
            }
          }
        }
        _pdfState = {
          paperId,
          paperTitle: found.title || title || "",
          relativePath: found.relative_path,
          pdfDoc,
          pages: [],
          // 对照模式（译文按原文排版）—— 默认关，用户点工具栏的「对照」才开
          bilingual: !!_bilingualPref,
          // 对照模式里同时显示原页图（默认关）
          bilingualOrig: !!_bilingualOrigPref,
          rendering: { cancelled: false, generation: myGen },
        };
        // ---- 阅读进度续读：算出这次要回到哪儿 ----
        // 同一篇的重绘（缩放 / 重排）→ 用刚才从活 DOM 抓的位置；
        // 重新打开一篇 → 用本机记住的位置（localStorage）。
        beginReadPosSession(_readPosTargetFor(_liveLeft, _liveRight, found.relative_path));
        _initReadPosTracking();
        // 打开这篇论文的历史批注（高亮 + 我的笔记），下次进来直接可见
        loadAnnotationsForPaper(paperId);
        // 工具栏的「🖼 原页图」按钮要跟当前状态一致（偏好是跨会话记的）
        _syncBilingualOrigButton();
        // 这里原来挂了一个每秒轮询 `getComputedStyle(#reader-modal)` 的
        // interval，只为发现"弹窗被关掉了"。但全项目只有 closeReaderModal()
        // 会把弹窗藏起来，而它现在已经直接置 rendering.cancelled = true
        // → 轮询纯属多余（还会在 PDF 打开期间每秒强制一次样式重算）。删掉。

        // Render all pages
        listEl.innerHTML = "";
        // 渲染开始时的栏宽（渲染结束后用它复核栏宽有没有中途变过）
        const renderW0 = listEl.clientWidth;
        // 重绘进行中 → 忽略容器尺寸变化（否则清空列表触发的 ResizeObserver
        // 回调会再排一次重绘 → 自激循环）
        _pdfRelayoutBusyCount++;
        try {
          for (let p = 1; p <= pdfDoc.numPages; p++) {
            if (_pdfState.rendering.cancelled) break;
            // Generation guard: bail if a newer zoom toggle superseded us
            if (_pdfState.rendering.generation !== myGen) break;
            const pageData = await renderPdfPage(p);
            // Re-attach the translation we already paid for (zoom only).
            const prev = prevPageState.get(p);
            if (prev) {
              pageData.translated = true;
              pageData.translation = prev.translation;
              pageData.translation_model = prev.translation_model;
              pageData.translated_at = prev.translated_at;
              pageData.blocks = prev.blocks;
              pageData.blocksVersion = prev.blocksVersion;
              pageData.blockTranslations = prev.blockTranslations;
              pageData.blockTranslated = prev.blockTranslated;
              pageData.snapshotBlocks = prev.snapshotBlocks;
              pageData.snapshotBlocksVersion = prev.snapshotBlocksVersion;
            }
            _pdfState.pages.push(pageData);
            // 目标页已经渲染出来了 → 尽早回位（长论文不用干等全部渲染完）
            if (_readPosCurrent && !_readPosRestored
                && !_readPosUserMoved.pdf && !_readPosUserMoved.trans) {
              _applyReadPos(_readPosCurrent);
            }
          }
        } finally {
          _pdfRelayoutBusyCount--;
        }
        // 全部页都渲染完了 → 精确回到上次的位置（这次一定落得准）
        if (_readPosCurrent) {
          const _rpRes = _applyReadPos(_readPosCurrent);
          if (_rpRes.leftOk || _rpRes.rightOk) {
            markReadPosRestored(true);
            showToast("📖 已回到上次读到的位置（第 " + _rpRes.page + " 页）", false, 3600);
          }
        }
        // 批注徽标一次性刷新（原来在 renderPdfPage 里逐页刷 → O(页数²)）
        refreshPdfPageAnnoBadges();
        // Start smooth queue (page 1 translates immediately, pages 2..N translate paced at 3.2s)
        // Skip if we're a stale generation — otherwise we'd queue unneeded translates.
        if (_pdfState.rendering.generation === myGen) {
          // Snapshot restore: rehydrate already-translated pages so the user
          // doesn't pay LLM cost again. Pages whose hash doesn't match are
          // left as translated=false so they re-queue normally.
          if (restoreSnapshot) {
            try {
              await applySnapshotToState(_pdfState, restoreSnapshot);
            } catch (e) {
              console.error("snapshot 应用失败", e);
            }
          } else if (_pdfState.paperId) {
            // Auto-load: even when the caller didn't pass a snapshot (the
            // normal "open this paper" path), check whether a snapshot was
            // saved by a previous session. The user expects: close MicroBench
            // → reopen tomorrow → resume where they left off without paying
            // LLM cost again. If the snapshot exists, we apply it transparently.
            try {
              const r = await fetch(
                `/api/pdf/snapshot/load?paper_id=${encodeURIComponent(_pdfState.paperId)}`
              );
              if (r.ok) {
                const snap = await r.json();
                if (snap && snap.stale_extractor) {
                  // 旧快照是按老版抽取算法翻的（栏序错、公式是占位符）→
                  // 服务端已整份作废，这里如实告诉用户，别让他以为修复没生效。
                  showToast(
                    "♻️ 旧版译文已失效（抽取算法已升级：栏序/公式重做），正在重新翻译",
                    true,
                    5200
                  );
                } else if (snap && snap.pages && snap.pages.length > 0) {
                  const { matched, stale } = await applySnapshotToState(_pdfState, snap);
                  if (matched > 0) {
                    const staleNote = stale > 0
                      ? ` · 其中 ${stale} 页原文微变(已标 ⚠️,可重译)`
                      : "";
                    showToast(
                      `📚 已自动恢复 ${matched}/${_pdfState.pages.length} 页翻译快照 (免重翻)${staleNote}`,
                      false,
                      4600
                    );
                  }
                }
              } else if (r.status !== 404) {
                // 404 = no snapshot, that's normal first visit. Anything
                // else is a real error worth logging.
                console.warn(`[snapshot auto-load] HTTP ${r.status}`);
              }
            } catch (e) {
              console.error("snapshot auto-load failed", e);
            }
          }
          // ⚠ 「中译替换版」：**不能**再翻一遍
          //   译文已经打进 PDF 的替换页里了，再翻 12 页 = 白花钱。
          if (!_pdfReplacedView) startPdfTranslationQueue();
        }
        // 阅读器可能是在渲染过程中才从隐藏变可见的（那一瞬间的
        // ResizeObserver 回调会被「忙」标记挡掉），
        // 不核对的话画布就停在「看不见时量到的兜底宽度 520px」上。
        _pdfVerifyRenderedWidth(renderW0, listEl);
      } catch (err) {
        listEl.innerHTML = `<div style="padding:18px; color:#B25336;">❌ PDF 加载失败: ${escapeHtml(String(err))}</div>`;
        if (!keepTranslations) transList.innerHTML = "";
      }
    }

    let _pdfQueueTimer = null;
    let _pdfQueuePaused = false;
    // Chain ownership: while a translation chain is running for paperId X,
    // we ignore startPdfTranslationQueue calls for a different paper. Without
    // this guard, switching papers mid-chain would leave an old chain
    // re-priming the new chain's page 1 — "翻译两遍".
    let _pdfChainOwnerPaperId = null;

    // -----------------------------------------------------------------------
    // Snapshot helpers (PDF translation session persistence)
    // -----------------------------------------------------------------------
    // Lets the user skip re-translating pages after a restart, or hop
    // between snapshots without re-paying LLM cost. Snapshots are saved
    // manually via the "💾 保存当前" button in the PDF reader header,
    // listed in the TopNav "📚 论文快照" button, and restored through
    // the snapshot modal.
    // Non-crypto fallback digest (16 hex chars). crypto.subtle only exists
    // in a secure context (https / http://127.0.0.1) — on
    // http://<LAN-IP>:5000 it is undefined, and returning "" made every
    // snapshot save 422 (the backend requires text_hash.min_length = 4).
    // This digest is not cryptographic: it only detects "the text of this
    // page changed since the snapshot was saved".

    // Strip duplicate id'd blocks (pdf-trans-block-N / pdf-page-text-N /
    // pdf-page-tag-N) from the DOM, keeping the first occurrence. A zoom
    // re-render used to append a second block per page, so the whole
    // translation column grew with every zoom click.
    function _dedupePdfBlocks() {
      const seen = new Set();
      document.querySelectorAll("[id^='pdf-trans-block-'], [id^='pdf-page-text-'], [id^='pdf-page-tag-']")
        .forEach(el => {
          if (seen.has(el.id)) {
            if (el.parentNode) el.parentNode.removeChild(el);
          } else {
            seen.add(el.id);
          }
        });
    }

    async function applySnapshotToState(pdfState, snapshot) {
      // 1) Mtime check (PDF file on disk vs saved-at-time)
      let curMtime = null;
      try {
        const r = await fetch(`/api/pdf/info?relative_path=${encodeURIComponent(pdfState.relativePath)}`);
        if (r.ok) {
          const info = await r.json();
          curMtime = info.mtime;
        }
      } catch {}
      if (curMtime != null && snapshot.paper_mtime &&
          Math.abs(curMtime - snapshot.paper_mtime) > 1) {
        showToast("⚠️ PDF 自上次保存后已改动 (mtime 不同),部分翻译可能错位,已自动标红", true, 5400);
      }
      // 2) Per-page hash check + DOM rehydrate
      let matched = 0, stale = 0;
      // Pre-cleanup: if a previous zoom toggle left duplicate trans-blocks
      // behind, getElementById would return the wrong one (and miss page
      // 1's id entirely). Strip duplicates by id before we start.
      _dedupePdfBlocks();
      for (const pg of pdfState.pages) {
        const snapPage = (snapshot.pages || []).find(s => s.page_num === pg.num);
        if (!snapPage) continue;
        // 段落译文（对照模式）：先存着，等段落块抽出来再比对版本闸门
        // （_applySnapshotBlocks）。版本对不上 = 段落切分算法变了 →
        // 块编号已错位 → 旧译文一律丢弃，绝不贴到错的段落上。
        if (snapPage.blocks && snapPage.blocks.length) {
          pg.snapshotBlocks = snapPage.blocks;
          pg.snapshotBlocksVersion = snapPage.blocks_version;
        }
        const curHash = await _sha256Hex16(pg.text || "");
        const hashOk = (curHash === snapPage.text_hash);
        // Two-phase resolve for DOM elements: if getElementById misses (e.g.
        // browser hasn't flushed the renderPdfPage appendChild yet), retry
        // once after a tick. This is the fix for "page 1 stays stuck on
        // ⏳ 翻译本页…" when PDF.js render loop runs but DOM isn't ready by
        // the time applySnapshotToState fires.
        let textEl = document.getElementById(`pdf-page-text-${pg.num}`);
        let tagEl = document.getElementById(`pdf-page-tag-${pg.num}`);
        if ((!textEl || !tagEl) && pdfState.rendering && pdfState.rendering.generation) {
          await new Promise(r => setTimeout(r, 80));
          textEl = document.getElementById(`pdf-page-text-${pg.num}`);
          tagEl = document.getElementById(`pdf-page-tag-${pg.num}`);
        }
        // Last-resort: lazy-create the placeholder transBlock if the DOM
        // still has no element for this page (e.g. race between renderPdfPage
        // appendChild and applySnapshotToState fires, or prior zoom toggle
        // nuked the block but kept the pageData around). This guarantees
        // hydrate can always write a translation.
        if ((!textEl || !tagEl)) {
          const transList = document.getElementById("pdf-translations-list");
          if (transList) {
            const block = document.createElement("div");
            block.className = "pdf-translation-block";
            block.id = `pdf-trans-block-${pg.num}`;
            block.innerHTML = `
              <div class="page-num" style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
                <span>第 ${pg.num} 页 / Page ${pg.num}</span>
                <span class="page-num-right" style="display:inline-flex; align-items:center; gap:6px;">
                  <span id="pdf-page-tag-${pg.num}" style="font-size:10px; color:var(--text-subtle); font-family:var(--font-sans);">⏳ 即将翻译</span>
                  <span class="anno-badge-slot"></span>
                  <button class="btn-zoom-toggle" style="font-size:11px; padding:1px 6px;"
                          onclick="retranslatePdfPage(${pg.num})"
                          title="用当前模型重新翻译本页（忽略缓存）">🔄</button>
                </span>
              </div>
              <div class="page-text pdf-translation-loading" id="pdf-page-text-${pg.num}">⏳ 正在翻译本页...</div>
            `;
            transList.appendChild(block);
            textEl = document.getElementById(`pdf-page-text-${pg.num}`);
            tagEl = document.getElementById(`pdf-page-tag-${pg.num}`);
            console.warn(`[snapshot p${pg.num}] DOM 缺失,已 lazy-create placeholder`);
          }
        }
        let hydrated = false;
        if (textEl && tagEl && snapPage.translation) {
          try {
            textEl.className = "page-text";
            textEl.innerHTML = formatMarkdownLite(snapPage.translation);
            // 公式原图：从 PDF 原页按坐标裁出来贴进译文
            renderFormulaImages(textEl, (pdfState && pdfState.relativePath) || "", pg.num);
            // 快照恢复后立刻重放这一页的批注高亮（在 KaTeX 渲染之前）
            applyAnnotationsToPage(pg.num, textEl);
            if (typeof renderMathIn === "function") renderMathIn(textEl);
            const modelTag = snapPage.model ? ` · ${snapPage.model}` : "";
            const staleMark = hashOk ? "" : " ⚠️原文微变";
            tagEl.textContent = `✅ 已恢复 (快照${modelTag})${staleMark}`;
            if (!hashOk) {
              tagEl.title = "原文 hash 与快照不一致(可能是 PDF.js 文本提取波动),翻译可能略有错位,可点「重译本页」";
              pg.stale = true;
              stale++;
            }
            hydrated = true;
          } catch (e) {
            console.error(`[snapshot hydrate p${pg.num}]`, e);
          }
        } else {
          // Diagnostic: surface the reason hydration failed so we can debug
          // from the user's perspective.
          const why = !textEl && !tagEl ? "DOM 双双缺失"
                    : !textEl ? "textEl 缺失"
                    : !tagEl ? "tagEl 缺失"
                    : "snapPage.translation 为空";
          console.warn(`[snapshot p${pg.num}] hydrate 失败:${why}。已标 stale,queue 将自动翻译。`);
        }
        if (hydrated) {
          pg.translated = true;
          pg.translating = false;
          pg.translation = snapPage.translation;
          pg.translation_model = snapPage.model || "";
          pg.translated_at = snapPage.translated_at || "";
          matched++;
        } else {
          pg.stale = true;
          stale++;
          // pg.translated 保持 false,processNextInPdfQueue 会接走这一页
        }
      }
      console.log(`[snapshot] ${matched} hydrated, ${stale} stale/fallback out of ${(snapshot.pages || []).length}`);
      // 对照模式：快照里的段落译文现在才拿得到（上面刚存进 pg.snapshotBlocks），
      // 而对照视图在 renderPdfPage 阶段就已经画过了（那时快照还没到）→
      // 这里补一次：比对版本闸门 → 贴回译文 → 重画对照视图。
      if (_pdfState && _pdfState.bilingual) {
        for (const pg of pdfState.pages) {
          if (!pg.snapshotBlocks) continue;
          _applySnapshotBlocks(pg);
          renderBilingualPage(pg.num);
        }
      }
      // Safety net: kick the queue once more 200ms after hydrate. If hydrate
      // marked translated=true on all pages, this is a no-op (find returns
      // undefined). If any page is still translated=false, it gets picked up.
      setTimeout(() => {
        if (typeof processNextInPdfQueue === "function") {
          processNextInPdfQueue();
        }
      }, 200);
      // Surface the hydrate outcome to the user so they can tell whether
      // page 1 was actually restored from snapshot or routed to auto-retranslate.
      if (typeof showToast === "function") {
        const debounceMs = 600; // small delay so it doesn't collide with the load toast
        setTimeout(() => {
          if (matched > 0) {
            showToast(
              stale > 0
                ? `📚 快照恢复 ${matched} 页 · ${stale} 页需要重新翻译(LLM 启动中)`
                : `📚 快照恢复 ${matched} 页 (全部)`,
              false, 4600
            );
          }
        }, debounceMs);
      }
      // 快照回填 → 译文高度变了 → 续读位置会漂 → 重新贴回去
      _readPosReapply();
      return { matched, stale };
    }

    // Debounced snapshot auto-save: triggered from the success branch of
    // translatePdfPage(). After a 5s idle window we POST to /api/pdf/snapshot/save
    // so the user's progress lands on disk even if they close MicroBench
    // without clicking "💾 保存当前". Keeps the public API surface unchanged.
    let _autoSnapshotTimer = null;
    function _scheduleAutoSnapshot() {
      if (_autoSnapshotTimer) clearTimeout(_autoSnapshotTimer);
      _autoSnapshotTimer = setTimeout(() => {
        _autoSnapshotTimer = null;
        // Skip when nothing is translated yet (savePdfSnapshot would toast
        // an empty state warning every time, which is annoying).
        if (!_pdfState || !_pdfState.pages) return;
        const translatable = _pdfState.pages.filter(p => p.translated && p.translation);
        if (translatable.length === 0) return;
        savePdfSnapshot({ silent: true });
      }, 5000);
    }

    async function savePdfSnapshot(opts = {}) {
      const silent = !!(opts && opts.silent);
      if (!_pdfState || !_pdfState.paperId) {
        if (!silent) showToast("请先打开一篇 PDF 再保存快照", true);
        return null;
      }
      // 「中译替换版」不存快照：译文**已经在 PDF 里**了，
      //   再存一份只会给快照目录添垃圾（还会把 APK 烘焙护栏顶红）。
      if (_pdfReplacedView) {
        if (!silent) showToast("中译替换版不用存快照：译文已经打进 PDF 里了", false, 3600);
        return null;
      }
      const translatable = _pdfState.pages.filter(p => p.translated && p.translation);
      // 每页的段落译文**只算一次**（下面拼 payload 时要用两次：判空 + 取值）
      translatable.forEach((pg) => { pg.__bilingualBlocks = _bilingualBlocksPayload(pg); });
      if (translatable.length === 0) {
        if (!silent) showToast("当前论文还没有翻译完成的页面,先翻几页再保存", true, 3800);
        return null;
      }
      const btn = document.getElementById("pdf-snapshot-save-btn");
      const origText = btn ? btn.innerText : null;
      if (btn && !silent) { btn.disabled = true; btn.innerText = "⏳ 计算 hash..."; }
      // Compute per-page text_hash in parallel — sha256 of small strings
      // is microseconds, but a 30-page paper would still feel slow if serial.
      const pagesPayload = await Promise.all(translatable.map(async (pg) => ({
        page_num: pg.num,
        text_hash: await _sha256Hex16(pg.text || ""),
        translation: pg.translation,
        model: pg.translation_model || "",
        translated_at: pg.translated_at || new Date().toISOString(),
        // 段落级译文（对照模式）。
        // ⚠ 它有**独立版本闸门**（blocks_version）—— 段落切分算法一改，
        //   块编号就变了，旧译文贴上去就是错位 → 前端会比对版本，对不上就丢弃。
        //   整页译文（translation）不受影响，两者的生命周期必须分开。
        ...(pg.__bilingualBlocks
            ? { blocks: pg.__bilingualBlocks, blocks_version: pg.blocksVersion || 0 }
            : {}),
      })));
      // Pull current mtime so load-time check has something to compare
      let paperMtime = 0;
      try {
        const r = await fetch(`/api/pdf/info?relative_path=${encodeURIComponent(_pdfState.relativePath)}`);
        if (r.ok) {
          const info = await r.json();
          paperMtime = info.mtime || 0;
        }
      } catch {}
      if (btn && !silent) btn.innerText = "💾 保存中...";
      try {
        const data = await postJson("/api/pdf/snapshot/save", {
          paper_id: _pdfState.paperId,
          paper_title: _pdfState.paperTitle,
          relative_path: _pdfState.relativePath,
          paper_mtime: paperMtime,
          pages: pagesPayload,
        });
        if (!silent) {
          showToast(
            `💾 已保存 ${pagesPayload.length}/${_pdfState.pages.length} 页快照 (${(data.bytes / 1024).toFixed(1)} KB · ${data.saved_at.slice(11, 19)})`,
            false, 4600
          );
        }
        // (Removed 2026-09-24: was calling _refreshSnapshotBadge — empty stub.)
        return data;
      } catch (err) {
        if (!silent) showToast("保存快照失败: " + err.message, true);
        return null;
      } finally {
        if (btn && !silent) { btn.disabled = false; btn.innerText = origText; }
      }
    }

    /**
     * 这一页的段落译文（存进快照用）。
     * 没有就返回 null（调用方决定要不要带这个字段）。
     *
     * 只存 **i / kind / col / bbox / translation**：
     * 原文（text）平板自己渲染 PDF 就有，存了只是让快照白白变大
     * （快照有 5MB 上限）。
     */
    function _bilingualBlocksPayload(pg) {
      if (!pg || !pg.blocks || !pg.blocks.length) return null;
      const trans = pg.blockTranslations || {};
      const out = [];
      pg.blocks.forEach(b => {
        const t = trans[b.i];
        if (!t) return;
        out.push({
          i: b.i,
          kind: b.kind || "body",
          col: b.col || "S",
          bbox: (b.bbox || []).slice(0, 4),
          translation: t,
        });
      });
      return out.length ? out : null;
    }

    async function listSnapshots() {
      try {
        const r = await fetch("/api/pdf/snapshot/list");
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        return data.snapshots || [];
      } catch (err) {
        console.error("list snapshots failed", err);
        return [];
      }
    }

    async function deleteSnapshot(paperId) {
      if (!confirm(`删除该论文的翻译快照?\n\n${paperId}\n\n(下次打开需要重新翻译)`)) return;
      try {
        const r = await fetch(`/api/pdf/snapshot/delete?paper_id=${encodeURIComponent(paperId)}`, { method: "DELETE" });
        if (!r.ok) throw new Error("HTTP " + r.status);
        showToast("🗑️ 快照已删除", false, 2400);
        await refreshSnapshotModal();
      } catch (err) {
        showToast("删除失败: " + err.message, true);
      }
    }

    async function openFromSnapshot(snap) {
      // 1) wire _readerState so handleFetchFullText path works, but we
      //    don't want to spam LLM calls — just bypass that and inject the
      //    snapshot directly.
      _readerState.pdfPath = snap.relative_path;
      _readerState.title = snap.paper_title || _readerState.title || "";
      // 2) Close any open modal first
      if (document.getElementById("snapshot-modal")) closeSnapshotModal();
      // 3) Open reader + load with restoreSnapshot. switchReaderMode is
      //    called with skipLoad: otherwise it starts its own
      //    loadPdfDualColumn() and the two concurrent loads race on
      //    _pdfState (and re-translate pages the snapshot already holds).
      openReaderModal();
      switchReaderMode("pdf", { skipLoad: true });
      try {
        await loadPdfDualColumn({ restoreSnapshot: snap });
        showToast(
          `📚 已恢复 ${snap.paper_title} · ${snap.pages.length} 页快照已重水化`,
          false, 3600
        );
      } catch (err) {
        showToast("打开快照失败: " + err.message, true);
      }
    }

    async function showSnapshotModal() {
      let modal = document.getElementById("snapshot-modal");
      if (!modal) {
        modal = document.createElement("div");
        modal.id = "snapshot-modal";
        modal.style.cssText = "position:fixed; inset:0; z-index:2000; background:rgba(0,0,0,0.42); display:flex; align-items:center; justify-content:center;";
        modal.innerHTML = `
          <div class="snap-modal-panel" style="background:var(--bg-canvas); border-radius:14px; padding:24px 28px; width:min(640px, 94vw); max-height:80vh; overflow-y:auto; box-shadow:0 18px 48px rgba(0,0,0,0.22); border:1px solid var(--card-border); font-family:var(--font-sans);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;">
              <div>
                <div style="font-family:var(--font-serif); font-size:20px; font-weight:500;">📚 论文翻译快照</div>
                <div style="font-size:12px; color:var(--text-subtle); margin-top:4px;">
                  切换到任何一篇已保存的论文,自动恢复已翻译页面,免重翻。
                </div>
              </div>
              <button type="button" onclick="closeSnapshotModal()" style="background:transparent; border:0; font-size:22px; color:var(--text-muted); cursor:pointer;" title="关闭">×</button>
            </div>
            <div id="snapshot-list" style="display:flex; flex-direction:column; gap:10px;">
              <div style="color:var(--text-subtle); padding:30px 0; text-align:center;">
                <div style="font-size:24px;">⏳</div>
                <div style="margin-top:8px; font-size:13px;">加载快照列表...</div>
              </div>
            </div>
          </div>
        `;
        document.body.appendChild(modal);
      } else {
        modal.style.display = "flex";
      }
      // ESC to close —— 走 _wireModalEsc（旧写法 {once:true} 有坑，见其注释）
      _wireModalEsc(modal, closeSnapshotModal);
      await refreshSnapshotModal();
    }

    async function refreshSnapshotModal() {
      const listEl = document.getElementById("snapshot-list");
      if (!listEl) return;
      const snapshots = await listSnapshots();
      if (snapshots.length === 0) {
        listEl.innerHTML = `
          <div style="text-align:center; padding:36px 16px; color:var(--text-subtle);">
            <div style="font-size:36px; margin-bottom:12px;">📭</div>
            <div style="font-size:14px; color:var(--text-muted);">还没有任何快照</div>
            <div style="font-size:12px; margin-top:6px; line-height:1.6;">
              在 PDF reader 顶栏点击 <strong>「💾 保存当前」</strong> 后,这里会出现保存记录。
            </div>
          </div>
        `;
        return;
      }
      listEl.innerHTML = snapshots.map((s) => {
        const safe = (s) => escapeHtml(String(s));
        // The 📖/🗑️ buttons embed the paper id inside an inline
        // onclick="fn('...')" handler — escapeHtml's &#39; would be
        // resolved back to ' by the HTML parser and break the handler.
        const jsSafe = (s) => escapeJsAttr(String(s));
        const staleHint = s.paper_mtime
          ? `mtime=${new Date(s.paper_mtime * 1000).toLocaleString()}`
          : "";
        const pathHint = s.relative_path ? ` · ${safe(s.relative_path.split("/").slice(-2).join("/"))}` : "";
        const pct = s.total_pages > 0 ? Math.round((s.translated_pages / s.total_pages) * 100) : 0;
        return `
        <div class="snap-card" data-paper-id="${safe(s.paper_id)}">
          <div style="display:flex; align-items:flex-start; gap:14px;">
            <div style="font-size:32px; line-height:1;">📄</div>
            <div style="flex:1; min-width:0;">
              <div style="font-family:var(--font-serif); font-size:15px; font-weight:500; color:var(--text-main); margin-bottom:4px; word-break:break-word;">
                ${safe(s.paper_title || s.paper_id)}
              </div>
              <div style="display:flex; flex-wrap:wrap; gap:6px; align-items:center; font-size:11px; color:var(--text-subtle); margin-bottom:6px;">
                <span style="background:var(--card-bg-subtle); padding:2px 8px; border-radius:10px;">
                  ✓ ${s.translated_pages}/${s.total_pages} 页 (${pct}%)
                </span>
                <span title="${safe(staleHint)}">💾 ${safe((s.saved_at || "").slice(0, 16).replace("T", " "))}</span>
              </div>
              <div style="font-size:11px; color:var(--text-muted); font-family:var(--font-mono); word-break:break-all;">
                ${safe(s.paper_id)}${safe(pathHint)}
              </div>
            </div>
            <div style="display:flex; flex-direction:column; gap:6px; flex-shrink:0;">
              <button class="btn-pill" style="font-size:12px;" onclick="openSnapshotById('${jsSafe(s.paper_id)}')">📖 打开</button>
              <button class="btn-pill-outline" style="font-size:11px;" onclick="deleteSnapshot('${jsSafe(s.paper_id)}')">🗑️ 删除</button>
            </div>
          </div>
        </div>`;
      }).join("");
    }

    async function openSnapshotById(paperId) {
      try {
        const r = await fetch(`/api/pdf/snapshot/load?paper_id=${encodeURIComponent(paperId)}`);
        if (!r.ok) throw new Error("HTTP " + r.status);
        const snap = await r.json();
        await openFromSnapshot(snap);
      } catch (err) {
        showToast("加载快照失败: " + err.message, true);
      }
    }

    // Esc 关闭快照弹窗的监听句柄（跨调用保存，close 时移除 —— 见 closeSnapshotModal）
    let _snapshotEscHandler = null;

    function closeSnapshotModal() {
      const m = document.getElementById("snapshot-modal");
      if (m) m.style.display = "none";
      if (m && m._escDismiss) { m._escDismiss(); m._escDismiss = null; }
    }

    // Expose to window for inline onclick attribute on the TopNav button
    window.savePdfSnapshot = savePdfSnapshot;
    window.listSnapshots = listSnapshots;
    window.deleteSnapshot = deleteSnapshot;
    window.openSnapshotById = openSnapshotById;
    window.showSnapshotModal = showSnapshotModal;
    window.closeSnapshotModal = closeSnapshotModal;
    window.openFromSnapshot = openFromSnapshot;
    window.refreshSnapshotModal = refreshSnapshotModal;

    // -----------------------------------------------------------------------
    // Translation diagnostic ring buffer + in-app panel
    // -----------------------------------------------------------------------
    // Replaces the need for F12 devtools. The webview in desktop.py does
    // NOT expose F12, so the user has no way to inspect pending/failed
    // network requests. This buffer records the last 50 translate_page
    // requests with status / duration / model / error; the "🔧 诊断" pill
    // opens a modal listing them.
    const _DIAG_CAP = 50;
    const _diagLog = [];          // newest first
    function _recordDiag(entry) {
      try {
        _diagLog.unshift(entry);
        if (_diagLog.length > _DIAG_CAP) _diagLog.length = _DIAG_CAP;
        // Keep the badge counter in sync if mounted. The old lookup used
        // "#diag-fab", an id that does not exist in this document, so the
        // counter was silently never updated. The real element is the
        // TopNav "🔧 诊断" pill (#diag-nav-btn).
        const badge = document.getElementById("diag-nav-btn");
        if (badge) {
          const last = _diagLog[0];
          const errCount = _diagLog.filter(e => e.error || !e.ok).length;
          badge.dataset.errCount = String(errCount);
          badge.dataset.lastOk = last && !last.error && last.ok ? "1" : "0";
          const baseTitle = badge.dataset.baseTitle || badge.title || "";
          if (!badge.dataset.baseTitle) badge.dataset.baseTitle = baseTitle;
          badge.title = baseTitle + (errCount > 0 ? ` · ${errCount} 条失败` : "");
        }
      } catch {}
    }
    function _clearDiagLog() {
      _diagLog.length = 0;
      showDiagnoseModal();
    }
    function _diagSummary() {
      const total = _diagLog.length;
      const errs = _diagLog.filter(e => e.error || !e.ok).length;
      const oks = total - errs;
      const durations = _diagLog.map(e => e.duration_ms || 0).filter(x => x > 0);
      const avg = durations.length ? Math.round(durations.reduce((a,b) => a+b, 0) / durations.length) : 0;
      const max = durations.length ? Math.max(...durations) : 0;
      const slow = durations.filter(x => x > 30000).length;
      return { total, errs, oks, avg, max, slow };
    }
    function showDiagnoseModal() {
      let modal = document.getElementById("diag-modal");
      if (!modal) {
        modal = document.createElement("div");
        modal.id = "diag-modal";
        modal.style.cssText = "position:fixed; inset:0; z-index:2000; background:rgba(0,0,0,0.42); display:flex; align-items:center; justify-content:center;";
        modal.innerHTML = `
          <div style="background:var(--bg-canvas); border-radius:14px; padding:22px 26px; width:min(820px, 94vw); max-height:84vh; overflow-y:auto; box-shadow:0 18px 48px rgba(0,0,0,0.22); border:1px solid var(--card-border); font-family:var(--font-sans);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;">
              <div>
                <div style="font-family:var(--font-serif); font-size:20px; font-weight:500;">🔧 翻译诊断面板</div>
                <div style="font-size:12px; color:var(--text-subtle); margin-top:4px;">
                  最近 <span id="diag-cap">50</span> 条 translate_page 请求记录。超时(>210s)、错误、模型、耗时一览。
                </div>
              </div>
              <div style="display:flex; gap:6px;">
                <button class="btn-pill-outline" style="font-size:11px; padding:4px 12px;" onclick="_clearDiagLog()">🧹 清空</button>
                <button type="button" onclick="closeDiagnoseModal()" style="background:transparent; border:0; font-size:22px; color:var(--text-muted); cursor:pointer;" title="关闭">×</button>
              </div>
            </div>
            <div id="diag-summary-bar" style="display:flex; flex-wrap:wrap; gap:12px; padding:10px 12px; background:var(--card-bg-subtle); border-radius:8px; margin-bottom:12px; font-size:12px;"></div>
            <div id="diag-table" style="font-family:var(--font-mono); font-size:11.5px;"></div>
          </div>
        `;
        document.body.appendChild(modal);
      } else {
        modal.style.display = "flex";
      }
      // ESC to close —— 必须放在 if/else **外面**：旧写法挂在 `if (!modal)` 里，
      // 弹窗第一次创建时才挂一次 → 关掉再打开 Esc 永久失效（实测复现）。
      _wireModalEsc(modal, closeDiagnoseModal);
      _renderDiagnoseTable();
    }
    function _renderDiagnoseTable() {
      const summaryEl = document.getElementById("diag-summary-bar");
      const tableEl = document.getElementById("diag-table");
      if (!summaryEl || !tableEl) return;
      const s = _diagSummary();
      summaryEl.innerHTML = `
        <span>📊 共 <strong>${s.total}</strong> 条</span>
        <span style="color:#5a8a3e;">✓ ${s.oks}</span>
        <span style="color:#B25336;">✗ ${s.errs}</span>
        <span>⏱ 平均 <strong>${(s.avg/1000).toFixed(1)}s</strong> · 最长 ${(s.max/1000).toFixed(1)}s</span>
        <span style="color:${s.slow > 0 ? "#B25336" : "var(--text-muted)"};">🐢 慢响应(>30s): ${s.slow}</span>
      `;
      if (_diagLog.length === 0) {
        tableEl.innerHTML = `
          <div style="text-align:center; padding:36px 16px; color:var(--text-subtle);">
            <div style="font-size:32px; margin-bottom:12px;">📭</div>
            <div style="font-size:14px;">还没有翻译请求记录</div>
            <div style="font-size:12px; margin-top:6px;">打开一篇 PDF 后这里的 ring buffer 会被填充。</div>
          </div>
        `;
        return;
      }
      tableEl.innerHTML = `
        <div style="display:grid; grid-template-columns: 60px 50px 90px 70px 110px 60px 1fr; gap:6px 12px; padding:8px 10px; background:var(--card-bg); border-radius:6px; font-weight:500; color:var(--text-muted); text-transform:uppercase; font-size:10.5px;">
          <div>Page</div><div>Status</div><div>Duration</div><div>Model</div><div>Chars in/out</div><div>Time</div><div>Error</div>
        </div>
        ${_diagLog.map(e => {
          const ok = e.ok && !e.error;
          const color = ok ? "#5a8a3e" : "#B25336";
          const icon = ok ? "✓" : "✗";
          const dur = (e.duration_ms / 1000).toFixed(1) + "s";
          // 判断顺序必须从**大**到小：原来先判 >30000 再判 >180000，
          // 后者永远已经被前者吞掉 → 超慢请求从不变最深的红色。
          const durColor = e.duration_ms > 180000 ? "#7a1a1a"
            : (e.duration_ms > 30000 ? "#B25336" : "var(--text-main)");
          const time = (e.ts || "").slice(11, 19);
          return `
            <div style="display:grid; grid-template-columns: 60px 50px 90px 70px 110px 60px 1fr; gap:6px 12px; padding:8px 10px; border-bottom:1px solid var(--card-border-subtle); align-items:center;">
              <div style="color:${color}; font-weight:500;">${icon} p${e.page}</div>
              <div style="color:${color};">${escapeHtml(e.status || '—')}</div>
              <div style="color:${durColor};">${dur}</div>
              <div>${escapeHtml(e.model || '—')}</div>
              <div>${e.chars_in}→${e.chars_out}</div>
              <div style="color:var(--text-subtle);">${time}</div>
              <div style="color:${color}; word-break:break-word; font-family:var(--font-mono); font-size:10.5px;">${escapeHtml(e.error || '')}</div>
            </div>`;
        }).join('')}
      `;
    }
    // Esc 关闭诊断弹窗的监听句柄（见 closeDiagnoseModal）
    let _diagEscHandler = null;

    function closeDiagnoseModal() {
      const m = document.getElementById("diag-modal");
      if (m) m.style.display = "none";
      if (m && m._escDismiss) { m._escDismiss(); m._escDismiss = null; }
    }
    window.showDiagnoseModal = showDiagnoseModal;
    window.closeDiagnoseModal = closeDiagnoseModal;
    window._diagLog = _diagLog;
    window._recordDiag = _recordDiag;

    function togglePdfQueuePause() {
      _pdfQueuePaused = !_pdfQueuePaused;
      const btn = document.getElementById("pdf-queue-pause-btn");
      if (btn) {
        btn.textContent = _pdfQueuePaused ? "▶️ 继续排队" : "⏸️ 暂停排队";
        btn.style.color = _pdfQueuePaused ? "var(--accent-terracotta)" : "var(--text-muted)";
      }
      if (!_pdfQueuePaused) {
        showToast("已继续 PDF 队列翻译");
        processNextInPdfQueue();
      } else {
        // ⚠ 2026-11-25 审计 P1：原来暂停**只翻标志位**，已经排好的
        //   setTimeout 还在 → 暂停后那一拍仍会触发一次 processNextInPdfQueue
        //   （进去再判 _pdfQueuePaused 才停）→ 用户看到「暂停了还在翻页」。
        // → 暂停时直接把定时器掐掉。
        if (_pdfQueueTimer) { clearTimeout(_pdfQueueTimer); _pdfQueueTimer = null; }
        showToast("已暂停后续页面队列翻译");
      }
    }

    async function startPdfTranslationQueue() {
      // Always claim ownership of the current paperId. The previous
      // paper's chain (if any) parked in setTimeout 3200ms will stop
      // itself on its next tick because its `_pdfState.rendering.cancelled`
      // is true AND its old paperId no longer matches the owner. We do
      // NOT need to early-return here — claim regardless.
      _pdfChainOwnerPaperId = _pdfState.paperId;
      _pdfQueuePaused = false;
      const btn = document.getElementById("pdf-queue-pause-btn");
      if (btn) {
        btn.textContent = "⏸️ 暂停排队";
        btn.style.color = "var(--text-muted)";
      }
      processNextInPdfQueue();
    }

    async function processNextInPdfQueue() {
      if (!_pdfState || !_pdfState.pages || _pdfState.rendering.cancelled || _pdfQueuePaused) return;
      // 「中译替换版」：译文已在 PDF 里 → 不排队翻译（省钱）
      if (_pdfReplacedView) return;
      // Bail if a newer paper started its own chain while we were waiting
      if (_pdfChainOwnerPaperId !== _pdfState.paperId) return;

      // 对照模式 → 找「还没做**段落**翻译」的页；否则找「还没做整页翻译」的页。
      // ⚠ 两个标志不能混用：段落翻译成功后也会把 translated 置 true
      //   （快照要收它），所以对照模式下必须按 blockTranslated 找。
      // ⚠ blockFailed 的页要**跳过**：翻失败过一次就别再无限重试
      //   （但它仍然算「没翻好」—— 统计口径看 blockTranslated）。
      const nextPage = _pdfState.bilingual
        ? _pdfState.pages.find(p => !p.blockTranslated && !p.translating && !p.blockFailed)
        : _pdfState.pages.find(p => !p.translated && !p.translating);
      if (!nextPage) return;

      // Ownership of `translating` flag lives in translatePdfPage (it sets
      // it on entry and clears it on success/error). Setting it here too
      // would race the dedupe check inside translatePdfPage and cause the
      // inner call to return immediately, freezing page 1 forever with
      // its initial "⏳ 正在翻译本页…" HTML never replaced.
      if (_pdfState.bilingual) {
        await translateBlocksForPage(nextPage.num, false);
      } else {
        await translatePdfPage(nextPage.num, nextPage.text, false);
      }

      if (!_pdfState || _pdfState.rendering.cancelled || _pdfQueuePaused) return;
      // After awaiting, the user may have switched papers. Verify ownership
      // before scheduling the next tick.
      if (_pdfChainOwnerPaperId !== _pdfState.paperId) return;

      _pdfQueueTimer = setTimeout(() => {
        processNextInPdfQueue();
      }, 3200);
    }

    // =====================================================================
    // 画布位图的「按需渲染 + 远端释放」（2026-11-25 审计 P1）
    //
    // 为什么：整页位图 = 2200×2800×4B ≈ **24MB/页**（DPR=2），
    //   36 页论文 ≈ **880MB** → 长论文直接把标签页撑爆（白屏/OOM）。
    //   以前还有两个尾巴问题：没有 RenderTask.cancel()、关阅读器不释放。
    //
    // 怎么做：位图**只在靠近视口时**存在。
    //   · 靠近视口 → 画（IntersectionObserver 判定）
    //   · 滚远了   → 把 canvas.width/height 归零（浏览器立刻回收显存）
    //   · 滚回来   → 重画一次
    //   DOM 结构 / 文本层 / 译文 / 批注高亮**全程保留** → 划词、批注、
    //   对照视图、续读回位都不受影响。

    async function renderPdfPage(pageNum) {
      const listEl = document.getElementById("pdf-pages-list");
      const page = await _pdfState.pdfDoc.getPage(pageNum);
      const viewport0 = page.getViewport({ scale: 1.0 });
      // 按左栏「实际可用宽度」自适应：_pdfZoom = 1.0 表示正好占满栏宽。
      // 原先固定 baseWidth=520px，栏宽一变两侧就空出一大片。
      const availWidth = Math.max(260, (listEl.clientWidth || 520) - 4);
      // 「中译替换版」独占整栏 → 直接铺满 1500+px 会让整页大到离谱
      // （一屏只看得到半页版面）。给个阅读上限：整页最多 1100px 宽，
      // 页块是 align-items:center（CSS）→ 会自动居中，两侧留白。
      const renderWidth = _pdfReplacedView ? Math.min(availWidth, 1100) : availWidth;
      const fitScale = renderWidth / viewport0.width;
      const scale = fitScale * (_pdfZoom || 1.0);
      const viewport = page.getViewport({ scale });
      if (_pdfState) _pdfState.lastRenderWidth = availWidth;
      const wrapper = document.createElement("div");
      wrapper.className = "pdf-page-block";
      wrapper.id = `pdf-page-block-${pageNum}`;
      const label = document.createElement("div");
      label.className = "pdf-page-label";
      label.textContent = `PAGE ${pageNum}`;
      wrapper.appendChild(label);

      // Wrapper for canvas + textLayer (allows direct text selection on PDF)
      const canvasWrapper = document.createElement("div");
      canvasWrapper.className = "pdf-page-canvas-wrapper";
      canvasWrapper.style.width = `${Math.floor(viewport.width)}px`;
      canvasWrapper.style.height = `${Math.floor(viewport.height)}px`;

      // HiDPI: 画布按 devicePixelRatio 放大渲染，再用 CSS 尺寸缩回，
      // 否则大尺寸下文字会发虚。
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const renderViewport = page.getViewport({ scale: scale * dpr });

      const canvas = document.createElement("canvas");
      canvas.className = "pdf-page-canvas";
      // ⚠⚠ 位图**按需分配**：先**不分配**显存（width/height 归零），
      //   需要画的时候由 `_pdfPaintCanvas` 按 renderViewport 还原。
      //   为什么不能一上来就按尺寸分配：
      //   → `<canvas width=733 height=948>` 一创建就**占掉 2.7MB** backing store
      //     （DPR=2 时 11MB），哪怕一个像素都没画 ——
      //     36 页 = 100~400MB 照样把标签页撑爆。
      //   → 「不画」必须等于「不占显存」。
      //   ⚠ 版面尺寸不受影响：CSS 尺寸走 style.width/height（下面的两行），
      //     与位图尺寸无关 → 页块高度、滚动位置、续读回位全部照旧。
      canvas.width = 0;
      canvas.height = 0;
      canvas.style.width = `${Math.floor(viewport.width)}px`;
      canvas.style.height = `${Math.floor(viewport.height)}px`;
      canvasWrapper.appendChild(canvas);

      const textLayerDiv = document.createElement("div");
      textLayerDiv.className = "textLayer";
      textLayerDiv.style.width = `${Math.floor(viewport.width)}px`;
      textLayerDiv.style.height = `${Math.floor(viewport.height)}px`;
      textLayerDiv.style.setProperty("--scale-factor", scale);
      canvasWrapper.appendChild(textLayerDiv);

      wrapper.appendChild(canvasWrapper);

      // 对照模式（译文按原文排版）的容器。
      // 默认藏起来；开了对照模式才显示（此时 canvasWrapper 也藏起来）。
      const biHost = document.createElement("div");
      biHost.className = "bilingual-page";
      biHost.id = `bilingual-page-${pageNum}`;
      biHost.style.display = "none";
      wrapper.appendChild(biHost);

      listEl.appendChild(wrapper);

      // ⚠⚠ 2026-11-25 审计 P1：**画布位图按需渲染**。
      //   原来每页都直接画一张 DPR=2 的整页位图：
      //   1100px 宽的页 × 2 倍设备像素 ≈ 2200×2800×4B ≈ **24MB/页**
      //   → 36 页论文 ≈ **880MB** → 长论文直接把标签页撑爆（OOM/白屏）。
      //   → 现在只有**靠近视口**的页才真的画；滚远了把位图**释放**
      //     （DOM 结构、文本层、译文、批注坐标全都留着），
      //     滚回来再画一次。
      wrapper._pdfRender = {
        pageNum, pdfPage: page, scale, dpr,
        canvas, canvasWrapper, renderViewport,
      };
      wrapper._pdfPainted = false;
      _pdfObserveCanvas(wrapper);
      if (_pdfPageNearViewport(wrapper)) {
        await _pdfPaintCanvas(wrapper);
      }

      // Extract text via PDF.js (fast + no server roundtrip)
      const tc = await page.getTextContent();
      const items = tc.items.map(it => it.str).join(" ").trim();

      // Render Text Layer for drag-selection on the PDF
      if (typeof pdfjsLib !== "undefined" && pdfjsLib.renderTextLayer) {
        try {
          pdfjsLib.renderTextLayer({
            textContentSource: tc,
            textContent: tc,
            container: textLayerDiv,
            viewport: viewport,
            textDivs: [],
          });
        } catch (tle) {
          renderTextLayerFallback(tc, textLayerDiv, viewport);
        }
      } else {
        renderTextLayerFallback(tc, textLayerDiv, viewport);
      }

      // 原文侧已有的批注高亮（缩放重绘后按比例还原）
      // 注意：**不要**在这里调 refreshPdfPageAnnoBadges() —— 它会
      // querySelectorAll 全部页块 × 每条批注，在 20 页的循环里被调 20 次
      // 就是 O(N²)。徽标统一在循环结束后刷一次。
      drawAnnoOverlays(pageNum, canvasWrapper);

      // 对照模式开着 → 这一页也要画对照视图（段落块是异步抽的，不挡渲染）
      if (_pdfState && _pdfState.bilingual) {
        // 「同时显示原页图」开着 → canvas 留着（左右并排 / 上下堆叠）
        const showOrig = !!_pdfState.bilingualOrig;
        canvasWrapper.style.display = showOrig ? "" : "none";
        biHost.style.display = "";
        // ⚠ 宽度判断必须**所有页卡统一**（原页图刚渲染完，宽度才准）——
        //   逐页各判各的会让同一篇里有的页并排、有的页堆叠。
        _applyBilingualOrigLayout(showOrig);
        ensurePageBlocks(pageNum).then(() => renderBilingualPage(pageNum));
      }

      // Add placeholder translation block. A zoom re-render walks every
      // page again, so this append has to be idempotent: if a block for
      // this page already exists (it holds the translation), reuse it
      // instead of appending a second copy of the whole column.
      const transList = document.getElementById("pdf-translations-list");
      let transBlock = document.getElementById(`pdf-trans-block-${pageNum}`);
      if (!transBlock) {
        transBlock = document.createElement("div");
        transBlock.className = "pdf-translation-block";
        transBlock.id = `pdf-trans-block-${pageNum}`;
        transBlock.innerHTML = `
        <div class="page-num" style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <span>第 ${pageNum} 页 / Page ${pageNum}</span>
          <span class="page-num-right" style="display:inline-flex; align-items:center; gap:6px;">
            <span id="pdf-page-tag-${pageNum}" style="font-size:10px; color:var(--text-subtle); font-family:var(--font-sans);">
              ${pageNum === 1 ? "⏳ 即将翻译" : `🕒 排队中 (约 ${(pageNum - 1) * _getCurrentRpmInfo().interval}s)`}
            </span>
            <span class="anno-badge-slot"></span>
            <button class="btn-zoom-toggle" style="font-size:11px; padding:1px 6px;"
                    onclick="retranslatePdfPage(${pageNum})"
                    title="用当前模型重新翻译本页（忽略缓存）">🔄</button>
          </span>
        </div>
        <div class="page-text pdf-translation-loading" id="pdf-page-text-${pageNum}">
          ${pageNum === 1 ? "⏳ 正在翻译本页..." : `<span>🕒 等待排队翻译中 (受 ${_getCurrentRpmInfo().rpm} RPM 保护, 间隔 ${_getCurrentRpmInfo().interval}s)...</span> <button class="btn-pill-outline" style="font-size:10.5px; padding:2px 8px; margin-left:6px; vertical-align:middle;" onclick="translatePdfPage(${pageNum}, null, true)">优先翻译此页</button>`}
        </div>
      `;
        transList.appendChild(transBlock);
      }

      // ⚠ ptW/ptH：页面**物理尺寸**（pt，1/72 英寸）。
      //   划选时记下的高亮矩形是**归一化**的（0~1），要换算成页面坐标
      //   才能和服务端 page_blocks 的 bbox（页面绝对 pt）对上
      //   → 才能判断「选中的这句话落在哪个段落块、哪个小节」。
      return { num: pageNum, canvas, text: items, translated: false, translating: false,
               ptW: viewport0.width, ptH: viewport0.height };
    }

    function renderTextLayerFallback(tc, container, viewport) {
      if (!container || !tc || !tc.items) return;
      container.innerHTML = "";
      for (const item of tc.items) {
        if (!item.str || !item.str.trim()) continue;
        const span = document.createElement("span");
        span.textContent = item.str;
        if (pdfjsLib.Util && pdfjsLib.Util.transform && item.transform) {
          try {
            const tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
            span.style.left = `${tx[4]}px`;
            span.style.top = `${tx[5] - (item.height * viewport.scale)}px`;
          } catch (e) {}
        }
        span.style.fontSize = `${Math.max(8, item.height * viewport.scale)}px`;
        container.appendChild(span);
      }
    }

    // ---------------------------------------------------------------
    // 当前 RPM 保护信息 (按**当前选中的模型**动态返回)
    // ---------------------------------------------------------------
    // 为什么要按"选中的模型"算：换了模型，请求就发到那个模型的服务器上，
    // 间隔必须跟着那个模型的限流走。原来只有 primary/agnes 两档，
    // 自定义模型一律按主模型的 30 RPM / 2 秒算 —— 对方的限流更紧时会被打爆。
    function _getCurrentRpmInfo() {
      const sel = (_pdfTransModel || "");
      const status = _llmStatus;
      const isAgnes = !status || status.is_degraded;
      const rl = status && status.rate_limit;

      // 自定义模型（用户自己的 API 模型）：用服务端下发的它自己的档
      if (sel && sel.indexOf("custom:") === 0) {
        const cm = (status && status.custom_models || []).find(c => c.id === sel);
        if (cm) {
          return {
            rpm: cm.rpm || 30,
            interval: cm.min_interval || 2,
            label: `${cm.label}（${cm.rpm || 30} RPM）`,
          };
        }
      }
      // 显式选了 Agnes
      if (sel === "agnes") {
        const lim = rl && rl.agnes;
        return {
          rpm: (lim && lim.rpm_limit) || 11,
          interval: 6,
          label: `Agnes (免费版·${(lim && lim.rpm_limit) || 11} RPM)`,
        };
      }
      // 跟随默认：降级了就走 Agnes，否则走主模型
      if (isAgnes) {
        const lim = rl && rl.agnes;
        return {
          rpm: (lim && lim.rpm_limit) || 11,
          interval: 6,
          label: `Agnes (降格·${(lim && lim.rpm_limit) || 11} RPM)`,
        };
      }
      const lim = rl && rl.primary;
      return {
        rpm: (lim && lim.rpm_limit) || 30,
        interval: 2,
        label: `Primary (~${(lim && lim.rpm_limit) || 30} RPM)`,
      };
    }

    async function translatePdfPage(pageNum, pageText = null, isPriority = false, force = false, model = null) {
      const block = document.getElementById(`pdf-trans-block-${pageNum}`);
      if (!block) return;
      const textEl = document.getElementById(`pdf-page-text-${pageNum}`) || block.querySelector(".page-text");
      const tagEl = document.getElementById(`pdf-page-tag-${pageNum}`);

      let text = pageText;
      if (!text && _pdfState && _pdfState.pages) {
        const pObj = _pdfState.pages.find(p => p.num === pageNum);
        if (pObj) text = pObj.text;
      }

      // De-dupe: skip if this page is already being translated by another
      // invocation (auto-queue + manual priority click could otherwise
      // race the same fetch). pObj.translating is left set; let the
      // in-flight caller finish first.
      if (_pdfState && _pdfState.pages) {
        const pObj = _pdfState.pages.find(p => p.num === pageNum);
        if (pObj) {
          if (pObj.translating) return;
          pObj.translating = true;
        }
      }

      if (!text || text.length < 10) {
        if (textEl) textEl.textContent = "(本页无文字内容)";
        if (tagEl) tagEl.textContent = "无文字";
        // Mark the slot as done so the queue doesn't keep picking it up
        if (_pdfState && _pdfState.pages) {
          const pObj = _pdfState.pages.find(p => p.num === pageNum);
          if (pObj) {
            pObj.translated = true;
            pObj.translating = false;
          }
        }
        return;
      }

      if (tagEl) tagEl.textContent = isPriority ? "⚡ 优先翻译中..." : "⏳ 翻译中...";
      if (textEl) textEl.innerHTML = `<span class="pdf-translation-loading">⏳ 正在调用 AI 翻译第 ${pageNum} 页...</span>`;

      // AbortController + 210s hard timeout. 服务端的单次翻译总时限是
      // LLM_TRANSLATION_DEADLINE（默认 180s），这里留 30s 余量。
      // 思考型模型（M3.1-Flash 强制思考）整页要 40~60s，一节三段可能 135s，
      // 90s 原来会把它砍在半路；砍掉之后拿到的还是残缺译文。
      const ac = new AbortController();
      const acTimer = setTimeout(() => {
        ac.abort();
        if (tagEl) tagEl.textContent = "⏳ 超时(>210s)";
      }, 210000);
      const t0 = performance.now();

      // 登记到「AI 活动」：页面标签会实时显示已用时，工具栏面板可取消。
      // 没有这个，用户面对的是一个不动的转圈 —— 分不清"在跑"还是"卡了"。
      const _aiModelLabel = (model != null ? model : (_pdfTransModel || "")) || "跟随默认";
      const aiTaskId = _aiTaskStart({
        kind: "translate",
        label: "翻译第 " + pageNum + " 页",
        model: _aiModelLabel,
        controller: ac,
        onStage: (stage, t) => {
          if (!tagEl) return;
          const el = t.startedAt ? (Date.now() - t.startedAt) : 0;
          const danger = el > 150000;
          const warn = el > 60000;
          if (t.state === "running") {
            tagEl.innerHTML = `<span class="ai-inline-status" data-warn="${danger ? "1" : "0"}">`
              + `<span class="ai-beat"></span>${escapeHtml(stage)} · ${_aiFormatMs(el)}</span>`;
          }
        },
      });

      try {
        const r = await fetch("/api/paper/translate_page", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paper_id: _pdfState.paperId,
            page: pageNum,
            // The backend caps `text` at 15000 chars — a dense 2-column
            // IEEE page exceeds that and 422s. The server chunks the text
            // itself, so truncating here is safe.
            text: String(text).slice(0, 15000),
            // 带上 PDF 相对路径：服务端会用 pdfplumber 重新抽取该页文本
            // （正确恢复丢失的单词空格 + 按栏顺序），翻译质量比 PDF.js 抓取的高。
            // relative_path 有效时服务端用自己的抽取结果，前端这份只作兜底。
            relative_path: (_pdfState && _pdfState.relativePath) || null,
            // force: 用户显式点「重新翻译」时忽略缓存，否则按了按钮
            // 拿到的还是同一份旧译文。
            force: !!force,
            // 换模型翻译：空 = 跟随默认设置
            model: (model != null ? model : (_pdfTransModel || "")),
          }),
          signal: ac.signal,
        });
        clearTimeout(acTimer);
        // ⚠ 2026-11-25 审计 P1：原来直接 `await r.json()`，而且**在检查
        //   `r.ok` 之前**。服务端/网关返 HTML 错误页或纯文本 502 时，
        //   JSON.parse 会抛 SyntaxError → 用户看到的是
        //   「Unexpected token '<'」，真正的错误原因（限流/超时/校验失败）
        //   **全被吞掉**，排查方向也被带偏。
        // → 走 _readJsonSafe：先拿原文，解析不出来就用状态码 + 片段兜底。
        const { data, text: resText } = await _readJsonSafe(r);
        const t1 = performance.now();
        _recordDiag({
          ts: new Date().toISOString(),
          page: pageNum,
          url: "/api/paper/translate_page",
          status: r.status,
          ok: r.ok,
          duration_ms: Math.round(t1 - t0),
          model: data?.model || "",
          chars_in: text.length,
          chars_out: (data?.translation || "").length,
          error: null,
        });
        if (!r.ok) {
          // FastAPI Pydantic 校验错误返 detail: [{loc, msg, type}], 转 string
          throw new Error(_httpErrorMessage(data, r, resText, "按页翻译失败"));
        }
        if (!data) {
          throw new Error("按页翻译返回了非 JSON 内容（HTTP " + r.status + "）");
        }

        // 后端可能返回 200 + 空串（思考模型把 token 预算全花在思考上，
        // 没产出正文）。这种情况必须当失败处理，否则页面只会显示
        // "已翻译"却没有内容。
        const translated = (data.translation || "").trim();
        if (!translated) {
          throw new Error("模型返回空内容（思考模式可能耗尽了 token 预算），请重试本页");
        }

        textEl.className = "page-text";
        textEl.innerHTML = formatMarkdownLite(translated);
        // 公式原图：从 PDF 原页按坐标裁出来贴进译文
        renderFormulaImages(textEl, (_pdfState && _pdfState.relativePath) || "", pageNum);
        // 先打高亮再渲染公式：引文里含公式时，用的是 TeX 源，
        // 在 KaTeX 渲染前匹配最准（渲染后也能匹配，但这里更直接）
        applyAnnotationsToPage(pageNum, textEl);
        if (typeof renderMathIn === "function") renderMathIn(textEl);
        // 译文回填 → 上方高度变了 → 续读位置会漂 → 重新贴回去
        _readPosReapply();

        // 服务端报的是**实际**用的模型：用户选的模型连不上时会自动退回主模型，
        // 这时显示实际那个，并明确告诉用户"已退回"，免得以为选模型没生效。
        const fellBack = !!data.model_fell_back;
        const modelTag = data.model ? ` · ${data.model}${fellBack ? "（已自动退回）" : ""}` : "";
        if (tagEl) {
          if (data.partial) {
            // 服务端超出了单次翻译的总时限，只翻完了前面的分段
            const n = data.translated_chunks || 0;
            const total = data.chunk_count || n;
            tagEl.textContent = `⚠️ 已翻译 ${n}/${total} 段 · 超出时限${modelTag}`;
          } else {
            tagEl.textContent = `✅ 已翻译${modelTag}`;
          }
        }
        if (fellBack && typeof showToast === "function") {
          showToast(`⚠️ 选中的模型连不上，已自动退回「${data.model}」翻译`, true, 5200);
        }
        _aiTaskEnd(aiTaskId, "done");

        if (_pdfState && _pdfState.pages) {
          const pObj = _pdfState.pages.find(p => p.num === pageNum);
          if (pObj) {
            pObj.translated = true;
            pObj.translating = false;
            // Cache translation text + model on the pageData so:
            //   1. "💾 保存当前" can POST them to /api/pdf/snapshot/save
            //   2. Future restore-from-snapshot can rehydrate without LLM
            pObj.translation = data.translation;
            pObj.translation_model = data.model || "";
            pObj.translated_at = new Date().toISOString();
          }
        }
        // Auto-persist: schedule a debounced snapshot save so closing the
        // app 5 seconds from now still keeps the user's progress. The
        // manual "💾 保存当前" button still works the same way.
        _scheduleAutoSnapshot();
        fetchModelStatus();
      } catch (err) {
        clearTimeout(acTimer);
        const t1 = performance.now();
        const isAbort = err && (err.name === "AbortError" || /aborted/i.test(err.message || ""));
        const detail = isAbort ? "请求超时 (>210s) — LLM 久未响应,请查看诊断面板判断"
                                : (err?.message || JSON.stringify(err) || "未知错误");
        _recordDiag({
          ts: new Date().toISOString(),
          page: pageNum,
          url: "/api/paper/translate_page",
          status: 0,
          ok: false,
          duration_ms: Math.round(t1 - t0),
          model: "",
          chars_in: text.length,
          chars_out: 0,
          error: isAbort ? "AbortError: timeout 210s" : detail,
        });
        console.error(`[translatePdfPage#${pageNum}]`, err);
        if (tagEl) tagEl.textContent = isAbort ? "⏳ 超时" : "❌ 失败";
        // 任务收尾：区分"用户取消"和"真失败"（取消不该显示成失败）
        _aiTaskEnd(aiTaskId, isAbort ? "cancelled" : "error", detail);
        if (textEl) {
          textEl.innerHTML = `<span style="color:#B25336;">❌ 翻译失败: ${escapeHtml(detail)}</span>
            <button class="btn-pill-outline" style="font-size:11px; padding:2px 8px; margin-left:8px;"
                    onclick="window.resetAndRetryPdfPage(${pageNum})">🔄 重试本页</button>`;
        }
        if (_pdfState && _pdfState.pages) {
          const pObj = _pdfState.pages.find(p => p.num === pageNum);
          if (pObj) {
            // Critical: mark translated=true so the auto-queue's `find`
            // doesn't keep picking up this failed page every 3.2s. Without
            // this, a transient LLM error caused the same page to be
            // re-fetched over and over ("翻译两遍"). Manual resetAndRetryPdfPage
            // resets the flag before re-issuing the request.
            pObj.translated = true;
            pObj.translating = false;
            pObj.failed = true;
          }
        }
      }
    }

    // Manual retry helper for a single failed page: clears the
    // translated/failed flags so the auto-queue (or this caller's
    // priority path) will pick it up again, but only one fresh attempt
    // is issued per click — no infinite-loop retry.
    function resetAndRetryPdfPage(pageNum) {
      if (_pdfState && _pdfState.pages) {
        const pObj = _pdfState.pages.find(p => p.num === pageNum);
        if (pObj) {
          pObj.translated = false;
          pObj.failed = false;
          pObj.translating = false;
        }
      }
      // force=true：显式重试必须绕过翻译缓存。否则用户点了「重试本页」，
      // 服务端直接命中旧缓存返回同一份坏译文，看起来像按钮没生效。
      translatePdfPage(pageNum, null, true, true);
    }
    window.resetAndRetryPdfPage = resetAndRetryPdfPage;

    // =====================================================================
    // 对照模式：译文按原文排版（2026-11-12）
    // =====================================================================
    //
    // 用户诉求原话：「就像那种翻译插件一样，他们和原文的排版是一模一样的，
    // 就是没什么区别。原文是双栏，他们也是双栏。」
    //
    // 实现要点（改这块之前先读）：
    //  1. **重排**而不是把译文贴回 canvas。
    //     PDF 页是按栏宽自适应渲染的（612pt 的页塞进 ~420px 的栏 → 倍率 0.69）
    //     → 原文正文只有 ~6px 高，贴回原位也读不了。
    //     所以对照视图改成「按原文分栏重排」：左栏进左栏、右栏进右栏，
    //     字号恢复正常（可读）。图片/图表不在文本层 → 重排后看不到，
    //     切回「原文」视图即可。
    //  2. 段落译文有**独立版本闸门**（服务端返回的 blocks_version）。
    //     段落切分算法一改 → 块编号变了 → 旧译文贴上去就是错位 → 整份丢弃。
    //     ⚠ **绝对不要动 paper_text.EXTRACTOR_VERSION** —— 动了用户全部整页
    //     译文作废重翻，还会波及另一台设备。
    //  3. 段落译文存进**快照**（pages[].blocks）→ 平板端能同步过去。
    //     平板访问不到本地后端（没有 baseURL、CSP 与原生桥白名单都拦着），
    //     快照是它唯一的通道。
    // =====================================================================

    /** 这一页的对照视图容器。 */
    function _bilingualHost(pageNum) {
      return document.getElementById(`bilingual-page-${pageNum}`);
    }

    /**
     * 取这一页的段落块（几何：i/kind/col/bbox/text）。
     * 没有就现抽 —— 服务端抽一页 64~332ms，丢在异步里不挡界面。
     */
    async function ensurePageBlocks(pageNum) {
      if (!_pdfState || !_pdfState.pages) return null;
      const pg = _pdfState.pages.find(p => p.num === pageNum);
      if (!pg) return null;
      if (pg.blocks) { _applySnapshotBlocks(pg); return pg.blocks; }
      if (pg.blocksPromise) { await pg.blocksPromise; return pg.blocks || null; }
      pg.blocksPromise = (async () => {
        try {
          const url = `/api/paper/page_blocks?relative_path=${encodeURIComponent(_pdfState.relativePath || "")}&page=${pageNum}`;
          const data = await getJson(url);
          pg.blocks = data.blocks || [];
          pg.blocksVersion = data.blocks_version || 0;
          // 快照里存着的段落译文，版本对得上才复用（对不上 = 段落切分变了）
          _applySnapshotBlocks(pg);
        } catch (e) {
          console.warn(`[bilingual p${pageNum}] 段落块抽取失败`, e);
          pg.blocks = [];
        }
      })();
      await pg.blocksPromise;
      pg.blocksPromise = null;
      return pg.blocks;
    }

    /** 把快照里的段落译文贴回这一页（版本闸门在这里把关）。 */
    function _applySnapshotBlocks(pg) {
      if (!pg || !pg.snapshotBlocks) return;
      if (pg.snapshotBlocksVersion !== pg.blocksVersion) return;   // 版本对不上 → 丢弃
      // 块编号必须一致（段数变了说明切分算法变了，编号已经错位）
      const snapIds = (pg.snapshotBlocks || []).map(b => b.i).join(",");
      const curIds = (pg.blocks || []).map(b => b.i).join(",");
      if (snapIds !== curIds) return;
      const map = {};
      (pg.snapshotBlocks || []).forEach(b => {
        if (b.translation) map[b.i] = b.translation;
      });
      if (Object.keys(map).length) {
        pg.blockTranslations = map;
        pg.blockTranslated = true;
      }
    }

    /** 把一段 Markdown 渲染进对照块（公式原图 / 表格 / KaTeX 都走现成那套）。 */
    function _bilingualRenderInto(el, text, relativePath, pageNum) {
      el.innerHTML = formatMarkdownLite(text || "");
      renderFormulaImages(el, relativePath || "", pageNum);
      if (typeof renderMathIn === "function") renderMathIn(el);
    }

    /**
     * 画一页的对照视图。
     * 结构：整宽块（标题/跨栏表） → [左栏 | 右栏] 两列 → 尾部整宽块。
     */
    function renderBilingualPage(pageNum) {
      const host = _bilingualHost(pageNum);
      if (!host || !_pdfState || !_pdfState.pages) return;
      const pg = _pdfState.pages.find(p => p.num === pageNum);
      const blocks = (pg && pg.blocks) || [];
      const trans = (pg && pg.blockTranslations) || {};
      const rel = (_pdfState && _pdfState.relativePath) || "";

      if (!blocks.length) {
        host.innerHTML = `<div class="bilingual-note">第 ${pageNum} 页没有可对照的段落（纯图片页？）</div>`;
        host.style.display = _pdfState.bilingual ? "" : "none";
        return;
      }

      // 按栏分桶。阅读顺序是「整宽顶部 → 左栏 → 右栏 → 跨栏」，
      // 所以整宽块按它在序列里的位置拆成「顶部」和「尾部」两段。
      const firstColIdx = blocks.findIndex(b => b.col === "L" || b.col === "R");
      const top = [], left = [], right = [], tail = [];
      blocks.forEach((b, idx) => {
        if (b.col === "L") { left.push(b); return; }
        if (b.col === "R") { right.push(b); return; }
        if (firstColIdx >= 0 && idx < firstColIdx) top.push(b);
        else tail.push(b);
      });

      const mkBlock = (b) => {
        const d = document.createElement("div");
        d.className = "bilingual-block kind-" + (b.kind || "body");
        // 原文：公式块是乱序碎片、表格块没有原文 → 不显示
        if (b.kind === "body" || b.kind === "heading") {
          const orig = document.createElement("div");
          orig.className = "bb-orig";
          orig.textContent = (b.text || "").replace(/\s*\n\s*/g, " ");
          d.appendChild(orig);
        }
        const tr = document.createElement("div");
        tr.className = "bb-trans";
        const t = trans[b.i];
        if (t) {
          _bilingualRenderInto(tr, t, rel, pageNum);
        } else {
          tr.innerHTML = `<span style="color:var(--fg-subtle);">（这一段还没翻）</span>`;
        }
        d.appendChild(tr);
        return d;
      };

      host.innerHTML = "";
      if (top.length) {
        const span = document.createElement("div");
        span.className = "bilingual-span";
        top.forEach(b => span.appendChild(mkBlock(b)));
        host.appendChild(span);
      }
      if (left.length || right.length) {
        const cols = document.createElement("div");
        cols.className = "bilingual-cols";
        [["L", left], ["R", right]].forEach(([key, arr]) => {
          if (!arr.length) return;
          const col = document.createElement("div");
          col.className = "bilingual-col";
          col.dataset.col = key;
          arr.forEach(b => col.appendChild(mkBlock(b)));
          cols.appendChild(col);
        });
        host.appendChild(cols);
      }
      if (tail.length) {
        const span = document.createElement("div");
        span.className = "bilingual-span";
        span.style.marginTop = "var(--s-4)";
        tail.forEach(b => span.appendChild(mkBlock(b)));
        host.appendChild(span);
      }
      host.style.display = _pdfState.bilingual ? "" : "none";
    }

    /** 对照模式的段落级翻译（一次 LLM 调用翻整页，按段号回填）。 */
    async function translateBlocksForPage(pageNum, isPriority = false, force = false, model = null) {
      if (!_pdfState || !_pdfState.pages) return;
      const pg = _pdfState.pages.find(p => p.num === pageNum);
      if (!pg) return;
      if (pg.translating) return;
      pg.translating = true;

      const tagEl = document.getElementById(`pdf-page-tag-${pageNum}`);
      const textEl = document.getElementById(`pdf-page-text-${pageNum}`);
      if (tagEl) tagEl.textContent = isPriority ? "⚡ 对照翻译中..." : "⏳ 对照翻译中...";
      if (textEl) textEl.innerHTML = `<span class="pdf-translation-loading">⏳ 正在按段落翻译第 ${pageNum} 页...</span>`;

      const ac = new AbortController();
      const acTimer = setTimeout(() => ac.abort(), 240000);
      const t0 = performance.now();
      try {
        const r = await fetch("/api/paper/translate_blocks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paper_id: _pdfState.paperId,
            page: pageNum,
            relative_path: (_pdfState && _pdfState.relativePath) || null,
            force: !!force,
            model: (model != null ? model : (_pdfTransModel || "")),
          }),
          signal: ac.signal,
        });
        clearTimeout(acTimer);
        // ⚠ 2026-11-25 审计 P1：同 translatePdfPage —— 原来 `await r.json()`
        //   在检查 `r.ok` **之前**，服务端返非 JSON 时抛 SyntaxError，
        //   真正的失败原因被吞掉 → 走 _readJsonSafe。
        const { data, text: resText } = await _readJsonSafe(r);
        _recordDiag({
          ts: new Date().toISOString(),
          page: pageNum,
          url: "/api/paper/translate_blocks",
          status: r.status,
          ok: r.ok,
          duration_ms: Math.round(performance.now() - t0),
          model: data?.model || "",
          chars_in: 0,
          chars_out: (data?.translation || "").length,
          error: null,
        });
        if (!r.ok) {
          throw new Error(_httpErrorMessage(data, r, resText, "对照翻译失败"));
        }
        if (!data) {
          throw new Error("对照翻译返回了非 JSON 内容（HTTP " + r.status + "）");
        }

        // 回填：几何 + 每段译文
        const map = {};
        (data.blocks || []).forEach(b => {
          if (b.translation) map[b.i] = b.translation;
        });
        pg.blocks = data.blocks || pg.blocks;
        pg.blocksVersion = data.blocks_version || 0;
        pg.blockTranslations = map;
        pg.blockTranslated = true;
        pg.blockFailed = false;
        pg.translated = true;
        pg.translating = false;
        pg.failed = false;
        pg.translation = data.translation || "";
        pg.translation_model = data.model || "";
        pg.translated_at = new Date().toISOString();

        // 右栏面板照旧显示整页译文（服务端把段落译文拼好了一起给）
        if (textEl && pg.translation) {
          textEl.className = "page-text";
          textEl.innerHTML = formatMarkdownLite(pg.translation);
          renderFormulaImages(textEl, (_pdfState && _pdfState.relativePath) || "", pageNum);
          applyAnnotationsToPage(pageNum, textEl);
          if (typeof renderMathIn === "function") renderMathIn(textEl);
        }
        const modelTag = data.model ? ` · ${data.model}` : "";
        if (tagEl) {
          tagEl.textContent = data.partial
            ? `⚠️ 对照翻译不完整${modelTag}`
            : `✅ 已对照翻译${modelTag}`;
        }
        renderBilingualPage(pageNum);
        _scheduleAutoSnapshot();
        fetchModelStatus();
      } catch (err) {
        clearTimeout(acTimer);
        const isAbort = err && (err.name === "AbortError" || /aborted/i.test(err.message || ""));
        const detail = isAbort ? "请求超时 (>240s)" : (err?.message || "未知错误");
        console.error(`[translateBlocksForPage#${pageNum}]`, err);
        if (tagEl) tagEl.textContent = isAbort ? "⏳ 超时" : "❌ 对照翻译失败";
        if (textEl) {
          textEl.innerHTML = `<span style="color:#B25336;">❌ 对照翻译失败: ${escapeHtml(detail)}</span>`;
        }
        pg.translating = false;
        pg.failed = true;
        // ⚠ 2026-11-25 审计 P1：原来这里写 `pg.blockTranslated = true`，
        //   本意是「标记成已处理，免得队列每 3.2 秒又来翻一次」。
        //   但 `blockTranslated` 同时被**替换版的补翻统计**当成
        //   「这页已经翻好了」→ **失败的页被算进「已完成」**
        //   → 用户以为全翻好了，实际那几页还是英文。
        // → 拆成两个标志：
        //     blockTranslated —— 只在**成功**时置 true（对外：这页翻好了）
        //     blockFailed    —— 失败标记（对队列：别再翻这一页）
        pg.blockTranslated = false;
        pg.blockFailed = true;
      }
    }

    /**
     * 直接在 PDF 双栏里打开一个 vault 里的 PDF —— **不做全文解析**。
     *
     * 为什么要单独一条：生成出来的「中译替换版」没必要再拆章节
     * （openVaultPdf 会跑一遍全文抽取 + 分章，慢且没用）。
     *
     * opts.replaced = true → 这是「中译替换版」：
     *   译文**已经打进 PDF 的替换页**里了（第 2 页就是第 1 页的中文版），
     *   所以 ① 右栏（对照译文）整条收起，PDF 占满宽度；
     *        ② **不能**再让翻译队列去翻一遍（12 页白花钱）。
     */
    async function openPdfDirect(relativePath, title, opts = {}) {
      if (!relativePath) return;
      _readerState.title = title || "";
      _readerState.sections = [];
      _readerState.translations = {};
      _readerState.translationModels = {};
      _readerState.activeSection = 0;
      _readerState.pdfPath = relativePath;
      renderReader();
      openReaderModal();
      switchReaderMode("pdf", { skipLoad: true });
      await loadPdfDualColumn({ replaced: !!opts.replaced });
    }

    /**
     * 「中译替换版」视图的界面状态。
     *
     * ⚠ 为什么不直接沿用普通 PDF 视图：
     *   1. 右栏挂的是**旧论文**的对照译文（页码也对不上这份 PDF）→
     *      用户会以为那就是替换页的内容；
     *   2. 翻译队列会把这份 PDF 的 12 页**重新翻一遍** → 白花钱。
     */
    function _applyReplacedPdfView(on) {
      const trans = document.getElementById("pdf-translations-container");
      if (trans) trans.style.display = on ? "none" : "";
      // ⚠ 光藏右栏不够：.pdf-split 是两列 grid → 还要把列数改成 1，
      //   否则 PDF 只占半屏（实测 1600px 视口里容器仍是 785px）。
      const split = document.querySelector("#reader-pdf-view .pdf-split");
      if (split) split.classList.toggle("pdf-split-single", !!on);
      // 回到普通 PDF → 源也没了（否则会拿旧的源去重建）
      if (!on) _pdfReplacedSource = null;
      if (!on) return;
      if (_pdfState) {
        _pdfState.bilingual = false;
        _pdfState.bilingualOrig = false;
      }
      const b1 = document.getElementById("pdf-bilingual-toggle");
      if (b1) { b1.classList.remove("active"); b1.textContent = "🔀 对照"; }
      const b2 = document.getElementById("pdf-bilingual-orig-toggle");
      if (b2) b2.classList.remove("active");
      _showBilingualVisibility(false);
    }

    /**
     * 生成「中译替换版 PDF」—— 原文页 + 替换页交替（10 页 → 20 页）。
     *
     * 用户原话（2026-11-15）：
     *   「直接就是原文上直接把翻译打上去……这篇论文一共十页，
     *     然后我翻译完之后就变成二十页……然后原本的图表还都保持不变。」
     *
     * 译文来自**快照**（对照模式翻好的段落译文 + 段落坐标），
     * 不额外调模型 → 一两秒出结果。
     */
    /**
     * 「中译替换版」的**全量体检**：这篇论文有多少页能替换、多少页不能。
     *
     * ⚠ 为什么必须「全量」（用户 2026-11-24 报「有的页数没翻到」）：
     *   旧实现只查**快照里已有的页** → 「压根没翻过的页」根本不在名单里
     *   → 那些页静默保留英文，用户**毫无提示**，只看到「几页还是英文」。
     *   实测本机 8 份快照：6 份的每一页都只有「整页译文」（普通模式翻的），
     *   其中 JAP 综述 36 页**全部**如此 → 替换版一份都替换不了。
     *
     * → 这里拿 PDF 的**总页数**对照快照，分成三类：
     *   ① ready        —— 有段落级译文 → 能替换
     *   ② onlyFulltext —— 只有整页译文（普通模式翻的）→ 没有坐标，替换不了
     *   ③ never        —— 压根没翻过 → 替换不了
     */
    async function _replacedPdfHealth() {
      const all = (_pdfState && _pdfState.pages) || [];
      const total = all.reduce((m, p) => Math.max(m, p.num || 0), 0);
      let snapPages = [];
      try {
        const snap = await getJson(
          `/api/pdf/snapshot/load?paper_id=${encodeURIComponent(_pdfState.paperId)}`);
        snapPages = snap.pages || [];
      } catch (e) {
        snapPages = [];
      }
      const withBlocks = new Set();
      const onlyFulltext = new Set();
      const seen = new Set();
      for (const p of snapPages) {
        const n = Number(p.page_num);
        if (!Number.isFinite(n) || n < 1) continue;
        seen.add(n);
        const blocks = p.blocks || [];
        const hasTranslatedBlock = blocks.some(b => b && (b.translation || "").trim());
        if (hasTranslatedBlock) withBlocks.add(n);
        else if ((p.translation || "").trim()) onlyFulltext.add(n);
      }
      const never = [];
      const missing = [];
      for (let n = 1; n <= total; n++) {
        if (!withBlocks.has(n)) missing.push(n);
        if (!seen.has(n)) never.push(n);
      }
      return {
        total,
        ready: Array.from(withBlocks).sort((a, b) => a - b),
        onlyFulltext: Array.from(onlyFulltext).sort((a, b) => a - b),
        never,
        missing,
      };
    }

    /**
     * 把缺段落级译文的页按段落重翻一遍（复用对照模式那套机器）。
     * 翻完的 blocks 会随快照存下来 → 下次构建不用再翻。
     * @returns {{done: number[], failed: number[], skipped: number[], total: number}}
     */
    async function _ensureParagraphTranslations() {
      const out = { done: [], failed: [], skipped: [], total: 0 };
      const h = await _replacedPdfHealth();
      out.total = h.total;
      const missing = h.missing;
      if (!missing.length) return out;
      const brief = missing.length > 12
        ? `${missing.slice(0, 12).join("、")} … 等 ${missing.length} 页`
        : missing.join("、");
      const detail = [
        h.never.length ? `· ${h.never.length} 页从未翻过` : "",
        h.onlyFulltext.length ? `· ${h.onlyFulltext.length} 页只有整页译文（没有段落坐标）` : "",
      ].filter(Boolean).join("\n");
      const ok = confirm(
        `这篇论文共 ${h.total} 页，其中 ${missing.length} 页还没有「段落级译文」：\n${detail}\n\n` +
        `替换版要把中文按段落原地贴回原页 → 必须有段落坐标，否则这些页只能保留英文。\n\n` +
        `要现在按段落重翻这 ${missing.length} 页吗？\n` +
        `（${brief}；约 ${missing.length * 12} 秒，消耗 API 额度）`);
      if (!ok) {
        out.skipped = missing.slice();
        return out;
      }
      showToast(`正在按段落补翻 ${missing.length} 页（约 ${missing.length * 12} 秒）…`,
                false, 8000);
      // ⚠ **并发**补翻：串行 26 页 × 15s ≈ 6.5 分钟，等不起。
      //   限并发 3 路（再多会打爆模型端速率限制 → 触发降级）。
      //   translateBlocksForPage 的 DOM 更新是**按页隔离**的
      //   （pdf-page-tag-N / pdf-page-text-N）→ 并发安全。
      const CONCURRENCY = 3;
      let cursor = 0;
      const worker = async () => {
        for (;;) {
          const i = cursor++;
          if (i >= missing.length) return;
          const n = missing[i];
          try {
            await translateBlocksForPage(n, true);
            const pg = (_pdfState.pages || []).find(p => p.num === n);
            if (pg && pg.blockTranslated) out.done.push(n);
            else out.failed.push(n);
          } catch (e) {
            out.failed.push(n);
          }
        }
      };
      await Promise.all(
        Array.from({ length: Math.min(CONCURRENCY, missing.length) }, worker));
      out.done.sort((a, b) => a - b);
      out.failed.sort((a, b) => a - b);
      if (out.done.length) {
        // 把新翻的段落译文存进快照（不存的话下次构建又要重翻一遍）
        await savePdfSnapshot({ silent: true });
      }
      return out;
    }

    async function buildReplacedPdf() {
      if (!_pdfState || !_pdfState.relativePath) {
        showToast("先打开一篇 PDF", true, 3000);
        return;
      }
      // ⚠ 先把缺段落级译文的页补上 —— 否则那些页会**原样保留英文**
      //   （用户 2026-11-17：「我论文最后几页都没替换」；
      //    2026-11-24：「有的页数没翻到」—— 根因是**压根没翻过的页**
      //    压根不在旧名单里，现在改成**全量体检**）。
      const fill = await _ensureParagraphTranslations();
      // ⚠ 源必须是**原论文**：生成完阅读器里打开的是替换版，
      //   `_pdfState` 也就变成了它 → 再点一次若还用它 = 找「替换版的快照」→ 必然 409。
      const src = _pdfReplacedSource || {
        paper_id: _pdfState.paperId,
        relative_path: _pdfState.relativePath,
      };
      const started = Date.now();
      showToast("正在生成中译替换版 PDF…");
      const btn = document.getElementById("pdf-replaced-toggle");
      if (btn) { btn.disabled = true; btn.textContent = "⏳ 生成中"; }
      try {
        const res = await fetch("/api/paper/build_replaced_pdf", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paper_id: src.paper_id,
            relative_path: src.relative_path,
          }),
        });
        const { data, text } = await _readJsonSafe(res);
        if (!res.ok) {
          throw new Error(_httpErrorMessage(data, res, text, "生成失败"));
        }
        const d = data || {};
        const sec = ((Date.now() - started) / 1000).toFixed(1);
        const total = (d.pages_total || 0) + (d.pages_replaced || 0);
        const warn = d.blocks_unfit ? ` · ${d.blocks_unfit} 段放不下（保留英文）` : "";
        // ⚠ 如实说明哪些页**没**被替换（用户 2026-11-17 报的「最后几页都没替换」）
        // ⚠ 2026-11-24：改成用**全量体检**的结果报，别只报「有整页译文」的页 ——
        //   **压根没翻过的页**也要报出来（那才是「有的页数没翻到」的主因）。
        const never = Array.isArray(d.pages_never_translated) ? d.pages_never_translated : [];
        const onlyFt = Array.isArray(d.pages_only_fulltext) ? d.pages_only_fulltext : [];
        const stillEnglish = Array.isArray(d.pages_missing) ? d.pages_missing : [];
        const parts = [];
        if (never.length) parts.push(`第 ${never.join("、")} 页从未翻过`);
        if (onlyFt.length) parts.push(`第 ${onlyFt.join("、")} 页只有整页译文（无段落坐标）`);
        const skipNote = parts.length ? ` · ⚠ ${parts.join("；")} → 保留英文` : "";
        // 用户主动放弃了补翻 → 明确说出来，别让人以为「全翻了」
        const declined = (fill.skipped && fill.skipped.length)
          ? ` · ⚠ 已跳过 ${fill.skipped.length} 页补翻` : "";
        const failedNote = (fill.failed && fill.failed.length)
          ? ` · ⚠ 第 ${fill.failed.join("、")} 页补翻失败` : "";
        showToast(`✅ 已生成 ${total} 页（${d.pages_total} 原文 + ${d.pages_replaced} 替换），`
          + `${d.blocks_written} 段已替换${warn}${skipNote}${declined}${failedNote} · ${sec}s`,
          false, stillEnglish.length ? 12000 : 8000);
        // 记住源 → 再点一次还能重建
        _pdfReplacedSource = { paper_id: src.paper_id, relative_path: src.relative_path };
        // ⚠ 标题别叠成「（中译替换版）（中译替换版）」—— 再点一次就会叠一次
        const baseTitle = (_readerState.title || "论文")
          .replace(/（中译替换版）\s*$/, "");
        await openPdfDirect(d.out_relative_path,
          `${baseTitle}（中译替换版）`, { replaced: true });
      } catch (err) {
        console.error("[buildReplacedPdf]", err);
        showToast("生成失败: " + (err && err.message ? err.message : err), true, 6000);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = "🧾 中译替换版"; }
      }
    }

    /** 开关对照模式。 */
    async function toggleBilingualMode() {
      if (!_pdfState) return;
      _pdfState.bilingual = !_pdfState.bilingual;
      _bilingualPref = !!_pdfState.bilingual;
      const btn = document.getElementById("pdf-bilingual-toggle");
      if (btn) {
        btn.classList.toggle("active", !!_pdfState.bilingual);
        btn.textContent = _pdfState.bilingual ? "🔀 对照中" : "🔀 对照";
      }
      _syncBilingualOrigButton();
      _persistLocalState();
      const pages = _pdfState.pages || [];
      if (_pdfState.bilingual) {
        for (const pg of pages) {
          await ensurePageBlocks(pg.num);
          renderBilingualPage(pg.num);
        }
        _showBilingualVisibility(true);
        // 没做段落翻译的页 → 交给队列按段落翻
        processNextInPdfQueue();
        showToast("🔀 对照模式：译文按原文分栏排版（左栏回左栏、右栏回右栏）", false, 4200);
      } else {
        _showBilingualVisibility(false);
      }
    }
    window.toggleBilingualMode = toggleBilingualMode;

    /**
     * 开关「对照模式里同时显示原页图」。
     *
     * 为什么需要（遗留项：对照视图里看不到图片/图表）：
     *   图片/图表**不在 PDF 文本层里** → 译文按原文重排后，
     *   页面上只剩文字，图全没了（用户原话）。
     *   开着这个开关 → 原页 canvas 不藏了：
     *     宽屏 → 左「原页图」右「对照译文」并排；
     *     窄屏 → 原页图在上、对照译文在下（默认的 flex column 布局）。
     *   零额外请求：canvas 本来就已经渲染好了。
     */
    function toggleBilingualOrig() {
      if (!_pdfState) return;
      _pdfState.bilingualOrig = !_pdfState.bilingualOrig;
      _bilingualOrigPref = !!_pdfState.bilingualOrig;
      _syncBilingualOrigButton();
      _persistLocalState();
      _showBilingualVisibility(!!_pdfState.bilingual);
      if (!_pdfState.bilingualOrig) {
        showToast("🖼 对照模式只显示重排后的译文", false, 3200);
        return;
      }
      // 并排还是上下堆叠，取决于**阅读器栏**放不放得下（不是视口宽度）
      const side = _applyBilingualOrigLayout(true);
      showToast(side
        ? "🖼 原页图在左、对照译文在右（图看得见了）"
        : "🖼 原页图在上、对照译文在下（窗口不够宽，并排放不下）",
        false, 4200);
    }
    window.toggleBilingualOrig = toggleBilingualOrig;

    /** 把「原页图」按钮的文案/高亮同步成当前状态。 */
    function _syncBilingualOrigButton() {
      const btn = document.getElementById("pdf-bilingual-orig-toggle");
      if (!btn) return;
      const on = !!(_pdfState && _pdfState.bilingualOrig);
      btn.classList.toggle("active", on);
      btn.textContent = on ? "🖼 原页图中" : "🖼 原页图";
    }

    /**
     * 「原页图 + 对照译文」到底是**并排**还是**上下堆叠**？
     *
     * ⚠⚠ 不能只看视口宽度（2026-11-13 真机实测踩过）：
     *   原来的门槛是 `@media (min-width:900px)`（**视口**宽），
     *   但真正决定「并排放不放得下」的是**阅读器栏**有多宽：
     *   1600px 的窗口里，阅读器栏只有 ~790px，而原页 canvas 就有 733px
     *   → 并排后对照译文只剩 **43px**（挤成一条竖线，根本没法读）。
     *   而且当时是**逐页按各自的 canvas 宽度算** → 同一篇里第 1 页全宽、
     *   第 2 页被挤成一条，滚起来忽宽忽窄（实测截图）。
     *
     * → 现在：**所有页卡统一**判断（取最大的 canvas 宽度），
     *   译文列至少要 `_BILINGUAL_MIN_TRANS_WIDTH` 宽，否则一律**上下堆叠**
     *   （原页图在上、对照译文在下 → 译文全宽，可读）。
     *
     * 返回 true = 并排，false = 上下堆叠。
     */
    const _BILINGUAL_MIN_TRANS_WIDTH = 380;
    function _applyBilingualOrigLayout(showOrig) {
      const cards = Array.from(document.querySelectorAll(".pdf-page-block"));
      let maxCanvas = 0;
      cards.forEach(c => {
        const w = c.querySelector(".pdf-page-canvas-wrapper");
        if (w) maxCanvas = Math.max(maxCanvas, w.offsetWidth || 0);
      });
      // 页卡可用宽度（= 阅读器栏宽 - 边距）。所有页卡取同一个值，
      // 保证**整篇一致**，不会一页宽一页窄。
      const avail = cards.length ? (cards[0].clientWidth || 0) : 0;
      const gap = 14;                       // --s-5
      const canSide = !!(showOrig && avail > 0 && maxCanvas > 0 &&
                         (avail - maxCanvas - gap) >= _BILINGUAL_MIN_TRANS_WIDTH);
      cards.forEach(c => {
        c.classList.toggle("bilingual-orig", !!showOrig);
        c.classList.toggle("bilingual-orig-wide", canSide);
      });
      return canSide;
    }
    window._applyBilingualOrigLayout = _applyBilingualOrigLayout;

    /** 切换「原文画布 / 对照重排」两者的显隐。 */
    function _showBilingualVisibility(on) {
      // 「同时显示原页图」开着 → 原页 canvas 留着（左右并排 / 上下堆叠）
      const showOrig = !!(on && _pdfState && _pdfState.bilingualOrig);
      document.querySelectorAll(".pdf-page-canvas-wrapper").forEach(w => {
        w.style.display = showOrig ? "" : (on ? "none" : "");
      });
      document.querySelectorAll(".bilingual-page").forEach(el => {
        el.style.display = on ? "" : "none";
      });
      _applyBilingualOrigLayout(showOrig);
    }



    async function handleFetchFullText() {
      const idInput = document.getElementById("paper-id-input").value.trim();
      const title = document.getElementById("paper-title").value.trim();
      if (!idInput) {
        showToast("请先输入 arXiv ID 或 DOI 或 PDF 链接", true);
        return;
      }
      showToast("正在拉取全文并拆分章节...");
      const startedAt = Date.now();
      const ticker = setInterval(() => {
        showToast(`正在拉取全文并拆分章节…（${Math.round((Date.now() - startedAt) / 1000)}s）`);
      }, 3000);
      try {
        const result = await postJson("/api/paper/fetch_full", { identifier: idInput });
        if (!result.sections || result.sections.length === 0) {
          throw new Error("PDF 解析后未识别到任何章节, 请检查 PDF 是否可读");
        }
        _readerState.title = result.title || title || idInput;
        _readerState.sections = result.sections;
        _readerState.translations = {};
        _readerState.activeSection = 0;
        _readerState.pdfPath = result.pdf_path || null;
        // Remember what the user typed — the hash restore needs the
        // identifier (a vault path cannot be re-fetched).
        _readerState.identifier = idInput;
        renderReader();
        _persistReaderState();
        _restoreTranslationsFromCache();
        openReaderModal();
        showToast(`✅ 已加载 ${result.sections.length} 个章节 (${result.total_pages} 页, ${result.char_count.toLocaleString()} 字, ${Math.round((Date.now() - startedAt) / 1000)}s)`);
      } catch (err) {
        showToast("拉取失败: " + err.message, true);
      } finally {
        clearInterval(ticker);
      }
    }

    // ---------------------------------------------------------------------------
    // 从 vault 打开已有 PDF（不复制文件、不重复上传）
    // ---------------------------------------------------------------------------
    let _vaultPdfOptions = [];

    async function loadVaultPdfOptions() {
      const sel = document.getElementById("vault-pdf-select");
      if (!sel) return;
      sel.innerHTML = `<option value="">📂 从仓库论文库打开…</option>`;
      try {
        const data = await getJson("/api/paper/list_vault_pdfs");
        if (!data.pdfs || data.pdfs.length === 0) {
          sel.innerHTML = `<option value="">📂 仓库里还没有 PDF</option>`;
          showToast("仓库 01_Literature 下还没有 PDF", true);
          return;
        }
        _vaultPdfOptions = data.pdfs;
        const groups = {};
        data.pdfs.forEach(p => { (groups[p.topic] = groups[p.topic] || []).push(p); });
        Object.keys(groups).sort().forEach(topic => {
          const og = document.createElement("optgroup");
          og.label = topic;
          groups[topic].forEach(p => {
            const opt = document.createElement("option");
            opt.value = p.relative_path;
            opt.textContent = `${p.name}  (${p.size_kb} KB)`;
            og.appendChild(opt);
          });
          sel.appendChild(og);
        });
        sel.title = `共 ${data.count} 个 PDF · 选中即打开分屏精读（不会复制文件）`;
      } catch (err) {
        sel.innerHTML = `<option value="">📂 扫描失败</option>`;
        showToast("扫描仓库失败: " + (err.message || err), true);
      }
    }

    async function openVaultPdf(relativePath) {
      const mark = (s) => { console.log("[vault-pdf]", s); };
      mark("OPENING: " + relativePath);
      showToast("正在解析 PDF 全文并拆分章节…");
      // 长任务要让用户看见"在动"：每 3 秒更新一次阶段提示 + 已耗时，
      // 否则一次几十秒的解析看起来就像卡死（2026-10-25 用户反馈）。
      const startedAt = Date.now();
      const ticker = setInterval(() => {
        const sec = Math.round((Date.now() - startedAt) / 1000);
        showToast(`正在解析 PDF 全文并拆分章节…（${sec}s）`);
      }, 3000);
      try {
        const res = await fetch("/api/paper/open_vault_pdf", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ relative_path: relativePath }),
        });
        const { data: result, text: resText } = await _readJsonSafe(res);
        mark("HTTP " + res.status);
        if (!res.ok) {
          throw new Error(_httpErrorMessage(result, res, resText, "打开失败"));
        }

        _readerState.title = result.title || "";
        _readerState.sections = result.sections;
        _readerState.translations = {};
        _readerState.translationModels = {};
        _readerState.activeSection = 0;
        _readerState.pdfPath = result.pdf_path || null;

        mark("RENDERING...");
        renderReader();
        mark("OPENING MODAL...");
        openReaderModal();
        mark("SWITCHING TO PDF...");
        // 直接进 PDF 双栏：用户从仓库挑论文就是为了看逐页译文
        // （switchReaderMode 内部会调用 loadPdfDualColumn）
        switchReaderMode("pdf");
        mark(`OK: ${result.total_pages} 页 / ${result.sections.length} 章节`);
        const sec = Math.round((Date.now() - startedAt) / 1000);
        showToast(`✅ 已打开 ${result.total_pages} 页 / ${result.sections.length} 章节（${result.char_count.toLocaleString()} 字，${sec}s）`);
        loadStats();
      } catch (err) {
        mark("ERROR: " + (err && err.message ? err.message : err));
        showToast("打开失败: " + (err && err.message ? err.message : err), true);
      } finally {
        clearInterval(ticker);
      }
    }

    async function handleUploadLocalPdf(event) {
      const input = event.target;
      if (!input.files || input.files.length === 0) return;
      const file = input.files[0];
      if (!file.name.toLowerCase().endsWith(".pdf")) {
        showToast("仅支持上传 PDF 文件 (.pdf)", true);
        input.value = "";
        return;
      }

      showToast(`正在上传并解析本地 PDF (${file.name})...`);
      const startedAt = Date.now();
      const ticker = setInterval(() => {
        const sec = Math.round((Date.now() - startedAt) / 1000);
        showToast(`正在上传并解析本地 PDF (${file.name})…（${sec}s）`);
      }, 3000);
      const category = document.getElementById("paper-category") ? document.getElementById("paper-category").value : "01_Device_TCAD_器件仿真";
      const titleInput = document.getElementById("paper-title") ? document.getElementById("paper-title").value.trim() : "";

      const formData = new FormData();
      formData.append("file", file);

      try {
        let url = `/api/paper/upload_pdf?topic_category=${encodeURIComponent(category)}`;
        if (titleInput) {
          url += `&title=${encodeURIComponent(titleInput)}`;
        }
        const res = await fetch(url, {
          method: "POST",
          body: formData,
        });
        const { data: result, text: resText } = await _readJsonSafe(res);
        if (!res.ok) {
          throw new Error(_httpErrorMessage(result, res, resText, "上传失败"));
        }

        // Fill metadata into form if blank
        if (document.getElementById("paper-title") && !titleInput) {
          document.getElementById("paper-title").value = result.title || file.name.replace(/\.pdf$/i, "");
        }
        const absSec = result.sections.find(s => s.section === "abstract");
        if (absSec && document.getElementById("paper-abstract") && !document.getElementById("paper-abstract").value.trim()) {
          document.getElementById("paper-abstract").value = absSec.text.slice(0, 800);
        }

        // Set reader state
        _readerState.title = result.title || titleInput || file.name.replace(/\.pdf$/i, "");
        _readerState.sections = result.sections;
        _readerState.translations = {};
        _readerState.activeSection = 0;
        _readerState.pdfPath = result.pdf_path || null;
        // A local upload has no arXiv/DOI — drop any stale identifier so
        // the hash restore does not re-open the previous paper.
        _readerState.identifier = "";

        renderReader();
        openReaderModal();
        _persistReaderState();
        _restoreTranslationsFromCache();

        // Refresh PDF view if currently active
        if (_readerMode === "pdf") {
          document.getElementById("pdf-pages-list").innerHTML = "";
          document.getElementById("pdf-translations-list").innerHTML = "";
          loadPdfDualColumn();
        }

        showToast(`✅ 本地 PDF 已归档至 [${result.pdf_path}], 解析就绪: ${result.sections.length} 个章节 (${result.total_pages} 页, ${result.char_count.toLocaleString()} 字, ${Math.round((Date.now() - startedAt) / 1000)}s)`);
        loadStats();
      } catch (err) {
        showToast("上传解析失败: " + err.message, true);
      } finally {
        clearInterval(ticker);
        input.value = "";
      }
    }

    function renderReader() {
      const s = _readerState;
      document.getElementById("reader-title").textContent = s.title;
      document.getElementById("reader-meta").textContent =
        `${s.sections.length} 章节 · 共 ${s.sections.reduce((n, x) => n + x.char_count, 0).toLocaleString()} 字`;

      // Section nav
      const navHtml = s.sections.map((sec, i) => {
        const isActive = i === s.activeSection;
        const translated = s.translations[i];
        const icon = sec.section === "preamble" ? "📄" :
                     sec.section === "abstract" ? "📋" :
                     sec.section === "introduction" ? "🔭" :
                     sec.section === "methods" ? "🔬" :
                     sec.section === "results" ? "📊" :
                     sec.section === "discussion" ? "💭" :
                     sec.section === "conclusion" ? "✅" :
                     sec.section === "references" ? "📚" : "📑";
        return `<div class="reader-nav-item ${isActive ? "active" : ""}" onclick="selectSection(${i})">
          <span style="margin-right:6px;">${icon}</span>
          <span>${escapeHtml(sec.title)}</span>
          ${translated ? '<span style="margin-left:auto; color:var(--accent-olive); font-size:10px;">译</span>' : ""}
        </div>`;
      }).join("");
      document.getElementById("reader-nav").innerHTML = navHtml;

      renderSection(_readerState.activeSection);
      // Cross-QA section picker — runs once per reader open via idempotent render
      renderCrossQASectionPicker();
    }

    function selectSection(idx) {
      _readerState.activeSection = idx;
      renderReader();
      _persistReaderState();
    }

    function renderSection(idx) {
      const sec = _readerState.sections[idx];
      if (!sec) return;
      document.getElementById("reader-original").innerHTML = `
        <div class="reader-section-header">
          <div class="reader-section-title">${escapeHtml(sec.title)}</div>
          <div class="reader-section-meta">第 ${sec.page_start}-${sec.page_end} 页 · ${sec.char_count.toLocaleString()} 字 · ${escapeHtml(sec.section)}</div>
        </div>
        <div class="reader-section-body en-original">${formatMarkdownLite(sec.text)}</div>`;
      const translated = _readerState.translations[idx];
      const modelName = (_readerState.translationModels && _readerState.translationModels[idx]) || "";
      const transEl = document.getElementById("reader-translation");
      if (translated) {
        transEl.innerHTML = `
          <div class="reader-section-header">
            <div class="reader-section-title">📝 中文翻译</div>
            <div style="display:flex; gap:8px; align-items:center;">
              <span class="reader-section-meta">${modelName ? `[${escapeHtml(modelName)}] · ` : ""}${translated.length} 字</span>
              <button class="btn-pill-outline" style="font-size:11px; padding:3px 10px;" onclick="copySectionTranslation(${idx})">📋 复制译文</button>
              <button class="btn-pill-outline" style="font-size:11px; padding:3px 10px;" onclick="translateSection(${idx}, true)">🔄 重新翻译</button>
            </div>
          </div>
          <div class="reader-section-body cn-translation">${formatMarkdownLite(translated)}</div>`;
        // 章节视图的批注（page=0）同样先打高亮再渲染公式
        applyAnnotationsToPage(0, transEl.querySelector(".cn-translation") || transEl);
        renderMathIn(transEl);
      } else {
        transEl.innerHTML = `
          <div class="reader-section-header">
            <div class="reader-section-title">📝 中文翻译</div>
            <div class="reader-section-meta">点击下方按钮调用 AI 翻译</div>
          </div>
          <button class="btn-pill-outline" style="margin-top:16px;" onclick="translateSection(${idx})">
            <span>✨ 一键翻译本节</span>
          </button>`;
      }
    }

    function copySectionTranslation(idx) {
      const trans = _readerState.translations[idx];
      if (!trans) return;
      navigator.clipboard.writeText(trans).then(() => {
        showToast("✅ 已复制本节译文到剪贴板");
      }).catch(() => {
        showToast("复制失败，请手动选取复制", true);
      });
    }


    /**
     * 把译文里的 ⟦FORMULA:x0,y0,x1,y1⟧ 换成从 PDF 原页裁下来的公式图。
     *
     * 为什么这么做：公式在 PDF 里是矢量排版（分子一行、分母一行、上下标各
     * 一行），文本层抽出来是乱序碎片，硬拼 LaTeX 极容易把分子分母装反
     * （用户原话："分子分母分不清谁是谁"）。错公式比没公式危险得多 ——
     * 用户会照着它去算。所以抽取层只记坐标，这里裁原图贴进去：和论文里
     * 长得一模一样。
     *
     * 裁图失败（PDF 被挪走/接口报错）→ 退回文字提示，不留破图。
     */
    function renderFormulaImages(container, relativePath, pageNum) {
      if (!container || !relativePath) return;
      if ((container.textContent || "").indexOf("⟦FORMULA:") < 0) return;
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
      const nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      for (const node of nodes) {
        const v = node.nodeValue || "";
        if (v.indexOf("⟦FORMULA:") < 0) continue;
        const frag = document.createDocumentFragment();
        let last = 0;
        const re = /⟦FORMULA:(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)⟧/g;
        let m;
        while ((m = re.exec(v)) !== null) {
          if (m.index > last) {
            frag.appendChild(document.createTextNode(v.slice(last, m.index)));
          }
          frag.appendChild(_formulaShotNode(m, relativePath, pageNum));
          last = m.index + m[0].length;
        }
        if (last < v.length) frag.appendChild(document.createTextNode(v.slice(last)));
        if (node.parentNode) node.parentNode.replaceChild(frag, node);
      }
    }

    function _formulaShotNode(m, relativePath, pageNum) {
      const x0 = parseFloat(m[1]), y0 = parseFloat(m[2]);
      const x1 = parseFloat(m[3]), y1 = parseFloat(m[4]);
      const wrap = document.createElement("span");
      wrap.className = "formula-shot";
      wrap.title = "公式原图（从 PDF 原页裁出，与论文一致）";
      const img = document.createElement("img");
      img.className = "formula-shot-img";
      img.alt = "公式原图";
      img.loading = "lazy";
      // 1pt = 1/72 inch；按 96dpi 折算成 CSS 像素，让公式显示成论文里的尺寸
      const wpx = Math.max(60, Math.min(620, Math.round((x1 - x0) * 1.3333)));
      img.style.width = wpx + "px";
      img.src = "/api/paper/formula_image?relative_path=" + encodeURIComponent(relativePath)
        + "&page=" + encodeURIComponent(String(pageNum || 1))
        + "&rect=" + encodeURIComponent([x0, y0, x1, y1].join(","))
        + "&scale=4";
      img.onerror = function () {
        wrap.innerHTML = '<span class="formula-fallback">【公式未能完整提取：请对照左侧 PDF 原文】</span>';
      };
      wrap.appendChild(img);
      return wrap;
    }
    window.renderFormulaImages = renderFormulaImages;

    /**
     * Run KaTeX on all .math-display-block and .math-placeholder elements inside a container.
     * KaTeX replaces each placeholder's textContent with rendered math.
     * Errors render as red text.
     */

    async function translateSection(idx, force = false) {
      const sec = _readerState.sections[idx];
      const transEl = document.getElementById("reader-translation");
      const estChars = (sec.text || "").length;
      const estChunks = estChars > 8000 ? Math.ceil(estChars / 8000) : 1;
      // Backend: section_text has min_length=10 / max_length=60000. A stub
      // section would 422 — guard it client-side instead of sending it.
      const secText = (sec.text || "");
      if (secText.trim().length < 10) {
        if (transEl) {
          transEl.innerHTML = `<div style="padding:14px; color:var(--text-muted);">本节正文过短 (${secText.trim().length} 字), 无法翻译。</div>`;
        }
        showToast("本节正文过短, 无法翻译", true, 3000);
        return;
      }
      transEl.innerHTML = `<div style="padding:14px; color:var(--text-muted);">⏳ 翻译中${estChunks > 1 ? ` (大章节, 约拆 ${estChunks} 段)` : ""}...</div>`;
      // 章节翻译也可能跑好几分钟 → 给它一个 AbortController，
      // 「AI 活动」面板里的 ✕ 取消要**真的**中止请求，
      // 不能只弹一句"已取消"然后让任务在后台继续跑完（独立审查抓到的）。
      const _aiSecAbort = new AbortController();
      // 登记到「AI 活动」（大章节可能要几分钟，更需要看得见）
      const _aiSecTaskId = _aiTaskStart({
        kind: "section",
        label: "翻译章节：" + ((sec.title || sec.section || ("第 " + (idx + 1) + " 节")).slice(0, 24)),
        model: (_pdfTransModel || "") || "跟随默认",
        controller: _aiSecAbort,
        onStage: (stage, t) => {
          if (!transEl) return;
          if (t.state !== "running") return;   // 收尾后不再改这里（译文由 renderSection 写）
          const el = t.startedAt ? (Date.now() - t.startedAt) : 0;
          transEl.innerHTML = `<div style="padding:14px; color:var(--text-muted);">`
            + `<span class="ai-inline-status"><span class="ai-beat"></span>`
            + `${escapeHtml(stage)} · ${_aiFormatMs(el)}</span>`
            + `${estChunks > 1 ? ` (大章节, 约拆 ${estChunks} 段)` : ""}</div>`;
        },
      });
      try {
        const res = await fetch("/api/paper/translate_section", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            section_title: (sec.title || "").slice(0, 200),
            section_text: secText.slice(0, 60000),
            section_key: (sec.section || "").slice(0, 50),
            // 点「重新翻译」时忽略缓存，否则拿到的还是同一份旧译文
            force: !!force,
            // 换模型翻译：空 = 跟随默认设置
            model: (_pdfTransModel || ""),
          }),
          signal: _aiSecAbort.signal,
        });
        const { data: result, text: resText } = await _readJsonSafe(res);
        if (!res.ok) {
          throw new Error(_httpErrorMessage(result, res, resText, "翻译失败"));
        }
        _readerState.translations[idx] = result.translation;
        _readerState.translationModels = _readerState.translationModels || {};
        _readerState.translationModels[idx] = result.model;
        renderSection(idx);
        renderReader();
        fetchModelStatus();

        if (result.from_cache) {
          showToast(`⚡ 缓存命中 · ${(result.chunk_count || 1)} 段 · 0s`);
        } else if (result.partial) {
          // 服务端超出了单次翻译的总时限，只翻完了前面的分段
          const n = result.translated_chunks || 0;
          const total = result.chunk_count || n;
          showToast(`⚠️ 超出时限，只翻了 ${n}/${total} 段 · 可点「重新翻译本节」继续`, true, 6000);
        } else if (result.was_segmented) {
          showToast(`✅ 翻译完成 · 拆 ${result.chunk_count} 段`);
        } else {
          showToast(`✅ 翻译完成 (${result.model || "AI"})`);
        }
        _aiTaskEnd(_aiSecTaskId, "done");
      } catch (err) {
        // 用户自己取消的 → 标"已取消"，不该显示成"翻译失败"
        const _isAbort = err && (err.name === "AbortError" || /aborted/i.test(err.message || ""));
        if (_isAbort) {
          _aiTaskEnd(_aiSecTaskId, "cancelled");
          if (transEl) {
            transEl.innerHTML = `<div style="padding:14px; color:var(--text-muted);">✕ 已取消本次翻译</div>`;
          }
          return;
        }
        _aiTaskEnd(_aiSecTaskId, "error", err && (err.message || String(err)));
        transEl.innerHTML = `<div style="padding:18px; color:var(--accent-terracotta);">
          <div style="font-weight:600; margin-bottom:6px;">❌ 翻译失败: ${escapeHtml(err.message)}</div>
          <div style="font-size:12px; color:var(--text-subtle); margin-bottom:12px;">提示: 若主模型 429 额度耗尽，系统已自动降格至 Agnes 3.0 Flash，点击重试即可生效。</div>
          <button class="btn-pill-outline" style="font-size:12px; padding:6px 14px;" onclick="translateSection(${idx}, true)">🔄 重新翻译本节</button>
        </div>`;
      }
    }

    async function translateAllSections() {
      const untranslated = _readerState.sections.filter((_, i) => !_readerState.translations[i]);
      if (!untranslated.length) {
        showToast("所有章节均已翻译完毕");
        return;
      }
      const rpmInfo0 = _getCurrentRpmInfo();
      if (!confirm(`将批量翻译剩余 ${untranslated.length} 个章节。为避免打满 ${rpmInfo0.rpm} RPM 限制，章节间将自动保持 ${(rpmInfo0.interval + 0.2).toFixed(1)} 秒间隔，确定继续?`)) {
        return;
      }
      const btn = document.getElementById("translate-all-btn");
      btn.disabled = true;
      for (let i = 0; i < _readerState.sections.length; i++) {
        if (_readerState.translations[i]) continue;
        btn.innerHTML = `<span>⏳ 翻译中 (${i + 1}/${_readerState.sections.length} 节，${rpmInfo0.rpm} RPM 保护)...</span>`;
        await translateSection(i);
        await new Promise(r => setTimeout(r, rpmInfo0.interval * 1000 + 200));
      }
      btn.disabled = false;
      btn.innerHTML = '<span>✨ 批量翻译全部章节</span>';
      showToast("✅ 批量翻译完成");
    }

    async function exportBilingualNote(mode = "bilingual") {
      const s = _readerState;
      // Two possible data sources: section-view reader (s.sections[idx] +
      // s.translations[idx]) OR PDF dual-column view (_pdfState.pages[i] with
      // embedded .translation). The latter never touches _readerState.sections
      // — loadPdfDualColumn writes to its own _pdfState — so the buttons used
      // to silently no-op when the user was in PDF mode. Normalize to a
      // single {sections, translations, title} shape that downstream code can
      // consume without branching.
      let sections, translations, title, transKeySpace;
      if (s.sections && s.sections.length) {
        sections = s.sections;
        translations = s.translations || {};
        // _readerState.translations is keyed by SECTION ORDINAL (0,1,2…).
        transKeySpace = "ordinal";
        title = s.title;
      } else if (_pdfState && _pdfState.pages && _pdfState.pages.length) {
        // Synthesize sections from per-page data. Each page becomes one
        // section (with page title + page-range hints) so the exported .md
        // is still useful as a reading note even in PDF dual-column mode.
        sections = _pdfState.pages.map((p) => ({
          title: `第 ${p.num} 页 / Page ${p.num}`,
          section: String(p.num),
          text: p.text || "",
          page_start: p.num,
          page_end: p.num,
        }));
        translations = {};
        for (const p of _pdfState.pages) {
          if (p.translated && p.translation) {
            translations[p.num] = p.translation;
          }
        }
        // PDF-mode translations are keyed by PAGE NUMBER.
        transKeySpace = "page";
        title = _pdfState.paperTitle || s.title || "Untitled";
      } else {
        showToast("当前无论文内容可导出", true);
        return;
      }
      // Translation coverage guard: bilingual notes are only useful when
      // sections actually have their AI translation. Without this check the
      // user gets a file that is just the English original wrapped in
      // collapsible <details> blocks — defeating the whole point of an
      // "双语精读笔记" export. Pre-flight the count so they can react.
      const totalSecs = sections.length;
      // sections are indexed either by sequential idx (section-view) OR by
      // page num (PDF-view synthetic) — we need a lookup helper that handles
      // both. translations object is `{idx: text}` in section view but
      // `{pageNum: text}` in PDF-view synthesis.
      // The two key spaces must never mix: a section ordinal (0,1,2…) and
      // a page number (1,2,3…) are different namespaces. Looking a page
      // number up in the ordinal-keyed map used to put the Introduction's
      // Chinese text into the Abstract (the Abstract is usually section 0
      // with page_start 1), and in PDF mode every page showed page 1's
      // translation (the map is page-keyed but the loop index is 0-based).
      const lookupTranslation = (idx, sec) => {
        if (transKeySpace === "page") {
          const pageNum = sec && sec.page_start ? sec.page_start : idx + 1;
          return translations[pageNum] || "";
        }
        return translations[idx] || "";
      };
      const isTranslated = (idx, sec) => !!lookupTranslation(idx, sec);
      const translatedSecs = sections.reduce(
        (n, sec, idx) => n + (isTranslated(idx, sec) ? 1 : 0), 0
      );
      if (mode !== "en-only" && translatedSecs === 0) {
        showToast(
          `⚠️ 这 ${totalSecs} 章都还没翻译,中文模式没东西可写。先点「一键翻译本节」翻完再导出。`,
          true,
          5400
        );
        return;
      }
      const missingList = sections
        .map((sec, idx) => (!isTranslated(idx, sec)) ? (sec.title || `第 ${idx+1} 节`) : null)
        .filter(Boolean);
      const topic = (document.getElementById("paper-category") && document.getElementById("paper-category").value) ||
                    "01_Device_TCAD_器件仿真";
      const authors = document.getElementById("paper-authors") ? document.getElementById("paper-authors").value : "";
      const yearVal = document.getElementById("paper-year") ? parseInt(document.getElementById("paper-year").value, 10) : 2024;
      // Adjust filename suffix to make the variant obvious in the file list
      const variantSuffix = mode === "cn-only" ? "中文精译" : mode === "en-only" ? "English" : "双语精读";
      const defaultFn = `${yearVal || 2024}_${(title || "Untitled").replace(/[\\/:*?"<>|]/g, "_").slice(0, 35)}_${variantSuffix}`;
      const warning = (mode !== "en-only" && missingList.length > 0)
        ? `⚠️ 还有 ${missingList.length}/${totalSecs} 章未翻译 (${missingList.slice(0, 3).join("、")}${missingList.length > 3 ? " 等" : ""}),这些章节将只含英文原文(空中文)。`
        : "";
      // Surface the warning as a non-blocking toast so the user still
      // gets the heads-up but we don't make them click through a confirm
      // dialog they might dismiss by reflex (which used to silently abort
      // the export — "点了仅中文但保存不了").
      if (warning) showToast(warning, true, 5400);
      const dest = await showVaultSaveModal({
        topic: topic,
        kind: "literature_cards",
        defaultFilename: defaultFn,
      });
      if (!dest) return; // user cancelled
      // When the user doesn't type a custom one, the modal returns
      // dest.filename === "" — in that case fall back to the variant-aware
      // defaultFn so the on-disk filename reflects the chosen mode (e.g.
      // "_中文精译" suffix instead of always "_双语精读").
      const finalFilename = (dest.filename || "").trim() || defaultFn;
      showToast("正在生成排版 HTML 并导出...", false, 2400);
      // Build the embedded HTML for each section: apply formatMarkdownLite
      // for markdown structure (paragraphs, headings, bold, lists, code),
      // then convert math placeholders BACK to raw `$..$` / `$$..$$` source
      // — the user prefers the saved .md to NOT embed <span class="katex">
      // AST nodes. Users who DO install an Obsidian math plugin (e.g.
      // obsidian-latex) will still see properly rendered formulas; without
      // a plugin the literal source is shown.
      let renderedSections = 0;
      const sectionsPayload = [];
      for (let idx = 0; idx < sections.length; idx++) {
        const sec = sections[idx];
        const transStr = lookupTranslation(idx, sec).toString();
        let translationHtml = "";
        let origHtml = "";
        if (transStr) {
          const tmp = document.createElement("div");
          tmp.innerHTML = formatMarkdownLite(transStr);
          translationHtml = _unrenderMathPlaceholders(tmp.innerHTML);
        }
        if (sec.text) {
          const tmp2 = document.createElement("div");
          tmp2.innerHTML = formatMarkdownLite(sec.text);
          origHtml = _unrenderMathPlaceholders(tmp2.innerHTML);
        }
        // Apply mode: drop the field the user didn't ask for. Server still
        // sees the keys as "" and skips the corresponding block in the
        // generator's `if trans_html` / `if orig_html` guards.
        const payloadObj = {
          title: sec.title,
          section: sec.section,
          text: sec.text,
          orig_html: origHtml,
          translation: transStr,
          translation_html: translationHtml,
          page_start: sec.page_start,
          page_end: sec.page_end,
        };
        if (mode === "cn-only") {
          payloadObj.text = "";
          payloadObj.orig_html = "";
        } else if (mode === "en-only") {
          payloadObj.translation = "";
          payloadObj.translation_html = "";
        }
        sectionsPayload.push(payloadObj);
        renderedSections++;
      }
      try {
        const payload = {
          title: title || "未命名论文",
          topic_category: topic,
          authors: authors,
          year: isNaN(yearVal) ? 2024 : yearVal,
          sections: sectionsPayload,
          qa_records: getSessionQaList(),
          subfolder: dest.subfolder || "",
          filename_override: finalFilename,
        };
        const data = await postJson("/api/paper/export_bilingual", payload);
        const qaCountNotice = (getSessionQaList().length) ? ` (已整合 ${getSessionQaList().length} 条划词答疑)` : "";
        showToast(
          `✅ 双语笔记已导出 (${renderedSections} 章节已 KaTeX 排版): [${data.folder}/${data.file_name}]${qaCountNotice}`,
          false, 5400
        );
        loadStats();
      } catch (err) {
        showToast("导出失败: " + err.message, true);
      }
    }


    // 用户要的是"一个设备上看了论文、做了标注、划了关键词，点个同步，
    // 另一个设备点下同步就能接着看"。所以同步的是三样东西：
    //   1) 已翻好的译文（快照）→ 另一台设备直接拿到，不用重翻、不烧额度
    //   2) 批注（高亮 + 笔记）  → 按 id 合并，两边同时标注不互相覆盖
    //   3) 阅读进度（第几页/缩放）→ 直接跳过去
    // 两台设备点同一个按钮即可：推的是自己，拉的是对方，最后合并。

    function _readingProgress() {
      const pid = (_pdfState && _pdfState.paperId) || "";
      if (!pid) return null;
      // ⚠ 原来只认**右栏译文栏** → 用户在看左栏 PDF 原文时，
      //   推给另一台设备的 page 恒为 0（等于什么也没推过去）。
      //   现在两栏都取，**原文栏优先**（原文栏才是「翻到第几页」的那一栏）。
      const left = _readPosAnchor(_READPOS_PANES.pdf);
      const right = _readPosAnchor(_READPOS_PANES.trans);
      const primary = left || right;
      return {
        paper_id: pid,
        relative_path: (_pdfState && _pdfState.relativePath) || "",
        title: (_pdfState && _pdfState.paperTitle) || "",
        page: primary ? primary.page : 0,
        zoom: _pdfZoom,
        scroll_top: primary ? primary.scroll_top : 0,
        mode: _readerMode,
        updated_at: new Date().toISOString(),
      };
    }

    function _applyReadingProgress(prog) {
      if (!prog) return;
      const pid = (_pdfState && _pdfState.paperId) || "";
      if (prog.paper_id && pid && prog.paper_id !== pid) return;   // 不是这一篇
      if (prog.zoom && Math.abs((_pdfZoom || 1) - prog.zoom) > 0.01) {
        _pdfZoom = prog.zoom;
        updateZoomLabel();
      }
      if (!prog.page) return;
      // ⚠ 原来只找**译文块**（pdf-trans-block-N）→ 对方在读左栏原文时，
      //   推过来的页码在这边找不到译文块 → 跳不过去（点「跳到第 N 页」没反应）。
      //   现在两栏都试：先原文页块，再译文块；页块还没渲染出来就重试几次。
      const attempt = (n) => {
        let hit = false;
        const pdfPane = _READPOS_PANES.pdf;
        if (document.getElementById(pdfPane.idPrefix + prog.page)) {
          hit = _restoreReadPosAnchor(pdfPane, prog.page, 0);
        }
        if (!hit) {
          const transPane = _READPOS_PANES.trans;
          if (document.getElementById(transPane.idPrefix + prog.page)) {
            hit = _restoreReadPosAnchor(transPane, prog.page, 0);
          }
        }
        if (hit) {
          showToast(`已跳到第 ${prog.page} 页（另一台设备读到这里）`);
        } else if (n < 10) {
          setTimeout(() => attempt(n + 1), 400);
        }
      };
      attempt(0);
    }

    // ===========================================================================
    // 一键同步（跨设备）
    // ===========================================================================
    const SYNC_STEPS = [
      { id: "push_snapshots",  name: "推送 · 译文快照", kind: "snapshots",  dir: "push" },
      { id: "push_annotations", name: "批注（写入即同步）", kind: "annotations", dir: "push" },
      { id: "push_progress",  name: "推送 · 阅读进度", kind: "progress",  dir: "push" },
      { id: "push_files",      name: "推送 · 论文 PDF（增量）", kind: "files", dir: "push" },
      { id: "pull_snapshots",  name: "拉取 · 译文快照", kind: "snapshots",  dir: "pull" },
      { id: "pull_annotations", name: "拉取 · 批注（高亮+笔记）", kind: "annotations", dir: "pull" },
      { id: "pull_progress",   name: "拉取 · 阅读进度", kind: "progress",  dir: "pull" },
      { id: "pull_files",      name: "拉取 · 论文 PDF（自动补齐）", kind: "files", dir: "pull" },
    ];

    function _syncStep(id, state, detail) {
      const el = document.getElementById("sync-step-" + id);
      if (!el) return;
      el.className = "sync-step " + state;
      const ico = { run: "⏳", done: "✅", fail: "❌", skip: "·" }[state] || "·";
      el.querySelector(".ico").textContent = ico;
      el.querySelector(".st-detail").textContent = detail || "";
    }

    function closeSyncModal() {
      const m = document.getElementById("sync-modal");
      if (m) m.style.display = "none";
      _escUnbind(m);
      if (_syncReloadTimer) { clearTimeout(_syncReloadTimer); _syncReloadTimer = null; }
    }

    // 拉到新译文后用它重建页面（用户点按钮，或倒计时结束自动执行）
    let _syncReloadTimer = null;
    function reloadAfterSync() {
      closeSyncModal();
      location.reload();
    }

    // 论文 PDF 通道：空间信息 + 释放云端实体
    async function refreshSyncFilesInfo() {
      const el = document.getElementById("sync-files-info");
      if (!el) return;
      try {
        const r = await fetch("/api/sync/status");
        if (!r.ok) return;
        const d = await r.json();
        const f = d.files || {};
        const items = f.items || [];
        const mb = ((f.total_bytes || 0) / 1048576).toFixed(1);
        const local = items.filter(x => x.local).length;
        const tombs = items.filter(x => x.deleted).length;
        const released = items.filter(x => x.blob === false && !x.deleted).length;
        let txt = `论文 PDF：云端 ${f.count || 0} 篇 / ${mb} MB · 本机已有 ${local} 篇`;
        if (released) txt += ` · ${released} 篇已释放`;
        if (tombs) txt += ` · ${tombs} 篇标记删除`;
        el.textContent = txt;
      } catch (e) { /* 统计失败不影响同步 */ }
    }

    async function releaseCloudPdfs() {
      const ok = confirm(
        "释放云端保存的 PDF 实体？\n\n" +
        "· 云端 PDF 会被删除，腾出坚果云空间\n" +
        "· 批注、译文快照、阅读进度全部保留\n" +
        "· 本机的 PDF 不受影响\n\n" +
        "（PDF 内容没变的话，以后同步也不会再自动传回去）");
      if (!ok) return;
      try {
        const d = await postJson("/api/sync/files/release", { all: true });
        // ⚠ 释放失败的那几篇**必须说出来** —— 否则「腾出 X MB」是虚的，
        //   用户据此删本地副本，实则云端那份还在（实体被坚果云锁住时常见）。
        const bad = (d.errors || []).length;
        if (bad) {
          showToast(`已释放 ${d.released} 篇云端 PDF，腾出 ${((d.freed_bytes || 0) / 1048576).toFixed(1)} MB；`
            + `另有 ${bad} 篇释放失败（文件被占用），云端那份还在，别急着删本地副本`, true);
        } else {
          showToast(`已释放 ${d.released} 篇云端 PDF，腾出 ${((d.freed_bytes || 0) / 1048576).toFixed(1)} MB`);
        }
        await refreshSyncFilesInfo();
      } catch (e) {
        showToast("释放失败: " + (e.message || e), true);
      }
    }

    async function syncNow() {
      console.log("[sync] 开始");
      const modal = document.getElementById("sync-modal");
      const stepsEl = document.getElementById("sync-steps");
      const dirEl = document.getElementById("sync-modal-dir");
      const noteEl = document.getElementById("sync-modal-note");
      const reloadBtn = document.getElementById("sync-reload-btn");
      const btn = document.getElementById("sync-btn");
      if (modal) modal.style.display = "flex";
      // ⚠ 2026-11-25 审计 P1：登记进 Esc 栈 ——
      //   否则同步弹窗开着时按 Esc = 整个阅读器被关掉（内容全丢）。
      _escBind(modal, closeSyncModal);
      if (btn) { btn.disabled = true; btn.innerText = "☁ 同步中…"; }
      if (reloadBtn) reloadBtn.style.display = "none";
      if (_syncReloadTimer) { clearTimeout(_syncReloadTimer); _syncReloadTimer = null; }
      if (stepsEl) {
        stepsEl.innerHTML = SYNC_STEPS.map(s =>
          `<div class="sync-step run" id="sync-step-${s.id}">` +
          `<span class="ico">⏳</span><span class="st-name">${s.name}</span>` +
          `<span class="st-detail">进行中…</span></div>`).join("");
      }
      if (noteEl) noteEl.textContent = "正在与共享目录交换状态…";
      const t0 = Date.now();

      try {
        const r = await fetch("/api/sync", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paper_id: (_pdfState && _pdfState.paperId) || "",
            progress: _readingProgress(),
          }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const d = await r.json();
        console.log("[sync] 完成", d);

        if (dirEl) dirEl.textContent = "同步目录：" + (d.root || "未知");
        SYNC_STEPS.forEach(s => {
          const bucket = (s.dir === "push" ? d.pushed : d.pulled) || {};
          const list = bucket[s.kind] || [];

          // PDF 通道单独渲染：它的语义和「文件列表」不一样
          if (s.kind === "files") {
            const rep = ((d.files || {})[s.dir]) || {};
            if (rep.disabled) {
              _syncStep(s.id, "skip", "PDF 本体由坚果云 vault 直接共享，不经此通道传输");
              return;
            }
            if (s.dir === "push") {
              const pub = (rep.published || []).length;
              const upd = (rep.updated || []).length;
              const del = (rep.deleted || []).length;
              if (pub || upd || del) {
                const bits = [];
                if (pub) bits.push(`新增 ${pub} 篇`);
                if (upd) bits.push(`更新 ${upd} 篇`);
                if (del) bits.push(`标记删除 ${del} 篇`);
                _syncStep(s.id, "done", bits.join(" · "));
              } else {
                _syncStep(s.id, "skip", `无需上传（${rep.unchanged || 0} 篇未变）`);
              }
            } else {
              const rest = (rep.restored || []).length;
              const conf = (rep.conflicts || []).length;
              const rem = (rep.removed || []).length;
              const keep = (rep.retained || []).length;
              if (rest || rem) {
                const bits = [];
                if (rest) bits.push(`补齐 ${rest} 篇`);
                if (rem) bits.push(`删除 ${rem} 篇`);
                if (keep) bits.push(`${keep} 篇按设置保留在本机`);
                _syncStep(s.id, "done", bits.join(" · "));
              } else if (conf) {
                _syncStep(s.id, "fail", `${conf} 篇版本冲突，未覆盖`);
              } else {
                _syncStep(s.id, "skip", "本机已齐全");
              }
            }
            return;
          }

          if (!list.length) {
            _syncStep(s.id, "skip", s.dir === "push" ? "无需推送" : "没有新内容");
            return;
          }
          const names = list.map(x => {
            // 文件名是「论文名_哈希」，太长 → 只留中间的论文名尾部
            const n = String(x).replace(/\.json$/i, "");
            return n.length > 28 ? "…" + n.slice(-27) : n;
          }).slice(0, 2).join("、");
          const more = list.length > 2 ? ` 等 ${list.length} 项` : "";
          _syncStep(s.id, "done", `${list.length} 项 · ${names}${more}`);
        });

        const secs = ((Date.now() - t0) / 1000).toFixed(1);
        // 2026-11-09 审计：后端单项失败不再被吞掉，errors 里有明细。
        // 只要有失败项就不能再显示「✅ 同步成功」—— 否则用户以为全上云了。
        const syncErrors = Array.isArray(d.errors) ? d.errors : [];
        const errCount = Number(d.error_count) || syncErrors.length;
        const bits = [];
        if (errCount) {
          const detail = syncErrors.slice(0, 2)
            .map(x => `${x.kind || "同步"}: ${x.name || ""}`).join("、");
          bits.push(`⚠ ${errCount} 项失败${detail ? "（" + detail + "）" : ""}`);
        }
        const pulledAnno = ((d.pulled || {}).annotations || []).length;
        if (pulledAnno) bits.push(`更新了 ${pulledAnno} 条批注`);
        if (d.progress && d.progress.page) bits.push(`阅读进度：第 ${d.progress.page} 页`);
        const fPush = ((d.files || {}).push) || {};
        const fPull = ((d.files || {}).pull) || {};
        if ((fPush.published || []).length) bits.push(`上传 ${fPush.published.length} 篇 PDF`);
        if ((fPull.restored || []).length) bits.push(`补齐 ${fPull.restored.length} 篇 PDF`);
        if ((fPull.conflicts || []).length) bits.push(`${fPull.conflicts.length} 篇 PDF 冲突`);
        refreshSyncFilesInfo();

        if (d.snapshot_updated) {
          // 拉到了新译文：结果留在面板上（别一闪而过），给用户看清再刷新
          if (noteEl) {
            noteEl.textContent = `${errCount ? "⚠ 同步完成但有失败项" : "✅ 同步成功"} · 推送 ${d.pushed_total || 0} 项 / 拉取 ${d.pulled_total || 0} 项`
              + ` · 耗时 ${secs}s · 拉到了新的译文，10 秒后自动刷新页面重建`
              + (bits.length ? " · " + bits.join(" · ") : "");
          }
          if (reloadBtn) reloadBtn.style.display = "inline-block";
          let left = 10;
          const tick = setInterval(() => {
            left -= 1;
            if (noteEl) {
              noteEl.textContent = noteEl.textContent.replace(/\d+ 秒后自动刷新页面重建/, `${left} 秒后自动刷新页面重建`);
            }
            if (left <= 0) clearInterval(tick);
          }, 1000);
          _syncReloadTimer = setTimeout(reloadAfterSync, 10500);
          return;
        }

        if (noteEl) {
          noteEl.textContent = `${errCount ? "⚠ 同步完成但有失败项" : "✅ 同步成功"} · 推送 ${d.pushed_total || 0} 项 / 拉取 ${d.pulled_total || 0} 项`
            + ` · 耗时 ${secs}s` + (bits.length ? " · " + bits.join(" · ") : "");
        }
        if (pulledAnno) {
          await loadAnnotationsForPaper((_pdfState && _pdfState.paperId) || null);
        }
        if (d.progress) _applyReadingProgress(d.progress);
      } catch (e) {
        SYNC_STEPS.forEach(s => _syncStep(s.id, "fail", "同步失败"));
        if (noteEl) noteEl.textContent = "❌ 同步失败: " + (e.message || e);
      } finally {
        if (btn) { btn.disabled = false; btn.innerText = "☁ 同步"; }
      }
    }

    // ===========================================================================
    // 翻译：换模型 + 重新翻译
    // ===========================================================================
    // _pdfTransModel：工具栏下拉框选中的模型（"" = 跟随 .env 的默认设置）。
    // 换模型后缓存键也跟着换（服务端按模型分键），所以换模型 = 真的重译。
    //
    // ⚠️ 翻译和答疑是**两套独立**的选择（用户 2026-10-26 的要求）：
    //   翻译 → M3（关思考，快）/ Agnes Flash / DeepSeek Flash
    //   答疑 → M3.1-Flash（会推导）/ DeepSeek Flash / Agnes Flash
    // 共用一个下拉框 → 想换翻译模型就连带换掉答疑模型，反之亦然。
    let _pdfTransModel = "";


    async function loadTranslationModelChoices() {
      const sel = document.getElementById("pdf-trans-model");
      if (!sel) return;
      try {
        const r = await fetch("/api/llm/translation_models");
        if (!r.ok) return;
        const d = await r.json();
        const cur = sel.value;
        sel.innerHTML = "";
        (d.models || []).forEach(m => {
          const o = document.createElement("option");
          o.value = m.id;
          let label = m.label;
          // 降格状态要说清楚：用户只会觉得"变慢了"，不知道为什么
          if (m.id === "" && _llmStatus && _llmStatus.is_degraded) {
            label += "（已降格到备用模型）";
          }
          o.textContent = label + (m.id ? (m.active ? "（当前）" : "") : "");
          o.title = m.hint || "";
          sel.appendChild(o);
        });
        sel.value = cur || "";
        _pdfTransModel = sel.value || "";
      } catch (e) { /* 拉不到就保持"跟随默认" */ }
    }

    // 重新翻译本页：忽略缓存，用当前选中的模型
    async function retranslatePdfPage(pageNum) {
      const tagEl = document.getElementById(`pdf-page-tag-${pageNum}`);
      const textEl = document.getElementById(`pdf-page-text-${pageNum}`);
      if (tagEl) tagEl.textContent = "🔄 重译中…";
      if (textEl) textEl.className = "page-text pdf-translation-loading";
      // 对照模式开着 → 重译走**段落**那条路（否则对照视图和右栏会各说各话）
      if (_pdfState && _pdfState.bilingual) {
        const pg = _pdfState.pages.find(p => p.num === pageNum);
        if (pg) { pg.blockTranslated = false; pg.translating = false; pg.blockFailed = false; }
        await translateBlocksForPage(pageNum, true, true, _pdfTransModel);
        return;
      }
      await translatePdfPage(pageNum, null, true, true, _pdfTransModel);
    }


    // 重新翻译整篇：按 RPM 保护排队，一页一页来
    async function retranslateAllPdfPages() {
      if (!_pdfState || !_pdfState.pages || !_pdfState.pages.length) {
        showToast("先打开一篇论文", true);
        return;
      }
      const n = _pdfState.pages.length;
      const modelName = (_pdfTransModel || "跟随默认");
      if (!confirm(`用「${modelName}」重新翻译全部 ${n} 页？\n\n` +
                   `会忽略现有译文重新调用模型，消耗 API 额度；\n` +
                   `每页约 15~30 秒，按 RPM 保护排队。`)) return;
      const btn = document.getElementById("pdf-retranslate-all-btn");
      const orig = btn ? btn.innerText : null;
      for (const p of _pdfState.pages) {
        if (btn) btn.innerText = `🔄 ${p.num}/${n}`;
        await retranslatePdfPage(p.num);
        const info = _getCurrentRpmInfo();
        await new Promise(r => setTimeout(r, info.interval * 1000 + 400));
      }
      if (btn) btn.innerText = orig || "🔄 全篇重译";
      showToast(`已重新翻译 ${n} 页 · 记得点「保存快照」`);
    }


function applyRestoredReaderPrefs(s) {
  if (!s) return;
  if (s.readerMode === "section" || s.readerMode === "pdf") _readerMode = s.readerMode;
  if (typeof s.pdfZoom === "number") _pdfZoom = s.pdfZoom;
  if (s.zenPref === true) _zenPref = true;
  if (s.bilingual === true) _bilingualPref = true;
  if (s.bilingualOrig === true) _bilingualOrigPref = true;
}
function getReaderPrefsSnapshot() {
  return {
    readerMode: _readerMode || "section",
    pdfZoom: _pdfZoom || 1.0,
    zenPref: !!_zenPref,
    bilingual: !!_bilingualPref,
    bilingualOrig: !!_bilingualOrigPref,
  };
}
function getPdfState() { return _pdfState; }
function getReaderMode() { return _readerMode; }
function getPdfZoom() { return _pdfZoom; }

export {
  openReaderModal, closeReaderModal, switchReaderMode, setReaderZoom, updateZoomLabel,
  _readerMode, _pdfZoom, _bilingualPref, _bilingualOrigPref, _pdfState, _pdfReplacedView, _pdfReplacedSource, _pdfZoomReloadTimer,
  _zenMode, _zenPref, isZenMode, enterZenMode, exitZenMode, toggleZenMode, toggleZenNav, toggleZenAux, toggleZenSingle, zenTogglePane, _initZenMode,
  _pdfIsRelayoutBusy, _pdfRelayoutIfNeeded, _pdfVerifyRenderedWidth, _initPdfWidthObserver, loadPdfDocument, _isReplacedPdfPath, loadPdfDualColumn,
  _dedupePdfBlocks, applySnapshotToState, _scheduleAutoSnapshot, savePdfSnapshot, _bilingualBlocksPayload,
  listSnapshots, deleteSnapshot, openFromSnapshot, showSnapshotModal, refreshSnapshotModal, openSnapshotById, closeSnapshotModal,
  _recordDiag, _clearDiagLog, _diagSummary, showDiagnoseModal, _renderDiagnoseTable, closeDiagnoseModal,
  togglePdfQueuePause, startPdfTranslationQueue, processNextInPdfQueue, renderPdfPage, renderTextLayerFallback, _getCurrentRpmInfo,
  translatePdfPage, resetAndRetryPdfPage, _bilingualHost, ensurePageBlocks, _applySnapshotBlocks, _bilingualRenderInto, renderBilingualPage,
  translateBlocksForPage, openPdfDirect, _applyReplacedPdfView, _replacedPdfHealth, _ensureParagraphTranslations, buildReplacedPdf,
  toggleBilingualMode, toggleBilingualOrig, _syncBilingualOrigButton, _applyBilingualOrigLayout, _showBilingualVisibility,
  handleFetchFullText, loadVaultPdfOptions, openVaultPdf, handleUploadLocalPdf, renderReader, selectSection, renderSection, copySectionTranslation,
  renderFormulaImages, _formulaShotNode, translateSection, translateAllSections, exportBilingualNote,
  _readingProgress, _applyReadingProgress, SYNC_STEPS, closeSyncModal, reloadAfterSync, refreshSyncFilesInfo, releaseCloudPdfs, syncNow,
  _pdfTransModel, loadTranslationModelChoices, retranslatePdfPage, retranslateAllPdfPages,
  applyRestoredReaderPrefs, getReaderPrefsSnapshot, getPdfState, getReaderMode, getPdfZoom,
};
