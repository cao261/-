// 应用入口：挂载全局视图控制器与内联模板回调、初始化图书馆/实验室/快捷键并启动工作台
import {
  showToast, getJson, postJson, scrollToWork, _applyTheme, toggleTheme,
  _initHeaderScrollState, loadPdfJs, loadKatex,
} from "./core/utils.js";
import {
  _readerState, fetchModelStatus, updateModelBadges, probeModelNow, checkOrProbeModel,
  loadQAModelChoices, onQAModelChange,
} from "./state/store.js";
import { _initReadPosTracking } from "./state/readpos.js";
import { _aiTaskCancel, toggleAiPanel, askPaperQuestion, presetQuestion, crossSectionQA, presetCrossQA } from "./reader/ai-activity.js";
import {
  openAnnotationEditor, closeAnnotationEditor, saveAnnotation, deleteCurrentAnnotation,
  toggleAnnoDrawer, closeAnnoDrawer, initAnnotationFeatures,
} from "./reader/annotations.js";
import {
  initReaderSelectionListener, toggleReaderQAPanel, minimizeReaderQAPanel, _qaCollapsedHeadClick,
  openReaderQAPanel, initReaderQAPanelDrag, clearQAQuotedText, _refineQA,
  triggerQAPreset, handleQAInputKeydown, submitReaderQA, exportCurrentQAToObsidian,
  copyLastQAAnswer, saveQAToTroubleshoot,
} from "./reader/qa-copilot.js";
import {
  openReaderModal, closeReaderModal, switchReaderMode, setReaderZoom,
  toggleZenMode, exitZenMode, toggleZenNav, toggleZenAux, toggleZenSingle, zenTogglePane, _initZenMode,
  _initPdfWidthObserver, savePdfSnapshot, deleteSnapshot, showSnapshotModal, refreshSnapshotModal,
  openSnapshotById, closeSnapshotModal, _clearDiagLog, showDiagnoseModal, closeDiagnoseModal,
  togglePdfQueuePause, translatePdfPage, resetAndRetryPdfPage, buildReplacedPdf,
  toggleBilingualMode, toggleBilingualOrig, handleFetchFullText, loadVaultPdfOptions,
  openVaultPdf, handleUploadLocalPdf, selectSection, copySectionTranslation,
  translateSection, translateAllSections, exportBilingualNote, closeSyncModal, reloadAfterSync,
  releaseCloudPdfs, syncNow, loadTranslationModelChoices, retranslatePdfPage, retranslateAllPdfPages,
  getPdfState,
} from "./reader/pdf-reader.js";
import { openShortcutsModal, closeShortcutsModal, initShortcuts } from "./reader/shortcuts.js";
import {
  saveObsidianVaultName, updateObsidianStatus, loadRecentFeed, feedLoadMore, feedReveal, revealInExplorer,
  feedOpen, feedDelete, loadStats, handleFetchPaper, handleSavePaper, refreshPaperSubfolderList,
  updatePaperSavePathPreview, _wirePaperFormInputs, handleCreateRepro, handleGenerateWeekly,
  handleSaveTroubleshoot, _restoreFromHash, _restoreLocalState, handlePasteTextFallback,
  switchWorkbenchView,
} from "./views/dashboard.js";
import { loadLibraryCatalog, initLibraryView } from "./views/library.js";
import { loadLabsDiagnostics, runLabPlotCompare, initLabsView } from "./views/labs.js";

// 后台批量翻译（调用 POST /api/paper/translate_batch + 轮询 SQLite 任务表）
let _batchPollTimer = null;
let _activeBatchJobId = null;

function _renderBatchBanner(job) {
  const banner = document.getElementById("lib-batch-banner");
  const textEl = document.getElementById("lib-batch-banner-text");
  if (!banner || !textEl) return;
  if (!job) {
    banner.setAttribute("data-active", "false");
    return;
  }
  banner.setAttribute("data-active", "true");
  const cur = job.current_page ? ` · 当前第 ${job.current_page} 页` : "";
  const err = job.failed ? ` · 失败 ${job.failed} 页` : "";
  textEl.textContent = `后台批量翻译 [${job.status}]: ${job.done}/${job.total} 页${cur}${err}`;
}

async function cancelBackendBatchTranslate() {
  if (!_activeBatchJobId) return;
  try {
    const res = await getJson(`/api/paper/translate_batch/${encodeURIComponent(_activeBatchJobId)}`, { method: "DELETE" });
    clearInterval(_batchPollTimer);
    _batchPollTimer = null;
    _renderBatchBanner(res.job);
    showToast("已取消后台批量翻译任务");
  } catch (e) {
    showToast("取消失败: " + (e.message || e), true);
  }
}

async function startBackendBatchTranslate() {
  const ps = getPdfState();
  if (!ps || !ps.relativePath || !ps.pages || !ps.pages.length) {
    showToast("请先在 PDF 双栏模式打开一篇论文", true);
    return;
  }
  const mode = ps.bilingual ? "blocks" : "page";
  const missing = ps.pages
    .filter((p) => (mode === "blocks" ? !p.blockTranslated : !p.translated))
    .map((p) => p.num);
  if (!missing.length) {
    showToast("当前论文所有页面均已完成翻译 ✓");
    return;
  }
  try {
    const res = await postJson("/api/paper/translate_batch", {
      paper_id: ps.paperId,
      relative_path: ps.relativePath,
      pages: missing,
      mode,
      model: "",
      force: false,
    });
    const job = res.job;
    _activeBatchJobId = job.job_id;
    _renderBatchBanner(job);
    showToast(`✅ 已启动后台批量翻译（共 ${missing.length} 页）`);
    clearInterval(_batchPollTimer);
    _batchPollTimer = setInterval(async () => {
      if (!_activeBatchJobId) return;
      try {
        const poll = await getJson(`/api/paper/translate_batch/${encodeURIComponent(_activeBatchJobId)}`);
        const j = poll.job;
        _renderBatchBanner(j);
        if (j.status !== "running") {
          clearInterval(_batchPollTimer);
          _batchPollTimer = null;
          showToast(j.status === "completed" ? `✅ 后台批量翻译完成（${j.done} 页）` : `后台批量任务结束: ${j.status}`);
        }
      } catch (e) {
        clearInterval(_batchPollTimer);
        _batchPollTimer = null;
      }
    }, 2500);
  } catch (e) {
    showToast("启动后台批量翻译失败: " + (e.message || e), true);
  }
}

// 绑定供 HTML inline 事件及模板字符串访问的全局入口
Object.assign(globalThis, {
  scrollToWork, toggleTheme, showToast, loadPdfJs, loadKatex,
  checkOrProbeModel, probeModelNow, toggleAiPanel, _aiTaskCancel,
  saveObsidianVaultName, loadRecentFeed, feedLoadMore, feedReveal, revealInExplorer, feedOpen, feedDelete,
  handleFetchPaper, handleSavePaper, refreshPaperSubfolderList, updatePaperSavePathPreview,
  handleCreateRepro, handleGenerateWeekly, handleSaveTroubleshoot, handlePasteTextFallback,
  switchWorkbenchView, loadLibraryCatalog, loadLabsDiagnostics, runLabPlotCompare,
  openShortcutsModal, closeShortcutsModal,
  openReaderModal, closeReaderModal, switchReaderMode, setReaderZoom,
  toggleZenMode, exitZenMode, toggleZenNav, toggleZenAux, toggleZenSingle, zenTogglePane,
  savePdfSnapshot, deleteSnapshot, showSnapshotModal, refreshSnapshotModal, openSnapshotById, closeSnapshotModal,
  _clearDiagLog, showDiagnoseModal, closeDiagnoseModal,
  togglePdfQueuePause, translatePdfPage, resetAndRetryPdfPage, buildReplacedPdf,
  toggleBilingualMode, toggleBilingualOrig, handleFetchFullText, loadVaultPdfOptions,
  openVaultPdf, handleUploadLocalPdf, selectSection, copySectionTranslation,
  translateSection, translateAllSections, exportBilingualNote, closeSyncModal, reloadAfterSync,
  releaseCloudPdfs, syncNow, onQAModelChange, retranslatePdfPage, retranslateAllPdfPages,
  openAnnotationEditor, closeAnnotationEditor, saveAnnotation, deleteCurrentAnnotation,
  toggleAnnoDrawer, closeAnnoDrawer,
  toggleReaderQAPanel, minimizeReaderQAPanel, _qaCollapsedHeadClick, openReaderQAPanel,
  clearQAQuotedText, _refineQA, triggerQAPreset, handleQAInputKeydown, submitReaderQA,
  exportCurrentQAToObsidian, copyLastQAAnswer, saveQAToTroubleshoot,
  askPaperQuestion, presetQuestion, crossSectionQA, presetCrossQA,
  startBackendBatchTranslate, cancelBackendBatchTranslate,
});

function _bootDesktopApp() {
  try {
    if (typeof loadRecentFeed === "function") loadRecentFeed();
  } catch (e) { console.warn("加载文献 feed 失败", e); }
  _applyTheme(document.documentElement.getAttribute("data-theme") || "light");
  _initHeaderScrollState();
  _wirePaperFormInputs();
  loadStats();
  fetchModelStatus();
  initReaderSelectionListener();
  _initZenMode();
  _initPdfWidthObserver();
  _restoreLocalState();
  _restoreFromHash();
  loadVaultPdfOptions();
  initReaderQAPanelDrag();
  initAnnotationFeatures();
  loadTranslationModelChoices();
  initLibraryView();
  initLabsView();
  initShortcuts();

  try {
    if (new URLSearchParams(location.search).get("sync_now") === "1") {
      setTimeout(() => syncNow(), 800);
    }
  } catch (e) {}

  try {
    let p = new URLSearchParams(location.search).get("open_pdf");
    if (!p && location.hash.indexOf("open_pdf=") >= 0) {
      p = decodeURIComponent(location.hash.split("open_pdf=")[1] || "");
    }
    if (p) openVaultPdf(p);
  } catch (e) { showToast("打开失败: " + e.message, true); }
}

if (document.readyState === "loading") {
  window.addEventListener("DOMContentLoaded", _bootDesktopApp);
} else {
  _bootDesktopApp();
}

export { _bootDesktopApp, startBackendBatchTranslate, cancelBackendBatchTranslate };
