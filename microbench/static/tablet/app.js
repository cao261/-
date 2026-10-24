/* ===================================================================
   MicroBench 平板工作台 — 主逻辑
   -------------------------------------------------------------------
   本地优先：笔记全部存在 IndexedDB，任何操作都不依赖网络。
   同步：Android Chrome 用 File System Access API 直连 vault 目录，
        真·双向读写；其他浏览器降级为「vault 包导入 / 变更包导出」。
   AI：平板直连 MiniMax，key 只存本地 IndexedDB，不随笔记同步。
   =================================================================== */
(function () {
  "use strict";

  // ------------------------------------------------------------------
  // 小工具
  // ------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  // HTML 实体用 String.fromCharCode 拼，而不是写字面量 "&" ——
  // 这个文件在某些写入通道里会被整体 HTML 反转义一次，实体字面量会被
  // 啃成裸的 & " < >，直接把 JS 语法弄坏（曾因此报 missing ) after
  // argument list）。用码点构造就没有这个风险。
  const E_AMP = String.fromCharCode(38);
  const E_LT = String.fromCharCode(60);
  const E_GT = String.fromCharCode(62);
  const E_QUOT = String.fromCharCode(34);
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, E_AMP + "amp;")
    .replace(/</g, E_LT + "lt;")
    .replace(/>/g, E_GT + "gt;")
    .replace(/"/g, E_QUOT + "quot;");

  /* ------------------------------------------------------------------
     HTML 消毒
     ------------------------------------------------------------------
     笔记正文会被塞进 innerHTML，所以外来 HTML 必须处理。但又不能一把
     全转义 —— 01_Literature 的双语精读笔记里存着 <h2 class="md-h2">
     这类原始 HTML 块，全转义的话那些笔记会变成一堆标签文本。

     所以走白名单：常见排版标签放行，其余标签连同其内容丢弃，
     事件处理器和 javascript:/data:text/html 一律清掉。
     ------------------------------------------------------------------ */
  const ALLOWED_TAGS = new Set([
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr",
    "strong", "b", "em", "i", "u", "s", "del", "mark", "small", "sub", "sup",
    "code", "pre", "blockquote", "ul", "ol", "li",
    "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
    "a", "img", "span", "div", "figure", "figcaption",
    "details", "summary", "dl", "dt", "dd", "abbr", "cite", "q",
  ]);

  // 连内容一起丢掉的标签
  const DROP_WITH_CONTENT = /<\s*(script|style|iframe|object|embed|link|meta|base|form|svg|math|noscript|template)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>/gi;
  const DROP_SELF_CLOSING = /<\s*(script|style|iframe|object|embed|link|meta|base|form|svg|math|noscript|template)\b[^>]*\/?>/gi;

  function sanitizeHtml(s) {
    let out = String(s == null ? "" : s);
    out = out.replace(DROP_WITH_CONTENT, "").replace(DROP_SELF_CLOSING, "");
    out = out.replace(/<\/?([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g,
      (m, rawTag, attrs) => {
        const tag = String(rawTag).toLowerCase();
        if (!ALLOWED_TAGS.has(tag)) {
          // 不在白名单 -> 当普通文本显示
          return m.replace(/</g, E_LT).replace(/>/g, E_GT);
        }
        let a = String(attrs || "");
        // 事件处理器
        a = a.replace(/\son[a-zA-Z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "");
        // 危险协议
        a = a.replace(/\s(href|src|xlink:href|formaction|action)\s*=\s*("|')?\s*(javascript|vbscript|data)\s*:[^"'>\s]*("|')?/gi,
          " $1=\"#\"");
        return m.replace(rawTag, tag).replace(attrs, a);
      });
    return out;
  }

  /** 链接协议白名单 —— 只有这些能进 href */
  function safeHref(url) {
    const u = String(url || "").trim();
    if (/^(javascript|vbscript|data):/i.test(u)) return "#";
    if (/^[a-z][a-z0-9+.-]*:/i.test(u) && !/^(https?|mailto|tel):/i.test(u)) return "#";
    return u;
  }

  /** djb2 — 用来判断一份内容有没有变过。刻意不用 crypto.subtle：
   *  file:// 在部分浏览器里不是 secure context，subtle 会直接不存在。 */
  function hash(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
    return h.toString(36);
  }

  function toast(msg, isError) {
    let t = $("tb-toast");
    if (!t) {
      t = el("div", "tb-toast");
      t.id = "tb-toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.setAttribute("data-error", isError ? "1" : "0");
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 3200);
  }

  function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // ------------------------------------------------------------------
  // IndexedDB
  // ------------------------------------------------------------------
  const DB_NAME = "mb_tablet";
  const DB_VER = 2;
  let db = null;

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VER);
      req.onupgradeneeded = () => {
        const d = req.result;
        if (!d.objectStoreNames.contains("notes")) {
          const s = d.createObjectStore("notes", { keyPath: "id" });
          s.createIndex("group", "group");
        }
        if (!d.objectStoreNames.contains("meta")) {
          d.createObjectStore("meta", { keyPath: "k" });
        }
        // 原文 PDF 的本地缓存 —— 没网的时候也能翻论文
        if (!d.objectStoreNames.contains("pdfcache")) {
          d.createObjectStore("pdfcache", { keyPath: "id" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(new Error("IndexedDB 被另一个标签页占着"));
    });
  }

  function tx(store, mode, fn) {
    return new Promise((resolve, reject) => {
      const t = db.transaction(store, mode);
      const s = t.objectStore(store);
      let out;
      try { out = fn(s); } catch (e) { reject(e); return; }
      t.oncomplete = () => resolve(out && out.result !== undefined ? out.result : out);
      t.onerror = () => reject(t.error);
      t.onabort = () => reject(t.error);
    });
  }

  const DB = {
    async allNotes() {
      return new Promise((resolve, reject) => {
        const t = db.transaction("notes", "readonly");
        const s = t.objectStore("notes");
        const req = s.getAll();
        req.onsuccess = () => resolve(req.result || []);
        req.onerror = () => reject(req.error);
      });
    },
    putNote(n) { return tx("notes", "readwrite", (s) => s.put(n)); },
    putNotes(list) {
      return new Promise((resolve, reject) => {
        const t = db.transaction("notes", "readwrite");
        const s = t.objectStore("notes");
        list.forEach((n) => s.put(n));
        t.oncomplete = () => resolve();
        t.onerror = () => reject(t.error);
      });
    },
    deleteNote(id) { return tx("notes", "readwrite", (s) => s.delete(id)); },
    clearNotes() { return tx("notes", "readwrite", (s) => s.clear()); },
    getMeta(k, dflt) {
      return new Promise((resolve, reject) => {
        const t = db.transaction("meta", "readonly");
        const req = t.objectStore("meta").get(k);
        req.onsuccess = () => resolve(req.result ? req.result.v : dflt);
        req.onerror = () => reject(req.error);
      });
    },
    setMeta(k, v) { return tx("meta", "readwrite", (s) => s.put({ k, v })); },

    // ---- 原文 PDF 的本地缓存 ----
    cachePdf(id, blob) {
      return tx("pdfcache", "readwrite", (s) => s.put({ id, blob, at: Date.now() }));
    },
    getCachedPdf(id) {
      return new Promise((resolve) => {
        try {
          const t = db.transaction("pdfcache", "readonly");
          const req = t.objectStore("pdfcache").get(id);
          req.onsuccess = () => resolve(req.result ? req.result.blob : null);
          req.onerror = () => resolve(null);
        } catch (e) {
          resolve(null);
        }
      });
    },
    cachedPdfIds() {
      return new Promise((resolve) => {
        try {
          const t = db.transaction("pdfcache", "readonly");
          const req = t.objectStore("pdfcache").getAllKeys();
          req.onsuccess = () => resolve(req.result || []);
          req.onerror = () => resolve([]);
        } catch (e) {
          resolve([]);
        }
      });
    },
  };

  // ------------------------------------------------------------------
  // Markdown 渲染（与桌面端 formatMarkdownLite 的数学规则保持一致）
  //   $$..$$ 先于 $..$；单行且 <=60 字符且无 \begin..\end 的 $$ 降级为行内
  // ------------------------------------------------------------------
  const INLINE_MAX = 60;
  const MATH_TOKEN = (i) => `@@MBK${i}@@`;

  /** Obsidian 的 YAML frontmatter 保留在原文里（编辑后要能原样写回），
   *  但渲染时不能让人看到 title: "..." 这种裸字段。 */
  function stripFrontmatter(src) {
    return src.replace(/^\uFEFF?---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/, "");
  }

  function extractMath(text) {
    const formulas = [];
    // 注意回调签名：String.replace 的第一个参数是**完整匹配字符串**
    // （是个 string，不是 match 数组），第二个才是第一个捕获组。
    // 写成 (m) => m[1] 拿到的是匹配串的第二个字符 —— 公式会被截成一个 "$"。
    const display = (match, body) => {
      const t = body.trim();
      const isDisplay = t.indexOf("\n") >= 0 ||
        /\\(begin|end)\s*\{[^}]+\}/.test(t) || t.length > INLINE_MAX;
      formulas.push({ tex: body, display: isDisplay });
      return MATH_TOKEN(formulas.length - 1);
    };
    const inline = (match, body) => {
      formulas.push({ tex: body, display: false });
      return MATH_TOKEN(formulas.length - 1);
    };
    // 代码块与行内代码先保护起来，里面的 $ 不是公式
    const parts = text.split(/(```[\s\S]*?```|`[^`\n]+`)/);
    for (let i = 0; i < parts.length; i += 2) {
      parts[i] = parts[i]
        .replace(/\$\$([\s\S]+?)\$\$/g, display)
        .replace(/(?<!\$)\$([^$\n]+?)\$(?!\$)/g, inline);
    }
    return { text: parts.join(""), formulas };
  }

  function renderMath(formulas) {
    if (!formulas.length) return "";
    if (typeof katex === "undefined") {
      // KaTeX 没加载出来时退回源码，至少不吞掉公式
      return formulas.map((f) =>
        `<code class="math-error">${esc(f.tex.trim())}</code>`).join("");
    }
    return formulas.map((f) => {
      try {
        return katex.renderToString(f.tex, {
          displayMode: f.display, throwOnError: false, strict: "ignore",
          output: "html",
          macros: {
            "\\RR": "\\mathbb{R}", "\\NN": "\\mathbb{N}", "\\ZZ": "\\mathbb{Z}",
            "\\CC": "\\mathbb{C}", "\\ket": "\\left|#1\\right\\rangle",
            "\\bra": "\\left\\langle#1\\right|"
          }
        });
      } catch (e) {
        return `<code class="math-error">${esc(f.tex.trim())}</code>`;
      }
    }).join("");
  }

  function renderMarkdown(src) {
    if (!src) return "";
    const { text, formulas } = extractMath(stripFrontmatter(String(src)));
    const lines = text.split("\n");
    let out = "";
    let i = 0;

    const inline = (s) => {
      // 先把行内代码抽出来占位 —— 代码里的内容不该再被当成 HTML 或
      // 强调符号处理，最后原样放回（转义后）。
      const codes = [];
      s = String(s).replace(/`([^`\n]+)`/g, (m, c) => {
        codes.push(c);
        return "\u0000C" + (codes.length - 1) + "\u0000";
      });
      // 外来 HTML 过一遍白名单消毒
      s = sanitizeHtml(s);
      // 图片
      s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
        (m, alt, src2) => `<img alt="${alt}" src="${safeHref(src2)}">`);
      // 链接
      s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
        (m, t2, href) => `<a href="${safeHref(href)}" rel="noopener noreferrer">${t2}</a>`);
      // Obsidian 双链
      s = s.replace(/\[\[([^\]]+)\]\]/g, (m, raw) => {
        const target = raw.split("|")[0].trim();
        const alias = (raw.split("|")[1] || target).trim();
        return `<span class="wikilink-missing">${alias}</span>`;
      });
      // 粗斜体
      s = s.replace(/\*\*\*([^*]+)\*\*\*/g, "<strong><em>$1</em></strong>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
      // 代码放回
      s = s.replace(/\u0000C(\d+)\u0000/g, (m, i) => "<code>" + esc(codes[+i]) + "</code>");
      return s;
    };

    while (i < lines.length) {
      const line = lines[i];

      // 代码块
      if (/^```/.test(line)) {
        const lang = line.slice(3).trim();
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        out += `<pre><code data-lang="${esc(lang)}">${esc(buf.join("\n"))}</code></pre>`;
        continue;
      }
      // 标题
      let m = /^(#{1,6})\s+(.*)$/.exec(line);
      if (m) {
        const lv = m[1].length;
        out += `<h${lv}>${inline(m[2])}</h${lv}>`;
        i++; continue;
      }
      // 分隔线
      if (/^\s*([-*_])\s*\1\s*\1[\s\S]*$/.test(line) && /^-{3,}$|^\*{3,}$|^_{3,}$/.test(line.trim())) {
        out += "<hr>"; i++; continue;
      }
      // 表格
      if (line.indexOf("|") >= 0 && i + 1 < lines.length &&
          /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1]) && lines[i + 1].indexOf("-") >= 0) {
        const cells = (r) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
        const head = cells(lines[i]);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim()) {
          rows.push(cells(lines[i])); i++;
        }
        out += "<table><thead><tr>" +
          head.map((c) => `<th>${inline(c)}</th>`).join("") +
          "</tr></thead><tbody>" +
          rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
          "</tbody></table>";
        continue;
      }
      // 引用
      if (/^\s*>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, "")); i++;
        }
        out += `<blockquote>${renderMarkdown(buf.join("\n"))}</blockquote>`;
        continue;
      }
      // 列表（含任务列表）
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        const ordered = /^\s*\d+\./.test(line);
        const items = [];
        let cur = null;
        while (i < lines.length) {
          const l = lines[i];
          const lm = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(l);
          if (!lm) {
            if (cur && l.trim() && !/^\s*([-*+]|\d+\.)\s+/.test(l) && /^\s{2,}/.test(l)) {
              cur.lines.push(l.replace(/^\s{2,}/, ""));
              i++; continue;
            }
            break;
          }
          const indent = lm[1].length;
          if (indent >= 2 && cur) { cur.lines.push(lm[3]); i++; continue; }
          cur = { lines: [lm[3]] };
          items.push(cur);
          i++;
        }
        const tag = ordered ? "ol" : "ul";
        const isTask = !ordered && items.length && items.every((x) => /^\[[ xX]\]\s?/.test(x.lines[0] || ""));
        out += `<${tag}${isTask ? ' class="task-list"' : ""}>` +
          items.map((x) => {
            let txt = x.lines.join("\n");
            let done = false;
            if (/^\[[ xX]\]\s?/.test(txt)) { done = /^\[[xX]\]/.test(txt); txt = txt.replace(/^\[[ xX]\]\s?/, ""); }
            const inner = x.lines.length > 1
              ? inline(txt.split("\n")[0]) + renderMarkdown(x.lines.slice(1).join("\n"))
              : inline(txt);
            return `<li${done ? ' class="done"' : ""}>${inner}</li>`;
          }).join("") + `</${tag}>`;
        continue;
      }
      // 空行
      if (!line.trim()) { i++; continue; }
      // 段落
      const buf = [];
      while (i < lines.length && lines[i].trim() &&
             !/^(#{1,6})\s/.test(lines[i]) && !/^```/.test(lines[i]) &&
             !/^\s*>\s?/.test(lines[i]) && !/^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        buf.push(lines[i]); i++;
      }
      if (buf.length) out += `<p>${inline(buf.join("\n")).replace(/\n/g, "<br>")}</p>`;
      else i++;
    }

    // 把公式占位符换成渲染好的 KaTeX
    const rendered = renderMath(formulas);
    return out.replace(/@@MBK(\d+)@@/g, (m, idx) => {
      const one = renderMath([formulas[+idx]]);
      return one;
    });
  }

  // ------------------------------------------------------------------
  // 应用状态
  // ------------------------------------------------------------------
  const state = {
    notes: new Map(),       // id -> note
    pdfs: {},              // pdf id -> {id, name, size, stem}
    currentId: null,
    filter: "",
    ai: { base: "", key: "", model: "MiniMax-M3.1-Flash", modelT: "MiniMax-M3" },
    dirHandle: null,       // FileSystemDirectoryHandle
    syncMode: "local",     // local | dir
    dirtyCount: 0,
    pendingSelection: "",  // 从 PDF 里划词带过来的原文
  };

  const FOLDERS = [
    "00_Dashboard", "01_Literature", "02_Reproduction",
    "03_Knowledge", "04_Weekly_Reports", "_Templates",
  ];

  // ------------------------------------------------------------------
  // 初始数据装载
  // ------------------------------------------------------------------
  async function loadSeed() {
    let raw = null;
    try {
      const node = $("mb-data");
      raw = JSON.parse(node.textContent);
    } catch (e) { /* ignore */ }

    if (raw && raw.pdfs) state.pdfs = raw.pdfs;

    const existing = await DB.allNotes();
    const have = new Map(existing.map((n) => [n.id, n]));

    if (raw && raw.notes && raw.notes.length) {
      const seed = raw.notes.map((r) => {
        const old = have.get(r.id);
        return {
          id: r.id,
          title: r.title || "",
          group: r.group || "",
          rel: r.rel || r.id,
          body: r.body || "",
          html: r.html || "",
          pdfs: r.pdfs || (old ? old.pdfs : []),
          dirty: old ? old.dirty : 0,
          deleted: old ? old.deleted : 0,
          baseHash: old ? old.baseHash : hash(r.body || ""),
          mtime: old ? old.mtime : (r.mtime || 0),
        };
      });
      // 本地有、种子没有的（用户自己新建的）要保留
      const seedIds = new Set(seed.map((s) => s.id));
      const extra = existing.filter((n) => !seedIds.has(n.id));
      await DB.putNotes(seed.concat(extra));
    }

    const all = await DB.allNotes();
    state.notes.clear();
    all.forEach((n) => state.notes.set(n.id, n));
    state.dirtyCount = all.filter((n) => n.dirty && !n.deleted).length;
  }

  // ------------------------------------------------------------------
  // 目录树
  // ------------------------------------------------------------------
  function buildNav() {
    const list = $("nav-list");
    list.innerHTML = "";
    const q = state.filter.trim().toLowerCase();

    const byGroup = new Map();
    state.notes.forEach((n) => {
      if (n.deleted) return;
      if (q && !(n.title.toLowerCase().includes(q) ||
                 (n.body || "").toLowerCase().includes(q))) return;
      const g = n.group || "_其它";
      if (!byGroup.has(g)) byGroup.set(g, []);
      byGroup.get(g).push(n);
    });

    if (!byGroup.size) {
      list.appendChild(el("div", "tb-empty", q ? "没有匹配的笔记" : "还没有笔记"));
      return;
    }

    const groups = FOLDERS.filter((f) => byGroup.has(f))
      .concat([...byGroup.keys()].filter((k) => FOLDERS.indexOf(k) < 0).sort());
    groups.forEach((g) => {
      list.appendChild(el("div", "tb-group", g));
      byGroup.get(g).sort((a, b) => (a.title || "").localeCompare(b.title || "", "zh"))
        .forEach((n) => {
          const b = el("button", "tb-item");
          if (n.id === state.currentId) b.classList.add("is-active");
          if (n.dirty) b.classList.add("is-dirty");
          b.appendChild(document.createTextNode(n.title || n.id));
          b.appendChild(el("span", "tb-item-sub", n.rel || ""));
          b.onclick = () => openNote(n.id);
          list.appendChild(b);
        });
    });
  }

  // ------------------------------------------------------------------
  // 阅读 / 编辑
  // ------------------------------------------------------------------
  function openNote(id) {
    const n = state.notes.get(id);
    if (!n) return;
    state.currentId = id;
    // URL 跟随当前笔记 —— 这样刷新 / 分享 / 收藏都能回到同一篇。
    if (location.hash !== "#/note/" + encodeURIComponent(id)) {
      location.hash = "#/note/" + encodeURIComponent(id);
    }
    $("crumb").textContent = n.rel || n.id;
    const dirty = $("dirty-flag");
    dirty.hidden = !n.dirty;
    // 这篇笔记关联到的原文 PDF
    const pdfBtn = $("btn-pdf-of-note");
    if (pdfBtn) {
      const refs = n.pdfs || [];
      pdfBtn.hidden = refs.length === 0;
      pdfBtn.textContent = refs.length > 1 ? `原文 PDF (${refs.length})` : "原文 PDF";
      pdfBtn.onclick = () => {
        if (refs.length === 1) { openPdf(refs[0]); return; }
        // 多篇 -> 先列出这一篇的关联,让用户选
        const list = $("pdf-lib-list");
        const note = $("pdf-lib-note");
        list.innerHTML = "";
        note.textContent = `「${n.title}」关联了 ${refs.length} 篇原文：`;
        refs.forEach((pid) => {
          const meta = state.pdfs[pid];
          const b = el("button", "tb-pdf-item");
          b.appendChild(el("span", "tb-pdf-item-name", meta ? meta.name : pid));
          b.onclick = () => { $("pdf-lib").hidden = true; openPdf(pid); };
          list.appendChild(b);
        });
        $("pdf-lib").hidden = false;
      };
    }
    $("editor").value = n.body || "";
    $("edit-hint").textContent = n.dirty ? "有未同步的改动" : "";
    const doc = $("doc");
    doc.innerHTML = (n.html && !n.dirty) ? n.html : renderMarkdown(n.body || "");
    doc.scrollTop = 0;
    buildNav();
    DB.setMeta("current_note", id);
    if (window.matchMedia("(max-width: 719px)").matches) setPane("doc");
  }

  function currentNote() { return state.notes.get(state.currentId); }

  /** 从 #/note/<id> 里取出笔记 id（id 里有中文和斜杠，必须解码） */
  function hashNoteId() {
    const m = location.hash.match(/^#\/note\/(.+)$/);
    if (!m) return "";
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  async function saveCurrent() {
    const n = currentNote();
    if (!n) return;
    const body = $("editor").value;
    n.body = body;
    n.dirty = 1;
    n.title = firstHeading(body) || n.title;
    n.html = "";
    await DB.putNote(n);
    state.dirtyCount = [...state.notes.values()].filter((x) => x.dirty && !x.deleted).length;
    updateSyncDot();
    buildNav();
    openNote(n.id);
    toast("已保存到平板本地");
  }

  function firstHeading(src) {
    const m = /^\s*#{1,3}\s+(.+)$/m.exec(stripFrontmatter(src || ""));
    return m ? m[1].trim() : "";
  }

  // ------------------------------------------------------------------
  // 新建 / 速记
  // ------------------------------------------------------------------
  function openNewModal(quick) {
    $("new-title").textContent = quick ? "速记" : "新建笔记";
    $("new-name").value = "";
    $("new-body").value = "";
    $("new-body-field").style.display = quick ? "" : "";
    const sel = $("new-folder");
    sel.innerHTML = "";
    FOLDERS.forEach((f) => {
      const o = el("option", null, f);
      o.value = f;
      if (f === "03_Knowledge") o.selected = true;
      sel.appendChild(o);
    });
    $("modal-new").hidden = false;
    $("new-name").focus();
  }

  async function createNote() {
    const title = $("new-name").value.trim();
    if (!title) { toast("请输入标题", true); return; }
    const folder = $("new-folder").value;
    const body = $("new-body").value.trim();
    const stamp = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const fname = `${title.replace(/[\\/:*?"<>|]/g, "_")}_${stamp.getFullYear()}${p(stamp.getMonth() + 1)}${p(stamp.getDate())}.md`;
    const id = `${folder}/${fname}`;
    const content = body
      ? `# ${title}\n\n${body}\n`
      : `# ${title}\n\n> 创建于 ${fmtTime(stamp.getTime())}\n`;
    const note = {
      id, title, group: folder, rel: id, body: content, html: "",
      dirty: 1, deleted: 0, baseHash: "", mtime: stamp.getTime(),
    };
    state.notes.set(id, note);
    await DB.putNote(note);
    state.dirtyCount = [...state.notes.values()].filter((x) => x.dirty && !x.deleted).length;
    updateSyncDot();
    buildNav();
    $("modal-new").hidden = true;
    openNote(id);
    toast(STATE_DIRTY_SAVED());
  }

  function STATE_DIRTY_SAVED() { return "已创建，记得同步到云盘"; }

  // ------------------------------------------------------------------
  // 同步：File System Access（安卓 Chrome 真双向）
  // ------------------------------------------------------------------
  const SKIP_DIRS = new Set([".obsidian", ".git", "logs", "microbench", "tests",
                             "__pycache__", "_vendor", ".pytest_cache", "assets"]);

  function fsSupported() {
    return typeof window.showDirectoryPicker === "function";
  }

  /* ------------------------------------------------------------------
     存储后端抽象
     ------------------------------------------------------------------
     两种形态, 网页代码不用分叉:
       fsa  —— 浏览器里用 File System Access API 直连本地文件夹
       dav  —— APK 里用原生桥走坚果云 WebDAV
     APK 里的 WebView **没有** showDirectoryPicker, 所以必须留 dav 这条路。
     ------------------------------------------------------------------ */
  const nativeBridge = (typeof window !== "undefined" && window.MicroBench)
    ? window.MicroBench : null;
  let _nativeSeq = 0;
  const _nativeWaiters = {};
  window.__mbNativeResolve = function (cbId, ok, payload) {
    const w = _nativeWaiters[cbId];
    if (!w) return;
    delete _nativeWaiters[cbId];
    if (ok) w.resolve(payload);
    else w.reject(new Error((payload && payload.error) || "原生调用失败"));
  };
  function nativeCall(method, args) {
    return new Promise((resolve, reject) => {
      const id = "cb" + (++_nativeSeq);
      _nativeWaiters[id] = { resolve, reject };
      try {
        nativeBridge[method].apply(nativeBridge, args.concat([id]));
      } catch (e) {
        delete _nativeWaiters[id];
        reject(e);
      }
    });
  }

  /** 逐段编码, 保留 / 作分隔符 (Java 侧按段拼 URL) */
  function davUrl(kind, path) {
    const segs = String(path || "").split("/")
      .filter((s) => s && s !== "." && s !== "..")
      .map(encodeURIComponent);
    return "/dav/" + kind + "/" + segs.join("/");
  }

  const BACKENDS = {
    fsa: {
      mode: "fsa",
      async list(dir) {
        const h = state.dirHandle;
        if (!h) return [];
        const out = [];
        const walk = async (d, prefix) => {
          for await (const entry of d.values()) {
            if (entry.name.startsWith(".")) continue;
            if (entry.kind === "directory") {
              if (SKIP_DIRS.has(entry.name)) continue;
              await walk(entry, prefix + entry.name + "/");
            } else {
              out.push({
                path: prefix + entry.name,
                name: entry.name,
                isDir: false,
                size: 0,
              });
            }
          }
        };
        try {
          const d = dir ? await h.getDirectoryHandle(dir) : h;
          await walk(d, dir ? dir + "/" : "");
        } catch (e) { /* 目录不存在 */ }
        return out;
      },
      async readText(path) {
        const f = await getVaultFile(path);
        return f ? f.text() : null;
      },
      async readBlob(path) {
        return await fsaGetFile(path);
      },
      async writeText(path, text) {
        const h = state.dirHandle;
        if (!h) throw new Error("还没连接文件夹");
        const parts = path.split("/");
        let dir = h;
        for (let i = 0; i < parts.length - 1; i++) {
          dir = await dir.getDirectoryHandle(parts[i], { create: true });
        }
        const fh = await dir.getFileHandle(parts[parts.length - 1], { create: true });
        const w = await fh.createWritable();
        await w.write(text);
        await w.close();
      },
    },

    dav: {
      mode: "dav",
      async list(dir) {
        const r = await fetch(davUrl("list", dir));
        if (!r.ok) throw new Error("列目录失败 HTTP " + r.status);
        const arr = await r.json();
        return Array.isArray(arr) ? arr : [];
      },
      async readText(path) {
        const r = await fetch(davUrl("file", path));
        if (!r.ok) return null;
        return r.text();
      },
      async readBlob(path) {
        // 本地缓存优先 —— 缓存过的离线也能翻
        const cached = await DB.getCachedPdf(path);
        if (cached) return cached;
        const r = await fetch(davUrl("file", path));
        if (!r.ok) return null;
        return r.blob();
      },
      async writeText(path, text) {
        await nativeCall("put", [path, text]);
      },
    },
  };

  /** 当前该用哪个后端。APK 里优先 WebDAV; 浏览器里用 FSA。 */
  function currentBackend() {
    if (nativeBridge && typeof nativeBridge.davConfigured === "function"
        && nativeBridge.davConfigured()) {
      return BACKENDS.dav;
    }
    if (state.dirHandle) return BACKENDS.fsa;
    return null;
  }

  /**
   * 原生设置页保存完凭证后, 由 Android 调这个 —— 重新探测同步状态并刷新界面。
   * (之前用 location.reload() 会把页面状态整个丢掉, 而且 onActivityResult
   *  根本不会触发: startActivity 拿不到 result。)
   */
  window.__mbRefreshSync = async function () {
    if (nativeBridge) {
      state.syncMode = nativeBridge.davConfigured() ? "dav" : "none";
    }
    updateSyncDot();
    refreshSyncUi();
    if (state.syncMode === "dav") {
      toast("坚果云已连接, 正在拉取笔记…");
      try {
        await pullFromDir();
        toast("坚果云已连接");
      } catch (e) {
        toast("坚果云已连接, 但拉取失败: " + (e.message || e), true);
      }
    }
  };

  /** 把同步状态显示同步到所有相关 UI。 */
  function refreshSyncUi() {
    const mode = $("sync-mode");
    const detail = $("sync-detail");
    if (!mode || !detail) return;
    if (nativeBridge) {
      if (state.syncMode === "dav") {
        mode.textContent = "坚果云 WebDAV（真双向）";
        detail.textContent = "已连接坚果云。笔记改动直接写回云盘里的 vault 目录；" +
          "原文 PDF 点开时从云盘按需下载。";
      } else {
        mode.textContent = "未连接坚果云";
        detail.textContent = "点「配置坚果云」填邮箱和应用密码，" +
          "就能直接读写云盘里的 vault 目录（笔记 + 24 篇原文 PDF）。";
      }
    } else {
      mode.textContent = state.syncMode === "fsa"
        ? "目录直连（真双向）" : "仅本机（未连接文件夹）";
      detail.textContent = fsSupported()
        ? "当前浏览器支持直连文件夹。"
        : "当前浏览器不支持 File System Access，请用 vault 包导入/导出。";
    }
  }

  async function connectDir() {
    // APK 里没有 showDirectoryPicker, 走原生设置页填坚果云账号
    if (nativeBridge) {
      nativeBridge.openSettings();
      return;
    }
    if (!fsSupported()) {
      toast("这个浏览器不支持直连文件夹，请用 Chrome，或改用「载入 vault 包」", true);
      return;
    }
    try {
      const h = await window.showDirectoryPicker({ mode: "readwrite", id: "mb-vault" });
      state.dirHandle = h;
      state.syncMode = "fsa";
      await DB.setMeta("dir_handle", h);   // FileSystemHandle 可结构化克隆
      $("sync-mode").textContent = "目录直连（真双向）";
      $("sync-detail").textContent = "已连接：" + h.name;
      await pullFromDir();
      await pushToDir();
      toast("已连接文件夹");
    } catch (e) {
      if (e && e.name !== "AbortError") toast("连接失败：" + e.message, true);
    }
  }

  async function ensurePermission(handle, writable) {
    if (!handle) return false;
    const opts = { mode: writable ? "readwrite" : "read" };
    if (await handle.queryPermission(opts) === "granted") return true;
    return await handle.requestPermission(opts) === "granted";
  }

  async function walkDir(dir, prefix, out) {
    for await (const entry of dir.values()) {
      if (entry.name.startsWith(".")) continue;
      if (entry.kind === "directory") {
        if (SKIP_DIRS.has(entry.name)) continue;
        await walkDir(entry, prefix + entry.name + "/", out);
      } else if (entry.name.toLowerCase().endsWith(".md")) {
        out.push({ path: prefix + entry.name, handle: entry });
      }
    }
    return out;
  }

  /** 列出云盘里的所有 .md 笔记（相对 vault 根的路径）。 */
  async function listRemoteNotes(backend) {
    if (backend.mode === "fsa") {
      const h = state.dirHandle;
      if (!await ensurePermission(h, false)) { toast("没有读取权限", true); return null; }
      const files = await walkDir(h, "", []);
      const out = [];
      for (const f of files) {
        const file = await f.handle.getFile();
        out.push({ path: f.path, text: await file.text(), mtime: file.lastModified });
      }
      return out;
    }
    // WebDAV: 递归列目录
    const out = [];
    const walk = async (dir) => {
      let entries;
      try {
        entries = await backend.list(dir);
      } catch (e) {
        return;
      }
      for (const e of entries) {
        if (e.isDir) {
          if (SKIP_DIRS.has(e.name)) continue;
          await walk(e.path);
        } else if (e.name.toLowerCase().endsWith(".md")) {
          const text = await backend.readText(e.path);
          if (text == null) continue;
          out.push({ path: e.path, text, mtime: e.mtime || 0 });
        }
      }
    };
    await walk("");
    return out;
  }

  async function pullFromDir() {
    const backend = currentBackend();
    if (!backend) return;
    const files = await listRemoteNotes(backend);
    if (!files) return;
    let added = 0, updated = 0, conflicts = 0;

    for (const f of files) {
      const text = f.text;
      const cur = state.notes.get(f.path);
      if (!cur) {
        const note = {
          id: f.path, title: firstHeading(text) || f.path.split("/").pop(),
          group: f.path.split("/")[0] || "_其它", rel: f.path,
          body: text, html: "", dirty: 0, deleted: 0,
          baseHash: hash(text), mtime: f.mtime || 0,
        };
        state.notes.set(f.path, note);
        await DB.putNote(note);
        added++;
      } else if (cur.dirty) {
        // 本地改过：云盘也变了才算冲突
        if (cur.baseHash && hash(text) !== cur.baseHash) {
          conflicts++;
          await resolveConflict(cur, text);
        }
      } else if (hash(text) !== hash(cur.body || "")) {
        cur.body = text;
        cur.html = "";
        cur.title = firstHeading(text) || cur.title;
        cur.baseHash = hash(text);
        cur.mtime = f.mtime || cur.mtime;
        await DB.putNote(cur);
        updated++;
      }
    }
    buildNav();
    if (state.currentId) openNote(state.currentId);
    $("sync-detail").textContent =
      `已连接 ${files.length} 篇 · 新增 ${added} · 更新 ${updated}` +
      (conflicts ? ` · 冲突 ${conflicts}` : "");
    toast(`读取完成：新增 ${added}，更新 ${updated}` + (conflicts ? `，冲突 ${conflicts}` : ""));
  }

  async function resolveConflict(localNote, diskText) {
    // 绝不丢数据：默认两边都保留，冲突版写到新笔记
    const merged = [
      `# ${localNote.title}（冲突合并）`,
      "",
      "## 平板版本",
      "",
      localNote.body || "",
      "",
      "## 云盘版本",
      "",
      diskText,
      "",
    ].join("\n");
    const stamp = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const id = `${localNote.group || "_其它"}/${localNote.title.replace(/[\\/:*?"<>|]/g, "_")}_冲突_${stamp.getFullYear()}${p(stamp.getMonth() + 1)}${p(stamp.getDate())}${p(stamp.getHours())}${p(stamp.getMinutes())}.md`;
    const note = {
      id, title: `${localNote.title}（冲突合并）`, group: localNote.group || "_其它",
      rel: id, body: merged, html: "", dirty: 1, deleted: 0,
      baseHash: "", mtime: stamp.getTime(),
    };
    state.notes.set(id, note);
    await DB.putNote(note);
    // 本地这条不再标记 dirty，让云盘版本直接落下来
    localNote.dirty = 0;
    localNote.body = diskText;
    localNote.html = "";
    localNote.baseHash = hash(diskText);
    await DB.putNote(localNote);
    toast("发现冲突，已另存为「冲突合并」笔记，两边都没丢", true);
  }

  async function pushToDir() {
    const backend = currentBackend();
    if (!backend) {
      toast(nativeBridge ? "还没配坚果云账号，点设置填一下" : "还没连接文件夹", true);
      return;
    }
    const dirty = [...state.notes.values()].filter((n) => n.dirty && !n.deleted);
    if (!dirty.length) { toast("没有待同步的改动"); return; }

    let ok = 0, fail = 0;
    for (const n of dirty) {
      try {
        await backend.writeText(n.id, n.body || "");
        n.dirty = 0;
        n.baseHash = hash(n.body || "");
        await DB.putNote(n);
        ok++;
      } catch (e) {
        fail++;
        console.error("写入失败", n.id, e);
      }
    }
    state.dirtyCount = [...state.notes.values()].filter((x) => x.dirty && !x.deleted).length;
    updateSyncDot();
    buildNav();
    toast(`已写入 ${ok} 篇到云盘` + (fail ? `，失败 ${fail}` : ""), fail > 0);
  }

  async function ensureDir(root, group) {
    let dir = root;
    if (!group) return dir;
    for (const seg of group.split("/").filter(Boolean)) {
      dir = await dir.getDirectoryHandle(seg, { create: true });
    }
    return dir;
  }

  // ------------------------------------------------------------------
  // 同步降级：vault 包 / 变更包
  // ------------------------------------------------------------------
  function buildPack(onlyDirty) {
    const notes = onlyDirty
      ? [...state.notes.values()].filter((n) => n.dirty && !n.deleted)
      : [...state.notes.values()].filter((n) => !n.deleted);
    return {
      format: "microbench-tablet-pack",
      version: 1,
      exported: Date.now(),
      dirty: !!onlyDirty,
      notes: notes.map((n) => ({
        id: n.id, title: n.title, group: n.group, body: n.body || "",
        baseHash: n.baseHash || "", mtime: n.mtime || 0,
      })),
    };
  }

  function downloadPack(onlyDirty) {
    const pack = buildPack(onlyDirty);
    const blob = new Blob([JSON.stringify(pack, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    const stamp = new Date();
    const p = (n) => String(n).padStart(2, "0");
    a.download = onlyDirty
      ? `MicroBench_变更包_${stamp.getFullYear()}${p(stamp.getMonth() + 1)}${p(stamp.getDate())}_${p(stamp.getHours())}${p(stamp.getMinutes())}.json`
      : `MicroBench_vault包_${stamp.getFullYear()}${p(stamp.getMonth() + 1)}${p(stamp.getDate())}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    toast(`已导出 ${pack.notes.length} 篇`);
  }

  async function importPack(file) {
    try {
      const pack = JSON.parse(await file.text());
      if (!pack || pack.format !== "microbench-tablet-pack") {
        toast("不是有效的 vault 包", true); return;
      }
      let added = 0, updated = 0;
      for (const r of pack.notes || []) {
        const cur = state.notes.get(r.id);
        if (!cur) {
          const note = {
            id: r.id, title: r.title || firstHeading(r.body) || r.id,
            group: r.group || (r.id.split("/")[0] || "_其它"), rel: r.id,
            body: r.body || "", html: "", dirty: 0, deleted: 0,
            baseHash: hash(r.body || ""), mtime: r.mtime || Date.now(),
          };
          state.notes.set(r.id, note);
          await DB.putNote(note);
          added++;
        } else if (cur.dirty && cur.baseHash && r.baseHash &&
                   hash(cur.body || "") !== cur.baseHash && r.baseHash !== cur.baseHash) {
          await resolveConflict(cur, r.body || "");
          updated++;
        } else {
          cur.body = r.body || "";
          cur.title = r.title || cur.title;
          cur.html = "";
          cur.baseHash = hash(r.body || "");
          cur.dirty = 0;
          await DB.putNote(cur);
          updated++;
        }
      }
      state.dirtyCount = [...state.notes.values()].filter((x) => x.dirty && !x.deleted).length;
      updateSyncDot();
      buildNav();
      if (state.currentId) openNote(state.currentId);
      toast(`导入完成：新增 ${added}，更新 ${updated}`);
    } catch (e) {
      toast("导入失败：" + e.message, true);
    }
  }

  // ------------------------------------------------------------------
  // AI：平板直连 MiniMax（流式）
  // ------------------------------------------------------------------
  const PROMPTS = {
    explain: (sel) => [
      { role: "system", content:
        "你是半导体/微电子方向的科研助手。用中文回答，先给结论再给依据，" +
        "涉及公式时给出 LaTeX 源码（行内 $...$，块级 $$...$$）。" },
      { role: "user", content:
        `请解读下面这段论文原文，说明它在讲什么、关键结论是什么、有没有值得警惕的前提。\n\n${sel}` },
    ],
    translate: (sel) => [
      { role: "system", content:
        "你是学术翻译。把用户给的论文段落译成中文，" +
        "保留术语的英文原文（如 TMR、spin-transfer torque），" +
        "公式用 $...$ 原样保留，只输出译文，不要解释。" },
      { role: "user", content: sel },
    ],
    summarize: (sel) => [
      { role: "system", content:
        "你是科研助手。用中文给出结构化摘要：研究问题、方法、关键结论、" +
        "对本方向的价值与局限。分点写，不超过 600 字。" },
      { role: "user", content: `请总结这篇笔记：\n\n${sel}` },
    ],
    ask: (sel, q) => [
      { role: "system", content:
        "你是半导体/微电子方向的科研助手，回答用中文，简洁直接，" +
        "涉及公式给出 LaTeX 源码。" },
      { role: "user", content: q + (sel ? `\n\n【选中的原文】\n${sel}` : "") },
    ],
  };

  let aiAbort = null;

  function selectedText() {
    const sel = window.getSelection();
    return sel && sel.toString().trim() ? sel.toString().trim() : "";
  }

  async function runAI(kind) {
    const cfg = state.ai;
    if (!cfg.key) {
      toast("还没配置 API Key，先去设置里填一下", true);
      openSettings();
      return;
    }
    const note = currentNote();
    const sel = selectedText() || state.pendingSelection || "";
    const bodyText = note ? (note.body || "") : "";
    if (kind === "explain" || kind === "translate") {
      if (!sel) { toast("先在正文或 PDF 里选中一段文字", true); return; }
    }
    const q = $("ai-question").value.trim();
    if (kind === "ask" && !q) { toast("写下你的问题", true); return; }
    state.pendingSelection = "";

    const model = kind === "translate" ? (cfg.modelT || cfg.model) : cfg.model;
    const messages = PROMPTS[kind](kind === "ask" ? (sel || bodyText.slice(0, 4000)) : (sel || bodyText.slice(0, 6000)), q);

    const out = $("ai-output");
    out.innerHTML = "";
    const status = el("div", "tb-ai-status", "正在思考…");
    out.appendChild(status);
    const buf = el("div");
    out.appendChild(buf);

    if (aiAbort) aiAbort.abort();
    aiAbort = new AbortController();

    // 翻译用快模型并关思考；解读/答疑用推理模型
    const isTranslate = kind === "translate";
    const payload = {
      model,
      messages,
      max_tokens: isTranslate ? 4000 : 8000,
      temperature: 0.3,
      stream: true,
    };
    if (isTranslate && /MiniMax-M3$/.test(model)) {
      payload.thinking = { type: "disabled" };
    }

    try {
      const res = await fetch(`${cfg.base.replace(/\/+$/, "")}/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${cfg.key}`,
        },
        body: JSON.stringify(payload),
        signal: aiAbort.signal,
      });
      if (!res.ok) {
        const t = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status} ${t.slice(0, 200)}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let pending = "";
      let full = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        pending += dec.decode(value, { stream: true });
        const lines = pending.split("\n");
        pending = lines.pop();
        for (const line of lines) {
          const s = line.trim();
          if (!s.startsWith("data:")) continue;
          const payload2 = s.slice(5).trim();
          if (payload2 === "[DONE]") continue;
          try {
            const obj = JSON.parse(payload2);
            const delta = obj.choices && obj.choices[0].delta
              ? (obj.choices[0].delta.content || "") : "";
            if (delta) {
              full += delta;
              buf.innerHTML = renderMarkdown(full);
            }
          } catch (e) { /* 心跳/畸形行忽略 */ }
        }
      }
      status.textContent = full ? `完成 · ${model}` : "上游没有返回正文（可能预算被思考吃掉）";
      if (!full) {
        status.textContent = "上游返回了空内容 — 换个模型或把 max_tokens 调大";
      }
    } catch (e) {
      if (e.name === "AbortError") status.textContent = "已取消";
      else {
        status.textContent = "失败：" + e.message;
        status.setAttribute("data-error", "1");
      }
    }
  }

  async function testLLM() {
    const hint = $("llm-hint");
    hint.textContent = "测试中…";
    try {
      const res = await fetch(`${state.ai.base.replace(/\/+$/, "")}/chat/completions`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${state.ai.key}`,
        },
        body: JSON.stringify({
          model: state.ai.model, messages: [{ role: "user", content: "1" }],
          max_tokens: 8, temperature: 0.1,
        }),
      });
      if (res.ok) { hint.textContent = "连接正常"; hint.removeAttribute("data-error"); }
      else {
        const t = await res.text().catch(() => "");
        hint.textContent = `HTTP ${res.status} ${t.slice(0, 160)}`;
        hint.setAttribute("data-error", "1");
      }
    } catch (e) {
      hint.textContent = "连不上：" + e.message + "（可能是浏览器跨域限制）";
      hint.setAttribute("data-error", "1");
    }
  }

  // ------------------------------------------------------------------
  // PDF 阅读器（pdf.js 内联，worker 用 Blob URL 起）
  // ------------------------------------------------------------------
  let _pdfJsPromise = null;

  function loadPdfJs() {
    if (_pdfJsPromise) return _pdfJsPromise;
    _pdfJsPromise = (async () => {
      if (!window.pdfjsLib) throw new Error("PDF.js 没有内联进来");
      const workerEl = document.getElementById("mb-pdf-worker-src");
      if (workerEl) {
        const src = workerEl.textContent || "";
        const blob = new Blob([src], { type: "text/javascript" });
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = URL.createObjectURL(blob);
      }
      return window.pdfjsLib;
    })();
    return _pdfJsPromise;
  }

  const pdfState = {
    doc: null, numPages: 0, scale: 1.15, id: null, url: null,
    currentPage: 1,
    rendered: new Set(),   // 已经画过的页码
    pending: false,
  };

  /** 从已连接的 vault 目录里取一个文件。没连目录就返回 null。 */
  async function getVaultFile(relPath) {
    const b = currentBackend();
    if (!b) return null;
    try {
      return await b.readBlob(relPath);
    } catch (e) {
      return null;
    }
  }

  /** FSA 专用：从本地目录句柄取文件。 */
  async function fsaGetFile(relPath) {
    const h = state.dirHandle;
    if (!h) return null;
    const parts = String(relPath).split("/").filter(Boolean);
    if (!parts.length) return null;
    let dir = h;
    try {
      for (let i = 0; i < parts.length - 1; i++) {
        dir = await dir.getDirectoryHandle(parts[i]);
      }
      const fh = await dir.getFileHandle(parts[parts.length - 1]);
      return await fh.getFile();
    } catch (e) {
      return null;
    }
  }

  function pdfStatus(msg) {
    const el = $("pdf-status");
    if (el) el.textContent = msg || "";
  }

  async function openPdf(pdfId) {
    const meta = state.pdfs[pdfId];
    if (!meta) { toast("找不到这篇 PDF", true); return; }
    $("pdf-reader").hidden = false;
    $("pdf-title").textContent = meta.name;
    pdfStatus("");

    if (!currentBackend()) {
      pdfStatus(nativeBridge
        ? "还没配置坚果云 —— 点设置里的「配置坚果云」填账号和应用密码"
        : "还没连接文件夹 —— PDF 原文在云盘的 vault 目录里。" +
          "安卓平板上请选坚果云同步下来的「我的坚果云\\MicroBench工作台」");
      return;
    }
    // 原文 PDF 不打包进 APK（76 MB），是点开时从云盘按需下载的
    const kb = Math.round((meta.size || 0) / 1024);
    pdfStatus(kb ? `正在从云盘下载原文（${kb} KB）…` : "正在从云盘下载原文…");
    const t0 = Date.now();
    const file = await getVaultFile(pdfId);
    if (!file) {
      pdfStatus("云盘里找不到这个文件：" + meta.name);
      toast("打不开原文：云盘里没找到这个文件。\n" +
            "检查一下坚果云是不是同步完了，或者路径对不对。", true);
      return;
    }
    const secs = Math.max(1, Math.round((Date.now() - t0) / 1000));
    try {
      const pdfjsLib = await loadPdfJs();
      if (pdfState.url) URL.revokeObjectURL(pdfState.url);
      pdfState.url = URL.createObjectURL(file);
      pdfState.doc = await pdfjsLib.getDocument({ url: pdfState.url }).promise;
      pdfState.numPages = pdfState.doc.numPages;
      pdfState.id = pdfId;
      pdfState.rendered.clear();
      pdfState.currentPage = 1;
      $("pdf-body").innerHTML = "";
      pdfStatus("");
      await renderPdfPage(1);
      $("pdf-pageinfo").textContent = `1 / ${pdfState.numPages}`;
    } catch (e) {
      pdfStatus("打开失败：" + e.message);
    }
  }

  async function renderPdfPage(n) {
    const doc = pdfState.doc;
    if (!doc || n < 1 || n > pdfState.numPages) return;
    if (pdfState.rendered.has(n)) {
      const node = document.querySelector(`[data-pdf-page="${n}"]`);
      if (node) {
        node.scrollIntoView({ block: "start", behavior: "smooth" });
        return;
      }
    }
    const pdfjsLib = await loadPdfJs();
    const page = await doc.getPage(n);
    const viewport = page.getViewport({ scale: pdfState.scale });
    const dpr = window.devicePixelRatio || 1;

    const wrap = el("div", "tb-pdf-page");
    wrap.setAttribute("data-pdf-page", String(n));
    const canvas = el("canvas");
    wrap.appendChild(canvas);
    const textLayer = el("div", "textLayer");
    textLayer.style.width = viewport.width + "px";
    textLayer.style.height = viewport.height + "px";
    wrap.appendChild(textLayer);

    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = viewport.width + "px";
    canvas.style.height = viewport.height + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const body = $("pdf-body");
    // 按页码顺序插入,保证连续阅读时顺序正确
    let before = null;
    for (const child of Array.from(body.children)) {
      const pn = parseInt(child.getAttribute("data-pdf-page") || "0", 10);
      if (pn > n) { before = child; break; }
    }
    body.insertBefore(wrap, before);

    await page.render({ canvasContext: ctx, viewport }).promise;

    // 文本层：PDF.js 3.11 的 renderTextLayer 是**全局函数**（不是 page 的方法），
    // 而且要先拿到 getTextContent() 的结果。参数与桌面版 index.html 保持一致。
    try {
      const tc = await page.getTextContent();
      if (pdfjsLib.renderTextLayer) {
        pdfjsLib.renderTextLayer({
          textContentSource: tc,
          textContent: tc,
          container: textLayer,
          viewport: viewport,
          textDivs: [],
        });
      } else {
        renderTextLayerFallback(tc, textLayer, viewport, pdfjsLib);
      }
    } catch (e) {
      console.warn("文本层渲染失败", e);
    }
    pdfState.rendered.add(n);
    if (n === 1) wrap.scrollIntoView({ block: "start" });
  }

  /** pdf.js 的 renderTextLayer 不可用时的手工兜底（照抄桌面版实现）。 */
  function renderTextLayerFallback(tc, container, viewport, pdfjsLib) {
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
        } catch (e) { /* ignore */ }
      }
      span.style.fontSize = `${Math.max(8, item.height * viewport.scale)}px`;
      container.appendChild(span);
    }
  }

  /** 往前/往后翻：先渲染目标页，再滚过去。 */
  async function gotoPdfPage(n) {
    if (!pdfState.doc) return;
    const target = Math.max(1, Math.min(pdfState.numPages, n));
    pdfState.currentPage = target;
    await renderPdfPage(target);
    $("pdf-pageinfo").textContent = `${target} / ${pdfState.numPages}`;
    const node = document.querySelector(`[data-pdf-page="${target}"]`);
    if (node) node.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  /** 缩放后整本重画（页数多时只重画当前附近的页）。 */
  async function redrawPdf() {
    if (!pdfState.doc) return;
    const pages = Array.from(pdfState.rendered).sort((a, b) => a - b);
    pdfState.rendered.clear();
    $("pdf-body").innerHTML = "";
    for (const n of pages) {
      await renderPdfPage(n);
    }
    pdfStatus("");
  }

  /** 滚动时更新当前页码（读者最关心「我现在在第几页」）。 */
  function initPdfScrollWatch() {
    const body = $("pdf-body");
    if (!body) return;
    let ticking = false;
    body.addEventListener("scroll", () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        const mid = body.scrollTop + body.clientHeight / 2;
        let current = 1;
        for (const child of Array.from(body.children)) {
          const n = parseInt(child.getAttribute("data-pdf-page") || "0", 10);
          if (!n) continue;
          if (child.offsetTop <= mid) current = n;
        }
        if (current !== pdfState.currentPage) {
          pdfState.currentPage = current;
          $("pdf-pageinfo").textContent = `${current} / ${pdfState.numPages}`;
        }
        // 滚到接近底部 -> 预渲染下一页, 连续读不用等
        if (body.scrollTop + body.clientHeight > body.scrollHeight - body.clientHeight * 0.8) {
          const next = pdfState.currentPage + 1;
          if (next <= pdfState.numPages && !pdfState.rendered.has(next)) {
            renderPdfPage(next);
          }
        }
      });
    }, { passive: true });
  }

  function closePdf() {
    $("pdf-reader").hidden = true;
    if (pdfState.url) { URL.revokeObjectURL(pdfState.url); pdfState.url = null; }
    pdfState.doc = null;
    pdfState.rendered.clear();
    $("pdf-body").innerHTML = "";
  }

  /** 划词气泡：PDF 里选中文字 -> 解读 / 翻译 / 提问 */
  function initPdfSelection() {
    const layer = $("pdf-textlayer");
    const bubble = $("pdf-sel");
    if (!layer || !bubble) return;

    const hide = () => { bubble.hidden = true; };
    const show = (sel) => {
      const text = String(sel || "").trim();
      if (!text) { hide(); return; }
      const rect = window.getSelection().getRangeAt(0).getBoundingClientRect();
      if (!rect || (rect.width === 0 && rect.height === 0)) { hide(); return; }
      bubble.dataset.text = text;
      bubble.hidden = false;
      const top = Math.max(8, rect.top - 52);
      const left = Math.min(
        Math.max(8, rect.left),
        window.innerWidth - bubble.offsetWidth - 8
      );
      bubble.style.top = top + "px";
      bubble.style.left = left + "px";
    };

    layer.addEventListener("mouseup", () => {
      setTimeout(() => show(window.getSelection().toString()), 10);
    });
    layer.addEventListener("touchend", () => {
      setTimeout(() => show(window.getSelection().toString()), 60);
    });
    layer.addEventListener("scroll", hide);

    bubble.querySelectorAll("[data-pdf-ai]").forEach((b) => {
      b.onclick = () => {
        const text = bubble.dataset.text || "";
        const kind = b.dataset.pdfAi;
        hide();
        closePdf();
        // 交给 AI 面板处理
        state.pendingSelection = text;
        $("ai-question").value = kind === "ask" ? "" : "";
        setPane("ai");
        if (kind === "ask") {
          $("ai-question").focus();
        } else {
          runAI(kind);
        }
      };
    });
  }

  /**
   * 把全部原文 PDF 下载到平板本地（IndexedDB），之后没网也能翻。
   *
   * 原文一共 76 MB, 平板下完就没网也不影响读 —— 这是「带上飞机看论文」
   * 真正需要的那一步。
   */
  async function cacheAllPdfs() {
    const backend = currentBackend();
    if (!backend) {
      toast("还没连上云盘，先配置坚果云", true);
      return;
    }
    const ids = Object.keys(state.pdfs || {});
    if (!ids.length) { toast("没有可缓存的原文"); return; }
    const done = await DB.cachedPdfIds();
    const todo = ids.filter((id) => done.indexOf(id) < 0);
    if (!todo.length) { toast("全部原文已经在本地了（可离线阅读）"); return; }

    const total = todo.reduce((a, id) => a + ((state.pdfs[id].size || 0)), 0);
    let got = 0, ok = 0, fail = 0;
    const t0 = Date.now();
    toast(`开始缓存 ${todo.length} 篇原文（约 ${Math.round(total / 1048576)} MB）…`);
    for (let i = 0; i < todo.length; i++) {
      const id = todo[i];
      try {
        const r = await fetch(davUrl("file", id));
        if (!r.ok) { fail++; continue; }
        const blob = await r.blob();
        await DB.cachePdf(id, blob);
        ok++;
        got += blob.size;
      } catch (e) {
        fail++;
      }
      if (i % 2 === 0 || i === todo.length - 1) {
        pdfStatus(`缓存中 ${i + 1}/${todo.length}（${Math.round(got / 1048576)} MB）`);
      }
    }
    pdfStatus("");
    const secs = Math.max(1, Math.round((Date.now() - t0) / 1000));
    toast(`缓存完成：${ok} 篇 ${Math.round(got / 1048576)} MB，用了 ${secs} 秒` +
          (fail ? `，失败 ${fail}` : "") + "。现在没网也能翻论文了。", fail > 0);
    await openPdfLib();
  }

  /** 文献库：列出所有 PDF，以及它们关联的笔记 */
  function openPdfLib() {
    const list = $("pdf-lib-list");
    const note = $("pdf-lib-note");
    const ids = Object.keys(state.pdfs || {});
    list.innerHTML = "";

    if (!ids.length) {
      note.textContent = "没有找到任何 PDF。";
      $("pdf-lib").hidden = false;
      return;
    }
    note.textContent = state.dirHandle
      ? `共 ${ids.length} 篇原文。PDF 直接从已连接的云盘目录读取。`
      : `共 ${ids.length} 篇原文。⚠ 还没连接文件夹 —— 点开会提示你先连接云盘里的 vault 目录。` +
        `（在安卓平板上就是坚果云同步下来的「我的坚果云\\MicroBench工作台」那个文件夹）`;

    // 反查：PDF -> 关联笔记
    const byPdf = {};
    state.notes.forEach((n) => {
      (n.pdfs || []).forEach((pid) => {
        (byPdf[pid] = byPdf[pid] || []).push(n.title || n.id);
      });
    });

    const groups = {};
    ids.forEach((id) => {
      const g = id.split("/").slice(0, 2).join("/");
      (groups[g] = groups[g] || []).push(id);
    });

    // 缓存状态
    DB.cachedPdfIds().then((cached) => {
      const hint = $("pdf-cache-hint");
      if (!hint) return;
      if (!cached.length) {
        hint.textContent = "还没缓存 —— 没网的时候打不开原文";
      } else {
        const mb = ids.reduce((a, id) => {
          const m = state.pdfs[id];
          return a + ((cached.indexOf(id) >= 0) ? (m.size || 0) : 0);
        }, 0);
        hint.textContent = `已缓存 ${cached.length}/${ids.length} 篇（${Math.round(mb / 1048576)} MB），可离线阅读`;
      }
    });

    Object.keys(groups).sort().forEach((g) => {
      list.appendChild(el("div", "tb-group", g));
      groups[g].sort().forEach((id) => {
        const meta = state.pdfs[id];
        const b = el("button", "tb-pdf-item");
        b.appendChild(el("span", "tb-pdf-item-name", meta.name));
        const linked = byPdf[id] || [];
        const size = meta.size ? Math.round(meta.size / 1024) + " KB" : "";
        const meta2 = el("span", "tb-pdf-item-meta",
          [size, linked.length ? "关联笔记：" + linked.slice(0, 2).join("、") : ""]
            .filter(Boolean).join(" · "));
        b.appendChild(meta2);
        b.onclick = () => { $("pdf-lib").hidden = true; openPdf(id); };
        list.appendChild(b);
      });
    });
    $("pdf-lib").hidden = false;
  }

  // ------------------------------------------------------------------
  // 设置
  // ------------------------------------------------------------------
  function openSettings() {
    refreshSyncUi();
    $("cfg-base").value = state.ai.base || "";
    $("cfg-key").value = state.ai.key || "";
    $("cfg-model").value = state.ai.model || "";
    $("cfg-model-t").value = state.ai.modelT || "";
    $("modal-settings").hidden = false;
    $("about-note").textContent =
      `MicroBench 平板工作台 · 本地 ${state.notes.size} 篇 · 待同步 ${state.dirtyCount} 篇`;
  }

  async function saveSettings() {
    state.ai.base = $("cfg-base").value.trim();
    state.ai.key = $("cfg-key").value.trim();
    state.ai.model = $("cfg-model").value.trim() || "MiniMax-M3.1-Flash";
    state.ai.modelT = $("cfg-model-t").value.trim() || "MiniMax-M3";
    await DB.setMeta("ai", state.ai);
    toast("设置已保存");
  }

  // ------------------------------------------------------------------
  // UI 装配
  // ------------------------------------------------------------------
  function updateSyncDot() {
    const b = $("btn-sync");
    b.classList.toggle("has-dot", state.dirtyCount > 0);
    const s = $("sync-status");
    const mode = nativeBridge
      ? (state.syncMode === "dav" ? "坚果云" : "未连接")
      : (state.syncMode === "fsa" ? "目录直连" : "本地模式");
    if (s) {
      s.textContent = `${mode} · 待同步 ${state.dirtyCount}`;
    }
    // 顶栏的云���状态标签
    const c = $("cloud-state");
    if (c) {
      const on = nativeBridge ? state.syncMode === "dav" : state.syncMode === "fsa";
      c.classList.toggle("is-on", on);
      c.classList.toggle("is-off", nativeBridge && !on);
      c.textContent = nativeBridge ? (on ? "坚果云" : "未连接") : (on ? "已连接" : "本地");
      c.title = nativeBridge
        ? (on ? "坚果云已连接，笔记改动直接写回云盘" : "点设置里的「配置坚果云」连接")
        : (on ? "已连接本地文件夹" : "未连接文件夹");
    }
  }

  function setPane(pane) {
    const shell = document.querySelector(".tb-shell");
    shell.classList.remove("nav-open", "side-open");
    $("scrim").classList.remove("show");
    if (pane === "nav") shell.classList.add("nav-open");
    if (pane === "edit" || pane === "ai") {
      shell.classList.add("side-open");
      document.querySelectorAll(".tb-tab").forEach((t) => {
        t.classList.toggle("is-active", t.dataset.pane === pane);
      });
      document.querySelectorAll(".tb-pane").forEach((p) => {
        p.classList.toggle("is-active", p.id === "pane-" + pane);
      });
    }
    if (pane === "doc") {
      document.querySelectorAll(".tb-tabbar-btn").forEach((b) => {
        b.classList.toggle("is-active", b.dataset.pane === "doc");
      });
    }
  }

  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("mb_theme", t); } catch (e) {}
  }

  async function boot() {
    // 启动阶段标记。平板上白屏时, 这个元素会告诉我们卡在了哪一步
    // (IndexedDB 打开 / 读种子数据 / 恢复目录句柄 ...)。
    const stage = (s) => {
      const e = $("mb-stage");
      if (e) e.textContent = (e.textContent ? e.textContent + " > " : "") + s;
    };
    stage("begin");
    db = await openDB();
    stage("db");

    // 主题
    let theme = "light";
    try { theme = localStorage.getItem("mb_theme") || "light"; } catch (e) {}
    applyTheme(theme);

    await loadSeed();
    stage("seed:" + state.notes.size);

    // AI 配置
    const ai = await DB.getMeta("ai", null);
    if (ai) state.ai = Object.assign(state.ai, ai);

    // 目录句柄（安卓 Chrome 可持久化）/ 坚果云 WebDAV（APK）
    if (nativeBridge) {
      state.syncMode = nativeBridge.davConfigured() ? "dav" : "none";
    } else if (fsSupported()) {
      const h = await DB.getMeta("dir_handle", null);
      if (h) {
        state.dirHandle = h;
        state.syncMode = "fsa";
      }
    }
    stage("meta");

    // 当前笔记：URL 里的 hash 优先（分享/收藏链接要能直达），
    // 其次是上次看的，最后才是第一篇。
    const hashId = hashNoteId();
    let cur = hashId && state.notes.has(hashId) ? hashId : null;
    if (!cur) cur = await DB.getMeta("current_note", null);
    if (!cur || !state.notes.has(cur)) {
      cur = ([...state.notes.keys()][0] || null);
    }
    if (cur) openNote(cur);
    else $("doc").innerHTML = '<div class="tb-empty">还没有笔记</div>';
    stage("opened");

    buildNav();
    stage("nav");
    updateSyncDot();
    stage("done");

    // 事件
    $("btn-menu").onclick = () => setPane("nav");
    $("btn-pdfs").onclick = openPdfLib;
    $("btn-connect-dir").onclick = () => {
      // APK 里这个按钮直通原生设置页
      connectDir();
    };
    $("btn-connect-dir").textContent = nativeBridge ? "配置坚果云" : "连接文件夹（真双向）";
    $("btn-cache-all").onclick = cacheAllPdfs;
    $("pdf-close").onclick = closePdf;
    $("pdf-prev").onclick = () => gotoPdfPage(pdfState.currentPage - 1);
    $("pdf-next").onclick = () => gotoPdfPage(pdfState.currentPage + 1);
    $("pdf-zoom-in").onclick = () => { pdfState.scale = Math.min(3, pdfState.scale * 1.2); redrawPdf(); };
    $("pdf-zoom-out").onclick = () => { pdfState.scale = Math.max(0.4, pdfState.scale / 1.2); redrawPdf(); };
    document.querySelectorAll("[data-close-lib]").forEach((b) => {
      b.onclick = () => { $("pdf-lib").hidden = true; };
    });
    initPdfSelection();
    initPdfScrollWatch();
    $("btn-sync").onclick = async () => {
      if (state.syncMode === "dir") {
        await pullFromDir();
        await pushToDir();
      } else {
        openSettings();
      }
    };
    $("btn-settings").onclick = openSettings;
    $("btn-new").onclick = () => openNewModal(false);
    $("btn-quick").onclick = () => openNewModal(true);
    $("btn-create").onclick = createNote;
    $("btn-edit").onclick = () => setPane("edit");
    $("btn-ai").onclick = () => setPane("ai");
    $("btn-save").onclick = saveCurrent;
    $("btn-side-close").onclick = () => setPane("doc");
    $("btn-theme").onclick = async () => {
      const cur2 = document.documentElement.getAttribute("data-theme");
      applyTheme(cur2 === "night" ? "light" : "night");
    };
    $("btn-reset").onclick = async () => {
      if (!confirm("清空平板上的全部本地数据？云盘里的文件不受影响。")) return;
      await DB.clearNotes();
      await DB.setMeta("current_note", null);
      location.reload();
    };
    $("btn-connect-dir").onclick = connectDir;
    $("btn-import-pack").onclick = () => $("file-pack").click();
    $("btn-export-pack").onclick = () => downloadPack(true);
    $("btn-test-llm").onclick = testLLM;

    $("file-pack").onchange = (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) importPack(f);
      e.target.value = "";
    };

    document.querySelectorAll("[data-close]").forEach((b) => {
      b.onclick = () => { b.closest(".tb-modal").hidden = true; };
    });
    document.querySelectorAll(".tb-modal").forEach((m) => {
      m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; });
    });

    document.querySelectorAll(".tb-tab").forEach((t) => {
      t.onclick = () => setPane(t.dataset.pane);
    });
    document.querySelectorAll(".tb-tabbar-btn").forEach((b) => {
      b.onclick = () => setPane(b.dataset.pane);
    });
    document.querySelectorAll("[data-ai]").forEach((b) => {
      b.onclick = () => runAI(b.dataset.ai);
    });

    $("scrim").onclick = () => setPane("doc");

    let searchTimer = null;
    $("search").oninput = () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.filter = $("search").value;
        buildNav();
      }, 140);
    };

    $("cfg-base").onchange = saveSettings;
    $("cfg-key").onchange = saveSettings;
    $("cfg-model").onchange = saveSettings;
    $("cfg-model-t").onchange = saveSettings;

    // 长按/双击正文进入编辑
    $("doc").addEventListener("dblclick", () => setPane("edit"));

    // 浏览器返回键 / 手势返回 -> 回到上一篇笔记
    window.addEventListener("hashchange", () => {
      const id = hashNoteId();
      if (id && id !== state.currentId && state.notes.has(id)) openNote(id);
    });

    // 离开前提醒还有未同步的改动
    window.addEventListener("beforeunload", (e) => {
      if (state.dirtyCount > 0) {
        e.preventDefault();
        e.returnValue = "";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    // boot() 是 async，任何一个 await 抛错都会变成一个没人看的 rejected
    // promise —— 页面就停在"壳子渲染出来了但空空如也"的状态，完全看不出
    // 出了什么事。这里把错误直接摊到页面上。
    boot().catch((err) => {
      console.error("启动失败", err);
      const doc = document.getElementById("doc");
      if (doc) {
        doc.innerHTML =
          '<div class="tb-empty" style="color:var(--bad)">启动失败：' +
          esc(err && err.message ? err.message : String(err)) +
          '<br><br><span style="font-size:13px">如果是第一次打开，可能是浏览器版本太旧；' +
          '请换 Chrome 最新版试试。</span></div>';
      }
      const s = document.getElementById("sync-status");
      if (s) s.textContent = "启动失败";
    });
  });

  // 暴露给测试：渲染器和 hash 是这个应用最容易出错的两块，必须能脱离
  // 浏览器单测。挂在 window 上，页面里没人用它。
  if (typeof window !== "undefined") {
    window.__MB_TEST__ = {
      renderMarkdown: renderMarkdown,
      extractMath: extractMath,
      hash: hash,
      firstHeading: firstHeading,
      stripFrontmatter: stripFrontmatter,
    };
  }
})();
