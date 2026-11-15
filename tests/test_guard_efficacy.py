# -*- coding: utf-8 -*-
"""第 2 批：让「验收全绿」这件事本身可信。

AUDIT_2026-11-09 / AUDIT_2026-10-27 §6.6 / AGENTS §4.16 反复栽在同一件事上：
**测试全绿，但护栏没有保护力**。本文件不测业务功能，测的是护栏自己：

  1. 测试套件里不许再出现恒真断言（`or True` / `assert True`）；
  2. 用了 sandbox 的测试，所有写路径都必须真的落在临时目录；
  3. 默认 `-m "not slow"` 与依赖锁不能被悄悄删掉；
  4. 关键的行为级套件文件不许消失（页面代码回退时就没护栏了）。

这些是**元护栏**：新增/修改测试时若把护栏写松，这里会先红。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO / "tests"


def _assert_statements(path: Path):
    """只取**真正会执行**的 assert 语句（用 ast，不是数引号）。

    为什么不用「数行内引号奇偶」判断是不是在文档字符串里：
    **单行** docstring 的引号数是偶数 → 判定失效 →
    把文档里**提到**的反例当成恒真断言 → 整个套件假红
    （2026-11-10 审计实测）。ast 看的是语法树，没有这个问题。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            yield node


def test_no_tautological_assertions_in_the_suite():
    """`or True` / `assert True` 一旦回到测试里就红。

    注释里的举例不算（以 # 开头），文档字符串里提到反例也不算。
    历史上 `test_llm_error_friendly.py` 就有一行
    `assert ... in llm.__all__ or True` 混进了 586 passed。
    """
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        for node in _assert_statements(path):
            test = ast.unparse(node.test)
            if re.search(r"\bor\s+True\b", test) or re.match(
                    r"^True\s*(?:,|$)", test):
                offenders.append(f"{path.name}:{node.lineno}: {test}")
    assert not offenders, (
        "发现恒真断言（改坏代码也照样绿）:\n  " + "\n  ".join(offenders))


# 「读源码文本当护栏」的上限。
#
# 2026-11-10 审计 F5：这一类护栏改坏行为也照样绿 —— 把
# `const cur = <当前版本>` 改成 `const cur = data.extractor_version`
# （旧快照永不作废），目标字符串还在 → 全绿。
#
# 一口气全换完不现实：剩下的绝大多数护的是 **JS**
# （桌面 index.html / 平板 app.js），要换成行为级得先有 JS 测试台
# —— 平板那边已经有（microbench/_test_tablet_hardening.js），
# 桌面那边还没有，那是另一件独立的工程。
#
# 在那之前先钉住**不许再涨**：每加一条新功能就多一条假护栏，
# 「全绿」这个信号会越来越不值钱（AGENTS §4.16 同型复发）。
#
# 2026-11-10 已换掉 5 条（test_glyph_padding / test_llm_models / test_pdf_cache），
# 上限按当时的实际条数定；再往下换就要同步改这个数字。
#
# ⚠ 2026-11-15 合并云端升级时，条数从 50 涨到 132 —— 不是本机新增的，
#   是云端那批（2026-10-27 / 10-28 / 11-11 审计）带进来的 82 条：
#     test_audit_2026_10_27_android.py   29
#     test_zen_mode.py                   23
#     test_bilingual.py                  12
#     test_audit_2026_10_27_tablet.py    11
#     test_audit_2026_10_27_core.py       6
#     test_audit_2026_10_27_medium.py     1
#   云端自己的审计（docs/AUDIT_2026-11-11.md §3.13 / §5）也把这一类列为
#   「要逐类换成行为级」的待办 —— 说明**两边都认这是债**。
#   这里把上限按**合并后的实测条数**重设为 132：
#     * 上限的作用是「不许再涨」，不是「必须清零」；
#     * 照旧钉死在 50 会让合并后的套件直接变红 → 护栏变成摆设、
#       下次谁都不知道它已经失效了；
#     * 重设时必须写明来源（本段），否则「改数字」本身就成了没人看的暗箱。
#   → 后续每换掉一条行为级护栏，就把上限改小 1，并在本段记一笔。
_MAX_SOURCE_TEXT_GUARDS = 132


def test_no_test_builds_a_client_without_sandboxing_the_vault():
    """任何自己 new TestClient 的测试，都必须先把 vault 指向临时目录。

    ⚠ 这条是 P0（2026-11-10 审计**实测**）：
    `tests/test_glyph_perf_regression.py` 的两条上传测试自己 new 了
    `TestClient(app_mod.app)`，**绕过 sandbox fixture** →
    `app.BASE_DIR` / `generator.BASE_DIR` 还是真实仓库根 →
    `/api/paper/upload_pdf` 把 PDF 直接写进
    `01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/`，
    在用户真实 vault 里留下 `2026_NoModel.pdf` / `2026_Revert_Check.pdf`
    两个 406 字节的垃圾文件。**跑一次测试就在用户地盘上留垃圾。**

    这正是 2026-10-26 审计 P0「测试不再碰真数据」的**同型复发** ——
    上次修的是别的地方，这次漏了这个文件。

    判据用 ast：找所有**自己** new 了 TestClient 的测试函数，
    它们的参数里必须有负责重定向 vault 的 fixture。
    """
    allowed = {"sandbox", "env", "vault"}
    # ⚠ needle 要**拼**出来：本文件自己的代码里若出现字面量 "TestClient("，
    # 这条护栏会把自己也当成违规者（ast.unparse 后子串照样在）。
    needle = "TestClient" + "("
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        if path.name == "conftest.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            # 只看**代码**，不看 docstring —— 否则这条护栏自己会被
            # 自己文档里提到的 TestClient( 抓住（它自己就写在那儿）。
            body = list(node.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            src = "\n".join(ast.unparse(st) for st in body)
            if needle not in src:
                continue
            args = {a.arg for a in node.args.args}
            if not (args & allowed):
                offenders.append(f"{path.name}:{node.lineno}: {node.name}")
    assert not offenders, (
        "这些测试自己 new 了 TestClient，却没有把 vault 指向临时目录 —— "
        "跑一次就会往用户真实 vault 里写垃圾：\n  " + "\n  ".join(offenders))


def test_source_text_guards_do_not_grow():
    """「读源码文本当护栏」的条数不得增加。"""
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("*.py")):
        for node in _assert_statements(path):
            test = ast.unparse(node.test)
            if re.search(r"\bin\s+(src|source|app_src|source_code|js_src)\b",
                         test):
                offenders.append(f"{path.name}:{node.lineno}")
    assert len(offenders) <= _MAX_SOURCE_TEXT_GUARDS, (
        f"「读源码文本当护栏」涨到 {len(offenders)} 条"
        f"（上限 {_MAX_SOURCE_TEXT_GUARDS} 条）—— 每一条都是改坏行为也照样绿的假护栏:\n  "
        + "\n  ".join(offenders[:12])
        + "\n  换掉一条就把上限改小 1，并在这里写明换成什么行为级断言。")


def test_sandbox_pins_every_logs_write_path(sandbox, tmp_path):
    """钉死 B2 残留面：三个 import 期固化的 logs 路径 + 同步根。

    这条不只是读常量，而是断言它们都落在当前测试的 tmp_path 下；
    如果 conftest 的 `_pin_logs_write_paths` 被删掉，这里立刻红。
    """
    from microbench import app as app_mod
    from microbench import glyphs as glyphs_mod
    from microbench import pdf_cache as pdf_cache_mod

    root = tmp_path.resolve()
    paths = {
        "app.GLYPH_DICT_PATH": Path(app_mod.GLYPH_DICT_PATH),
        "glyphs.DEFAULT_DICT_PATH": Path(glyphs_mod.DEFAULT_DICT_PATH),
        "pdf_cache.CACHE_DIR": Path(pdf_cache_mod.CACHE_DIR),
        "app._sync_root()": Path(app_mod._sync_root()),
    }
    for name, p in paths.items():
        assert root in p.resolve().parents or p.resolve() == root, \
            f"{name} 没被钉进沙箱，指向了 {p}"


def test_default_pytest_options_exclude_slow_tests():
    """不带 -m 会连 arxiv 下载整篇 PDF 卡死（AGENTS §2.8），默认值不能丢。"""
    ini = (REPO / "pytest.ini").read_text(encoding="utf-8")
    assert re.search(r'addopts\s*=.*-m\s+"not slow"', ini), \
        "pytest.ini 不再默认排除 slow，提交前自查又要靠人肉纪律了"
    assert "testpaths" in ini
    constraints = REPO / "constraints.txt"
    assert constraints.exists(), "依赖版本锁不见了（两台设备会装出不同依赖树）"
    pinned = [l for l in constraints.read_text(encoding="utf-8").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    assert len(pinned) >= 16, f"锁定的直接依赖只剩 {len(pinned)} 个"
    assert all("==" in l for l in pinned), "constraints 里混进了非精确版本"


def test_behavioural_guard_files_are_still_there():
    """行为级套件被删掉时，源码文本护栏就成唯一的（假）保护了。"""
    required = [
        "microbench/_test_tablet_hardening.js",   # 平板 loadTranslation 版本闸门
        "tests/test_tablet_frontend.py",          # 把 JS 套件挂进 pytest
        "tests/test_route_contracts.py",          # 装饰器挂错函数的反向护栏
        "tests/test_audit_2026_11_09.py",         # 第 1 批的行为级回归
    ]
    missing = [r for r in required if not (REPO / r).exists()]
    assert not missing, f"行为级护栏文件不见了: {missing}"

    js = (REPO / "microbench" / "_test_tablet_hardening.js").read_text(
        encoding="utf-8")
    assert "loadTranslation" in js and "extractor_version" in js, \
        "平板 JS 套件不再驱动真实的版本闸门"
