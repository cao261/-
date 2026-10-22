/*
 * Batch KaTeX renderer for the offline export.
 *
 * Input  (stdin):  [{"id": 0, "tex": "\\mu_0", "display": false}, ...]
 * Output (stdout): [{"id": 0, "html": "<span class=\"katex\">...", "error": null}, ...]
 *
 * The formulas are rendered here, at export time, rather than in the
 * browser. That keeps the generated HTML self-contained: the tablet can open
 * it from file:// with no network, no CDN and no KaTeX JavaScript at all.
 */
const katex = require("./_vendor/node_modules/katex");

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => { input += d; });
process.stdin.on("end", () => {
  let items;
  try {
    items = JSON.parse(input);
  } catch (e) {
    process.stderr.write("bad json: " + e.message + "\n");
    process.exit(1);
    return;
  }
  const out = items.map((it) => {
    try {
      const html = katex.renderToString(it.tex, {
        displayMode: !!it.display,
        throwOnError: false,
        strict: "ignore",
        trust: false,
        output: "html",
        macros: {
          "\\RR": "\\mathbb{R}",
          "\\NN": "\\mathbb{N}",
          "\\ZZ": "\\mathbb{Z}",
          "\\CC": "\\mathbb{C}",
          "\\EE": "\\mathbb{E}",
          "\\ket": "\\left|#1\\right\\rangle",
          "\\bra": "\\left\\langle#1\\right|",
          "\\dd": "\\mathrm{d}"
        }
      });
      return { id: it.id, html, error: null };
    } catch (e) {
      return { id: it.id, html: null, error: String((e && e.message) || e) };
    }
  });
  process.stdout.write(JSON.stringify(out));
});
