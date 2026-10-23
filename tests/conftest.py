# -*- coding: utf-8 -*-
"""
Shared pytest fixtures for the MicroBench test suite.

The most important fixture is `sandbox`, which redirects the whole
application at a THROWAWAY vault. The user's real vault holds real
research notes; a test that writes to it is a test that can destroy
work. Every write-path test therefore runs against a temporary
directory that contains a copy of `_Templates/`.

Run:
    python -m pytest tests -v
    python -m pytest tests -v -m "not slow"     # skip network/LLM tests
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# 在任何 microbench 模块被 import 之前，先占住部署开关。
#
# `microbench.llm` 在 import 时 `load_dotenv(..., override=False)`，会把 .env
# 的值灌进 os.environ。而 .env 是**部署配置**（这台机器要 SYNC_PDF_BODY=0，
# 因为 vault 整体搬进了坚果云），pytest 跑在同一进程里也会吃到 → files 通道
# 的 11 个测试因为「通道被关」集体变红，看着像代码坏了，其实是环境泄漏。
#
# load_dotenv 是 override=False：**已存在的环境变量不会被覆盖** → 这里先占位，
# .env 就进不了测试进程。setdefault 而非硬赋值：想显式用别的值跑测试时，
# `SYNC_PDF_BODY=0 pytest` 仍然有效。
# ---------------------------------------------------------------------------
os.environ.setdefault("SYNC_PDF_BODY", "1")


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def real_vault() -> Path:
    """The real project root (= the Obsidian vault)."""
    return PROJECT_ROOT


@pytest.fixture(autouse=True)
def _pin_sync_pdf_body(monkeypatch):
    """测试不依赖本机 .env 里的部署开关。

    `microbench.llm` 在 import 时 `load_dotenv(..., override=False)`，会把 .env
    的 SYNC_PDF_BODY 注进 os.environ。而部署方一旦把 vault 整体搬进坚果云，
    就必须设 `SYNC_PDF_BODY=0`（PDF 直接由坚果云共享，files 通道不再传第二份），
    于是 files 通道的 11 个测试会因为「通道被关」全红 —— 这是环境泄漏，不是
    代码坏了。

    测试套件固定跑**默认行为（通道开启）**；关闭模式由
    `test_pdf_body_can_be_disabled` 显式 monkeypatch 覆盖。

    上面模块级那句 `os.environ.setdefault(...)` 是第一道防线（不让 .env 进进程），
    这个 fixture 是第二道（每个测试都保证，monkeypatch 结束后自动还原）。
    两道都要：fixture 只在测试执行期生效，模块级那道才挡得住 import 期泄漏。
    """
    monkeypatch.setenv("SYNC_PDF_BODY", "1")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch) -> Path:
    """Redirect BASE_DIR at both `generator` and `app` to a temp vault.

    `microbench.generator` uses its module-level BASE_DIR for every
    filesystem operation, and `microbench.app` imported a *copy* of the
    constant — so both have to be patched.
    """
    from microbench import app as app_mod
    from microbench import generator as gen_mod

    # Copy the templates so the card/report writers work.
    # The vault's _Templates/ is gitignored (it belongs to the user's Obsidian
    # vault), so a fresh clone has none — fall back to the bundled copies that
    # ship inside the package.
    tpl_src = PROJECT_ROOT / "_Templates"
    if not tpl_src.exists():
        tpl_src = PROJECT_ROOT / "microbench" / "templates"
    if tpl_src.exists():
        shutil.copytree(tpl_src, tmp_path / "_Templates", dirs_exist_ok=True)

    for d in ("01_Literature", "02_Reproduction", "03_Knowledge",
              "04_Weekly_Reports", "logs"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(gen_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(app_mod, "BASE_DIR", tmp_path)
    gen_mod._invalidate_vault_stats_cache()
    yield tmp_path
    gen_mod._invalidate_vault_stats_cache()


@pytest.fixture()
def client(sandbox):
    """A TestClient bound to the sandboxed app."""
    from fastapi.testclient import TestClient
    from microbench.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def vault_client(real_vault):
    """A TestClient against the REAL vault — read-only tests only.

    Use this only for endpoints that do not write (GET /api/stats,
    GET /api/vault/literature, ...). Writing tests must use `client`.
    """
    from fastapi.testclient import TestClient
    from microbench.app import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def llm_ready() -> bool:
    """True when an LLM provider is configured in .env."""
    from microbench import llm
    return llm.is_configured()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: hits the network or the LLM provider (seconds to minutes)"
    )
    config.addinivalue_line(
        "markers", "network: requires outbound internet access"
    )
    config.addinivalue_line(
        "markers", "llm: requires a configured LLM provider and spends tokens"
    )
