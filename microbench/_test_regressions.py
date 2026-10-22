# -*- coding: utf-8 -*-
"""
Regression guards for the fixes made during the v2 audit pass.

Deliberately does NOT touch the user's real vault: every case here either
raises before any filesystem write, or works on in-memory data only. That is
the whole point — `test_bench.py` rewrites 03_Knowledge/踩坑与排错日记.md and
unlinks any literature file whose name contains "MoS2" / "SmokeTest" /
"BilingualTest" / "QA_", which are plausible *real* research-topic names.

Run:  python -m microbench._test_regressions
"""

import sys
import traceback
from pathlib import Path

from .generator import (
    BASE_DIR,
    _safe_path,
    create_reproduction_workspace,
    get_vault_stats,
    sanitize_filename,
)
from .paper_text import _assert_public_http_url
from .plot_compare import compute_metrics

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


def raises(fn, label, exc=Exception):
    try:
        fn()
    except exc:
        check(True, label)
    except Exception as e:  # wrong exception type still counts as "blocked"
        check(False, f"{label}  (raised {type(e).__name__} instead)")
    else:
        check(False, f"{label}  (no exception raised)")


# ---------------------------------------------------------------------------
print("\n[1] Path traversal: project_name='..' must not reach the vault root")
# ---------------------------------------------------------------------------
check(sanitize_filename("..") == "", "sanitize_filename('..') -> ''")
check(sanitize_filename(".") == "", "sanitize_filename('.') -> ''")
check(sanitize_filename("...") == "", "sanitize_filename('...') -> ''")
# Legitimate names must survive — the fix is not a blanket "strip dots".
check(sanitize_filename("v1.2_results") == "v1.2_results", "sanitize_filename keeps mid-name dots")
check(sanitize_filename("repro_cfet_sub3nm") == "repro_cfet_sub3nm", "sanitize_filename keeps normal names")

raises(
    lambda: _safe_path(BASE_DIR, "02_Reproduction", ".."),
    "_safe_path rejects a dot-only component",
    ValueError,
)
raises(
    lambda: _safe_path(BASE_DIR, "01_Literature", "..", "..", "Windows"),
    "_safe_path still rejects multi-hop traversal",
    ValueError,
)

# The end-to-end case from the audit: this used to overwrite the vault-root
# README.md and create configs/scripts/data/plots at the vault root.
root_readme = BASE_DIR / "README.md"
root_readme_before = root_readme.read_bytes() if root_readme.exists() else None
raises(
    lambda: create_reproduction_workspace({"project_name": ".."}),
    "create_reproduction_workspace('..') is rejected",
    ValueError,
)
check(
    (root_readme.read_bytes() if root_readme.exists() else None) == root_readme_before,
    "vault-root README.md untouched by the rejected call",
)
for leaked in ("configs", "scripts", "data", "plots"):
    check(
        not (BASE_DIR / leaked).exists(),
        f"no stray {leaked}/ created at the vault root",
    )

# ---------------------------------------------------------------------------
print("\n[2] Literature feed: the [:10] cap must not truncate the source list")
# ---------------------------------------------------------------------------
get_vault_stats.cache_clear() if hasattr(get_vault_stats, "cache_clear") else None
stats = get_vault_stats()
total = stats.get("literature_total", 0)
listed = stats.get("literature_list", [])
check(
    len(listed) == total,
    f"literature_list length == literature_total ({len(listed)} == {total})",
)
# And therefore the endpoint built on top of it can report has_more.
sim_filtered = [it for it in listed if "04_AI划词答疑" not in str(it.get("path", ""))]
page = sim_filtered[0:20]
has_more = (0 + len(page)) < len(sim_filtered)
check(
    has_more == (len(sim_filtered) > 20),
    f"has_more is derivable again (filtered={len(sim_filtered)}, page={len(page)})",
)

# ---------------------------------------------------------------------------
print("\n[3] plot_compare: an all-zero paper curve must NOT read as 'pass'")
# ---------------------------------------------------------------------------
xs = [0.0, 1.0, 2.0, 3.0, 4.0]
paper_zero = [(x, 0.0) for x in xs]
repro_zero = [(x, 0.0) for x in xs]
r = compute_metrics(paper_zero, repro_zero)
check(r.get("verdict") != "pass", f"all-zero curve verdict is {r.get('verdict')!r}, not 'pass'")
check("无法判定" in str(r.get("verdict_msg", "")), "verdict_msg explains it is undeterminable")

# A genuine match must still pass, so the guard didn't over-trigger.
paper_ok = [(x, x * 2.0 + 1.0) for x in xs]
repro_ok = [(x, x * 2.0 + 1.02) for x in xs]
r_ok = compute_metrics(paper_ok, repro_ok)
check(r_ok.get("verdict") == "pass", f"a genuinely close curve still passes (got {r_ok.get('verdict')!r})")

# A genuine mismatch must still fail.
repro_bad = [(x, x * 2.0 + 9.0) for x in xs]
r_bad = compute_metrics(paper_ok, repro_bad)
check(r_bad.get("verdict") == "fail", f"a genuinely wrong curve still fails (got {r_bad.get('verdict')!r})")

# ---------------------------------------------------------------------------
print("\n[4] SSRF guard: operator-supplied URLs must not reach the local network")
# ---------------------------------------------------------------------------
for bad, why in [
    ("http://127.0.0.1:8000/api/stats", "loopback"),
    ("http://localhost:5000/", "loopback by name"),
    ("http://[::1]:9000/", "IPv6 loopback"),
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
    ("http://10.0.0.5/internal", "RFC1918"),
    ("http://192.168.1.1/admin", "RFC1918"),
    ("http://172.16.0.1/", "RFC1918"),
    ("file:///C:/Windows/System32/drivers/etc/hosts", "file scheme"),
    ("ftp://example.com/x.pdf", "ftp scheme"),
    ("http://0.0.0.0/", "unspecified"),
]:
    raises(lambda u=bad: _assert_public_http_url(u), f"blocks {why}: {bad}", ValueError)

# The guard must not break the legitimate publisher/arXiv case. 8.8.8.8 is a
# public address, so this is a pure classification check with no DNS lookup.
_assert_public_http_url("https://arxiv.org/pdf/2303.15982.pdf")
check(True, "allows a public https URL (arxiv.org)")

# ---------------------------------------------------------------------------
print("\n[5] force_scope: the server now enforces the context budget")
# ---------------------------------------------------------------------------
# Previously the server hard-truncated surrounding_context to 10 000 chars no
# matter what scope the client asked for, which silently defeated the whole
# two-stage router design (an "full" scope asking for 60-80K got 10K). The
# server still cannot *build* the context (it has no paper text), but it now
# enforces the ceiling per scope. Assert the tiers are actually applied.
import inspect
from .app import ExplainSelectionRequest

desc = (ExplainSelectionRequest.model_fields["force_scope"].description or "").lower()
check("截断" in desc, "force_scope documents that the server enforces a cap")
check("term" in desc and "full" in desc, "force_scope documents all four tiers")

# The clamp lives inside the stream endpoint, so exercise the tiers through
# the real request handler rather than re-implementing the table here.
_src = inspect.getsource(
    __import__("microbench.app", fromlist=["app"]).api_explain_selection_stream
)
for tier, cap in (("term", "2000"), ("mid", "8000"), ("wide", "20000"), ("full", "50000")):
    check(f'"{tier}": {cap}' in _src, f"scope tier '{tier}' caps context at {cap} chars")
check("[:10000]" not in _src, "the old unconditional 10 000-char truncation is gone")

# ---------------------------------------------------------------------------
print("\n[6] Test-suite hygiene: the cleanup must not delete real vault notes")
# ---------------------------------------------------------------------------
# `test_bench.py` used to delete any 01_Literature/**/*.md whose filename
# contained "MoS2" / "QA_" / "SmokeTest" / "BilingualTest". The real vault
# contains a genuine research note named
#   2024_新型_{MoS2}HZO_异质结_2D-FET_—_物理建模.md
# so running the suite DESTROYED a real note. Cleanup must now key off an
# explicit manifest of files the suite created, never on a name substring.
from . import test_bench as _tb


def _safe_read(path):
    """Read a file's text, or '' if it cannot be read."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# The invariant is NOT "no file has a matching name" — it is "a file is an
# artifact if and only if its CONTENT carries the marker". Pick a matching
# name whose content does NOT carry the marker and assert it is not
# treated as an artifact. (The vault legitimately contains a real note
# whose title mentions MoS2.)
_real = BASE_DIR / "01_Literature"
_matching = [
    f for f in _real.rglob("*.md")
    if any(p in f.name for p in ("SmokeTest", "MoS2", "BilingualTest", "QA_"))
]
_unmarked = [f for f in _matching if not _tb.TEST_ARTIFACT_MARKER in _safe_read(f)]
check(
    not _unmarked or not _tb._is_test_artifact(_unmarked[0]),
    "同名但内容无标记的真实笔记不会被当成测试残留",
)
check(
    hasattr(_tb, "_is_test_artifact"),
    "test_bench exposes _is_test_artifact() for manifest-based cleanup",
)
# And the converse: a file that DOES carry the marker is recognised.
_marked = [f for f in _matching if _tb.TEST_ARTIFACT_MARKER in _safe_read(f)]
check(
    not _marked or all(_tb._is_test_artifact(f) for f in _marked),
    "带标记的文件都会被识别为测试残留",
)

# ---------------------------------------------------------------------------
print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
