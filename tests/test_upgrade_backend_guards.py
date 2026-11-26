# -*- coding: utf-8 -*-
"""后端模块化、SQLite 索引、安全边界、并发锁与流式心跳的行为级回归护栏。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from microbench import app as app_mod
from microbench.core.security import AUTH_LIMITER, escape_html_text, extract_host_port
from microbench.services import vault_index


def _minimal_pdf_bytes() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]"
        b"/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>endobj\n"
        b"4 0 obj<</Length 58>>stream\n"
        b"BT /F1 16 Tf 40 140 Td (MicroBench Search Test) Tj ET\n"
        b"endstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"trailer<</Root 1 0 R>>\nstartxref 0\n%%EOF\n"
    )


def _seed_vault(root: Path) -> None:
    pdf_dir = root / "01_Literature" / "01_Device_TCAD_器件仿真" / "01_论文原文_PDF"
    rep_dir = root / "01_Literature" / "01_Device_TCAD_器件仿真" / "05_译文替换版_PDF"
    card_dir = root / "01_Literature" / "01_Device_TCAD_器件仿真" / "03_文献速览卡片"
    for d in (pdf_dir, rep_dir, card_dir):
        d.mkdir(parents=True, exist_ok=True)
    (pdf_dir / "2025_FerroFET_Study.pdf").write_bytes(_minimal_pdf_bytes())
    (rep_dir / "2025_FerroFET_Study_中译替换版.pdf").write_bytes(_minimal_pdf_bytes())
    (card_dir / "2025_FerroFET_笔记.md").write_text(
        '---\ntitle: "FerroFET 器件分析"\ntags: ["FeFET", "TCAD"]\nstatus: "精读中"\n---\n\n本文研究了铁电栅极电容匹配。\n',
        encoding="utf-8",
    )


def test_health_and_sandboxed_sqlite_search(sandbox):
    _seed_vault(sandbox)
    vault_index._invalidate_index_cache(sandbox)

    with TestClient(app_mod.app) as client:
        hr = client.get("/api/health")
        assert hr.status_code == 200
        assert hr.json()["status"] == "ok"

        sr = client.get("/api/vault/search", params={"q": "FerroFET", "refresh": "true"})
        assert sr.status_code == 200, sr.text
        data = sr.json()
        assert data["total"] == 2
        rels = {it["relative_path"] for it in data["items"]}
        assert "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/2025_FerroFET_Study.pdf" in rels
        assert "01_Literature/01_Device_TCAD_器件仿真/03_文献速览卡片/2025_FerroFET_笔记.md" in rels
        assert all("05_译文替换版_PDF" not in r for r in rels)

        # 状态过滤
        nr = client.get("/api/vault/search", params={"kind": "note", "status": "精读中"})
        assert nr.status_code == 200
        assert nr.json()["total"] == 1
        assert (sandbox / "logs" / "vault_index.db").exists()


def test_vault_paper_meta_updates_frontmatter_and_rejects_escape(sandbox):
    _seed_vault(sandbox)
    rel = "01_Literature/01_Device_TCAD_器件仿真/03_文献速览卡片/2025_FerroFET_笔记.md"

    with TestClient(app_mod.app) as client:
        r = client.post(
            "/api/vault/paper_meta",
            json={"relative_path": rel, "status": "已精读", "tags": ["FeFET", "验证通过"]},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["paper_status"] == "已精读"
        assert body["tags"] == ["FeFET", "验证通过"]

        disk = (sandbox / rel).read_text(encoding="utf-8")
        assert "铁电栅极电容匹配" in disk
        assert 'status: "已精读"' in disk

        # 更新后 SQLite 检索应立刻反映新状态
        sr = client.get("/api/vault/search", params={"kind": "note", "status": "已精读"})
        assert sr.status_code == 200
        assert sr.json()["total"] == 1

        bad = client.post(
            "/api/vault/paper_meta",
            json={"relative_path": "logs/lan_token.txt", "status": "已精读"},
        )
        assert bad.status_code == 400


def test_batch_translate_job_lifecycle_and_cancel(sandbox, monkeypatch):
    _seed_vault(sandbox)
    rel = "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/2025_FerroFET_Study.pdf"
    translated_pages = []

    monkeypatch.setattr(app_mod.llm, "is_configured", lambda: True)

    async def fake_translate_page(req):
        await asyncio.sleep(0.02)
        translated_pages.append(req.page)
        return {"status": "success", "page": req.page, "translation": f"第{req.page}页"}

    monkeypatch.setattr(app_mod, "api_translate_page", fake_translate_page)
    monkeypatch.setattr(app_mod, "_extract_page_cached", lambda path, page: ("Sample long text for batch translation page", []))

    with TestClient(app_mod.app) as client:
        r = client.post(
            "/api/paper/translate_batch",
            json={
                "paper_id": "ferro_test_1",
                "relative_path": rel,
                "pages": [1, 2],
                "mode": "page",
            },
        )
        assert r.status_code == 200, r.text
        job = r.json()["job"]
        job_id = job["job_id"]

        deadline = time.monotonic() + 5.0
        final = job
        while time.monotonic() < deadline:
            poll = client.get(f"/api/paper/translate_batch/{job_id}")
            assert poll.status_code == 200
            final = poll.json()["job"]
            if final["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.03)

        assert final["status"] == "completed", final
        assert final["done"] == 2
        assert sorted(translated_pages) == [1, 2]

        # 不存在的任务返 404
        assert client.delete("/api/paper/translate_batch/bt_missing").status_code == 404


def test_csrf_escapes_html_and_handles_ipv6_host_port(sandbox, monkeypatch):
    assert "&lt;script&gt;" in escape_html_text('<script>alert(1)</script>')
    assert extract_host_port("[::1]") == 80
    assert extract_host_port("[::1]:5173") == 5173
    assert extract_host_port("127.0.0.1:5000") == 5000

    with TestClient(app_mod.app) as client:
        r = client.post(
            "/api/auth/verify",
            headers={"Origin": 'http://evil.com/<script>alert(1)</script>', "Host": "[::1]:5000"},
            json={"token": "x"},
        )
        assert r.status_code == 403
        assert "<script>" not in r.text
        assert "&lt;script&gt;" in r.text


def test_auth_verify_rate_limits_repeated_failures(sandbox, monkeypatch):
    monkeypatch.setattr(app_mod, "_LAN_TOKEN", "correct-secret-token")
    AUTH_LIMITER._failures.clear()
    try:
        with TestClient(app_mod.app) as client:
            for _ in range(10):
                bad = client.post("/api/auth/verify", json={"token": "wrong"})
                assert bad.status_code == 401
            blocked = client.post("/api/auth/verify", json={"token": "wrong"})
            assert blocked.status_code == 429
            assert "Retry-After" in blocked.headers
    finally:
        AUTH_LIMITER._failures.clear()


def test_pdf_endpoints_enforce_vault_content_dir_scope(sandbox):
    (sandbox / "logs").mkdir(exist_ok=True)
    (sandbox / "logs" / "secret.pdf").write_bytes(_minimal_pdf_bytes())
    (sandbox / "root_leak.pdf").write_bytes(_minimal_pdf_bytes())

    with TestClient(app_mod.app) as client:
        for path in ("logs/secret.pdf", "root_leak.pdf"):
            assert client.get("/api/paper/serve_pdf", params={"relative_path": path}).status_code == 400
            assert client.get("/api/pdf/info", params={"relative_path": path}).status_code == 400
            assert client.get("/api/paper/outline", params={"relative_path": path}).status_code == 400
            assert client.post("/api/paper/open_vault_pdf", json={"relative_path": path}).status_code == 400


def test_open_vault_pdf_warm_cache_skips_pdf_read_and_extraction(sandbox, monkeypatch):
    _seed_vault(sandbox)
    rel = "01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/2025_FerroFET_Study.pdf"
    pdf_path = sandbox / rel
    monkeypatch.setattr(
        app_mod.pdf_cache,
        "read_cache",
        lambda p: [{"page": 1, "text": "Introduction\nCached text without glyphs.", "glyphs": []}],
    )
    monkeypatch.setattr(
        app_mod,
        "extract_text_from_pdf",
        lambda b: (_ for _ in ()).throw(AssertionError("缓存命中时不该重抽")),
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: (_ for _ in ()).throw(AssertionError("缓存命中且无缺失符号时不该读整份 PDF")),
    )

    with TestClient(app_mod.app) as client:
        r = client.post("/api/paper/open_vault_pdf", json={"relative_path": rel})
        assert r.status_code == 200, r.text
        assert r.json()["total_pages"] == 1


def test_sse_stream_emits_heartbeat_and_safe_error():
    async def _collect():
        async def _slow_gen():
            await asyncio.sleep(0.06)
            yield "你好"
            raise RuntimeError("boom D:\\secret\\path")

        out = []
        async for chunk in app_mod._stream_llm_event_gen(_slow_gen(), heartbeat_seconds=0.02):
            out.append(chunk)
        return out

    chunks = asyncio.run(_collect())
    assert ": heartbeat\n\n" in chunks
    assert any('"text": "你好"' in c for c in chunks)
    assert any('"error"' in c for c in chunks)
    assert not any("D:\\secret" in c for c in chunks)


def test_sync_files_release_records_cas_conflict_in_errors(sandbox, monkeypatch):
    sync_root = sandbox / "sync_root"
    fdir = sync_root / "files"
    fdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_mod, "_sync_root", lambda: sync_root)
    safe = "paper_conflict_1"
    (fdir / f"{safe}.json").write_text(json.dumps({"paper_id": safe, "relative_path": "01_Literature/a.pdf"}), encoding="utf-8")
    (fdir / f"{safe}.pdf").write_bytes(b"%PDF-1.4 data")

    from fastapi import HTTPException
    monkeypatch.setattr(
        app_mod,
        "_cas_update_file_meta",
        lambda s, b: (_ for _ in ()).throw(HTTPException(status_code=409, detail="冲突测试")),
    )

    with TestClient(app_mod.app) as client:
        r = client.post("/api/sync/files/release", json={"paper_id": safe})
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "partial"
        assert len(d["errors"]) == 1
        assert "冲突测试" in d["errors"][0]["error"]


def test_export_offline_excludes_replaced_pdfs_and_bakes_blocks(tmp_path, monkeypatch):
    from microbench import export_offline as eo

    orig_dir = tmp_path / "01_Literature" / "01_Device_TCAD_器件仿真" / "01_论文原文_PDF"
    rep_dir = tmp_path / "01_Literature" / "01_Device_TCAD_器件仿真" / "00_中译替换版_PDF"
    snap_dir = tmp_path / "logs" / "_pdf_snapshots"
    orig_dir.mkdir(parents=True)
    rep_dir.mkdir(parents=True)
    snap_dir.mkdir(parents=True)

    (orig_dir / "paper.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF")
    (rep_dir / "paper_中译替换版.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF")
    (orig_dir / "legacy_中译替换版.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF")

    ext_ver = eo.current_extractor_version()
    blk_ver = eo.current_blocks_version()
    assert ext_ver is not None and blk_ver is not None

    snap_payload = {
        "extractor_version": ext_ver,
        "pages": [
            {
                "page_num": 1,
                "translation": "第一页译文",
                "blocks_version": blk_ver,
                "blocks": [{"i": 1, "text": "Orig", "translation": "译"}],
            },
            {
                "page_num": 2,
                "translation": "第二页译文",
                "blocks_version": blk_ver + 99,
                "blocks": [{"i": 1, "text": "Stale", "translation": "旧"}],
            },
        ],
    }
    (snap_dir / "paper_key.json").write_text(json.dumps(snap_payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(eo, "REPO", tmp_path)
    pdfs = eo.collect_pdfs()
    assert list(pdfs.keys()) == ["01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/paper.pdf"]

    trans = eo.collect_translations()
    assert trans == {"paper_key": {"1": "第一页译文", "2": "第二页译文"}}

    blocks = eo.collect_block_translations()
    assert blocks == {
        "paper_key": {
            "1": {"blocks": [{"i": 1, "text": "Orig", "translation": "译"}], "version": blk_ver},
        }
    }

    assert "<\\/style" in eo._escape_inline_tag_close("body{} </style><script>", "style")
    assert "<\\/script" in eo._escape_inline_tag_close("var x = '</script>';", "script")
