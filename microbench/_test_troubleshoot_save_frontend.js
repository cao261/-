/*
 * 「➕ 存入避坑库」一键保存 —— 前端**行为级**护栏（node 跑 index.html 的真源码）。
 *
 * 为什么要有这个文件（2026-11-17 全面功能审查 D1，**确证缺陷**）：
 *   前端 `saveQAToTroubleshoot()` 发的是
 *       raw_error: "", root_cause: ""
 *   而后端 `TroubleshootRequest` 这两个字段都是 `min_length=1`
 *   → **必然 422**，「一键存入避坑库」这个功能**从来就没成功过**；
 *   而且报错被写成「API 暂不可用」，把排查方向带偏（用户会去查网络）。
 *   （同一端点的另一条路——右侧手填表单——传的是非空占位串，所以它是好的：
 *     **只有「一键存」这条路 100% 失败**。）
 *
 * ⚠ 这条护栏是行为级的：把 index.html 里那个函数**切出来真跑一遍**，
 *   断言它**实际发出的请求体**能不能通过后端的校验。
 *   不是「源码里有某个字符串」那种恒真的假护栏。
 *
 * ⚠⚠ 自带「变异自检」：把抽出来的函数改坏后重跑同一套断言，必须**出现失败**。
 *
 * Run:  node microbench/_test_troubleshoot_save_frontend.js
 *       node microbench/_test_troubleshoot_save_frontend.js --suite-only
 */
const fs = require("fs");
const path = require("path");

const { loadDesktopSource } = require("./_test_frontend_loader");

const TARGET = path.join(__dirname, "static", "index.html");
const SRC = loadDesktopSource(TARGET);
const SUITE_ONLY = process.argv.includes("--suite-only");

/** 从源码里切出某个函数（按花括号配平）。 */
function sliceFn(src, name) {
  const i = src.indexOf("function " + name);
  if (i < 0) throw new Error("找不到函数：" + name);
  const open = src.indexOf("{", i);
  let depth = 0;
  for (let k = open; k < src.length; k++) {
    if (src[k] === "{") depth++;
    else if (src[k] === "}") { depth--; if (depth === 0) return src.slice(i, k + 1); }
  }
  throw new Error("函数体没闭合：" + name);
}

// 后端 TroubleshootRequest 的约束（app.py）—— 改这里就要同步改后端
const LIMITS = {
  tool_name: [1, 100],
  category: [1, 50],
  error_keyword: [1, 200],
  raw_error: [1, 10000],
  root_cause: [1, 5000],
  solution: [1, 5000],
};

/**
 * 跑一遍「一键存入避坑库」，返回实际发出的请求体 + 断言结果。
 * 用的全是**真函数**，桩只提供它依赖的外部世界。
 */
async function runSuite(src) {
  const fnSrc = sliceFn(src, "saveQAToTroubleshoot");
  const sent = [];
  const toasts = [];
  const els = {};
  const doc = {
    getElementById(id) {
      if (!els[id]) els[id] = { value: "" };
      return els[id];
    },
  };
  const fetchStub = async (url, opts) => {
    sent.push({ url: String(url), body: JSON.parse(opts.body) });
    return {
      ok: true, status: 200,
      json: async () => ({ status: "success" }),
    };
  };
  const factory = new Function(
    "showKeywordPickerModal", "_lastAnswerText", "_lastQAItem",
    "fetch", "showToast", "loadStats", "document",
    fnSrc + "\nreturn saveQAToTroubleshoot;");
  let modalCall = 0;
  const save = factory(
    async () => {
      modalCall++;
      if (modalCall === 1) {
        return {
          tool: "论文精读答疑",
          category: "TCAD仿真收敛",
          keyword: "磁电传感器 灵敏度",
          solution: "灵敏度 S = dE/dH，线性区最好。",
        };
      }
      // 用户把关键词清空了 → 后端 error_keyword 有 min_length=1 → 必须有兜底
      return {
        tool: "论文精读答疑",
        category: "TCAD仿真收敛",
        keyword: "",
        solution: "灵敏度 S = dE/dH，线性区最好。",
      };
    },
    "灵敏度 S = dE/dH，线性区最好。",
    {
      title: "2026-磁电器件综述",
      question: "磁电传感器的灵敏度怎么定义？",
      selection: "d33 = dE/dH",
      page: 7,
      answer: "灵敏度 S = dE/dH。",
    },
    fetchStub,
    (msg) => { toasts.push(msg); },
    () => {},
    doc);
  await save("磁电传感器 灵敏度");
  await new Promise((r) => setTimeout(r, 0));
  await save("磁电传感器 灵敏度");
  await new Promise((r) => setTimeout(r, 0));

  // ---- 断言（在 runSuite 内部，变异自检才能复用同一套）----
  const results = [];
  const ck = (cond, label, detail) =>
    results.push({ ok: !!cond, label: label, detail: detail || "" });

  ck(sent.length === 2, "两次点击各发出去 1 次请求", "sent=" + sent.length);
  if (!sent.length) return { sent, toasts, results, body: {} };
  const url = sent[0].url;
  const body = sent[0].body;
  ck(url.indexOf("/api/troubleshoot/add") >= 0, "打的是避坑库端点", url);
  for (const [k, [lo, hi]] of Object.entries(LIMITS)) {
    const v = body[k];
    const isStr = typeof v === "string";
    ck(isStr && v.trim().length >= lo && v.length <= hi,
      `字段 ${k} 非空且不超长（后端要求 ${lo}~${hi}）`,
      isStr ? JSON.stringify(v).slice(0, 80) : typeof v);
  }
  ck(String(body.raw_error).indexOf("灵敏度") >= 0,
    "raw_error 里真的有内容（原始问题）", String(body.raw_error).slice(0, 80));
  ck(String(body.root_cause).indexOf("dE/dH") >= 0,
    "root_cause 里真的有内容（解答的结论）", String(body.root_cause).slice(0, 80));
  ck(toasts.some((t) => String(t).indexOf("已存入避坑知识库") >= 0),
    "成功后提示「已存入避坑知识库」", JSON.stringify(toasts));
  // 用户把关键词清空 → 也不能发空串（后端 min_length=1 → 又一次 422）
  const body2 = (sent.length >= 2) ? sent[1].body : {};
  ck(typeof body2.error_keyword === "string" &&
    body2.error_keyword.trim().length >= 1,
    "关键词被清空时也有兜底（不会又 422）",
    JSON.stringify(body2.error_keyword));
  return { sent, toasts, results, body };
}

let pass = 0, fail = 0;
function report(results) {
  for (const r of results) {
    if (r.ok) { pass++; console.log("  PASS  " + r.label); }
    else { fail++; console.log("  FAIL  " + r.label + (r.detail ? " — " + r.detail : "")); }
  }
}

(async function main() {
  if (process.argv.includes("--dump-payload")) {
    // 给 pytest 用：把「实际发出的请求体」吐出来，让后端模型去校验。
    const r = await runSuite(SRC);
    console.log("PAYLOAD:" + JSON.stringify(r.sent.map((x) => x.body)));
    return;
  }
  console.log("\n=== 一键存入避坑库：请求体必须能过后端校验 ===");
  const r0 = await runSuite(SRC);
  report(r0.results);

  if (SUITE_ONLY) {
    console.log("\nPASS " + pass + "  FAIL " + fail);
    process.exit(fail === 0 ? 0 : 1);
  }

  console.log("\n=== 变异自检（断言必须能抓到回归）===");
  const MUTATIONS = [
    {
      name: "raw_error 又变回空串（原始缺陷）",
      from: 'raw_error: rawError || "论文精读答疑记录",',
      to: 'raw_error: "",',
    },
    {
      name: "root_cause 又变回空串（原始缺陷）",
      from: 'root_cause: rootCause || "见解决方案",',
      to: 'root_cause: "",',
    },
    {
      name: "关键词不再兜底（用户清空关键词 → 又 422）",
      from: 'error_keyword: String(result.keyword || "").trim()\n            || String(qa.question || "").trim().slice(0, 30) || "论文精读答疑",',
      to: 'error_keyword: String(result.keyword || "").trim(),',
    },
  ];
  let caught = 0;
  for (const m of MUTATIONS) {
    if (SRC.indexOf(m.from) < 0) {
      console.log("  FAIL  变异 [" + m.name + "] 没匹配到源码 —— 变异自检本身失效");
      fail++;
      continue;
    }
    const mutated = SRC.replace(m.from, m.to);
    let mutantFail = 0;
    try {
      const r = await runSuite(mutated);
      mutantFail = r.results.filter((x) => !x.ok).length;
    } catch (e) {
      mutantFail = 1;
    }
    if (mutantFail > 0) {
      caught++;
      console.log("  PASS  变异 [" + m.name + "] 被抓到了");
    } else {
      console.log("  FAIL  变异 [" + m.name + "] 没被抓到 —— 这套断言是恒真的！");
      fail++;
    }
  }

  console.log("\n============================================================");
  console.log("PASS " + pass + "  FAIL " + fail +
              "  (变异捕获 " + caught + "/" + MUTATIONS.length + ")");
  console.log("============================================================");
  process.exit(fail === 0 && caught === MUTATIONS.length ? 0 : 1);
})().catch((e) => {
  console.error("护栏自身崩了: " + (e && e.stack));
  process.exit(1);
});
