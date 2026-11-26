// PDF 画布按需渲染与远端显存回收
    // =====================================================================
    let _pdfCanvasObserver = null;
    // 视口外 1400px 内仍保留位图（滚动手感：提前画好，不用等）
    const _PDF_CANVAS_MARGIN_PX = 1400;

    function _pdfPageNearViewport(wrapper) {
      if (!wrapper || !wrapper.getBoundingClientRect) return true;
      const r = wrapper.getBoundingClientRect();
      const vh = window.innerHeight || 1200;
      const vw = window.innerWidth || 1600;
      return (r.bottom > -_PDF_CANVAS_MARGIN_PX
              && r.top < vh + _PDF_CANVAS_MARGIN_PX
              && r.right > -200 && r.left < vw + 200);
    }

    function _pdfObserveCanvas(wrapper) {
      if (!wrapper) return;
      if (typeof IntersectionObserver === "undefined") {
        // 没有 IO 的环境 → 退回「一律画出来」（护栏环境常见）
        if (!wrapper._pdfPainted) _pdfPaintCanvas(wrapper);
        return;
      }
      if (!_pdfCanvasObserver) {
        _pdfCanvasObserver = new IntersectionObserver((entries) => {
          for (const en of entries) {
            const w = en.target;
            if (en.isIntersecting) {
              if (w._pdfRender && !w._pdfPainted) _pdfPaintCanvas(w);
            } else {
              _pdfReleaseCanvas(w);
            }
          }
        }, { root: null, rootMargin: _PDF_CANVAS_MARGIN_PX + "px 0px" });
      }
      _pdfCanvasObserver.observe(wrapper);
    }

    async function _pdfPaintCanvas(wrapper) {
      const st = wrapper && wrapper._pdfRender;
      if (!st) return;
      if (st.painting) { st.paintAgain = true; return; }
      st.painting = true;
      try {
        const c = st.canvas;
        if (!c.width || !c.height) {
          // 位图被释放过 → 先把尺寸还回去（否则 PDF.js 画出来是 0×0）
          c.width = Math.floor(st.renderViewport.width);
          c.height = Math.floor(st.renderViewport.height);
        }
        const ctx = c.getContext("2d");
        const task = st.pdfPage.render({
          canvasContext: ctx, viewport: st.renderViewport,
        });
        st.renderTask = task;
        try { await task.promise; }
        catch (e) {
          // 取消/被打断 → 半张图。稍后再试一次。
          wrapper._pdfPainted = false;
          setTimeout(() => { if (wrapper.isConnected) _pdfPaintCanvas(wrapper); }, 150);
          return;
        }
        st.renderTask = null;
        wrapper._pdfPainted = true;
      } finally {
        st.painting = false;
        if (st.paintAgain) { st.paintAgain = false; _pdfPaintCanvas(wrapper); }
      }
    }

    function _pdfReleaseCanvas(wrapper) {
      const st = wrapper && wrapper._pdfRender;
      if (!st || !wrapper._pdfPainted) return;
      wrapper._pdfPainted = false;
      try {
        if (st.renderTask && typeof st.renderTask.cancel === "function") {
          st.renderTask.cancel();
        }
      } catch (e) { /* 忽略 */ }
      st.renderTask = null;
      // 归零位图 → 浏览器立刻回收这一大块显存
      try { st.canvas.width = 0; st.canvas.height = 0; } catch (e) { /* 忽略 */ }
    }

    /** 关阅读器 / 缩放重绘前：掐掉所有在飞的渲染任务 + 断开观察器。 */
    function _pdfCancelAllCanvasRenders() {
      if (_pdfCanvasObserver) {
        try { _pdfCanvasObserver.disconnect(); } catch (e) { /* 忽略 */ }
        _pdfCanvasObserver = null;
      }
      const listEl = document.getElementById("pdf-pages-list");
      if (!listEl) return;
      listEl.querySelectorAll(".pdf-page-block").forEach((w) => {
        const st = w._pdfRender;
        if (st && st.renderTask && typeof st.renderTask.cancel === "function") {
          try { st.renderTask.cancel(); } catch (e) { /* 忽略 */ }
          st.renderTask = null;
        }
      });
    }


export {
  _PDF_CANVAS_MARGIN_PX, _pdfPageNearViewport, _pdfObserveCanvas, _pdfPaintCanvas, _pdfReleaseCanvas, _pdfCancelAllCanvasRenders,
};
