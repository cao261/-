import { _readerState } from "./store.js";
import { _pdfState, _readerMode, _pdfZoom, getPdfState, getReaderMode, getPdfZoom, renderReader } from "../reader/pdf-reader.js";

    // ===========================================================================
    // 阅读进度续读 —— 「像看小说一样」：这次看到这里，关掉应用，下次打开还在这里
    //
    // 用户 2026-11-20 原话：
    //   「我这论文都很长嘛，我不想每次打开都要重新翻好久翻到我目前看的部分，
    //     需要你能做到像看小说一样，这次看到这里了，关掉应用，下次打开也还是这里」
    //
    // 【为什么进度存在 localStorage，而不是后端 progress 通道】
    //   ① 桌面应用（desktop.py）每次启动的 URL 都带随机 cache-bust（?v=<hex>），
    //      **hash 不会被保留** → 靠 URL/hash 记进度，活不过「关掉应用」这一关。
    //   ② 后端 progress 通道会把进度写进**坚果云共享目录**，而另一台设备
    //      （25353）也在用那个目录 → 本机自动续读不该悄悄写进去。
    //      （本轮用户已拍板：只要本机，不碰坚果云。）
    //   ③ desktop.py 显式 private_mode=False + storage_path → localStorage
    //      跨次启动保留；浏览器 --app 兜底模式同理。
    //
    // 【跨设备那条路没删】
    //   菜单里的「推送 · 阅读进度 / 拉取 · 阅读进度」仍然在，
    //   只是顺手把它的**采集**修好了：原来只认右栏译文栏，
    //   用户在看左栏 PDF 原文时推出去的 page 恒为 0（等于什么也没推）。
    //
    // 【记什么】
    //   页码 + **页内位置**（当前页顶相对滚动容器顶的带符号偏移 + 一个比例）。
    //   只记页码不够 —— 长论文里一页可能有半屏字，差 200px 就会让人
    //   觉得「没跳对」，那就不是「像看小说」了。
    // ===========================================================================
    const LS_READPOS_KEY = "microbench_readpos_v1";
    const _READPOS_MAX_PAPERS = 200;

    // 两栏的 DOM 结构不同、算法完全一样 → 抽成一张表（护栏也按这张表切片）
    const _READPOS_PANES = {
      pdf: {
        key: "pdf",
        scrollerId: "pdf-viewer-container",
        listId: "pdf-pages-list",
        blockSel: ".pdf-page-block",
        idPrefix: "pdf-page-block-",
      },
      trans: {
        key: "trans",
        scrollerId: "pdf-translations-container",
        listId: "pdf-translations-list",
        blockSel: ".pdf-translation-block",
        idPrefix: "pdf-trans-block-",
      },
    };

    let _readPosSaveTimer = null;
    let _readPosTrackingBound = false;
    let _readPosRestored = false;
    let _readPosCurrent = null;                 // 当前论文的续读目标（记录本体）
    let _readPosUserMoved = { pdf: false, trans: false };
    // 我们自己滚到位之后的 scrollTop —— 用来把「程序性滚动」和「用户滚动」分开
    const _readPosExpected = { pdf: null, trans: null };

    function _readPosStore() {
      try {
        const raw = localStorage.getItem(LS_READPOS_KEY);
        if (!raw) return {};
        const o = JSON.parse(raw);
        return (o && typeof o === "object" && !Array.isArray(o)) ? o : {};
      } catch (e) { return {}; }
    }

    function _writeReadPosStore(store) {
      try {
        const keys = Object.keys(store);
        if (keys.length > _READPOS_MAX_PAPERS) {
          // 只留最近读的这些（按更新时间倒序砍掉最旧的）
          keys.sort((a, b) => String(store[b].updated_at || "").localeCompare(String(store[a].updated_at || "")));
          keys.slice(_READPOS_MAX_PAPERS).forEach(function (k) { delete store[k]; });
        }
        localStorage.setItem(LS_READPOS_KEY, JSON.stringify(store));
      } catch (e) {
        // 配额满了就放弃记进度 —— 绝不能因为记进度把阅读器搞崩
        console.warn("[readpos] 写入失败", e);
      }
    }

    /** 续读主键：优先 vault 相对路径（章节视图 / PDF 视图都认），没有才用 paperId。 */
    function _readPosKeyForPath(relPath) {
      const rp = String(relPath || "").replace(/\\/g, "/");
      if (rp) return "path:" + rp;
      const pid = (_pdfState && _pdfState.paperId) || "";
      return pid ? "pid:" + pid : "";
    }

    /** 只认路径的主键（没有路径就返回空 —— 不许退化到 `_pdfState.paperId`，
     *  那是**上一篇**的残留，会把这一篇的进度写到别人头上）。 */
    function _readPosPathKey(relPath) {
      const rp = String(relPath || "").replace(/\\/g, "/");
      return rp ? "path:" + rp : "";
    }

    function _readPosKey() {
      if (_readerMode === "pdf") {
        return _readPosKeyForPath((_pdfState && _pdfState.relativePath) || "");
      }
      // 章节视图：只有 vault 相对路径才认
      return _readPosPathKey((_readerState && _readerState.pdfPath) || "");
    }

    function _getReadPos(key) {
      if (!key) return null;
      const rec = _readPosStore()[key];
      return (rec && typeof rec === "object") ? rec : null;
    }

    /**
     * 取一栏的「当前页 + 页内位置 + scrollTop」。
     *
     * delta = 当前页顶相对滚动容器顶的**带符号**偏移（负数 = 已经滚进这一页里）。
     *   · 之所以要带符号：容器有 padding（.pdf-split-pane 有上下内边距），
     *     停在第 1 页页首时 delta 是 +padding（页顶在容器顶下面），
     *     滚进页里才是负数。**只存「滚进了多少」会把 padding 那部分丢掉**，
     *     恢复后整页会差一个 padding 的高度。
     * 判「当前页」：块顶相对容器顶 <= 0 的**最后一个**块 —— 页块自上而下排，
     * 越靠后的块 delta 越负，所以「最后一个 delta<=0」就是视口顶部所在的那一页。
     */
    function _readPosAnchor(pane) {
      const scroller = document.getElementById(pane.scrollerId);
      const list = document.getElementById(pane.listId);
      if (!scroller || !list) return null;
      if (!scroller.clientHeight) return null;      // 这一栏当前不可见（display:none）
      const blocks = list.querySelectorAll(pane.blockSel);
      if (!blocks.length) return null;
      const sTop = scroller.getBoundingClientRect().top;
      let page = 0, delta = 0, pageH = 0;
      for (const b of blocks) {
        const r = b.getBoundingClientRect();
        const d = r.top - sTop;
        const m = b.id.match(/(\d+)$/);
        const num = m ? parseInt(m[1], 10) : 0;
        if (d > 0) {
          // 还没滚到这一页 —— 如果这是第一页，它就是当前页
          if (!page) { page = num; delta = d; pageH = r.height; }
          break;
        }
        page = num;
        delta = d;
        pageH = r.height;
      }
      if (!page) return null;
      const offset = Math.max(0, -delta);
      return {
        page: page,
        delta: Math.round(delta),
        offset: Math.round(offset),
        // 页内偏移的**比例**（0~1）：缩放变了以后 px 偏移指的不是同一个位置，
        // 比例才是「这一页读到了百分之几」—— 跨缩放回位靠它。
        ratio: pageH > 0 ? Math.round((offset / pageH) * 1000) / 1000 : 0,
        scroll_top: Math.round(scroller.scrollTop || 0),
      };
    }

    /** 采一次进度并落盘（滚动时防抖调用）。 */
    function _captureReadPos() {
      const key = _readPosKey();
      if (!key) return null;
      const inPdf = _readerMode === "pdf";
      let left = inPdf ? _readPosAnchor(_READPOS_PANES.pdf) : null;
      let right = inPdf ? _readPosAnchor(_READPOS_PANES.trans) : null;
      const store = _readPosStore();
      const prev = (store[key] && typeof store[key] === "object") ? store[key] : null;
      // ⚠⚠ 栏不可见时 anchor 恒为 null（阅读器已关 / 还没打开过）。
      //   `beforeunload` / `pagehide` 是在**关应用**时触发的，
      //   而用户最常见的顺序是「先关阅读器 → 再关应用」→ 那一刻两栏都
      //   display:none → clientHeight 为 0 → anchor 恒 null。
      //   原来整条 `store[key] = rec` → **把刚存好的位置覆盖成 null**
      //   → 下次打开既不回位、也不弹提示（静默降级，§4.96 同型复发）。
      //   → 新的采不到就**沿用旧的**，绝不许把好数据抹掉。
      if (!left && prev && prev.left) left = prev.left;
      if (!right && prev && prev.right) right = prev.right;
      const primary = left || right;
      const rec = {
        paper_id: (_pdfState && _pdfState.paperId) || "",
        relative_path: (_pdfState && _pdfState.relativePath) || (_readerState && _readerState.pdfPath) || "",
        title: (_pdfState && _pdfState.paperTitle) || (_readerState && _readerState.title) || "",
        zoom: _pdfZoom,
        mode: _readerMode || "pdf",
        section: (_readerState && _readerState.activeSection) || 0,
        left: left,
        right: right,
        // 给跨设备那条路留的顶层字段（后端只保留这几个核心字段）
        page: primary ? primary.page : 0,
        scroll_top: primary ? primary.scroll_top : 0,
        updated_at: new Date().toISOString(),
      };
      store[key] = rec;
      _writeReadPosStore(store);
      return rec;
    }

    function _scheduleSaveReadPos() {
      if (_readPosSaveTimer) return;
      _readPosSaveTimer = setTimeout(function () {
        _readPosSaveTimer = null;
        try { _captureReadPos(); } catch (e) { console.warn("[readpos] 保存失败", e); }
      }, 400);
    }

    /**
     * 把一栏滚回「某页 + 页内位置」。返回 true = 精确落位（没被内容高度夹住）。
     *
     * 公式：设 delta = 页顶相对容器顶的偏移（负数 = 已滚进页里），
     *   目标 scrollTop = 当前 scrollTop + (页顶相对容器顶的偏移) - delta
     * —— 前半句把页顶挪到容器顶，后半句再把它挪回页内原处。
     */
    function _restoreReadPosAnchor(pane, page, delta, ratio, recZoom) {
      const scroller = document.getElementById(pane.scrollerId);
      if (!scroller || !page) return false;
      if (!scroller.clientHeight) return false;
      const block = document.getElementById(pane.idPrefix + page);
      if (!block) return false;
      const sTop = scroller.getBoundingClientRect().top;
      const bRect = block.getBoundingClientRect();
      let signed = delta || 0;
      // 缩放跟记录时不一样 → px 偏移指的不是同一个位置 → 改用比例换算
      if (signed <= 0 && ratio != null && recZoom && Math.abs((_pdfZoom || 1) - recZoom) > 0.01) {
        const bh = bRect.height;
        if (bh > 0) signed = -ratio * bh;
      }
      const target = scroller.scrollTop + (bRect.top - sTop) - signed;
      const max = scroller.scrollHeight - scroller.clientHeight;
      if (max <= 0) return false;              // 内容还没渲染够 → 现在根本滚不动
      scroller.scrollTop = Math.max(0, Math.min(target, max));
      const landed = scroller.scrollTop;
      // 标记「这次滚动是我们自己干的」→ scroll 处理器要认出来，不能当成用户滚动
      _readPosExpected[pane.key] = landed;
      return Math.abs(landed - target) <= 2;
    }

    /** 把两栏都放回记录的位置。 */
    function _applyReadPos(rec) {
      if (!rec) return { leftOk: false, rightOk: false, page: 0 };
      let leftOk = false, rightOk = false;
      if (rec.left) {
        leftOk = _restoreReadPosAnchor(_READPOS_PANES.pdf, rec.left.page, rec.left.delta,
                                      rec.left.ratio, rec.zoom);
      }
      if (rec.right) {
        rightOk = _restoreReadPosAnchor(_READPOS_PANES.trans, rec.right.page, rec.right.delta,
                                       rec.right.ratio, rec.zoom);
      }
      const page = (rec.left && rec.left.page) || (rec.right && rec.right.page) || 0;
      return { leftOk: leftOk, rightOk: rightOk, page: page };
    }

    /**
     * 这次重绘 / 重排要回到哪儿？
     *
     *   ① 有活锚点（同一篇的重绘，且 DOM 还在）→ 用它（最新、最准）
     *   ② 没有 → 回落到本机记住的位置（localStorage）
     *
     * ⚠ 单独抽成一个函数，是为了**能被护栏直接跑**（探针里真调它）——
     *   免得这条「回位目标怎么算」只能靠「读源码文本」来守（同型复发）。
     */
    function _readPosTargetFor(liveLeft, liveRight, relPath) {
      if (liveLeft) {
        return { left: liveLeft, right: liveRight, zoom: _pdfZoom };
      }
      return _getReadPos(_readPosKeyForPath(relPath));
    }

    /**
     * 译文回填 / 快照恢复之后，上方高度会变 → 目标页会往下漂 →
     * 重新贴回去（用户自己滚过就不动，别跟用户抢滚动条）。
     */
    function _readPosReapply() {
      if (!_readPosRestored) return;
      const rec = _readPosCurrent;
      if (!rec) return;
      if (rec.left && !_readPosUserMoved.pdf) {
        _restoreReadPosAnchor(_READPOS_PANES.pdf, rec.left.page, rec.left.delta,
                              rec.left.ratio, rec.zoom);
      }
      if (rec.right && !_readPosUserMoved.trans) {
        _restoreReadPosAnchor(_READPOS_PANES.trans, rec.right.page, rec.right.delta,
                              rec.right.ratio, rec.zoom);
      }
    }

    function _initReadPosTracking() {
      if (_readPosTrackingBound) return;
      _readPosTrackingBound = true;
      Object.keys(_READPOS_PANES).forEach(function (k) {
        const pane = _READPOS_PANES[k];
        const el = document.getElementById(pane.scrollerId);
        if (!el) return;
        el.addEventListener("scroll", function () {
          const scroller = document.getElementById(pane.scrollerId);
          if (!scroller) return;
          const exp = _readPosExpected[pane.key];
          if (exp !== null && Math.abs(scroller.scrollTop - exp) <= 2) {
            _readPosExpected[pane.key] = null;    // 这是我们自己滚的
            return;
          }
          _readPosExpected[pane.key] = null;
          _readPosUserMoved[pane.key] = true;
          _scheduleSaveReadPos();
        }, { passive: true });
      });
      // 关窗 / 退出应用前也存一次（用户可能直接点 X 关掉）
      window.addEventListener("beforeunload", function () {
        try { _captureReadPos(); } catch (e) {}
      });
      window.addEventListener("pagehide", function () {
        try { _captureReadPos(); } catch (e) {}
      });
    }

    /** 章节视图：回到上次打开的那一节。 */
    function _restoreSectionReadPos() {
      if (_readerMode === "pdf") return;
      if (!_readerState || !Array.isArray(_readerState.sections) || !_readerState.sections.length) return;
      const rec = _getReadPos(_readPosPathKey(_readerState.pdfPath || ""));
      if (!rec) return;
      // ⚠ 只认「上次确实是在章节视图读的」那条记录 ——
      //   PDF 模式下记下的 section 只是残留值，拿它回位会让人莫名其妙。
      if (rec.mode && rec.mode !== "section") return;
      const idx = rec.section || 0;
      if (idx > 0 && idx < _readerState.sections.length && idx !== _readerState.activeSection) {
        _readerState.activeSection = idx;
        if (typeof renderReader === "function") renderReader();
      }
    }


function beginReadPosSession(target) {
  _readPosCurrent = target;
  _readPosRestored = false;
  _readPosUserMoved = { pdf: false, trans: false };
}
function markReadPosRestored(val) { _readPosRestored = !!val; }
function getReadPosCurrent() { return _readPosCurrent; }
function isReadPosRestored() { return _readPosRestored; }
function getReadPosUserMoved() { return _readPosUserMoved; }

export {
  LS_READPOS_KEY, _READPOS_MAX_PAPERS, _READPOS_PANES,
  _readPosStore, _writeReadPosStore, _readPosKeyForPath, _readPosPathKey, _readPosKey, _getReadPos,
  _readPosAnchor, _captureReadPos, _scheduleSaveReadPos, _restoreReadPosAnchor, _applyReadPos,
  _readPosTargetFor, _readPosReapply, _initReadPosTracking, _restoreSectionReadPos,
  _readPosCurrent, _readPosRestored, _readPosUserMoved,
  beginReadPosSession, markReadPosRestored, getReadPosCurrent, isReadPosRestored, getReadPosUserMoved,
};
