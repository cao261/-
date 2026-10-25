# -*- coding: utf-8 -*-
"""交付前最后一道关：逐条核对**真实 APK** 里有没有用户要的每个功能。

为什么要有这个：
「代码里有这个字符串」不等于「装到手机上能用」。这个文件把 APK 拆开
（assets/index.html + classes.dex），对着**用户提过的每一条需求**逐项核对，
缺哪项就红。

跑法:
    python -m pytest tests/test_apk_delivery.py -q
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APK = REPO / "MicroBench-平板工作台.apk"
BUNDLE = REPO / "MicroBench_平板工作台.html"


def _load() -> tuple[str, str, str]:
    """返回 (前端代码, Java 侧 dex 字符串, 导出器源码)。"""
    if not APK.exists():
        pytest.skip("还没构建 APK —— 先跑 android/build_apk.ps1")
    with zipfile.ZipFile(APK) as z:
        html = z.read("assets/index.html").decode("utf-8")
        dex = b"".join(z.read(n) for n in z.namelist() if n.endswith(".dex"))
    exporter = (REPO / "microbench" / "export_offline.py").read_text(encoding="utf-8")
    return html, dex.decode("latin-1"), exporter


@pytest.fixture(scope="module")
def parts():
    return _load()


# ---------------------------------------------------------------------------
# 需求 → 证据（在哪个文件里找）
#   None     = 前端 assets/index.html
#   "dex"    = Java 侧 classes.dex
#   "export" = 导出器 export_offline.py
# ---------------------------------------------------------------------------
REQUIREMENTS = [
    # ---- ① 本地优先：断网可读可写，联网时同步 ----
    ("① 本地优先：批注存本地", ['"annos_" + key'], None),
    ("① 本地优先：先写本地再同步云盘", ["putAnnos", "pushAnnosToCloud"], None),
    ("① 本地优先：批注按 id 合并不整份覆盖", ["function mergeAnnos"], None),
    ("① 本地优先：IndexedDB 不升版本（升级会白屏）", ["const DB_VER = 2;"], None),
    ("① 本地优先：版本对不上时降级打开（不白屏）", ["openDBWithoutVersion"], None),

    # ---- ② 同步语义与电脑端一致 ----
    ("② 同步：读同步根（译文/批注所在）", ['"/dav/sync/"', "syncUrl("], None),
    ("② 同步：paper_id 与电脑端逐字一致",
     ["function paperIdFor", "function safePaperKey"], None),
    ("② 同步：写同步根（手写/批注传得上去）", ["putSync"], "dex"),
    ("② 同步：Java 侧有同步根路由与探测",
     ["/dav/sync/list/", "/dav/sync/file/", "detectSyncRoot"], "dex"),

    # ---- ③ 布局三选一 ----
    ("③ 布局：原文+译文 / 仅原文 / 仅译文",
     ["仅原文", "仅译文", 'lay-" + (pdfState.layout'], None),
    ("③ 布局：单栏态真的把另一边藏了",
     [".tb-pdf-page.lay-orig .tb-pdf-trans",
      ".tb-pdf-page.lay-trans .tb-pdf-orig"], None),

    # ---- ④ 手写 ↔ AI ↔ 批注 ----
    ("④ 手写：原文上有手写层", ["tb-pdf-ink"], None),
    ("④ 手写：能直接问 AI（多模态）",
     ["askAiWithInk", "captureInkPage", "image_url"], None),
    ("④ 手写：能存成批注（跟电脑端共享）",
     ["saveInk", 'source: "ink"', "strokes: strokes"], None),
    ("④ 划词能存批注（选区归一化，同一套算法）",
     ["saveQuoteAsAnno", "collectSelectionRects", "data-pdf-anno"], None),

    # ---- ⑤ 译文 ----
    ("⑤ 译文：能跟电脑端共享（读快照）",
     ["loadTranslation", 'snapshots/"'], None),
    ("⑤ 译文：页面认导出里烤的译文", ["state.transSeed"], None),
    ("⑤ 译文：导出器确实把译文烤进去", ["collect_translations"], "export"),
    ("⑤ 译文：公式能渲染", ["renderTranslationInto"], None),

    # ---- ⑥ 阅读器 ----
    ("⑥ 阅读器：按容器宽度缩放（不再只显示一半）", ["pdfFitScale"], None),
    ("⑥ 阅读器：缩放保留滚动位置", ["ratio * br.height"], None),

    # ---- ⑦ AI 问答 ----
    ("⑦ AI：发送后清空输入栏", ['if (qBox) qBox.value = "";'], None),
    ("⑦ AI：多轮上下文", ["AI_HISTORY_TURNS", "aiHistory"], None),
    ("⑦ AI：Enter 发送且不误触发（中文输入法）", ["!e.isComposing"], None),
    ("⑦ AI：停止 / 清空对话", ["btn-ai-stop", "btn-ai-clear"], None),

    # ---- ⑧ 限流防护 ----
    ("⑧ 限流：Java 侧节流 + 退避重试",
     ["throttle", "isRateLimited", "backoff"], "dex"),
    ("⑧ 限流：Java 侧解析 mtime", ["parseHttpDate"], "dex"),
    ("⑧ 限流：页面按 mtime 跳过没变的笔记",
     ["local.mtime === e.mtime"], None),

    # ---- ⑨ 安全与数据安全 ----
    ("⑨ 安全：esc() 的引号转义修对了（原来是坏的）",
     ['.replace(/"/g, E_AMP + "quot;");'], None),
    ("⑨ 安全：属性白名单（style / on* 都丢掉）", ["ALLOWED_ATTRS"], None),
    ("⑨ 数据安全：种子不覆盖本地正文",
     ["本地已有的记录必须**以本地为准**"], None),
    ("⑨ 数据安全：渲染崩溃有兜底（不白屏）",
     ["Markdown 渲染失败，回退纯文本"], None),
]

# 曾经有过、现在必须不在的坏代码
STALE = [
    ("不再有 flex 横排的阅读器", "display:flex; justify-content:center;"),
    ("不再有 esc 的 E_QUOT 错误", '.replace(/"/g, E_QUOT'),
    ("不再有永远不成立的 syncMode === \"dir\"", 'state.syncMode === "dir"'),
    ("不再有划词绑在会被清掉的 textLayer 上", 'const layer = $("pdf-textlayer")'),
    ("不再有把批注复活的老回灌分支",
     '_merge_annotation_lists(\n                        cur.get("annotations")'),
]


def test_every_requirement_is_in_the_apk(parts):
    html, dex, exporter = parts
    hay = {None: html, "dex": dex, "export": exporter}
    missing = []
    for label, needles, where in REQUIREMENTS:
        for n in needles:
            if n not in hay[where]:
                missing.append(f"{label}  → 缺 {n!r}")
    assert not missing, "APK 里缺这些功能:\n  " + "\n  ".join(missing)


def test_broken_code_is_gone(parts):
    html, _dex, _exporter = parts
    still = [label for label, needle in STALE if needle in html]
    assert not still, "APK 里还留着这些坏代码:\n  " + "\n  ".join(still)


def test_bundle_in_apk_matches_repo_copy():
    """APK 里内嵌的页面 = 仓库根目录那份（不能装到旧版本）。"""
    if not APK.exists() or not BUNDLE.exists():
        pytest.skip("产物不全")
    with zipfile.ZipFile(APK) as z:
        asset = z.read("assets/index.html")
    assert asset == BUNDLE.read_bytes(), (
        "APK 里的 assets/index.html 和 MicroBench_平板工作台.html 不一致 —— "
        "装到手机上会是旧版本"
    )


def test_translations_are_baked_in(parts):
    """译文要真的烤进单文件（没连云盘也得有译文）。"""
    html, _dex, _exporter = parts
    m = re.search(r'<script type="application/json" id="mb-data">(.*?)</script>',
                  html, re.S)
    assert m, "找不到种子数据"
    data = json.loads(m.group(1).replace("<\\/", "</"))
    trans = data.get("trans") or {}
    assert trans, "单文件里没有译文 —— 没连云盘时就只有原文了"
    assert sum(len(v) for v in trans.values()) > 0, "译文是空的"
    assert len(data.get("pdfs") or {}) >= 20, "PDF 清单不全"
    assert len(data.get("notes") or []) >= 20, "笔记不全"


def test_apk_is_signed_and_coverable():
    """签名必须和已装版本一致 —— 否则装不上（只能卸载，数据会丢）。"""
    if not APK.exists():
        pytest.skip("还没构建 APK")
    import shutil
    import subprocess
    bt = Path(r"D:\Android\Sdk\build-tools\35.0.1")
    jdk = Path(r"D:\Desktop\杂物\z\Zotero\jdk")
    if not (bt / "lib" / "apksigner.jar").exists() or not jdk.exists():
        pytest.skip("这台机器没有 apksigner / JDK")
    r = subprocess.run(
        [str(jdk / "bin" / "java.exe"), "-cp", str(bt / "lib" / "apksigner.jar"),
         "com.android.apksigner.ApkSignerTool", "verify", "--verbose",
         "--print-certs", str(APK)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = r.stdout
    assert "Verifies" in out, f"签名校验没过:\n{out}"
    assert ("ac50efaf39045a110f60e9922848f0ad61cf689dc59c3ebd2b5de2731e964a55"
            in out), "签名证书变了 —— 装到平板上会失败（只能卸载，数据会丢）"
    assert "Verified using v2 scheme (APK Signature Scheme v2): true" in out, \
        "v2 签名无效（Android 7+ 认这个）"
