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
    // ⚠⚠ 这里必须是 **E_AMP**，不是 E_QUOT。
    //   原来写成了 E_QUOT + "quot;" —— 产出的是 `"quot;` 而不是 `&quot;`，
    //   于是「引号转义」根本没生效：属性值里的引号照样能闭合属性，
    //   `![x" onerror="alert(1)](a.png)` 照样能注入 onerror。
    //   这就是 XSS 能打通的真正根因 —— esc() 从上线起就没转义过引号。
    .replace(/"/g, E_AMP + "quot;");

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

  // 属性白名单 —— 比「逐个禁 on*」稳得多。
  // 禁 on* 的话，漏掉一个新名字（onfocus / formaction / xlink:href…）
  // 就是一条现成的注入通道。
  const ALLOWED_ATTRS = new Set([
    "class", "id", "href", "src", "alt", "title", "width", "height",
    "colspan", "rowspan", "start", "type", "lang", "dir",
    "data-lang", "data-tex", "data-type",
  ]);

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
        if (m.charAt(1) === "/") {
          return "</" + tag + ">";
        }
        // 属性**白名单**：不在名单里的一律丢掉。
        // style 也在名单外 —— 它能做全屏 UI 覆盖。
        const kept = [];
        const src = String(attrs || "");
        const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+))/g;
        let am;
        while ((am = attrRe.exec(src)) !== null) {
          const name = am[1].toLowerCase();
          if (!ALLOWED_ATTRS.has(name)) continue;
          let val = am[3] != null ? am[3] : (am[4] != null ? am[4] : (am[5] || ""));
          if (name === "href" || name === "src") {
            val = safeHref(val);
          }
          kept.push(name + "=\"" + esc(val) + "\"");
        }
        const selfClose = /\/\s*$/.test(src);
        return "<" + tag + (kept.length ? " " + kept.join(" ") : "")
          + (selfClose ? "/" : "") + ">";
      });
    return out;
  }

  /**
   * 链接协议白名单 —— 只有这些能进 href。
   *
   * ⚠ 判定协议之前必须先「解码实体 + 去控制字符」：
   *   - `jav&#x61;script:alert(1)` —— 实体是 HTML **解析时**才解码的，
   *     不先解码就会当成普通字符串放过去。
   *   - `java<TAB>script:alert(1)` —— 浏览器会忽略 URL 里的制表符/换行，
   *     看着不像 javascript:，实际是。
   */
  function safeHref(url) {
    const raw = String(url == null ? "" : url).trim();
    const probe = raw
      // 数字实体
      .replace(/&#x([0-9a-fA-F]+);?/g, (m2, h) => String.fromCharCode(parseInt(h, 16)))
      .replace(/&#([0-9]+);?/g, (m2, d) => String.fromCharCode(parseInt(d, 10)))
      // 少数几个关键的具名实体
      .replace(/&colon;/gi, ":")
      .replace(/&tab;/gi, "\t")
      .replace(/&newline;/gi, "\n")
      .replace(/&NewLine;/gi, "\n")
      // 浏览器忽略 URL 里的控制字符与空白
      .replace(/[\u0000-\u0020\u007f]+/g, "");
    if (/^(javascript|vbscript|data):/i.test(probe)) return "#";
    if (/^[a-z][a-z0-9+.-]*:/i.test(probe) && !/^(https?|mailto|tel):/i.test(probe)) return "#";
    return raw;
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
      req.onsuccess = () => { db = req.result; resolve(db); };
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

  /**
   * 抽出公式，换成占位符。
   *
   * ⚠ ctx 用来**跨递归层共享公式表**。
   *   原来每层递归都自己建一张表，而占位符 @@MBK0@@ 是外层生成的 ——
   *   内层的表是空的，取 formulas[0] 就是 undefined → renderMath 抛 TypeError
   *   → 整篇笔记渲染不出来（引用块、多行列表里的公式必崩）。
   */
  function extractMath(text, ctx) {
    const formulas = ctx ? ctx.formulas : [];
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
    if (!formulas || !formulas.length) return "";
    // 兜底：表里不该有空洞，但万一有，别让整篇笔记渲染不出来
    const list = formulas.filter((f) => f && typeof f.tex === "string");
    if (!list.length) return "";
    if (typeof katex === "undefined") {
      // KaTeX 没加载出来时退回源码，至少不吞掉公式
      return list.map((f) =>
        `<code class="math-error">${esc(f.tex.trim())}</code>`).join("");
    }
    return list.map((f) => {
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

  /**
   * 渲染 Markdown。
   *
   * ⚠ 外层包了 try/catch —— 渲染失败宁可退回纯文本源码，也别白屏。
   *   白屏会让 openNote 抛异常，连带把 saveCurrent 里的 pushToDir 跳过
   *   （用户以为存了，其实只存在本地），还会让 pullFromDir 末尾的
   *   openNote 抛出去，整轮同步静默中断。
   */
  function renderMarkdown(src, ctx) {
    if (!src) return "";
    try {
      return renderMarkdownInner(src, ctx);
    } catch (e) {
      console.warn("Markdown 渲染失败，回退纯文本", e);
      return `<pre>${esc(String(src))}</pre>`;
    }
  }

  function renderMarkdownInner(src, ctx) {
    if (!src) return "";
    // 公式表跨递归层共享（见 extractMath 的注释）
    const c = ctx || { formulas: [] };
    const { text } = extractMath(stripFrontmatter(String(src)), c);
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
      // ⚠⚠ 图片 / 链接的插值**必须转义**。
      //   sanitizeHtml 跑在**前面**，管不到这里新生成的 HTML ——
      //   而 alt / href 里的引号能直接冲破属性：
      //     ![x" onerror="alert(1)](a.png)
      //       -> <img alt="x" onerror="alert(1)" src="a.png">   （实测命中）
      //   笔记正文、云盘同步下来的 .md、导入的 vault 包都能走到这里。
      s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
        (m, alt, src2) => `<img alt="${esc(alt)}" src="${esc(safeHref(src2))}">`);
      // 链接
      s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
        (m, t2, href) => `<a href="${esc(safeHref(href))}" rel="noopener noreferrer">${esc(t2)}</a>`);
      // Obsidian 双链
      s = s.replace(/\[\[([^\]]+)\]\]/g, (m, raw) => {
        const target = raw.split("|")[0].trim();
        const alias = (raw.split("|")[1] || target).trim();
        return `<span class="wikilink-missing">${esc(alias)}</span>`;
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
        out += `<blockquote>${renderMarkdown(buf.join("\n"), c)}</blockquote>`;
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
              ? inline(txt.split("\n")[0]) + renderMarkdown(x.lines.slice(1).join("\n"), c)
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

    // 把公式占位符换成渲染好的 KaTeX。
    // ⚠ 别在这儿再调一次 renderMath(formulas) —— 那是死代码，
    //   而且会让每个公式被 KaTeX 渲染**两遍**（长笔记明显卡顿）。
    return out.replace(/@@MBK(\d+)@@/g, (m, idx) => {
      const one = c.formulas[+idx];
      if (!one) return "";
      return renderMath([one]);
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
    aiHistory: new Map(),   // noteId -> [{role, content}] 多轮对话上下文
    trans: new Map(),       // paperId -> {pageNum: 译文}（与电脑端共享）
    annos: new Map(),       // paperId -> [批注]
    transSeed: {},          // 导出版里烤进来的译文 {safeKey: {pageNum: 译文}}
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
    // 导出版里烤进来的译文（还没连云盘时也能看）
    if (raw && raw.trans) state.transSeed = raw.trans;

    const existing = await DB.allNotes();
    const have = new Map(existing.map((n) => [n.id, n]));

    if (raw && raw.notes && raw.notes.length) {
      const seed = raw.notes.map((r) => {
        const old = have.get(r.id);
        // ⚠⚠ 本地已有的记录必须**以本地为准**。
        //   种子里的 body 是「打包那一刻」的静态快照（可能是三天前的），
        //   拿它覆盖本地正文 = 刷新一次就把平板上的编辑抹掉；
        //   更糟的是 dirty 还留着 1、baseHash 却是旧文本的哈希 →
        //   下次 push 会把**旧文本写回坚果云**，把两边一起回滚。
        //   （这正是「本地优先」被违反的��方。）
        if (old) {
          return Object.assign({}, old, {
            title: old.title || r.title || "",
            group: old.group || r.group || "",
            pdfs: (old.pdfs && old.pdfs.length) ? old.pdfs : (r.pdfs || []),
          });
        }
        return {
          id: r.id,
          title: r.title || "",
          group: r.group || "",
          rel: r.rel || r.id,
          body: r.body || "",
          html: r.html || "",
          pdfs: r.pdfs || [],
          dirty: 0,
          deleted: 0,
          baseHash: hash(r.body || ""),
          mtime: r.mtime || 0,
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
    // ⚠ 只有**真的换了笔记**才清 AI 对话区。
    //   同步完成后 pullFromDir 会重调一次 openNote（同一篇），
    //   那时候要是也清，用户正在看的 AI 回答就被抹掉了。
    const noteChanged = state.currentId !== id;
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
    // 换笔记了，AI 面板的对话区也要跟着换 ——
    // 否则上一篇的问答会挂在新笔记下面，看着像是问这一篇的。
    if (noteChanged) {
      const aiOut = $("ai-output");
      if (aiOut) aiOut.innerHTML = "";
      const qb = $("ai-question");
      if (qb) qb.value = "";
      state.pendingSelection = "";
      aiShowContext("");
    }
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
    // ⚠ 以前这里调 openNote(n.id) 来「刷新预览」——
    //   而 openNote 是**有副作用的整页渲染**：它会把 #editor 重置成
    //   n.body、清 AI 面板、重置滚动位置。
    //   于是保存期间继续敲的字，在保存完成后被**无声抹掉**；
    //   而且 openNote 一旦抛异常（比如笔记里有嵌套公式），
    //   后面的 pushToDir 根本不会执行 —— 用户以为存了，其实只存在本地。
    // 现在只刷新正文预览，不动编辑器。
    refreshDocPreview(n);

    // ⚠ 以前只写 IndexedDB —— 平板上改的笔记**永远写不回坚果云**。
    //   结果是下次同步发现云端版本不一样，走冲突分支，
    //   每改一次就多出一份「冲突合并」副本 —— 双向同步其实是单向的。
    if (currentBackend()) {
      const r = await pushToDir(true);
      if (r.ok > 0) {
        toast("已保存并写回云盘");
        return;
      }
      if (r.fail > 0) {
        toast("已存本地，但写回云盘失败：" + r.fail + " 篇", true);
        return;
      }
    }
    toast("已保存到平板本地");
  }

  /** 只刷新正文预览（不动编辑器、不重置滚动、不清 AI 面板）。 */
  function refreshDocPreview(n) {
    const doc = $("doc");
    if (!doc || !n) return;
    try {
      doc.innerHTML = (n.html && !n.dirty) ? n.html : renderMarkdown(n.body || "");
    } catch (e) {
      console.warn("预览刷新失败", e);
    }
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
    // ⚠ 原来这里是 `quick ? "" : ""` —— 三元两边一样，
    //   等于「速记」和「新建笔记」完全是一回事（死代码）。
    //   速记 = 快速记一条（只填标题，进去再写）；
    //   新建笔记 = 连正文一起写。
    const bodyField = $("new-body-field");
    if (bodyField) bodyField.hidden = !!quick;
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
    const baseName = title.replace(/[\\/:*?"<>|]/g, "_");
    // ⚠ 文件名必须**唯一**。
    //   原来只到「标题 + 日期」—— 同一天建两篇同名笔记，第二篇的
    //   state.notes.set(id, …) + DB.putNote 会**直接覆盖第一篇**，
    //   第一篇连同正文一起消失，而且没有任何提示。
    const id = uniqueNoteId(folder, baseName, stamp);
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

  /**
   * 生成一个**不与现有笔记重名**的 id。
   *
   * ⚠ 同名覆盖是静默的数据丢失：
   *   state.notes.set(id, …) + DB.putNote 会直接顶掉原来那篇，
   *   连同正文一起消失，还没有提示。
   *   命名带上秒级时间戳 + 随机后缀；万一还是撞了（极小概率），
   *   就再追加序号，直到不重复为止。
   */
  function uniqueNoteId(folder, baseName, stamp) {
    const p = (n) => String(n).padStart(2, "0");
    const day = `${stamp.getFullYear()}${p(stamp.getMonth() + 1)}${p(stamp.getDate())}`;
    const sec = `${p(stamp.getHours())}${p(stamp.getMinutes())}${p(stamp.getSeconds())}`;
    const rand = Math.random().toString(36).slice(2, 6);
    let id = `${folder}/${baseName}_${day}_${sec}_${rand}.md`;
    let n = 2;
    while (state.notes.has(id)) {
      id = `${folder}/${baseName}_${day}_${sec}_${rand}_${n}.md`;
      n++;
      if (n > 99) break;
    }
    return id;
  }

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
      // 超时兜底 —— 原生那边要是不回调（Activity 已销毁 / 线程卡死），
      // Promise 会永远挂着, 界面就一直转圈。
      const timer = setTimeout(() => {
        if (_nativeWaiters[id]) {
          delete _nativeWaiters[id];
          reject(new Error("原生调用超时（" + method + "）"));
        }
      }, 150000);
      const finish = (fn) => (v) => { clearTimeout(timer); fn(v); };
      _nativeWaiters[id] = { resolve: finish(resolve), reject: finish(reject) };
      try {
        nativeBridge[method].apply(nativeBridge, args.concat([id]));
      } catch (e) {
        clearTimeout(timer);
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
        if (!r.ok) {
          // ⚠ 把服务端说的原因带上来 —— 否则「HTTP 500」这种话
          //   用户完全看不懂（限流时的提示是「请求太频繁」）。
          let detail = "";
          try { detail = (await r.text()).slice(0, 200); } catch (e) { /* ignore */ }
          throw new Error("列目录失败 HTTP " + r.status + (detail ? " " + detail : ""));
        }
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

  async function ensurePermission(handle, writable, quiet) {
    if (!handle) return false;
    const opts = { mode: writable ? "readwrite" : "read" };
    if (await handle.queryPermission(opts) === "granted") return true;
    // 启动时不要弹权限框（用户刚打开应用就被问一遍很烦）
    if (quiet) return false;
    return await handle.requestPermission(opts) === "granted";
  }

  /**
   * 扫描云盘里的笔记 **和原文 PDF**。
   *
   * ⚠ 以前 PDF 清单是「导出期烤进页面」的静态清单（__DATA__.pdfs）——
   *   云盘里新加的论文在平板上**根本发现不了**，文献库里永远是那 24 篇。
   *   现在改成每次同步实时扫。
   *
   * 返回 { notes: [{path,text,mtime}], pdfs: {path: {id,name,size,stem}} }
   */
  async function scanRemote(backend, quiet) {
    const notes = [];
    const pdfs = {};
    const addPdf = (path, name, size) => {
      const stem = String(name || "").replace(/\.pdf$/i, "");
      pdfs[path] = { id: path, name: name, size: size || 0, stem: stem };
    };

    if (backend.mode === "fsa") {
      const h = state.dirHandle;
      if (!await ensurePermission(h, false, quiet)) {
        if (!quiet) toast("没有读取权限", true);
        return null;
      }
      const walk = async (d, prefix) => {
        for await (const entry of d.values()) {
          if (entry.name.startsWith(".")) continue;
          if (entry.kind === "directory") {
            if (SKIP_DIRS.has(entry.name)) continue;
            await walk(entry, prefix + entry.name + "/");
          } else {
            const path = prefix + entry.name;
            const lower = entry.name.toLowerCase();
            if (lower.endsWith(".md")) {
              const file = await entry.getFile();
              notes.push({ path, text: await file.text(), mtime: file.lastModified });
            } else if (lower.endsWith(".pdf")) {
              const file = await entry.getFile();
              addPdf(path, entry.name, file.size);
            }
          }
        }
      };
      await walk(h, "");
      return { notes, pdfs };
    }

    // WebDAV: 递归列目录
    const walk = async (dir) => {
      let entries;
      try {
        entries = await backend.list(dir);
      } catch (e) {
        // ⚠ 限流要**明确报出来** —— 静默跳过的话，
        //   用户会以为「同步成功了」，其实一篇都没拉下来。
        //   别的错误（某个目录没权限）跳过就行。
        const msg = String((e && e.message) || e);
        if (msg.indexOf("限流") >= 0) {
          throw e;
        }
        return;
      }
      for (const e of entries) {
        if (e.isDir) {
          if (SKIP_DIRS.has(e.name)) continue;
          await walk(e.path);
        } else {
          const lower = e.name.toLowerCase();
          if (lower.endsWith(".md")) {
            // ⚠⚠ 没变过的笔记**不要重新下载**。
            //   坚果云对请求频率很敏感：一次同步 30 篇笔记 = 30 个 GET，
            //   加上每个目录一次 PROPFIND 就是 50+ 个请求 —— 必被限流
            //   （503 BlockedTemporarily「Too many requests are received recently」）。
            //   PROPFIND 里带了 getlastmodified，用它判断有没有变。
            //   ⚠ 本地改过（dirty）的**仍然要拉** —— 不拉就没法发现冲突。
            const local = state.notes.get(e.path);
            if (local && !local.dirty && local.mtime && e.mtime
                && local.mtime === e.mtime) {
              continue;   // 没变 —— 本地那份就是最新的，不用再下
            }
            const text = await backend.readText(e.path);
            if (text == null) continue;
            notes.push({ path: e.path, text, mtime: e.mtime || 0 });
          } else if (lower.endsWith(".pdf")) {
            // 不下载内容, 只登记清单 —— 大小从 PROPFIND 里就有
            addPdf(e.path, e.name, e.size || 0);
          }
        }
      }
    };
    await walk("");
    return { notes, pdfs };
  }

  async function pullFromDir(quiet) {
    const backend = currentBackend();
    if (!backend) return;
    let scan;
    try {
      scan = await scanRemote(backend, quiet);
    } catch (e) {
      // ⚠ 同步失败要**说出来** —— 静默失败的话，用户会以为同步好了，
      //   其实一篇都没拉下来（限流时最容易发生）。
      if (!quiet) {
        toast("同步失败：" + ((e && e.message) || e), true);
      }
      return;
    }
    if (!scan) return;
    const files = scan.notes;
    let added = 0, updated = 0, conflicts = 0;

    // 原文 PDF 清单：实时扫出来的直接覆盖旧的（云盘里新加的论文立刻能看见），
    // 旧的不删 —— 扫到一半失败时不至于把已有的原文从列表里抹掉。
    const merged = Object.assign({}, state.pdfs || {});
    Object.keys(scan.pdfs || {}).forEach((id) => { merged[id] = scan.pdfs[id]; });
    state.pdfs = merged;

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
        // 本地改过。
        // ⚠ 不能拿 baseHash 是否为空来短路 —— 平板新建的笔记 baseHash 是 ""，
        //   原来那样整个分支被跳过，云端同名版本被**无声忽略**
        //   （既不采用、也不报冲突）。
        // 判据：两边内容不一样就是冲突。
        // baseHash 有值时再额外要求「云端相对 base 也变了」，
        // 否则只是本地单方面编辑，不算冲突。
        const localHash = hash(cur.body || "");
        const cloudHash = hash(text);
        const cloudChanged = cur.baseHash
          ? (cloudHash !== cur.baseHash)
          : (cloudHash !== localHash);
        if (cloudHash !== localHash && cloudChanged) {
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
      } else if (f.mtime && cur.mtime !== f.mtime) {
        // 内容没变、只是时间戳变了（文件被 touch 过）—— 只更新 mtime。
        // 不更新的话，下次同步又会把这一篇重新下载一遍。
        cur.mtime = f.mtime;
        await DB.putNote(cur);
      }
    }
    buildNav();
    if (state.currentId) openNote(state.currentId);
    const nPdfs = Object.keys(state.pdfs || {}).length;
    $("sync-detail").textContent =
      `已连接 ${files.length} 篇 · 新增 ${added} · 更新 ${updated}` +
      (conflicts ? ` · 冲突 ${conflicts}` : "") +
      ` · 原文 ${nPdfs} 篇`;
    if (!quiet) {
      toast(`读取完成：新增 ${added}，更新 ${updated}` + (conflicts ? `，冲突 ${conflicts}` : ""));
    }
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
    // ⚠ 冲突合并稿也要唯一 —— 原来只到「分钟」，同名笔记在同一分钟内
    //   冲突两次，后一份就把前一份**覆盖**了（恰好在最易出事的时候丢数据）。
    const id = uniqueNoteId(
      localNote.group || "_其它",
      `${localNote.title.replace(/[\\/:*?"<>|]/g, "_")}_冲突`,
      stamp);
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

  /**
   * 把本地改动写回云盘。返回 {ok, fail} —— silent=true 时不弹 toast。
   *
   * ⚠ 单飞（同一时刻只跑一次）+ 写盘前后比对内容哈希。
   *   以前每次调用都独立跑一遍循环，没有互斥：
   *   连点两次「保存」时，P1 写盘途中 P2 改了 n.body 并置 dirty=1，
   *   P1 回来后执行 `n.dirty = 0; n.baseHash = hash(n.body)` ——
   *   而此时 n.body 已经是新内容了，于是**新内容被标记成"已同步"**，
   *   但真正落盘的是旧内容 → 云端拿到旧版本、本地改动消失，无任何提示。
   */
  let pushChain = Promise.resolve();

  async function pushToDir(silent) {
    // 串成一条链：后来的排在前面那次后面跑
    const run = pushChain.then(() => doPushToDir(silent), () => doPushToDir(silent));
    pushChain = run.catch(() => {});
    return run;
  }

  async function doPushToDir(silent) {
    const backend = currentBackend();
    if (!backend) {
      if (!silent) {
        toast(nativeBridge ? "还没配坚果云账号，点设置填一下" : "还没连接文件夹", true);
      }
      return { ok: 0, fail: 0 };
    }
    const dirty = [...state.notes.values()].filter((n) => n.dirty && !n.deleted);
    if (!dirty.length) {
      if (!silent) toast("没有待同步的改动");
      return { ok: 0, fail: 0 };
    }

    let ok = 0, fail = 0;
    for (const n of dirty) {
      // 写盘用**快照**，不引用可变对象
      const snap = n.body || "";
      const snapHash = hash(snap);
      try {
        await backend.writeText(n.id, snap);
        if (hash(n.body || "") === snapHash) {
          // 写盘期间没人改它 —— 可以安全地清 dirty
          n.dirty = 0;
          n.baseHash = snapHash;
          await DB.putNote(n);
          ok++;
        } else {
          // 写盘期间又改了 —— 保留 dirty，下一轮再推
          fail++;
          console.warn("写盘期间内容又变了，保留待同步标记:", n.id);
        }
      } catch (e) {
        fail++;
        console.error("写入失败", n.id, e);
      }
    }
    state.dirtyCount = [...state.notes.values()].filter((x) => x.dirty && !x.deleted).length;
    updateSyncDot();
    buildNav();
    if (!silent) {
      toast(`已写入 ${ok} 篇到云盘` + (fail ? `，失败 ${fail}` : ""), fail > 0);
    }
    return { ok, fail };
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
        } else if (cur.dirty && hash(cur.body || "") !== hash(r.body || "")) {
          // 本地改过 + 两边内容不同 ⇒ 冲突，两边都保留。
          // ⚠ 原来要求 `cur.baseHash && r.baseHash` **都非空**才走冲突分支。
          //   平板新建的笔记 baseHash 是 ""，旧版本导出的包里也没有 ——
          //   于是直接落进 else 分支：本地改动被包里的内容**静默覆盖**，
          //   dirty 还被清 0。改动的笔记就这么没了。
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
  // AI：平板直连 MiniMax（浏览器里流式，APK 里走原生代理）
  // ------------------------------------------------------------------
  const SYS_RESEARCH =
    "你是半导体/微电子方向的科研助手。用中文回答，先给结论再给依据，" +
    "涉及公式时给出 LaTeX 源码（行内 $...$，块级 $$...$$）。";

  const PROMPTS = {
    explain: (sel) => [
      { role: "system", content: SYS_RESEARCH },
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
      { role: "system", content: SYS_RESEARCH },
      { role: "user", content: q + (sel ? `\n\n【选中的原文】\n${sel}` : "") },
    ],
  };

  const AI_LABEL = { ask: "问", explain: "解读", translate: "翻译", summarize: "摘要" };
  /** 带上最近几轮对话 —— 追问时模型才知道前文在讲什么 */
  const AI_HISTORY_TURNS = 3;
  /** 历史里每条答案最多留这么多字，免得 token 越滚越大 */
  const AI_HISTORY_MAX_CHARS = 1200;

  let aiAbort = null;

  function selectedText() {
    const sel = window.getSelection();
    return sel && sel.toString().trim() ? sel.toString().trim() : "";
  }

  /**
   * 解析一段 SSE 文本，拆出正文和思考过程。
   *
   * 原生代理拿到的是**完整**的 SSE 文本（没法做流式增量），
   * 浏览器路径是边收边解析 —— 两条路共用这个函数。
   */
  function parseSse(raw) {
    let content = "";
    let reasoning = "";
    const lines = String(raw || "").split("\n");
    for (const line of lines) {
      const s = line.trim();
      if (!s.startsWith("data:")) continue;
      const p2 = s.slice(5).trim();
      if (!p2 || p2 === "[DONE]") continue;
      try {
        const obj = JSON.parse(p2);
        const ch = (obj.choices && obj.choices[0]) || {};
        const d = ch.delta || {};
        if (d.content) content += d.content;
        if (d.reasoning_content) reasoning += d.reasoning_content;
      } catch (e) { /* 心跳/畸形行忽略 */ }
    }
    return { content, reasoning };
  }

  /** 取（并按需创建）某一篇笔记的对话历史。 */
  function aiHistory(noteId) {
    if (!noteId) return [];
    let h = state.aiHistory.get(noteId);
    if (!h) {
      h = [];
      state.aiHistory.set(noteId, h);
    }
    return h;
  }

  /**
   * 往对话区追加一条「提问」记录。
   *
   * ⚠ 输入栏发完就清空（用户明确要求）—— 但问题不能跟着消失，
   *   所以它作为一条记录留在对话区里。
   *   用 textContent 赋值，不会注入 HTML。
   */
  function aiAppendTurn(kind, question, selection) {
    const out = $("ai-output");
    if (!out) return null;
    const turn = el("div", "tb-ai-turn tb-ai-turn-q");
    turn.appendChild(el("span", "tb-ai-turn-label", AI_LABEL[kind] || "问"));
    const body = el("div", "tb-ai-turn-body");
    if (kind === "ask") {
      body.textContent = question || "";
    } else if (selection) {
      const sel = el("div", "tb-ai-turn-sel");
      sel.textContent = selection.length > 280
        ? selection.slice(0, 280) + "…" : selection;
      body.appendChild(sel);
    }
    turn.appendChild(body);
    out.appendChild(turn);
    return turn;
  }

  /** 追加一个回答块，返回 [状态条, 正文容器]。 */
  function aiAppendAnswer() {
    const out = $("ai-output");
    const turn = el("div", "tb-ai-turn tb-ai-turn-a");
    const status = el("div", "tb-ai-status", "正在思考…");
    const buf = el("div");
    turn.appendChild(status);
    turn.appendChild(buf);
    out.appendChild(turn);
    out.scrollTop = out.scrollHeight;
    return [status, buf];
  }

  /** 显示/隐藏「带上原文」的提示条 —— 让人看得见这次提问会带上哪段。 */
  function aiShowContext(text) {
    const box = $("ai-context");
    const label = $("ai-context-text");
    if (!box || !label) return;
    if (text) {
      label.textContent = text.length > 200 ? text.slice(0, 200) + "…" : text;
      box.hidden = false;
    } else {
      label.textContent = "";
      box.hidden = true;
    }
  }

  /**
   * 发一次请求，把正文取回来。
   *
   * 浏览器里流式（onDelta 边收边渲染）；APK 里走原生代理（一次性拿回）。
   * 返回 {content, reasoning, aborted}。
   */
  async function aiRequest(payload, cfg, onDelta) {
    const url = `${cfg.base.replace(/\/+$/, "")}/chat/completions`;
    const bodyStr = JSON.stringify(payload);

    // APK 里页面跑在 https://appassets.androidplatform.net ——
    // 外部 AI 接口不会回这个 origin 的 Access-Control-Allow-Origin，
    // **fetch 必被 CORS 打死**。走原生代理就没有跨域问题。
    // 代价：原生代理拿不到流式增量，只能一次性拿回完整 SSE 文本。
    if (nativeBridge && typeof nativeBridge.httpPost === "function") {
      const r = await nativeCall("httpPost", [url, bodyStr, `Bearer ${cfg.key}`]);
      if (aiAbort && aiAbort.signal.aborted) {
        return { content: "", reasoning: "", aborted: true };
      }
      if (!r || !r.ok) {
        throw new Error("HTTP " + ((r && r.status) || "?") + " "
          + String((r && (r.error || r.body)) || "").slice(0, 200));
      }
      const parsed = parseSse(r.body);
      if (parsed.content && onDelta) onDelta(parsed.content);
      return parsed;
    }

    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${cfg.key}`,
      },
      body: bodyStr,
      signal: aiAbort ? aiAbort.signal : undefined,
    });
    if (!res.ok) {
      const t = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${t.slice(0, 200)}`);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let pending = "";
    let full = "";
    let reasoning = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      pending += dec.decode(value, { stream: true });
      const lines = pending.split("\n");
      pending = lines.pop();
      for (const line of lines) {
        const s = line.trim();
        if (!s.startsWith("data:")) continue;
        const p2 = s.slice(5).trim();
        if (!p2 || p2 === "[DONE]") continue;
        try {
          const obj = JSON.parse(p2);
          const ch = (obj.choices && obj.choices[0]) || {};
          const d = ch.delta || {};
          if (d.content) {
            full += d.content;
            if (onDelta) onDelta(full);
          }
          if (d.reasoning_content) reasoning += d.reasoning_content;
        } catch (e) { /* 心跳/畸形行忽略 */ }
      }
    }
    return { content: full, reasoning };
  }

  /**
   * 输入栏的 Enter 处理。抽成独立函数是为了能脱离浏览器单测。
   *
   * ⚠ isComposing 必须判断：中文输入法正在选词时按 Enter 是
   *   「确认候选词」，不是「发送」。不判断的话，
   *   选完词一按回车问题就发出去了 —— 中文用户必踩。
   */
  function aiOnKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.metaKey
        && !e.isComposing) {
      e.preventDefault();
      runAI("ask");
    }
  }

  async function runAI(kind) {
    const cfg = state.ai;
    if (!cfg.key) {
      toast("还没配置 API Key，先去设置里填一下", true);
      openSettings();
      return;
    }
    // ⚠ 记下发起时是哪篇笔记。
    //   请求是异步的 —— 用户在等待期间切了笔记的话，
    //   事后用 state.currentId 记账就会记到**另一篇**笔记名下。
    const noteId = state.currentId;
    const note = currentNote();
    const sel = selectedText() || state.pendingSelection || "";
    const bodyText = note ? (note.body || "") : "";
    if (kind === "explain" || kind === "translate") {
      if (!sel) { toast("先在正文或 PDF 里选中一段文字", true); return; }
    }

    const qBox = $("ai-question");
    const q = (qBox.value || "").trim();
    if (kind === "ask" && !q) {
      toast("写下你的问题", true);
      if (qBox) qBox.focus();
      return;
    }

    // ⚠⚠ 发出去就**清空输入栏** —— 别把问题留在框里。
    //   留着的话下一次很容易顺手按回车，把上一句原样再发一遍
    //   （而且框里堆着长问题，看不清自己到底问了什么）。
    //   问题本身不会丢：它作为一条记录留在下面的对话区里。
    if (qBox) qBox.value = "";
    state.pendingSelection = "";
    aiShowContext("");

    // 提问记录留在对话区（清空输入栏后，问题还能看到）
    aiAppendTurn(kind, q, sel);

    const model = kind === "translate" ? (cfg.modelT || cfg.model) : cfg.model;
    const base = PROMPTS[kind](
      kind === "ask" ? (sel || bodyText.slice(0, 4000)) : (sel || bodyText.slice(0, 6000)),
      q);

    // 多轮上下文：把这一篇笔记之前的问答带上 —— 追问时模型才知道前文在讲什么。
    // ⚠ system 消息必须在最前面，所以历史插在 system 和本次 user 之间。
    let messages = base;
    if (kind === "ask") {
      const hist = aiHistory(noteId);
      const ctx = hist.slice(-AI_HISTORY_TURNS * 2).map((h) => ({
        role: h.role, content: h.content,
      }));
      messages = [base[0]].concat(ctx, [base[1]]);
    }

    const [status, buf] = aiAppendAnswer();
    const out = $("ai-output");
    const stopBtn = $("btn-ai-stop");
    if (stopBtn) stopBtn.hidden = false;

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

    let answer = "";
    try {
      const res = await aiRequest(payload, cfg, (txt) => {
        buf.innerHTML = renderMarkdown(txt);
        if (out) out.scrollTop = out.scrollHeight;
      });
      if (res.aborted) {
        status.textContent = "已停止";
        return;
      }
      answer = res.content || "";
      if (answer) {
        buf.innerHTML = renderMarkdown(answer);
        status.textContent = `完成 · ${model}`;
      } else if (res.reasoning) {
        // 正文空、只剩思考 —— 思考把 max_tokens 预算吃光了
        buf.innerHTML = renderMarkdown(res.reasoning);
        status.textContent = "上游只返回了思考过程（正文预算被吃光）—— 换个模型或调大 max_tokens";
        status.setAttribute("data-error", "1");
      } else {
        status.textContent = "上游返回了空内容 — 换个模型或把 max_tokens 调大";
        status.setAttribute("data-error", "1");
      }
    } catch (e) {
      if (e.name === "AbortError") status.textContent = "已停止";
      else {
        status.textContent = "失败：" + e.message;
        status.setAttribute("data-error", "1");
      }
    } finally {
      if (stopBtn) stopBtn.hidden = true;
      // 记进历史，供下一轮追问用
      if (answer) {
        const hist = aiHistory(noteId);
        hist.push({
          role: "user",
          content: kind === "ask"
            ? q
            : (AI_LABEL[kind] + "：" + (sel || "").slice(0, 300)),
        });
        hist.push({ role: "assistant", content: answer.slice(0, AI_HISTORY_MAX_CHARS) });
        while (hist.length > AI_HISTORY_TURNS * 2) hist.shift();
      }
    }
  }

  async function testLLM() {
    const hint = $("llm-hint");
    hint.textContent = "测试中…";
    try {
      const url = `${state.ai.base.replace(/\/+$/, "")}/chat/completions`;
      const bodyStr = JSON.stringify({
        model: state.ai.model, messages: [{ role: "user", content: "1" }],
        max_tokens: 8, temperature: 0.1,
      });
      // APK 里走原生代理（页面的 fetch 会被 CORS 打死）
      if (nativeBridge && typeof nativeBridge.httpPost === "function") {
        const r = await nativeCall("httpPost", [url, bodyStr, `Bearer ${state.ai.key}`]);
        if (r && r.ok) {
          hint.textContent = "连接正常";
          hint.removeAttribute("data-error");
        } else {
          hint.textContent = `HTTP ${(r && r.status) || "?"} `
            + String((r && (r.error || r.body)) || "").slice(0, 160);
          hint.setAttribute("data-error", "1");
        }
        return;
      }
      const res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${state.ai.key}`,
        },
        body: bodyStr,
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
    zoom: 1,               // 用户缩放倍数（1 = 适合宽度）
    showTrans: true,        // 显示译文栏
    trans: null,           // {pageNum: 译文}
    annos: [],             // 批注
  };

  // ------------------------------------------------------------------
  // 译文快照 / 批注：来自「同步根」
  //
  //   同步根在 WebDAV 里是 vault 根的**兄弟目录**：
  //     「我的坚果云/MicroBench同步」   ← 译文快照 + 批注（两台设备共享）
  //     「我的坚果云/MicroBench工作台」 ← 笔记与原文 PDF
  //
  //   ⚠ 没有它，平板就只剩「能看 PDF」—— 跟用微信直接打开一个 PDF
  //     没有任何区别。译文（和标注）才是它跟电脑端对等的地方。
  // ------------------------------------------------------------------

  /** 同步根的 URL（Java 侧按 syncRoot 拼，不走 vaultRoot）。 */
  function syncUrl(kind, path) {
    const segs = String(path || "").split("/")
      .filter((s) => s && s !== "." && s !== "..")
      .map(encodeURIComponent);
    return "/dav/sync/" + kind + "/" + segs.join("/");
  }

  /**
   * 复刻桌面端的 paper_id：文件名(去.pdf, 截70) + "_" + djb2(完整相对路径)。
   * 必须与 app.py:_paper_id_for 逐字一致，否则找不到对应的快照文件。
   */
  function paperIdFor(relPath) {
    const p = String(relPath || "").replace(/\\/g, "/");
    const parts = p.split("/");
    let base = parts[parts.length - 1] || "";
    if (/\.pdf$/i.test(base)) base = base.slice(0, -4);
    base = base.slice(0, 70) || "paper";
    return base + "_" + hash(p);
  }

  /**
   * 同步目录里的文件名 = paper_id 去掉非「字母/数字/_/-」的字符。
   * ⚠ Python 的 str.isalnum() 对中文是 True，所以中文**要保留** ——
   *   这里用 \p{L}\p{N} 覆盖 Unicode 字母与数字。
   */
  function safePaperKey(pid) {
    return String(pid || "").replace(/[^\p{L}\p{N}_-]/gu, "").slice(0, 128);
  }

  /** 拉一篇论文的译文快照。返回 {pageNum: 译文}，没有就返回 null。 */
  async function loadTranslation(pdfId) {
    const key = safePaperKey(paperIdFor(pdfId));
    if (!key) return null;
    // ① 本地缓存 —— 没网也能看译文
    const cached = await DB.getMeta("trans_" + key, null);
    if (cached && Object.keys(cached).length) return cached;
    // ② 云盘同步根（最新 —— 电脑端翻完同步过来，平板立刻能看）
    try {
      const r = await fetch(syncUrl("file", "snapshots/" + key + ".json"));
      if (r.ok) {
        const data = await r.json();
        const pages = {};
        (data.pages || []).forEach((p) => {
          if (p && p.translation) pages[p.page_num] = p.translation;
        });
        if (Object.keys(pages).length) {
          await DB.setMeta("trans_" + key, pages);
          return pages;
        }
      }
    } catch (e) {
      console.warn("拉译文快照失败（离线？）", e);
    }
    // ③ 导出版里烤进来的（还没连云盘时也有）
    const baked = (state.transSeed || {})[key];
    if (baked && Object.keys(baked).length) return baked;
    return null;
  }

  /** 拉一篇论文的批注。 */
  async function loadAnnotations(pdfId) {
    const key = safePaperKey(paperIdFor(pdfId));
    if (!key) return [];
    try {
      const r = await fetch(syncUrl("file", "annotations/" + key + ".json"));
      if (!r.ok) return [];
      const data = await r.json();
      return Array.isArray(data.annotations) ? data.annotations : [];
    } catch (e) {
      console.warn("拉批注失败", e);
      return [];
    }
  }

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

  /**
   * 让整页刚好塞进可视宽度（再乘用户的缩放倍数）。
   *
   * ⚠ 之前固定 scale=1.15 —— 在平板上整页比屏幕宽，
   *   于是只能看到左半边（用户说的「只显示了一部分」）。
   */
  function pdfColumnWidth() {
    const body = $("pdf-body");
    const avail = Math.max(240, (body ? body.clientWidth : 320) - 20);
    // 宽屏是左右双栏（原文 | 译文），每栏只有一半宽
    return window.innerWidth >= 900 ? Math.max(200, Math.floor(avail / 2)) : avail;
  }

  function pdfFitScale(page) {
    const vp = page.getViewport({ scale: 1 });
    let s = pdfColumnWidth() / vp.width;
    s = Math.max(0.25, Math.min(s, 3));
    return s * (pdfState.zoom || 1);
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
      pdfState.zoom = 1;
      $("pdf-body").innerHTML = "";
      pdfStatus("正在取译文…");
      await renderPdfPage(1);
      $("pdf-pageinfo").textContent = `1 / ${pdfState.numPages}`;
      const node = document.querySelector('[data-pdf-page="1"]');
      if (node) node.scrollIntoView({ block: "start" });

      // 译文和批注：**与电脑端共享的那份**（同步根里）。
      // 拿到它们，平板才跟电脑端对等 —— 否则就只是个 PDF 阅读器。
      const [trans, annos] = await Promise.all([
        loadTranslation(pdfId),
        loadAnnotations(pdfId),
      ]);
      pdfState.trans = trans;
      pdfState.annos = annos || [];
      // 手写笔迹跟批注存在一起 —— 先把已有的笔迹装回来
      inkState.strokes = {};
      inkState.paperId = pdfId;
      (annos || []).forEach((a) => {
        if (!a || !a.strokes || !a.strokes.length) return;
        a.strokes.forEach((s) => {
          const pg = (s && s.page != null) ? s.page : 0;
          const pts = (s && s.pts) ? s.pts : (Array.isArray(s) ? s : null);
          if (pts && pts.length >= 4) inkStrokes(pg).push(pts);
        });
      });
      if (trans || (annos && annos.length)) {
        // 译文到了要重画（每页卡里要插入译文栏）
        await redrawPdf();
        const n = Object.keys(trans || {}).length;
        pdfStatus(trans
          ? `已载入译文 ${n} 页` + (annos && annos.length ? ` · 批注 ${annos.length} 条` : "")
          : (annos && annos.length ? `已载入批注 ${annos.length} 条` : ""));
        setTimeout(() => { pdfStatus(""); }, 2600);
      } else {
        pdfStatus("没有找到这台论文的译文快照（电脑端翻译后同步过来就有了）");
      }
    } catch (e) {
      pdfStatus("打开失败：" + e.message);
    }
  }

  /** 把某页的译文插进页卡。 */
  function renderTranslationInto(wrap, n) {
    if (pdfState.showTrans === false) return;
    const t = pdfState.trans && pdfState.trans[n];
    if (!t) return;
    const box = el("div", "tb-pdf-trans");
    box.appendChild(el("div", "tb-pdf-trans-head", `第 ${n} 页 · 译文（与电脑端共享）`));
    const body = el("div", "tb-pdf-trans-body");
    // renderMarkdown 内部会做白名单消毒
    body.innerHTML = renderMarkdown(t);
    box.appendChild(body);
    wrap.appendChild(box);
  }

  /** 把某页的批注高亮画在原文上。 */
  function renderAnnoInto(annoLayer, n) {
    const list = (pdfState.annos || []).filter(
      (a) => a && a.page === n && a.rects && a.rects.length);
    list.forEach((a) => {
      (a.rects || []).forEach((r) => {
        if (!r) return;
        const d = el("div", "tb-pdf-anno");
        d.style.left = (r.x * 100) + "%";
        d.style.top = (r.y * 100) + "%";
        d.style.width = (r.w * 100) + "%";
        d.style.height = (r.h * 100) + "%";
        d.setAttribute("data-color", a.color || "yellow");
        d.title = a.note || a.quote || "";
        annoLayer.appendChild(d);
      });
    });
  }

  // ------------------------------------------------------------------
  // 手写标注：在 PDF 原文上手写
  //
  //   笔迹用**归一化坐标**存（0~1），跟批注放在同一个文件里 ——
  //   这样换设备、换缩放比例都不会错位，而且能跟电脑端共享同一份。
  // ------------------------------------------------------------------

  /** {page: [ [x0,y0,x1,y1,...], ... ]} */
  const inkState = { on: false, paperId: null, strokes: {} };

  function inkStrokes(page) {
    if (!inkState.strokes[page]) inkState.strokes[page] = [];
    return inkState.strokes[page];
  }

  /** 把某一页的笔迹画到画布上。 */
  function drawInkPage(page, cv, ctx) {
    if (!cv || !ctx) return;
    const r = cv.getBoundingClientRect();
    ctx.clearRect(0, 0, r.width || cv.width, r.height || cv.height);
    const w = r.width || cv.width;
    const h = r.height || cv.height;
    ctx.strokeStyle = "#D8342A";
    ctx.lineWidth = 2.2;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    (inkState.strokes[page] || []).forEach((pts) => {
      if (!pts || pts.length < 4) return;
      ctx.beginPath();
      ctx.moveTo(pts[0] * w, pts[1] * h);
      for (let i = 2; i + 1 < pts.length; i += 2) {
        ctx.lineTo(pts[i] * w, pts[i + 1] * h);
      }
      ctx.stroke();
    });
  }

  /**
   * 给某一页的原文装上手写层。
   * @param layer  .tb-pdf-ink 容器
   * @param page   页码
   */
  function setupInkLayer(layer, page) {
    if (!layer) return;
    const cv = el("canvas");
    layer.appendChild(cv);
    const ctx = cv.getContext("2d");
    let drawing = false;
    let cur = null;

    function fit() {
      const r = layer.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      cv.width = Math.max(1, Math.floor((r.width || 1) * dpr));
      cv.height = Math.max(1, Math.floor((r.height || 1) * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawInkPage(page, cv, ctx);
    }

    function pt(e) {
      const r = layer.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      return [
        Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
        Math.max(0, Math.min(1, (e.clientY - r.top) / r.height)),
      ];
    }

    layer.addEventListener("pointerdown", (e) => {
      if (!inkState.on) return;
      e.preventDefault();
      drawing = true;
      cur = [];
      const p = pt(e);
      if (p) cur = [p[0], p[1]];
    });
    layer.addEventListener("pointermove", (e) => {
      if (!drawing) return;
      e.preventDefault();
      const p = pt(e);
      if (!p) return;
      cur.push(p[0], p[1]);
      drawInkPage(page, cv, ctx);
    });
    const end = (e) => {
      if (!drawing) return;
      drawing = false;
      if (cur && cur.length >= 4) {
        inkStrokes(page).push(cur);
        drawInkPage(page, cv, ctx);
        scheduleInkSave();
      }
      cur = null;
      if (e && e.preventDefault) e.preventDefault();
    };
    layer.addEventListener("pointerup", end);
    layer.addEventListener("pointercancel", end);
    layer.addEventListener("pointerleave", end);

    // 画布尺寸要等原文画完才知道
    fit();
    if (window.requestAnimationFrame) requestAnimationFrame(fit);
    pdfState.inkLayers = pdfState.inkLayers || {};
    pdfState.inkLayers[page] = layer;
    layer.classList.toggle("is-on", inkState.on);
  }

  /** 手写开关。 */
  function inkToggle(on) {
    inkState.on = on;
    const layers = pdfState.inkLayers || {};
    Object.keys(layers).forEach((k) => {
      const l = layers[k];
      if (l) l.classList.toggle("is-on", on);
    });
    const btn = $("pdf-toggle-ink");
    if (btn) btn.classList.toggle("is-active", on);
    if (on) toast("手写已开启：在原文上直接画。画完点「保存标注」同步到坚果云。");
  }

  let _inkSaveTimer = null;
  function scheduleInkSave() {
    if (_inkSaveTimer) clearTimeout(_inkSaveTimer);
    _inkSaveTimer = setTimeout(() => { saveInk(); }, 1200);
  }

  /**
   * 把手写笔迹存到**同步根**的批注文件里（跟电脑端共享同一份）。
   */
  async function saveInk() {
    const pdfId = pdfState.id;
    if (!pdfId) return;
    const key = safePaperKey(paperIdFor(pdfId));
    if (!key) return;
    const strokes = [];
    Object.keys(inkState.strokes).forEach((p) => {
      (inkState.strokes[p] || []).forEach((pts) => {
        strokes.push({ page: parseInt(p, 10) || 0, pts: pts });
      });
    });
    if (!strokes.length) { toast("还没有笔迹"); return; }

    // 读现有批注 → 合并（去掉旧的同 id 手写条目）→ 写回
    let data = { paper_id: key, annotations: [] };
    try {
      const r = await fetch(syncUrl("file", "annotations/" + key + ".json"));
      if (r.ok) data = await r.json();
    } catch (e) { /* 离线：下面会提示 */ }
    const list = Array.isArray(data.annotations) ? data.annotations : [];
    const id = "ink_" + key;
    const merged = list.filter((a) => a && a.id !== id);
    merged.push({
      id: id,
      page: 0,
      source: "ink",
      quote: "手写标注",
      note: "",
      color: "red",
      rects: [],
      strokes: strokes,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    const payload = {
      paper_id: key,
      updated_at: new Date().toISOString(),
      schema_version: 2,
      annotations: merged,
    };
    if (!nativeBridge || typeof nativeBridge.putSync !== "function") {
      toast("浏览器里只能存在本机；装成 APK 才会同步到坚果云", true);
      return;
    }
    try {
      await nativeCall("putSync", [
        "annotations/" + key + ".json",
        JSON.stringify(payload, null, 1),
      ]);
      toast("手写标注已存到坚果云（电脑端同步后能看到）");
    } catch (e) {
      toast("保存手写失败：" + (e && e.message ? e.message : e), true);
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
    const viewport = page.getViewport({ scale: pdfFitScale(page) });
    const dpr = window.devicePixelRatio || 1;

    const wrap = el("div", "tb-pdf-page");
    wrap.setAttribute("data-pdf-page", String(n));

    // ---- 原文侧 ----
    const orig = el("div", "tb-pdf-orig");
    const canvas = el("canvas");
    orig.appendChild(canvas);
    const textLayer = el("div", "textLayer");
    textLayer.style.width = viewport.width + "px";
    textLayer.style.height = viewport.height + "px";
    orig.appendChild(textLayer);
    // 批注高亮层（盖在原文上）
    const annoLayer = el("div", "tb-pdf-anno-layer");
    orig.appendChild(annoLayer);
    // 手写层
    const inkLayer = el("div", "tb-pdf-ink");
    orig.appendChild(inkLayer);
    wrap.appendChild(orig);

    // ---- 译文侧 ----
    renderTranslationInto(wrap, n);

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
    renderAnnoInto(annoLayer, n);
    // 手写层要等原文画完（尺寸定了）才能装
    setupInkLayer(inkLayer, n);
    pdfState.rendered.add(n);
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

  /**
   * 缩放后整本重画。
   *
   * ⚠ 必须**记住滚动锚点**再重画 ——
   *   之前重画完就停在最上面，用户会觉得「缩放之后划不动」。
   */
  async function redrawPdf() {
    if (!pdfState.doc) return;
    const pages = Array.from(pdfState.rendered).sort((a, b) => a - b);
    if (!pages.length) return;
    const body = $("pdf-body");
    const cur = pdfState.currentPage || 1;
    // 记下当前页在视口里的相对位置（0 = 刚到顶部，1 = 在底部）
    const anchor0 = body.querySelector(`[data-pdf-page="${cur}"]`);
    let ratio = 0;
    if (anchor0) {
      const br = body.getBoundingClientRect();
      const ar = anchor0.getBoundingClientRect();
      ratio = br.height ? (ar.top - br.top) / br.height : 0;
    }
    pdfState.rendered.clear();
    body.innerHTML = "";
    for (const n of pages) {
      await renderPdfPage(n);
    }
    // 重画后回到同一个位置
    const anchor1 = body.querySelector(`[data-pdf-page="${cur}"]`);
    if (anchor1) {
      const br = body.getBoundingClientRect();
      const ar = anchor1.getBoundingClientRect();
      body.scrollTop = (ar.top - br.top) + ratio * br.height - 8;
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
    // ⚠⚠ 必须绑在 **#pdf-body** 上，**不能**绑在某个 textLayer 上。
    //   textLayer 是每渲染一页就新建的，而 openPdf() 一进来就会
    //   `$("pdf-body").innerHTML = ""` 把旧的整个清掉 ——
    //   监听器绑在会被清掉的节点上，划词气泡就**永远不再出现**，
    //   而且没有任何报错（划词答疑整条功能静默不通）。
    const body = $("pdf-body");
    const bubble = $("pdf-sel");
    if (!body || !bubble) return;

    const hide = () => { bubble.hidden = true; };
    const show = (sel) => {
      const text = String(sel || "").trim();
      if (!text) { hide(); return; }
      let rect = null;
      try {
        const s = window.getSelection();
        if (!s || s.rangeCount === 0) { hide(); return; }
        rect = s.getRangeAt(0).getBoundingClientRect();
      } catch (e) {
        hide();
        return;
      }
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

    body.addEventListener("mouseup", () => {
      setTimeout(() => show(window.getSelection().toString()), 10);
    });
    body.addEventListener("touchend", () => {
      setTimeout(() => show(window.getSelection().toString()), 60);
    });
    body.addEventListener("scroll", hide, { passive: true });

    bubble.querySelectorAll("[data-pdf-ai]").forEach((b) => {
      b.onclick = () => {
        const text = bubble.dataset.text || "";
        const kind = b.dataset.pdfAi;
        hide();
        closePdf();
        // 交给 AI 面板处理
        state.pendingSelection = text;
        // ⚠ 原来这里是 `$("ai-question").value = kind === "ask" ? "" : "";`
        //   —— 三元两边一样，等于永远清空输入框（死代码）。
        //   真正该做的是：把「这次会带上哪段原文」显示出来，
        //   输入框本身留给用户写字。
        aiShowContext(kind === "ask" ? text : "");
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
        // ⚠ 走 backend.readBlob()，不要写死 fetch("/dav/file/…")。
        //   写死的话，浏览器（FSA）模式下每个请求都 404 ——
        //   「缓存全部原文」在浏览器里 100% 失败，只弹一句「失败 24」。
        //   backend.readBlob 会先看本地缓存，再按当前后端取
        //   （DAV 走云盘，FSA 走本地目录）。
        const blob = await backend.readBlob(id);
        if (!blob) { fail++; continue; }
        await DB.cachePdf(id, blob);
        ok++;
        got += blob.size || 0;
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
    $("pdf-zoom-in").onclick = () => { pdfState.zoom = Math.min(4, (pdfState.zoom || 1) * 1.2); redrawPdf(); };
    $("pdf-zoom-out").onclick = () => { pdfState.zoom = Math.max(0.4, (pdfState.zoom || 1) / 1.2); redrawPdf(); };
    // 译文开关 —— 有译文时默认开着（这是平板跟「直接看 PDF」的根本区别）
    $("pdf-toggle-trans").onclick = () => {
      pdfState.showTrans = pdfState.showTrans === false;
      $("pdf-toggle-trans").classList.toggle("is-active", pdfState.showTrans);
      redrawPdf();
    };
    // 手写开关
    $("pdf-toggle-ink").onclick = () => { inkToggle(!inkState.on); };
    // 保存手写
    $("pdf-save-ink").onclick = () => { saveInk(); };
    document.querySelectorAll("[data-close-lib]").forEach((b) => {
      b.onclick = () => { $("pdf-lib").hidden = true; };
    });
    initPdfSelection();
    initPdfScrollWatch();
    $("btn-sync").onclick = async () => {
      // ⚠ 以前这里判断的是一个**从来不出现**的 syncMode 取值（"dir"）——
      //   而 syncMode 的实际取值只有 "local"/"fsa"/"dav"/"none"，
      //   ⇒ 点顶栏的「同步」只会再弹一次设置页，
      //   真正的 pullFromDir/pushToDir 在 APK 里永远走不到。
      const b = currentBackend();
      if (b) {
        await pullFromDir();
        await pushToDir();
      } else {
        connectDir();      // APK 里 connectDir 会转去原生设置页
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

    // ---- AI 面板的输入与对话控制 ----
    const qBox = $("ai-question");
    if (qBox) {
      qBox.addEventListener("keydown", aiOnKeyDown);
    }
    const stopBtn = $("btn-ai-stop");
    if (stopBtn) {
      stopBtn.onclick = () => {
        if (aiAbort) {
          aiAbort.abort();
          toast("已停止");
        }
      };
    }
    const clearBtn = $("btn-ai-clear");
    if (clearBtn) {
      clearBtn.onclick = () => {
        const out = $("ai-output");
        if (out) out.innerHTML = "";
        state.aiHistory.delete(state.currentId);
        toast("对话已清空");
      };
    }
    const ctxClear = $("ai-context-clear");
    if (ctxClear) {
      ctxClear.onclick = () => {
        state.pendingSelection = "";
        aiShowContext("");
        toast("这次不带原文");
      };
    }

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

    // 云盘已经连上了 —— 启动后自动拉一次。
    // 以前只在「配好账号返回设置页」那一次拉取，之后每次打开应用
    // 都只能看到上次打包进来的静态种子数据 —— PC 上改过的笔记
    // 在平板上不会自己出现。
    if (state.syncMode === "dav") {
      // 延后一点，别跟首屏渲染抢主线程
      setTimeout(() => {
        pullFromDir(true).catch((e) => {
          console.warn("启动时拉取云盘失败", e);
        });
      }, 400);
    }

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
      // 把阶段轨迹亮出来 —— 平时它是隐藏的（没人看得见），
      // 但白屏时它是唯一能看出卡在哪一步的线索。
      const stage = document.getElementById("mb-stage");
      if (stage) {
        stage.hidden = false;
        stage.textContent = "启动阶段: " + (stage.textContent || "(空)");
      }
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
      // AI 面板（输入栏清空、多轮上下文这些行为必须有护栏）
      runAI: runAI,
      aiOnKeyDown: aiOnKeyDown,
      aiHistory: aiHistory,
      aiAppendTurn: aiAppendTurn,
      aiAppendAnswer: aiAppendAnswer,
      aiShowContext: aiShowContext,
      parseSse: parseSse,
      // 种子装载（本地优先：不能拿导出快照覆盖本地正文）
      loadSeed: loadSeed,
      openDB: openDB,
      // paper_id 契约（必须与 app.py:_paper_id_for 逐字一致）
      paperIdFor: paperIdFor,
      safePaperKey: safePaperKey,
      syncUrl: syncUrl,
      state: state,
    };
  }
})();
