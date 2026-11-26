// 实验室与诊断视图：系统健康、跨设备同步诊断、后台批量任务与曲线误差对比
import { showToast, getJson, postJson, escapeHtml } from "../core/utils.js";
import { syncNow, releaseCloudPdfs, listSnapshots } from "../reader/pdf-reader.js";

async function loadLabsDiagnostics() {
  const sysBox = document.getElementById("labs-health-kv");
  const syncBox = document.getElementById("labs-sync-kv");
  if (!sysBox && !syncBox) return;

  try {
    const [health, llmSt, cacheSt] = await Promise.all([
      getJson("/api/health").catch(() => ({ status: "unknown" })),
      getJson("/api/llm/status").catch(() => ({})),
      getJson("/api/translation/cache_stats").catch(() => ({})),
    ]);
    if (sysBox) {
      const rows = [
        ["服务状态", health.status === "ok" ? "✅ 运行正常 (ok)" : String(health.status || "异常")],
        ["活跃模型", llmSt.active_model || (llmSt.primary && llmSt.primary.model) || "未配置"],
        ["模型降格", llmSt.is_degraded ? `⚠️ ${llmSt.degraded_text || "已降格"}` : "正常"],
        ["翻译缓存", `${cacheSt.entries || 0} 条 · 命中 ${cacheSt.hits || 0} 次`],
      ];
      sysBox.innerHTML = rows.map(([k, v]) => `
        <div class="lab-kv-row">
          <span class="lab-kv-key">${escapeHtml(k)}</span>
          <span class="lab-kv-val">${escapeHtml(v)}</span>
        </div>`).join("");
    }
  } catch (e) {
    if (sysBox) sysBox.textContent = "健康检查失败: " + (e.message || e);
  }

  try {
    const [syncSt, snaps] = await Promise.all([
      getJson("/api/sync/status").catch(() => ({})),
      listSnapshots().catch(() => []),
    ]);
    if (syncBox) {
      const kinds = syncSt.kinds || {};
      const files = syncSt.files || {};
      const mb = files.total_bytes ? (files.total_bytes / 1048576).toFixed(1) : "0.0";
      const rows = [
        ["同步根目录", syncSt.root || "未检测"],
        ["同步通道数量", `快照 ${(kinds.snapshots || []).length} · 批注 ${(kinds.annotations || []).length} · 进度 ${(kinds.progress || []).length}`],
        ["云端 PDF 实体", `${files.count || 0} 篇 · ${mb} MB`],
        ["本机译文快照", `${Array.isArray(snaps) ? snaps.length : 0} 篇`],
      ];
      syncBox.innerHTML = rows.map(([k, v]) => `
        <div class="lab-kv-row">
          <span class="lab-kv-key">${escapeHtml(k)}</span>
          <span class="lab-kv-val">${escapeHtml(v)}</span>
        </div>`).join("");
    }
  } catch (e) {
    if (syncBox) syncBox.textContent = "同步检测失败: " + (e.message || e);
  }
}

async function runLabPlotCompare() {
  const paperCsv = (document.getElementById("lab-paper-csv")?.value || "").trim();
  const reproCsv = (document.getElementById("lab-repro-csv")?.value || "").trim();
  const out = document.getElementById("lab-plot-metrics");
  if (!paperCsv || !reproCsv) {
    showToast("请粘贴论文基准与复现曲线的 CSV 点集", true);
    return;
  }
  try {
    const res = await postJson("/api/plot/compare", {
      paper_points: [],
      repro_points: [],
      paper_csv: paperCsv,
      repro_csv: reproCsv,
      pass_pct: 5.0,
      warn_pct: 20.0,
    });
    if (out) {
      out.innerHTML = `
        <div class="lab-metric-card"><div class="lab-metric-val">${escapeHtml(res.verdict || res.status)}</div><div class="lab-metric-lbl">评估结论</div></div>
        <div class="lab-metric-card"><div class="lab-metric-val">${Number(res.mean_rel_error_pct || 0).toFixed(2)}%</div><div class="lab-metric-lbl">平均相对误差</div></div>
        <div class="lab-metric-card"><div class="lab-metric-val">${Number(res.rmse || 0).toExponential(2)}</div><div class="lab-metric-lbl">RMSE</div></div>
        <div class="lab-metric-card"><div class="lab-metric-val">${Number(res.r_squared ?? 0).toFixed(4)}</div><div class="lab-metric-lbl">R² 拟合优度</div></div>
      `;
    }
    showToast("✅ 曲线误差评估完成");
  } catch (e) {
    showToast("评估失败: " + (e.message || e), true);
  }
}

function initLabsView() {
  const root = document.getElementById("view-labs");
  if (!root || root.dataset.bound === "1") return;
  root.dataset.bound = "1";

  const refreshBtn = document.getElementById("labs-refresh-btn");
  if (refreshBtn) refreshBtn.addEventListener("click", loadLabsDiagnostics);

  const syncBtn = document.getElementById("labs-sync-btn");
  if (syncBtn) syncBtn.addEventListener("click", () => syncNow());

  const releaseBtn = document.getElementById("labs-release-btn");
  if (releaseBtn) releaseBtn.addEventListener("click", () => releaseCloudPdfs());

  const clearCacheBtn = document.getElementById("labs-clear-cache-btn");
  if (clearCacheBtn) {
    clearCacheBtn.addEventListener("click", async () => {
      try {
        await postJson("/api/translation/cache_clear", {});
        showToast("✅ 翻译内存缓存已清空");
        loadLabsDiagnostics();
      } catch (e) {
        showToast("清空失败: " + (e.message || e), true);
      }
    });
  }

  const cmpBtn = document.getElementById("labs-plot-compare-btn");
  if (cmpBtn) cmpBtn.addEventListener("click", runLabPlotCompare);
}

export {
  loadLabsDiagnostics,
  runLabPlotCompare,
  initLabsView,
};
