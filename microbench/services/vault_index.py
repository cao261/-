# -*- coding: utf-8 -*-
"""SQLite WAL vault 索引、frontmatter 元数据编辑与后台批量翻译任务管理。

⚠ 严格沙箱隔离：所有路径由调用方传入 `base_dir`（或从 `microbench.app.BASE_DIR`
   动态解析），测试替换 `BASE_DIR` 时绝不碰到真实库 `logs/vault_index.db`。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .. import generator as gen_mod
from .. import paperid

_INDEX_TTL_SECONDS = 5.0
_INDEX_LOCK = threading.Lock()
_LAST_SYNC: dict[str, float] = {}
_VALID_STATUSES = ("待精读", "精读中", "已精读", "已复现")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    fd_taken = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd_taken = True
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        if not fd_taken:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _db_path(base_dir: Path) -> Path:
    return Path(base_dir) / "logs" / "vault_index.db"


def _connect(base_dir: Path) -> sqlite3.Connection:
    dbp = _db_path(base_dir)
    dbp.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dbp), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
            paper_id TEXT PRIMARY KEY,
            relative_path TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            mtime_iso TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notes (
            relative_path TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            tags TEXT NOT NULL,
            status TEXT NOT NULL,
            category TEXT NOT NULL,
            body_excerpt TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            mtime_iso TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            paper_id TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            total INTEGER NOT NULL,
            done INTEGER NOT NULL,
            failed INTEGER NOT NULL,
            current_page INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            pages_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return conn


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    if not m:
        return {}, text
    fm: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        key = k.strip().lower()
        val = v.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [
                it.strip().strip('"').strip("'")
                for it in val[1:-1].split(",")
                if it.strip()
            ]
            fm[key] = items
        else:
            fm[key] = val.strip('"').strip("'")
    return fm, text[m.end():]


def _extract_excerpt(body: str, limit: int = 360) -> str:
    lines = []
    for raw in (body or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("---") or s.startswith("#") or s.startswith("<!--"):
            continue
        s = re.sub(r"[*_`>\[\]]+", "", s).strip()
        if s:
            lines.append(s)
        if sum(len(x) for x in lines) >= limit:
            break
    return " ".join(lines)[:limit]


def _iso_from_mtime(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def sync_index(base_dir: Path, force: bool = False) -> dict:
    """增量同步 vault 中的 PDF 与 Markdown 笔记进 SQLite 索引。"""
    base_dir = Path(base_dir)
    key = str(base_dir)
    now_mono = time.monotonic()
    with _INDEX_LOCK:
        if not force and (now_mono - _LAST_SYNC.get(key, 0.0)) < _INDEX_TTL_SECONDS:
            if _db_path(base_dir).exists():
                conn = _connect(base_dir)
                try:
                    p_cnt = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
                    n_cnt = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
                    return {"papers": p_cnt, "notes": n_cnt, "cached": True}
                finally:
                    conn.close()

        conn = _connect(base_dir)
        try:
            existing_papers = {
                r["relative_path"]: (r["size"], r["mtime_ns"])
                for r in conn.execute("SELECT relative_path, size, mtime_ns FROM papers")
            }
            existing_notes = {
                r["relative_path"]: (r["size"], r["mtime_ns"])
                for r in conn.execute("SELECT relative_path, size, mtime_ns FROM notes")
            }

            seen_papers: set[str] = set()
            seen_notes: set[str] = set()
            now_iso = _dt.datetime.now().isoformat(timespec="seconds")

            lit_dir = base_dir / "01_Literature"
            if lit_dir.is_dir():
                for pdf in lit_dir.rglob("*.pdf"):
                    if not pdf.is_file() or any(pt.startswith(".") or pt == "05_译文替换版_PDF" for pt in pdf.parts):
                        continue
                    rel = str(pdf.relative_to(base_dir)).replace("\\", "/")
                    seen_papers.add(rel)
                    try:
                        st = pdf.stat()
                    except OSError:
                        continue
                    if existing_papers.get(rel) == (st.st_size, st.st_mtime_ns):
                        continue
                    rel_parts = rel.split("/")
                    cat = rel_parts[1] if len(rel_parts) >= 2 else ""
                    pid = paperid.paper_id_for(rel)
                    conn.execute(
                        """
                        INSERT INTO papers (paper_id, relative_path, title, category, size, mtime_ns, mtime_iso, indexed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            paper_id=excluded.paper_id,
                            title=excluded.title,
                            category=excluded.category,
                            size=excluded.size,
                            mtime_ns=excluded.mtime_ns,
                            mtime_iso=excluded.mtime_iso,
                            indexed_at=excluded.indexed_at
                        """,
                        (pid, rel, pdf.stem, cat, st.st_size, st.st_mtime_ns, _iso_from_mtime(st.st_mtime), now_iso),
                    )

            for root_name in ("01_Literature", "02_Reproduction", "03_Knowledge", "04_Weekly_Reports"):
                root_dir = base_dir / root_name
                if not root_dir.is_dir():
                    continue
                for md in root_dir.rglob("*.md"):
                    if not md.is_file() or any(pt.startswith(".") for pt in md.parts):
                        continue
                    rel = str(md.relative_to(base_dir)).replace("\\", "/")
                    seen_notes.add(rel)
                    try:
                        st = md.stat()
                    except OSError:
                        continue
                    if existing_notes.get(rel) == (st.st_size, st.st_mtime_ns):
                        continue
                    try:
                        raw = md.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        try:
                            raw = md.read_text(encoding="gbk", errors="replace")
                        except OSError:
                            continue
                    fm, body = _parse_frontmatter(raw)
                    title = str(fm.get("title") or md.stem)
                    tags_val = fm.get("tags") or ""
                    if isinstance(tags_val, list):
                        tags_str = ", ".join(str(x) for x in tags_val if str(x).strip())
                    else:
                        tags_str = str(tags_val)
                    status = str(fm.get("status") or "")
                    rel_parts = rel.split("/")
                    cat = str(fm.get("category") or (rel_parts[1] if len(rel_parts) >= 2 else root_name))
                    excerpt = _extract_excerpt(body)
                    conn.execute(
                        """
                        INSERT INTO notes (relative_path, title, tags, status, category, body_excerpt, size, mtime_ns, mtime_iso)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            title=excluded.title,
                            tags=excluded.tags,
                            status=excluded.status,
                            category=excluded.category,
                            body_excerpt=excluded.body_excerpt,
                            size=excluded.size,
                            mtime_ns=excluded.mtime_ns,
                            mtime_iso=excluded.mtime_iso
                        """,
                        (rel, title, tags_str, status, cat, excerpt, st.st_size, st.st_mtime_ns, _iso_from_mtime(st.st_mtime)),
                    )

            stale_papers = [rp for rp in existing_papers if rp not in seen_papers]
            if stale_papers:
                conn.executemany("DELETE FROM papers WHERE relative_path = ?", [(r,) for r in stale_papers])
            stale_notes = [rp for rp in existing_notes if rp not in seen_notes]
            if stale_notes:
                conn.executemany("DELETE FROM notes WHERE relative_path = ?", [(r,) for r in stale_notes])

            conn.commit()
            _LAST_SYNC[key] = time.monotonic()
            return {"papers": len(seen_papers), "notes": len(seen_notes), "cached": False}
        finally:
            conn.close()


def search_index(
    base_dir: Path,
    q: str = "",
    kind: str = "all",
    category: str = "",
    status: str = "",
    limit: int = 100,
    refresh: bool = False,
) -> dict:
    """从 SQLite 索引执行跨文献/笔记快速检索与维度过滤。"""
    sync_stats = sync_index(base_dir, force=refresh)
    tokens = [t.strip().lower() for t in re.split(r"\s+", q or "") if t.strip()]
    cap = max(1, min(int(limit or 100), 300))
    conn = _connect(base_dir)
    try:
        items: list[dict] = []
        if kind in ("all", "pdf"):
            rows = conn.execute(
                "SELECT paper_id, relative_path, title, category, size, mtime_iso FROM papers ORDER BY mtime_iso DESC"
            ).fetchall()
            for r in rows:
                if category and category != r["category"]:
                    continue
                if status:
                    continue
                hay = f"{r['title']} {r['category']} {r['relative_path']}".lower()
                if tokens and not all(tok in hay for tok in tokens):
                    continue
                items.append({
                    "kind": "pdf",
                    "paper_id": r["paper_id"],
                    "relative_path": r["relative_path"],
                    "title": r["title"],
                    "category": r["category"],
                    "status": "",
                    "tags": [],
                    "snippet": "",
                    "size": r["size"],
                    "mtime": r["mtime_iso"],
                })

        if kind in ("all", "note"):
            rows = conn.execute(
                "SELECT relative_path, title, tags, status, category, body_excerpt, size, mtime_iso FROM notes ORDER BY mtime_iso DESC"
            ).fetchall()
            for r in rows:
                if category and category not in (r["category"], r["relative_path"].split("/")[0]):
                    continue
                if status and status != r["status"]:
                    continue
                hay = f"{r['title']} {r['tags']} {r['category']} {r['body_excerpt']} {r['relative_path']}".lower()
                if tokens and not all(tok in hay for tok in tokens):
                    continue
                tags_list = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
                items.append({
                    "kind": "note",
                    "paper_id": "",
                    "relative_path": r["relative_path"],
                    "title": r["title"],
                    "category": r["category"],
                    "status": r["status"],
                    "tags": tags_list,
                    "snippet": r["body_excerpt"],
                    "size": r["size"],
                    "mtime": r["mtime_iso"],
                })

        items.sort(key=lambda x: x.get("mtime") or "", reverse=True)
        return {
            "total": len(items),
            "items": items[:cap],
            "indexed": sync_stats,
        }
    finally:
        conn.close()


def update_note_meta(
    base_dir: Path,
    relative_path: str,
    status: Optional[str] = None,
    tags: Optional[list[str]] = None,
) -> dict:
    """安全更新笔记 YAML frontmatter 中的 status / tags，并原子写回与刷新索引。"""
    target = gen_mod._safe_path(base_dir, relative_path)
    if not gen_mod.is_vault_content_path(base_dir, target):
        raise ValueError("只能修改 vault 内容目录下的 Markdown 笔记")
    if not target.exists() or not target.is_file() or target.suffix.lower() not in (".md", ".markdown"):
        raise FileNotFoundError(f"笔记不存在: {relative_path}")

    raw = target.read_text(encoding="utf-8")
    clean_status = gen_mod._one_line(status).strip() if status is not None else None
    clean_tags = (
        [gen_mod._one_line(t).strip() for t in tags if gen_mod._one_line(t).strip()]
        if tags is not None
        else None
    )

    m = re.match(r"^(---\s*\n)(.*?)(\n---\s*(?:\n|$))", raw, re.S)
    if m:
        fm_lines = m.group(2).splitlines()
        out_lines = []
        seen_status = False
        seen_tags = False
        for line in fm_lines:
            key = line.partition(":")[0].strip().lower() if ":" in line else ""
            if key == "status" and clean_status is not None:
                out_lines.append(f'status: "{gen_mod._yaml_quoted(clean_status)}"')
                seen_status = True
            elif key == "tags" and clean_tags is not None:
                csv_tags = ", ".join(f'"{gen_mod._yaml_quoted(t)}"' for t in clean_tags)
                out_lines.append(f"tags: [{csv_tags}]")
                seen_tags = True
            else:
                out_lines.append(line)
        if clean_status is not None and not seen_status:
            out_lines.append(f'status: "{gen_mod._yaml_quoted(clean_status)}"')
        if clean_tags is not None and not seen_tags:
            csv_tags = ", ".join(f'"{gen_mod._yaml_quoted(t)}"' for t in clean_tags)
            out_lines.append(f"tags: [{csv_tags}]")
        updated_text = m.group(1) + "\n".join(out_lines) + m.group(3) + raw[m.end():]
    else:
        hdr = ["---", f'title: "{gen_mod._yaml_quoted(target.stem)}"']
        if clean_status is not None:
            hdr.append(f'status: "{gen_mod._yaml_quoted(clean_status)}"')
        if clean_tags is not None:
            csv_tags = ", ".join(f'"{gen_mod._yaml_quoted(t)}"' for t in clean_tags)
            hdr.append(f"tags: [{csv_tags}]")
        hdr.append("---")
        updated_text = "\n".join(hdr) + "\n\n" + raw

    updated_text = gen_mod._safe_frontmatter(updated_text)
    _atomic_write_text(target, updated_text)
    gen_mod._invalidate_vault_stats_cache()
    sync_index(base_dir, force=True)

    fm_after, _ = _parse_frontmatter(updated_text)
    return {
        "status": "success",
        "relative_path": str(target.relative_to(base_dir)).replace("\\", "/"),
        "paper_status": str(fm_after.get("status") or ""),
        "tags": fm_after.get("tags") if isinstance(fm_after.get("tags"), list) else [],
    }


def _invalidate_index_cache(base_dir: Path) -> None:
    with _INDEX_LOCK:
        _LAST_SYNC.pop(str(Path(base_dir)), None)


class BatchTranslateManager:
    """异步批量翻译任务管理器（每轮只推进一页，严格尊重模型限流与取消指令）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancels: dict[str, asyncio.Event] = {}

    def start_job(
        self,
        base_dir: Path,
        paper_id: str,
        relative_path: str,
        pages: list[int],
        mode: str,
        runner,
    ) -> dict:
        unique_pages = sorted({int(p) for p in pages if 1 <= int(p) <= 2000})
        if not unique_pages:
            raise ValueError("没有合法页码可翻译")

        job_id = f"bt_{uuid.uuid4().hex[:12]}"
        now = _dt.datetime.now().isoformat(timespec="seconds")
        conn = _connect(base_dir)
        try:
            conn.execute(
                """
                INSERT INTO jobs (job_id, kind, paper_id, relative_path, mode, status, total, done, failed, current_page, error, pages_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    "translate_batch",
                    paper_id,
                    relative_path,
                    mode,
                    "running",
                    len(unique_pages),
                    0,
                    0,
                    0,
                    "",
                    json.dumps(unique_pages),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        cancel_ev = asyncio.Event()
        with self._lock:
            self._cancels[job_id] = cancel_ev

        async def _worker():
            done = 0
            failed = 0
            last_err = ""
            final_status = "completed"
            try:
                for pno in unique_pages:
                    if cancel_ev.is_set():
                        final_status = "cancelled"
                        break
                    self._update_job(base_dir, job_id, status="running", done=done, failed=failed, current_page=pno)
                    try:
                        await runner(pno)
                        done += 1
                    except asyncio.CancelledError:
                        final_status = "cancelled"
                        break
                    except Exception as exc:
                        failed += 1
                        last_err = str(exc)[:200]
                if cancel_ev.is_set():
                    final_status = "cancelled"
                elif failed > 0 and done == 0:
                    final_status = "failed"
            finally:
                self._update_job(
                    base_dir,
                    job_id,
                    status=final_status,
                    done=done,
                    failed=failed,
                    current_page=0,
                    error=last_err,
                )
                with self._lock:
                    self._tasks.pop(job_id, None)
                    self._cancels.pop(job_id, None)

        task = asyncio.create_task(_worker())
        with self._lock:
            self._tasks[job_id] = task
        return self.get_job(base_dir, job_id) or {"job_id": job_id, "status": "running"}

    def _update_job(
        self,
        base_dir: Path,
        job_id: str,
        *,
        status: str,
        done: int,
        failed: int,
        current_page: int,
        error: Optional[str] = None,
    ) -> None:
        now = _dt.datetime.now().isoformat(timespec="seconds")
        conn = _connect(base_dir)
        try:
            if error is not None:
                conn.execute(
                    "UPDATE jobs SET status=?, done=?, failed=?, current_page=?, error=?, updated_at=? WHERE job_id=?",
                    (status, done, failed, current_page, error, now, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status=?, done=?, failed=?, current_page=?, updated_at=? WHERE job_id=?",
                    (status, done, failed, current_page, now, job_id),
                )
            conn.commit()
        finally:
            conn.close()

    def get_job(self, base_dir: Path, job_id: str) -> Optional[dict]:
        conn = _connect(base_dir)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            return {
                "job_id": row["job_id"],
                "kind": row["kind"],
                "paper_id": row["paper_id"],
                "relative_path": row["relative_path"],
                "mode": row["mode"],
                "status": row["status"],
                "total": row["total"],
                "done": row["done"],
                "failed": row["failed"],
                "current_page": row["current_page"],
                "error": row["error"],
                "pages": json.loads(row["pages_json"] or "[]"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        finally:
            conn.close()

    def cancel_job(self, base_dir: Path, job_id: str) -> Optional[dict]:
        with self._lock:
            ev = self._cancels.get(job_id)
            if ev is not None:
                ev.set()
            t = self._tasks.get(job_id)
            if t is not None and not t.done():
                t.cancel()
        cur = self.get_job(base_dir, job_id)
        if cur is None:
            return None
        if cur["status"] == "running":
            self._update_job(
                base_dir,
                job_id,
                status="cancelled",
                done=cur["done"],
                failed=cur["failed"],
                current_page=0,
            )
            cur = self.get_job(base_dir, job_id)
        return cur


BATCH_MANAGER = BatchTranslateManager()
