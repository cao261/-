import { showToast, _readJsonSafe, _httpErrorMessage } from "../core/utils.js";

    let _readerState = {
      title: "",
      sections: [],   // [{section, title, text, page_start, page_end}]
      translations: {}, // {section_idx: {translation, model: "..."}}
      activeSection: 0,
      pdfPath: null,
      identifier: "",  // original arXiv ID / DOI / PDF URL (for hash restore)
    };


    // ----- Model Status & Probing -----
    let _llmStatus = null;
    // 降级提示只弹一次（额度恢复后允许再弹）
    let _degradeToastShown = false;

    async function fetchModelStatus() {
      try {
        const res = await fetch("/api/llm/status");
        if (!res.ok) return;
        _llmStatus = await res.json();
        updateModelBadges();
      } catch (e) {}
    }

    function updateModelBadges() {
      if (!_llmStatus) return;
      const headerDot = document.getElementById("header-model-dot");
      const headerText = document.getElementById("header-model-text");
      const readerDot = document.getElementById("reader-model-dot");
      const readerText = document.getElementById("reader-model-text");
      const readerProbeBtn = document.getElementById("reader-probe-btn");

      // Dots are driven by data-state so they pick up theme tokens; the old
      // version hardcoded hex here, which stayed light-grey-ish in night mode.
      const setDot = (el, state) => { if (el) el.setAttribute("data-state", state); };

      if (!_llmStatus.configured) {
        setDot(headerDot, "bad"); setDot(readerDot, "bad");
        if (headerText) headerText.textContent = "LLM 未配置";
        if (readerText) readerText.textContent = "LLM 未配置";
        if (readerProbeBtn) readerProbeBtn.style.display = "none";
        return;
      }

      const modelName = _llmStatus.active_model || "LLM";
      if (_llmStatus.is_degraded) {
        // ⚠️ 别把备用模型的 RPM 写进徽标 —— 那是 Agnes 的限流值（11/20），
        //    用户会读成"我被限流到 20 RPM 了"，正是这次要消除的误判。
        const kindLabel = { quota: "配额", rate_limit: "限流", no_output: "无正文" }
          [_llmStatus.degraded_kind] || "降格";
        const badgeText = `${modelName} · ${kindLabel}`;
        setDot(headerDot, "warn"); setDot(readerDot, "warn");
        if (headerText) headerText.textContent = badgeText;
        if (readerText) readerText.textContent = badgeText;
        if (readerProbeBtn) readerProbeBtn.style.display = "inline-block";
        // ⚠️ 必须说**真实原因**，不能一律说"额度用完了"：
        //    降格有三种完全不同的原因（配额窗口 / 限流 / 这次没出正文），
        //    处置方式也不同。以前写死"额度用完了"，用户一看自己额度明明还有，
        //    只会觉得程序在瞎说（真实反馈）。
        const why = _llmStatus.degraded_text
          || "主模型暂时不可用，已自动降级到免费备用模型：还能用，但慢一些（表格读图尤其慢，8 倍左右）";
        const probeHint = _llmStatus.degraded_kind === "quota"
          ? "（额度恢复后会自动切回主模型，也可以点左侧「探测升格」立刻试）"
          : "（点左侧「探测升格」可以立刻试主模型）";
        if (headerText) headerText.title = why;
        if (readerText) readerText.title = why;
        // 降级提示只弹一次（额度恢复后允许再弹）
        if (!_degradeToastShown) {
          _degradeToastShown = true;
          if (typeof showToast === "function") {
            showToast("⚠️ " + why + probeHint, true, 7000);
          }
        }
      } else {
        _degradeToastShown = false;   // 额度恢复后允许再提示一次
        const badgeText = modelName;
        setDot(headerDot, "ok"); setDot(readerDot, "ok");
        if (headerText) headerText.textContent = badgeText;
        if (readerText) readerText.textContent = badgeText;
        if (readerProbeBtn) readerProbeBtn.style.display = "none";
      }
    }

    async function probeModelNow() {
      showToast("正在探测 MiniMax 主模型额度状态...");
      try {
        const res = await fetch("/api/llm/probe", { method: "POST" });
        // 先判状态码再解析：探针失败时后端可能返回 HTML 错误页，
        // 直接 res.json() 会抛 SyntaxError → 用户看到的是语法错误，
        // 而不是"探测失败"（排查方向被带偏）。
        const { data, text } = await _readJsonSafe(res);
        if (!res.ok) {
          throw new Error(_httpErrorMessage(data, res, text, "探测失败"));
        }
        if (data.success) {
          showToast(`🚀 ${data.message}`);
        } else {
          showToast(`⚠️ ${data.message}`, true);
        }
        await fetchModelStatus();
      } catch (err) {
        showToast("探测失败: " + err.message, true);
      }
    }

    async function checkOrProbeModel() {
      if (_llmStatus && _llmStatus.is_degraded) {
        probeModelNow();
      } else {
        await fetchModelStatus();
        showToast(`当前模型: ${_llmStatus?.active_model || "未配置"} (${_llmStatus?.is_degraded ? "降格保护中" : "主模型正常"})`);
      }
    }


    // -------------------------------------------------------------------
    // 整篇问答的请求体（2026-11-19，25353 台）
    //
    // ⚠️ 原来这里内联着 `_readerState.sections.slice(0, 20)` —— 长论文
    // 20+ 节时，后半部分（**恰恰是用户正在问的那部分**）被**静默丢弃**，
    // 模型于是回答「我的上下文里没有这一节，我只能猜」。
    // 后端上限已放宽到 100，真正的上下文预算由后端按需分配
    // （见 microbench/qa_context.py 的 pick_context）。
    //
    // 抽成独立函数是为了能被**行为级**测试直接调用
    // （microbench/_test_focus_context.js 从 index.html 抽真代码跑）。
    // -------------------------------------------------------------------

    // _qaModel：划词答疑用的模型（与翻译**独立**）。
    // 存 localStorage：用户的额度和偏好不该每次开面板都要重选。
    let _qaModel = "";
    let _qaModelChoicesLoaded = false;

    const _QA_MODEL_STORAGE_KEY = "microbench.qaModel";

    function _loadQAModelPref() {
      try {
        _qaModel = localStorage.getItem(_QA_MODEL_STORAGE_KEY) || "";
      } catch (e) { _qaModel = ""; }
    }

    function _saveQAModelPref(v) {
      _qaModel = v || "";
      try {
        if (_qaModel) localStorage.setItem(_QA_MODEL_STORAGE_KEY, _qaModel);
        else localStorage.removeItem(_QA_MODEL_STORAGE_KEY);
      } catch (e) { /* 隐私模式静默降级 */ }
    }


    // ---------------------------------------------------------------------------
    // 答疑模型选择（与翻译**独立**）
    // ---------------------------------------------------------------------------
    // 用户 2026-10-26："翻译用 M3 不开思考…解答问题用 M3.1 Flash，
    // 也能换 DeepSeek V4.1 Flash…我能自主的切换，我这个额度用完，
    // 我能切换另一个额度。"
    //
    // 所以答疑面板必须能**单独**换模型，不能跟翻译共用一个下拉框。
    async function loadQAModelChoices() {
      const sel = document.getElementById("qa-model-select");
      if (!sel) return;
      try {
        const r = await fetch("/api/llm/qa_models");
        if (!r.ok) return;
        const d = await r.json();
        const cur = sel.value || _qaModel;
        sel.innerHTML = "";
        (d.models || []).forEach(m => {
          const o = document.createElement("option");
          o.value = m.id;
          o.textContent = m.label;
          o.title = m.hint || "";
          sel.appendChild(o);
        });
        // 首选用户记住的选择；没有记住的就用服务端说的默认模型
        const wanted = cur || d.active_model || "";
        sel.value = wanted;
        if (!sel.value && sel.options.length) sel.selectedIndex = 0;
        _qaModel = sel.value || "";
      } catch (e) { /* 拉不到就保持默认 */ }
    }

    function onQAModelChange(v) {
      _saveQAModelPref(v);
      _qaModel = v || "";
      const pill = document.getElementById("qa-panel-model-pill");
      // 徽标立刻反映当前选择，别让用户以为还用的是主模型
      if (pill) {
        const opt = document.querySelector('#qa-model-select option[value="' +
          (_qaModel || "").replace(/"/g, '\\"') + '"]');
        pill.textContent = opt ? opt.textContent : "🤖 主模型";
        pill.style.color = "";
      }
    }


function getQaModel() { return _qaModel; }
function isQaModelChoicesLoaded() { return _qaModelChoicesLoaded; }
function markQaModelChoicesLoaded() { _qaModelChoicesLoaded = true; }
function getLlmStatus() { return _llmStatus; }

export {
  _readerState, _llmStatus, getLlmStatus, fetchModelStatus, updateModelBadges, probeModelNow, checkOrProbeModel,
  _qaModel, _qaModelChoicesLoaded, _QA_MODEL_STORAGE_KEY, _loadQAModelPref, _saveQAModelPref, loadQAModelChoices, onQAModelChange,
  getQaModel, isQaModelChoicesLoaded, markQaModelChoicesLoaded,
};
