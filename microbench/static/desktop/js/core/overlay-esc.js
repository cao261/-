// 统一浮层 Esc 栈与遮罩关闭处理（后进先出，防止误关整个阅读器）
      // ---------------------------------------------------------------
      // Shared modal dismissal: Esc key + click-outside (overlay backdrop).
      // Returns a wrapped close fn that also cleans up its listeners.
      // Use for any overlay-based modal: showKeywordPickerModal,
      // showVaultSaveModal, and any future inline pickers.
      // ---------------------------------------------------------------
      function _wireModalDismissals(overlay, closeFn) {
        // ⚠ 核心不变量：**任何**关闭路径（Esc / 点遮罩 / 点按钮）都必须摘掉
        //   挂在 document 上的 keydown。原来 Esc 分支直接调 closeFn，绕过了
        //   removeEventListener → 每按一次 Esc 关窗就永久留下一个 document
        //   监听；弹窗开得越多越慢，而且旧监听还会去关已经关掉的弹窗。
        //   同时加 closed 幂等：Esc / 遮罩 / 按钮 三个动作只生效第一个。
        //
        //   上一次没关闭就又调进来（按钮连点两次打开同一个 overlay）时，
        //   先摘掉上一套，避免叠加。
        if (typeof overlay._dismissCleanup === "function") {
          overlay._dismissCleanup();
        }

        let closed = false;
        // ⚠ 2026-11-25 审计 P1：Esc 改走**统一栈**（见 _escRegister）。
        //   原来这里自己往 document 上挂 keydown —— 而阅读器自己的 Esc
        //   处理器也在 document 上 → 同一节点上的两个监听器，
        //   stopPropagation 拦不住彼此 → 一次 Esc 既关弹窗又关掉整个阅读器。
        const unregisterEsc = _escRegister(() => fire(null));
        const onClick = (e) => {
          // Only the backdrop itself, not children (inner panel)
          if (e.target === overlay) fire(null);
        };
        const cleanup = () => {
          unregisterEsc();
          overlay.removeEventListener("click", onClick);
          overlay._dismissCleanup = null;
        };
        const fire = (r) => {
          if (closed) return;
          closed = true;
          cleanup();
          closeFn(r);
        };

        overlay.addEventListener("click", onClick);
        overlay._dismissCleanup = cleanup;
        return fire;
      }

      // ---------------------------------------------------------------
      // Modal Esc dismissal (no click-outside).
      //
      // ⚠ 旧写法 `document.addEventListener("keydown", onEsc, { once: true })`
      //   有三个坑（2026-10-27 审计 HIGH-2，全部实测复现）：
      //   ① { once: true } 的语义是「**任意**键按一次就摘掉监听器」，
      //      不是「Esc 才摘」→ 弹窗开着时先按了字母键，Esc 就再也关不掉它；
      //   ② 诊断弹窗的监听器只在 `if (!modal)` 里挂一次 →
      //      关掉再打开后 Esc **永久失效**；
      //   ③ 快照弹窗每打开一次就多挂一个 keydown 监听器（实测 3 次 → 3 个）。
      //
      // 这里统一：只认 Esc、关掉时摘干净、并且 stopPropagation
      // —— 否则专注模式的 Esc 会和弹窗的 Esc 一起响应 → 两边都不响应
      // （zen 的让路逻辑 `if (_zenOverlayOpen()) return;` 会把 Esc 让给弹窗，
      //   而弹窗自己的 Esc 已经被别的键吃掉了 → 键盘陷阱）。
      //
      // ⚠⚠ 2026-11-25 审计 P1：**stopPropagation 根本拦不住同行监听器**。
      //   阅读器自己的 Esc 处理器（`document.addEventListener`）与弹窗的
      //   处理器是**同一个节点上的两个监听器** → stopPropagation 只阻止
      //   「继续往父节点传」，**不阻止同一节点上的其它监听器**。
      //   → 结果：按一次 Esc，弹窗关了，**整个阅读器也一起关了**（内容全丢）。
      //   而且 `#sync-modal` / `#anno-editor` / `#anno-drawer` /
      //   `#vault-save-modal` / `#keyword-picker-modal` 压根**没登记**到任何
      //   Esc 机制 → 这些浮层开着时按 Esc = 直接关掉阅读器。
      //
      // → 改成一个**统一的 Esc 栈**（后进先出）：
      //     谁打开就登记谁，Esc 永远只关**最上面**那一个；
      //     栈空时才轮到阅读器自己响应。
      // ---------------------------------------------------------------
      const _escStack = [];

      /** 把一个「关闭函数」登记进 Esc 栈；返回注销句柄。 */
      function _escRegister(closeFn) {
        if (typeof closeFn !== "function") return function () {};
        _escStack.push(closeFn);
        return function () {
          const i = _escStack.lastIndexOf(closeFn);
          if (i >= 0) _escStack.splice(i, 1);
        };
      }

      /** Esc 栈里还有没有浮层（阅读器的 Esc 要给它让路）。 */
      function _escOverlayOpen() { return _escStack.length > 0; }

      /**
       * 把某个浮层元素登记进 Esc 栈（同一元素重复打开时先注销旧的）。
       * 之前只有「快照弹窗 / 诊断弹窗」登记过，
       * `#sync-modal` / `#anno-editor` / `#anno-drawer` 这三个
       * 开着的时候按 Esc = **整个阅读器被关掉**（内容全丢）。
       */
      function _escBind(el, closeFn) {
        if (!el) return;
        if (el._escUnregister) { el._escUnregister(); el._escUnregister = null; }
        el._escUnregister = _escRegister(closeFn);
      }
      function _escUnbind(el) {
        if (el && el._escUnregister) {
          el._escUnregister();
          el._escUnregister = null;
        }
      }

      // 单一的 Esc 入口。**注册时机最早**（脚本执行时就挂上），
      // 所以它排在阅读器/专注模式的处理器**前面** →
      // 栈非空时它处理完并 stopImmediatePropagation，同节点上的其它
      // 处理器一律不再响应 → 一次按键只干一件事。
      //
      // ⚠ 出栈由**这里**做（pop），不依赖关闭函数自己注销 ——
      //   关闭函数忘了注销时，栈会卡住一个已经关掉的浮层，
      //   之后每次 Esc 都在关那个「早就不在了」的浮层 → Esc 彻底失灵。
      //   （关闭函数里调 _escUnbind 是幂等的，两边都做也没关系。）
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (!_escStack.length) return;   // 没有浮层 → 交给阅读器自己处理
        const t = e.target;
        const tag = (t && t.tagName || "").toLowerCase();
        // 输入框里的 Esc 不抢（用户在编辑批注正文，想取消编辑而已）
        if (tag === "input" || tag === "textarea" || tag === "select"
            || (t && t.isContentEditable)) return;
        const closeFn = _escStack.pop();   // 先出栈，再关
        if (!closeFn) return;
        e.stopPropagation();
        e.stopImmediatePropagation();
        try { closeFn(); }
        catch (err) { console.warn("[esc] 关闭浮层失败", err); }
      });

      function _wireModalEsc(overlay, closeFn) {
        if (!overlay) return;
        if (overlay._escDismiss) overlay._escDismiss();
        // ⚠ 登记进统一栈（不再自己往 document 上挂 keydown）
        const unregister = _escRegister(() => {
          try { closeFn(); } catch (e) { console.warn("[esc] 关闭弹窗失败", e); }
        });
        overlay._escDismiss = () => {
          unregister();
        };
      }


export {
  _escStack, _escRegister, _escOverlayOpen, _escBind, _escUnbind, _wireModalEsc, _wireModalDismissals,
};
