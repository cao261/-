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
  // ⚠⚠⚠ 只留 E_AMP 一个码点常量。以前还有 E_LT / E_GT / E_QUOT，**已删** ——
  //   它们正是这个 bug 的源头：E_LT 就是字符 '<' 本身，
  //   `E_LT + "lt;"` 产出的是 `<lt;` 而不是 `&lt;`，**等于什么都没转义**。
  //   （引号那次已经踩过并修好了：E_QUOT + "quot;" 产出的是 `"quot;"` 而不是
  //     `&quot;`，于是 `![x" onerror="alert(1)](a.png)` 能注入 onerror。）
  //   ⚠ 当时**只修了引号**，`<` / `>` 一直漏着 —— 后果：
  //     ① 代码块（<pre><code>）里的 `<script>` 会真的执行；
  //     ② 白名单外的标签（details/ontoggle、marquee/onstart…）
  //        原样进 DOM，事件属性照样触发。
  //   → 码点常量只留一个，错的写法就再也拼不出来了。
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, E_AMP + "amp;")
    .replace(/</g, E_AMP + "lt;")
    .replace(/>/g, E_AMP + "gt;")
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
    // ⚠ 结尾的 `(>|$)` 不能省。
    //   浏览器对**没闭合**的标签会在 EOF 处隐式闭合它 ——
    //   `<img src=x onerror=alert(1)`（没有 `>`）照样会变成一个带 onerror
    //   的 img 元素并触发。原来这条正则要求必须有 `>`，
    //   于是这种标签**整段原样保留** → 就是一个现成的 XSS 入口。
    out = out.replace(/<\/?([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|'[^']*'|[^>"'])*)(>|$)/g,
      (m, rawTag, attrs, closer) => {
        const tag = String(rawTag).toLowerCase();
        if (!closer) {
          // 未闭合（一直吃到字符串末尾）→ 整段当纯文本。
          // 只转义开头的 '<'：既堵住执行，又不丢后面的内容。
          return m.replace(/</, E_AMP + "lt;");
        }
        if (!ALLOWED_TAGS.has(tag)) {
          // 不在白名单 -> 当普通文本显示
          // ⚠ 必须是 E_AMP + "lt;" —— 码点常量只留 E_AMP 一个，
          //   拼不出「字符 '<' 本身」那种空转义的写法了
          //   （白名单外的标签能原样进 DOM、事件属性能执行的根因）。
          return m.replace(/</g, E_AMP + "lt;").replace(/>/g, E_AMP + "gt;");
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

  /**
   * 给用户看的错误文案 —— 先脱敏，再限长。
   *
   * ⚠ 三件事必须在这里做掉（交接文档 §8.1 / §8.2 / §8.5）：
   *   ① 抹掉 URL 里的 userinfo（https://user:pass@host）；
   *   ② 抹掉 API key —— 上游报文 / 异常消息里可能带出来
   *      （平板的 key 存在 IndexedDB 的 meta/ai 里，同源脚本读得到，
   *        所以任何一条能执行的 XSS 都等于「密钥外泄」）；
   *   ③ 抹掉绝对路径 —— 异常消息里的本机路径不该出现在界面上。
   */
  function safeErr(msg) {
    let s = String(msg == null ? "" : msg);
    // ① URL userinfo
    s = s.replace(/([a-zA-Z][a-zA-Z0-9+.-]*:\/\/)[^\s/@:]+:[^\s/@]+@/g, "$1***:***@");
    // ② API key：先抹用户自己填的那一把（最准），再抹常见形态
    const key = (state && state.ai && state.ai.key) || "";
    if (key.length >= 6) s = s.split(key).join("***");
    s = s.replace(/sk-[A-Za-z0-9_-]{8,}/g, "sk-***");
    // ⚠⚠ 值那一段必须**连 "Bearer " 一起吃掉**。
    //   原来的写法 `[:=]\s*"?)([^"\s,}]+)` 只匹配到第一个空白前的词 ——
    //   "Authorization: Bearer abcdefghij" 被改成
    //   "Authorization: *** abcdefghij"：**令牌原样留在界面上**（2026-10-27 审计）。
    //   → 可选的 "bearer\s+" 前缀 + 整个令牌，一起脱敏。
    s = s.replace(/((?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)"?\s*[:=]\s*"?)(?:bearer\s+)?[^"\s,}]+/gi, "$1***");
    // ②b 裸的 Bearer 令牌（不在 "authorization:" 后面 —— 堆栈 / 日志里常见）
    s = s.replace(/\bbearer\s+[A-Za-z0-9._~+/=-]{4,}/gi, "Bearer ***");
    // ③ 绝对路径（Windows / 安卓 / Linux / macOS）
    s = s.replace(/[A-Za-z]:\\[^\s"'<>|]*/g, "<路径>");
    s = s.replace(/\/(?:data|storage|sdcard|Users|home)\/[^\s"'<>|]*/g, "<路径>");
    s = s.replace(/file:\/\/\/[^\s"'<>|]*/g, "<路径>");
    return s.length > 400 ? s.slice(0, 400) + "…" : s;
  }

  /**
   * 上游 HTTP 错误 → 给用户看的中文。
   *
   * ⚠ 「配额耗尽」和「速率限制」必须分开说（交接文档 §4）：
   *   前者只能等额度恢复，后者等几十秒就行 —— 说错用户就会一直干等。
   *   用户原话：「M3 额度没耗尽啊，你这肯定又有什么问题出现了」。
   *
   * ⚠ 配额判定必须看**整条**报文：像
   *   「降格模型调用失败: HTTP 429: ... (原主模型报错: HTTP 429: Token Plan
   *   limit exceeded)」这种，主模型那半句才是「额度用完」的证据，
   *   只看响应体会误判成限流。
   */
  function friendlyHttpError(status, body) {
    const raw = String(body == null ? "" : body);
    const code = Number(status) || 0;
    if (code === 429) {
      if (/quota|limit exceeded|insufficient|billing|credit|余额|额度|配额/i.test(raw)) {
        return "模型额度用完了（HTTP 429 · 配额耗尽）：套餐/余额额度已用尽，现在重试也一样。"
          + "等额度恢复再试，或在设置里配一个你自己的模型。";
      }
      return "请求太频繁（HTTP 429 · 限流）：刚发出的请求超过了该模型的每分钟上限。"
        + "等几十秒再试，或换一个模型/拉长间隔。";
    }
    if (code === 401 || code === 403) {
      return "鉴权没过（HTTP " + code + "）：API key 不对或没有权限，回设置里核对一下 key。";
    }
    if (code === 404) {
      return "接口地址不对（HTTP 404）：检查设置里的 API 地址（base）有没有填全。";
    }
    if (code >= 500) {
      return "上游服务自己出错了（HTTP " + code + "）：稍等几分钟再试。";
    }
    if (code) {
      return "上游返回了 HTTP " + code + "：" + safeErr(raw).slice(0, 160);
    }
    return safeErr(raw).slice(0, 200);
  }

  /* ------------------------------------------------------------------
   * 按模型限流
   *
   * 平板是**直连** LLM API 的（不经过本机服务端），
   * 所以桌面端 `llm.py` 里的限流表对平板不生效 —— 得自己卡。
   *
   * 桌面实测过的数字：Agnes（免费版）只有 11 RPM，
   * 付费模型（MiniMax / DeepSeek）30 RPM 够用。
   * 超了就是 429 —— 上面还有「换模型重试」兜着，
   * 但先别把自己打爆。
   * ------------------------------------------------------------------ */
  const _lastReqAt = new Map();        // model -> 上次真正发出去的时刻

  function rpmFor(model) {
    const m = String(model || "").toLowerCase();
    return m.indexOf("agnes") >= 0 ? 11 : 30;
  }

  async function throttleModel(model) {
    try {
      const minGap = Math.ceil(60000 / rpmFor(model));
      const last = _lastReqAt.get(model) || 0;
      const wait = last + minGap - Date.now();
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      _lastReqAt.set(model, Date.now());
    } catch (e) {
      // 限流器出问题不能挡住正事
    }
  }

  /** djb2 — 用来判断一份内容有没有变过。刻意不用 crypto.subtle：
   *  file:// 在部分浏览器里不是 secure context，subtle 会直接不存在。 */
  function hash(s) {
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
    return h.toString(36);
  }

  /* ------------------------------------------------------------------
     带超时的 fetch
     ------------------------------------------------------------------
     ⚠ 以前 8 处 fetch **一个超时都没有** ——
       WebDAV 挂住 / 网关不回，界面就一直转圈，用户只能强杀应用。
     → 统一走 fetchWithTimeout：到点 abort，并抛出带 .timeout 标记的错误，
       上层能分辨「超时」和「网络断了」。

     ⚠ AI 请求的时限必须**比答疑时限宽**（AI_QA_TIMEOUT_MS = 300s）：
       否则 fetch 先超时 → 抛 AbortError → runAI 会当成「用户点了停止」，
       用户看到的是「已停止」，而不是「等太久了」。
     ------------------------------------------------------------------ */
  const FETCH_TIMEOUT_MS = 30000;      // 同步 / 列表这类小请求
  const AI_FETCH_TIMEOUT_MS = 330000;  // AI 请求（> AI_QA_TIMEOUT_MS）

  function fetchWithTimeout(url, opts, ms) {
    const o = opts || {};
    const limit = Number(ms) > 0 ? Number(ms) : FETCH_TIMEOUT_MS;
    const ctrl = new AbortController();
    const callerSignal = o.signal;
    let timedOut = false;
    let timer = null;
    const abort = () => { try { ctrl.abort(); } catch (e) { /* ignore */ } };
    // 调用方自己的 signal（用户点了停止）也要能中止这次请求
    if (callerSignal) {
      if (callerSignal.aborted) {
        abort();
      } else if (callerSignal.addEventListener) {
        callerSignal.addEventListener("abort", abort, { once: true });
      }
    }
    timer = setTimeout(() => { timedOut = true; abort(); }, limit);
    const opts2 = Object.assign({}, o, { signal: ctrl.signal });
    return fetch(url, opts2).then(
      (r) => { cleanup(); return r; },
      (e) => {
        cleanup();
        if (timedOut) {
          const err = new Error("请求超时（" + Math.round(limit / 1000) + " 秒没等到回应）");
          err.timeout = true;
          throw err;
        }
        throw e;
      }
    );
    function cleanup() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (callerSignal && callerSignal.removeEventListener) {
        callerSignal.removeEventListener("abort", abort);
      }
    }
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
  // ⚠⚠ 不要因为「想加个新 store」就升 DB_VER。
  //   IndexedDB 升版本需要**所有**打开着这个库的连接都让路；
  //   有一个旧版本标签页/旧 WebView 挡着，新版本就永远
  //   onblocked —— 表现是**白屏 + 「启动失败」**。
  //   本地批注因此放在已有的 meta store 里（键 "annos_<paper_id>"），
  //   不需要升版本，零风险。
  const DB_VER = 2;
  let db = null;

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VER);
      let settled = false;
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
      req.onsuccess = () => {
        settled = true;
        db = req.result;
        // 别的标签页要升版本时，把这边的连接关掉 ——
        // 不关的话它会一直挡着别人，白屏。
        try {
          db.onversionchange = () => { try { db.close(); } catch (e) { /* ignore */ } };
        } catch (e) { /* ignore */ }
        resolve(db);
      };
      req.onerror = () => {
        if (settled) return;
        settled = true;
        // ⚠ 版本对不上（本地库比这份代码新 —— 装过更新的版本又装回旧的）
        //   直接抛出去就是「白屏 + 一句看不懂的英文」。
        //   退一步：按现有版本打开、不做升级 —— 存储照常能用，
        //   只是可能少几个新功能，**但不会白屏**。
        if (req.error && req.error.name === "VersionError") {
          openDBWithoutVersion(resolve, reject);
          return;
        }
        reject(req.error);
      };
      req.onblocked = () => {
        // 被旧版本挡住了 —— 给它 6 秒自己关掉；超时给一条**能指路**的提示。
        setTimeout(() => {
          if (!settled) {
            reject(new Error("另一个标签页正开着旧版本的工作台 —— 关掉它再打开"));
          }
        }, 6000);
      };
    });
  }

  /** 不带版本号打开：沿用现有版本，不触发升级。 */
  function openDBWithoutVersion(resolve, reject) {
    try {
      const req = indexedDB.open(DB_NAME);
      req.onsuccess = () => {
        db = req.result;
        try {
          db.onversionchange = () => { try { db.close(); } catch (e) { /* ignore */ } };
        } catch (e) { /* ignore */ }
        resolve(db);
      };
      req.onerror = () => reject(req.error);
    } catch (e) {
      reject(e);
    }
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
    clearMeta() { return tx("meta", "readwrite", (s) => s.clear()); },
    clearPdfCache() { return tx("pdfcache", "readwrite", (s) => s.clear()); },
    /**
     * 清空**全部**本地数据（2026-10-27 审计 D-4）。
     *
     * ⚠ 以前「清空本地数据」只调了 clearNotes() ——
     *   meta store 里**明文存着 API Key**（state.ai.key 就从那儿读），
     *   pdfcache store 里躺着**全部原文 PDF（76MB）**，
     *   两样都留在平板上 → 「清空了」其实没清干净。
     *   → 三个 store 一起清。
     */
    async resetAll() {
      await this.clearNotes();
      await this.clearMeta();
      await this.clearPdfCache();
    },
    // ---- 本地批注（离线可写可看，联网时同步） ----
    // ⚠ 放在 meta store 里而不是新建 store —— 新建 store 要升 DB_VER，
    //   而升版本会被旧标签页挡住（白屏 + 启动失败）。零风险优先。
    getAnnos(key) {
      return this.getMeta("annos_" + key, null);
    },
    putAnnos(key, value) {
      return this.setMeta("annos_" + key, value);
    },
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

  /**
   * 公式渲染结果的**缓存**。
   *
   * ⚠ 为什么必须有（2026-10-27 审计 D-3）：
   *   AI 流式输出时每来一片就把**全量文本**重新渲染一遍 →
   *   每片都要重跑一遍 KaTeX。4000 字答案按 20 字一片 ≈ 200 片
   *   → 200 次全量 Markdown + 全部公式重渲染 → 低端平板主线程冻结数秒。
   *   同一个公式在同一次输出里会反复出现（"$x$"、"\frac{a}{b}"…），
   *   → 按「tex + 显示模式」缓存，命中就不用再跑 KaTeX。
   *
   * ⚠ 缓存**有上限** —— 公式表无上限增长同样是内存泄漏。
   */
  const _mathCache = new Map();
  const MATH_CACHE_MAX = 400;

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
      const ck = (f.display ? "D" : "I") + " " + f.tex;
      const hit = _mathCache.get(ck);
      if (hit !== undefined) return hit;
      let out;
      try {
        out = katex.renderToString(f.tex, {
          displayMode: f.display, throwOnError: false, strict: "ignore",
          output: "html",
          macros: {
            "\\RR": "\\mathbb{R}", "\\NN": "\\mathbb{N}", "\\ZZ": "\\mathbb{Z}",
            "\\CC": "\\mathbb{C}", "\\ket": "\\left|#1\\right\\rangle",
            "\\bra": "\\left\\langle#1\\right|"
          }
        });
      } catch (e) {
        out = `<code class="math-error">${esc(f.tex.trim())}</code>`;
      }
      if (_mathCache.size >= MATH_CACHE_MAX) {
        // 满了就丢最早的一条（Map 保序）
        const first = _mathCache.keys().next();
        if (!first.done) _mathCache.delete(first.value);
      }
      _mathCache.set(ck, out);
      return out;
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
    // ⚠ 以前这里还有 `trans: new Map()` 和 `annos: new Map()` ——
    //   全文件**从来没有写入过**（译文走 state.transSeed / state.transVer，
    //   批注走 pdfState.annos 和 DB.getAnnos）→ 纯死状态，删掉。
    transSeed: {},          // 导出版里烤进来的译文 {safeKey: {pageNum: 译文}}
    transVer: null,         // 当前的抽取算法版本（判断译文是不是旧的）
    transStale: false,      // 这篇的译文是旧算法翻的（已作废）
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

    if (raw && raw.pdfs) state.pdfs = trimPdfList(raw.pdfs);
    // 导出版里烤进来的译文（还没连云盘时也能看）
    if (raw && raw.trans) state.transSeed = raw.trans;
    // 当前的抽取算法版本 —— 用来判断云盘上的译文是不是旧算法翻的
    if (raw && raw.trans_ver != null) state.transVer = raw.trans_ver;

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
  /**
   * 目录树。
   *
   * ⚠⚠ **增量**重建（2026-10-27 审计 D-8）：
   *   以前每搜一下、每存一次笔记都把整个 nav-list 的 DOM 全扔了重建 ——
   *   几十条笔记时还能忍，几百条时低端平板会明显卡顿（点一下保存要等一下）。
   *   → 现在**复用**已有的按钮节点，只增删变化的部分。
   *
   * ⚠ 复用节点的前提是「id → 节点」的映射一直在（state.navNodes），
   *   节点本身不重建 —— 所以 onclick 里的 id 不用每次重绑。
   */
  const navNodes = new Map();     // note id -> 目录按钮（增量重建时复用）
  const navGroups = new Map();   // 分组名 -> 分组标题（同样复用）

  function navItemNode(n) {
    let b = navNodes.get(n.id);
    if (!b) {
      b = el("button", "tb-item");
      b.__title = document.createTextNode("");
      b.appendChild(b.__title);
      b.__sub = el("span", "tb-item-sub", "");
      b.appendChild(b.__sub);
      b.onclick = () => openNote(n.id);
      navNodes.set(n.id, b);
    }
    if (b.__title) b.__title.nodeValue = n.title || n.id;
    if (b.__sub) b.__sub.textContent = n.rel || "";
    let cls = "tb-item";
    if (n.id === state.currentId) cls += " is-active";
    if (n.dirty) cls += " is-dirty";
    b.className = cls;
    return b;
  }

  function navGroupNode(g) {
    let n = navGroups.get(g);
    if (!n) {
      n = el("div", "tb-group", g);
      navGroups.set(g, n);
    }
    return n;
  }

  /** 把 list 调整成「按 wanted 的顺序、且只包含 wanted 的节点」。 */
  function reconcileNav(list, wanted) {
    const keep = new Set(wanted);
    // ① 摘掉不再要的
    Array.from(list.children).forEach((c) => {
      if (keep.has(c)) return;
      try {
        if (c.remove) c.remove();
        else if (c.parentNode) c.parentNode.removeChild(c);
      } catch (e) { /* ignore */ }
    });
    // ② 按顺序对齐（insertBefore 会把已在表里的节点**移动**过去）
    let i = 0;
    wanted.forEach((node) => {
      const have = list.children[i];
      if (have === node) { i++; return; }
      list.insertBefore(node, have || null);
      i++;
    });
  }

  function buildNav() {
    const list = $("nav-list");
    if (!list) return;
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
      list.innerHTML = "";
      list.appendChild(el("div", "tb-empty", q ? "没有匹配的笔记" : "还没有笔记"));
      return;
    }

    const groups = FOLDERS.filter((f) => byGroup.has(f))
      .concat([...byGroup.keys()].filter((k) => FOLDERS.indexOf(k) < 0).sort());
    // ① 先算出这次要显示的节点序列
    const wanted = [];
    groups.forEach((g) => {
      wanted.push(navGroupNode(g));
      byGroup.get(g).sort((a, b) => (a.title || "").localeCompare(b.title || "", "zh"))
        .forEach((n) => {
          wanted.push(navItemNode(n));
        });
    });
    // ② 增量对齐（复用旧节点）
    reconcileNav(list, wanted);
    // ③ 回收已经不存在的笔记的节点
    navNodes.forEach((node, id) => {
      if (!state.notes.has(id)) navNodes.delete(id);
    });
    navGroups.forEach((node, g) => {
      if (!byGroup.has(g)) navGroups.delete(g);
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
    if (ok) {
      w.resolve(payload);
      return;
    }
    // ⚠⚠ **必须把 status 带出来**（2026-10-27 审计 D-1）。
    //   Java 侧回的包是 {"ok":false,"status":429,"error":"..."}
    //   （DavBridge.java:314-316 确实带了 status）——
    //   原来这里 `new Error(payload.error)` 把 status **整个丢了** →
    //   上层 `Number(e1.status) || 0` 恒为 0 →
    //   「429 换模型」「400 剥参数」两条兜底**永远不命中**，
    //   APK 上用户看到的是上游的原始英文报错。
    const p = payload || {};
    const err = new Error(p.error || "原生调用失败");
    err.status = Number(p.status) || 0;
    w.reject(err);
  };
  /**
   * 原生调用的超时兜底（毫秒），**按方法分别给**。
   *
   * ⚠ 为什么不能一个数打天下（2026-10-27 独立审查揪出来的）：
   *   原来所有原生调用共用 150s。但 **AI 答疑的真实时限是 300s**
   *   （AI_QA_TIMEOUT_MS，与电脑端 QA_TIMEOUT 一致）——
   *   于是 APK 上的 300s 超时**永远触发不了**：150s 时 nativeCall 先 reject，
   *   那个 Error 没有 .status → 不匹配 400/429 → 直接上屏「原生调用超时（httpPost）」，
   *   用户还是什么都没拿到（正是本轮要消灭的症状）。
   *
   *   → AI 请求必须给得比 300s 还宽一点（留出超时判定的余量），
   *     WebDAV 同步保持短（同步卡住就该早点失败重试）。
   */
  const NATIVE_TIMEOUT_MS = {
    httpPost: 360000,   // AI 请求：必须 > AI_QA_TIMEOUT_MS(300s)
    default: 150000,    // WebDAV 同步等：保持原值
  };
  function nativeCall(method, args) {
    return new Promise((resolve, reject) => {
      const id = "cb" + (++_nativeSeq);
      // 超时兜底 —— 原生那边要是不回调（Activity 已销毁 / 线程卡死），
      // Promise 会永远挂着, 界面就一直转圈。
      const limit = NATIVE_TIMEOUT_MS[method] || NATIVE_TIMEOUT_MS.default;
      const timer = setTimeout(() => {
        if (_nativeWaiters[id]) {
          delete _nativeWaiters[id];
          const err = new Error("原生调用超时（" + method + "）");
          // ⚠ 带上方法名标记：上层要靠它区分「这是超时，不是别的错」，
          //   否则会当成普通失败，把内部术语直接甩给用户看。
          err.nativeTimeout = true;
          err.method = method;
          reject(err);
        }
      }, limit);
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
        const r = await fetchWithTimeout(davUrl("list", dir), {}, FETCH_TIMEOUT_MS);
        if (!r.ok) {
          // ⚠ 把服务端说的原因带上来 —— 否则「HTTP 500」这种话
          //   用户完全看不懂（限流时的提示是「请求太频繁」）。
          let detail = "";
          try { detail = (await r.text()).slice(0, 200); } catch (e) { /* ignore */ }
          throw new Error("列目录失败 HTTP " + r.status + (detail ? " " + safeErr(detail) : ""));
        }
        const arr = await r.json();
        return Array.isArray(arr) ? arr : [];
      },
      async readText(path) {
        const r = await fetchWithTimeout(davUrl("file", path), {}, FETCH_TIMEOUT_MS);
        if (!r.ok) return null;
        return r.text();
      },
      async readBlob(path) {
        // 本地缓存优先 —— 缓存过的离线也能翻
        const cached = await DB.getCachedPdf(path);
        if (cached) return cached;
        const r = await fetchWithTimeout(davUrl("file", path), {}, FETCH_TIMEOUT_MS);
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
   * 问原生要「vault 在云盘里的位置」（相对 WebDAV 根）。
   *
   * ⚠ 为什么网页必须知道这个（2026-10-25 用户真机故障）：
   *   「账号密码齐了」**不等于**「连上了」——
   *   少了 vault 前缀（我的坚果云/MicroBench工作台），
   *   列目录返回**空**、下载全部 404，而界面照样显示「已连接」。
   *   → 界面必须能显示「vault 到底在哪」，才能说真话。
   */
  function davVaultRootLabel() {
    if (!nativeBridge) return "";
    try {
      if (typeof nativeBridge.davInfo !== "function") return "";
      const raw = nativeBridge.davInfo();
      if (!raw) return "";
      const info = JSON.parse(raw);
      return (info && info.vaultRoot) || "";
    } catch (e) {
      return "";
    }
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
        const vr = davVaultRootLabel();
        detail.textContent = "已连接坚果云。笔记改动直接写回云盘里的 vault 目录；" +
          "原文 PDF 点开时从云盘按需下载。" +
          // ⚠ 把 vault 的真实位置亮出来 —— 「已连接」但路径不对，
          //   是最难查的一类故障（2026-10-25 用户真机故障）。
          (vr ? "当前 vault 位置：" + vr + "。" : "⚠ vault 位置还没定，正在自动探测…");
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
    let entriesSeen = 0;
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
      entriesSeen += (entries || []).length;
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
    // ⚠⚠ 一个条目都没列出来 = **路径不对**，不是「云盘里还没东西」。
    //   这正是 2026-10-25 用户真机故障的表现：vault 前缀没配，
    //   PROPFIND 打到「我的坚果云/01_Literature」→ 服务器回 **207 + 0 个条目**
    //   （**不是 404**）→ 以前这里直接返回空数组 →
    //   界面显示「已连接 0 篇 · 新增 0」，**一点错都看不出来**。
    //   → 现在必须报错，让用户知道「路径没找对」而不是「云盘是空的」。
    if (entriesSeen === 0) {
      const vr = davVaultRootLabel();
      throw new Error("云盘里没找到 vault 目录" +
        (vr ? "（当前 vault 位置：" + vr + "）" : "（vault 位置没定）") +
        "。请点设置里的「配置坚果云」重新测一次并保存。");
    }
    return { notes, pdfs };
  }

  /**
   * 原文清单**要有上限**（2026-10-27 审计 D-7）。
   *
   * ⚠ 以前 `state.pdfs` 只 merge 不剪枝 → 云盘里论文越加越多，
   *   这张表（连带文献库分组的渲染）会一直变大，占内存不释放。
   * → 只留最近扫描到的 N 篇（对象的键序 = 插入序，末尾最新）。
   */
  const PDF_LIST_MAX = 50;

  function trimPdfList(list) {
    const out = list || {};
    const keys = Object.keys(out);
    if (keys.length <= PDF_LIST_MAX) return out;
    const keep = {};
    keys.slice(keys.length - PDF_LIST_MAX).forEach((k) => { keep[k] = out[k]; });
    return keep;
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
    state.pdfs = trimPdfList(merged);

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
  // ⚠️ 能力边界（2026-10-26，与电脑端 app.py 的提示词保持一致）：
  // 平板端是**直连模型**的（不走后端提示词），所以边界说明必须自己写一份。
  // 不写的话，问"帮我上网找张实物图"时模型会长时间空转、最后一个字都不吐
  // （实测 M3.1-Flash：43 秒思考 + 0 字正文）。
  // 写法：**先给实质内容，能力边界放最后一句**（用户原话："不需要他第一句话
  // 就说自己的能力边界……只需要最后能说一句就行了"）。
  //
  // ⚠ 2026-10-27 独立审查揪出来的漂移：能力边界这段原来在 SYS_RESEARCH 和
  // SYS_SUMMARY 里**各抄了一份**，抄着抄着就差了半句（摘要那句多了个「最后能说一句」）。
  // → 抽成**共享常量**，两个提示词都引用它 → 物理上不可能漂移。
  const CAPABILITY_BOUNDARY =
    "【能力边界】你不能联网、不能搜索网页、不能打开链接、不能获取任何图片或实物照片，" +
    "也没有其他外部工具。若问题需要这些能力，先把基于给定内容能答的部分答完" +
    "（如建议检索关键词和数据库），再在回答的最后一句用一句话说明" +
    "\"我没有联网检索能力，无法直接找到图片/最新资料\"。" +
    "不要长时间空转，不要编造链接或图片。" +
    "【必须回答】无论如何都要给出有实质内容的回答，做不到也要写清哪部分能做哪部分做不到，不允许交白卷。";

  const SYS_RESEARCH =
    "你是半导体/微电子方向的科研助手。用中文回答，先给结论再给依据，" +
    "涉及公式时给出 LaTeX 源码（行内 $...$，块级 $$...$$）。" +
    CAPABILITY_BOUNDARY;

  // ⚠ 摘要也是「科研助手」角色 —— 同样可能被问到"帮我查下最新进展"，
  //   一样要让它**自己说清做不到**，而不是长时间空转交白卷。
  //   → 引用同一份 CAPABILITY_BOUNDARY（不另抄一份，防漂移）。
  const SYS_SUMMARY =
    "你是科研助手。用中文给出结构化摘要：研究问题、方法、关键结论、" +
    "对本方向的价值与局限。分点写，不超过 600 字。" +
    CAPABILITY_BOUNDARY;

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
      { role: "system", content: SYS_SUMMARY },
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
  /** ⚠ 同时保留**多少篇笔记**的对话（2026-10-27 审计 D-7）——
   *  以前只增不减，翻几十篇笔记就攒几十份对话，内存不回收。 */
  const AI_HISTORY_MAX_NOTES = 100;

  // ------------------------------------------------------------------
  // 思考预算与时限（2026-10-27 抄电脑端 AGENTS.md §4.38 / §4.39）
  //
  // ⚠ 平板端是**直连模型**的（不走后端），所以这套预算必须自己写一份。
  //
  // 根因：**M3.1-Flash 强制思考，思考 token 与正文共用同一个 max_tokens
  // 预算池**。答疑原来给 8000 → 8K 上下文时思考就把预算吃光 → **正文 0 字**
  // → 用户看到「一直显示在思考、就是不回答」。
  //
  // 电脑端实测（7K 上下文 + 生产提示词 + 真实模型）：
  //     max_tokens=4000  → 59.9s / 正文 **0 字**
  //     max_tokens=16000 → 86.2s / 正文 **4884 字**
  //
  // → 答疑/解读/摘要统一给 16000；翻译是「关思考的直译型模型」，4000 够用
  //   （⚠ 同一个常量在不同任务里的含义不同，别照抄）。
  const AI_QA_MAX_TOKENS = 16000;
  const AI_TRANSLATE_MAX_TOKENS = 4000;
  /** 空正文后抬高预算重试时的上限。再大也没意义：32000 也可能不产出正文。 */
  const AI_MAX_ESCALATION_MAX_TOKENS = 32000;
  /**
   * 答疑时限（秒）。时限只该防「真卡死」，不该砍掉还在正常思考的请求
   * （用户原话："他即使思考再久也行，我只要他能回答问题"）。
   * 电脑端 QA_TIMEOUT = 300。
   */
  const AI_QA_TIMEOUT_MS = 300000;
  /**
   * 思考强度：用户原话"思考型模型思考要三十秒到九十秒……你把它给设定成 low"。
   *
   * ⚠ 判错的后果是 **HTTP 400**（请求直接被拒，不是静默失败）→ 判定必须保守。
   * 只有**实测认这个参数**的网关才发；未知网关一律不发。
   *
   * ⚠ 强制思考模型（M3.1-Flash）**不能**用 none —— 上游 400 原文：
   *   "reasoning effort none is not allowed" → 只能给 low。
   */
  const AI_REASONING_EFFORT = "low";
  /**
   * 实测接受 reasoning_effort 的**网关白名单**（精确主机名，不是子串）。
   *
   * ⚠ 2026-10-27 独立审查揪出来的 D4：原来用「base 里含 minimax/deepseek/agnes
   *   就算」的子串匹配 → 用户填 `https://minimax.中转.example.com/v1`
   *   这种**中转站**地址也会命中 → 发了它不认的参数 → HTTP 400，无兜底。
   *   → 改成**精确主机名白名单**，其余一律不发。
   */
  const AI_REASONING_EFFORT_OK_HOSTS = [
    "api.minimaxi.com", "api.minimax.io",
    "api.deepseek.com",
    "apihub.agnes-ai.com",
  ];

  /**
   * 这个模型是不是**强制思考**（推理 token 与正文共用 max_tokens 预算池）。
   *
   * 判据跟电脑端 llm.py 的 `model_forces_thinking` 一致：只认**已知**模型名，
   * 未知模型按「不强制」处理（宁可多给预算，也不要误判）。
   */
  function modelForcesThinking(model) {
    const m = String(model || "").trim();
    if (!m) return false;
    return m === "MiniMax-M3.1-Flash";
  }

  /**
   * 这个网关能不能接受 reasoning_effort 参数？
   *
   * 判错 → HTTP 400 → 请求直接被拒。→ 未知网关**一律不发**。
   *
   * ⚠ 判据只认 **base 的主机名**，不看模型名 —— 这跟电脑端 llm.py
   *   （base + 模型名一起判）有一处**故意的**差别：
   *   平板的用户可以把自己的接口指向**任意** OpenAI 兼容网关，
   *   那种网关上照样挂着「MiniMax-M3.1-Flash」这个模型名，
   *   但它**不一定认 reasoning_effort** → 发了就 400。
   *   → 网关才是决定「认不认这个参数」的东西，模型名不是。
   *
   * ⚠ 而且要用**精确主机名**白名单，不能用子串匹配：
   *   `https://minimax.中转.example.com` 里含 "minimax"，
   *   但那个中转站**不保证**转发 reasoning_effort。
   */
  function acceptsReasoningEffort(base, model) {
    const b = String(base || "").trim().toLowerCase();
    if (!b) return false;
    // 取主机名（去掉协议、端口、路径）
    let host = b;
    const m = b.match(/^[a-z][a-z0-9+.-]*:\/\/([^/?#]+)/);
    if (m) host = m[1];
    host = host.split("@").pop();       // 去掉 userinfo
    host = host.split(":")[0];          // 去掉端口
    if (!AI_REASONING_EFFORT_OK_HOSTS.includes(host)) {
      return false;
    }
    // 强制思考模型不能拿 none（会 400）→ 由 reasoningEffortFor 降级成 low。
    if (modelForcesThinking(model)) return true;
    return true;
  }

  /**
   * 算出这次请求该带的 reasoning_effort 值（null = 不带）。
   *
   * level: "low"（默认）/ "minimal" / "none"
   *   - 强制思考模型：none 会被 400 → 自动降级成 "low"
   *   - 不支持该参数的网关：返回 null（干脆不发）
   */
  function reasoningEffortFor(base, model, level) {
    if (!acceptsReasoningEffort(base, model)) return null;
    let lv = String(level || AI_REASONING_EFFORT).trim().toLowerCase();
    if (lv !== "low" && lv !== "minimal" && lv !== "none") lv = AI_REASONING_EFFORT;
    if (lv === "none" && modelForcesThinking(model)) return "low";
    return lv;
  }

  /**
   * 这次请求要不要带 thinking={"type":"disabled"}？
   *
   * 抄电脑端 llm.py 的 `should_send_thinking_disabled`：
   *   - MiniMax-M3.1-Flash：强制 adaptive thinking → 传了直接 **400**
   *   - Agnes：传了会**静默**把预算全花在思考上 → 正文一字不出
   *   - DeepSeek：**必须**传（不传反而正文 0 字）
   * 判据走 **provider**（模型名会变，provider 不会）。
   *
   * ⚠ 2026-10-27 独立审查揪出来的 D3：原来的写法是
   *   「除 M3.1-Flash 和 Agnes 之外**一律发**」——
   *   那意味着用户在设置里填**任意**模型名（如 gpt-4o / glm-4.6）都会带上
   *   thinking 参数，严格校验的网关直接 400。
   *   → 改成**白名单**：只有**实测支持**这个参数的 provider 才发，
   *     未知网关不发（宁可慢，不要 400）。
   */
  const AI_THINKING_DISABLED_OK_PROVIDERS = ["deepseek", "minimaxi", "minimax"];

  function shouldSendThinkingDisabled(base, model) {
    const m = String(model || "").trim();
    if (modelForcesThinking(m)) return false;
    const hay = (String(base || "") + " " + m).toLowerCase();
    // Agnes 整个 provider 不吃这个参数（静默吃光预算）
    if (hay.indexOf("agnes") >= 0) return false;
    // 未知网关不发：用户填什么模型名都有可能，宁可慢一点也不要 400
    if (!AI_THINKING_DISABLED_OK_PROVIDERS.some((p) => hay.indexOf(p) >= 0)) {
      return false;
    }
    return true;
  }

  let aiAbort = null;
  /**
   * 请求序号守卫（抄电脑端 `_qaRequestSeq` / `_isCurrentAsk`）。
   *
   * ⚠ 没有它会怎样：新请求替换旧请求后，**被中止的旧请求的回调仍会执行**
   * → 旧请求把「已取消」写进**新请求正在写的答案区** → 用户看到新答案被覆盖成
   * 「已停止」。→ 每次请求拿一个序号，回调先确认「我还是当前主人」再动手。
   */
  let aiRequestSeq = 0;

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
      // ⚠ 对话历史**只增不减**（2026-10-27 审计 D-7）——
      //   用得越久、翻的笔记越多，内存里攒的对话越多，从不回收。
      // → 只留最近 AI_HISTORY_MAX_NOTES 篇笔记的对话（Map 保序，丢最早的）。
      while (state.aiHistory.size > AI_HISTORY_MAX_NOTES) {
        const oldest = state.aiHistory.keys().next();
        if (oldest.done) break;
        state.aiHistory.delete(oldest.value);
      }
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
   *
   * ⚠ signal 必须**由调用方传进来**：不能读全局的 aiAbort ——
   *   全局变量会被下一次请求换掉（旧请求的回调就跟着新请求走了）。
   */
  async function aiRequest(payload, cfg, onDelta, signal) {
    const url = `${cfg.base.replace(/\/+$/, "")}/chat/completions`;
    const bodyStr = JSON.stringify(payload);

    // APK 里页面跑在 https://appassets.androidplatform.net ——
    // 外部 AI 接口不会回这个 origin 的 Access-Control-Allow-Origin，
    // **fetch 必被 CORS 打死**。走原生代理就没有跨域问题。
    // 代价：原生代理拿不到流式增量，只能一次性拿回完整 SSE 文本。
    if (nativeBridge && typeof nativeBridge.httpPost === "function") {
      let r = null;
      try {
        r = await nativeCall("httpPost", [url, bodyStr, `Bearer ${cfg.key}`]);
      } catch (e) {
        // ⚠ 原生桥**失败**（HTTP 非 2xx）时，桥是 reject 的，
        //   状态码在 e.status 里（__mbNativeResolve 已经挂上）。
        //   → 这里必须把它转成「友好中文 + 带 status 的 Error」，
        //     否则上层既看不到 429（换不了模型），用户也只能看到英文原文。
        const st = Number(e && e.status) || 0;
        const raw = String((e && e.message) || "");
        const err = new Error(friendlyHttpError(st, raw));
        err.status = st;
        throw err;
      }
      if (signal && signal.aborted) {
        return { content: "", reasoning: "", aborted: true };
      }
      if (!r || !r.ok) {
        const err = new Error(friendlyHttpError((r && r.status) || 0,
          String((r && (r.error || r.body)) || "")));
        // ⚠ 把状态码带出去 —— 上层要靠它判断「是不是 429」，
        //   才知道该不该换模型重试。靠字符串匹配 HTTP 429 太脆。
        err.status = Number((r && r.status) || 0) || 0;
        throw err;
      }
      const parsed = parseSse(r.body);
      if (parsed.content && onDelta) onDelta(parsed.content);
      return parsed;
    }

    const res = await fetchWithTimeout(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${cfg.key}`,
      },
      body: bodyStr,
      signal: signal || undefined,
    }, AI_FETCH_TIMEOUT_MS);
    if (!res.ok) {
      const t = await res.text().catch(() => "");
      const err = new Error(friendlyHttpError(res.status, t));
      err.status = res.status;          // ← 上层靠它判断该不该换模型
      throw err;
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

  /**
   * 把「原文 + 手写」合成一张图（给 AI 看）。
   *
   * ⚠ 手写是图形，AI 读不懂 —— 必须转成**图片**发过去（多模态）。
   *   合成而不是只截笔迹层：AI 需要看到手写标注**画在哪**。
   */
  async function captureInkPage(pageNum) {
    const wrap = document.querySelector(`[data-pdf-page="${pageNum}"]`);
    if (!wrap || !wrap.querySelector) return null;
    const orig = wrap.querySelector(".tb-pdf-orig");
    const src = orig ? orig.querySelector("canvas") : null;
    if (!src || !src.width) return null;
    const inkCv = wrap.querySelector(".tb-pdf-ink canvas");
    // 压到 1200px 宽 —— 原图带 dpr，一页可能 2000+ px，发出去太大
    const maxW = 1200;
    const scale = Math.min(1, maxW / src.width);
    const cv = document.createElement("canvas");
    cv.width = Math.max(1, Math.floor(src.width * scale));
    cv.height = Math.max(1, Math.floor(src.height * scale));
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.drawImage(src, 0, 0, cv.width, cv.height);
    if (inkCv && inkCv.width) {
      ctx.drawImage(inkCv, 0, 0, cv.width, cv.height);
    }
    try {
      return cv.toDataURL("image/jpeg", 0.82);
    } catch (e) {
      return null;
    }
  }

  /** 「带上我的手写问 AI」—— 把手写标注连同问题一起发给模型。 */
  async function askAiWithInk() {
    const pages = Object.keys(inkState.strokes)
      .filter((p) => (inkState.strokes[p] || []).length);
    if (!pages.length) {
      toast("先在手写模式下画几笔，再点这个", true);
      return;
    }
    const q = ($("ai-question").value || "").trim();
    if (!q) {
      toast("写下你的问题", true);
      $("ai-question").focus();
      return;
    }
    const img = await captureInkPage(parseInt(pages[0], 10));
    if (!img) { toast("截不到手写内容", true); return; }
    await runAI("ask", { imageDataUrl: img });
  }

  async function runAI(kind, opts) {
    const o = opts || {};
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

    // ⚠ 带上图片（手写标注）—— OpenAI 兼容的 content 数组格式。
    //   手写是图形，文字描述不出来，得让模型直接看图。
    if (o.imageDataUrl) {
      const last = messages[messages.length - 1];
      if (last && last.role === "user") {
        messages[messages.length - 1] = {
          role: "user",
          content: [
            { type: "text", text: String(last.content || "") },
            { type: "image_url", image_url: { url: o.imageDataUrl } },
          ],
        };
      }
    }

    const [status, buf] = aiAppendAnswer();
    const out = $("ai-output");
    const stopBtn = $("btn-ai-stop");
    if (stopBtn) stopBtn.hidden = false;

    // ⚠ 请求序号守卫（抄电脑端 `_qaRequestSeq` / `_isCurrentAsk`）：
    //   新请求替换旧请求后，**被中止的旧请求的回调仍会执行**。
    //   不设守卫的话，旧请求收尾会把「新请求正在用的停止按钮」藏掉，
    //   还会把旧答案塞进多轮上下文 —— 用户看着像"新问题没生效"。
    const mySeq = ++aiRequestSeq;
    const isCurrent = () => mySeq === aiRequestSeq;

    if (aiAbort) aiAbort.abort();
    const controller = new AbortController();
    aiAbort = controller;
    // 超时：只该防"真卡死"，不该砍掉还在正常思考的请求
    // （用户原话："他即使思考再久也行，我只要他能回答问题"）。
    // 电脑端 QA_TIMEOUT = 300 → 平板同样给 300s。
    let timedOut = false;
    const timeoutTimer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, AI_QA_TIMEOUT_MS);

    // 翻译用快模型并关思考；解读/答疑用推理模型
    const isTranslate = kind === "translate";
    const payload = {
      model,
      messages,
      max_tokens: isTranslate ? AI_TRANSLATE_MAX_TOKENS : AI_QA_MAX_TOKENS,
      temperature: 0.3,
      stream: true,
    };
    // ⚠ 强制思考的模型（M3.1-Flash）**不能**按直译型给 4000 ——
    //   思考会把预算吃光、正文 0 字（电脑端实测：4000 时正文只剩 291 字，
    //   8000 时一个字都没有）。→ 这种模型按"思考+正文"给预算。
    if (isTranslate && modelForcesThinking(model)) {
      payload.max_tokens = AI_QA_MAX_TOKENS;
    }
    // 翻译：直译型模型关掉思考（省时）。判据抄电脑端 llm.py：
    //   MiniMax-M3 ✅ / DeepSeek ✅（不传反而正文 0 字）/
    //   M3.1-Flash ❌（传了直接 400）/ Agnes ❌（传了静默吃光预算）
    if (isTranslate && shouldSendThinkingDisabled(cfg.base, model)) {
      payload.thinking = { type: "disabled" };
    }
    // 答疑/解读/摘要：思考强度 low（用户原话"设定成 low"）。
    // ⚠ 未知网关**一律不发** —— 判错的后果是 HTTP 400（请求直接被拒）。
    if (!isTranslate) {
      const effort = reasoningEffortFor(cfg.base, model, AI_REASONING_EFFORT);
      if (effort) payload.reasoning_effort = effort;
    }

    let answer = "";
    let usedModel = model;
    // ⚠ 等待期要**看得见**（交接文档 §2 ①）：
    //   用户原话「他有可能等了半天然后什么也没输出」。
    //   没有已用时，用户只能盯着一个不动的光标猜是不是死了。
    const t0 = Date.now();
    let gotFirstDelta = false;
    // ⚠ 节流渲染的状态必须在 try **外面**声明 ——
    //   finally 里要收掉那个定时器（try 块里的 let/const 在块外看不见，
    //   在 finally 里引用会直接 ReferenceError）。
    let pendingText = null;
    let renderTimer = null;
    let lastRenderAt = 0;
    const tick = setInterval(() => {
      if (gotFirstDelta) return;
      const s = Math.floor((Date.now() - t0) / 1000);
      status.textContent = s > 60
        ? "⏳ 还在想… · " + s + "s（可能模型在长思考，可点停止或换模型）"
        : "⏳ 思考中… · " + s + "s";
    }, 1000);
    try {
      // ⚠ 429 自动换模型（交接文档 §2 ② —— 平板最该做的一条）：
      //   平板天然有两个模型配置（cfg.model 推理 / cfg.modelT 翻译快模型），
      //   主模型额度用完或被限流 → 换另一个顶一下。
      //   **只换一次**：换完还不行就照实报错，别无限重试烧额度。
      const altModel = (kind === "translate")
        ? (cfg.model || "")
        : (cfg.modelT || "");
      /**
       * ⚠ 流式渲染**必须节流**（2026-10-27 审计 D-3）。
       *   onDelta 收到的是**全量累积文本**，每来一片就整段重渲染 = O(n²)：
       *   4000 字答案按 20 字/片 ≈ 200 片 → 200 次全量 Markdown
       *   + 全部公式重渲染 → 低端平板主线程冻结数秒（界面看着像「卡死了」）。
       *   → 最多每 100ms 渲染一次。
       *   ⚠ **最终输出内容不变**：收尾时仍然用 renderMarkdown(answer) 整段渲染一次，
       *     节流只影响「打字过程中」的中间帧。
       */
      const AI_RENDER_THROTTLE_MS = 100;
      const flushRender = () => {
        if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
        if (pendingText == null) return;
        const txt = pendingText;
        pendingText = null;
        lastRenderAt = Date.now();
        buf.innerHTML = renderMarkdown(txt);
        if (out) out.scrollTop = out.scrollHeight;
      };
      const onDelta = (txt) => {
        if (!gotFirstDelta) { gotFirstDelta = true; clearInterval(tick); }
        pendingText = txt;
        const wait = AI_RENDER_THROTTLE_MS - (Date.now() - lastRenderAt);
        if (wait <= 0) { flushRender(); return; }
        if (!renderTimer) {
          renderTimer = setTimeout(() => { renderTimer = null; flushRender(); }, wait);
        }
      };
      /**
       * 发一次请求（自带三层兜底）。
       *
       * ① 模型不认 thinking / reasoning_effort 参数 → 剥掉重试一次
       *    （还没吐内容，重试是无缝的；仿电脑端 llm.py 的 400 处理）
       * ② 429 → 换另一个模型重试一次（只换一次）
       *
       * ⚠ ① 的判据**不能只看错误文案**：上游可能说
       *   "unrecognized request argument"，也可能把参数名写在第 200 字之后。
       *   → 判据改成「**请求带了可选参数 + 400**」，文案只作辅助。
       */
      const sendOnce = async () => {
        try {
          await throttleModel(payload.model);
          return await aiRequest(payload, cfg, onDelta, controller.signal);
        } catch (e1) {
          const st = Number(e1 && e1.status) || 0;
          const msg = String((e1 && e1.message) || "");
          // ① 参数不认 → 剥掉可选参数重试一次
          //   ⚠ 顺序：先剥 reasoning_effort，再剥 thinking（两个都可能触发 400）
          if (st === 400 && payload.reasoning_effort) {
            delete payload.reasoning_effort;
            await throttleModel(payload.model);
            return await aiRequest(payload, cfg, onDelta, controller.signal);
          }
          if (st === 400 && payload.thinking) {
            delete payload.thinking;
            await throttleModel(payload.model);
            return await aiRequest(payload, cfg, onDelta, controller.signal);
          }
          if (st === 429 && altModel && altModel !== payload.model) {
            status.textContent = "主模型 " + payload.model + " 额度/限流 → 换 " + altModel + " 重试…";
            usedModel = altModel;
            payload.model = altModel;
            // 换模型后，思考开关/思考强度/预算都要按**新模型**重算
            //（原来那个是给主模型定的）
            //
            // ⚠ 预算要用 **max（下限, 当前值）**，不能用赋值覆盖
            //   （2026-10-27 独立审查揪出来的 D2）：
            //   正文空 → 预算刚从 16000 抬到 32000 → 重试正好撞 429 →
            //   换模型 → 被重置回 16000 → 若新模型也是强制思考型
            //   → 思考再吃一次预算 → **又 0 字**。用户白等三倍时间。
            if (isTranslate) {
              if (shouldSendThinkingDisabled(cfg.base, altModel)) {
                payload.thinking = { type: "disabled" };
              } else {
                delete payload.thinking;
              }
              const floor = modelForcesThinking(altModel)
                ? AI_QA_MAX_TOKENS : AI_TRANSLATE_MAX_TOKENS;
              payload.max_tokens = Math.max(payload.max_tokens, floor);
            } else {
              delete payload.thinking;
              payload.max_tokens = Math.max(payload.max_tokens, AI_QA_MAX_TOKENS);
              const effort2 = reasoningEffortFor(cfg.base, altModel, AI_REASONING_EFFORT);
              if (effort2) payload.reasoning_effort = effort2;
              else delete payload.reasoning_effort;
            }
            await throttleModel(altModel);
            return await aiRequest(payload, cfg, onDelta, controller.signal);
          }
          throw e1;
        }
      };

      let res = await sendOnce();
      if (res.aborted) {
        status.textContent = timedOut
          ? "超过 " + Math.round(AI_QA_TIMEOUT_MS / 1000) + " 秒还没返回（可能是模型在长思考）—— 可点停止或换模型重试"
          : "已停止";
        return;
      }
      answer = res.content || "";

      // ⚠ 空正文 → **先自己救一次**，别让用户干等再报错。
      //   根因：思考型模型的思考 token 与正文共用 max_tokens 预算池，
      //   思考吃光预算 → 正文 0 字（电脑端实测 4000 → 0 字 / 16000 → 4884 字）。
      //   → 能关思考的模型：关掉思考再试；关不掉的（强制思考型）：**加预算**再试。
      //   ⚠ 抄电脑端的教训：对强制思考模型"关思考"是**空操作**
      //     （参数根本不下发 → 请求与第一次完全相同 → 再等 55 秒还是 0 字）。
      if (!answer) {
        const canDisable = shouldSendThinkingDisabled(cfg.base, payload.model);
        if (canDisable) {
          payload.thinking = { type: "disabled" };
          payload.max_tokens = Math.max(payload.max_tokens, AI_QA_MAX_TOKENS);
        } else {
          payload.max_tokens = Math.min(
            Math.max(payload.max_tokens * 4, 4000), AI_MAX_ESCALATION_MAX_TOKENS);
        }
        status.textContent = canDisable
          ? "正文为空 → 关掉思考重试…"
          : "正文为空（预算被思考吃光）→ 把预算抬到 " + payload.max_tokens + " 重试…";
        buf.innerHTML = "";
        pendingText = null;
        if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
        gotFirstDelta = false;
        const res2 = await sendOnce();
        if (res2 && res2.aborted) {
          // 重试期间被中止（用户点了停止，或超时了）→ 如实说，别报「空内容」
          status.textContent = timedOut
            ? "超过 " + Math.round(AI_QA_TIMEOUT_MS / 1000) + " 秒还没返回（可能是模型在长思考）—— 可点停止或换模型重试"
            : "已停止";
          return;
        }
        if (res2) {
          res = res2;              // 后面报错时要用**最后一次**的思考内容
          answer = res2.content || "";
          if (answer) buf.innerHTML = renderMarkdown(answer);
        }
      }

      if (answer) {
        buf.innerHTML = renderMarkdown(answer);
        status.textContent = `完成 · ${usedModel}`;
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
      if (e.name === "AbortError") {
        status.textContent = timedOut
          ? "超过 " + Math.round(AI_QA_TIMEOUT_MS / 1000) + " 秒还没返回（可能是模型在长思考）—— 可点停止或换模型重试"
          : "已停止";
      } else if (e && e.nativeTimeout) {
        // ⚠ 原生代理超时（APK 专属路径）：不能把内部术语甩给用户
        //   "原生调用超时（httpPost）" —— 用户看不懂，也不知道下一步做什么。
        status.textContent = "等待模型响应超时（" + Math.round(NATIVE_TIMEOUT_MS.httpPost / 1000)
          + " 秒）—— 可能是模型在长时间思考，可点停止或换个模型重试";
        status.setAttribute("data-error", "1");
      } else {
        status.textContent = "失败：" + safeErr(e && e.message ? e.message : e);
        status.setAttribute("data-error", "1");
      }
    } finally {
      clearInterval(tick);
      clearTimeout(timeoutTimer);
      // 节流渲染的尾巴也要收掉 —— 否则收尾后还会再渲染一次旧文本。
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      pendingText = null;
      // ⚠ 只有**当前请求**才能碰共享状态（停止按钮 / 多轮上下文）。
      //   旧请求收尾时把新请求的停止按钮藏掉 → 用户没法停止新请求了。
      if (isCurrent()) {
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
        let r = null;
        try {
          r = await nativeCall("httpPost", [url, bodyStr, `Bearer ${state.ai.key}`]);
        } catch (e) {
          // ⚠ APK 里**没有跨域问题**（请求走原生代理）——
          //   原来一律说「可能是浏览器跨域限制」，是在误导用户。
          //   → 按状态码区分：401/403 说 key 不对，429 说额度/限流，
          //     其余才说「模型服务没响应」。
          const st = Number(e && e.status) || 0;
          hint.textContent = st
            ? friendlyHttpError(st, String((e && e.message) || ""))
            : ("连不上模型服务：" + safeErr(e && e.message ? e.message : e)
               + "（APK 里走的是原生代理，不是跨域问题）");
          hint.setAttribute("data-error", "1");
          return;
        }
        if (r && r.ok) {
          hint.textContent = "连接正常";
          hint.removeAttribute("data-error");
        } else {
          hint.textContent = friendlyHttpError((r && r.status) || 0,
            String((r && (r.error || r.body)) || ""));
          hint.setAttribute("data-error", "1");
        }
        return;
      }
      const res = await fetchWithTimeout(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${state.ai.key}`,
        },
        body: bodyStr,
      }, AI_FETCH_TIMEOUT_MS);
      if (res.ok) { hint.textContent = "连接正常"; hint.removeAttribute("data-error"); }
      else {
        const t = await res.text().catch(() => "");
        hint.textContent = friendlyHttpError(res.status, t);
        hint.setAttribute("data-error", "1");
      }
    } catch (e) {
      hint.textContent = "连不上：" + safeErr(e && e.message ? e.message : e)
        + "（可能是浏览器跨域限制）";
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
    layout: "both",         // both | orig | trans（原文+译文 / 仅原文 / 仅译文）
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

  /**
   * 译文快照的本地缓存键。**必须带上版本号**。
   *
   * ⚠ 为什么：缓存里存的是「按当时那个抽取算法翻出来的译文」。
   * APK 一升级（抽取算法跟着变），旧缓存里的译文就是**坏的** ——
   * 而缓存命中时根本不会去查云盘、也就不会过版本闸门，
   * 于是用户看到的就是旧算法翻的那一列（正是最早报的那个问题）。
   * 把版本编进键里 → 换版本自动失效，不用手动清缓存。
   */
  function transCacheKey(key, ver) {
    const v = (ver == null) ? "none" : String(ver);
    return "trans_v" + v + "_" + key;
  }

  /**
   * 拉一篇论文的译文快照。返回 {pageNum: 译文}，没有就返回 null。
   *
   * ⚠⚠ 抽取算法版本必须与当前一致。
   *   桌面端给快照加了 `extractor_version` —— 抽取算法一改，
   *   旧快照里的译文就是**坏的**（按旧算法翻的，可能整段栏序都是反的），
   *   桌面端读取时整份作废重翻。平板端要是照搬，用户看到的就是**错的那一列**
   *   —— 正是用户最早报的那个问题。**两端必须一致。**
   *
   *   判据与 app.py:api_pdf_snapshot_load 完全相同：
   *     `saved_ver != 当前版本` → 整份作废。
   *   ⚠「**没有** extractor_version」也算对不上 —— 那是旧算法翻的
   *     （2026-10-25 实测：本机现存 3 份快照全都没有版本号）。
   */
  async function loadTranslation(pdfId) {
    const key = safePaperKey(paperIdFor(pdfId));
    if (!key) return null;
    state.transStale = false;
    const cur = state.transVer;
    // ① 本地缓存 —— 没网也能看译文（键里带版本 → 换版本自动失效）
    if (cur != null) {
      const cached = await DB.getMeta(transCacheKey(key, cur), null);
      if (cached && Object.keys(cached).length) return cached;
    }
    // ② 云盘同步根（最新 —— 电脑端翻完同步过来，平板立刻能看）
    try {
      const r = await fetchWithTimeout(syncUrl("file", "snapshots/" + key + ".json"), {}, FETCH_TIMEOUT_MS);
      if (r.ok) {
        const data = await r.json();
        // 版本对不上就整份作废 —— 与桌面端同一个判据。
        // 不知道当前版本（cur == null）时也不能用：证明不了它是好的。
        if (cur == null || data.extractor_version !== cur) {
          state.transStale = true;
          return null;
        }
        const pages = {};
        (data.pages || []).forEach((p) => {
          if (p && p.translation) pages[p.page_num] = p.translation;
        });
        if (Object.keys(pages).length) {
          await DB.setMeta(transCacheKey(key, cur), pages);
          return pages;
        }
      }
    } catch (e) {
      console.warn("拉译文快照失败（离线？）", e);
    }
    // ③ 导出版里烤进来的（还没连云盘时也有；导出时已按版本过滤过）
    const baked = (state.transSeed || {})[key];
    if (cur != null && baked && Object.keys(baked).length) return baked;
    return null;
  }

  /**
   * 取一篇论文的批注：**本地优先**，联网时再跟云端对齐。
   *
   * ⚠ 本地优先是硬要求 —— 断网也要能看批注、也要能写批注。
   *   云盘只是「让另一台设备也能看到」的同步目标，**不是数据源**。
   *   跟电脑端的语义一致：本地改完，联网时同步上去。
   */
  async function loadAnnotations(pdfId) {
    const key = safePaperKey(paperIdFor(pdfId));
    if (!key) return [];
    // ① 本地
    const rec = await DB.getAnnos(key);
    const local = (rec && rec.items) || [];
    const dirty = !!(rec && rec.dirty);
    // ② 云端（联网时；离线就跳过，不报错）
    let cloud = [];
    let cloudDeleted = [];
    let cloudOk = false;
    try {
      const r = await fetchWithTimeout(syncUrl("file", "annotations/" + key + ".json"), {}, FETCH_TIMEOUT_MS);
      if (r.ok) {
        cloudOk = true;
        const data = await r.json();
        cloud = Array.isArray(data.annotations) ? data.annotations : [];
        cloudDeleted = Array.isArray(data.deleted) ? data.deleted : [];
      }
    } catch (e) {
      console.warn("拉批注失败（离线？）", e);
    }
    // ⚠ 云端**读不到**（离线 / 还没有这个文件）→ 本地优先，直接返回。
    //   云端读得到但列表是空的 → 那是「电脑端把批注全删了」，要照办（见下面）。
    if (!cloudOk) return local;
    // ③ 三方合并：云端 + 本地（本地优先），墓碑**最后**应用。
    //   墓碑告诉合并逻辑「这些 id 已经被删了」——
    //   不应用墓碑的话，电脑端删掉的批注会在平板上复活、还会被推回云端。
    const deleted = annoTombstones(rec, cloud, local, cloudDeleted, cloudOk);
    const merged = mergeAnnos(cloud, local, deleted);
    await DB.putAnnos(key, {
      items: merged,
      dirty: dirty ? 1 : 0,
      at: Date.now(),
      // base = **云端有的** id（平板自己还没同步上去的不算 ——
      // 算进去的话，下次会把它当成「电脑端删了」→ 自己把自己的批注删了）
      base: cloud.map((a) => a && a.id).filter(Boolean),
      deleted: deleted,
    });
    return merged;
  }

  /**
   * 三方合并：云端 / 本地 / 删除墓碑。
   *
   * ⚠ 墓碑必须**最后**应用 —— 跟电脑端 `_merge_annotation_lists`
   *   （microbench/app.py:4875）一个顺序：先按 id 合并，再删墓碑。
   *   反过来的话，同一个 id 会被 upsert 又加回来。
   *
   * ⚠ 同 id 冲突时**本地那条为准**（本地优先是平板端的硬要求）。
   */
  function mergeAnnos(base, extra, deleted) {
    const map = new Map();
    (base || []).forEach((a) => { if (a && a.id) map.set(a.id, a); });
    (extra || []).forEach((a) => { if (a && a.id) map.set(a.id, a); });
    (deleted || []).forEach((id) => { if (id) map.delete(id); });
    return Array.from(map.values()).sort((a, b) =>
      String(a.created_at || "").localeCompare(String(b.created_at || "")));
  }

  /**
   * 算出这次该应用的**删除墓碑**（被删掉的批注 id）。
   *
   * ⚠ 为什么不能只看「云端有而本地没有」（2026-10-27 审计 D-2）：
   *   「我这里没有」有好几种可能，混为一谈就会**删掉别的设备刚加的批注**：
   *     ① 本地删掉了          → 墓碑（要告诉电脑端）
   *     ② 电脑端删掉了        → 墓碑（本地也得删，别再推回去）
   *     ③ 电脑端**刚加的**、平板还没拉 → **不能**当墓碑！
   *   → 判据要靠 `base`：
   *     `base` = **上次同步时云端有的**那些 id（不是平板本地的全部）。
   *     base 里有、这次云端没了 → ② 电脑端删了 → 墓碑。
   *     base 里没有（电脑端刚加的 / 平板自己还没同步上去的）→ 保留。
   *
   * ⚠⚠ `cloudKnown` 必须传对（2026-10-27 审计 D-2 的连带坑）：
   *   云端**读失败**（离线 / DAV 挂了）时 cloud 是个空数组 ——
   *   此时若照样按「base - cloud」算墓碑，就会把**平板自己的批注全部**
   *   当成「被电脑端删了」→ 推上去等于把两边的批注全清空。
   *   → 云端状态未知时，一条墓碑都不许推算。
   *
   * @param rec          本地那条记录（{items, dirty, at, base, deleted}）
   * @param cloud        云端列表
   * @param local        本地列表
   * @param cloudDeleted 云端文件里带的墓碑（别的设备删的）
   * @param cloudKnown   云端列表**是不是真的读到了**（false = 状态未知）
   */
  function annoTombstones(rec, cloud, local, cloudDeleted, cloudKnown) {
    const tombs = new Set();
    const add = (id) => { if (id) tombs.add(id); };
    ((rec && rec.deleted) || []).forEach(add);
    (cloudDeleted || []).forEach(add);
    if (cloudKnown) {
      const cloudIds = new Set((cloud || []).map((a) => a && a.id).filter(Boolean));
      // base 里有、这次云端没了 → 电脑端删掉了 → 别再推回去
      ((rec && rec.base) || []).forEach((id) => {
        if (!cloudIds.has(id)) add(id);
      });
    }
    return Array.from(tombs);
  }

  /**
   * 保存批注：**先写本地**（立刻生效、断网也能用），再尽力同步到云盘。
   * 同步失败（离线/限流）时本地那份留着，下次再试 —— 不丢。
   */
  async function saveAnnotations(pdfId, items, silent) {
    const key = safePaperKey(paperIdFor(pdfId));
    if (!key) return false;
    // ① 本地先存 —— 硬要求：断网也能改
    const rec = await DB.getAnnos(key);
    const prevIds = new Set(((rec && rec.items) || []).map((a) => a && a.id).filter(Boolean));
    const newIds = new Set((items || []).map((a) => a && a.id).filter(Boolean));
    // ⚠ 本地删掉的 id 要记成墓碑（2026-10-27 审计 D-2）——
    //   不记的话，电脑端会把这些批注当成「平板还没删」→ 复活。
    const deleted = ((rec && rec.deleted) || []).slice();
    prevIds.forEach((id) => {
      if (!newIds.has(id) && deleted.indexOf(id) < 0) deleted.push(id);
    });
    await DB.putAnnos(key, {
      items: items,
      dirty: 1,
      at: Date.now(),
      base: (rec && rec.base) || [],
      deleted: deleted,
    });
    // ② 再同步到云盘
    const r = await pushAnnosToCloud(key, items);
    if (r.ok) {
      // 云端确认过的墓碑 → 本地也跟着删
      // （电脑端删掉的批注，别在平板上一直挂着、又被推回去）
      const gone = new Set(r.deleted || []);
      const kept = items.filter((a) => !gone.has(a && a.id));
      await DB.putAnnos(key, {
        items: kept,
        dirty: 0,
        at: Date.now(),
        // base = **云端现在有的** id（= 刚写上去的那份）
        base: (r.merged || kept).map((a) => a && a.id).filter(Boolean),
        deleted: [],
      });
      if (kept.length !== items.length && Array.isArray(pdfState.annos)) {
        pdfState.annos = kept;      // 界面也跟着更新
      }
    } else if (!silent) {
      toast("已存到平板本地（联网后会自动同步到坚果云）");
    }
    return r.ok;
  }

  /**
   * 把批注写到同步根（先读云端合并，再写回 —— 不能整份覆盖）。
   *
   * ⚠⚠ 协议字段名必须与电脑端**逐字一致**（2026-10-27 审计 D-2）：
   *   电脑端 `AnnotationSaveRequest`（microbench/app.py:5221）里有
   *     `deleted: list[str]`  —— 「本次删除掉的批注 id（跨设备合并不复活）」
   *   而 `_merge_annotation_lists` 最后会应用这批墓碑。
   *   平板原来**只写 annotations**，没有墓碑 →
   *   电脑端删掉的批注在平板上复活、又被推回云端。
   *   → 这里同样上报 `deleted`，并把墓碑应用在合并结果上。
   *
   * @returns {{ok: boolean, merged: Array|null}}
   */
  async function pushAnnosToCloud(key, items) {
    if (!nativeBridge || typeof nativeBridge.putSync !== "function") {
      return { ok: false, merged: null, deleted: [] };      // 浏览器里没有同步根
    }
    try {
      let cloud = [];
      let cloudDeleted = [];
      let cloudKnown = false;
      let readFailed = false;
      try {
        const r = await fetchWithTimeout(syncUrl("file", "annotations/" + key + ".json"), {}, FETCH_TIMEOUT_MS);
        if (r.ok) {
          cloudKnown = true;
          const d = await r.json();
          cloud = Array.isArray(d.annotations) ? d.annotations : [];
          cloudDeleted = Array.isArray(d.deleted) ? d.deleted : [];
        } else if (r.status === 404) {
          // 云端**还没有**这个文件 → 云端就是空的（不是「读失败」）
        } else {
          readFailed = true;      // 其它 HTTP 错误 → 云端状态**未知**
        }
      } catch (e) {
        readFailed = true;        // 网络/超时 → 云端状态**未知**
      }
      // ⚠⚠ 云端状态未知时**不能写**（2026-10-27 审计 D-2 的连带坑）：
      //   写上去的内容是「平板这一份」→ 等于把云端那半边覆盖掉，
      //   两边都会丢批注。宁可这次不同步（本地那份还在，下次再试）。
      if (readFailed) {
        console.warn("读不到云端批注，这轮先不写（本地那份还在）");
        return { ok: false, merged: null, deleted: [] };
      }
      const rec = await DB.getAnnos(key);
      const deleted = annoTombstones(rec, cloud, items, cloudDeleted, cloudKnown);
      const merged = mergeAnnos(cloud, items, deleted);
      const payload = {
        paper_id: key,
        updated_at: new Date().toISOString(),
        schema_version: 2,
        annotations: merged,
        // ⚠ 字段名与电脑端一致（app.py:AnnotationSaveRequest.deleted）
        deleted: deleted,
      };
      await nativeCall("putSync", [
        "annotations/" + key + ".json",
        JSON.stringify(payload, null, 1),
      ]);
      return { ok: true, merged: merged, deleted: deleted };
    } catch (e) {
      console.warn("批注同步失败", e);
      return { ok: false, merged: null, deleted: [] };
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
      const vr = davVaultRootLabel();
      pdfStatus("云盘里找不到这个文件：" + meta.name);
      toast("打不开原文：云盘里没找到这个文件。\n" +
            (vr ? "当前 vault 位置：" + vr + "\n" : "⚠ vault 位置没定（正在自动探测）\n") +
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
      } else if (state.transStale) {
        // ⚠ 译文是旧抽取算法翻的 —— 桌面端会整份作废重翻，平板也不能用
        pdfStatus("这篇的译文是旧版抽取算法翻的（栏序可能不对），已作废 —— "
          + "在电脑上重翻一次，同步过来就有了");
      } else {
        pdfStatus("没有找到这篇论文的译文快照（在电脑上翻一次，同步过来就有了）");
      }
    } catch (e) {
      pdfStatus("打开失败：" + e.message);
    }
  }

  /** 把某页的译文插进页卡。 */
  function renderTranslationInto(wrap, n) {
    if ((pdfState.layout || "both") === "orig") return;
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
      // ⚠ 尺寸为 0 时**不要**画到 1×1 的画布上（那等于没画）。
      //   layer 的尺寸取决于原文 canvas，布局没完成时它是 0。
      //   → 记下「还没量到尺寸」，等 ResizeObserver / rAF 再来一次。
      if (!r.width || !r.height) return false;
      cv.width = Math.max(1, Math.floor(r.width * dpr));
      cv.height = Math.max(1, Math.floor(r.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      drawInkPage(page, cv, ctx);
      return true;
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
      // ⚠ 把这一笔锁定到本元素上：手指划出层边界也继续画，
      //   不会因为 pointerleave 而中断（那会丢掉半笔）。
      //   捕获失败（老 WebView 不支持）不影响主流程，忽略即可。
      try { layer.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
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
      try { layer.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      // ⚠ 末尾也允许只有一个点（点一下 = 一个点），不算废笔
      if (cur && cur.length >= 2) {
        inkStrokes(page).push(cur);
        drawInkPage(page, cv, ctx);
        scheduleInkSave();
      }
      cur = null;
      if (e && e.preventDefault) e.preventDefault();
    };
    layer.addEventListener("pointerup", end);
    layer.addEventListener("pointercancel", end);
    // ⚠⚠ **不要**监听 pointerleave 来收笔（2026-10-28 用户实测：「手写没有作用」）：
    //   手指按住稍微一动就可能移出层边界 → pointerleave 提前收笔
    //   → 这一笔只攒到 1~2 个点 → 被当废笔丢掉 → 用户看到「画了没反应」。
    //   现在靠 pointer capture 兜住，移出边界也继续画。

    // 画布尺寸要等原文画完才知道
    fit();
    if (window.requestAnimationFrame) requestAnimationFrame(fit);
    // ⚠ 层尺寸会随**缩放 / 布局变化**而变（放大后原文 canvas 变大），
    //   只在装的时候量一次不够 → 用 ResizeObserver 跟着重测并重画笔迹。
    if (typeof ResizeObserver !== "undefined") {
      try {
        const ro = new ResizeObserver(() => { fit(); });
        ro.observe(layer);
        // 页面被移除时断开观察，别泄漏
        layer._inkRO = ro;
      } catch (e) { /* ignore */ }
    }
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
   * 手写批注的 id —— **必须 ≤64 字符，且同一篇论文每次都一样**。
   *
   * ⚠⚠ id 长度不能超过 64（2026-10-27 审计 D-5）：
   *   电脑端 `AnnotationItem.id` 是 `Field(..., max_length=64)`
   *   （microbench/app.py:5202）→ 平板写过去的 id 超长时，
   *   电脑端回传全量 items 会 **422**，那篇论文在电脑上**再也存不了批注**。
   *   原来的 "ink_" + key：key = paper_id 去噪后的名字，
   *   最长 70 + 1 + 7 = **78** 字符 → 必然超。
   *   → "ink_" + hash(paper_id)：djb2 稳定哈希，
   *     同一篇论文每次算出来都一样（批注不会被当成新的一条），
   *     长度 = 4 + ≤8 = ≤12 字符。
   */
  function inkAnnoId(key) {
    return "ink_" + hash(String(key || ""));
  }

  /**
   * 把手写笔迹存成一条批注（跟电脑端共享同一份文件）。
   *
   * ⚠ 先存**本地**（断网也能存），再尽力同步到云盘 ——
   *   跟电脑端的语义一致：本地改完，联网时同步上去。
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

    // ⚠⚠ id 长度**不能超过 64**（见 inkAnnoId 的说明）。
    const id = inkAnnoId(key);
    const now = new Date().toISOString();
    const items = (pdfState.annos || []).filter((a) => a && a.id !== id);
    items.push({
      id: id,
      page: 0,
      source: "ink",
      quote: "手写标注",
      note: "",
      color: "red",
      rects: [],
      strokes: strokes,
      created_at: now,
      updated_at: now,
    });
    pdfState.annos = items;
    const ok = await saveAnnotations(pdfId, items, true);
    toast(ok
      ? "手写标注已保存并同步到坚果云（电脑端同步后能看到）"
      : "手写标注已存到平板本地（联网后会自动同步）", !ok);
  }

  /**
   * 划词存成批注（跟电脑端同一套数据）。
   * @param quote 选中的原文
   * @param rects 选区在原文页面上的归一化矩形
   */
  async function saveQuoteAsAnno(quote, rects) {
    const pdfId = pdfState.id;
    if (!pdfId || !quote) return;
    const key = safePaperKey(paperIdFor(pdfId));
    if (!key) return;
    const now = new Date().toISOString();
    const id = "a" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    const items = (pdfState.annos || []).slice();
    items.push({
      id: id,
      page: pdfState.currentPage || 0,
      source: "pdf",
      quote: quote.slice(0, 4000),
      note: "",
      color: "yellow",
      rects: rects || [],
      created_at: now,
      updated_at: now,
    });
    pdfState.annos = items;
    const ok = await saveAnnotations(pdfId, items, true);
    toast(ok ? "批注已保存并同步到坚果云" : "批注已存到平板本地（联网后同步）", !ok);
  }

  /**
   * 收集选区相对于原文页面的**归一化矩形**（跟电脑端同一套算法）。
   * 这样高亮框在两台设备、两种缩放下都对得上。
   */
  function collectSelectionRects() {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return [];
    let range;
    try { range = sel.getRangeAt(0); } catch (e) { return []; }
    const rects = Array.from(range.getClientRects() || [])
      .filter((r) => r.width > 1 && r.height > 1);
    if (!rects.length) return [];
    let node = range.startContainer;
    if (node && node.nodeType === 3) node = node.parentElement;
    const wrapper = node && node.closest ? node.closest(".tb-pdf-orig") : null;
    if (!wrapper) return [];
    const wr = wrapper.getBoundingClientRect();
    if (!wr.width || !wr.height) return [];
    const out = [];
    rects.forEach((r) => {
      const x = (r.left - wr.left) / wr.width;
      const y = (r.top - wr.top) / wr.height;
      const w = r.width / wr.width;
      const h = r.height / wr.height;
      if (x < -0.02 || y < -0.02 || x > 1.02 || y > 1.02) return;  // 选到了页面外
      out.push({
        x: Math.max(0, x), y: Math.max(0, y),
        w: Math.min(1, w), h: Math.min(1, h),
      });
    });
    return out;
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

    const wrap = el("div", "tb-pdf-page lay-" + (pdfState.layout || "both"));
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

    const body = $("pdf-body");
    // 按页码顺序插入,保证连续阅读时顺序正确
    let before = null;
    for (const child of Array.from(body.children)) {
      const pn = parseInt(child.getAttribute("data-pdf-page") || "0", 10);
      if (pn > n) { before = child; break; }
    }
    body.insertBefore(wrap, before);

    await paintCanvas(canvas, page, viewport, dpr);

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
    // ⚠ 离屏页的 canvas 及时释放（见 pruneCanvases）
    //   ⚠ 以「用户当前所在的页」为中心，不是刚画完的那一页 ——
    //   预渲染下一页时若按 n 算，会把用户正在看的那一页给释放掉（一片空白）。
    pruneCanvases(pdfState.currentPage || n);
  }

  /**
   * 把一页画到 canvas 上（新建页卡、补画离屏页共用这一段）。
   */
  async function paintCanvas(canvas, page, viewport, dpr) {
    if (!canvas) return;
    canvas.width = Math.floor(viewport.width * dpr);
    canvas.height = Math.floor(viewport.height * dpr);
    canvas.style.width = viewport.width + "px";
    canvas.style.height = viewport.height + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    await page.render({ canvasContext: ctx, viewport }).promise;
  }

  /**
   * ⚠⚠ 只保留当前页 ±2 的 canvas（2026-10-27 审计 D-7）。
   *
   *   dpr=2 时一页 canvas 约 7~8 MB；翻几十页就是几百 MB ——
   *   低端平板会被系统直接杀掉（用户看到的是「应用自己关了」）。
   *   以前 `pdfState.rendered` 只增不减，canvas 也从不释放。
   *
   * ⚠ **只销毁 canvas，不动页卡节点**：
   *   删掉节点会改变文档高度 → 滚动位置跳 → 用户正在看的那页被顶走。
   *   页卡本身（译文栏 / 文本层 / 高亮层）留着，内存占用很小。
   *   滚回来时由 restoreCanvas() 补画。
   */
  const PDF_CANVAS_WINDOW = 2;

  function pruneCanvases(current) {
    const body = $("pdf-body");
    if (!body) return;
    const cur = Number(current) || 1;
    Array.from(body.children).forEach((child) => {
      const n = parseInt(child.getAttribute("data-pdf-page") || "0", 10);
      if (!n) return;
      if (Math.abs(n - cur) <= PDF_CANVAS_WINDOW) return;
      if (child.getAttribute && child.getAttribute("data-canvas-released") === "1") return;
      // ⚠ 只动**原文**那张 canvas —— 手写那张是用户画的，丢了就没了。
      //   （手写的数据在 inkState.strokes 里，恢复时能重画回来。）
      const canvas = child.querySelector ? child.querySelector(".tb-pdf-orig canvas") : null;
      if (!canvas || !canvas.width) return;
      canvas.width = 0;
      canvas.height = 0;      // ← 这一步才真的把那几 MB 放掉
      if (child.setAttribute) child.setAttribute("data-canvas-released", "1");
    });
  }

  /**
   * 离屏页的 canvas 被释放后，滚回来时补画回来。
   * （笔迹也在 inkState.strokes 里，一起补回来。）
   */
  async function restoreCanvas(n) {
    const wrap = document.querySelector(`[data-pdf-page="${n}"]`);
    if (!wrap) return;
    if (!wrap.getAttribute || wrap.getAttribute("data-canvas-released") !== "1") return;
    const doc = pdfState.doc;
    if (!doc) return;
    try {
      const pdfjsLib = await loadPdfJs();
      const page = await doc.getPage(n);
      const viewport = page.getViewport({ scale: pdfFitScale(page) });
      const dpr = window.devicePixelRatio || 1;
      const canvas = wrap.querySelector ? wrap.querySelector(".tb-pdf-orig canvas") : null;
      if (!canvas) return;
      wrap.removeAttribute("data-canvas-released");
      await paintCanvas(canvas, page, viewport, dpr);
      // 手写层也补回来（笔迹数据在 inkState.strokes）
      const inkCv = wrap.querySelector ? wrap.querySelector(".tb-pdf-ink canvas") : null;
      if (inkCv) {
        const layer = inkCv.parentElement;
        const r = layer && layer.getBoundingClientRect ? layer.getBoundingClientRect() : null;
        const w = (r && r.width) || inkCv.width || 1;
        const h = (r && r.height) || inkCv.height || 1;
        inkCv.width = Math.max(1, Math.floor(w * dpr));
        inkCv.height = Math.max(1, Math.floor(h * dpr));
        const ictx = inkCv.getContext("2d");
        ictx.setTransform(dpr, 0, 0, dpr, 0, 0);
        drawInkPage(n, inkCv, ictx);
      }
    } catch (e) {
      console.warn("补画离屏页失败", e);
    }
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
        // ⚠ 离屏页的 canvas 被释放过 → 滚回来时补画（否则是一片空白）
        if (pdfState.doc) {
          restoreCanvas(current);
          restoreCanvas(current + 1);
          restoreCanvas(current - 1);
          // 当前位置变了 → 离当前位置更远的 canvas 可以释放了
          pruneCanvases(current);
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

    // 划词存成批注 —— 跟电脑端同一套数据（本地先存，联网时同步）
    bubble.querySelectorAll("[data-pdf-anno]").forEach((b) => {
      b.onclick = () => {
        const text = bubble.dataset.text || "";
        if (!text) { hide(); return; }
        const rects = collectSelectionRects();
        hide();
        saveQuoteAsAnno(text, rects);
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
    // ⚠⚠ 缓存前先**看配额**（2026-10-27 审计 D-7）。
    //   76MB 原文塞进 IndexedDB，撞上 QuotaExceededError 会被吞成
    //   一句「失败 24」—— 用户只知道「没成功」，不知道为什么。
    //   → 先用 navigator.storage.estimate() 估一下，不够就**明说**差多少。
    if (typeof navigator !== "undefined" && navigator.storage
        && typeof navigator.storage.estimate === "function") {
      try {
        const est = await navigator.storage.estimate();
        const quota = Number(est && est.quota) || 0;
        const usage = Number(est && est.usage) || 0;
        // 留 20% 余量（浏览器自己也要占地方）
        const need = Math.ceil(total * 1.2);
        if (quota && usage + need > quota) {
          const lack = Math.ceil((usage + need - quota) / 1048576);
          toast("平板可用空间不够：这一批原文约 " + Math.round(total / 1048576)
            + " MB，但只剩 " + Math.max(0, Math.round((quota - usage) / 1048576))
            + " MB（还差约 " + lack + " MB）。先在系统设置里清点空间再试。", true);
          return;
        }
      } catch (e) {
        console.warn("读不到存储配额，继续缓存", e);
      }
    }
    let got = 0, ok = 0, fail = 0;
    let quotaFull = false;
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
        // ⚠ 配额用满是**明确的一种失败**，不能跟「下载失败」混为一谈：
        //   再试多少次都一样，得告诉用户「空间不够」。
        if (e && (e.name === "QuotaExceededError"
            || /quota/i.test(String((e && e.message) || "")))) {
          quotaFull = true;
          fail++;
          pdfStatus("存储空间已满，停止缓存");
          break;
        }
        fail++;
      }
      if (i % 2 === 0 || i === todo.length - 1) {
        pdfStatus(`缓存中 ${i + 1}/${todo.length}（${Math.round(got / 1048576)} MB）`);
      }
    }
    pdfStatus("");
    const secs = Math.max(1, Math.round((Date.now() - t0) / 1000));
    if (quotaFull) {
      toast("缓存中断：平板存储空间已满（已缓存 " + ok + " 篇 / "
        + Math.round(got / 1048576) + " MB）。先在系统设置里清点空间，"
        + "剩下的下次再缓存。", true);
      await openPdfLib();
      return;
    }
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
    // ⚠ 用 currentBackend() 判断，**不能只看 state.dirHandle** ——
    //   APK 走的是坚果云 WebDAV，dirHandle 永远是 null，
    //   照旧判断的话会一直显示「⚠ 还没连接文件夹」——
    //   哪怕坚果云连得好好的（2026-10-25 用户真机截图就是这个）。
    const backendNow = currentBackend();
    note.textContent = backendNow
      ? `共 ${ids.length} 篇原文。PDF 直接从已连接的云盘目录读取。`
      : (nativeBridge
        ? `共 ${ids.length} 篇原文。⚠ 还没连上坚果云 —— 点开时会提示你先「配置坚果云」。`
        : `共 ${ids.length} 篇原文。⚠ 还没连接文件夹 —— 点开会提示你先连接云盘里的 vault 目录。` +
          `（在安卓平板上就是坚果云同步下来的「我的坚果云\\MicroBench工作台」那个文件夹）`);

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

  /**
   * 把 AI 网关地址登记到**原生侧的主机白名单**。
   *
   * ⚠ 为什么要登记（2026-10-27 审计 C-1 的接线）：
   *   原生的 `httpPost` 有目标主机白名单 —— 只放行
   *   dav.jianguoyun.com / api.minimaxi.com / api.minimax.io /
   *   apihub.agnes-ai.com / api.deepseek.com / api.openai.com
   *   （否则平板就是个开放代理：页面里任何脚本都能让它代发请求，
   *    甚至拿它当跳板去打用户内网的 https）。
   *   而 **AI 网关地址存在网页的 IndexedDB 里（state.ai.base），原生读不到**
   *   → 用户配的**自定义网关**必须在这里登记一次，
   *     否则请求会被原生拒掉（报错：网关域名不在白名单）。
   *
   * ⚠ 只传 host 相关的**地址**，**绝不能**把 API key 一起传过去。
   * ⚠ 原生侧没有这个方法时（老 APK / 浏览器路径）**不能抛异常** ——
   *   保存设置照常成功。
   */
  function allowAiHost(base) {
    if (!nativeBridge || typeof nativeBridge.allowAiHost !== "function") return false;
    try {
      nativeBridge.allowAiHost(String(base || ""));
      return true;
    } catch (e) {
      console.warn("登记 AI 网关白名单失败", e);
      return false;
    }
  }

  async function saveSettings() {
    state.ai.base = $("cfg-base").value.trim();
    state.ai.key = $("cfg-key").value.trim();
    state.ai.model = $("cfg-model").value.trim() || "MiniMax-M3.1-Flash";
    state.ai.modelT = $("cfg-model-t").value.trim() || "MiniMax-M3";
    // ⚠ 登记给原生白名单（自定义网关必须登记，内置网关在默认名单里）
    allowAiHost(state.ai.base);
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
    // ⚠ 恢复设置后**也要登记一次**网关白名单 ——
    //   这样升级 APK 之后不用重新保存设置就能直接用自定义网关。
    allowAiHost(state.ai.base);

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
    // 布局三选一：原文+译文 / 仅原文 / 仅译文
    // ⚠ 平板屏幕小，双栏并排看很挤 —— 必须能切成单栏。
    const layBtn = $("pdf-layout");
    if (layBtn) {
      const LAYOUTS = [["both", "原文+译文"], ["orig", "仅原文"], ["trans", "仅译文"]];
      const syncLabel = () => {
        const cur = LAYOUTS.find((x) => x[0] === (pdfState.layout || "both")) || LAYOUTS[0];
        layBtn.textContent = cur[1];
        layBtn.classList.toggle("is-active", cur[0] !== "orig");
      };
      layBtn.onclick = () => {
        const i = LAYOUTS.findIndex((x) => x[0] === (pdfState.layout || "both"));
        pdfState.layout = LAYOUTS[(i + 1) % LAYOUTS.length][0];
        syncLabel();
        redrawPdf();
      };
      syncLabel();
    }
    // 手写开关
    $("pdf-toggle-ink").onclick = () => { inkToggle(!inkState.on); };
    // 保存手写
    $("pdf-save-ink").onclick = () => { saveInk(); };
    // 带上手写问 AI（多模态：把手写标注转成图片发给模型）
    $("btn-ai-ink").onclick = () => { askAiWithInk(); };
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
      // ⚠ 三个 store 一起清（2026-10-27 审计 D-4）：
      //   notes（笔记）+ meta（**API Key**、批注、译文缓存）+ pdfcache（**76MB 原文**）。
      //   以前只清 notes → Key 和 PDF 全留在机器上。
      await DB.resetAll();
      try { localStorage.clear(); } catch (e) { /* 没有 localStorage 也不该白屏 */ }
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

  // ⚠ 不能只挂 DOMContentLoaded（AGENTS.md §4.1 的同型坑）：
  //   平板端是**离线单页**，脚本在 </body> 前同步加载 → 正常路径下
  //   DOMContentLoaded 还没触发，监听器是有效的。
  //   但**不要**改成裸监听就完事 —— 一旦有人把脚本挪到 <head>、
  //   或改成 defer/async 动态注入，readyState 就会已经是 "interactive"，
  //   监听器再注册就是**死的** → boot() 一次都没跑 → 平板停在
  //   「壳子渲染出来了但空空如也」，且没有任何报错（正是用户看到的白屏）。
  //   → 判 readyState，已就绪就直接跑。
  function _bootSafely() {
    // boot() 是 async，任何一个 await 抛错都会变成一个没人看的 rejected
    // promise —— 页面就停在"壳子渲染出来了但空空如也"的状态，完全看不出
    // 出了什么事。这里把错误直接摊到页面上。
    boot().catch((err) => {
      console.error("启动失败", err);
      const doc = document.getElementById("doc");
      if (doc) {
        doc.innerHTML =
          '<div class="tb-empty" style="color:var(--bad)">启动失败：' +
          esc(safeErr(err && err.message ? err.message : String(err))) +
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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", _bootSafely);
  } else {
    _bootSafely();
  }

  // 暴露给测试：渲染器和 hash 是这个应用最容易出错的两块，必须能脱离
  // 浏览器单测。挂在 window 上，页面里没人用它。
  if (typeof window !== "undefined") {
    window.__MB_TEST__ = {
      renderMarkdown: renderMarkdown,
      extractMath: extractMath,
      hash: hash,
      firstHeading: firstHeading,
      stripFrontmatter: stripFrontmatter,
      // 消毒 / 脱敏 —— 必须能脱离浏览器单测（XSS 护栏不能只靠肉眼）
      sanitizeHtml: sanitizeHtml,
      safeHref: safeHref,
      safeErr: safeErr,
      friendlyHttpError: friendlyHttpError,
      esc: esc,
      // 按模型限流（平板直连，得自己卡）
      rpmFor: rpmFor,
      throttleModel: throttleModel,
      resetThrottle: function () { _lastReqAt.clear(); },
      // 同步链路 —— 「路径不对却显示已连接」这类故障只能靠单测钉住
      scanRemote: scanRemote,
      pullFromDir: pullFromDir,
      currentBackend: currentBackend,
      refreshSyncUi: refreshSyncUi,
      davVaultRootLabel: davVaultRootLabel,
      // 原生桥 —— 「回包丢了 status 导致 429 换模型不生效」只能靠单测钉住
      nativeCall: nativeCall,
      fetchWithTimeout: fetchWithTimeout,
      // 设置 —— 「自定义网关没登记到原生白名单」只能靠单测钉住
      saveSettings: saveSettings,
      allowAiHost: allowAiHost,
      // AI 面板（输入栏清空、多轮上下文这些行为必须有护栏）
      runAI: runAI,
      aiOnKeyDown: aiOnKeyDown,
      aiHistory: aiHistory,
      aiAppendTurn: aiAppendTurn,
      aiAppendAnswer: aiAppendAnswer,
      aiShowContext: aiShowContext,
      parseSse: parseSse,
      // 思考预算 / 思考强度判定（抄电脑端 llm.py，判错的后果是 HTTP 400）
      modelForcesThinking: modelForcesThinking,
      acceptsReasoningEffort: acceptsReasoningEffort,
      reasoningEffortFor: reasoningEffortFor,
      shouldSendThinkingDisabled: shouldSendThinkingDisabled,
      AI_QA_MAX_TOKENS: AI_QA_MAX_TOKENS,
      AI_TRANSLATE_MAX_TOKENS: AI_TRANSLATE_MAX_TOKENS,
      AI_MAX_ESCALATION_MAX_TOKENS: AI_MAX_ESCALATION_MAX_TOKENS,
      AI_QA_TIMEOUT_MS: AI_QA_TIMEOUT_MS,
      AI_REASONING_EFFORT: AI_REASONING_EFFORT,
      // 种子装载（本地优先：不能拿导出快照覆盖本地正文）
      loadSeed: loadSeed,
      openDB: openDB,
      // paper_id 契约（必须与 app.py:_paper_id_for 逐字一致）
      paperIdFor: paperIdFor,
      safePaperKey: safePaperKey,
      syncUrl: syncUrl,
      // 本地优先的批注存取
      DB: DB,
      saveAnnotations: saveAnnotations,
      loadAnnotations: loadAnnotations,
      mergeAnnos: mergeAnnos,
      annoTombstones: annoTombstones,
      inkAnnoId: inkAnnoId,
      // 目录树的增量重建（性能护栏）
      buildNav: buildNav,
      // 译文快照的版本闸门（两端必须一致，否则用户看到旧算法翻的坏译文）
      loadTranslation: loadTranslation,
      transCacheKey: transCacheKey,
      state: state,
    };
  }
})();
