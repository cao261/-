import { showToast, getJson, postJson, escapeHtml, escapeJsString, escapeJsAttr } from "../core/utils.js";
import { _wireModalDismissals } from "../core/overlay-esc.js";
import { _readerState } from "../state/store.js";
import { _READPOS_PANES, _readPosAnchor } from "../state/readpos.js";
import {
  openReaderModal, renderReader, switchReaderMode, openVaultPdf, handleFetchFullText,
  applyRestoredReaderPrefs, getReaderPrefsSnapshot, _readerMode, _pdfState, selectSection,
} from "../reader/pdf-reader.js";
import { applyRestoredQaPrefs, getQaPrefsSnapshot } from "../reader/qa-copilot.js";

    // ===================================================================
    // Helper layer: Obsidian vault helpers, recent-activity feed, the
    // cancellable LLM-ask machinery, the subfolder/filename save modal,
    // and the Markdown/KaTeX renderers.
    //
    // These used to live INSIDE loadStats()'s try{} block. That was two
    // bugs in one:
    //   1. `if (!res.ok) return;` ran before a single declaration, so a
    //      failing /api/stats left saveObsidianVaultName / renderRecentFeed
    //      / loadRecentFeed undefined -> ReferenceError from the HTML
    //      inline onchange handlers, and a dead feed.
    //   2. Declarations trapped in a block don't reliably reach global
    //      scope, which is what forced ~15 hand-written `window.X = X`
    //      exports. Those are kept below for compatibility, but they are
    //      no longer load-bearing.
    // ===================================================================

      // Obsidian integration: vault name → use obsidian://open?vault=...&file=...
      // Stored in localStorage so it persists across sessions.
      // Read order: live input box first (so edits show immediately),
      // then fall back to localStorage (so it survives reloads).
      // NOTE: these helpers are referenced from inline oninput/onchange
      // attributes, so they MUST be reachable from the global scope.
      function getObsidianVaultName() {
        const liveInput = document.getElementById("obsidian-vault-name");
        if (liveInput && liveInput.value && liveInput.value.trim()) {
          return liveInput.value.trim();
        }
        return (localStorage.getItem("obsidian_vault_name") || "").trim();
      }
      // Vault name validation: name must look like a real Obsidian vault —
      // length ≥ 3, must contain at least one letter (not pure digits),
      // no slashes / dots / colons (path-separator chars that would break
      // the obsidian://open?vault=NAME&file=PATH URL). Without these rules
      // the 🟢 status dot lit up for any non-empty string ("数字也变绿"
      // was the user's complaint).
      function isValidVaultName(name) {
        if (!name) return false;
        if (name.length < 3) return false;
        if (/[\/\\\.:]/.test(name)) return false;
        if (!/[A-Za-z\u4e00-\u9fff_-]/.test(name)) return false;
        return true;
      }
      function saveObsidianVaultName(v) {
        const trimmed = (v || "").trim();
        localStorage.setItem("obsidian_vault_name", trimmed);
        updateObsidianStatus();
        // Re-render feed so click handlers pick up the new vault name
        renderRecentFeed();
      }
      function updateObsidianStatus() {
        const dot = document.getElementById("obsidian-status-dot");
        const input = document.getElementById("obsidian-vault-name");
        const pillDot = document.getElementById("obsidian-pill-dot");
        if (input) input.value = getObsidianVaultName();
        const v = getObsidianVaultName();
        const valid = isValidVaultName(v);
        // ok = valid vault name · warn = something typed but rejected ·
        // idle = unset, feed falls back to opening in the browser
        const state = valid ? "ok" : (v ? "warn" : "idle");
        if (pillDot) pillDot.setAttribute("data-state", state);
        if (!dot) return;
        dot.textContent = valid ? "已连接 Obsidian" : (v ? "库名不合法" : "浏览器打开");
        dot.title = valid
          ? `点击将在 Obsidian 打开 (vault: ${v})`
          : (v
              ? `vault 名称不合法 (≥3 字符,需含字母/中文,不能含 / \\ : .) · 当前: "${v}"`
              : "留空 = 浏览器新标签打开");
      }
      /**
       * Build an open URL for a vault-relative path.
       * If an Obsidian vault name is configured, returns obsidian://open URL
       * which the OS will hand to Obsidian. Otherwise returns the
       * FastAPI vault-file endpoint URL.
       */
      function buildOpenUrl(relPath) {
        const vault = getObsidianVaultName();
        // Only build the obsidian:// URL when the vault name actually looks
        // valid — otherwise fall through to the browser endpoint so a typo
        // or pure-digit input doesn't open an empty / error page in Obsidian.
        if (vault && isValidVaultName(vault)) {
          // obsidian://open?vault=NAME&file=PATH  — Obsidian picks it up
          // via registered URL scheme on Windows / macOS / Linux.
          return `obsidian://open?vault=${encodeURIComponent(vault)}&file=${encodeURIComponent(relPath)}`;
        }
        return `/api/vault/file?relative_path=${encodeURIComponent(relPath)}`;
      }
      // Expose to window so inline oninput/onchange attributes can find them.
      window.getObsidianVaultName = getObsidianVaultName;
      window.saveObsidianVaultName = saveObsidianVaultName;
      window.updateObsidianStatus = updateObsidianStatus;
      window.buildOpenUrl = buildOpenUrl;

      // Render recent feed (called from loadDashboardStats and toggle change)
      // _feedState holds paginated feed data; we render slice(0, shownCount)
      // and append a "加载更多" pill when more items remain in memory.
      window._feedState = {
        items: [],         // literature rows fetched so far (server-paginated)
        filteredTotal: 0,  // total after include_qa filter (server reports)
        shownCount: 0,     // how many we've rendered so far
        pageSize: 5,       // batch size for "加载更多"
        offset: 0,         // server-side offset of the NEXT page to fetch
        hasMore: false,    // server says more rows exist beyond what we hold
      };
      function renderRecentFeed() {
        const feed = document.getElementById("recent-feed-container");
        const includeQA = document.getElementById("feed-include-qa")
          && document.getElementById("feed-include-qa").checked;
        const state = window._feedState;
        const items = state.items || [];
        if (items.length === 0) {
          feed.innerHTML = '<div style="font-size:13px; color:var(--text-subtle); padding: 12px 0;">'
            + (includeQA ? '暂无文献，请在左侧录入第一篇。' : '暂无非答疑文献。勾选「含 AI 答疑」可一并显示导出的答疑卡。')
            + '</div>';
          return;
        }
        // Render the leading slice [0, shownCount]
        const visible = items.slice(0, state.shownCount || state.pageSize);
        const rows = visible.map(item => {
          const relPath = String(item.path || "").replace(/\\/g, "/");
          const openUrl = relPath ? buildOpenUrl(relPath) : "";
          const isQA = /04_AI划词答疑/.test(relPath);
          const isBilingual = /双语精读/.test(item.title) || /双语精读/.test(relPath);
          const badge = isQA ? "AI 划词答疑" : isBilingual ? "双语精读" : "文献速览";
          const isObsidian = isValidVaultName(getObsidianVaultName());
          const titleHint = isObsidian
            ? `点击在 Obsidian 打开 (vault: ${escapeHtml(getObsidianVaultName())})`
            : "点击在浏览器新标签打开 Markdown 源文件";
          // The 📁/🌐/🗑️ buttons embed these values inside inline
          // onclick="fn('...')" handlers. escapeHtml turns ' into &#39;,
          // and the HTML parser resolves that back to ' BEFORE the
          // handler is compiled — which killed the button and let a
          // title like `x');alert(1);//` run arbitrary JS. escapeJsAttr
          // escapes for the JS literal *and* the surrounding attribute.
          const jsPath = escapeJsAttr(relPath);
          const jsTitle = escapeJsAttr(item.title);
          return `
          <div class="archive-item">
            <div style="cursor:pointer; flex:1; min-width: 0;" title="${titleHint}"
                 onclick="${openUrl ? `window.open('${escapeJsAttr(openUrl)}', '_blank')` : 'void(0)'}">
              <div class="archive-item-title">${escapeHtml(item.title)}</div>
              <div class="archive-item-meta">
                <span class="badge-terracotta">${escapeHtml(String(item.category).replace(/^\d+_/, ''))}</span>
                <span style="background:var(--card-bg-subtle); padding:1px 6px; border-radius:8px; font-size:10.5px;">${escapeHtml(badge)}</span>
                <span>${escapeHtml(item.mtime)}</span>
              </div>
            </div>
            <div class="archive-item-actions">
              <button onclick="event.stopPropagation(); feedReveal('${jsPath}')" title="在文件管理器中显示该文件">📁</button>
              <button onclick="event.stopPropagation(); feedOpen('${jsPath}')" title="浏览器新标签打开源文件">🌐</button>
              <button class="danger" onclick="event.stopPropagation(); feedDelete('${jsPath}', '${jsTitle}')" title="软删除(到回收站)">🗑️</button>
            </div>
          </div>
        `;}).join('');
        // "加载更多" reveals the rest of the page we already hold; the
        // "拉取下一页" button only shows when the server says there is
        // another page (has_more).
        const remaining = items.length - visible.length;
        const notLoaded = Math.max(0, (state.filteredTotal || 0) - (state.items || []).length);
        const more = remaining > 0
          ? `<button class="feed-loadmore" onclick="feedLoadMore()">加载更多 (剩余 ${remaining} 条 · 共 ${state.filteredTotal || items.length})</button>`
          : (state.hasMore
              ? `<button class="feed-loadmore" onclick="loadRecentFeed(true)">还有 ${notLoaded} 条未加载,点击拉取下一页 →</button>`
              : '');
        feed.innerHTML = rows + more;
      }
      async function loadRecentFeed(append = false) {
        const includeQA = document.getElementById("feed-include-qa")?.checked || false;
        // 状态对象可能还没建（初始化顺序问题）——就地补一个，别让这里抛出去
        const state = window._feedState || (window._feedState = {
          items: [], filteredTotal: 0, shownCount: 0, pageSize: 5,
          offset: 0, hasMore: false,
        });
        const PAGE = 50; // server page size; client-side paginates within a page
        // A fresh load (or a filter change) always restarts at offset 0.
        if (!append) state.offset = 0;
        try {
          const r = await fetch(`/api/vault/literature?limit=${PAGE}&offset=${state.offset || 0}&include_qa=${includeQA}`);
          if (!r.ok) throw new Error("HTTP " + r.status);
          const data = await r.json();
          const page = data.items || [];
          state.items = append ? (state.items || []).concat(page) : page;
          state.filteredTotal = data.filtered_total || 0;
          state.hasMore = !!data.has_more;
          // The next page starts right after the rows we just received.
          state.offset = (data.offset || 0) + page.length;
          if (!append) state.shownCount = state.pageSize;
          renderRecentFeed();
        } catch (err) {
          console.error("加载文献 feed 失败", err);
          showToast("加载文献列表失败: " + err.message, true);
        }
      }
      function feedLoadMore() {
        const s = window._feedState;
        s.shownCount = (s.shownCount || s.pageSize) + s.pageSize;
        renderRecentFeed();
      }
      async function revealInExplorer(relPath) {
  return feedReveal(relPath);
}
async function feedReveal(relPath) {
        try {
          const data = await postJson(`/api/vault/reveal?relative_path=${encodeURIComponent(relPath)}`);
          showToast("📁 已唤起文件管理器", false, 2400);
        } catch (err) {
          showToast("打开文件管理器失败: " + err.message, true);
        }
      }
      function feedOpen(relPath) {
        // Force browser-open even when Obsidian vault is configured
        window.open(`/api/vault/file?relative_path=${encodeURIComponent(relPath)}`, '_blank');
      }
      async function feedDelete(relPath, title) {
        const ok = confirm(`确定删除文献笔记?\n\n${title}\n\n文件将进入回收站 (send2trash),可手动恢复。`);
        if (!ok) return;
        try {
          const data = await getJson(`/api/vault/file?relative_path=${encodeURIComponent(relPath)}`, { method: "DELETE" });
          showToast(`🗑️ 已删除: ${data.path}${data.deleted_to === 'permanent' ? ' (永久)' : ''}`, false, 3000);
          // Refresh the feed and the dashboard stats card
          await loadRecentFeed();
          if (typeof loadStats === "function") loadStats();
        } catch (err) {
          showToast("删除失败: " + err.message, true);
        }
      }
      // Expose to window so inline onchange attribute on the QA toggle can find it.
      window.renderRecentFeed = renderRecentFeed;
      window.loadRecentFeed = loadRecentFeed;
      window.feedLoadMore = feedLoadMore;
      window.feedReveal = feedReveal;
      window.feedOpen = feedOpen;
      window.feedDelete = feedDelete;

      // ---------------------------------------------------------------
      // Vault save modal: pick subfolder + filename before exporting
      // bilingual notes or QA cards. Returns Promise<{subfolder, filename}|
      // null (user cancelled).
      //   opts.topic:    vault topic folder name (e.g. 01_Device_TCAD_器件仿真)
      //   opts.kind:     folder kind for the dropdown (literature_cards default)
      //   opts.defaultFilename: server-side default to show as placeholder
      // ---------------------------------------------------------------
      const _vaultSaveCache = {}; // kind -> {ts, folders}
      async function _listSubfoldersForKind(kind) {
        const cached = _vaultSaveCache[kind];
        if (cached && Date.now() - cached.ts < 30000) return cached.folders;
        try {
          const r = await fetch(`/api/paper/folders?topic_category=${encodeURIComponent(_vaultSaveModal.topic)}&kind=${encodeURIComponent(kind)}`);
          const data = await r.json();
          const folders = (data && data.folders) || [];
          _vaultSaveCache[kind] = { ts: Date.now(), folders };
          return folders;
        } catch (e) {
          return [];
        }
      }
      let _vaultSaveModal = { topic: "01_Device_TCAD_器件仿真" };
      async function showVaultSaveModal(opts) {
        const topic = opts.topic || "01_Device_TCAD_器件仿真";
        const kind = opts.kind || "literature_cards";
        const defaultFilename = opts.defaultFilename || "";
        _vaultSaveModal.topic = topic;
        const folders = await _listSubfoldersForKind(kind);

        // Build / reuse modal DOM
        let overlay = document.getElementById("vault-save-modal");
        if (!overlay) {
          overlay = document.createElement("div");
          overlay.id = "vault-save-modal";
          overlay.style.cssText = "position:fixed; inset:0; z-index:2000; background:rgba(0,0,0,0.42); display:flex; align-items:center; justify-content:center; z-index:2100;";
          overlay.innerHTML = `
            <div style="background:var(--bg-canvas); border-radius:12px; padding:22px 26px; width:min(560px, 92vw); box-shadow:0 18px 48px rgba(0,0,0,0.22); border:1px solid var(--card-border); font-family:var(--font-sans);">
              <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;">
                <div style="font-family:var(--font-serif); font-size:18px; font-weight:500;">📂 保存到 vault</div>
                <button type="button" data-vault-cancel style="background:transparent; border:0; font-size:20px; color:var(--text-muted); cursor:pointer;">×</button>
              </div>
              <div style="margin-bottom:12px;">
                <label style="display:block; font-size:12px; color:var(--text-muted); margin-bottom:4px;">子文件夹 (留空 = 上一层默认)</label>
                <div style="display:flex; gap:8px;">
                  <input list="vault-save-subfolder-list" type="text" data-vault-subfolder
                         style="flex:1; padding:6px 10px; font-size:13px; border:1px solid var(--card-border); border-radius:6px; background:var(--card-bg);"
                         placeholder="例如:周报01_GAA组会">
                  <datalist id="vault-save-subfolder-list"></datalist>
                  <button type="button" data-vault-refresh style="padding:4px 10px; font-size:11px; border:1px solid var(--card-border); border-radius:6px; background:var(--card-bg); cursor:pointer;">🔄</button>
                </div>
              </div>
              <div style="margin-bottom:12px;">
                <label style="display:block; font-size:12px; color:var(--text-muted); margin-bottom:4px;">文件名 (不带 .md 后缀)</label>
                <input type="text" data-vault-filename
                       style="width:100%; padding:6px 10px; font-size:13px; border:1px solid var(--card-border); border-radius:6px; background:var(--card-bg);"
                       placeholder="${escapeHtml(defaultFilename)}">
              </div>
              <div data-vault-path-preview
                   style="font-family:var(--font-mono); font-size:11px; color:var(--text-subtle); padding:8px 10px; background:var(--bg-canvas-subtle); border-radius:6px; word-break:break-all; min-height:18px; margin-bottom:14px;">
                保存路径会在此实时显示 →
              </div>
              <div style="display:flex; gap:8px; justify-content:flex-end;">
                <button type="button" data-vault-cancel style="padding:6px 16px; font-size:12px; border:1px solid var(--card-border); border-radius:6px; background:transparent; cursor:pointer;">取消</button>
                <button type="button" data-vault-confirm style="padding:6px 16px; font-size:12px; border:0; border-radius:6px; background:var(--accent-olive); color:white; cursor:pointer;">💾 确认保存</button>
              </div>
            </div>`;
          document.body.appendChild(overlay);
        } else {
          overlay.style.display = "flex";
        }

        // Populate inputs
        const subInput = overlay.querySelector("[data-vault-subfolder]");
        const fnInput = overlay.querySelector("[data-vault-filename]");
        const list = overlay.querySelector("#vault-save-subfolder-list");
        const preview = overlay.querySelector("[data-vault-path-preview]");
        subInput.value = "";
        fnInput.value = "";
        list.innerHTML = folders.map(f => `<option value="${f.replace(/"/g, '&quot;')}"></option>`).join("");
        fnInput.placeholder = defaultFilename;
        updatePreview();
        function updatePreview() {
          const sub = subInput.value.trim();
          const fn = fnInput.value.trim() || defaultFilename || "<自动>";
          const safeSub = sub.replace(/[\/\\]/g, "_");
          const safeFn = fn.replace(/[\\/:*?"<>|]/g, "_");
          preview.textContent = `📂 01_Literature/${topic}/<canonical>/${safeSub ? safeSub + "/" : ""}${safeFn}.md`;
          preview.style.color = (sub || fnInput.value.trim()) ? "var(--text-main)" : "var(--text-subtle)";
        }
        subInput.oninput = updatePreview;
        fnInput.oninput = updatePreview;

        // Wire buttons
        let resolveFn;
        const promise = new Promise(res => { resolveFn = res; });
        const close = _wireModalDismissals(overlay, (result) => {
          overlay.style.display = "none";
          resolveFn(result);
        });
        overlay.querySelectorAll("[data-vault-cancel]").forEach(b => b.onclick = () => close(null));
        overlay.querySelector("[data-vault-confirm]").onclick = () => {
          close({ subfolder: subInput.value.trim(), filename: fnInput.value.trim() });
        };
        overlay.querySelector("[data-vault-refresh]").onclick = async () => {
          delete _vaultSaveCache[kind];
          const fresh = await _listSubfoldersForKind(kind);
          list.innerHTML = fresh.map(f => `<option value="${f.replace(/"/g, '&quot;')}"></option>`).join("");
          showToast(`已刷新 ${fresh.length} 个子文件夹`);
        };
        // Focus first input
        setTimeout(() => subInput.focus(), 50);
        return promise;
      }
      window.showVaultSaveModal = showVaultSaveModal;

    /**
     * Dashboard counters. Deliberately narrow: it owns the /api/stats call
     * and nothing else, so a stats failure can never take the feed or the
     * vault switch down with it.
     */
    async function loadStats() {
      // 统计数字是看板的本体，必须先拿到手。
      // feed 和 Obsidian 状态都是附属品：它们失败（甚至抛 ReferenceError）
      // 都不能把数字带走 —— 之前这里是 `await loadRecentFeed()` 排在前面，
      // 它一抛错，数字就永远停在初始的 0（用户看到的「看板全是零」）。
      try {
        const res = await fetch("/api/stats");
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        // 看板显示「真实产出」：论文原文数 / 已译页数 / 划词批注数 / 复现工程数。
        // 之前这里只数 .md 笔记卡片等工具产物，而实际主线（翻论文、翻译、
        // 划词、批注）的产出没被计入 —— 看板永远是 0，看着像坏了。
        document.getElementById("stat-pdf").innerText = data.pdf_total ?? 0;
        document.getElementById("stat-pages").innerText = data.translated_pages ?? 0;
        document.getElementById("stat-anno").innerText = data.annotation_count ?? 0;
        document.getElementById("stat-repro").innerText = data.reproduction_total ?? 0;
        // 其余计数不占展示位，悬停提示里能看到
        const litItem = document.getElementById("stat-pdf-item");
        if (litItem) {
          litItem.title =
            `论文原文 ${data.pdf_total ?? 0} 篇 · 文献笔记 ${data.note_total ?? 0} 张\n` +
            `避坑沉淀 ${data.troubleshoot_count ?? 0} 条 · 周报归档 ${data.weekly_count ?? 0} 篇`;
        }
        console.log("[stats] 看板统计已更新", {
          pdf: data.pdf_total, pages: data.translated_pages,
          anno: data.annotation_count, repro: data.reproduction_total,
        });
      } catch (err) {
        console.error("加载统计数据失败", err);
      }
      // 附属品：失败也不影响上面的数字
      try {
        const fn = window.updateObsidianStatus || updateObsidianStatus;
        if (typeof fn === "function") fn();
      } catch (e) { console.warn("更新 Obsidian 状态失败", e); }
      // ⚠ 这里**不再**拉 feed（2026-10-27 性能优化）。
      //   原来 loadStats() 末尾 `await loadRecentFeed()`，而首屏的
      //   _bootDashboard 又自己调了一次 → **首屏打两次同一个请求**
      //   （实测 /api/vault/literature 被请求 2 次：52ms 那次 + 273ms 那次）。
      //   更糟的是那个 await 让 feed **串行阻塞**在 stats 后面：
      //   第一批请求 51ms 发出、161ms 才回来，第二批（stats 等）200ms 才开始。
      //
      //   → feed 归 _bootDashboard 管；loadStats 只负责数字。
      //     需要刷新 feed 的地方（删除笔记后）自己显式调 loadRecentFeed()。
    }

    async function handleFetchPaper() {
      const idInput = document.getElementById("paper-id-input").value.trim();
      if (!idInput) {
        showToast("请先输入 arXiv ID 或 DOI 识别码", true);
        return;
      }
      const btn = document.getElementById("btn-fetch-paper");
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span>检索中...</span>';
      }

      try {
        const result = await postJson("/api/paper/fetch", { identifier: idInput });

        const p = result.data;
        document.getElementById("paper-title").value = p.title || "";
        document.getElementById("paper-authors").value = p.authors || "";
        document.getElementById("paper-year").value = p.year || 2024;
        document.getElementById("paper-venue").value = p.venue || "";
        document.getElementById("paper-abstract").value = p.abstract || "";
        showToast(`成功提取: ${p.title.slice(0, 28)}...`);
      } catch (err) {
        showToast("抓取失败: " + err.message, true);
      } finally {
        btn.disabled = false;
        btn.innerHTML = '<span>智能抓取</span><span class="btn-pill-arrow">→</span>';
      }
    }

    async function handleSavePaper() {
      const title = document.getElementById("paper-title").value.trim();
      if (!title) {
        showToast("论文标题不能为空", true);
        return;
      }
      const subfolder = document.getElementById("paper-subfolder").value.trim();
      const filenameOverride = document.getElementById("paper-filename").value.trim();
      const rawId = document.getElementById("paper-id-input").value.trim();
      const topic = document.getElementById("paper-category").value;
      const year = parseInt(document.getElementById("paper-year").value) || 2024;
      const payload = {
        title: title,
        authors: document.getElementById("paper-authors").value.trim(),
        year: year,
        venue: document.getElementById("paper-venue").value.trim(),
        topic_category: topic,
        status: document.getElementById("paper-status").value,
        tags: document.getElementById("paper-tags").value.trim(),
        abstract: document.getElementById("paper-abstract").value.trim(),
        doi: rawId.startsWith("10.") ? rawId : "",
        arxiv: rawId.includes("arxiv") || /^\d{4}\.\d{4,5}/.test(rawId) ? rawId : "",
        subfolder: subfolder,
        filename_override: filenameOverride,
      };

      const btn = document.querySelector('button[onclick="handleSavePaper()"]');
      const original = btn ? btn.innerHTML : null;
      if (btn) { btn.disabled = true; btn.innerHTML = '<span>⏳ 保存中...</span>'; }
      try {
        const result = await postJson("/api/paper/save", payload);
        showToast(`✅ 文献卡片已归档: [${result.folder}/${result.file_name}]`, false, 5000);
        // Refresh subfolder list so the new folder (if any) shows in datalist
        refreshPaperSubfolderList();
        loadStats();
      } catch (err) {
        showToast("保存失败: " + err.message, true);
      } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = original; }
      }
    }

    /**
     * Refresh the subfolder datalist for the currently selected topic_category.
     * Cached lightly in memory so we don't refetch on every keystroke.
     */
    const _paperSubfolderCache = {}; // topic -> {ts, folders}
    async function refreshPaperSubfolderList() {
      const topic = document.getElementById("paper-category").value;
      const cached = _paperSubfolderCache[topic];
      const stale = !cached || (Date.now() - cached.ts > 30000); // 30s ttl
      if (!stale) return _renderSubfolderOptions(cached.folders);
      try {
        const r = await fetch(`/api/paper/folders?topic_category=${encodeURIComponent(topic)}&kind=literature_cards`);
        const data = await r.json();
        const folders = (data && data.folders) || [];
        _paperSubfolderCache[topic] = { ts: Date.now(), folders };
        _renderSubfolderOptions(folders);
      } catch (e) {
        // best-effort; ignore (preview is a UX nicety, not a hard requirement)
      }
    }
    function _renderSubfolderOptions(folders) {
      const list = document.getElementById("paper-subfolder-list");
      if (!list) return;
      list.innerHTML = (folders || []).map(f => `<option value="${f.replace(/"/g, '&quot;')}"></option>`).join("");
    }

    /**
     * Live preview of the file path the user is about to save into.
     * Uses a lightweight client-side slug that mirrors the server's default filename.
     */
    function updatePaperSavePathPreview() {
      const topic = document.getElementById("paper-category").value;
      const sub = document.getElementById("paper-subfolder").value.trim();
      const title = document.getElementById("paper-title").value.trim();
      const year = parseInt(document.getElementById("paper-year").value) || 2024;
      const fn = document.getElementById("paper-filename").value.trim();
      const subPath = sub ? sub.replace(/[\/\\]/g, "_") : "";
      const safeTitle = title
        ? title.replace(/[\\/:*?"<>|]/g, "_").replace(/\s+/g, " ").trim()
        : "<title>";
      const defaultBase = `${year}_${safeTitle.slice(0, 30)}`;
      const finalBase = fn ? fn.replace(/\.md$/i, "") : defaultBase;
      const parts = ["01_Literature", topic, "03_文献速览卡片"];
      if (subPath) parts.push(subPath);
      parts.push(`${finalBase}.md`);
      const el = document.getElementById("paper-save-path-preview");
      if (el) {
        el.textContent = "📂 " + parts.join("/");
        el.style.color = sub || fn ? "var(--text-main)" : "var(--text-subtle)";
      }
    }
    window.refreshPaperSubfolderList = refreshPaperSubfolderList;
    window.updatePaperSavePathPreview = updatePaperSavePathPreview;

    // Re-render preview & refresh subfolder list when topic category changes.
    // Use change event (datalist-driven select needs input + change).
    //
    // ⚠ 不能只挂 DOMContentLoaded：用户常常带 `?open_pdf=` / `#paper=` **直达
    //   打开**，那时 DOMContentLoaded 早已触发过（readyState 已是 interactive），
    //   监听器再注册就是死的 → 分类变更后子文件夹列表和保存路径预览都不动。
    //   这就是 AGENTS.md §4.1 那个"看板全是 0"的同型缺陷，别改回去。
    function _wirePaperFormInputs() {
      const cat = document.getElementById("paper-category");
      if (cat) {
        cat.addEventListener("change", () => { refreshPaperSubfolderList(); updatePaperSavePathPreview(); });
        cat.addEventListener("input", () => { refreshPaperSubfolderList(); updatePaperSavePathPreview(); });
      }
      const yr = document.getElementById("paper-year");
      if (yr) yr.addEventListener("input", updatePaperSavePathPreview);
      const tt = document.getElementById("paper-title");
      if (tt) tt.addEventListener("input", updatePaperSavePathPreview);
      // Initial render
      refreshPaperSubfolderList();
      updatePaperSavePathPreview();
    }
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", _wirePaperFormInputs);
    } else {
      _wirePaperFormInputs();
    }

    async function handleCreateRepro() {
      const name = document.getElementById("repro-name").value.trim();
      if (!name) {
        showToast("请输入复现工程代号", true);
        return;
      }
      const payload = {
        project_name: name,
        project_type: document.getElementById("repro-type").value,
        toolchain: document.getElementById("repro-toolchain").value.trim(),
        target_paper: document.getElementById("repro-paper").value.trim(),
        description: document.getElementById("repro-desc").value.trim()
      };

      // 防重复提交：双击会在磁盘上生成两份复现工程目录
      const btn = document.getElementById("btn-create-repro");
      if (btn) btn.disabled = true;
      try {
        const result = await postJson("/api/reproduction/create", payload);
        showToast(`✅ 复现工程已就绪: [02_Reproduction/${result.project_name}/]`);
        loadStats();
      } catch (err) {
        showToast("创建工程失败: " + err.message, true);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function handleGenerateWeekly() {
      // 防重复提交：双击会生成两份内容相同、时间戳不同的周报
      const btn = document.getElementById("btn-generate-weekly");
      if (btn) btn.disabled = true;
      try {
        const result = await postJson("/api/weekly/generate");
        showToast(`✅ 周报已生成至 Obsidian: [04_Weekly_Reports/${result.filename}]`);
        loadStats();
      } catch (err) {
        showToast("生成周报失败: " + err.message, true);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function handleSaveTroubleshoot() {
      const keyword = document.getElementById("tb-keyword").value.trim();
      const solution = document.getElementById("tb-solution").value.trim();
      if (!keyword || !solution) {
        showToast("请填写报错关键词与解决方案", true);
        return;
      }
      const payload = {
        tool_name: document.getElementById("tb-tool").value.trim() || "TCAD/EDA",
        category: document.getElementById("tb-category").value,
        error_keyword: keyword,
        raw_error: "Log recorded from editorial studio",
        root_cause: "Observed during simulation/experiment",
        solution: solution
      };

      // 防重复提交：双击会在 03_Knowledge 里写两条一模一样的踩坑记录
      const btn = document.getElementById("btn-save-troubleshoot");
      if (btn) btn.disabled = true;
      try {
        const result = await postJson("/api/troubleshoot/add", payload);
        showToast(`✅ 踩坑记录已沉淀至知识库: [03_Knowledge/踩坑与排错日记.md] (${result.keyword})`);
        document.getElementById("tb-keyword").value = "";
        document.getElementById("tb-solution").value = "";
        loadStats();
      } catch (err) {
        showToast("记录失败: " + err.message, true);
      } finally {
        if (btn) btn.disabled = false;
      }
    }


    // ---------------------------------------------------------------
    // 状态持久化 (刷新浏览器不丢失)
    // ---------------------------------------------------------------
    const LS_STATE_KEY = "microbench_state_v1";

    function _persistReaderState() {
      try {
        const params = new URLSearchParams();
        if (_pdfState && _pdfState.relativePath) {
          params.set("paper", _pdfState.relativePath);
          params.set("mode", "pdf");
          // 页码记**真实的当前页**（原来写死 "1" —— 地址栏里的 page 参数
          // 永远没用，§4.124）。页内偏移存在 localStorage 里，这里不放。
          const _rpAnchor = _readPosAnchor(_READPOS_PANES.pdf);
          params.set("page", String(_rpAnchor ? _rpAnchor.page : 1));
        } else if (_readerState && _readerState.pdfPath) {
          params.set("paper", _readerState.pdfPath);
          params.set("mode", _readerMode || "section");
          params.set("section", String(_readerState.activeSection || 0));
        } else {
          history.replaceState(null, "", window.location.pathname + window.location.search);
          return;
        }
        // Keep the ORIGINAL identifier (arXiv ID / DOI / PDF URL) next to the
        // vault path: "paper" is a vault-relative path that
        // /api/paper/fetch_full always rejects with 400, so restoring from
        // the hash needs the identifier to re-open the paper.
        const identifier = (_readerState && _readerState.identifier) || "";
        if (identifier) params.set("id", identifier);
        history.replaceState(null, "", "#" + params.toString());
      } catch (e) {}
    }

    // Does this string look like something /api/paper/fetch_full accepts
    // (arXiv id / DOI / http(s) URL)? Used by the hash restore to decide
    // between re-fetching the paper and falling back to PDF mode.
    function _looksLikePaperIdentifier(s) {
      const v = (s || "").trim();
      if (!v) return false;
      if (/^https?:\/\//i.test(v)) return true;
      if (/^10\.\d{4,9}\/\S+$/.test(v)) return true;                    // DOI
      if (/^arxiv[:\s]*\d{4}\.\d{4,5}(v\d+)?$/i.test(v)) return true;  // "arXiv:2401.01234"
      if (/^\d{4}\.\d{4,5}(v\d+)?$/i.test(v)) return true;             // 2401.01234
      if (/^[\w.\-]+\/\d{7}(v\d+)?$/i.test(v)) return true;            // cs/0301001
      return false;
    }

    async function _restoreFromHash() {
      try {
        const raw = window.location.hash.replace(/^#/, "");
        if (!raw) return;
        const params = new URLSearchParams(raw);
        const paper = params.get("paper");     // vault-relative path (legacy)
        const ident = params.get("id") || "";   // arXiv id / DOI / URL (new)
        const mode = params.get("mode") || "section";
        const sectionIdx = parseInt(params.get("section") || "0", 10);
        if (!paper && !ident) return;
        // 等待 reader 元素渲染好
        await new Promise(r => setTimeout(r, 350));
        const idInput = document.getElementById("paper-id-input");
        if (idInput) {
          idInput.value = ident || paper;
          idInput.dispatchEvent(new Event("input"));
        }
        // Only re-fetch when we stored a real identifier. The old code put
        // the vault path into the input and POSTed it as `identifier`,
        // which always failed with 400 + a "拉取失败" toast.
        if (ident && _looksLikePaperIdentifier(ident)) {
          if (typeof handleFetchFullText !== "function") {
            showToast("Reader 入口函数未实现,请手动重开论文", true);
            return;
          }
          await handleFetchFullText();
          // 跳到指定 section 或 PDF mode
          if (mode === "pdf") {
            if (typeof switchReaderMode === "function") switchReaderMode("pdf");
          } else if (sectionIdx > 0 && typeof selectSection === "function") {
            // 等 sections 填充
            setTimeout(() => selectSection(sectionIdx), 200);
          }
          return;
        }
        // No usable identifier → restore the PDF view from the stored
        // vault-relative path instead of asking the backend to "fetch" it.
        if (!paper) return;
        openReaderModal();
        _readerState.pdfPath = paper;
        if (mode === "pdf" && typeof switchReaderMode === "function") {
          switchReaderMode("pdf");
        } else {
          showToast("已从地址栏恢复本地 PDF (未记录论文标识,无法重新拉取全文)", false, 4200);
        }
      } catch (e) {
        console.error("[restoreFromHash]", e);
      }
    }

    function _persistLocalState() {
      try {
        const state = {
          ...getQaPrefsSnapshot(),
          ...getReaderPrefsSnapshot(),
        };
        localStorage.setItem(LS_STATE_KEY, JSON.stringify(state));
      } catch (e) {}
    }

    function _restoreLocalState() {
      try {
        const raw = localStorage.getItem(LS_STATE_KEY);
        if (!raw) return;
        const s = JSON.parse(raw);
        applyRestoredQaPrefs(s);
        applyRestoredReaderPrefs(s);
      } catch (e) {
        console.error("[restoreLocalState]", e);
      }
    }

    async function _restoreTranslationsFromCache() {
      if (!_readerState || !Array.isArray(_readerState.sections) || !_readerState.sections.length) return;
      try {
        const keys = _readerState.sections.map(s => ({
          section_key: s.section || s.id || "",
          text: s.text || "",
          model: "",
        }));
        const r = await fetch("/api/paper/translation_cache_batch_lookup", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ keys }),
        });
        if (!r.ok) return;
        const data = await r.json();
        const hits = data.hits || {};
        let restored = 0;
        _readerState.sections.forEach((sec, idx) => {
          const k = sec.section || sec.id || "";
          const hit = hits[k];
          if (hit && hit.translation) {
            _readerState.translations[idx] = hit;
            restored += 1;
          }
        });
        if (restored > 0) {
          console.log(`[batch restore] 恢复 ${restored}/${_readerState.sections.length} 节翻译`);
          if (typeof renderReader === "function") renderReader();
        }
      } catch (e) {
        console.error("[restoreTranslationsFromCache]", e);
      }
    }


    // ----- Paper Full-Text Reader (V1.4) -----
    async function handlePasteTextFallback() {
      const title = document.getElementById("paper-title").value.trim();
      const abstract = document.getElementById("paper-abstract").value.trim();
      if (!title) {
        showToast("请先输入论文标题", true);
        return;
      }
      const raw = prompt(
        `【粘贴全文文本】\n\n` +
        `论文: ${title}\n\n` +
        `请粘贴从 PDF 阅读器复制的全文文本 (Ctrl+A → Ctrl+C, 至少 100 字):`,
        ""
      );
      if (!raw || raw.trim().length < 100) {
        showToast("粘贴内容太短, 至少 100 字", true);
        return;
      }
      showToast("正在拆分章节...");
      try {
        const result = await postJson("/api/paper/split_text", { raw_text: raw });
        if (!result.sections || result.sections.length === 0) {
          throw new Error("未识别到任何章节, 请确认文本是否含章节标题");
        }
        _readerState.title = title;
        _readerState.sections = result.sections;
        _readerState.translations = {};
        _readerState.activeSection = 0;
        renderReader();
        openReaderModal();
        _persistReaderState();
        _restoreTranslationsFromCache();
        showToast(`✅ 已从粘贴文本识别 ${result.sections.length} 个章节`);
      } catch (err) {
        showToast("拆分失败: " + err.message, true);
      }
    }


    /**
     * Light-weight inline modal for the '存入避坑库' button on QA cards.
     * Lets the user pick category + adjust keyword in one click.
     *   defaultKeyword / defaultSolution / defaultTool seed the form.
     * Resolves to {tool, category, keyword, solution} or null on cancel.
     */
    async function showKeywordPickerModal(opts) {
      opts = opts || {};
      const categories = [
        "TCAD仿真收敛",
        "EDA环境与授权",
        "Linux与服务器",
        "Python与CUDA",
      ];
      let overlay = document.getElementById("keyword-picker-modal");
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.id = "keyword-picker-modal";
        overlay.style.cssText = "position:fixed; inset:0; z-index:2200; background:rgba(0,0,0,0.42); display:none; align-items:center; justify-content:center;";
        overlay.innerHTML = `
          <div style="background:var(--bg-canvas); border-radius:12px; padding:22px 26px; width:min(460px, 92vw); box-shadow:0 18px 48px rgba(0,0,0,0.22); border:1px solid var(--card-border); font-family:var(--font-sans);">
            <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;">
              <div style="font-family:var(--font-serif); font-size:17px; font-weight:500;">📑 存入避坑知识库</div>
              <button type="button" data-kp-cancel style="background:transparent; border:0; font-size:20px; color:var(--text-muted); cursor:pointer;">×</button>
            </div>
            <div style="margin-bottom:10px;">
              <label style="display:block; font-size:12px; color:var(--text-muted); margin-bottom:4px;">关键词 (一句话概括问题)</label>
              <input type="text" data-kp-keyword style="width:100%; padding:6px 10px; font-size:13px; border:1px solid var(--card-border); border-radius:6px; background:var(--card-bg);" placeholder="例如: Sentaurus SDevice RHS NaN">
            </div>
            <div style="margin-bottom:10px;">
              <label style="display:block; font-size:12px; color:var(--text-muted); margin-bottom:4px;">分类</label>
              <select data-kp-category style="width:100%; padding:6px 10px; font-size:13px; border:1px solid var(--card-border); border-radius:6px; background:var(--card-bg);"></select>
            </div>
            <div style="margin-bottom:14px;">
              <label style="display:block; font-size:12px; color:var(--text-muted); margin-bottom:4px;">工具 / 环境</label>
              <input type="text" data-kp-tool style="width:100%; padding:6px 10px; font-size:13px; border:1px solid var(--card-border); border-radius:6px; background:var(--card-bg);" placeholder="例如: Sentaurus TCAD 2023">
            </div>
            <div style="display:flex; gap:8px; justify-content:flex-end;">
              <button type="button" data-kp-cancel style="padding:6px 16px; font-size:12px; border:1px solid var(--card-border); border-radius:6px; background:transparent; cursor:pointer;">取消</button>
              <button type="button" data-kp-confirm style="padding:6px 16px; font-size:12px; border:0; border-radius:6px; background:var(--accent-olive); color:white; cursor:pointer;">📑 一键存入</button>
            </div>
          </div>`;
        document.body.appendChild(overlay);
      }
      overlay.style.display = "flex";

      const kwInput = overlay.querySelector("[data-kp-keyword]");
      const catSelect = overlay.querySelector("[data-kp-category]");
      const toolInput = overlay.querySelector("[data-kp-tool]");
      kwInput.value = opts.defaultKeyword || "";
      // 用 DOM API 而不是 innerHTML 拼 <option>：分类名一旦改成从接口取，
      // 未转义的拼接就是注入点（当前 categories 是硬编码常量，所以只是加固）。
      catSelect.innerHTML = "";
      categories.forEach(c => {
        const opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        catSelect.appendChild(opt);
      });
      catSelect.value = "TCAD仿真收敛";
      toolInput.value = opts.defaultTool || "";
      setTimeout(() => kwInput.focus(), 50);

      let resolveFn;
      const promise = new Promise(res => { resolveFn = res; });
      const close = _wireModalDismissals(overlay, (r) => { overlay.style.display = "none"; resolveFn(r); });
      overlay.querySelectorAll("[data-kp-cancel]").forEach(b => b.onclick = () => close(null));
      overlay.querySelector("[data-kp-confirm]").onclick = () => {
        const kw = kwInput.value.trim();
        if (!kw) { showToast("请填写关键词", true); return; }
        close({
          keyword: kw,
          category: catSelect.value,
          tool: toolInput.value.trim() || "TCAD/EDA",
          solution: opts.defaultSolution || "",
        });
      };
      return promise;
    }
    window.showKeywordPickerModal = showKeywordPickerModal;

let _workbenchView = "dashboard";
function switchWorkbenchView(viewName) {
  const valid = viewName === "library" || viewName === "labs" ? viewName : "dashboard";
  _workbenchView = valid;
  const views = {
    dashboard: document.getElementById("view-dashboard"),
    library: document.getElementById("view-library"),
    labs: document.getElementById("view-labs"),
  };
  Object.entries(views).forEach(([k, el]) => {
    if (!el) return;
    if (k === "dashboard") {
      el.style.display = valid === "dashboard" ? "" : "none";
    } else {
      el.setAttribute("data-active", k === valid ? "true" : "false");
    }
  });
  document.querySelectorAll("[data-wb-tab]").forEach((btn) => {
    btn.setAttribute("data-active", btn.getAttribute("data-wb-tab") === valid ? "true" : "false");
  });
  if (valid === "library" && typeof globalThis.loadLibraryCatalog === "function") {
    globalThis.loadLibraryCatalog(false);
  } else if (valid === "labs" && typeof globalThis.loadLabsDiagnostics === "function") {
    globalThis.loadLabsDiagnostics();
  }
}

export {
  getObsidianVaultName, isValidVaultName, saveObsidianVaultName, updateObsidianStatus, buildOpenUrl,
  renderRecentFeed, loadRecentFeed, feedLoadMore, feedReveal, revealInExplorer, feedOpen, feedDelete,
  showVaultSaveModal, loadStats, handleFetchPaper, handleSavePaper, refreshPaperSubfolderList, _renderSubfolderOptions, updatePaperSavePathPreview, _wirePaperFormInputs,
  handleCreateRepro, handleGenerateWeekly, handleSaveTroubleshoot,
  LS_STATE_KEY, _persistReaderState, _looksLikePaperIdentifier, _restoreFromHash, _persistLocalState, _restoreLocalState, _restoreTranslationsFromCache,
  handlePasteTextFallback, showKeywordPickerModal, switchWorkbenchView,
};
