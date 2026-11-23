# -*- coding: utf-8 -*-
"""
Validate the generated tablet bundle.

The bundle is one HTML file the user drops onto a tablet. If anything
needed at runtime is missing, the page silently comes up empty — so these
checks are about "the artefact is complete and self-contained", not about
how it looks.

Run:  python microbench/_test_tablet_export.py
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "MicroBench_平板工作台.html"

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


def hostile_payload_probe():
    """用**真浏览器**验证：恶意载荷放进数据块后，后续脚本仍然执行。

    机制（HTML 规范 script data state 家族）：
      载荷里出现 `<!--` 且**紧跟 `<`** → 解析器进入
      *script data double escape start* 状态 → 数据块的 `</script>`
      **永远不闭合** → 后面内联的 app.js / KaTeX / PDF.js 整段被吞掉
      → **平板工作台 / 离线阅读版整个报废**（打开只有一堆 JSON 文本）。

    判据 = 后续 `<script>` 有没有真的执行（它会改 document.title）。
    ⚠ 不能用「字符串出现在 DOM 里」当判据 —— --dump-dom 本来就含 script 原文。
    """
    sys.path.insert(0, str(REPO))
    try:
        from microbench import _headless_browser as hb
        from microbench.export_offline import _escape_json_for_script
    except Exception as e:  # pragma: no cover
        return None, "无法导入导出模块/浏览器工具: %s" % e

    browser = hb.find_playwright_chromium() or hb.find_system_browser()
    if not browser:
        return None, "找不到 Edge/Chrome（跳过浏览器实测）"

    tpl = ("<!DOCTYPE html><html><head><title>NOT_EXECUTED</title></head><body>\n"
           '<script type="application/json" id="d">{payload}</script>\n'
           "<script>document.title = \"EXECUTED\";</script>\n</body></html>")
    cases = [
        ("<!-- 紧跟 <script>（已实测会报废的原始载荷）", "<!--<script>"),
        ("<!-- 紧跟 <br>", "<!--<br>"),
        ("<!-- 紧跟 <math>", "<!--<math>"),
        ("正常正文", "普通正文"),
    ]
    out = []
    with tempfile.TemporaryDirectory() as td:
        for i, (label, hostile) in enumerate(cases):
            payload = _escape_json_for_script(json.dumps({"x": hostile},
                                                          ensure_ascii=False))
            p = Path(td) / ("case_%02d.html" % i)
            p.write_text(tpl.format(payload=payload), encoding="utf-8")
            try:
                dom = hb.dump_dom(p, browser=browser, virtual_time_budget=1200,
                                  timeout=60)
            except Exception as e:
                out.append((label, False, "渲染失败: %s" % e))
                continue
            title = ""
            k = dom.find("<title>")
            if k >= 0:
                j = dom.find("</title>", k)
                title = dom[k + 7:j].strip()
            out.append((label, title == "EXECUTED", title))
    return out, None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    # ⚠ 先**重新导出**再检查。
    #   以前这个测试直接读仓库根目录里那份 HTML —— 那是上一次构建留下的
    #   旧产物。改了 app.js 却忘了重导出，测试照样全绿，交付给用户的
    #   却是老版本。测试必须对着**刚导出来的**东西说话。
    #
    # ⚠⚠ 导出到**临时文件**，不要覆盖仓库根目录那份产物。
    #   根目录那份是**交付物**（用户直接用 Chrome 打开的，也是交付验收
    #   拿来跟 APK 里内嵌的页面逐字节比对的参照物）。
    #   测试去改它 ⇒ 别的套件/验收看到的就是「被测试动过手脚」的版本，
    #   两份一比必然不一致（2026-10-25 实际被这个坑绊了两次）。
    #   交付物只由构建脚本（android/build_apk.ps1）产出。
    print("[0] 重新导出平板工作台单文件（到临时文件，不碰交付物）")
    tmp_dir = tempfile.mkdtemp(prefix="mb_tablet_export_")
    tmp_bundle = Path(tmp_dir) / BUNDLE.name
    ex = subprocess.run(
        [sys.executable, str(REPO / "microbench" / "export_offline.py"),
         "--tablet", str(tmp_bundle), "--quiet"],
        capture_output=True, text=True, encoding="utf-8")
    if ex.returncode != 0:
        print(f"  导出失败: {ex.stdout[-500:]} {ex.stderr[-500:]}")
        return 1
    print(f"  已重新导出: {tmp_bundle}")

    if not tmp_bundle.exists():
        print(f"产物不存在: {tmp_bundle}\n先跑: python microbench/export_offline.py --tablet")
        return 1

    doc = tmp_bundle.read_text(encoding="utf-8")

    print("\n[1] 自包含: 不能依赖任何外部资源")
    ext = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//[^"\']+', doc)
    check(not ext, f"没有外部资源引用 ({len(ext)} 个)")
    # url() 里允许 data:、文档内引用 url(#mask)、以及 KaTeX 的模板变量
    # url(${x})。注意不能写成 url\(...(?!data:) —— 引号可选会产生回溯，
    # 带引号的 url("data:...") 会被误判成外部引用。
    all_urls = re.findall(r"url\([^)]*\)", doc)
    bad = [u for u in all_urls
           if not re.match(r'url\(\s*["\']?data:', u)
           and not re.match(r"url\(\s*#", u)
           and not u.startswith("url(${")]
    check(not bad, f"所有 url() 都是 data:/文档内引用 ({len(bad)} 个例外: {bad[:2]})")
    check("font/woff2;base64" in doc, "KaTeX 字体已内嵌为 base64")
    check("data:image/png;base64" in doc, "应用图标已内嵌")
    check(not re.search(r"@import\s+url\(\s*[\"']?https?:", doc), "CSS 没有 @import 外部表")

    print("\n[2] 运行时齐备")
    check("katex" in doc and "renderToString" in doc, "KaTeX 运行时已内联")
    check("renderMarkdown" in doc, "渲染器已内联")
    check("showDirectoryPicker" in doc, "目录直连能力在 (File System Access)")
    check("indexedDB" in doc, "IndexedDB 本地存储在")
    check("chat/completions" in doc, "AI 直连调用在")
    check("microbench-tablet-pack" in doc, "变更包格式在")

    print("\n[3] 触控 / 平板适配")
    # 平板工作台是触控优先的：触控目标尺寸是无条件放大的，
    # 不靠 @media (pointer: coarse) —— 那个断点只用在桌面版 index.html 上。
    flat = doc.replace(" ", "")
    check("min-height:44px" in flat or "@media(pointer:coarse)" in flat,
          "触控目标已按触控设备放大")
    check("viewport-fit=cover" in doc, "viewport-fit=cover 已声明")
    check("safe-area-inset" in doc, "使用了安全区内边距")
    check("100dvh" in doc, "用了 dvh 动态视口高度")
    check("tb-tabbar" in doc, "窄屏底部标签栏在")
    check("@media (pointer: coarse)" in flat or "min-height:44px" in flat,
          "触控目标已按触控设备放大")

    print("\n[3b] PDF 阅读器")
    check("pdfjsLib" in doc, "PDF.js 运行时已内联")
    check('id="mb-pdf-worker-src"' in doc, "worker 源码已内联 (运行时做 Blob URL)")
    check("GlobalWorkerOptions" in doc, "设置了 workerSrc")
    check("renderTextLayer" in doc, "文本层已接上 (可划词)")
    check("tb-pdf-reader" in doc or 'id="pdf-reader"' in doc, "PDF 阅读器 UI 在")
    check("tb-pdf-item" in doc, "文献库列表项样式在")
    check("getVaultFile" in doc or "getFileHandle" in doc, "从云盘目录取文件的逻辑在")

    print("\n[4] 数据块")
    m = re.search(r'<script type="application/json" id="mb-data">(.*?)</script>', doc, re.S)
    check(m is not None, "数据块存在")
    if not m:
        print("cannot continue")
        return 1
    raw = m.group(1)
    check("</" not in raw.replace("<\\/", ""), "数据块里没有会提前关闭 script 的 </")
    # ⚠⚠ 光没有 `</` 不够：`<!--` **紧跟 `<`** 会让解析器进入
    #   script-data double-escape 状态 → 数据块的 `</script>` 永不闭合
    #   → 后面内联的 app.js 全被吞掉 → 整个平板工作台报废。
    #   → 数据块里必须**一个裸的 `<` 都没有**（`<` 全部写成 `\u003c`）。
    check("<" not in raw,
          "数据块里没有任何裸的 <（`<!--<` 会让整个应用报废）")
    payload = json.loads(raw.replace("<\\/", "</"))
    # 转义必须**无损**：parse 回来要逐字相同（否则用户看到的正文被改了）
    sys.path.insert(0, str(REPO))
    try:
        from microbench.export_offline import _escape_json_for_script
        _probe = json.dumps({"x": "a<b><!--<script><!--"}, ensure_ascii=False)
        _rt = json.loads(_escape_json_for_script(_probe))
        check(_rt == {"x": "a<b><!--<script><!--"},
              "转义是无损的（parse 回来逐字相同，正文一个字节都没变）")
        check("<" not in _escape_json_for_script(_probe),
              "转义后载荷里不再有裸的 <")
    except Exception as e:
        check(False, "转义函数不可用: %s" % e)
    notes = payload["notes"]
    check(len(notes) >= 30, f"导出了 {len(notes)} 篇笔记")
    check(all(n.get("id") for n in notes), "每篇都有 id")
    check(all(n.get("title") for n in notes), "每篇都有标题")
    check(all(n.get("body") for n in notes), "每篇都有 Markdown 源码 (可编辑)")
    check(all(n.get("group") for n in notes), "每篇都有分组")
    with_math = [n for n in notes if "$" in (n.get("body") or "")]
    check(len(with_math) > 0, f"{len(with_math)} 篇笔记含公式")

    print("\n[4b] PDF 清单与关联")
    pdfs = payload.get("pdfs") or {}
    check(len(pdfs) > 0, f"PDF 清单有 {len(pdfs)} 个")
    for pid, meta in list(pdfs.items())[:3]:
        ok = (pid.endswith(".pdf") and meta.get("name") and meta.get("size", 0) > 0)
        check(ok, f"PDF 条目结构完整: {pid[:50]}")
    linked = [n for n in notes if n.get("pdfs")]
    check(len(linked) > 0, f"{len(linked)} 篇笔记关联到原文 PDF")
    bad = [n["rel"] for n in notes
           for pid in (n.get("pdfs") or []) if pid not in pdfs]
    check(not bad, f"关联指向的都是清单里的 PDF (坏链 {len(bad)})")

    print("\n[4c] 恶意载荷不得让整个应用报废（真浏览器实测）")
    probe_rows, probe_err = hostile_payload_probe()
    if probe_err:
        print(f"  SKIP  {probe_err}")
    else:
        for label, executed, detail in probe_rows:
            check(executed, f"恶意载荷下后续脚本仍执行: {label}  [{detail}]")

    print("\n[5] 内联的 JS 必须语法正确 (node --check)")
    blocks = re.findall(r"<script>\n(.*?)\n</script>", doc, re.S)
    app_block = None
    for b in blocks:
        if "renderMarkdown" in b:
            app_block = b
            break
    check(app_block is not None, "找到内联的 app.js")
    if app_block:
        tmp = REPO / "logs" / "_tablet_app_check.js"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(app_block, encoding="utf-8")
        proc = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
        check(proc.returncode == 0, f"node --check 通过 ({proc.stderr.strip()[:120]})")
        tmp.unlink(missing_ok=True)

    print("\n[6] 与源码一致 (内联不能被 str.replace 的 $ 规则改坏)")
    src = (REPO / "microbench" / "static" / "tablet" / "app.js").read_text(encoding="utf-8")
    check(app_block == src, "内联的 app.js 与源文件逐字节一致")

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
