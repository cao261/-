// 文献图书馆视图：基于 SQLite WAL 全库索引的检索、状态标签编辑与直开精读
import { showToast, getJson, postJson, escapeHtml } from "../core/utils.js";
import { openVaultPdf } from "../reader/pdf-reader.js";
import { buildOpenUrl, revealInExplorer } from "./dashboard.js";

const _libState = {
  q: "",
  kind: "all",
  category: "",
  status: "",
  items: [],
  total: 0,
  loading: false,
  timer: null,
};

function _libSetChipActive(groupSel, attr, value) {
  document.querySelectorAll(groupSel).forEach((btn) => {
    const match = (btn.getAttribute(attr) || "") === (value || "");
    btn.setAttribute("data-active", match ? "true" : "false");
  });
}

async function loadLibraryCatalog(forceRefresh = false) {
  const grid = document.getElementById("lib-results-grid");
  const summary = document.getElementById("lib-results-summary");
  if (!grid) return;

  _libState.loading = true;
  if (summary && !summary.textContent.trim()) {
    summary.textContent = "正在检索本地库…";
  }

  try {
    const params = new URLSearchParams();
    if (_libState.q) params.set("q", _libState.q);
    if (_libState.kind) params.set("kind", _libState.kind);
    if (_libState.category) params.set("category", _libState.category);
    if (_libState.status) params.set("status", _libState.status);
    params.set("limit", "150");
    if (forceRefresh) params.set("refresh", "true");

    const data = await getJson(`/api/vault/search?${params.toString()}`);
    _libState.items = Array.isArray(data.items) ? data.items : [];
    _libState.total = Number(data.total) || 0;

    if (summary) {
      const idx = data.indexed || {};
      summary.textContent = `匹配 ${_libState.total} 项（已索引 ${idx.papers || 0} 篇原文 · ${idx.notes || 0} 篇笔记）`;
    }
    renderLibraryCards();
  } catch (e) {
    if (summary) summary.textContent = "检索失败：" + (e.message || e);
    grid.innerHTML = `<div class="empty-state">无法加载文献索引：${escapeHtml(e.message || String(e))}</div>`;
  } finally {
    _libState.loading = false;
  }
}

function renderLibraryCards() {
  const grid = document.getElementById("lib-results-grid");
  if (!grid) return;
  if (!_libState.items.length) {
    grid.innerHTML = `<div class="empty-state">没有匹配的文献或笔记 —— 换个关键词或清空筛选条件试试</div>`;
    return;
  }

  const statuses = ["待精读", "精读中", "已精读", "已复现"];
  grid.innerHTML = _libState.items.map((it) => {
    const isPdf = it.kind === "pdf";
    const rel = it.relative_path || "";
    const relAttr = escapeHtml(rel);
    const titleHtml = escapeHtml(it.title || rel);
    const catHtml = escapeHtml(it.category || "未分类");
    const mtimeHtml = escapeHtml((it.mtime || "").replace("T", " ").slice(0, 16));
    const sizeText = isPdf && it.size ? ` · ${(it.size / 1024).toFixed(0)} KB` : "";
    const tags = Array.isArray(it.tags) ? it.tags : [];
    const tagsHtml = tags.map((t) => `<span class="lib-tag">${escapeHtml(t)}</span>`).join("");

    let controlsHtml = "";
    if (isPdf) {
      controlsHtml = `
        <button type="button" class="btn-pill chip" data-lib-open-pdf="${relAttr}">双栏精读</button>
        <button type="button" class="btn-pill-outline chip" data-lib-reveal="${relAttr}">定位</button>
      `;
    } else {
      const opts = ["", ...statuses].map((st) => {
        const label = st || "未设状态";
        const sel = (it.status || "") === st ? " selected" : "";
        return `<option value="${escapeHtml(st)}"${sel}>${escapeHtml(label)}</option>`;
      }).join("");
      const openHref = escapeHtml(buildOpenUrl(rel));
      controlsHtml = `
        <select class="lib-status-sel" data-lib-status="${relAttr}" aria-label="精读状态">${opts}</select>
        <div class="wrap gap-1">
          <button type="button" class="btn-pill-outline chip" data-lib-tags="${relAttr}">+ 标签</button>
          <a class="btn-pill-outline chip" href="${openHref}" target="_blank" rel="noopener">打开笔记</a>
          <button type="button" class="btn-pill-outline chip" data-lib-reveal="${relAttr}">定位</button>
        </div>
      `;
    }

    return `
      <article class="lib-card">
        <div>
          <div class="lib-card-head">
            <div class="lib-card-title">${titleHtml}</div>
            <span class="lib-kind-pill" data-kind="${isPdf ? "pdf" : "note"}">${isPdf ? "PDF" : "NOTE"}</span>
          </div>
          <div class="lib-card-meta">
            <span>${catHtml}</span>
            <span>· ${mtimeHtml}${sizeText}</span>
          </div>
        </div>
        ${it.snippet ? `<div class="lib-card-excerpt">${escapeHtml(it.snippet)}</div>` : ""}
        ${tagsHtml ? `<div class="lib-tag-row">${tagsHtml}</div>` : ""}
        <div class="lib-actions">${controlsHtml}</div>
      </article>
    `;
  }).join("");
}

async function _updateLibNoteMeta(relPath, patch) {
  try {
    const res = await postJson("/api/vault/paper_meta", {
      relative_path: relPath,
      ...patch,
    });
    const hit = _libState.items.find((x) => x.relative_path === relPath);
    if (hit) {
      const nextStatus = typeof res.status === "string" ? res.status : res.paper_status;
      if (typeof nextStatus === "string") hit.status = nextStatus;
      if (Array.isArray(res.tags)) hit.tags = res.tags;
    }
    showToast("✅ 文献元数据已同步写入笔记");
    renderLibraryCards();
  } catch (e) {
    showToast("更新失败: " + (e.message || e), true);
  }
}

function initLibraryView() {
  const searchInput = document.getElementById("lib-search-input");
  const catSel = document.getElementById("lib-category-filter");
  const refreshBtn = document.getElementById("lib-refresh-btn");
  const grid = document.getElementById("lib-results-grid");
  if (!grid || grid.dataset.bound === "1") return;
  grid.dataset.bound = "1";

  if (searchInput) {
    searchInput.addEventListener("input", () => {
      _libState.q = searchInput.value.trim();
      clearTimeout(_libState.timer);
      _libState.timer = setTimeout(() => loadLibraryCatalog(false), 220);
    });
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        clearTimeout(_libState.timer);
        _libState.q = searchInput.value.trim();
        loadLibraryCatalog(false);
      }
    });
  }
  if (catSel) {
    catSel.addEventListener("change", () => {
      _libState.category = catSel.value;
      loadLibraryCatalog(false);
    });
  }
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => loadLibraryCatalog(true));
  }

  document.querySelectorAll("[data-lib-kind]").forEach((btn) => {
    btn.addEventListener("click", () => {
      _libState.kind = btn.getAttribute("data-lib-kind") || "all";
      _libSetChipActive("[data-lib-kind]", "data-lib-kind", _libState.kind);
      loadLibraryCatalog(false);
    });
  });

  document.querySelectorAll("[data-lib-status-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      _libState.status = btn.getAttribute("data-lib-status-filter") || "";
      _libSetChipActive("[data-lib-status-filter]", "data-lib-status-filter", _libState.status);
      loadLibraryCatalog(false);
    });
  });

  grid.addEventListener("click", (e) => {
    const openBtn = e.target.closest("[data-lib-open-pdf]");
    if (openBtn) {
      const rel = openBtn.getAttribute("data-lib-open-pdf");
      if (rel) openVaultPdf(rel);
      return;
    }
    const revBtn = e.target.closest("[data-lib-reveal]");
    if (revBtn) {
      const rel = revBtn.getAttribute("data-lib-reveal");
      if (rel) revealInExplorer(rel);
      return;
    }
    const tagBtn = e.target.closest("[data-lib-tags]");
    if (tagBtn) {
      const rel = tagBtn.getAttribute("data-lib-tags");
      const item = _libState.items.find((x) => x.relative_path === rel);
      const cur = (item && Array.isArray(item.tags) ? item.tags : []).join(", ");
      const next = prompt("编辑研究方向标签（英文逗号分隔）：", cur);
      if (next !== null) {
        const tags = next.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
        _updateLibNoteMeta(rel, { tags });
      }
    }
  });

  grid.addEventListener("change", (e) => {
    const sel = e.target.closest("[data-lib-status]");
    if (!sel) return;
    const rel = sel.getAttribute("data-lib-status");
    if (rel) _updateLibNoteMeta(rel, { status: sel.value });
  });
}

export {
  loadLibraryCatalog,
  renderLibraryCards,
  initLibraryView,
};
