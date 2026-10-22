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


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def real_vault() -> Path:
    """The real project root (= the Obsidian vault)."""
    return PROJECT_ROOT


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
