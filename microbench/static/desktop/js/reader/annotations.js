import { showToast, escapeHtml, getJson, postJson } from "../core/utils.js";
import { _escBind, _escUnbind } from "../core/overlay-esc.js";
import { _pdfState, _pdfZoom } from "./pdf-reader.js";
import { _readerSelState } from "./qa-copilot.js";

    // 取当前划选在 PDF 原文侧的高亮矩形（归一化 0~1，随缩放还原）。
    // PDF.js 文本层是绝对定位的 span，不能往里插标记（会挤坏版面），
    // 所以只在画布上叠一层矩形。
    function _collectSelectionRects() {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return [];
      let range;
      try { range = sel.getRangeAt(0); } catch (e) { return []; }
      const rects = Array.from(range.getClientRects() || [])
        .filter(r => r.width > 1 && r.height > 1);
      if (!rects.length) return [];
      let node = range.startContainer;
      if (node && node.nodeType === 3) node = node.parentElement;
      const wrapper = node && node.closest ? node.closest(".pdf-page-canvas-wrapper") : null;
      if (!wrapper) return [];
      const wr = wrapper.getBoundingClientRect();
      if (!wr.width || !wr.height) return [];
      const out = [];
      for (const r of rects) {
        const x = (r.left - wr.left) / wr.width;
        const y = (r.top - wr.top) / wr.height;
        const w = r.width / wr.width;
        const h = r.height / wr.height;
        if (x < -0.02 || y < -0.02 || x > 1.02 || y > 1.02) continue;   // 选到了页面外
        out.push({
          x: Math.max(0, x), y: Math.max(0, y),
          w: Math.min(1, w), h: Math.min(1, h),
        });
      }
      return out;
    }

    // 在 PDF 原文侧画高亮（缩放重绘后会自动按比例还原）
    function drawAnnoOverlays(pageNum, canvasWrapper) {
      if (!canvasWrapper) return;
      let layer = canvasWrapper.querySelector(".anno-overlay-layer");
      if (!layer) {
        layer = document.createElement("div");
        layer.className = "anno-overlay-layer";
        canvasWrapper.appendChild(layer);
      }
      layer.innerHTML = "";
      const items = _annoState.items.filter(a => a.page === pageNum && a.rects && a.rects.length);
      items.forEach(a => {
        a.rects.forEach((r, i) => {
          const d = document.createElement("div");
          d.className = "anno-overlay";
          d.dataset.annoId = a.id;
          d.dataset.color = a.color || "yellow";
          d.style.left = (r.x * 100) + "%";
          d.style.top = (r.y * 100) + "%";
          d.style.width = (r.w * 100) + "%";
          d.style.height = (r.h * 100) + "%";
          d.title = a.note ? a.note.slice(0, 120) : "点击查看批注";
          layer.appendChild(d);
        });
        // 序号角标（放在第一段高亮的右上角）
        const first = a.rects[0];
        const badge = document.createElement("span");
        badge.className = "anno-overlay-num";
        badge.dataset.annoId = a.id;
        badge.textContent = String(_annoSeq(a.id));
        badge.style.left = (first.x * 100) + "%";
        badge.style.top = (first.y * 100) + "%";
        badge.title = a.note ? a.note.slice(0, 120) : "点击查看批注";
        layer.appendChild(badge);
      });

      // ⚠⚠ 手写笔迹（2026-10-28 用户实测：「手写没有任何作用」）。
      //   平板端手写存进批注的 `strokes` 字段（归一化坐标），每条笔迹自带页码：
      //     { page: 0, source:"ink", strokes:[ {page: 3, pts:[x,y,x,y,...]}, ... ] }
      //   ⚠⚠ **批注本身的 `page` 是 0**（一条手写批注可以跨多页），
      //     所以**不能**按 `a.page === pageNum` 过滤 —— 那会永远匹配不到
      //     → 要按 `strokes[].page` 判断笔迹属于哪一页。
      //   桌面端原来**只画 rects**（划词高亮）→ 完全忽略 strokes
      //   → 平板上画的东西同步到电脑后**一个字都看不到**。
      //   → 这里用 SVG polyline 画笔迹（不干扰原文 canvas，
      //     也不需要 dpr 处理 —— SVG 用 viewBox 缩放）。
      const NS = "http://www.w3.org/2000/svg";
      const svg = document.createElementNS(NS, "svg");
      svg.setAttribute("class", "anno-ink-svg");
      svg.setAttribute("preserveAspectRatio", "none");
      svg.setAttribute("viewBox", "0 0 1000 1000");
      let inkCount = 0;
      (_annoState.items || []).forEach(a => {
        (a.strokes || []).forEach(s => {
          const sgPage = (s && s.page != null) ? parseInt(s.page, 10) : NaN;
          // 页码归属：笔迹自带页码优先；没有就用批注的页码
          const target = isNaN(sgPage) ? parseInt(a.page, 10) : sgPage;
          if (target !== pageNum) return;
          const pts = (s && s.pts) ? s.pts : (Array.isArray(s) ? s : null);
          if (!pts || pts.length < 4) return;
          // ⚠ 数据形态有两种（平板历史版本）：
          //   ① [{page, pts:[x,y,x,y,...]}]  ← 现在这种
          //   ② [x,y,x,y,...]                  ← 早期直接是点序列
          const seq = [];
          for (let i = 0; i + 1 < pts.length; i += 2) {
            seq.push((Number(pts[i]) || 0) * 1000, (Number(pts[i + 1]) || 0) * 1000);
          }
          if (seq.length < 4) return;
          const poly = document.createElementNS(NS, "polyline");
          poly.setAttribute("points", seq.join(","));
          poly.setAttribute("class", "anno-ink-stroke");
          poly.setAttribute("data-anno-id", a.id || "");
          poly.setAttribute("fill", "none");
          poly.setAttribute("stroke", "#D8342A");
          poly.setAttribute("stroke-width", "4");
          poly.setAttribute("stroke-linecap", "round");
          poly.setAttribute("stroke-linejoin", "round");
          poly.setAttribute("vector-effect", "non-scaling-stroke");
          svg.appendChild(poly);
          inkCount++;
        });
      });
      if (inkCount) layer.appendChild(svg);
    }

    function drawAllAnnoOverlays() {
      document.querySelectorAll(".pdf-page-canvas-wrapper").forEach(w => {
        const block = w.closest(".pdf-page-block");
        const m = block && block.id.match(/(\d+)$/);
        if (m) drawAnnoOverlays(parseInt(m[1], 10), w);
      });
    }

    // PDF 页面标签上的批注计数（原文侧也有标识）
    function refreshPdfPageAnnoBadges() {
      document.querySelectorAll(".pdf-page-block").forEach(block => {
        const m = block.id.match(/(\d+)$/);
        if (!m) return;
        const pageNum = parseInt(m[1], 10);
        const count = _annoState.items.filter(a => a.page === pageNum).length;
        const label = block.querySelector(".pdf-page-label");
        if (!label) return;
        let badge = label.querySelector(".anno-badge");
        if (!count) { if (badge) badge.remove(); return; }
        if (!badge) {
          badge = document.createElement("button");
          badge.className = "anno-badge";
          label.appendChild(badge);
        }
        badge.textContent = "📝 " + count;
        const orphanN = _annoState.items.filter(a => a.page === pageNum && a.orphan).length;
        badge.title = "本页有 " + count + " 条批注"
          + (orphanN ? "（其中 " + orphanN + " 条位置已失效，笔记仍在）" : "")
          + " · 点击查看";
        badge.onclick = (e) => { e.stopPropagation(); openAnnoDrawer(pageNum); };
      });
    }


    // ===========================================================================
    // 阅读批注：在原文/译文上高亮一段，并写下自己的理解
    // ===========================================================================
    // 数据落在服务端 logs/_pdf_snapshots/annotations/<paperId>.json，
    // 跨会话保留 —— 下次点开同一篇论文，高亮和笔记自动回来。
    const _annoState = { paperId: null, items: [], editingId: null, color: "yellow", deleted: [] };
    const _ANNO_COLORS = [
      { id: "yellow", hex: "#FACC15" },
      { id: "green",  hex: "#4ADE80" },
      { id: "blue",   hex: "#60A5FA" },
      { id: "pink",   hex: "#F472B6" },
      { id: "purple", hex: "#C084FC" },
    ];

    function _annoPaperId() {
      return (_pdfState && _pdfState.paperId) || null;
    }

    function _annoSeq(id) {
      // 序号按创建顺序排，页面上显示成 ①②③
      const idx = _annoState.items.findIndex(a => a.id === id);
      return idx >= 0 ? idx + 1 : 0;
    }

    async function loadAnnotationsForPaper(paperId) {
      const pid = paperId || _annoPaperId();
      if (!pid) return;
      _annoState.paperId = pid;
      try {
        const r = await fetch("/api/pdf/annotations/load?paper_id=" + encodeURIComponent(pid));
        if (!r.ok) throw new Error("HTTP " + r.status);
        const data = await r.json();
        _annoState.items = Array.isArray(data.annotations) ? data.annotations : [];
        _annoState.deleted = [];
      } catch (e) {
        _annoState.items = [];
        _annoState.deleted = [];
      }
      refreshAnnoBadges();
      refreshPdfPageAnnoBadges();
      reapplyAnnotations();
      drawAllAnnoOverlays();
    }

    async function saveAnnotationsToServer() {
      const pid = _annoPaperId();
      if (!pid) return false;
      // ⚠ 409 = 共享文件正被**另一台设备**写（服务端的乐观 CAS 撞满了）。
      //   这是**可重试**的：服务端每次都会重新读、重新合并 → 重发同一条请求
      //   不会丢对方的数据。2026-11-17 全面审查 P1-高 修好服务端 CAS 顺序后
      //   加的：没有这层重试，跨设备同时标注会直接弹「保存失败」给用户。
      const body = JSON.stringify({
        paper_id: pid,
        annotations: _annoState.items,
        deleted: _annoState.deleted,
      });
      for (let attempt = 0; attempt < 3; attempt++) {
        try {
          const r = await fetch("/api/pdf/annotations/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            // deleted 一起发上去：服务端按 id 合并，别让另一台设备把已删的批注"复活"
            body: body,
          });
          if (r.status === 409) {
            // 冲突：等一下再发（服务端会重新读盘再合并）
            await new Promise((res) => setTimeout(res, 250 * (attempt + 1)));
            continue;
          }
          if (!r.ok) throw new Error("HTTP " + r.status);
          _annoState.deleted = [];
          return true;
        } catch (e) {
          showToast("批注保存失败: " + (e.message || e), true);
          return false;
        }
      }
      showToast("批注保存失败: 另一台设备正在写入同一份批注，请稍后再点一次保存", true);
      return false;
    }

    function renderAnnoColorPicker() {
      const row = document.getElementById("anno-color-row");
      if (!row) return;
      row.innerHTML = "";
      _ANNO_COLORS.forEach(c => {
        const b = document.createElement("button");
        b.className = "anno-swatch";
        b.style.background = c.hex;
        b.title = c.id;
        b.setAttribute("aria-pressed", _annoState.color === c.id ? "true" : "false");
        b.onclick = () => {
          _annoState.color = c.id;
          renderAnnoColorPicker();
          const mk = document.querySelector(".anno-mark.is-editing");
          if (mk) mk.dataset.color = c.id;
        };
        row.appendChild(b);
      });
    }

    function openAnnotationEditor(annoId) {
      const editor = document.getElementById("anno-editor");
      if (!editor) return;
      const quoteEl = document.getElementById("anno-quote");
      const noteEl = document.getElementById("anno-note");
      const titleEl = document.getElementById("anno-editor-title");
      const delBtn = document.getElementById("anno-delete-btn");
      const warnEl = document.getElementById("anno-orphan-warn");
      const sel = _readerSelState || {};

      if (annoId) {
        const a = _annoState.items.find(x => x.id === annoId);
        if (!a) return;
        _annoState.editingId = annoId;
        _annoState.color = a.color || "yellow";
        if (quoteEl) quoteEl.textContent = a.quote;
        if (noteEl) noteEl.value = a.note || "";
        if (titleEl) titleEl.textContent = "✍ 编辑批注";
        if (delBtn) delBtn.style.display = "inline-block";
        // 位置失效的标注：明确告诉用户"笔记还在，只是高亮找不到了"
        if (warnEl) warnEl.style.display = a.orphan ? "block" : "none";
      } else {
        const quote = (sel.text || "").trim();
        if (!quote) { showToast("请先划选一段文字再标注", true); return; }
        _annoState.editingId = null;
        _annoState.color = "yellow";
        if (quoteEl) quoteEl.textContent = quote;
        if (noteEl) noteEl.value = "";
        if (titleEl) titleEl.textContent = "✍ 新建批注";
        if (delBtn) delBtn.style.display = "none";
        if (warnEl) warnEl.style.display = "none";
      }

      // 位置：贴着划词气泡（没有气泡就放屏幕中间偏上）
      const bubble = document.getElementById("reader-selection-bubble");
      const br = (bubble && bubble.style.display !== "none") ? bubble.getBoundingClientRect() : null;
      const x = br ? br.left : Math.max(12, (window.innerWidth - 360) / 2);
      const y = br ? br.bottom + 8 : Math.max(12, (window.innerHeight - 320) / 2);
      editor.style.left = Math.max(12, Math.min(x, window.innerWidth - 372)) + "px";
      editor.style.top = Math.max(12, Math.min(y, window.innerHeight - 300)) + "px";
      editor.style.display = "flex";
      // ⚠ 2026-11-25 审计 P1：登记进 Esc 栈（否则 Esc 直接关掉整个阅读器）
      _escBind(editor, closeAnnotationEditor);
      renderAnnoColorPicker();
      if (noteEl) noteEl.focus();
    }

    function closeAnnotationEditor() {
      const editor = document.getElementById("anno-editor");
      if (editor) editor.style.display = "none";
      _escUnbind(editor);
      _annoState.editingId = null;
    }

    async function saveAnnotation() {
      const pid = _annoPaperId();
      if (!pid) { showToast("请先打开一篇论文再标注", true); return; }
      const quote = (document.getElementById("anno-quote")?.textContent || "").trim();
      const note = (document.getElementById("anno-note")?.value || "").trim();
      if (!quote) return;
      const now = new Date().toISOString();
      const sel = _readerSelState || {};
      const page = (sel.pageNum != null) ? sel.pageNum : 0;
      const id = _annoState.editingId
        || ("a" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6));

      const existing = _annoState.items.find(x => x.id === id);
      // ⚠ 2026-11-25 审计 P1：编辑**已有**批注时，原来拿「页面上当前的选区」
      //   （sel.rects）去**覆盖** existing.rects —— 只要用户在批注编辑器
      //   打开期间在别处划了一下字，这条已有批注的高亮位置就被改掉
      //   （甚至被清成空）→ 批注和引文对不上，另一台设备同步过去也是错的。
      // → 编辑已有批注：**坐标一律沿用原批注**；
      //   只有「新建」批注才取当前选区。
      const rects = existing
        ? (existing.rects || [])
        : ((sel.rects && sel.rects.length) ? sel.rects : []);
      if (existing) {
        existing.quote = quote;
        existing.note = note;
        existing.color = _annoState.color;
        existing.rects = rects;
        existing.updated_at = now;
      } else {
        _annoState.items.push({
          id, page,
          source: sel.source || "translation",
          quote, note, color: _annoState.color, rects,
          created_at: now, updated_at: now,
        });
      }
      const ok = await saveAnnotationsToServer();
      if (!ok) return;
      closeAnnotationEditor();
      refreshAnnoBadges();
      refreshPdfPageAnnoBadges();
      reapplyAnnotations();
      drawAllAnnoOverlays();
      showToast(existing ? "批注已更新 ✓" : "批注已保存 · 下次打开还在 ✓");
    }

    async function deleteCurrentAnnotation() {
      const id = _annoState.editingId;
      if (!id) return;
      const before = _annoState.items.length;
      _annoState.items = _annoState.items.filter(a => a.id !== id);
      if (_annoState.items.length === before) return;
      // 记下删除的 id，保存时一并告诉服务端（跨设备合并用）
      if (_annoState.deleted.indexOf(id) === -1) _annoState.deleted.push(id);
      refreshAnnoBadges();
      refreshPdfPageAnnoBadges();
      reapplyAnnotations();
      drawAllAnnoOverlays();
      const ok = await saveAnnotationsToServer();
      closeAnnotationEditor();
      if (ok) showToast("批注已删除");
    }

    // 取当前划选作为「引文」。两个坑：
    //  1. KaTeX 的 MathML 副本用户根本看不见（position:absolute + clip），
    //     但 sel.toString() 会把它一起吐出来（还会带上重复的字母和换行）。
    //  2. 公式要用它的 **TeX 源** 表示，而不是渲染后的样子 ——
    //     因为重放高亮时匹配的是"渲染前"的 DOM（占位符里存的就是 TeX 源），
    //     两者才能对上。
    function _currentSelectionQuote() {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return "";
      let frag;
      try { frag = sel.getRangeAt(0).cloneContents(); }
      catch (e) { return (sel.toString() || "").trim(); }
      frag.querySelectorAll(".katex-mathml").forEach(n => n.remove());
      frag.querySelectorAll(".katex").forEach(k => {
        const ann = k.querySelector("annotation[encoding='application/x-tex']");
        const tex = ann ? ann.textContent
                        : ((k.querySelector(".katex-html") || {}).textContent || "");
        try { k.replaceWith(document.createTextNode(tex)); } catch (e) {}
      });
      return (frag.textContent || "").replace(/[ \t]+/g, " ").replace(/\n+/g, " ").trim();
    }

    // 公式的"统一表示"：一律用 TeX 源（annotation 里的原文）。
    // 这样引文既能在 KaTeX 渲染前的 DOM（占位符里就是 TeX 源）匹配，
    // 也能在渲染后的 DOM 匹配 —— 否则 α_ME 这类公式的批注重开后就找不到位置了。
    function _annoTexOf(ph) {
      const ann = ph.querySelector("annotation[encoding='application/x-tex']");
      if (ann) return ann.textContent;
      if (ph.querySelector(".katex")) return null;   // 渲染了但取不到源，放弃
      return ph.textContent;
    }

    // 把容器里的可见文本拼成一条"虚拟行"：普通文本原样，公式用 TeX 源替代。
    function _annoVirtualText(container) {
      let v = "";
      const pieces = [];
      const seen = new Set();
      const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (!node.nodeValue) return NodeFilter.FILTER_REJECT;
          const p = node.parentElement;
          if (!p) return NodeFilter.FILTER_REJECT;
          // MathML 副本用户看不见，不能算进来
          if (p.closest(".katex-mathml, .anno-mark, script, style")) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      let node;
      while ((node = walker.nextNode())) {
        const p = node.parentElement;
        const ph = p.closest(".math-placeholder");
        if (ph) {
          if (seen.has(ph)) continue;
          seen.add(ph);
          const tex = _annoTexOf(ph);
          if (tex == null) continue;
          v += tex;
          pieces.push({ node: node, ph: ph, tex: true, vStart: v.length - tex.length, vEnd: v.length });
          continue;
        }
        const t = node.nodeValue;
        v += t;
        pieces.push({ node: node, ph: null, tex: false, vStart: v.length - t.length, vEnd: v.length });
      }
      return { text: v, pieces: pieces };
    }

    // 把某页译文里的引文包进 <mark>。引文可能横跨多个文本节点（中间夹着公式），
    // 所以要先拼成虚拟行定位，再映射回真实节点。
    function _wrapTextWithMark(container, quote, id, color) {
      if (!container || !quote || quote.length < 2) return false;
      const { text, pieces } = _annoVirtualText(container);
      if (!text) return false;
      const idx = text.indexOf(quote);
      if (idx === -1) return false;
      const end = idx + quote.length;
      let first = null, last = null;
      for (const p of pieces) {
        if (p.vEnd > idx && !first) first = p;
        if (p.vEnd >= end) { last = p; break; }
      }
      if (!first || !last) return false;

      const range = document.createRange();
      try {
        if (first.tex) range.setStartBefore(first.ph);
        else range.setStart(first.node, Math.max(0, idx - first.vStart));
        if (last.tex) range.setEndAfter(last.ph);
        else range.setEnd(last.node, Math.max(0, end - last.vStart));
      } catch (err) {
        return false;
      }
      const mark = document.createElement("mark");
      mark.className = "anno-mark";
      mark.dataset.annoId = id;
      mark.dataset.color = color || "yellow";
      mark.title = "点击查看 / 编辑这条批注";
      try {
        // extract + insert 可以跨元素边界；surroundContents 不行
        const frag = range.extractContents();
        mark.appendChild(frag);
        range.insertNode(mark);
      } catch (err) {
        return false;
      }
      const sup = document.createElement("sup");
      sup.className = "anno-num";
      sup.textContent = String(_annoSeq(id));
      mark.appendChild(sup);
      return true;
    }

    function _clearAnnoMarks(container) {
      if (!container) return;
      container.querySelectorAll(".anno-mark").forEach(mark => {
        const sup = mark.querySelector(".anno-num");
        if (sup) sup.remove();
        const parent = mark.parentNode;
        if (!parent) return;
        while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
        parent.removeChild(mark);
        parent.normalize();
      });
    }

    function applyAnnotationsToPage(pageNum, container) {
      if (!container) return 0;
      let applied = 0;
      _annoState.items.forEach(a => {
        if (a.page !== pageNum) return;
        if (_wrapTextWithMark(container, a.quote, a.id, a.color)) {
          applied++;
          a.orphan = false;
        } else {
          // 译文侧标注（没有 PDF 坐标）遇到重译 → 引文对不上，位置失效。
          // **记录必须留着**（用户写的笔记不能凭空消失），只是高亮找不到了。
          // PDF 侧标注有 rects，高亮画在 PDF 画布上，不受重译影响 → 不算失效。
          a.orphan = !(a.rects && a.rects.length);
        }
      });
      return applied;
    }

    function reapplyAnnotations() {
      document.querySelectorAll(".pdf-translation-block").forEach(block => {
        const m = block.id.match(/(\d+)$/);
        if (!m) return;
        const textEl = block.querySelector(".page-text");
        if (!textEl) return;
        _clearAnnoMarks(textEl);
        applyAnnotationsToPage(parseInt(m[1], 10), textEl);
      });
      // 章节视图（没有页码，page=0）
      const secEl = document.getElementById("reader-translation");
      if (secEl) {
        _clearAnnoMarks(secEl);
        applyAnnotationsToPage(0, secEl);
      }
      // 页块可能是这次才渲染出来的（快照恢复 / 排队翻译），
      // 徽标必须跟着重算一次，否则高亮有了、页头的「📝 N」却是空的
      refreshAnnoBadges();
    }

    function refreshAnnoBadges() {
      document.querySelectorAll(".pdf-translation-block").forEach(block => {
        const m = block.id.match(/(\d+)$/);
        if (!m) return;
        const pageNum = parseInt(m[1], 10);
        const count = _annoState.items.filter(a => a.page === pageNum).length;
        const head = block.querySelector(".page-num");
        if (!head) return;
        let badge = head.querySelector(".anno-badge");
        if (!count) { if (badge) badge.remove(); return; }
        if (!badge) {
          badge = document.createElement("button");
          badge.className = "anno-badge";
          badge.onclick = (e) => { e.stopPropagation(); openAnnoDrawer(pageNum); };
          const slot = head.querySelector(".anno-badge-slot");
          (slot || head).appendChild(badge);
        }
        badge.textContent = "📝 " + count;
        const orphanN = _annoState.items.filter(a => a.page === pageNum && a.orphan).length;
        badge.title = "本页有 " + count + " 条批注"
          + (orphanN ? "（其中 " + orphanN + " 条位置已失效，笔记仍在）" : "")
          + " · 点击查看";
      });
    }

    function openAnnoDrawer(pageNum) {
      const drawer = document.getElementById("anno-drawer");
      const list = document.getElementById("anno-drawer-list");
      const countEl = document.getElementById("anno-drawer-count");
      if (!drawer || !list) return;
      const items = _annoState.items.slice().sort((a, b) => (a.page || 0) - (b.page || 0));
      if (countEl) countEl.textContent = items.length ? "(" + items.length + " 条)" : "";
      list.innerHTML = "";
      if (!items.length) {
        list.innerHTML = '<div class="anno-empty">还没有批注。<br>在左侧 PDF 或右侧译文里划选一段文字，点「✍ 标注笔记」即可。</div>';
      } else {
        items.forEach(a => {
          const el = document.createElement("div");
          el.className = "anno-item";
          el.dataset.page = String(a.page || 0);
          if (a.orphan) el.classList.add("anno-item-orphan");
          const color = _ANNO_COLORS.find(c => c.id === a.color);
          el.innerHTML =
            '<div class="anno-item-quote" style="--anno-line:' + ((color && color.hex) || "#FACC15") + '">'
            + escapeHtml(a.quote) + '</div>'
            + (a.orphan ? '<div class="anno-item-orphan-tip">⚠ 位置失效（该页重译后原文变了）· 笔记已保留</div>' : '')
            + (a.note ? '<div class="anno-item-note">' + escapeHtml(a.note) + '</div>' : '<div class="anno-item-note" style="color:var(--fg-subtle);font-style:italic;">（只高亮，没有笔记）</div>')
            + '<div class="anno-item-meta"><span>' + (a.page > 0 ? "第 " + a.page + " 页" : "章节视图") + '</span><span>' + escapeHtml((a.updated_at || a.created_at || "").slice(0, 10)) + '</span></div>';
          el.onclick = () => {
            if (a.page > 0) {
              const block = document.getElementById("pdf-trans-block-" + a.page);
              if (block) block.scrollIntoView({ behavior: "smooth", block: "center" });
            }
            openAnnotationEditor(a.id);
          };
          list.appendChild(el);
        });
      }
      drawer.style.display = "flex";
      // ⚠ 2026-11-25 审计 P1：登记进 Esc 栈（否则 Esc 直接关掉整个阅读器）
      _escBind(drawer, closeAnnoDrawer);
      if (pageNum) {
        // 定位到**当前页**的批注（不是每次都从第一页的第一条开始）：
        // 本页的第一条 → 本页没有就落在下一页的第一条 → 再没有就退回第一条。
        let target = null;
        const els = Array.from(list.querySelectorAll(".anno-item"));
        for (const el of els) {
          const p = parseInt(el.dataset.page || "0", 10);
          if (p === pageNum) { target = el; break; }
          if (!target && p > pageNum) target = el;
        }
        if (!target) target = els[0] || null;
        if (target) target.scrollIntoView({ block: "start" });
      }
    }

    function closeAnnoDrawer() {
      const drawer = document.getElementById("anno-drawer");
      if (drawer) drawer.style.display = "none";
      _escUnbind(drawer);
    }

    function toggleAnnoDrawer(pageNum) {
      const drawer = document.getElementById("anno-drawer");
      if (!drawer) return;
      if (drawer.style.display === "flex") { closeAnnoDrawer(); return; }
      openAnnoDrawer(pageNum);
    }

    function initAnnotationFeatures() {
      // ⚠ 监听器只能挂一次：这个函数在**每次打开论文**时都会被调用，
      //   而 document 级的 click 监听器挂一次就常驻 → 开 N 篇论文就有 N 个
      //   监听器，每次点批注都会触发 N 次 openAnnotationEditor。
      if (window.__mbAnnoFeaturesInited) return;
      window.__mbAnnoFeaturesInited = true;
      // 点高亮 → 编辑对应批注
      document.addEventListener("click", (e) => {
        const mark = e.target.closest(".anno-mark");
        if (mark) {
          e.preventDefault();
          e.stopPropagation();
          openAnnotationEditor(mark.dataset.annoId);
        }
      });
      // 译文内容变化（翻译完成 / 快照恢复）后自动重放高亮
      const list = document.getElementById("pdf-translations-list");
      if (list && list.dataset.annoObs !== "1") {
        list.dataset.annoObs = "1";
        // ⚠ 这里**必须防抖**，否则就是自激循环（2026-10-27 审计 CRITICAL-1，已实测复现）：
        //   reapplyAnnotations 自己会改 DOM（_clearAnnoMarks 清标记 + _wrapTextWithMark
        //   重新包 <mark>），而这个 DOM 正在被观察 →
        //   改 DOM → 触发 observer → 再排一次 setTimeout → 再改 DOM → …
        //   实测：只要有一条能匹配上的批注，就会以 ~10.7 次/秒 无限重放，永不停止。
        // 后果不是"有点卡"，而是**每 80ms 洗一次译文栏的文本选区** →
        //   用户的划词选区被清空 → 划词答疑读不到 getSelection() → 核心工作流断掉。
        //
        // 两条防线：
        //   ① clearTimeout 真防抖（旧的写法是"每次变更都排队"，不是防抖）；
        //   ② 用户正在划词时**不动 DOM**（选区非折叠就跳过这一轮）。
        let _annoReplayTimer = null;
        const obs = new MutationObserver(() => {
          if (!_annoState.items.length) return;
          clearTimeout(_annoReplayTimer);
          _annoReplayTimer = setTimeout(() => {
            _annoReplayTimer = null;
            const sel = window.getSelection();
            if (sel && !sel.isCollapsed && sel.rangeCount) return;  // 用户正在划词 → 跳过
            reapplyAnnotations();
          }, 200);
        });
        obs.observe(list, { childList: true, subtree: true, characterData: true });
      }
    }


export {
  _collectSelectionRects, drawAnnoOverlays, drawAllAnnoOverlays, refreshPdfPageAnnoBadges,
  _annoState, _ANNO_COLORS, _annoPaperId, _annoSeq, loadAnnotationsForPaper, saveAnnotationsToServer,
  renderAnnoColorPicker, openAnnotationEditor, closeAnnotationEditor, saveAnnotation, deleteCurrentAnnotation,
  _currentSelectionQuote, _annoTexOf, _annoVirtualText, _wrapTextWithMark, _clearAnnoMarks,
  applyAnnotationsToPage, reapplyAnnotations, refreshAnnoBadges, openAnnoDrawer, closeAnnoDrawer, toggleAnnoDrawer, initAnnotationFeatures,
};
