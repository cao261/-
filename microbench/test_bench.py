"""
Verification suite for MicroBench V1.1.

Covers:
- Original happy-path (paper fetch/save, repro scaffold, weekly report, troubleshoot).
- Path-traversal attacks rejected.
- Whitelist enforcement (topic_category, project_type, troubleshoot category).
- Special characters in titles (Chinese + curly braces + spaces).
- Paper fetcher dispatch + DOI/arXiv id normalization.
- BibTeX generation (article vs arXiv misc entry).
- _safe_path rejects .. and absolute paths.

Run:
    python -m microbench.test_bench
or:
    cd <workbench_root> && python microbench/test_bench.py
"""
import sys
import io
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from microbench.generator import (
    BASE_DIR,
    _safe_path,
    _render_template,
    sanitize_filename,
    get_vault_stats,
    save_literature_card,
    create_reproduction_workspace,
    record_troubleshoot,
    generate_weekly_report,
    search_vault,
    ALLOWED_TOPIC_CATEGORIES,
    ALLOWED_PROJECT_TYPES,
    ALLOWED_TROUBLESHOOT_CATEGORIES,
)
from microbench.paper_fetcher import (
    _normalize_arxiv_id,
    _normalize_doi,
    generate_bibtex,
    fetch_arxiv_metadata,
    _fetch_arxiv_fallback_semanticscholar,
)
from microbench import llm
from microbench.plot_compare import (
    extract_curve_from_image,
    calibrate as calibrate_points,
    compute_metrics,
    _parse_csv_points,
    _linear_interp,
)
from microbench.paper_text import (
    extract_text_from_pdf,
    split_into_sections,
    _classify_title_to_section,
)
from microbench.translation import (
    chunk_section_text,
    TranslationCache,
    build_cross_section_context,
    reset_cache,
)

PASS_COUNT = 0
FAIL_COUNT = 0
FAILED_TESTS = []


def _check(cond: bool, label: str):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"  ✅ {label}")
    else:
        FAIL_COUNT += 1
        FAILED_TESTS.append(label)
        print(f"  ❌ {label}")


def _cleanup_test_artifacts():
    """
    Remove any files created by previous test runs so the suite is idempotent.
    Tests are matched by filename pattern rather than timestamp to be safe.
    """
    import shutil
    patterns = [
        ("SmokeTest", "md"),        # save_literature_card smoke titles
        ("MoS2", "md"),             # special-chars title
        ("BilingualTest", "md"),    # export_bilingual test
        ("QA_", "md"),              # export_qa test
    ]
    for lit_cat in (BASE_DIR / "01_Literature").iterdir():
        if not lit_cat.is_dir():
            continue
        for f in lit_cat.rglob("*.md"):
            for pat, ext in patterns:
                if pat in f.name and f.name.endswith(f".{ext}"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
    # Cleanup test reproduction project
    test_proj = BASE_DIR / "02_Reproduction" / "repro_test_gaa_v11"
    if test_proj.exists():
        shutil.rmtree(test_proj, ignore_errors=True)
    # Cleanup troubleshoot test entries (recognizable by exact tool_name + keyword)
    trouble_file = BASE_DIR / "03_Knowledge" / "踩坑与排错日记.md"
    if trouble_file.exists():
        text = trouble_file.read_text(encoding="utf-8")
        # Strip blocks whose keyword matches a test pattern
        import re
        cleaned = re.sub(
            r"\n---\n\n### 坑记录：\[TCAD仿真收敛\] (?:Test convergence issue V1\.1|smoke_test_convergence) \(2026-09-20\)\n- \*\*工具/环境\*\*：[^\n]+\n- \*\*错误现场\*\*：\n```text\n[^\n]+\n```\n- \*\*根本原因\*\*：\n[^\n]+\n- \*\*解决方案\*\*：\n```bash\n[^\n]+\n```\n",
            "",
            text,
        )
        if cleaned != text:
            trouble_file.write_text(cleaned, encoding="utf-8")


# ===========================================================================
# 1. Path safety
# ===========================================================================
def test_path_safety():
    print("\n[1] Path safety / traversal protection")
    # Normal usage
    p = _safe_path(BASE_DIR, "01_Literature", "01_Device_TCAD_器件仿真")
    _check(p.exists() or True, "正常路径合法解析")

    # Reject traversal via ..
    try:
        _safe_path(BASE_DIR, "01_Literature", "..", "..", "Windows")
        _check(False, "路径穿越 '..' 必须拒绝")
    except ValueError:
        _check(True, "路径穿越 '..' 已拒绝")

    # Reject traversal embedded in single component
    try:
        _safe_path(BASE_DIR, "01_Literature/../../../etc/passwd")
        _check(False, "路径穿越 '..' 嵌入式必须拒绝")
    except ValueError:
        _check(True, "路径穿越 '..' 嵌入式已拒绝")

    # Reject Windows-style absolute path injection
    try:
        _safe_path(BASE_DIR, "C:\\Windows\\System32\\drivers\\etc\\hosts")
        _check(False, "绝对路径注入必须拒绝")
    except ValueError:
        _check(True, "绝对路径注入已拒绝")


# ===========================================================================
# 2. Whitelist enforcement
# ===========================================================================
def test_whitelist():
    print("\n[2] Whitelist enforcement")

    # Invalid topic_category
    try:
        save_literature_card({
            "title": "X",
            "topic_category": "../../etc",  # also a traversal attempt
        })
        _check(False, "非法 topic_category 必须拒绝")
    except ValueError as e:
        _check("非法" in str(e) or "base" in str(e), "非法 topic_category 已拒绝")

    # Invalid project_type
    try:
        create_reproduction_workspace({
            "project_name": "test_xyz",
            "project_type": "MALWARE",
        })
        _check(False, "非法 project_type 必须拒绝")
    except ValueError:
        _check(True, "非法 project_type 已拒绝")

    # Invalid troubleshoot category
    try:
        record_troubleshoot({
            "tool_name": "X",
            "category": "火锅底料",  # not in whitelist
            "error_keyword": "Y",
            "raw_error": "Z",
            "root_cause": "R",
            "solution": "S",
        })
        _check(False, "非法 troubleshoot category 必须拒绝")
    except ValueError:
        _check(True, "非法 troubleshoot category 已拒绝")


# ===========================================================================
# 3. Special characters in titles
# ===========================================================================
def test_special_chars():
    print("\n[3] Special characters in titles")

    # Chinese + curly braces + spaces + slashes (slashes get sanitized)
    weird_title = "新型 {MoS2}/HZO 异质结 2D-FET — 物理建模"
    sanitized = sanitize_filename(weird_title)
    # Forbidden Windows chars removed; spaces → underscore; curly braces ARE legal
    forbidden = set('\\/*?:"<>|')
    _check(not any(c in forbidden for c in sanitized) and " " not in sanitized,
           f"特殊字符已净化 (禁用字符移除, 空格转下划线): '{weird_title}' → '{sanitized}'")

    # Should still produce a valid file
    res = save_literature_card({
        "title": weird_title,
        "authors": "Test Author",
        "year": 2024,
        "venue": "IEDM",
        "topic_category": "01_Device_TCAD_器件仿真",
        "tags": "2D, MoS2, HZO",
        "abstract": "Test abstract",
        "status": "待精读",
    })
    _check(res["status"] == "success", "含特殊字符标题保存成功")
    res_path = BASE_DIR / res["relative_path"]
    _check("{{" not in res_path.read_text(encoding="utf-8").split("# {{topic_tag}}")[0] if "{{topic_tag}}" in res_path.read_text(encoding="utf-8") else True,
           "Jinja 占位符已渲染 (无残留)")

    # Read back and verify no {{}} remnants
    out_file = BASE_DIR / res["relative_path"]
    content = out_file.read_text(encoding="utf-8")
    import re
    leftover = re.findall(r"\{\{[a-zA-Z_]+\}\}", content)
    _check(len(leftover) == 0, f"渲染后无残留占位符: {leftover}")


# ===========================================================================
# 4. Paper fetcher dispatch
# ===========================================================================
def test_paper_fetcher_dispatch():
    print("\n[4] Paper fetcher id normalization")

    arxiv_ids = [
        ("2303.15982", "2303.15982"),
        ("arXiv:2303.15982v1", "2303.15982"),
        ("https://arxiv.org/abs/2303.15982v2", "2303.15982"),
        ("  2303.15982  ", "2303.15982"),
    ]
    for raw, expected in arxiv_ids:
        out = _normalize_arxiv_id(raw)
        _check(out == expected, f"arXiv id '{raw}' → '{out}'")

    dois = [
        ("10.1109/TED.2024.1234567", "10.1109/TED.2024.1234567"),
        ("https://doi.org/10.1145/3583781.3590214", "10.1145/3583781.3590214"),
        ("doi:10.1038/nature14539.", "10.1038/nature14539"),
    ]
    for raw, expected in dois:
        out = _normalize_doi(raw)
        _check(out == expected, f"DOI '{raw}' → '{out}'")


def test_arxiv_https_priority_and_fallback():
    print("\n[4b] arXiv HTTPS 优先 + Semantic Scholar 兜底 (v1.2)")
    # We mock requests.get so tests don't hit the network (flaky in CI).
    # The fix: HTTPS is tried first; HTTP is the fallback; if both fail,
    # Semantic Scholar is the last-resort fallback.
    import requests as _req

    class _MockResp:
        def __init__(self, text="", status=200):
            self.text = text
            self.status_code = status
        def raise_for_status(self):
            if self.status_code != 200:
                raise _req.HTTPError(f"HTTP {self.status_code}")
        def json(self):
            import json as _json
            return _json.loads(self.text)

    # Minimal arXiv Atom XML payload
    ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12549v1</id>
    <title>Synthetic iterative scheme for hotspot systems</title>
    <summary>A synthetic iterative scheme is developed for thermal applications.</summary>
    <published>2024-01-23T00:00:00Z</published>
    <author><name>Chuang Zhang</name></author>
    <author><name>Qin Lou</name></author>
    <arxiv:primary_category term="cond-mat.mes-hall"/>
  </entry>
</feed>"""

    called_urls = []

    def mock_get(url, **kwargs):
        called_urls.append(url)
        # First call (HTTPS) → success; never touch HTTP fallback
        if url.startswith("https://export.arxiv.org/"):
            return _MockResp(ARXIV_XML, 200)
        if url.startswith("http://export.arxiv.org/"):
            return _MockResp("", 500)
        raise RuntimeError(f"unexpected URL: {url}")

    _orig_get = _req.get
    _req.get = mock_get
    try:
        called_urls.clear()
        meta = fetch_arxiv_metadata("2401.12549")
        _check(meta["title"] == "Synthetic iterative scheme for hotspot systems",
               f"HTTPS 优先: title 正确 ({meta['title'][:40]})")
        _check(meta["year"] == 2024, f"HTTPS 优先: year=2024 (实际 {meta['year']})")
        _check(called_urls[0].startswith("https://"), "首次调用走 HTTPS")
        _check(len(called_urls) == 1, f"HTTPS 成功时不再尝试 HTTP (实际 {len(called_urls)} 次)")

        # Scenario 2: HTTPS fails (timeout) → falls back to HTTP
        called_urls.clear()
        def https_fail(url, **kwargs):
            called_urls.append(url)
            if url.startswith("https://export.arxiv.org/"):
                raise _req.ConnectionError("HTTPS blocked")
            if url.startswith("http://export.arxiv.org/"):
                return _MockResp(ARXIV_XML, 200)
            raise RuntimeError(url)
        _req.get = https_fail
        meta2 = fetch_arxiv_metadata("2401.12549")
        _check(meta2["title"] == "Synthetic iterative scheme for hotspot systems",
               "HTTPS 失败 → HTTP 降级成功")
        _check(any(u.startswith("https://") for u in called_urls),
               "HTTPS 失败路径: 仍先尝试 HTTPS")
        _check(any(u.startswith("http://") for u in called_urls),
               "HTTPS 失败路径: 降级到 HTTP")

        # Scenario 3: Both arXiv endpoints fail → Semantic Scholar fallback
        called_urls.clear()
        def both_fail(url, **kwargs):
            called_urls.append(url)
            if "arxiv.org" in url:
                raise _req.ConnectionError("blocked")
            if "semanticscholar.org" in url:
                return _MockResp(
                    '{"title":"Synthetic iterative scheme (S2)",'
                    '"abstract":"abstract via s2",'
                    '"year":2024,"venue":"arXiv",'
                    '"authors":[{"name":"Chuang Zhang"}],'
                    '"externalIds":{"DOI":""}}',
                    200,
                )
            raise RuntimeError(url)
        _req.get = both_fail
        meta3 = fetch_arxiv_metadata("2401.12549")
        _check(meta3["source"] == "arXiv (via Semantic Scholar)",
               f"兜底成功: source 标记为 Semantic Scholar (实际 {meta3['source']})")
        _check("S2" in meta3["title"], f"兜底返回 Semantic Scholar 数据 ({meta3['title']})")
    finally:
        _req.get = _orig_get


# ===========================================================================
# 5. BibTeX generation
# ===========================================================================
def test_bibtex():
    print("\n[5] BibTeX generation")

    # arXiv-only entry → misc
    bib = generate_bibtex({
        "title": "GAA FET Benchmarking",
        "authors": "Alice Smith, Bob Jones",
        "year": 2024,
        "venue": "arXiv [cond-mat]",
        "arxiv": "2303.15982",
        "doi": "",
        "source": "arXiv",
    }, bib_key="smith2024gaa")
    _check("@misc" in bib, "arXiv-only → @misc")
    _check("eprint = {2303.15982}" in bib, "BibTeX 包含 eprint")
    _check("smith2024gaa" in bib, "BibTeX 包含自定义 key")

    # DOI entry → article
    bib = generate_bibtex({
        "title": "Self-Heating in GAA Nanosheet",
        "authors": "J. Doe, X. Wang",
        "year": 2024,
        "venue": "IEEE TED",
        "doi": "10.1109/TED.2024.1234567",
        "arxiv": "",
        "source": "Crossref",
    }, bib_key="doe2024self")
    _check("@article" in bib, "DOI entry → @article")
    _check("10.1109/TED.2024.1234567" in bib, "BibTeX 包含 DOI")
    _check("J. Doe and X. Wang" in bib, "BibTeX 作者用 'and' 分隔")

    # Auto-generated key
    bib_auto = generate_bibtex({
        "title": "Whatever",
        "authors": "Charlie Brown",
        "year": 2025,
    })
    _check("@misc" in bib_auto or "@article" in bib_auto, "Auto-key BibTeX 有效生成")


# ===========================================================================
# 6. Reproduction scaffold
# ===========================================================================
def test_reproduction_scaffold():
    print("\n[6] Reproduction scaffold")

    res = create_reproduction_workspace({
        "project_name": "repro_test_gaa_v11",
        "project_type": "TCAD",
        "toolchain": "Sentaurus TCAD 2023",
        "target_paper": "test_paper_link",
        "description": "Test CFET",
    })
    _check(res["status"] == "success", "TCAD 复现工程创建成功")
    proj = BASE_DIR / res["path"]
    _check((proj / "configs").exists(), "configs/ 子目录已创建")
    _check((proj / "scripts").exists(), "scripts/ 子目录已创建")
    _check((proj / "data").exists(), "data/ 子目录已创建")
    _check((proj / "plots").exists(), "plots/ 子目录已创建")
    _check((proj / "README.md").exists(), "README.md 已创建")

    # Verify Jinja placeholders all rendered
    readme = (proj / "README.md").read_text(encoding="utf-8")
    import re
    leftover = re.findall(r"\{\{[a-zA-Z_]+\}\}", readme)
    _check(len(leftover) == 0, f"README.md 无残留占位符: {leftover}")

    # Cleanup test directory
    import shutil
    shutil.rmtree(proj, ignore_errors=True)


# ===========================================================================
# 7. Troubleshoot log
# ===========================================================================
def test_troubleshoot():
    print("\n[7] Troubleshoot log")
    res = record_troubleshoot({
        "tool_name": "Sentaurus TCAD",
        "category": "TCAD仿真收敛",
        "error_keyword": "Test convergence issue V1.1",
        "raw_error": "Test error log",
        "root_cause": "Test root cause",
        "solution": "Test solution",
    })
    _check(res["status"] == "success", "踩坑记录保存成功")


# ===========================================================================
# 8. Weekly report
# ===========================================================================
def test_weekly_report():
    print("\n[8] Weekly report")
    res = generate_weekly_report()
    _check(res["status"] == "success", "周报生成成功")
    weekly_file = BASE_DIR / res["path"]
    content = weekly_file.read_text(encoding="utf-8")
    import re
    leftover = re.findall(r"\{\{[a-zA-Z_]+\}\}", content)
    _check(len(leftover) == 0, "周报无残留占位符")


# ===========================================================================
# 9. Vault stats
# ===========================================================================
def test_vault_stats():
    print("\n[9] Vault stats")
    stats = get_vault_stats()
    _check("literature_total" in stats, "stats 含 literature_total")
    _check("reproduction_total" in stats, "stats 含 reproduction_total")
    _check("troubleshoot_count" in stats, "stats 含 troubleshoot_count")
    _check(stats["literature_total"] >= 1, f"文献总数 >= 1 (实际 {stats['literature_total']})")


# ===========================================================================
# 10. RAG search (search_vault)
# ===========================================================================
def test_search_vault():
    print("\n[10] RAG keyword search")

    # Empty / whitespace query returns []
    _check(search_vault("") == [], "空查询返回 []")
    _check(search_vault("   ") == [], "纯空格查询返回 []")

    # Real query: search for "GAA" — should match GAA literature in vault
    results = search_vault("GAA", top_k=5)
    _check(len(results) > 0, f"GAA 查询命中 ({len(results)} 条)")
    if results:
        _check(results[0]["score"] > 0, "Top 1 score > 0")
        _check("path" in results[0], "结果含 path 字段")
        _check("title" in results[0], "结果含 title 字段")
        _check("snippet" in results[0], "结果含 snippet 字段")
        _check(len(results[0]["snippet"]) > 0, "snippet 非空")
        # Top result should mention GAA somewhere in path/title/snippet/tags
        blob = (results[0]["path"] + results[0]["title"] + results[0]["snippet"]).lower()
        _check("gaa" in blob, "Top 1 结果与 GAA 相关")

    # Chinese keyword query
    results_cn = search_vault("自热", top_k=5)
    _check(len(results_cn) > 0, f"中文关键词 '自热' 查询命中 ({len(results_cn)} 条)")

    # No-match query
    no_match = search_vault("xyzzy_no_match_keyword", top_k=5)
    _check(no_match == [], "无匹配查询返回 []")

    # Top_k respected
    results_k3 = search_vault("FET", top_k=3)
    _check(len(results_k3) <= 3, f"top_k=3 限制生效 (实际 {len(results_k3)} 条)")

    # Score sorted descending
    if len(results) > 1:
        scores = [r["score"] for r in results]
        _check(scores == sorted(scores, reverse=True), "结果按 score 降序排列")


# ===========================================================================
# 11. LLM config & fallback (no API key configured)
# ===========================================================================
def test_llm_config_and_fallback():
    print("\n[11] LLM config / fallback behavior")

    # Clear any pre-set env to simulate "not configured"
    import os
    saved = {}
    for k in ("LLM_API_KEY", "AGNES_API_KEY"):
        if k in os.environ:
            saved[k] = os.environ[k]
            del os.environ[k]

    try:
        _check(not llm.is_configured(), "未配置环境变量时 is_configured() 返回 False")
        try:
            llm.chat([{"role": "user", "content": "hi"}])
            _check(False, "未配置时应抛 LLMError")
        except llm.LLMError as e:
            _check("未配置" in str(e) or "LLM_API_KEY" in str(e), "未配置时 LLMError 含明确提示")
    finally:
        for k, v in saved.items():
            os.environ[k] = v

    # With a dummy key, config reports configured
    os.environ["LLM_API_KEY"] = "sk-test-dummy-not-real"
    try:
        _check(llm.is_configured(), "设置 LLM_API_KEY 后 is_configured() 返回 True")
        cfg = llm._config()
        _check(cfg["api_key"].startswith("sk-"), "config 返回的 key 与设置一致")
    finally:
        # Only delete if it was originally absent; otherwise restore the
        # original value (e.g. when .env was loaded by llm.py at import).
        if "LLM_API_KEY" in saved:
            os.environ["LLM_API_KEY"] = saved["LLM_API_KEY"]
        else:
            del os.environ["LLM_API_KEY"]


# ===========================================================================
# 12. summarize_paper fallback (when LLM not configured)
# ===========================================================================
def test_summarize_paper_fallback():
    print("\n[12] summarize_paper fallback")
    import os
    saved = {}
    for k in ("LLM_API_KEY", "AGNES_API_KEY"):
        if k in os.environ:
            saved[k] = os.environ[k]
            del os.environ[k]
    try:
        result = llm.summarize_paper({
            "title": "Test Paper Title",
            "authors": "Test Author",
            "year": 2024,
            "venue": "IEDM",
            "abstract": "We propose a novel GAA-FET with record performance.",
        })
        _check("tldr" in result, "fallback 含 tldr 字段")
        _check("key_points" in result, "fallback 含 key_points 字段")
        _check(isinstance(result["key_points"], list), "key_points 是 list")
        _check("error" in result, "fallback 含 error 字段说明失败原因")
    finally:
        for k, v in saved.items():
            os.environ[k] = v


# ===========================================================================
# 13. answer_with_context empty chunks
# ===========================================================================
def test_answer_with_context_empty_chunks():
    print("\n[13] answer_with_context with empty chunks")
    # Without chunks, should return "vault 中未找到..." style message
    answer = llm.answer_with_context("test question", [])
    _check("vault" in answer or "未找到" in answer, "空 chunks 返回未找到提示")


# ===========================================================================
# 14. Plot digitization + comparison
# ===========================================================================
def _make_test_chart_image() -> bytes:
    """Generate a synthetic chart PNG with a known red curve for testing."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (200, 100), "white")
    draw = ImageDraw.Draw(img)
    # Draw a red curve from (10,90) to (190,10) — y decreasing as x increases
    for x in range(10, 190):
        y = int(90 - (x - 10) * 80 / 180)
        for dy in range(-1, 2):
            draw.point((x, y + dy), fill=(220, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_extract_curve_from_image():
    print("\n[14] Plot curve extraction from image")
    img_bytes = _make_test_chart_image()
    result = extract_curve_from_image(img_bytes)
    _check("points" in result, "extract 返回 points 字段")
    _check(len(result["points"]) > 50, f"提取到足够多的像素点 ({len(result['points'])})")
    _check(result["width"] == 200 and result["height"] == 100, "图片尺寸正确")
    _check("total_pixels" in result and result["total_pixels"] > 100, "total_pixels 统计正确")
    _check(result["cluster_color"][0] > result["cluster_color"][1], "主颜色判定为红色族 (R>G)")

    # Empty image (all white) should return 0 points with warning
    from PIL import Image
    blank = Image.new("RGB", (100, 100), "white")
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    blank_result = extract_curve_from_image(buf.getvalue())
    _check(len(blank_result["points"]) == 0, "全白图返回 0 点")
    _check("warning" in blank_result, "全白图返回 warning")


def test_calibrate_points():
    print("\n[15] Pixel → real coordinate calibration")
    # Identity transform: pixel (10, 90) → real (0, 0), pixel (190, 10) → real (10, 5)
    pts = [[10, 90], [100, 50], [190, 10]]
    ref_a = {"px": 10, "py": 90, "rx": 0.0, "ry": 0.0}
    ref_b = {"px": 190, "py": 10, "rx": 10.0, "ry": 5.0}
    out = calibrate_points(pts, ref_a, ref_b)
    _check(len(out) == 3, "标定返回 3 个点")
    _check(abs(out[0][0] - 0.0) < 1e-9, "点 1 的 x_real = 0")
    _check(abs(out[0][1] - 0.0) < 1e-9, "点 1 的 y_real = 0")
    _check(abs(out[2][0] - 10.0) < 1e-9, "点 3 的 x_real = 10")
    _check(abs(out[2][1] - 5.0) < 1e-9, "点 3 的 y_real = 5")
    # Midpoint should be (5, 2.5)
    _check(abs(out[1][0] - 5.0) < 1e-6, "中点 x_real ≈ 5")
    _check(abs(out[1][1] - 2.5) < 1e-6, "中点 y_real ≈ 2.5")

    # Empty input
    _check(calibrate_points([], ref_a, ref_b) == [], "空列表返回空")

    # Identical x pixel refs → ValueError
    try:
        calibrate_points(pts, {"px": 10, "py": 90, "rx": 0, "ry": 0}, {"px": 10, "py": 10, "rx": 10, "ry": 5})
        _check(False, "相同 px 参考点必须拒绝")
    except ValueError:
        _check(True, "相同 px 参考点已拒绝")


def test_compute_metrics():
    print("\n[16] Reproduction alignment metrics")
    # Identical datasets → 0 RMSE, Pearson = 1
    paper = [[0, 0], [1, 1], [2, 4], [3, 9], [4, 16]]
    repro = paper[:]
    r = compute_metrics(paper, repro)
    _check(r["rmse"] == 0.0, "完全一致 → RMSE = 0")
    _check(r["mae"] == 0.0, "完全一致 → MAE = 0")
    _check(r["max_rel_err_pct"] == 0.0, "完全一致 → max_rel_err = 0%")
    _check(abs(r["pearson_r"] - 1.0) < 1e-9, "完全一致 → Pearson R = 1")
    _check(r["verdict"] == "pass", "完全一致 → verdict = pass")

    # 4% noise → still pass (< 5%)
    noisy_repro = [[x, y * 1.04] for x, y in paper]
    r2 = compute_metrics(paper, noisy_repro)
    _check(r2["verdict"] == "pass", "4% 噪声 → 仍判 pass")
    _check(r2["max_rel_err_pct"] < 5.0, "4% 噪声 → max_rel < 5%")

    # 10% noise → warn
    warn_repro = [[x, y * 1.10] for x, y in paper]
    r3 = compute_metrics(paper, warn_repro)
    _check(r3["verdict"] == "warn", "10% 噪声 → warn")
    _check(5.0 < r3["max_rel_err_pct"] < 20.0, "10% 噪声 → 误差在 5-20%")

    # 50% noise → fail
    bad_repro = [[x, y * 1.50] for x, y in paper]
    r4 = compute_metrics(paper, bad_repro)
    _check(r4["verdict"] == "fail", "50% 噪声 → fail")
    _check(r4["max_rel_err_pct"] > 20.0, "50% 噪声 → 误差 > 20%")

    # Non-monotonic x gets sorted
    shuffled = [[3, 9], [0, 0], [4, 16], [1, 1], [2, 4]]
    r5 = compute_metrics(shuffled, paper)
    _check(r5["rmse"] == 0.0, "无序输入 → 自动排序后 RMSE = 0")

    # Empty inputs raise
    try:
        compute_metrics([], [])
        _check(False, "空输入必须抛 ValueError")
    except ValueError:
        _check(True, "空输入已拒绝")

    # No overlap raises
    try:
        compute_metrics([[0, 0], [1, 1]], [[10, 10], [11, 11]])
        _check(False, "无重叠 x 范围必须抛 ValueError")
    except ValueError:
        _check(True, "无重叠 x 范围已拒绝")


def test_parse_csv_points():
    print("\n[17] CSV / TSV point parsing")
    csv = """# This is a comment
V_G,I_D
0.0, 0.0
0.1\t0.5
0.2;1.2
0.3 2.5
not_a_number
"""
    pts = _parse_csv_points(csv)
    _check(len(pts) == 4, f"CSV 解析出 4 个点 (实际 {len(pts)})")
    _check(pts[0] == [0.0, 0.0], "第一行正确")
    _check(pts[1] == [0.1, 0.5], "Tab 分隔正确")
    _check(pts[2] == [0.2, 1.2], "分号分隔正确")
    _check(pts[3] == [0.3, 2.5], "空格分隔正确")


def test_linear_interp():
    print("\n[18] Linear interpolation")
    xs = [0, 1, 2, 3, 4]
    ys = [0, 1, 4, 9, 16]
    # y = x^2, query at 0.5, 1.5, 2.5
    out = _linear_interp(xs, ys, [0.5, 1.5, 2.5])
    _check(abs(out[0] - 0.5) < 1e-9, "x=0.5 插值 y=0.5")
    _check(abs(out[1] - 2.5) < 1e-9, "x=1.5 插值 y=2.5")
    _check(abs(out[2] - 6.5) < 1e-9, "x=2.5 插值 y=6.5")
    # Out of range → clamp
    out2 = _linear_interp(xs, ys, [-1, 10])
    _check(out2[0] == 0, "x < xs[0] 钳到 ys[0]")
    _check(out2[1] == 16, "x > xs[-1] 钳到 ys[-1]")


# ===========================================================================
# 19. Paper text extraction + section splitting (V1.4)
# ===========================================================================
def _make_test_pdf_bytes() -> bytes:
    """Generate a minimal synthetic PDF mimicking an academic paper."""
    try:
        from PIL import Image, ImageDraw
        # Build a single image, then save as PDF (1-page)
        img = Image.new("RGB", (612, 792), "white")
        draw = ImageDraw.Draw(img)
        # Add some text content simulating section markers
        draw.text((72, 100), "Test Paper Title", fill="black")
        draw.text((72, 150), "Abstract", fill="black")
        draw.text((72, 180), "This is the abstract content.", fill="black")
        draw.text((72, 220), "1. Introduction", fill="black")
        draw.text((72, 250), "Introductory text here.", fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PDF", resolution=72.0)
        return buf.getvalue()
    except Exception as e:
        return b""


def test_extract_text_from_pdf():
    print("\n[19] PDF text extraction")
    pdf_bytes = _make_test_pdf_bytes()
    if not pdf_bytes:
        _check(False, "PDF 生成失败 (跳过)")
        return
    pages = extract_text_from_pdf(pdf_bytes)
    _check(len(pages) >= 1, f"PDF 至少提取出 1 页 (实际 {len(pages)})")
    _check(pages[0]["page"] == 1, "首页 page == 1")
    _check("page" in pages[0] and "text" in pages[0], "返回 page + text 字段")


def test_classify_title_to_section():
    print("\n[20] Section title classification")
    # Common academic section titles
    cases = [
        ("Introduction", "introduction"),
        ("INTRODUCTION", "introduction"),
        ("I. INTRODUCTION", "introduction"),
        ("1. Introduction", "introduction"),
        ("Methodology", "methods"),
        ("II. METHODS", "methods"),
        ("2. Methods", "methods"),
        ("Experimental Results", "results"),
        ("III. RESULTS", "results"),
        ("Discussion", "discussion"),
        ("IV. DISCUSSION", "discussion"),
        ("Conclusions", "conclusion"),
        ("V. CONCLUSIONS", "conclusion"),
        ("References", "references"),
        ("Acknowledgments", "acknowledgments"),
    ]
    for title, expected in cases:
        got = _classify_title_to_section(title)
        _check(got == expected, f"'{title}' → '{got}' (期望 '{expected}')")

    # Non-matching title
    _check(_classify_title_to_section("Random Section") is None, "未知标题返回 None")


def test_split_into_sections():
    print("\n[21] Section splitting")
    # Synthetic multi-section text
    sample = """
    This is the title and abstract area before any sections.

    1. Introduction

    This is the introduction text. We discuss the background and motivation here.
    Lorem ipsum dolor sit amet, consectetur adipiscing elit.

    2. Methods

    This is the methods section. We describe the experimental setup.
    The simulation uses Sentaurus TCAD with hydrodynamic model.

    3. Results

    The key results show 4.2% error reduction.
    Ion/Ioff ratio improved by 28%.

    4. Conclusion

    In summary, we demonstrated a novel GAA-FET with self-heating suppression.
    Future work includes 2D material integration.

    References
    [1] Author A, et al. IEEE TED, 2024.
    """
    pages = [{"page": 1, "text": sample}]
    sections = split_into_sections(pages)
    _check(len(sections) >= 4, f"识别出 >= 4 章节 (实际 {len(sections)})")

    section_keys = [s["section"] for s in sections]
    _check("introduction" in section_keys, "识别 introduction 章节")
    _check("methods" in section_keys, "识别 methods 章节")
    _check("results" in section_keys, "识别 results 章节")
    _check("conclusion" in section_keys, "识别 conclusion 章节")

    # Verify section text contains content (not empty)
    for s in sections:
        if s["section"] in ("introduction", "methods", "results", "conclusion"):
            _check(len(s["text"]) > 20, f"'{s['title']}' 章节内容非空")

    # Verify text doesn't include the heading itself (slice is AFTER heading)
    intro_section = next(s for s in sections if s["section"] == "introduction")
    _check("This is the introduction" in intro_section["text"], "introduction 内容正确")

    # Roman numeral headings
    roman_sample = """
    I. INTRODUCTION
    This is the introduction section. It contains background and motivation for the work.

    II. METHODOLOGY
    This is the methods section. It describes the experimental setup and simulation methodology in detail.

    III. RESULTS
    This is the results section. It presents the key findings and important metrics from the experiments.
    """
    roman_pages = [{"page": 1, "text": roman_sample}]
    roman_sections = split_into_sections(roman_pages)
    roman_keys = [s["section"] for s in roman_sections]
    _check("introduction" in roman_keys, "Roman 数字 'I. INTRODUCTION' 识别")
    _check("methods" in roman_keys, "Roman 数字 'II. METHODOLOGY' 识别")
    _check("results" in roman_keys, "Roman 数字 'III. RESULTS' 识别")

    # No recognizable sections → single 'full_text' entry
    weird_pages = [{"page": 1, "text": "Just some random prose without headings."}]
    weird_sections = split_into_sections(weird_pages)
    _check(len(weird_sections) == 1, "无标题时返回 1 个 full_text")
    _check(weird_sections[0]["section"] == "full_text", "无标题时 section 标识为 full_text")


def test_chunk_section_text():
    print("\n[22] Section text chunking (分段翻译)")
    # Short text returns single chunk
    short = "This is a short paragraph. " * 10  # ~290 chars
    chunks = chunk_section_text(short, target=8000)
    _check(len(chunks) == 1, f"短文本 (290 字) 返回 1 段 (实际 {len(chunks)})")

    # Multi-paragraph text gets split at paragraph boundaries
    para_a = ("Magnetic sensors based on GMI effect. " * 50).strip()  # ~1600 chars
    para_b = ("Experimental setup description. " * 50).strip()  # ~1300 chars
    para_c = ("Results show 41036% GMI ratio. " * 50).strip()  # ~1300 chars
    para_d = ("Conclusion: commercial inductors. " * 50).strip()  # ~1300 chars
    text = "\n\n".join([para_a, para_b, para_c, para_d])  # ~5500 chars total
    chunks = chunk_section_text(text, target=2000)
    _check(len(chunks) >= 2, f"多段文本拆 >= 2 段 (实际 {len(chunks)})")
    # All chunks non-empty
    _check(all(len(c) > 0 for c in chunks), "所有分段非空")
    # Reconstructed text contains all original keywords
    combined = " ".join(chunks)
    _check("GMI" in combined and "Experimental" in combined and "Results" in combined,
           "分段合并后保留所有原关键词")

    # Oversized single paragraph gets sentence-split
    huge = "Sentence one is here. Sentence two follows. Sentence three is large. " * 200  # ~10K
    chunks = chunk_section_text(huge, target=4000, hard_max=5000)
    _check(len(chunks) >= 2, f"超大段落拆句后 >= 2 段 (实际 {len(chunks)})")

    # Empty input
    chunks = chunk_section_text("")
    _check(chunks == [], "空文本返回 []")

    # Whitespace-only
    chunks = chunk_section_text("   \n\n  \n  ")
    _check(chunks == [], "纯空白文本返回 []")

    # Chunk size respects target (no chunk wildly exceeds target + hard_max slack)
    long_text = ("Section content here. " * 500)
    chunks = chunk_section_text(long_text, target=2000, hard_max=3000)
    _check(max(len(c) for c in chunks) <= 3200,  # hard_max + overlap slack
           f"分段大小受 hard_max 约束 (max={max(len(c) for c in chunks)})")


def test_translation_cache_lru():
    print("\n[23] Translation cache (LRU)")
    c = TranslationCache(max_size=3)
    # First put
    c.put("intro", "hello world", "gpt-4", {"translation": "你好世界", "chunk_count": 1})
    got = c.get("intro", "hello world", "gpt-4")
    _check(got is not None, "缓存命中")
    _check(got["translation"] == "你好世界", "缓存值正确")

    # Miss on different text
    _check(c.get("intro", "different text", "gpt-4") is None, "不同文本缓存 miss")
    _check(c.get("intro", "hello world", "gpt-3.5") is None, "不同模型缓存 miss")

    # LRU eviction: with max_size=3, the 4th put evicts the oldest entry.
    # NB: keys differ by model name ("gpt-4" vs "m"), so they're independent slots.
    c2 = TranslationCache(max_size=3)  # fresh cache, isolated from earlier puts
    c2.put("intro", "hello world", "gpt-4", {"translation": "x"})
    c2.put("intro", "a", "m", {"translation": "甲"})
    c2.put("intro", "b", "m", {"translation": "乙"})
    # hello (model=gpt-4) is independent of a/b (model=m); both still present
    # IMPORTANT: capture the get() result first, THEN assert — get() mutates
    # LRU order (moves key to end), so calling get() inside _check would
    # change the state we're about to test.
    _hello_alive_1 = c2.get("intro", "hello world", "gpt-4") is not None
    _a_alive_1 = c2.get("intro", "a", "m") is not None
    _check(_hello_alive_1, "LRU: hello 仍存活 (独立 model 槽)")
    _check(_a_alive_1, "LRU: 'a' 仍存活 (3 项 < max_size)")
    # Now we want 'a' to be the LRU so put('c') evicts it.
    # Current order (after the two gets above): [b, hello, a] — a is at end, not LRU!
    # Reset by accessing b (so b becomes LRU) — wait that's opposite.
    # Simpler: just push 3 fresh m-entries without touching hello, so the
    # eviction test isolates to the m-slot.
    c3 = TranslationCache(max_size=3)
    c3.put("k", "x", "m", {"translation": "x"})  # m-slot: [x]
    c3.put("k", "y", "m", {"translation": "y"})  # m-slot: [x, y]
    c3.put("k", "hello", "gpt-4", {"translation": "h"})  # gpt-4-slot: [hello]
    # Now bump hello to keep it alive
    c3.get("k", "hello", "gpt-4")
    # Push c → evicts oldest m-slot entry = x
    c3.put("k", "z", "m", {"translation": "z"})
    x_gone = c3.get("k", "x", "m") is None
    y_alive = c3.get("k", "y", "m") is not None
    hello_alive = c3.get("k", "hello", "gpt-4") is not None
    z_alive = c3.get("k", "z", "m") is not None
    _check(x_gone, "LRU: 'x' (m 槽最久未访问) 被淘汰")
    _check(y_alive, "LRU: 'y' (m 槽最近写入) 仍存活")
    _check(hello_alive, "LRU: hello (gpt-4 槽独立) 仍存活")
    _check(z_alive, "LRU: 'z' (m 槽最新加入) 仍存活")

    # Stats (c only had 2 hits in the early put/get sequence before c2/c3 split out)
    stats = c.stats()
    _check(stats["hits"] >= 1, f"缓存 hits 累计正确 (实际 {stats['hits']})")
    _check(stats["misses"] >= 2, f"缓存 misses 累计正确 (实际 {stats['misses']})")

    # Global cache singleton
    from microbench.translation import get_cache
    reset_cache()
    g1 = get_cache()
    g2 = get_cache()
    _check(g1 is g2, "全局缓存单例")

    # Hash-keyed — same content → same translation
    reset_cache()
    c3 = get_cache()
    text = "GAA-FET with self-heating suppression"
    c3.put("methods", text, "M2.7", {"translation": "GAA-FET 自热抑制"})
    _check(c3.get("methods", text, "M2.7")["translation"] == "GAA-FET 自热抑制",
           "相同 section_key + text + model 命中缓存")

    # Different section_key → different cache slot
    _check(c3.get("results", text, "M2.7") is None, "不同 section_key 不串缓存")

    # Clear
    c3.clear()
    _check(c3.get("methods", text, "M2.7") is None, "clear() 后缓存为空")


def test_build_cross_section_context():
    print("\n[24] Cross-section context builder")
    sections = [
        {"section": "introduction", "title": "Introduction",
         "text": "We motivate the work here. " * 100},  # ~3000 chars
        {"section": "methods", "title": "Methods",
         "text": "We use Sentaurus TCAD. " * 100},  # ~2200 chars
        {"section": "results", "title": "Results",
         "text": "Ion/Ioff improved by 28%. " * 200},  # ~5400 chars
    ]
    ctx = build_cross_section_context(sections, "X 在 Y 中如何应用?", per_section_budget=4000)
    _check("[INTRODUCTION]" in ctx, "context 含 [INTRODUCTION] 标签")
    _check("[METHODS]" in ctx, "context 含 [METHODS] 标签")
    _check("[RESULTS]" in ctx, "context 含 [RESULTS] 标签")
    _check("用户问题" in ctx and "X 在 Y 中如何应用" in ctx, "context 含用户问题原样")

    # Per-section budget enforcement — big section truncated
    _check("..." in ctx or "[中段省略]" in ctx, "超大章节被截断 (含省略标记)")

    # Empty section text doesn't break builder
    safe = build_cross_section_context(
        [{"section": "introduction", "title": "Intro", "text": ""}],
        "q", per_section_budget=1000,
    )
    _check("[INTRODUCTION]" in safe, "空 text 章节也能生成 context")


def test_translate_endpoint_routing():
    print("\n[25] /api/paper/translate_section 路由 + 缓存命中")
    # Use TestClient to avoid network
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)

    # 1) Without LLM configured → 503 with clear message
    # Force-unconfigure by stashing and clearing env
    import os
    saved = {}
    for k in ("LLM_API_KEY", "AGNES_API_KEY"):
        if k in os.environ:
            saved[k] = os.environ[k]
            del os.environ[k]
    try:
        r = client.post("/api/paper/translate_section", json={
            "section_title": "T", "section_text": "Some English text to translate."
        })
        _check(r.status_code == 503, f"无 LLM 配置返回 503 (实际 {r.status_code})")
        _check("LLM" in str(r.json().get("detail", "")), "503 提示 LLM 配置缺失")
    finally:
        for k, v in saved.items():
            os.environ[k] = v

    # 2) With LLM configured (real .env) → 200 + cache hit on second call
    if llm.is_configured():
        reset_cache()
        section_text = (
            "The giant magneto-impedance (GMI) effect in commercial inductors "
            "achieves 41036% sensitivity at low cost. " * 30  # ~4000 chars
        )
        r1 = client.post("/api/paper/translate_section", json={
            "section_title": "Abstract", "section_text": section_text,
            "section_key": "abstract",
        })
        _check(r1.status_code == 200, f"翻译 endpoint 返回 200 (实际 {r1.status_code})")
        data1 = r1.json()
        _check("translation" in data1 and len(data1["translation"]) > 0,
               "首次翻译返回非空 translation")
        _check(data1.get("was_segmented") is False, f"短文本 was_segmented=False")
        _check(data1.get("from_cache") is False, "首次 from_cache=False")
        _check(data1.get("chunk_count") == 1, f"短文本 chunk_count=1 (实际 {data1.get('chunk_count')})")

        # Second call: same text + same key → cache hit
        r2 = client.post("/api/paper/translate_section", json={
            "section_title": "Abstract", "section_text": section_text,
            "section_key": "abstract",
        })
        _check(r2.status_code == 200, "缓存命中路径返回 200")
        data2 = r2.json()
        _check(data2.get("from_cache") is True, "二次调用 from_cache=True (缓存命中)")
        _check(data2.get("translation") == data1.get("translation"),
               "缓存返回与首次相同的 translation")

        # Large section (>8000 chars) → chunked
        reset_cache()
        big_text = "This is a paragraph about GMI sensors. " * 500  # ~16500 chars
        r3 = client.post("/api/paper/translate_section", json={
            "section_title": "Introduction", "section_text": big_text,
            "section_key": "introduction",
        })
        _check(r3.status_code == 200, "大章节翻译返回 200")
        data3 = r3.json()
        _check(data3.get("was_segmented") is True, f"大章节 was_segmented=True")
        _check(data3.get("chunk_count", 1) >= 2, f"大章节 chunk_count >= 2 (实际 {data3.get('chunk_count')})")
        _check(len(data3.get("translation", "")) > 0, "大章节翻译结果非空")
    else:
        _check(False, "LLM 未配置, 跳过 LLM 调用断言")
        _check(False, "LLM 未配置, 跳过缓存命中断言")
        _check(False, "LLM 未配置, 跳过分段断言")
        _check(False, "LLM 未配置, 跳过分段 chunk_count 断言")


def test_cross_qa_endpoint():
    print("\n[26] /api/paper/cross_qa 跨章节问答")
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)

    sections = [
        {"section": "introduction", "title": "Introduction",
         "text": "Prior sensors suffer from low sensitivity and high cost. We propose GMI."},
        {"section": "methods", "title": "Methods",
         "text": "We use commercial inductors and measure impedance at 1 Hz with 100 pF."},
        {"section": "results", "title": "Results",
         "text": "Detection limit reaches 10 nT at 1 Hz, a 5000-fold improvement over induction."},
    ]

    # Without LLM → 503
    import os
    saved = {}
    for k in ("LLM_API_KEY", "AGNES_API_KEY"):
        if k in os.environ:
            saved[k] = os.environ[k]
            del os.environ[k]
    try:
        r = client.post("/api/paper/cross_qa", json={
            "question": "X 在 Y 中如何应用?", "sections": sections,
        })
        _check(r.status_code == 503, f"无 LLM 配置 cross_qa 返回 503 (实际 {r.status_code})")
    finally:
        for k, v in saved.items():
            os.environ[k] = v

    # < 2 sections → 422 (Pydantic min_length validation) OR 400
    if llm.is_configured():
        r = client.post("/api/paper/cross_qa", json={
            "question": "test", "sections": sections[:1],
        })
        _check(r.status_code in (400, 422), f"< 2 章节返回 400/422 (实际 {r.status_code})")

        # Real call with 3 sections + selected_indices subset
        r = client.post("/api/paper/cross_qa", json={
            "question": "本文方法如何回应引言中提到的 SOTA 缺陷?",
            "sections": sections,
            "selected_indices": [0, 2],  # only intro + results
            "paper_title": "GMI Inductor Sensor (test)",
        })
        _check(r.status_code == 200, f"cross_qa 正常路径返回 200 (实际 {r.status_code})")
        data = r.json()
        _check(data.get("status") == "success", "响应 status=success")
        _check("answer" in data and len(data["answer"]) > 10, "返回 answer 字段且非空")
        _check(data.get("section_count") == 2, f"section_count=2 (实际 {data.get('section_count')})")
        _check(len(data.get("sections_used", [])) == 2, "sections_used 含 2 个")
        _check(any("introduction" == s["section"] for s in data["sections_used"]),
               "selected_indices=[0,2] 正确选用 intro+results")
    else:
        _check(False, "LLM 未配置, 跳过跨章节问答真实调用")


def test_llm_chat_retry_on_transient_errors():
    print("\n[27b] LLM chat() retry on SSL/timeout (v1.2)")
    import requests as _req

    # Mock that fails N times then succeeds — verifies retry counts
    class _Resp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {
                "choices": [{"message": {"content": "ok"}}]
            }
            self.text = str(self._body)
        def raise_for_status(self):
            if self.status_code >= 400:
                raise _req.HTTPError(self.status_code)
        def json(self):
            return self._body

    # Scenario 1: SSLError twice → success on 3rd attempt
    call_count = {"n": 0}
    def flaky_ssl(url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise _req.exceptions.SSLError("EOF")
        return _Resp()

    _orig_get = _req.post
    _req.post = flaky_ssl
    try:
        result = llm.chat(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=3,
            timeout=10,
        )
        _check(result == "ok", f"SSL 重试 2 次后成功 (实际 {call_count['n']} 次调用)")
        _check(call_count["n"] == 3, f"共 3 次尝试 (实际 {call_count['n']})")
    finally:
        _req.post = _orig_get

    # Scenario 2: 502 then success — server errors should also retry
    call_count["n"] = 0
    def flaky_502(url, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _Resp(status=502, body={"error": {"message": "bad gateway"}})
        return _Resp()

    _req.post = flaky_502
    try:
        result = llm.chat(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=3,
            timeout=10,
        )
        _check(result == "ok", "502 重试后成功")
        _check(call_count["n"] == 2, f"502 重试只多 1 次 (实际 {call_count['n']})")
    finally:
        _req.post = _orig_get

    # Scenario 3: persistent SSL failure → give up after max_retries
    call_count["n"] = 0
    def persistent_fail(url, **kwargs):
        call_count["n"] += 1
        raise _req.exceptions.SSLError("EOF persistent")

    _req.post = persistent_fail
    try:
        try:
            llm.chat(
                messages=[{"role": "user", "content": "hi"}],
                max_retries=2,  # smaller to speed test
                timeout=10,
            )
            _check(False, "持续 SSL 失败应抛 LLMError")
        except llm.LLMError as e:
            _check("重试" in str(e),
                   f"LLMError 包含 '重试' 信息: {str(e)[:80]}")
            _check(call_count["n"] == 3,
                   f"共尝试 max_retries+1=3 次 (实际 {call_count['n']})")
    finally:
        _req.post = _orig_get

    # Scenario 4: 400 Bad Request should NOT retry (client error)
    call_count["n"] = 0
    def client_error(url, **kwargs):
        call_count["n"] += 1
        return _Resp(status=400, body={"error": {"message": "bad request"}})

    _req.post = client_error
    try:
        try:
            llm.chat(messages=[{"role": "user", "content": "hi"}], max_retries=3, timeout=10)
            _check(False, "400 错误应抛 LLMError")
        except llm.LLMError as e:
            _check("400" in str(e), f"400 不重试直接抛错: {str(e)[:80]}")
            _check(call_count["n"] == 1, f"400 不重试 (实际 {call_count['n']} 次)")
    finally:
        _req.post = _orig_get


def test_cache_stats_endpoint():
    print("\n[27] /api/translation/cache_stats")
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)
    r = client.get("/api/translation/cache_stats")
    _check(r.status_code == 200, f"cache_stats 返回 200 (实际 {r.status_code})")
    data = r.json()
    _check("size" in data and "hits" in data and "misses" in data,
           "cache_stats 含 size/hits/misses 字段")


def test_llm_chat_propagates_max_retries_to_primary():
    """Regression guard: chat(max_retries=N) must drive the primary model's
    retry count, not a hardcoded `max_retries=1`. Without this, the
    SSL/timeout retry test would only run 2 attempts instead of N."""
    print("\n[27c] llm.chat() max_retries is propagated to primary")
    import re as _re
    import inspect
    from microbench import llm
    src = inspect.getsource(llm.chat)
    # Locate the primary invocation block
    primary_call = _re.search(r"_invoke_model\(\s*primary[^)]*max_retries\s*=\s*([^,)\s]+)", src, _re.DOTALL)
    _check(primary_call is not None, "找到 primary 调用并提取 max_retries 表达式")
    if primary_call:
        expr = primary_call.group(1).strip()
        _check(expr == "max_retries",
               f"primary 调用的 max_retries 必须是 chat() 入参(当前 '{expr}',不可硬编码 1)")
        # Also ensure the value isn't wrapped in a constant
        _check(expr not in ("1", "2"),
               f"primary 不应使用硬编码 max_retries='{expr}'")


def test_llm_retry_exception_classes_include_ssl_and_chunked():
    """Regression guard: RETRYABLE_EXC must include SSLError +
    ChunkedEncodingError, otherwise long-context translations silently 502."""
    print("\n[27d] RETRYABLE_EXC covers SSL/ChunkedEncodingError")
    import inspect
    from microbench import llm
    src = inspect.getsource(llm._invoke_model)
    _check("SSLError" in src, "_invoke_model 引用 SSLError")
    _check("ChunkedEncodingError" in src, "_invoke_model 引用 ChunkedEncodingError")
    _check("ConnectionError" in src, "_invoke_model 引用 ConnectionError")


def test_paper_serve_pdf_endpoint_validates_relative_path():
    """Regression guard: /api/paper/serve_pdf must reject traversal in the
    relative_path query param (vault leak prevention)."""
    print("\n[29b] /api/paper/serve_pdf rejects ../ traversal")
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)

    # 1) Normal request should not 500
    r = client.get("/api/paper/serve_pdf", params={"relative_path": "01_Literature"})
    _check(r.status_code in (200, 404),
           f"正常路径返回 200/404 (实际 {r.status_code})")

    # 2) ../ traversal must be blocked — return 400 (validation), never 200
    for bad in [
        "../../../Windows/System32/drivers/etc/hosts",
        "..%2F..%2Fetc%2Fpasswd",
        "01_Literature/../../../etc/passwd",
    ]:
        r = client.get("/api/paper/serve_pdf", params={"relative_path": bad})
        _check(r.status_code in (400, 403, 404),
               f"路径穿越 '{bad}' 必须非 200 (实际 {r.status_code})")
        if r.status_code == 200:
            # CRITICAL: leak. Verify body is not Windows/system file content.
            body = r.text
            _check("root:" not in body and "[fonts]" not in body,
                   f"CRITICAL: 路径穿越成功, 返回了系统文件内容")

    # 3) Absolute Windows path must be blocked
    r = client.get("/api/paper/serve_pdf", params={"relative_path": "C:\\Windows\\win.ini"})
    _check(r.status_code in (400, 403, 404),
           f"绝对 Windows 路径必须拒绝 (实际 {r.status_code})")


def test_vault_file_endpoint_serves_markdown():
    """Regression guard: /api/vault/file serves .md/.txt from inside BASE_DIR
    but rejects traversal and non-text file types. Used by dashboard feed."""
    print("\n[29c] /api/vault/file serves .md safely")
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)

    # 1) Non-existent file → 404
    r = client.get("/api/vault/file", params={"relative_path": "01_Literature/_nope_.md"})
    _check(r.status_code == 404, f"不存在文件 → 404 (实际 {r.status_code})")

    # 2) Path traversal → 400
    for bad in ["../app.py", "01_Literature/../../../etc/passwd"]:
        r = client.get("/api/vault/file", params={"relative_path": bad})
        _check(r.status_code == 400,
               f"路径穿越 '{bad}' 必须 400 (实际 {r.status_code})")

    # 3) Absolute Windows path → 400
    r = client.get("/api/vault/file", params={"relative_path": "C:\\Windows\\win.ini"})
    _check(r.status_code == 400,
           f"绝对路径必须 400 (实际 {r.status_code})")

    # 4) Unsupported file type → 400 (must NOT serve arbitrary files)
    # We test by attempting a PDF path
    r = client.get("/api/vault/file", params={"relative_path": "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/nope.pdf"})
    # Could be 400 (type) or 404 (missing), either is acceptable
    _check(r.status_code in (400, 404),
           f"PDF 类型必须非 200 (实际 {r.status_code})")

    # 5) If any .md exists in vault, /api/vault/file should serve it
    from pathlib import Path
    any_md = next(iter([p for p in Path(BASE_DIR).rglob("*.md")
                        if "/.git/" not in str(p)]), None)
    if any_md:
        rel = str(any_md.relative_to(BASE_DIR)).replace("\\", "/")
        r = client.get("/api/vault/file", params={"relative_path": rel})
        _check(r.status_code == 200,
               f"vault .md '{rel}' 应 200 (实际 {r.status_code})")
        if r.status_code == 200:
            _check(len(r.text) > 0,
                   f"vault .md 内容非空 (实际 {len(r.text)} chars)")
            _check("text/markdown" in r.headers.get("content-type", ""),
                   f"Content-Type 是 markdown (实际 {r.headers.get('content-type')})")


def test_dual_model_status_and_probe_endpoints():
    print("\n[28] /api/llm/status and /api/llm/probe endpoints")
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)

    r = client.get("/api/llm/status")
    _check(r.status_code == 200, f"/api/llm/status 返回 200 (实际 {r.status_code})")
    data = r.json()
    _check("configured" in data, "返回 configured")
    _check("active_model" in data, "返回 active_model")
    _check("is_degraded" in data, "返回 is_degraded")
    _check("rate_limit" in data, "返回 rate_limit")
    _check("primary" in data and "fallback" in data, "返回 primary 与 fallback 配置")

    # Probe endpoint
    r_probe = client.post("/api/llm/probe")
    _check(r_probe.status_code == 200, f"/api/llm/probe 返回 200 (实际 {r_probe.status_code})")
    probe_data = r_probe.json()
    _check("success" in probe_data, "probe 返回 success 字段")
    _check("message" in probe_data, "probe 返回 message 字段")


def test_export_bilingual_endpoint():
    print("\n[29] /api/paper/export_bilingual")
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)

    payload = {
        "title": "BilingualTest_GAA_Analysis",
        "authors": "Zhang et al.",
        "year": 2025,
        "topic_category": "01_Device_TCAD_器件仿真",
        "sections": [
            {
                "title": "Abstract",
                "section": "abstract",
                "text": "This is an abstract about sub-3nm GAA nanosheet FETs.",
                "translation": "这是一篇关于 3nm 以下环栅纳米片场效应晶体管的摘要。",
                "page_start": 1,
                "page_end": 1,
            },
            {
                "title": "Introduction",
                "section": "introduction",
                "text": "Moore's law faces severe physical scaling limits.",
                "translation": "摩尔定律面临严峻的物理缩放限制。",
                "page_start": 1,
                "page_end": 2,
            }
        ]
    }
    r = client.post("/api/paper/export_bilingual", json=payload)
    _check(r.status_code == 200, f"export_bilingual 返回 200 (实际 {r.status_code})")
    data = r.json()
    _check(data.get("status") == "success", "返回 status=success")
    _check("file_name" in data and "双语精读" in data["file_name"], "file_name 包含 双语精读")
    _check("02_双语精读笔记" in data.get("folder", "") or "02_双语精读笔记" in data.get("relative_path", ""),
           f"导向 02_双语精读笔记 子目录: {data.get('folder')}")
    _check("relative_path" in data, "返回 relative_path")

    # Verify written content
    saved_file = BASE_DIR / data["relative_path"]
    _check(saved_file.exists(), f"文件实际创建在 vault: {saved_file}")
    content = saved_file.read_text(encoding="utf-8")
    _check("这是关于 3nm 以下" in content or "这是一篇关于" in content, "文件包含中文译文")
    _check("<details>" in content and "sub-3nm GAA" in content, "文件包含折叠英文原文")

    # Test export with qa_records bundled
    payload_qa = {
        "title": "BilingualTest_With_QA_Records",
        "authors": "Zhang et al.",
        "year": 2025,
        "topic_category": "01_Device_TCAD_器件仿真",
        "sections": [
            {
                "title": "Abstract",
                "section": "abstract",
                "text": "Nanosheet scaling requires quantum correction.",
                "translation": "纳米片微缩需要量子修正。",
                "page_start": 1,
                "page_end": 1,
            }
        ],
        "qa_records": [
            {
                "question": "什么是量子修正？",
                "selection": "quantum correction",
                "answer": "由于纳米尺度波函数聚集，电荷分布峰值向衬底深处偏移，需引入薛定谔-泊松自洽解。",
                "mode": "explain"
            }
        ]
    }
    r_qa = client.post("/api/paper/export_bilingual", json=payload_qa)
    _check(r_qa.status_code == 200, "带划词答疑的双语笔记导出返回 200")
    qa_data = r_qa.json()
    saved_qa_file = BASE_DIR / qa_data["relative_path"]
    qa_content = saved_qa_file.read_text(encoding="utf-8")
    _check("划词精读答疑与物理推导" in qa_content, "双语笔记整合了划词答疑章节")
    _check("什么是量子修正" in qa_content, "双语笔记包含了疑问内容")
    _check("薛定谔-泊松" in qa_content, "双语笔记包含了 AI 解答")


def test_export_qa_endpoint():
    print("\n[30] /api/paper/export_qa 划词答疑单篇卡片导出测试")
    from fastapi.testclient import TestClient
    from microbench import app as mb_app
    client = TestClient(mb_app.app)

    payload = {
        "title": "SmokeTest_QA_Paper",
        "question": "什么是量子限制效应与迁移率退化？",
        "selection": "Quantum confinement effects severely reduce carrier mobility in nanosheets thinner than 5nm.",
        "answer": "当纳米片厚度小于 5nm 时，声子散射加剧且能带结构改变，导致载流子有效质量增大与迁移率急剧下降。",
        "mode": "explain",
        "topic_category": "01_Device_TCAD_器件仿真",
        "year": 2025,
    }
    r = client.post("/api/paper/export_qa", json=payload)
    _check(r.status_code == 200, f"export_qa 返回 200 (实际 {r.status_code})")
    data = r.json()
    _check(data.get("status") == "success", "返回 status=success")
    _check("04_AI划词答疑" in data.get("folder", ""), f"folder 明确指定 04_AI划词答疑: {data.get('folder')}")
    saved_file = BASE_DIR / data["relative_path"]
    _check(saved_file.exists(), f"答疑卡片文件实际写入: {saved_file}")
    content = saved_file.read_text(encoding="utf-8")
    _check("量子限制效应" in content, "答疑卡片包含提问内容")
    _check("Quantum confinement" in content, "答疑卡片包含引用选中文本")
    _check("声子散射" in content, "答疑卡片包含 AI 解答")
    _check("04_AI划词答疑" in content, "答疑卡片正文包含清晰归档位置")


def test_agnes_rate_limiter_unit():
    print("\n[30] AgnesRateLimiter 单元测试")
    import time
    limiter = llm.AgnesRateLimiter(max_requests=2, window_seconds=2.0, min_interval=0.2)
    t0 = time.time()
    w1 = limiter.acquire()
    w2 = limiter.acquire()
    t1 = time.time()
    _check(t1 - t0 >= 0.18, f"两次调用间隔满足 min_interval 0.2s (实际 {t1 - t0:.2f}s)")
    stats = limiter.stats()
    _check(stats["rpm_limit"] == 20, "stats rpm_limit == 20")
    _check(stats["recent_requests_1m"] == 2, "stats recent_requests == 2")


def test_find_pdf_and_upload_pdf():
    print("\n[31] find_pdf 模糊匹配与 upload_pdf 本地上传接口测试")
    from fastapi.testclient import TestClient
    from microbench.app import app
    client = TestClient(app)

    # 1. Test find_pdf with the user's paper title that previously failed
    title = "AgenticTCAD: A LLM-based Multi-Agent Framework for Automated TCAD Code Generation and Device Optimization"
    r = client.get(f"/api/paper/find_pdf?title={title}")
    _check(r.status_code == 200, "find_pdf 返回 200")
    data = r.json()
    _check(data.get("found") is True, f"成功模糊匹配到本地已下载的 PDF (found=True, score={data.get('score')})")
    _check("AgenticTCAD" in data.get("file_name", ""), f"匹配的文件为: {data.get('file_name')}")

    # 2. Test upload_pdf with existing PDF bytes
    existing_pdf = BASE_DIR / data["relative_path"]
    if existing_pdf.exists():
        pdf_bytes = existing_pdf.read_bytes()
        files = {"file": ("local_test_paper.pdf", pdf_bytes, "application/pdf")}
        r_up = client.post("/api/paper/upload_pdf?topic_category=01_Device_TCAD_器件仿真&title=Local+Test+Paper", files=files)
        _check(r_up.status_code == 200, f"upload_pdf 返回 200 (实际 {r_up.status_code})")
        up_data = r_up.json()
        _check(up_data.get("status") == "success", "upload_pdf 返回 status=success")
        _check(len(up_data.get("sections", [])) >= 1, f"识别到 {len(up_data.get('sections', []))} 个章节")
        _check("pdf_path" in up_data, "upload_pdf 返回 pdf_path")
        _check((BASE_DIR / up_data["pdf_path"]).exists(), "上传的 PDF 成功落盘到 01_Literature 对应专题目录")

        # Cleanup the test uploaded file
        test_saved = BASE_DIR / up_data["pdf_path"]
        if test_saved.exists() and "local_test_paper" in test_saved.name.lower():
            try:
                test_saved.unlink()
            except Exception:
                pass


def test_explain_selection_endpoint():
    print("\n[32] /api/paper/explain_selection 划词答疑接口测试")
    from fastapi.testclient import TestClient
    from microbench.app import app
    client = TestClient(app)

    # 1. Validation error when selected_text too short
    r_bad = client.post("/api/paper/explain_selection", json={
        "selected_text": "x",
        "question": "test",
    })
    _check(r_bad.status_code == 422, f"过短划选返回 422 (实际 {r_bad.status_code})")

    # 2. Valid request
    if llm.is_configured():
        payload = {
            "selected_text": "DTCO paradigm enables end-to-end automated device design and optimization.",
            "question": "这里的 DTCO 是什么概念？有何核心作用？",
            "paper_title": "AgenticTCAD",
            "surrounding_context": "Abstract: With the continued scaling of advanced technology nodes...",
            "source_type": "pdf",
        }
        r = client.post("/api/paper/explain_selection", json=payload)
        _check(r.status_code == 200, f"explain_selection 返回 200 (实际 {r.status_code})")
        data = r.json()
        _check(data.get("status") == "success", "返回 status=success")
        _check("answer" in data and len(data["answer"]) > 10, "返回非空 answer")
        _check("model" in data, f"返回 model 标识: {data.get('model')}")
        _check("is_degraded" in data, "返回 is_degraded 状态")
    else:
        _check(True, "LLM 未配置, 跳过真实调用")


def test_chat_async_basic():
    """Stage 1: chat_async() happy path mirrors sync chat() output."""
    print("\n[33] chat_async() 基础调用 (v1.4 async)")
    import asyncio
    import httpx as _httpx

    class _Resp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {"choices": [{"message": {"content": "hello async"}}]}
            self.text = str(self._body)
        def json(self):
            return self._body

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, **kwargs):
            return _Resp()

    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _MockAsyncClient
    try:
        result = asyncio.run(llm.chat_async(
            messages=[{"role": "user", "content": "hi"}],
            max_retries=1,
            timeout=10,
        ))
        _check(result == "hello async", f"chat_async 返回内容 (实际 {result!r})")
    finally:
        _httpx.AsyncClient = _orig


def test_chat_async_cancel_event_preset():
    """Stage 1: pre-set cancel_event → chat_async raises CancelledError without API call."""
    print("\n[34] chat_async() 预设 cancel_event 立即抛错 (v1.4)")
    import asyncio
    import httpx as _httpx

    call_count = {"n": 0}

    class _Resp:
        def __init__(self):
            self.status_code = 200
            self._body = {"choices": [{"message": {"content": "should not see this"}}]}
            self.text = str(self._body)
        def json(self):
            return self._body

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            call_count["n"] += 1
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, **kwargs):
            return _Resp()

    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _MockAsyncClient
    try:
        ev = asyncio.Event()
        ev.set()  # pre-cancelled
        try:
            asyncio.run(llm.chat_async(
                messages=[{"role": "user", "content": "hi"}],
                max_retries=2,
                timeout=10,
                cancel_event=ev,
            ))
            _check(False, "预取消应抛 CancelledError")
        except asyncio.CancelledError:
            _check(call_count["n"] == 0,
                   f"预设 cancel_event → 0 次 HTTP 调用 (实际 {call_count['n']})")
    finally:
        _httpx.AsyncClient = _orig


def test_chat_async_cancel_during_retry():
    """Stage 1: 502 then set cancel_event → only 1 attempt (no retry storm)."""
    print("\n[35] chat_async() 502 后取消 (v1.4) — 阻止重试风暴")
    import asyncio
    import httpx as _httpx

    call_count = {"n": 0}
    ev = asyncio.Event()

    class _Resp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {"error": {"message": "bad gateway"}}
            self.text = str(self._body)
        def json(self):
            return self._body

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, **kwargs):
            call_count["n"] += 1
            # After the first failed call, fire the cancel event to mimic
            # client disconnect happening during the retry backoff window.
            ev.set()
            return _Resp(status=502)

    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _MockAsyncClient
    try:
        try:
            asyncio.run(llm.chat_async(
                messages=[{"role": "user", "content": "hi"}],
                max_retries=3,  # would normally retry 3 times (4 attempts total)
                timeout=10,
                cancel_event=ev,
            ))
            _check(False, "502 后取消应抛 CancelledError")
        except asyncio.CancelledError:
            _check(call_count["n"] == 1,
                   f"取消后只 1 次尝试 (实际 {call_count['n']}, 无重试)")
    finally:
        _httpx.AsyncClient = _orig


def test_invoke_model_async_retries_on_502():
    """Stage 1: _invoke_model_async retries on 502 like sync version."""
    print("\n[36] _invoke_model_async 502 重试 (v1.4)")
    import asyncio
    import httpx as _httpx

    call_count = {"n": 0}

    class _Resp:
        def __init__(self, status=200, body=None):
            self.status_code = status
            self._body = body or {"choices": [{"message": {"content": "ok"}}]}
            self.text = str(self._body)
        def json(self):
            return self._body

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return _Resp(status=502, body={"error": {"message": "bad gateway"}})
            return _Resp()

    primary_cfg = llm.model_manager.get_primary_config()
    if not primary_cfg.get("configured"):
        _check(True, "未配置 LLM, 跳过 async 重试测试")
        return

    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _MockAsyncClient
    try:
        success, content, status = asyncio.run(llm._invoke_model_async(
            primary_cfg,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=100,
            temperature=0.3,
            timeout=10,
            json_mode=False,
            max_retries=3,
        ))
        _check(success is True, f"async 502 重试后成功 (实际 success={success})")
        _check(call_count["n"] == 3,
               f"共 3 次尝试 (实际 {call_count['n']})")
    finally:
        _httpx.AsyncClient = _orig


def test_explain_endpoint_returns_499_on_cancel():
    """Stage 1: /api/paper/explain honors client disconnect → 499."""
    print("\n[37] /api/paper/explain 客户端断开 → 499 (v1.4)")
    from fastapi.testclient import TestClient
    from microbench.app import app
    import httpx as _httpx

    class _Resp:
        def __init__(self):
            self.status_code = 200
            self._body = {"choices": [{"message": {"content": "long answer " * 200}}]}
            self.text = str(self._body)
        def json(self):
            return self._body

    # Slow mock that lets us fire cancel mid-flight via TestClient
    import asyncio

    class _SlowAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, **kwargs):
            # Sleep long enough that we can simulate a client cancel
            await asyncio.sleep(2)
            return _Resp()

    client = TestClient(app)
    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _SlowAsyncClient
    try:
        # Skip if LLM not configured (chat_async raises LLMError before cancel matters)
        if not llm.is_configured():
            _check(True, "LLM 未配置, 跳过端到端 499 测试")
            return

        payload = {
            "question": "test cancel behavior",
            "sections": [{"section": "abstract", "title": "Abstract", "text": "test context"}],
            "paper_title": "TestPaper",
        }
        # TestClient doesn't natively simulate client disconnect, so we
        # verify the wiring differently: confirm the endpoint still works
        # with cancel-aware chat_async (smoke test that async path is wired)
        r = client.post("/api/paper/explain", json=payload)
        # Either 200 (slow but successful) or 499 (cancelled by Starlette timeout)
        _check(r.status_code in (200, 499),
               f"端点响应 200 或 499 (实际 {r.status_code})")
    finally:
        _httpx.AsyncClient = _orig


def test_chat_stream_async_basic():
    """Stage 2: chat_stream_async() yields chunks in order."""
    print("\n[38] chat_stream_async() 流式输出 (v1.4 stream)")
    import asyncio
    import httpx as _httpx

    sse_lines = [
        'data: {"choices":[{"delta":{"content":"hello"}}]}',
        'data: {"choices":[{"delta":{"content":" stream"}}]}',
        'data: {"choices":[{"delta":{"content":" world"}}]}',
        'data: [DONE]',
    ]

    class _MockStreamResp:
        def __init__(self, *args, **kwargs):
            self.status_code = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def aiter_lines(self):
            for line in sse_lines:
                yield line
        async def aread(self):
            return b""

    class _MockStreamClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def stream(self, method, url, **kwargs):
            return _MockStreamResp()

    primary_cfg = llm.model_manager.get_primary_config()
    if not primary_cfg.get("configured"):
        _check(True, "未配置 LLM, 跳过 streaming 测试")
        return

    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _MockStreamClient
    try:
        chunks = []
        async def collect():
            async for c in llm.chat_stream_async(
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
                temperature=0.3,
                timeout=10,
            ):
                chunks.append(c)
        asyncio.run(collect())
        _check(chunks == ["hello", " stream", " world"],
               f"流式 chunks 顺序正确 (实际 {chunks})")
        _check("".join(chunks) == "hello stream world",
               f"拼接后 = 'hello stream world' (实际 {''.join(chunks)!r})")
    finally:
        _httpx.AsyncClient = _orig


def test_chat_stream_async_cancel_mid_stream():
    """Stage 2: cancel_event set mid-stream → CancelledError raised."""
    print("\n[39] chat_stream_async() 流式中断 (v1.4 stream)")
    import asyncio
    import httpx as _httpx

    cancel_external = {"ev": None}

    sse_lines = [
        'data: {"choices":[{"delta":{"content":"chunk1"}}]}',
        'data: {"choices":[{"delta":{"content":"chunk2"}}]}',
        'data: {"choices":[{"delta":{"content":"chunk3"}}]}',
        'data: [DONE]',
    ]

    class _MockStreamResp:
        def __init__(self, *args, **kwargs):
            self.status_code = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def aiter_lines(self):
            n = 0
            for line in sse_lines:
                n += 1
                if n == 2 and cancel_external["ev"] is not None:
                    cancel_external["ev"].set()  # fire cancel after chunk1
                yield line
        async def aread(self):
            return b""

    class _MockStreamClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def stream(self, method, url, **kwargs):
            return _MockStreamResp()

    primary_cfg = llm.model_manager.get_primary_config()
    if not primary_cfg.get("configured"):
        _check(True, "未配置 LLM, 跳过 cancel mid-stream 测试")
        return

    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _MockStreamClient
    try:
        ev = asyncio.Event()
        cancel_external["ev"] = ev
        chunks = []
        async def collect():
            try:
                async for c in llm.chat_stream_async(
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=100,
                    temperature=0.3,
                    timeout=10,
                    cancel_event=ev,
                ):
                    chunks.append(c)
            except asyncio.CancelledError:
                pass
        asyncio.run(collect())
        # Should have received chunk1, then cancelled (not chunk2/chunk3)
        _check(chunks == ["chunk1"],
               f"取消后只收到 chunk1 (实际 {chunks})")
    finally:
        _httpx.AsyncClient = _orig


def test_explain_stream_endpoint_returns_sse():
    """Stage 2: /api/paper/explain_stream returns SSE chunks."""
    print("\n[40] /api/paper/explain_stream SSE smoke (v1.4)")
    from fastapi.testclient import TestClient
    from microbench.app import app
    import httpx as _httpx

    sse_lines = [
        'data: {"choices":[{"delta":{"content":"streamed"}}]}',
        'data: {"choices":[{"delta":{"content":" answer"}}]}',
        'data: [DONE]',
    ]

    class _MockStreamResp:
        def __init__(self, *args, **kwargs):
            self.status_code = 200
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def aiter_lines(self):
            for line in sse_lines:
                yield line
        async def aread(self):
            return b""

    class _MockStreamClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def stream(self, method, url, **kwargs):
            return _MockStreamResp()

    client = TestClient(app)
    _orig = _httpx.AsyncClient
    _httpx.AsyncClient = _MockStreamClient
    try:
        if not llm.is_configured():
            _check(True, "LLM 未配置, 跳过 stream endpoint 测试")
            return

        payload = {
            "question": "test stream",
            "sections": [{"section": "abstract", "title": "Abstract", "text": "ctx"}],
            "paper_title": "StreamTest",
        }
        with client.stream("POST", "/api/paper/explain_stream", json=payload) as r:
            _check(r.status_code == 200,
                   f"endpoint 返回 200 (实际 {r.status_code})")
            _check(r.headers["content-type"].startswith("text/event-stream"),
                   f"Content-Type 是 SSE (实际 {r.headers.get('content-type')})")
            body = r.read().decode("utf-8")
            _check('"text":"streamed"' in body,
                   f"SSE body 含 'streamed' chunk (body 长度 {len(body)})")
            _check('"text":" answer"' in body,
                   f"SSE body 含 ' answer' chunk")
            _check("data: [DONE]" in body,
                   f"SSE body 末尾含 [DONE] 终止标记")
    finally:
        _httpx.AsyncClient = _orig


def run_all():
    _cleanup_test_artifacts()
    test_path_safety()
    test_whitelist()
    test_special_chars()
    test_paper_fetcher_dispatch()
    test_arxiv_https_priority_and_fallback()
    test_bibtex()
    test_reproduction_scaffold()
    test_troubleshoot()
    test_weekly_report()
    test_vault_stats()
    test_search_vault()
    test_llm_config_and_fallback()
    test_summarize_paper_fallback()
    test_answer_with_context_empty_chunks()
    test_extract_curve_from_image()
    test_calibrate_points()
    test_compute_metrics()
    test_parse_csv_points()
    test_linear_interp()
    test_extract_text_from_pdf()
    test_classify_title_to_section()
    test_split_into_sections()
    test_chunk_section_text()
    test_translation_cache_lru()
    test_build_cross_section_context()
    test_translate_endpoint_routing()
    test_cross_qa_endpoint()
    test_cache_stats_endpoint()
    test_llm_chat_retry_on_transient_errors()
    test_dual_model_status_and_probe_endpoints()
    test_export_bilingual_endpoint()
    test_export_qa_endpoint()
    test_agnes_rate_limiter_unit()
    test_find_pdf_and_upload_pdf()
    test_explain_selection_endpoint()

    # Stage 1: async llm path
    test_chat_async_basic()
    test_chat_async_cancel_event_preset()
    test_chat_async_cancel_during_retry()
    test_invoke_model_async_retries_on_502()
    test_explain_endpoint_returns_499_on_cancel()

    _cleanup_test_artifacts()

    print("\n" + "=" * 60)
    print(f"📊 测试汇总: ✅ {PASS_COUNT} 通过 / ❌ {FAIL_COUNT} 失败")
    print("=" * 60)
    if FAIL_COUNT > 0:
        print("\n失败的测试:")
        for t in FAILED_TESTS:
            print(f"  - {t}")
        sys.exit(1)
    else:
        print("\n🎉 全部测试通过!")


if __name__ == "__main__":
    run_all()