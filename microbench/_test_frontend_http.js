// 前端 HTTP helper 的行为测试（2026-10-27）。
//
// 为什么要测：getJson/postJson 的核心价值是「**先判状态码，再解析 JSON**」。
// 旧写法 `const d = await res.json(); if (!res.ok) throw ...` 在服务端返回
// 非 JSON（502 HTML 页、504 网关文本）时会先抛 SyntaxError：
//   - 真实状态码丢失
//   - 用户看到 "Unexpected token '<'" 而不是"保存失败"
// 这个文件把 helper 从 index.html 里切出来测（测的是**发布出去的那份代码**，
// 不是抄一份）。

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const HTML = fs.readFileSync(
  path.join(__dirname, "static", "index.html"), "utf8");

// 从 index.html 里切出 helper 的源码
function extractHelper(name) {
  const marker = `function ${name}(`;
  const start = HTML.indexOf(marker);
  if (start < 0) throw new Error(`index.html 里找不到 ${name}`);
  const fnStart = HTML.lastIndexOf("async function", start);
  let depth = 0;
  const bodyStart = HTML.indexOf("{", start);
  let i = bodyStart;
  for (; i < HTML.length; i++) {
    if (HTML[i] === "{") depth++;
    else if (HTML[i] === "}") {
      depth--;
      if (depth === 0) break;
    }
  }
  return HTML.slice(fnStart, i + 1);
}

const src = [
  extractHelper("_readJsonSafe"),
  extractHelper("_httpErrorMessage"),
  extractHelper("getJson"),
  extractHelper("postJson"),
].join("\n");

const calls = [];
let nextResponse = null;
const fetchStub = async (url, opts) => {
  calls.push({ url, opts });
  return nextResponse;
};

const factory = new Function("fetch", src + `
  return { _readJsonSafe, _httpErrorMessage, getJson, postJson };
`);
const { _readJsonSafe, getJson, postJson } = factory(fetchStub);

function makeRes({ status = 200, body = "" } = {}) {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => body,
    json: async () => JSON.parse(body),
  };
}

let passed = 0;
const failures = [];

async function t(name, fn) {
  try {
    await fn();
    console.log("  ok   " + name);
    passed++;
  } catch (e) {
    console.error("  FAIL " + name + "\n       " + (e && e.message));
    failures.push(name);
  }
}

(async () => {
  console.log("== 顺序：先判状态码 ==");

  await t("非 JSON 的 502 → 抛的是可读错误，不是 SyntaxError", async () => {
    nextResponse = makeRes({ status: 502, body: "<html>Bad Gateway</html>" });
    let err = null;
    try { await getJson("/api/x"); } catch (e) { err = e; }
    assert.ok(err, "应该抛错");
    assert.ok(!(err instanceof SyntaxError),
      "抛的是 SyntaxError（正是要修的那个症状）：" + err.message);
    assert.ok(/502/.test(err.message), "错误消息里没有状态码：" + err.message);
    assert.ok(/Bad Gateway/.test(err.message), "错误消息里没有服务端原文：" + err.message);
  });

  await t("FastAPI 校验错误：detail 是数组 → 拼成可读文本", async () => {
    nextResponse = makeRes({
      status: 422,
      body: JSON.stringify({ detail: [{ loc: ["body", "page"], msg: "字段类型错误" }] }),
    });
    let err = null;
    try { await postJson("/api/x", {}); } catch (e) { err = e; }
    assert.ok(err, "应该抛错");
    assert.ok(/字段类型错误/.test(err.message), err.message);
    assert.ok(/body\.page/.test(err.message), err.message);
  });

  await t("detail 是字符串 → 直接用它", async () => {
    nextResponse = makeRes({ status: 400, body: JSON.stringify({ detail: "路径非法" }) });
    let err = null;
    try { await postJson("/api/x", {}); } catch (e) { err = e; }
    assert.ok(err, "应该抛错");
    assert.ok(/路径非法/.test(err.message), err.message);
  });

  await t("200 时正常返回解析后的数据", async () => {
    nextResponse = makeRes({ status: 200, body: JSON.stringify({ ok: 1, name: "x" }) });
    const d = await postJson("/api/x", { a: 1 });
    assert.strictEqual(d.ok, 1);
    assert.strictEqual(d.name, "x");
  });

  await t("postJson 发的 body 是 JSON 字符串且带 Content-Type", async () => {
    calls.length = 0;
    nextResponse = makeRes({ status: 200, body: "{}" });
    await postJson("/api/x", { a: 1 });
    assert.strictEqual(calls.length, 1, "fetch 没被调用");
    const c = calls[0];
    assert.strictEqual(c.opts.method, "POST");
    assert.strictEqual(c.opts.headers["Content-Type"], "application/json");
    assert.strictEqual(c.opts.body, JSON.stringify({ a: 1 }));
  });

  await t("postJson 不传 payload 时发 {}（不是 undefined）", async () => {
    calls.length = 0;
    nextResponse = makeRes({ status: 200, body: "{}" });
    await postJson("/api/x");
    assert.strictEqual(calls[0].opts.body, "{}");
  });

  await t("getJson 支持自定义 method（DELETE 等）", async () => {
    calls.length = 0;
    nextResponse = makeRes({ status: 200, body: "{}" });
    await getJson("/api/x", { method: "DELETE" });
    assert.strictEqual(calls[0].opts.method, "DELETE");
  });

  await t("空响应体 + 非 2xx → 不崩，消息里有状态码", async () => {
    nextResponse = makeRes({ status: 500, body: "" });
    let err = null;
    try { await getJson("/api/x"); } catch (e) { err = e; }
    assert.ok(err, "应该抛错");
    assert.ok(/500/.test(err.message), err.message);
  });

  await t("_readJsonSafe：非 JSON 文本 → data 为 null，text 保留", async () => {
    const r = makeRes({ status: 200, body: "not json" });
    const out = await _readJsonSafe(r);
    assert.strictEqual(out.data, null);
    assert.strictEqual(out.text, "not json");
  });

  await t("回归护栏：index.html 里不该再有「先 json() 后判 ok」", async () => {
    const lines = HTML.split("\n");
    const bad = [];
    for (let i = 0; i < lines.length; i++) {
      const m = /(?:const|let|var)\s+(\w+)\s*=\s*await\s+(res|r)\.json\(\)/.exec(lines[i]);
      if (!m) continue;
      for (let j = i + 1; j < Math.min(i + 4, lines.length); j++) {
        if (lines[j].includes(`${m[2]}.ok`)) { bad.push(i + 1); break; }
      }
    }
    assert.strictEqual(bad.length, 0,
      `这些行还是「先 json() 后判 ok」（行号 ${bad.join(", ")}）`);
  });

  console.log(`\n${passed + failures.length} 项，失败 ${failures.length} 个`);
  process.exit(failures.length ? 1 : 0);
})();
