"""跨设备同步契约测试 —— 锁死 paper_id 算法。

**为什么需要这个文件**

paper_id = 文件名(去.pdf, 截 70) + "_" + djb2(完整 vault 相对路径) 的 base36。
它是跨设备同步的**唯一寻址键**：快照 / 批注 / 进度 / PDF 四个通道全靠它对应。
一旦这个算法被改动，两台设备算出的 paper_id 就不同 →
对方的译文快照、批注、阅读进度**全部认不出来**（表现为"同步了但对不上"），
而且这种断裂**不会报任何错**，只会静默发生。

`tests/test_sync_files.py::test_paper_id_matches_frontend_algorithm` 只断言了
**后端** `_paper_id_for()` 等于一个硬编码值 —— 改后端会红，但**改前端
`static/index.html` 里的算法照样绿**。本文件补上这个缺口：
真正把前端那段 JS 抽出来、用 node 跑一遍，再和后端 + 锁定值三方对齐。

**破坏性验证**（改前端算法试试）：
    把 index.html 里的 `5381` 改成 `5382` → 本文件立刻失败。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO / "microbench" / "static" / "index.html"

# 真实设备上跑出来的 paper_id（原始 id，前后端一致，保留空格/括号/& 等原字符）。
# 这三个是 2026-10-23 两台设备实测互通过的，锁死它们 = 锁死跨设备兼容性。
LOCKED = {
    "01_Literature/04_Survey_综述/01_论文原文_PDF/"
    "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf":
        "2008-JACERS-综述-Magnetoelectric laminate composites An overview_pgghcp",
    "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/"
    "2026_Sensors_&_Actuators_A._Physical_382_(2025)_116174.pdf":
        "2026_Sensors_&_Actuators_A._Physical_382_(2025)_116174_1j6nqf7",
    "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/"
    "2025_AgenticTCAD_A_LLM-based_Multi-.pdf":
        "2025_AgenticTCAD_A_LLM-based_Multi-_1dawd47",
}

# 落盘文件名 = paper_id 去掉非 [A-Za-z0-9_-] 字符后的结果。
# 同步目录里实际出现的文件名长这样，跨设备对不上时先查这里。
LOCKED_FILENAMES = {
    "01_Literature/04_Survey_综述/01_论文原文_PDF/"
    "2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf":
        "2008-JACERS-综述-MagnetoelectriclaminatecompositesAnoverview_pgghcp",
    "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/"
    "2026_Sensors_&_Actuators_A._Physical_382_(2025)_116174.pdf":
        "2026_Sensors__Actuators_A_Physical_382_2025_116174_1j6nqf7",
    "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/"
    "2025_AgenticTCAD_A_LLM-based_Multi-.pdf":
        "2025_AgenticTCAD_A_LLM-based_Multi-_1dawd47",
}

# 从 index.html 里抽前端算法的那几行。
_FRONTEND_RE = re.compile(
    r"const _paperPath = found\.relative_path;\s*"
    r"(?P<body>.*?)"
    r"const paperId = `\$\{_paperBase\}_\$\{\(_paperHash >>> 0\)\.toString\(36\)\}`;",
    re.DOTALL,
)


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("没装 node —— 前端算法护栏跳过（后端锁定值仍然生效）")
    return exe


def _extract_frontend_algorithm() -> str:
    """把 index.html 里的 paperId 计算抽成一个可独立调用的 JS 函数。"""
    src = INDEX_HTML.read_text(encoding="utf-8")
    m = _FRONTEND_RE.search(src)
    assert m, (
        "无法从前端 index.html 提取 paper_id 算法 —— 前端重构过了。\n"
        "请人工确认新算法是否与后端 microbench/app.py:_paper_id_for() 一致，\n"
        "确认后更新本文件的 _FRONTEND_RE 和 LOCKED。"
    )
    body = m.group("body")
    return (
        "function paperIdOf(relative_path) {\n"
        "  const _paperPath = relative_path;\n"
        f"{body}\n"
        "  return `${_paperBase}_${(_paperHash >>> 0).toString(36)}`;\n"
        "}\n"
    )


def _run_frontend(paths: list) -> dict:
    """用 node 真跑一遍前端算法。"""
    node = _node()
    js = _extract_frontend_algorithm() + (
        "const paths = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));\n"
        "const out = {};\n"
        "for (const p of paths) out[p] = paperIdOf(p);\n"
        "console.log(JSON.stringify(out));\n"
    )
    with tempfile.TemporaryDirectory() as td:
        js_path = Path(td) / "paper_id_frontend.js"
        json_path = Path(td) / "paths.json"
        js_path.write_text(js, encoding="utf-8")
        json_path.write_text(json.dumps(paths, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run([node, str(js_path), str(json_path)],
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=60)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_frontend_algorithm_extractable():
    """前端算法必须还能被自动提取 —— 提不出来就说明有人动过它。"""
    assert _extract_frontend_algorithm(), "前端算法提取为空"


def test_frontend_matches_backend():
    """前端算出来的 paper_id 必须和后端 _paper_id_for() 完全一致。"""
    from microbench import app
    paths = list(LOCKED)
    frontend = _run_frontend(paths)
    for p in paths:
        assert frontend[p] == app._paper_id_for(p), (
            f"前端与后端 paper_id 不一致（同步会静默断裂）:\n"
            f"  路径: {p}\n"
            f"  前端: {frontend[p]}\n"
            f"  后端: {app._paper_id_for(p)}"
        )


def test_locked_paper_ids_unchanged():
    """锁定值：这两台设备真实互通过的 paper_id 不许变。"""
    from microbench import app
    for rel, expect in LOCKED.items():
        assert app._paper_id_for(rel) == expect, (
            f"paper_id 契约被改动了:\n"
            f"  路径: {rel}\n"
            f"  期望: {expect}\n"
            f"  实际: {app._paper_id_for(rel)}\n"
            f"改这个会让两台设备认不出对方的快照/批注/进度。"
        )


def test_frontend_locked_values_unchanged():
    """同样的锁定值，从前端算法那一侧也要成立。"""
    frontend = _run_frontend(list(LOCKED))
    for rel, expect in LOCKED.items():
        assert frontend[rel] == expect, (
            f"前端 paper_id 契约被改动了:\n"
            f"  路径: {rel}\n"
            f"  期望: {expect}\n"
            f"  实际: {frontend[rel]}"
        )


def test_locked_filenames_unchanged():
    """同步目录里实际出现的文件名也要锁 —— 两台设备靠它对应。"""
    from microbench import app
    for rel, expect in LOCKED_FILENAMES.items():
        got = app._safe_paper_key(app._paper_id_for(rel))
        assert got == expect, (
            f"同步文件名契约被改动了:\n"
            f"  路径: {rel}\n"
            f"  期望: {expect}\n"
            f"  实际: {got}"
        )


def test_relative_path_participates_in_hash():
    """完整相对路径参与 hash —— 换个目录 hash 必须变（否则跨目录会撞车）。"""
    from microbench import app
    a = "01_Literature/04_Survey_综述/01_论文原文_PDF/x.pdf"
    b = "01_Literature/99_Other/01_论文原文_PDF/x.pdf"
    assert app._paper_id_for(a) != app._paper_id_for(b)


# 非 BMP 字符（emoji、扩展 B 区汉字、数学字母）——
# ⚠ JS 的 charCodeAt 取 UTF-16 **码元**（代理对算 2 个），
#   Python 的 ord 取**码点**（代理对算 1 个）。
#   以前后端用 ord 逐码点算，于是这类路径的前后端 paper_id 不一样，
#   跨设备的快照/批注/进度就**静默**认不出来。
#   LOCKED 里的样本全是 BMP 字符，覆盖不到这个洞 —— 所以单独立一组。
NON_BMP_PATHS = [
    "01_Literature/04_Survey_综述/\U0001F680 rocket paper.pdf",
    "01_Literature/04_Survey_综述/a\U0001F600b.pdf",
    "01_Literature/04_Survey_综述/\U00020BB7 \U0001D400 math.pdf",
    "01_Literature/04_Survey_综述/中文路径.pdf",
]


def test_non_bmp_paths_match_frontend():
    """含代理对的路径，前后端 paper_id 必须逐字一致。"""
    from microbench import app
    frontend = _run_frontend(NON_BMP_PATHS)
    for p in NON_BMP_PATHS:
        assert frontend[p] == app._paper_id_for(p), (
            f"非 BMP 路径的前后端 paper_id 不一致（同步会静默断裂）:\n"
            f"  路径: {p}\n"
            f"  前端: {frontend[p]}\n"
            f"  后端: {app._paper_id_for(p)}"
        )


def test_backend_hash_uses_utf16_code_units():
    """后端必须按 UTF-16 码元算 —— 改回 ord() 逐码点就会红。"""
    from microbench import app
    p = "01_Literature/\U0001F600.pdf"
    # 代理对算 2 个码元（0xD83D, 0xDE00），不是 1 个码点
    assert app._djb2_base36_utf16("\U0001F600") != app._js_base36(
        ((5381 << 5) + 5381 + 0x1F600) & 0xFFFFFFFF
    )


def test_sync_channel_names_are_contract():
    """四个通道的目录名也是契约 —— 改名会让两台设备对不上。"""
    from microbench import app
    assert set(app.SYNC_KINDS) == {"snapshots", "annotations", "progress"}
    assert app.SYNC_FILE_SUBDIR == "files"
    # 云端文件夹名两台设备必须一致（写在 .env 的 ANNOTATIONS_DIR 末段）
    assert app.NUTSTORE_SUBDIR == "MicroBench同步"
    assert "我的坚果云" in app.NUTSTORE_CLOUD_NAMES
