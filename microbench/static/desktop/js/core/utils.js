// 核心基础工具：Toast、HTTP 安全解析、主题、Markdown 渲染、公式加载、摘要哈希
    let _toastTimer = null;
    // ⚠️ duration 以前是**死的** —— 函数只有两个形参，但代码库里好几处
    //    （含本分支新增的 3 处）都按 showToast(msg, isError, ms) 调，
    //    第三个参数被静默忽略 → 一律 3800ms。这里把形参补上。
    //    ms：提示停留时长。带排查建议的长文案要传长一点，否则用户还没看完就没了
    //    （历史上 15 处调用都传了第三个参数，但签名只有两个 → 全部被无声忽略）。
    function showToast(msg, isError = false, duration = 3800) {
      const t = document.getElementById("toast");
      if (!t) return;
      t.innerText = msg;
      // data-error instead of an inline hex so the toast follows the theme.
      t.setAttribute("data-error", isError ? "true" : "false");
      t.style.display = "block";
      clearTimeout(_toastTimer);
      _toastTimer = setTimeout(() => { t.style.display = "none"; }, duration);
    }

    // ------------------------------------------------------------------
    // HTTP helper：先判状态码，再解析 JSON
    // ------------------------------------------------------------------
    //
    // ⚠ 为什么不能写成 `const d = await res.json(); if (!res.ok) throw ...`：
    //   服务端 500 / 502 / 504 时，返回的往往**不是 JSON**（uvicorn 的
    //   `{"detail": ...}`、反代返回的 HTML 错误页、网关超时文本）。
    //   这时 `res.json()` 先抛 SyntaxError：
    //     - 真实状态码丢失（用户只看到 "Unexpected token '<'"）
    //     - `res.detail` 拿不到 → 报的是语法错误，不是"保存失败"
    //   → 排查方向直接被带偏。
    //
    // 这两个 helper 把顺序固定下来：**先看 res.ok，再解析**；解析失败时
    // 用状态码 + 原文片段兜底，永远不会抛 SyntaxError。
    async function _readJsonSafe(res) {
      const text = await res.text();
      let data = null;
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (e) {
          data = null;
        }
      }
      return { data, text };
    }

    function _httpErrorMessage(data, res, text, fallback) {
      // FastAPI 的校验错误：detail 是数组
      if (data && Array.isArray(data.detail)) {
        return data.detail
          .map(e => `${(e.loc || []).join('.')} — ${e.msg}`)
          .join('; ');
      }
      if (data && typeof data.detail === "string" && data.detail) return data.detail;
      if (data && typeof data.error === "string" && data.error) return data.error;
      const snippet = (text || "").trim().slice(0, 200);
      if (snippet) return `${fallback}（HTTP ${res.status}: ${snippet}）`;
      return `${fallback}（HTTP ${res.status}）`;
    }

    /** GET → 返回解析后的 JSON；非 2xx 直接抛 Error（消息可读）。 */
    async function getJson(url, opts) {
      const res = await fetch(url, opts);
      const { data, text } = await _readJsonSafe(res);
      if (!res.ok) throw new Error(_httpErrorMessage(data, res, text, "请求失败"));
      return data;
    }

    /** POST JSON → 返回解析后的 JSON；非 2xx 直接抛 Error（消息可读）。 */
    async function postJson(url, payload, opts) {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload === undefined ? {} : payload),
        ...(opts || {}),
      });
      const { data, text } = await _readJsonSafe(res);
      if (!res.ok) throw new Error(_httpErrorMessage(data, res, text, "请求失败"));
      return data;
    }

    function scrollToWork() {
      document.getElementById("workspace-core").scrollIntoView({ behavior: 'smooth' });
    }

    // ===================================================================
    // Theme: light ("linen") / night. Restored before first paint by the
    // inline bootstrap in <head>, so this only has to sync the toggle UI.
    // ===================================================================
    const _THEME_KEY = "mb_theme";
    function _applyTheme(theme) {
      document.documentElement.setAttribute("data-theme", theme);
      const icon = document.getElementById("theme-icon");
      if (icon) {
        // Sun while in night mode (click → go light), moon while light.
        icon.innerHTML = theme === "night"
          ? '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M18.7 5.3l-1.4 1.4M6.7 17.3l-1.4 1.4"/>'
          : '<path d="M20.5 14.3A8.6 8.6 0 0 1 9.7 3.5a8.6 8.6 0 1 0 10.8 10.8z"/>';
      }
      const btn = document.getElementById("theme-toggle");
      if (btn) btn.setAttribute("aria-pressed", String(theme === "night"));
    }
    function toggleTheme() {
      const next = document.documentElement.getAttribute("data-theme") === "night" ? "light" : "night";
      try { localStorage.setItem(_THEME_KEY, next); } catch (e) { /* private mode */ }
      _applyTheme(next);
    }

    // Hairline under the header firms up once the masthead scrolls away.
    //
    // ⚠ 2026-10-27 性能优化：这个监听器原来**每个滚动事件**都跑一次，
    //   而 scroll 事件在快速滚动时能每秒触发几十次 → 主线程被拖住。
    //   CPU Profiler 实测：onScroll 是首屏唯一的热点函数（占 8% 采样，
    //   远超第二名）。滚动帧 p90 96ms / max 391ms —— 用户说的「略微卡顿」。
    //
    // → 改成 **rAF 节流**：一帧最多写一次 DOM 属性。
    //   行为完全不变（还是 scrollY > 8 就切状态），只是不再每个事件都写。
    function _initHeaderScrollState() {
      const header = document.getElementById("app-header");
      if (!header) return;
      let queued = false;
      let lastState = null;
      const apply = () => {
        queued = false;
        const scrolled = String(window.scrollY > 8);
        // 状态没变就不写 DOM —— 避免无谓的样式重算
        if (scrolled !== lastState) {
          lastState = scrolled;
          header.setAttribute("data-scrolled", scrolled);
        }
      };
      const onScroll = () => {
        if (queued) return;
        queued = true;
        if (typeof requestAnimationFrame === "function") {
          requestAnimationFrame(apply);
        } else {
          apply();
        }
      };
      onScroll();
      window.addEventListener("scroll", onScroll, { passive: true });
    }


    // ---------------------------------------------------------------
    // 库懒加载: pdf.js / katex 只在用时才下载,启动更快
    // ---------------------------------------------------------------
    let _pdfJsPromise = null;
    let _katexPromise = null;
    // 本地 vendor 优先、CDN 兜底。
    // 背景：本机访问不了 cdn.jsdelivr.net，只引 CDN 会让 pdfjsLib / katex 一直
    // undefined（阅读器报「PDF.js 未加载」、公式不渲染）。vendor 文件随仓库分发
    // （microbench/static/vendor/），缺失时才回退 CDN。
    const _VENDOR = {
      pdfJsCss: "/static/vendor/pdfjs/pdf_viewer.css",
      pdfJsJs: "/static/vendor/pdfjs/pdf.min.js",
      pdfWorker: "/static/vendor/pdfjs/pdf.worker.min.js",
      katexCss: "/static/vendor/katex/katex.min.css",
      katexJs: "/static/vendor/katex/katex.min.js",
    };
    const _CDN = {
      pdfJsCss: "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/web/pdf_viewer.css",
      pdfJsJs: "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.min.js",
      pdfWorker: "https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js",
      katexCss: "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css",
      katexJs: "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js",
    };
    function _appendLazyCss(href, crossorigin, fallbackHref) {
      // CSS is render-blocking once attached, but appendChild + cdn fetch
      // can race ahead of the JS tag below — Chromium parallelizes.
      const l = document.createElement("link");
      l.rel = "stylesheet";
      l.href = href;
      if (crossorigin) l.crossOrigin = crossorigin;
      if (fallbackHref) {
        // 本地 vendor 404/缺失时自动切到 CDN，别让公式样式彻底消失
        l.onerror = () => {
          if (l.href !== fallbackHref) l.href = fallbackHref;
        };
      }
      document.head.appendChild(l);
      return l;
    }
    // 脚本同理：先试本地 vendor，onerror 再换 CDN；两处都失败才 reject。
    function _loadScriptWithFallback(primary, fallback, crossorigin) {
      return new Promise((resolve, reject) => {
        const s = document.createElement("script");
        if (crossorigin) s.crossOrigin = crossorigin;
        s.onload = () => resolve(s);
        s.onerror = () => {
          if (fallback && s.src !== fallback) {
            s.src = fallback;
          } else {
            reject(new Error("脚本加载失败（本地 vendor 与 CDN 均不可用）: " + primary));
          }
        };
        s.src = primary;
        document.head.appendChild(s);
      });
    }
    function loadPdfJs() {
      if (_pdfJsPromise) return _pdfJsPromise;
      _pdfJsPromise = (async () => {
        // CSS first — only used in PDF mode, no impact on first paint.
        _appendLazyCss(_VENDOR.pdfJsCss, null, _CDN.pdfJsCss);
        await _loadScriptWithFallback(_VENDOR.pdfJsJs, _CDN.pdfJsJs);
        if (window.pdfjsLib && window.pdfjsLib.GlobalWorkerOptions) {
          // 默认指本地 worker；loadPdfDocument() 里还有 LOCAL→CDN 候选链兜底。
          window.pdfjsLib.GlobalWorkerOptions.workerSrc = _VENDOR.pdfWorker;
        }
        if (!window.pdfjsLib) {
          throw new Error("PDF.js 未加载（本地 /static/vendor/pdfjs 与 CDN 均不可用）");
        }
        return window.pdfjsLib;
      })();
      return _pdfJsPromise;
    }
    function loadKatex() {
      if (_katexPromise) return _katexPromise;
      _katexPromise = (async () => {
        // CSS first — only used when first QA / translation renders math.
        _appendLazyCss(_VENDOR.katexCss, "anonymous", _CDN.katexCss);
        await _loadScriptWithFallback(_VENDOR.katexJs, _CDN.katexJs, "anonymous");
        if (!window.katex) {
          throw new Error("KaTeX 未加载（本地 /static/vendor/katex 与 CDN 均不可用）");
        }
        return window.katex;
      })();
      return _katexPromise;
    }
    // 把全局别名指向懒加载版本,避免后续代码改写
    window.loadPdfJs = loadPdfJs;
    window.loadKatex = loadKatex;


    function _fallbackHash16(str) {
      const s = String(str == null ? "" : str);
      let h1 = 5381, h2 = 52711;
      for (let i = 0; i < s.length; i++) {
        const c = s.charCodeAt(i);
        h1 = ((h1 << 5) + h1 + c) | 0;
        h2 = ((h2 << 5) + h2 + (c ^ (c >>> 8))) | 0;
      }
      const hex = (n) => (n >>> 0).toString(16).padStart(8, "0");
      return hex(h1) + hex(h2);
    }

    async function _sha256Hex16(str) {
      // 16-char SHA-256 hex — collision risk for 1 PDF page is negligible
      // (we don't use this for security). Avoids the cost of full 64-hex
      // serializing on every page render.
      const text = str == null ? "" : String(str);
      try {
        if (!text) return _fallbackHash16(text);
        const buf = new TextEncoder().encode(text);
        const digest = await crypto.subtle.digest("SHA-256", buf);
        const arr = Array.from(new Uint8Array(digest));
        let hex = "";
        for (let i = 0; i < 8; i++) {
          hex += arr[i].toString(16).padStart(2, "0");
        }
        return hex;
      } catch {
        // crypto.subtle unavailable (insecure context) — never return "",
        // an empty text_hash violates the backend's min_length=4.
        return _fallbackHash16(text);
      }
    }



    function escapeHtml(s) {
      // Single quotes NOT escaped: previous version left `'` intact, which
      // broke inline JS attribute handlers like `onclick="window.open('${url}')"`
      // because a vault name containing `'` would terminate the string early
      // and let an attacker inject arbitrary JS. Defense: escape both quotes.
      return (s == null ? "" : String(s))
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }
    // Escape for inside a single-quoted JS string literal (used in inline
    // onclick handlers like `window.open('${escapeJsString(url)}', '_blank')`).
    // Backslashes must be escaped first so the resulting string can be safely
    // embedded in `'..'` without quote-escape ambiguity.
    // " and the closing-script sequence are escaped as well so the
    // literal stays safe if it is ever emitted inside a script block.
    function escapeJsString(s) {
      return (s == null ? "" : String(s))
        .replace(/\\/g, "\\\\")
        .replace(/'/g, "\\'")
        .replace(/"/g, '\\"')
        .replace(/\n/g, "\\n")
        .replace(/\r/g, "\\r")
        .replace(/<\/script/gi, "<\\/script");
    }
    // Escape a value that ends up inside a single-quoted JS string literal
    // that itself lives inside a DOUBLE-QUOTED HTML attribute:
    //   onclick="feedDelete('${escapeJsAttr(path)}')"
    // Two layers are needed, and the order matters:
    //   1) HTML layer — & " < > become character references so the value
    //      cannot break out of the attribute. The single quote is
    //      deliberately NOT escaped here: escapeHtml turned ' into &#39;,
    //      the HTML parser resolved it back to ' before the handler was
    //      compiled, and a legal filename like `x');alert(1);//` became
    //      arbitrary JS in the vault page.
    //   2) JS layer — \ and ' become JS escapes so the value cannot break
    //      out of the string literal.
    function escapeJsAttr(s) {
      return escapeJsString(
        (s == null ? "" : String(s))
          .replace(/&/g, "&amp;")
          .replace(/"/g, "&quot;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
      );
    }

    // Inverse of (some of) formatMarkdownLite+renderMathIn: turn the
    // placeholder nodes back into raw `$..$` / `$$..$$` source. Used by
    // the bilingual-note exporter so the saved .md file contains
    // markdown structure (paragraphs, headings, lists, bold, code) but
    // leaves math as naked LaTeX — no <span class="katex"> AST nodes.
    // LaTeX inside the placeholder body was html-escaped by
    // formatMarkdownLite; reverse only the chars that can legitimately
    // appear inside math (`<`, `>`, `&`, `"`).
    function _unrenderMathPlaceholders(html) {
      if (!html) return "";
      // 公式原图标记（⟦FORMULA:x0,y0,x1,y1⟧）没法搬进 .md —— 坐标对读者
      // 没有意义，而且换个 PDF 就失效了。导出时换成一句人话。
      // 表格标记同理（正常情况下服务端已经换成 Markdown 表格了，这里兜底）。
      html = String(html).replace(/⟦FORMULA:[^⟧]*⟧/g, "【公式：见 PDF 原文】")
                      .replace(/⟦TABLE[^⟧]*⟧/g, "【表格：见 PDF 原文】");
      const un = (s) => String(s)
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&amp;/g, "&");
      let out = String(html)
        .replace(/<div class="math-display-block">([\s\S]*?)<\/div>/g,
                 (_, b) => "$$" + un(b) + "$$")
        .replace(/<span class="math-placeholder math-inline">([\s\S]*?)<\/span>/g,
                 (_, b) => "$" + un(b) + "$");
      return out;
    }
    // Expose for tests / other exporters
    window._unrenderMathPlaceholders = _unrenderMathPlaceholders;

    function formatMarkdownLite(s) {
      // Markdown with KaTeX math: **bold**, `code`, $..$ inline, $$..$$ display
      // Strategy: extract math first using SOH/SOH unicode markers that survive
      // HTML escape, then process markdown, then replace markers with span/div
      // containers that KaTeX will fill in.
      if (s == null) return "";

      // Use non-printable unicode chars as math delimiters (survive HTML escape)
      const D_OPEN = "\u0001", D_CLOSE = "\u0002";
      const I_OPEN = "\u0003", I_CLOSE = "\u0004";

      // 0) Normalize line endings first. Models often emit CRLF, and the
      //    paragraph splitter below looks for "\n{2,}" — "\r\n\r\n" does not
      //    match, which collapses the whole translation into one paragraph
      //    full of stray <br> tags.
      //    Then normalize math delimiters: models alternate between $..$ and
      //    \(..\) / \[..\] freely; the KaTeX pipeline only understands the
      //    dollar form, so anything else would show up as literal
      //    "\(\alpha\)" text in the translation.
      let h = String(s).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      h = h.replace(/\\\[([\s\S]+?)\\\]/g, (m, body) => "$$" + body + "$$");
      h = h.replace(/\\\(([\s\S]+?)\\\)/g, (m, body) => "$" + body + "$");

      // 1) Extract $$..$$ display math FIRST. Heuristic: if body is a single line
      //    AND short (≤ 60 chars / no multi-line / no \begin..\end), LLM almost
      //    always meant inline; downgrade to inline so layout stays compact and
      //    these short formulas don't push the paragraph apart.
      const INLINE_MAX_CHARS = 60;
      h = h.replace(/\$\$([\s\S]+?)\$\$/g, (m, body) => {
        const trimmed = body.trim();
        // Multi-line test uses the RAW body on purpose: a display formula
        // written as "$$\n...\n$$" trims down to one line, but downgrading it
        // to inline leaves the newlines behind and they turn into stray
        // <br> breaks inside the rendered formula.
        const isMultiLine = /\n/.test(body);
        const hasEnv = /\\(begin|end)\s*\{[^}]+\}/.test(trimmed);
        const tooLong = trimmed.length > INLINE_MAX_CHARS;
        if (!isMultiLine && !hasEnv && !tooLong) {
          // downgrade short inline-shaped display → inline
          return I_OPEN + trimmed + I_CLOSE;
        }
        return D_OPEN + body + D_CLOSE;
      });
      // 2) Extract $..$ inline math (avoiding $$$ escaping)
      h = h.replace(/(?<!\$)\$([^\$\n]+?)\$(?!\$)/g, (m, body) => I_OPEN + body + I_CLOSE);
      // 3) HTML escape everything
      h = escapeHtml(h);
      // 3b) Restore <sup>/<sub> that the model emitted for citation
      //     superscripts — escaped they would show up as literal tags.
      h = h.replace(/&lt;sup&gt;([\s\S]*?)&lt;\/sup&gt;/g, "<sup>$1</sup>");
      h = h.replace(/&lt;sub&gt;([\s\S]*?)&lt;\/sub&gt;/g, "<sub>$1</sub>");
      // 4) Bold
      h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      // 4b) Markdown headings (LLM translations often use # / ## on their own line)
      h = h.replace(/^###\s+(.+)$/gm, '<h4 class="md-h4">$1</h4>');
      h = h.replace(/^##\s+(.+)$/gm, '<h3 class="md-h3">$1</h3>');
      h = h.replace(/^#\s+(.+)$/gm, '<h2 class="md-h2">$1</h2>');
      // 5) Inline code (backticks survive escape, content is already escaped)
      h = h.replace(/`([^`]+)`/g, '<code class="md-code-inline">$1</code>');
      // 6) Restore display math markers as <div> placeholders
      h = h.replace(new RegExp(D_OPEN + "([\\s\\S]+?)" + D_CLOSE, "g"),
                    (_, body) => `<div class="math-display-block">${body}</div>`);
      // 7) Restore inline math markers as <span> placeholders
      h = h.replace(new RegExp(I_OPEN + "([^" + I_OPEN + I_CLOSE + "]+?)" + I_CLOSE, "g"),
                    (_, body) => `<span class="math-placeholder math-inline">${body}</span>`);

      // 7.5) Markdown 表格 → 真正的 <table>
      //     为什么必须做：模型本来就把表格输出成 "| 性能 | BaTiO3 | ..." 的
      //     Markdown 表格，但渲染层没有表格支持 → 用户看到的是一堆竖线加
      //     换行，"原有的表格形式完全看不出来"，还占掉半屏篇幅
      //     （2026-10-24 用户原话："表格的翻译，他都是很乱非常之乱……
      //     占的篇幅非常大"）。
      //     表格块单独成段，前后用空行隔开，免得被 <p> 包住。
      const _tables = [];
      {
        const _lines = h.split("\n");
        const _kept = [];
        let _i = 0;
        while (_i < _lines.length) {
          const _tbl = _matchMarkdownTable(_lines, _i);
          if (_tbl) {
            _tables.push(_tbl.html);
            _kept.push("", "\u0005TABLE" + (_tables.length - 1) + "\u0006", "");
            _i = _tbl.next;
          } else {
            _kept.push(_lines[_i]);
            _i++;
          }
        }
        h = _kept.join("\n");
      }

      // 8) Paragraphs — wrap each blank-line-separated block in <p>; CSS controls spacing.
      //    Pre-existing heading tags (h2 / h3 / h4) are preserved by being matched first.
      //    We split on blank lines, wrap each non-heading segment in <p>.
      const segments = h.split(/\n{2,}/);
      const wrapped = segments.map(seg => {
        const trimmed = seg.trim();
        if (!trimmed) return "";
        // 表格占位符 → 单独成块（不能被 <p> 包住）
        if (/^\u0005TABLE\d+\u0006$/.test(trimmed)) return trimmed;
        // NB: the display-math placeholder is a <div class="math-display-block">,
        // not a <math-display-block> tag — matching the bare tag name silently
        // failed, so display formulas got wrapped in <p> and their line breaks
        // turned into stray <br> inside the rendered formula.
        if (/^<(h\d|div\b|math-display-block|math-placeholder|code|pre|ul|ol|blockquote)/.test(trimmed)) {
          return trimmed; // already a block-level tag
        }
        return "<p>" + trimmed.replace(/\n/g, "<br>") + "</p>";
      }).join("");
      // 表格占位符 → 真正的 <table>
      return wrapped.replace(/\u0005TABLE(\d+)\u0006/g,
                             (_, idx) => _tables[Number(idx)] || "");
    }

    /**
     * 从第 i 行开始，试着识别一个 Markdown 表格。
     * 判据：第 1 行含 "|"；第 2 行是分隔行（只由 | - : 空格 组成且至少有一个 "-"）；
     *       之后的行含 "|" 就属于表格。
     * 返回 {html, next}；不是表格返回 null。
     */
    function _matchMarkdownTable(lines, i) {
      const head = (lines[i] || "").trim();
      if (!head || head.indexOf("|") < 0) return null;
      const sep = (lines[i + 1] || "").trim();
      if (!sep || sep.indexOf("-") < 0 || sep.indexOf("|") < 0) return null;
      if (!/^[\s|:-]+$/.test(sep)) return null;

      const rows = [];
      let j = i + 2;
      while (j < lines.length) {
        const ln = (lines[j] || "").trim();
        if (!ln || ln.indexOf("|") < 0) break;
        rows.push(ln);
        j++;
      }
      if (!rows.length) return null;

      // ⚠⚠ 2026-11-25 审计 P1：含 `|` 的行内公式（$P(A|B)$、$|\alpha|$）
      //   会把表格单元格**切碎**、公式本身也被劈成两半。
      //   根因：行内公式在更早的步骤（步骤 6/7）已经还原成
      //   `<span class="math-placeholder math-inline">P(A|B)</span>`，
      //   而这里的 `split("|")` **不认这个占位** → 把公式里的竖线
      //   当成了单元格分隔符。
      //   （同型的还有行内代码 `<code class="md-code-inline">a | b</code>`。）
      // → 切分前把公式/代码占位**整体换成不含 | 的 token**，切完再换回来。
      const _ATOM_PH_RE = new RegExp(
        '<span class="math-placeholder[^"]*">[\\s\\S]*?</span>' +
        '|<div class="math-display-block">[\\s\\S]*?</div>' +
        '|<code class="md-code-inline">[\\s\\S]*?</code>', "g");
      const splitCells = (row) => {
        let s = row.trim();
        if (s.startsWith("|")) s = s.slice(1);
        if (s.endsWith("|")) s = s.slice(0, -1);
        const stash = [];
        s = s.replace(_ATOM_PH_RE, (m) => {
          stash.push(m);
          return "\u0007M" + (stash.length - 1) + "\u0008";
        });
        const cells = s.split("|").map(c => c.trim());
        return cells.map(c => c.replace(/\u0007M(\d+)\u0008/g,
                                       (_, k) => stash[Number(k)] || ""));
      };
      // 对齐方式来自分隔行（:--- 左对齐 / ---: 右对齐 / :---: 居中）
      const aligns = splitCells(sep).map(c => {
        const left = c.startsWith(":");
        const right = c.endsWith(":");
        if (left && right) return ' style="text-align:center"';
        if (right) return ' style="text-align:right"';
        return "";
      });

      const headCells = splitCells(head);
      let html = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
      headCells.forEach((c, k) => {
        html += "<th" + (aligns[k] || "") + ">" + c + "</th>";
      });
      html += "</tr></thead><tbody>";
      for (const row of rows) {
        const cells = splitCells(row);
        html += "<tr>";
        headCells.forEach((_, k) => {
          html += "<td" + (aligns[k] || "") + ">" + (cells[k] || "") + "</td>";
        });
        html += "</tr>";
      }
      html += "</tbody></table></div>";
      return { html: html, next: j };
    }


    async function renderMathIn(container, _attempt) {
      if (!container) return;
      // KaTeX lazy-loaded on first need
      if (typeof katex === "undefined") {
        try { await loadKatex(); }
        catch (e) {
          const attempt = (_attempt || 0) + 1;
          if (attempt > 40) {
            // ~6s 仍未就绪：停止轮询并给出明确提示，避免无限重试刷屏
            container.querySelectorAll(".math-display-block, .math-placeholder.math-inline")
              .forEach(el => {
                if (!el.querySelector(".katex")) {
                  el.innerHTML = `<span style="color:#c43e3e; font-size:0.9em;">⚠ 公式渲染组件未加载</span>`;
                }
              });
            return;
          }
          setTimeout(() => renderMathIn(container, attempt), 150);
          return;
        }
      }
      // Display math
      container.querySelectorAll(".math-display-block").forEach(el => {
        // ⚠️ 幂等守卫：**已经渲染过就别再渲染一次**。
        // katex.render(tex, el) 把结果写回 el 之后，el.textContent 变成
        // 「MathML 的 <annotation> 原文 + 可视文本」= "H" + "H" = "HH"。
        // 再渲染一次 → "HH" → 再一次 → "HHH"……
        // 用户看到的就是 (H) 变成 (HHH)、α_ME 变成 αMEαME。
        // 触发条件：同一个元素被 renderMathIn 处理两次
        // （KaTeX 是懒加载的，首屏会并发调多次 + 150ms 重试，
        //   第一次渲染完成、第二次重试随后才拿到 katex，就会渲染两遍）。
        if (el.querySelector(".katex")) return;
        const tex = el.textContent || "";
        try {
          katex.render(tex, el, {
            displayMode: true,
            throwOnError: false,
            errorColor: "#c43e3e",
            strict: "ignore",
            trust: false,
          });
        } catch (e) {
          el.innerHTML = `<span style="color:#c43e3e; font-size:0.9em;">⚠ ${escapeHtml(String(e))}</span>`;
        }
      });
      // Inline math
      container.querySelectorAll(".math-placeholder.math-inline").forEach(el => {
        if (el.querySelector(".katex")) return;   // 同上：幂等
        const tex = el.textContent || "";
        try {
          katex.render(tex, el, {
            displayMode: false,
            throwOnError: false,
            errorColor: "#c43e3e",
            strict: "ignore",
            trust: false,
          });
        } catch (e) {
          el.innerHTML = `<span style="color:#c43e3e; font-size:0.9em;">⚠ ${escapeHtml(String(e))}</span>`;
        }
      });
    }


export {
  showToast, _readJsonSafe, _httpErrorMessage, getJson, postJson,
  scrollToWork, _THEME_KEY, _applyTheme, toggleTheme, _initHeaderScrollState,
  _VENDOR, _CDN, _appendLazyCss, _loadScriptWithFallback, loadPdfJs, loadKatex,
  _fallbackHash16, _sha256Hex16,
  escapeHtml, escapeJsString, escapeJsAttr, _unrenderMathPlaceholders, formatMarkdownLite, _matchMarkdownTable,
  renderMathIn,
};
