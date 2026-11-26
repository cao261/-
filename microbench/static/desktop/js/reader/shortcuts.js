// 全局与阅读器快捷键面板（? 呼出，统一接入 _escBind / _escUnbind 栈）
import { _escBind, _escUnbind } from "../core/overlay-esc.js";
import { setReaderZoom, getPdfState } from "./pdf-reader.js";
import { toggleZenMode } from "./pdf-reader.js";

function closeShortcutsModal() {
  const m = document.getElementById("shortcuts-modal");
  if (m) m.style.display = "none";
  _escUnbind(m);
}

function openShortcutsModal() {
  const m = document.getElementById("shortcuts-modal");
  if (!m) return;
  m.style.display = "flex";
  _escBind(m, closeShortcutsModal);
}

function _isTypingTarget(target) {
  if (!target) return false;
  const tag = (target.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || !!target.isContentEditable;
}

function initShortcuts() {
  if (globalThis.__mbShortcutsInited) return;
  globalThis.__mbShortcutsInited = true;

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      if (typeof globalThis.switchWorkbenchView === "function") {
        globalThis.switchWorkbenchView("library");
      }
      const box = document.getElementById("lib-search-input");
      if (box) setTimeout(() => box.focus(), 40);
      return;
    }

    if (_isTypingTarget(e.target) || e.ctrlKey || e.metaKey || e.altKey) return;

    if (e.key === "?" || (e.shiftKey && e.key === "/")) {
      e.preventDefault();
      const m = document.getElementById("shortcuts-modal");
      if (m && m.style.display === "flex") closeShortcutsModal();
      else openShortcutsModal();
      return;
    }

    const reader = document.getElementById("reader-modal");
    const readerOpen = reader && reader.style.display === "flex";
    if (!readerOpen) {
      if (e.key === "1" && typeof globalThis.switchWorkbenchView === "function") {
        globalThis.switchWorkbenchView("dashboard");
      } else if (e.key === "2" && typeof globalThis.switchWorkbenchView === "function") {
        globalThis.switchWorkbenchView("library");
      } else if (e.key === "3" && typeof globalThis.switchWorkbenchView === "function") {
        globalThis.switchWorkbenchView("labs");
      }
      return;
    }

    if (getPdfState()) {
      if (e.key === "=" || e.key === "+") setReaderZoom(1);
      else if (e.key === "-") setReaderZoom(-1);
      else if (e.key === "0") setReaderZoom("fit");
    }
  });
}

export {
  openShortcutsModal,
  closeShortcutsModal,
  initShortcuts,
};
