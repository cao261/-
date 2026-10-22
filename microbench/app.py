"""
MicroBench V1.1 - FastAPI Local Backend Server.

V1.1 hardening:
- New POST /api/paper/download_pdf endpoint for arXiv PDF auto-download.
- New GET /api/paper/bibtex?arxiv=... or ?doi=... for BibTeX export.
- CORS: only allow localhost origins (was allow_origins=['*'], which violates
  spec when combined with allow_credentials=True).
- All endpoints wrap generator logic with explicit error mapping.
"""

import os
import json
import re
import hmac
import ipaddress
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional

from .paper_fetcher import (
    smart_fetch_paper,
    download_arxiv_pdf,
    fetch_crossref_doi_metadata,
    resolve_doi_landing_url,
    generate_bibtex,
)
from .generator import (
    BASE_DIR,
    get_vault_stats,
    save_literature_card,
    save_bilingual_reading_card,
    save_qa_card,
    create_reproduction_workspace,
    record_troubleshoot,
    generate_weekly_report,
    search_vault,
    ALLOWED_TOPIC_CATEGORIES,
    sanitize_filename,
    ensure_vault_templates,
    _safe_path,             # exported once; used by all vault-file endpoints
)
from .translation import (
    chunk_section_text,
    get_cache,
    build_cross_section_context,
)
from . import llm
from .plot_compare import (
    extract_curve_from_image,
    calibrate as calibrate_points,
    compute_metrics,
    _parse_csv_points,
)
from .paper_text import fetch_full_text, split_into_sections, extract_text_from_pdf
from . import lan as lan_helpers
from .lan import resolve_token as _resolve_lan_token


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    """Release the pooled httpx clients when the server stops.

    Uses the modern `lifespan` hook — `app.on_event("shutdown")` is
    deprecated and emitted a DeprecationWarning on every startup.
    """
    # 把包内模板补进 vault 的 _Templates/（已存在的绝不覆盖）。
    # 全新克隆的仓库里这个目录是空的，用户也就看不到、也改不了自己的模板。
    try:
        written = ensure_vault_templates()
        if written:
            print(f"[MicroBench] 已补齐 vault 模板: {', '.join(written)}", file=sys.stderr)
    except Exception:
        pass
    yield
    try:
        from .llm import close_async_clients
        close_async_clients()
    except Exception:
        pass


app = FastAPI(
    title="MicroBench - 微电子与半导体科研工作台",
    version="1.4.0",
    description="本地学术工作台 API: 文献抓取/全文/PDF/BibTeX/翻译/AI解读/复现工程/踩坑/周报/RAG",
    lifespan=_lifespan,
)

# CORS — only allow localhost (UI is same-origin via FastAPI's static file serving).
# NOTE: allow_methods MUST include DELETE, otherwise the browser refuses the
# preflight for DELETE /api/vault/file and DELETE /api/pdf/snapshot/delete
# whenever the page is loaded from a slightly different origin (e.g. the
# desktop webview on a custom --port).
#
# allow_origin_regex additionally admits private-LAN origins (192.168.x.x /
# 10.x / 172.16-31.x). That is only reachable when the server is actually
# bound to 0.0.0.0, and every non-loopback caller still has to pass the
# token guard below — CORS alone never grants access.
_LAN_ORIGIN_RE = (
    r"^https?://("
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|\[[0-9a-fA-F:]+\]"
    r")(:\d{1,5})?$"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5000",
        "http://localhost:5000",
    ],
    allow_origin_regex=_LAN_ORIGIN_RE,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# CSRF / cross-origin request guard
# ---------------------------------------------------------------------------
# CORS only decides whether the *browser* may hand the response back to the
# requesting page. It does NOT stop a cross-site <form> from *delivering* a
# request to 127.0.0.1:5000 — "simple" POSTs (text/plain, or form-encoded)
# are sent without a preflight, so the side effect happens even though the
# attacker cannot read the answer.
#
# MicroBench's mutating endpoints have real side effects on the user's machine
# and wallet: writing notes into the vault, spawning explorer windows, and
# spending the user's LLM tokens. A hostile web page the user happens to have
# open can drive all of that via a hidden form post.
#
# Guard: every state-changing request must originate from the loopback
# interface, on the very port this server is listening on. We accept
#   - no Origin and no Referer at all  -> non-browser client (curl/Postman/IDE)
#   - Origin/Referer whose host is 127.0.0.1 / localhost / [::1] AND whose
#     port matches the port the request arrived on
# Everything else is rejected with 403 before it reaches a handler.
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# The shared token for LAN (tablet) access. Empty means "LAN access is
# closed" — we never fall back to "no token = open". The launcher script
# provisions it into logs/lan_token.txt; MB_LAN_TOKEN overrides it.
_LAN_TOKEN = _resolve_lan_token()


def _is_loopback_host(host: str) -> bool:
    """True for 127.0.0.0/8, localhost and ::1."""
    h = (host or "").strip().lower()
    if h in _LOOPBACK_HOSTS:
        return True
    if h.startswith("127."):
        parts = h.split(".")
        if len(parts) == 4:
            try:
                return all(0 <= int(p) <= 255 for p in parts[1:])
            except ValueError:
                return False
    return False


def _is_remote_peer(host: str) -> bool:
    """True only for a real network peer outside the loopback interface.

    This is the gate for every LAN-specific rule (token, same-origin
    relaxation). A peer address that is not an IP address at all cannot be
    a machine on the Wi-Fi: Starlette's in-process TestClient reports the
    literal string "testclient", and uvicorn always reports the socket's
    real peer IP. So "not an IP" means "not a network client" and the
    strict loopback rules apply — exactly as they did before LAN mode
    existed.
    """
    h = (host or "").strip()
    if not h:
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return not ip.is_loopback


def _origin_is_local(origin: str, allowed_ports: set) -> bool:
    """True if `origin` is the loopback interface on a port we serve."""
    if not origin or origin == "null":
        # "null" is what a sandboxed iframe / file:// page sends. It is NOT
        # our own page, so treat it as hostile.
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    # The UI is always served from the same origin as the API, so the port
    # must match one we are actually listening on. A different loopback
    # port is a different app.
    return port in allowed_ports


def _origin_matches_host(origin: str, host_header: str) -> bool:
    """True if `origin` and the Host header address the same host:port.

    This is the LAN counterpart of _origin_is_local(). A cross-site form
    post from evil.com targets http://192.168.1.101:5000, so the browser
    sends `Origin: https://evil.com` while `Host: 192.168.1.101:5000` — the
    two do not match and the request is rejected. The page controls the
    target URL, never the Host header.
    """
    if not origin or origin == "null":
        return False
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    origin_host = (parsed.hostname or "").lower()
    try:
        origin_port = parsed.port
    except ValueError:
        return False
    if origin_port is None:
        origin_port = 443 if parsed.scheme == "https" else 80

    hh = (host_header or "").strip().lower()
    if not hh:
        return False
    if hh.startswith("["):
        # IPv6 literal: [::1]:5000
        end = hh.find("]")
        if end == -1:
            return False
        host_part = hh[1:end]
        port_part = hh[end + 1:].lstrip(":")
    elif ":" in hh:
        host_part, _, port_part = hh.rpartition(":")
    else:
        host_part, port_part = hh, ""
    host_part = host_part.strip("[]")
    try:
        host_port = int(port_part) if port_part else None
    except ValueError:
        return False
    if host_part != origin_host:
        return False
    # A Host header without a port implies the default port for the scheme.
    if host_port is None:
        return True
    return host_port == origin_port


def _token_supplied(request: Request) -> str:
    """The token the client presented, from the header or the cookie.

    Two channels on purpose:
      * the `X-MB-Token` header — set by JS, used by the pairing page and by
        any scripted client
      * the `mb_token` cookie — set by the server after a successful
        pairing, HttpOnly so page scripts cannot read it, and SameSite=Lax
        so browsers refuse to attach it to cross-site requests
    """
    header = (request.headers.get("x-mb-token") or "").strip()
    if header:
        return header
    cookie = (request.cookies.get("mb_token") or "").strip()
    return cookie


def _token_valid(request: Request) -> bool:
    """Constant-time comparison of the presented token against the secret."""
    if not _LAN_TOKEN:
        return False
    supplied = _token_supplied(request)
    if not supplied:
        return False
    return hmac.compare_digest(supplied, _LAN_TOKEN)


def _client_is_local(request: Request) -> bool:
    """True when the caller is this machine (loopback) or an in-process client."""
    client = request.client
    return not _is_remote_peer(client.host if client else "")


def _set_token_cookie(response, token: str) -> None:
    response.set_cookie(
        key="mb_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 180,  # half a year
        path="/",
    )


@app.middleware("http")
async def csrf_origin_guard(request: Request, call_next):
    """Reject cross-origin state-changing requests (see block comment above).

    Two paths, because "this machine" and "my tablet" need different
    definitions of "same origin":

      * loopback client -> the Origin/Referer host must literally be a
        loopback name on a port we serve. This is what defeats DNS
        rebinding (evil.com resolving to 127.0.0.1), because the browser
        would send `Origin: https://evil.com`.

      * LAN client -> the Origin/Referer host:port must equal the Host
        header the browser connected to. Same-origin requests pass; a
        cross-site form post from evil.com does not.

    Both paths are additionally covered by the token guard above, so even a
    same-origin-looking request from a LAN device needs the secret.
    """
    if request.method.upper() not in _SAFE_METHODS:
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        client_host = request.client.host if request.client else ""
        host_header = request.headers.get("host") or ""

        if _is_remote_peer(client_host):
            # LAN client: same-origin only. The Origin/Referer host:port has
            # to equal the Host header the browser actually connected to.
            if origin:
                if not _origin_matches_host(origin, host_header):
                    return HTMLResponse(
                        f"<h1>403</h1><p>拒绝跨站请求 (Origin: {origin})。</p>",
                        status_code=403,
                    )
            elif referer:
                ref_origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
                if not _origin_matches_host(ref_origin, host_header):
                    return HTMLResponse(
                        f"<h1>403</h1><p>拒绝跨站请求 (Referer: {referer})。</p>",
                        status_code=403,
                    )
        else:
            # Local / in-process client: the Origin/Referer host must
            # literally be a loopback name on a port we serve.
            #
            # The ports this server answers on: the socket it is bound to, and
            # the port in the Host header (which is what the browser actually
            # used to reach us). The Host header cannot be forged by a
            # cross-site page — the page controls the *target* URL, not the
            # Host of the request it triggers.
            allowed_ports = set()
            server = request.scope.get("server") or ()
            if len(server) > 1 and server[1]:
                allowed_ports.add(int(server[1]))
            try:
                if ":" in host_header:
                    allowed_ports.add(int(host_header.rsplit(":", 1)[1]))
                elif host_header:
                    allowed_ports.add(80)
            except ValueError:
                pass
            if not allowed_ports:
                allowed_ports = {5000}

            if origin:
                if not _origin_is_local(origin, allowed_ports):
                    return HTMLResponse(
                        f"<h1>403</h1><p>拒绝跨站请求 (Origin: {origin})。</p>",
                        status_code=403,
                    )
            elif referer:
                ref_origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
                if not _origin_is_local(ref_origin, allowed_ports):
                    return HTMLResponse(
                        f"<h1>403</h1><p>拒绝跨站请求 (Referer: {referer})。</p>",
                        status_code=403,
                    )
            # No Origin AND no Referer -> not a browser form post. Allow.
    return await call_next(request)


# NOTE: registered *after* csrf_origin_guard on purpose. FastAPI inserts each
# @app.middleware at the front of the stack, so the last one registered is the
# outermost and therefore runs first. We want the token check to run before any
# origin reasoning, so every authentication failure surfaces as the same 401
# JSON body instead of a mix of 401 and 403 HTML pages.
@app.middleware("http")
async def lan_token_guard(request: Request, call_next):
    """Require the shared token for every non-loopback client.

    Loopback callers (this PC) are never asked for a token, so desktop use is
    completely unaffected. Everyone else must present the shared token — that
    covers reads as well as writes, because the vault itself is worth
    protecting.

    `/api/auth/*` is exempt so the tablet can ask "do I need a token?" and
    then hand one in.
    """
    path = request.url.path
    if path.startswith("/api/") and not path.startswith("/api/auth/"):
        if not _client_is_local(request):
            if not _token_valid(request):
                return JSONResponse(
                    {
                        "detail": "需要访问口令",
                        "error": "token_required",
                        "hint": "在平板上输入 PC 窗口显示的口令，或用带 ?k= 的配对链接打开",
                    },
                    status_code=401,
                )
    return await call_next(request)


STATIC_DIR = Path(__file__).resolve().parent / "static"

# Files under static/ that may be fetched directly by the browser. The app
# itself is a single self-contained index.html, so this whitelist only has to
# cover the PWA assets.
_PWA_ASSETS = {
    "manifest.webmanifest": "application/manifest+json",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "apple-touch-icon.png": "image/png",
}

# ---------------------------------------------------------------------------
# Vendored front-end libraries (/static/vendor)
#
# index.html is served by the "/" route, but the vendored front-end libraries
# (PDF.js + KaTeX under static/vendor/) need a real HTTP path so the browser
# can load them. They used to come from cdn.jsdelivr.net, which is blocked on
# this machine -> `pdfjsLib` was undefined -> "PDF.js 未加载" in the reader.
# Serving them locally makes the workbench fully offline-capable.
#
# Only the vendor/ subtree is mounted on purpose: the rest of static/
# (index.html, pair.html) keeps being served by the routes below, so the LAN
# pairing gate and the PWA whitelist are unaffected.
# ---------------------------------------------------------------------------
_VENDOR_DIR = STATIC_DIR / "vendor"
if _VENDOR_DIR.is_dir():
    app.mount(
        "/static/vendor",
        StaticFiles(directory=str(_VENDOR_DIR)),
        name="static-vendor",
    )


def _pairing_page_response(request: Request, error: str = ""):
    """The small "enter your access code" page a fresh tablet sees.

    Served instead of the app shell so an unpaired device never loads 6 000
    lines of UI that would immediately fail every API call.
    """
    page = STATIC_DIR / "pair.html"
    if not page.exists():
        return HTMLResponse(
            "<h1>需要访问口令</h1>"
            "<p>请在 PC 上用 start_workbench_lan.bat 启动，并用它显示的口令配对。</p>",
            status_code=401,
        )
    html = page.read_text(encoding="utf-8")
    if not _LAN_TOKEN:
        notice = "服务端没有配置局域网口令，LAN 访问已关闭。请在 PC 上用 start_workbench_lan.bat 重新启动。"
    elif error:
        notice = error
    else:
        notice = ""
    html = html.replace("{{NOTICE}}", notice)
    html = html.replace(
        "{{HOST}}",
        (request.headers.get("host") or "127.0.0.1").split(":")[0],
    )
    return HTMLResponse(html, status_code=403 if not _LAN_TOKEN else 200)


# ---------------------------------------------------------------------------
# Readable validation errors
# ---------------------------------------------------------------------------
# FastAPI's default 422 body is a list of dicts:
#   {"detail":[{"loc":["body","pages",0,"page_num"],"msg":"...","type":"..."}]}
# The frontend does `new Error(result.detail || "…")`, so a list gets
# stringified to "[object Object]" and the user sees a useless toast.
# Flatten it into a single human-readable line instead.
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "body")
        msg = err.get("msg", "参数无效")
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "请求参数无效 — " + "; ".join(parts) if parts else "请求参数无效"
    return JSONResponse(status_code=422, content={"detail": detail})


async def _stream_llm_event_gen(chunk_iter, meta=None):
    """Wrap a chat_stream_async() iterator into SSE format.

    Yields lines like:
        data: {"meta": {...}}\\n\\n     (optional initial event with model info)
        data: {"text": "..."}\\n\\n     (per content chunk)
        data: [DONE]\\n\\n              (on normal completion)
        data: {"cancelled": true}\\n\\n (on asyncio.CancelledError)
        data: {"error": "..."}\\n\\n    (on LLMError)

    Always emits a final terminal event so the frontend can cleanly
    close its ReadableStream without hanging on EOF.

    Note: this generator runs inside the StreamingResponse body. When
    the client disconnects, Starlette closes the response, which cancels
    this generator — chat_stream_async() inside raises CancelledError
    and the upstream httpx stream is closed.
    """
    try:
        if meta:
            yield f"data: {json.dumps({'meta': meta}, ensure_ascii=False)}\n\n"
        async for chunk in chunk_iter:
            yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        # Client disconnected — Starlette already closed the response.
        # Swallow without emitting (writing more would just buffer/throw).
        return
    except llm.LLMError as e:
        yield f'data: {json.dumps({"error": str(e)}, ensure_ascii=False)}\n\n'


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class FetchPaperRequest(BaseModel):
    identifier: str = Field(..., min_length=3, max_length=200)

class SavePaperRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    authors: Optional[str] = Field(default="", max_length=2000)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    venue: Optional[str] = Field(default="", max_length=300)
    doi: Optional[str] = Field(default="", max_length=200)
    arxiv: Optional[str] = Field(default="", max_length=50)
    topic_category: Optional[str] = Field(default="01_Device_TCAD_器件仿真", max_length=100)
    tags: Optional[str] = Field(default="TCAD", max_length=500)
    abstract: Optional[str] = Field(default="", max_length=10000)
    status: Optional[str] = Field(default="待精读", max_length=50)
    # Optional user-chosen destination inside the topic folder. Empty = default behaviour.
    # subfolder: a single relative path component (no / or ..), e.g. "周报01" or "GAAFET组会".
    # filename_override: full filename WITHOUT .md extension, e.g. "2024_GAAFET_Benchmark".
    # Empty string = server picks the default name.
    subfolder: Optional[str] = Field(default="", max_length=80,
                                     description="相对 topic_category 的子文件夹(可选,留空用默认)")
    filename_override: Optional[str] = Field(default="", max_length=200,
                                              description="自定义文件名(不带 .md 后缀,可选)")

class CreateReproductionRequest(BaseModel):
    project_name: str = Field(..., min_length=1, max_length=80)
    target_paper: Optional[str] = Field(default="", max_length=300)
    project_type: Optional[str] = Field(default="TCAD", max_length=10)
    toolchain: Optional[str] = Field(default="Sentaurus TCAD 2023", max_length=200)
    description: Optional[str] = Field(default="", max_length=2000)
    repo_url: Optional[str] = Field(default="", max_length=500)

class TroubleshootRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=100)
    category: str = Field(..., min_length=1, max_length=50)
    error_keyword: str = Field(..., min_length=1, max_length=200)
    raw_error: str = Field(..., min_length=1, max_length=10000)
    root_cause: str = Field(..., min_length=1, max_length=5000)
    solution: str = Field(..., min_length=1, max_length=5000)

class SummarizeRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    authors: Optional[str] = Field(default="", max_length=2000)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    venue: Optional[str] = Field(default="", max_length=300)
    abstract: Optional[str] = Field(default="", max_length=10000)

class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    top_k: Optional[int] = Field(default=5, ge=1, le=20)

class CalibrateRequest(BaseModel):
    pixel_points: list = Field(..., min_length=1, max_length=10000)
    ref_a: dict  # {px, py, rx, ry}
    ref_b: dict

class CompareRequest(BaseModel):
    # paper/repro can be supplied as either explicit points or CSV strings (or both).
    # The endpoint merges them, so we keep these optional with no min_length.
    paper_points: list = Field(default_factory=list, max_length=10000)
    repro_points: list = Field(default_factory=list, max_length=10000)
    paper_csv: Optional[str] = Field(default=None, max_length=200000)
    repro_csv: Optional[str] = Field(default=None, max_length=200000)
    pass_pct: Optional[float] = Field(default=5.0, ge=0.1, le=100.0)
    warn_pct: Optional[float] = Field(default=20.0, ge=0.1, le=100.0)

class FetchFullRequest(BaseModel):
    identifier: str = Field(..., min_length=3, max_length=300,
                            description="arXiv ID, DOI, 或 PDF 直接 URL")

class TranslateRequest(BaseModel):
    section_title: str = Field(..., min_length=1, max_length=200)
    # Large sections (Intro 30K+ chars) are valid; we cap at 60K on the server
    # before sending to LLM (see translation logic below).
    section_text: str = Field(..., min_length=10, max_length=60000)
    section_key: Optional[str] = Field(default=None, max_length=50,
                                        description="canonical section key e.g. 'introduction'")
    force: bool = Field(default=False,
                        description="忽略缓存强制重译（点「重新翻译」时用）")

class OpenVaultPdfRequest(BaseModel):
    """Open a PDF that already exists inside the vault (no upload, no copy)."""
    relative_path: str = Field(..., min_length=3, max_length=500)
    title: Optional[str] = Field(default=None, max_length=300)


class ExplainRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    sections: list = Field(..., min_length=1, max_length=20,
                            description="paper sections [{title, text}, ...]")
    paper_title: Optional[str] = Field(default="", max_length=300)


class ExplainSelectionRequest(BaseModel):
    selected_text: str = Field(..., min_length=2, max_length=15000,
                               description="用户划选的原文或译文片段")
    question: str = Field(..., min_length=1, max_length=1000,
                          description="用户针对该片段提出的问题")
    paper_title: Optional[str] = Field(default="", max_length=300)
    surrounding_context: Optional[str] = Field(default="", max_length=50000,
                                                description="上下文背景 (如所在章节或页面文本)")
    source_type: Optional[str] = Field(default="pdf", max_length=50,
                                       description="pdf / translation / original")
    feedback: Optional[str] = Field(default="", max_length=2000,
                                    description="用户对上一轮回答的反馈(为空=首次提问)。"
                                                "后端会把这段内容塞进 user_msg,让 LLM 知道用户为什么"
                                                "不满意、这次要怎么调整。")
    prior_answer: Optional[str] = Field(default="", max_length=8000,
                                        description="上一轮的回答全文(供 LLM 知道之前答了什么)")
    force_scope: Optional[str] = Field(default="", max_length=20,
                                        description="上下文预算档位 (term|mid|wide|full)。"
                                                    "客户端按所选 scope 构造 surrounding_context 并随请求下发; "
                                                    "服务端据此对上下文做**上限截断**"
                                                    "(term=2K / mid=8K / wide=20K / full=50K 字符), "
                                                    "防止客户端或路由器的预算失控。"
                                                    "留空则按 mid 的上限处理。")


class TranslatePageRequest(BaseModel):
    """Translate one page of a paper (used by PDF dual-column reader).
    `paper_id` is a stable identifier (e.g. arXiv ID or DOI) — used to
    key the page-level translation cache so re-opening a paper is instant.
    """
    # 上限放大到 300：paper_id 现在可能是中文路径派生出的短哈希（前端
    # 用 _shortPaperId 生成），也可能是 arXiv ID / DOI。200 会让合法请求
    # 直接 422（之前整页翻译全部失败就是这个原因）。
    paper_id: str = Field(..., min_length=3, max_length=300)
    page: int = Field(..., ge=1, le=200, description="1-based page number")
    text: str = Field(..., min_length=10, max_length=15000,
                      description="Full page text (single chunk from pdfplumber)")
    force: bool = Field(default=False,
                        description="忽略缓存强制重译（点「重新翻译」时用）")
    relative_path: Optional[str] = Field(
        default=None, max_length=500,
        description=("PDF 在 vault 中的相对路径。提供时服务端会用 pdfplumber "
                     "重新抽取该页文本（正确恢复丢失的单词空格 + 按栏顺序），"
                     "比浏览器 PDF.js 抓到的文本质量高得多。"),
    )


class TranslationCacheBatchItem(BaseModel):
    section_key: str = Field(default="", max_length=80)
    text: str = Field(default="", max_length=60000)
    model: str = Field(default="", max_length=80,
                       description="preferred model; empty = active model")


class TranslationCacheBatchRequest(BaseModel):
    keys: list[TranslationCacheBatchItem] = Field(..., min_length=1, max_length=200,
                       description="list of {section_key, text, model}")


class PasteTextRequest(BaseModel):
    """User pastes raw paper text (e.g. copy from PDF reader) for AI analysis.
    Useful when PDF can't be fetched (paywalled IEEE etc.)."""
    title: str = Field(..., min_length=1, max_length=300)
    raw_text: str = Field(..., min_length=100, max_length=200000)
    question: Optional[str] = Field(default=None, max_length=500,
                                      description="若指定, 直接调用 LLM 回答; 否则返回拆分后的章节")

class SplitTextRequest(BaseModel):
    """Split already-pasted raw text into sections (no PDF download)."""
    raw_text: str = Field(..., min_length=100, max_length=200000)

class ExportBilingualRequest(BaseModel):
    """Export bilingual reading notes directly to Obsidian vault."""
    title: str = Field(..., min_length=1, max_length=500)
    topic_category: Optional[str] = Field(default="01_Device_TCAD_器件仿真", max_length=100)
    sections: list = Field(..., min_length=1, description="List of section dicts with title, text, translation")
    authors: Optional[str] = Field(default="", max_length=2000)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    qa_records: Optional[list] = Field(default=None, description="List of Q&A interaction records to bundle into note")
    subfolder: Optional[str] = Field(default="", max_length=80,
                                     description="相对 02_双语精读笔记 的子文件夹(可选)")
    filename_override: Optional[str] = Field(default="", max_length=200,
                                              description="自定义文件名(不带 .md)")

class ExportQARequest(BaseModel):
    """Export a single Q&A record directly into 04_AI划词答疑 in Obsidian vault."""
    title: str = Field(..., min_length=1, max_length=500, description="论文标题")
    question: str = Field(..., min_length=1, max_length=2000, description="用户提问")
    selection: Optional[str] = Field(default="", max_length=15000, description="选中的原文/译文")
    answer: str = Field(..., min_length=1, max_length=50000, description="AI 的解答")
    mode: Optional[str] = Field(default="explain", max_length=50, description="答疑模式: explain, math, experiment")
    topic_category: Optional[str] = Field(default="01_Device_TCAD_器件仿真", max_length=100)
    year: Optional[int] = Field(default=2024, ge=1900, le=2100)
    source_pdf: Optional[str] = Field(default=None, max_length=300)
    subfolder: Optional[str] = Field(default="", max_length=80,
                                     description="相对 04_AI划词答疑 的子文件夹(可选)")
    filename_override: Optional[str] = Field(default="", max_length=200,
                                              description="自定义文件名(不带 .md)")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class AuthVerifyBody(BaseModel):
    token: str = Field(default="", max_length=200, description="局域网访问口令")


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    """Tell the client whether it needs the shared token, and whether it has it.

    The tablet calls this on boot. If `auth_required` is true and `ok` is
    false, the UI shows the token prompt instead of the dashboard.
    """
    loopback = _client_is_local(request)
    if loopback:
        # This PC never needs a token; desktop behaviour is untouched.
        return {
            "auth_required": False,
            "ok": True,
            "loopback": True,
            "token_configured": bool(_LAN_TOKEN),
            "server": "microbench",
        }
    return {
        "auth_required": True,
        "ok": _token_valid(request),
        "loopback": False,
        "token_configured": bool(_LAN_TOKEN),
        "server": "microbench",
    }


@app.post("/api/auth/verify")
async def api_auth_verify(request: Request, payload: AuthVerifyBody):
    """Accept a token the user typed in on the tablet.

    On success the token is stored in an HttpOnly, SameSite=Lax cookie so
    the browser attaches it to every later request on its own — no page
    script ever has to see it, and cross-site requests never carry it.
    """
    if not _LAN_TOKEN:
        return JSONResponse(
            {
                "detail": "服务端未配置局域网口令，LAN 访问已关闭",
                "error": "token_not_configured",
            },
            status_code=403,
        )
    supplied = (payload.token or "").strip()
    if supplied and hmac.compare_digest(supplied, _LAN_TOKEN):
        response = JSONResponse({"ok": True})
        _set_token_cookie(response, supplied)
        return response
    return JSONResponse(
        {"detail": "口令不正确", "error": "token_invalid"},
        status_code=401,
    )


@app.get("/", response_class=HTMLResponse)
async def serve_index(request: Request, k: Optional[str] = None):
    """Serve the app shell — or the pairing page for an unpaired tablet.

    A pairing link looks like `http://192.168.1.101:5000/?k=<token>`.
    The token is validated here, parked in an HttpOnly cookie and the URL is
    rewritten without `?k=` so the secret does not linger in the address bar,
    the browser history or a screenshot.
    """
    if k and not _client_is_local(request) and _LAN_TOKEN:
        if hmac.compare_digest(k.strip(), _LAN_TOKEN):
            response = RedirectResponse(url="/", status_code=303)
            _set_token_cookie(response, k.strip())
            return response
        # Wrong token in the link -> fall through to the pairing page, which
        # shows an explicit "口令不正确" instead of a silent blank screen.
        return _pairing_page_response(request, error="配对链接里的口令不正确")

    if not _client_is_local(request) and not _token_valid(request):
        return _pairing_page_response(request)

    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse("<h1>MicroBench static/index.html not found</h1>", status_code=404)
    # No-cache headers so users always get the latest HTML/JS after edits.
    # Without this, browsers may serve a stale cached version that references
    # old endpoints (e.g. export.arxiv.org HTTP path that times out).
    response = FileResponse(index_path, media_type="text/html")
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/manifest.webmanifest")
async def serve_manifest():
    """PWA manifest — lets the tablet install the workbench to its home screen."""
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        return JSONResponse({"detail": "manifest not found"}, status_code=404)
    return FileResponse(
        path,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/pwa/{filename}")
async def serve_pwa_asset(filename: str):
    """Serve the PWA icons (and nothing else) from static/.

    Whitelisted by name — there is no path traversal surface here because
    the filename never touches the filesystem as a path.
    """
    media_type = _PWA_ASSETS.get(filename)
    if not media_type:
        raise HTTPException(status_code=404, detail="Not found")
    path = STATIC_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "max-age=86400"})


@app.get("/api/stats")
def api_stats():
    """Vault counters for the dashboard.

    Deliberately counters-only. get_vault_stats() now returns the FULL
    (untruncated) literature list so that /api/vault/literature can
    paginate on top of it; shipping that list here as well meant the
    dashboard re-serialised every note on every page mount (~6 KB for 23
    notes, and growing linearly). The feed uses /api/vault/literature, so
    the list was dead weight here.
    """
    try:
        stats = get_vault_stats()
        return {
            "literature_total": stats.get("literature_total", 0),
            "reproduction_total": stats.get("reproduction_total", 0),
            "troubleshoot_count": stats.get("troubleshoot_count", 0),
            "weekly_count": stats.get("weekly_count", 0),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 vault 统计失败: {str(e)}")


@app.get("/api/vault/literature")
async def api_vault_literature(
    limit: int = Query(default=20, ge=1, le=200, description="每页条目数"),
    offset: int = Query(default=0, ge=0, description="跳过前面 N 条"),
    include_qa: bool = Query(default=False, description="是否包含 04_AI划词答疑/ 子目录"),
):
    """
    Paginated literature feed for the dashboard's "最新文献 & 进度流" panel.

    Returns:
      - items:        当前页条目
      - total:        总条目数 (全部 literature, 不分 include_qa)
      - filtered_total: 经过 include_qa 过滤后的条目总数
      - has_more:     offset + items.length < filtered_total

    Frontend replaces the old /api/stats feed with this endpoint so the
    "加载更多" button can stream additional pages without re-walking the disk.
    """
    try:
        # Reuse get_vault_stats to keep the directory scan in one place,
        # then apply our own pagination on top.
        stats = get_vault_stats()
        all_items = stats.get("literature_list", [])
        if include_qa:
            filtered = all_items
        else:
            # Same rule the old frontend used: hide QA cards unless asked.
            filtered = [
                it for it in all_items
                if "04_AI划词答疑" not in str(it.get("path", "")).replace("\\", "/")
            ]
        page = filtered[offset:offset + limit]
        return {
            "items": page,
            "total": len(all_items),
            "filtered_total": len(filtered),
            "offset": offset,
            "limit": limit,
            "has_more": (offset + len(page)) < len(filtered),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文献列表失败: {str(e)}")

@app.post("/api/paper/fetch")
def api_fetch_paper(req: FetchPaperRequest):
    """Fetch paper metadata (arXiv / DOI / Crossref).

    Sync `def` on purpose — smart_fetch_paper() performs several
    blocking HTTP calls (arXiv, then Semantic Scholar, then Crossref),
    each with a 15-20s timeout. As an `async def` it pinned the event
    loop for the whole chain, freezing every other request (including
    the dashboard's live counters and the SSE readers).
    """
    try:
        # 抓取走 arXiv / Crossref 外网，慢响应确实存在 —— 但本端点是同步 def，
        # FastAPI 已经把它丢进 anyio 线程池执行，慢响应不会冻结事件循环，
        # 所以这里直接调用即可，不需要再套一层 asyncio.to_thread。
        data = smart_fetch_paper(req.identifier)
        # Also try to resolve landing URL for DOI papers (for manual download link)
        if data.get("doi") and not data.get("arxiv"):
            try:
                data["landing_url"] = resolve_doi_landing_url(data["doi"])
            except Exception:
                # Non-fatal — landing URL is best-effort
                data["landing_url"] = None
        return {"status": "success", "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"参数无效: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"抓取文献失败: {str(e)}")

@app.post("/api/paper/save")
async def api_save_paper(req: SavePaperRequest):
    try:
        res = save_literature_card(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存文献卡片失败: {str(e)}")


@app.get("/api/paper/folders")
async def api_paper_folders(
    topic_category: str = Query(..., min_length=1, max_length=100,
                                description="分类, e.g. 01_Device_TCAD_器件仿真"),
    kind: str = Query(default="literature_cards",
                       description="literature_cards (03_文献速览卡片 子目录) / "
                                   "literature_pdfs (01_论文原文_PDF 子目录) / "
                                   "any (整个 topic_category 下所有子目录)"),
):
    """
    List existing subfolders inside the chosen topic folder.
    Used by the dashboard '保存位置' dropdown — no manual typing needed
    when the folder already exists.
    """
    from .generator import ALLOWED_TOPIC_CATEGORIES, _safe_path
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"非法 topic_category: '{topic_category}'")
    try:
        if kind == "literature_cards":
            base = _safe_path(BASE_DIR, "01_Literature", topic_category, "03_文献速览卡片")
        elif kind == "literature_pdfs":
            base = _safe_path(BASE_DIR, "01_Literature", topic_category, "01_论文原文_PDF")
        elif kind == "any":
            base = _safe_path(BASE_DIR, "01_Literature", topic_category)
        else:
            raise HTTPException(status_code=400, detail=f"不支持的 kind: {kind}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not base.exists():
        return {"topic_category": topic_category, "kind": kind, "folders": []}
    folders = sorted([p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")])
    return {"topic_category": topic_category, "kind": kind, "folders": folders}

@app.post("/api/paper/export_bilingual")
async def api_export_bilingual(req: ExportBilingualRequest):
    """
    Export bilingual reading card with English + Chinese sections
    directly into 01_Literature in Obsidian vault.
    """
    try:
        res = save_bilingual_reading_card(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出双语卡片失败: {str(e)}")


@app.post("/api/paper/export_qa")
async def api_export_qa(req: ExportQARequest):
    """
    Export single selection Q&A note into 01_Literature/{category}/04_AI划词答疑/
    """
    try:
        res = save_qa_card(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出答疑笔记失败: {str(e)}")




@app.get("/api/paper/download_pdf")
async def api_download_pdf(
    identifier: str = Query(..., min_length=3, max_length=200, description="arXiv ID 或 DOI"),
    topic_category: str = Query(
        "01_Device_TCAD_器件仿真",
        description="目标分类, 必须在 ALLOWED_TOPIC_CATEGORIES 内",
    ),
    title: str = Query(..., min_length=1, max_length=300, description="用于 PDF 文件命名"),
    year: Optional[int] = Query(default=2024, ge=1900, le=2100),
):
    """
    Download a PDF to the appropriate literature folder.
    Only supports arXiv direct PDF. For DOI papers, returns landing_url
    so the frontend can prompt user to download manually.
    """
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"非法 topic_category: '{topic_category}'",
        )

    is_arxiv = "arxiv" in identifier.lower() or bool(
        re.search(r"^\d{4}\.\d{4,5}", identifier.strip())
    )
    if not is_arxiv:
        # For DOI, return landing URL instead of trying direct PDF
        try:
            landing = await asyncio.to_thread(resolve_doi_landing_url, identifier)
            return {
                "status": "manual_required",
                "reason": "DOI 出版商通常需要浏览器渲染/登录, 无法直接下载 PDF",
                "landing_url": landing,
            }
        except ValueError as e:
            # Bad DOI format → user error, not upstream error
            raise HTTPException(status_code=400, detail=f"参数无效: {str(e)}")
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"无法解析 DOI 落地页: {str(e)}",
            )

    # arXiv path: build safe target path inside BASE_DIR
    from .generator import sanitize_filename, _safe_path
    safe_title = sanitize_filename(title)
    target_dir = _safe_path(BASE_DIR, "01_Literature", topic_category, "01_论文原文_PDF")
    target_dir.mkdir(parents=True, exist_ok=True)
    pdf_name = f"{year}_{safe_title[:30]}.pdf"
    save_to = target_dir / pdf_name
    # Re-validate (defense in depth)
    save_to = _safe_path(target_dir, pdf_name)

    try:
        # 下载走 arXiv 外网，放线程里执行
        info = await asyncio.to_thread(download_arxiv_pdf, identifier, save_to)
        return {
            "status": "success",
            "file_name": pdf_name,
            "relative_path": str(save_to.relative_to(BASE_DIR)).replace("\\", "/"),
            "bytes": info["bytes"],
            "source": info["source"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"下载 PDF 失败: {str(e)}")


@app.get("/api/paper/serve_pdf")
async def api_serve_pdf(
    relative_path: str = Query(..., min_length=3, max_length=500,
                                description="相对 BASE_DIR 的 PDF 路径"),
):
    """
    Serve a PDF file from inside BASE_DIR (safe path validation).

    Used by the PDF.js dual-column reader to load the original PDF
    without exposing the filesystem.
    """
    try:
        # Defensive: _safe_path rejects '..' and absolute paths
        pdf_path = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"PDF 不存在: {relative_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail=f"不是 PDF 文件: {relative_path}")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/api/vault/file")
async def api_vault_file(
    relative_path: str = Query(..., min_length=3, max_length=500,
                                description="相对 BASE_DIR 的 .md / .txt 路径"),
):
    """
    Serve a text file from inside the vault (safe path validation).

    Used by the dashboard feed to open literature / QA / bilingual notes
    in a new browser tab. Only .md / .txt files inside BASE_DIR are
    served; PDFs and other binaries must use /api/paper/serve_pdf.
    """
    try:
        file_path = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {relative_path}")
    suffix = file_path.suffix.lower()
    if suffix not in (".md", ".markdown", ".txt"):
        raise HTTPException(status_code=400,
                            detail=f"不支持的文件类型: {suffix} (仅支持 .md/.txt)")
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = file_path.read_text(encoding="gbk", errors="replace")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/vault/file")
async def api_vault_delete_file(
    relative_path: str = Query(..., min_length=3, max_length=500,
                               description="相对 BASE_DIR 的 .md / .txt 路径"),
):
    """
    Soft-delete a single text file from the vault (sent to OS Recycle Bin via
    send2trash). The file can be restored manually from the Recycle Bin if
    the delete was accidental.

    Used by the dashboard feed's per-row 🗑️ button. Path validation mirrors
    GET /api/vault/file: only _safe_path-resolved paths under BASE_DIR with
    .md/.txt/.markdown suffix are deletable. PDFs and other binaries are
    deliberately rejected — those should be managed through the Obsidian
    app or file manager.
    """
    try:
        file_path = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {relative_path}")
    suffix = file_path.suffix.lower()
    if suffix not in (".md", ".markdown", ".txt"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持删除的文件类型: {suffix} (仅支持 .md/.txt/.markdown)",
        )
    # Refuse to delete the vault-level shared knowledge files so a stray
    # "feed delete" can never wipe knowledge the rest of the app depends on.
    rel_str = str(file_path.relative_to(BASE_DIR)).replace("\\", "/").strip("/")
    protected_prefixes = (
        "_Templates/",
        "03_Knowledge/",
        "00_Dashboard/",
        "04_Weekly_Reports/",
    )
    if any(rel_str.startswith(p) for p in protected_prefixes):
        raise HTTPException(
            status_code=403,
            detail=f"该目录受保护,请在 Obsidian 或文件管理器中手动删除: {rel_str}",
        )
    try:
        try:
            from send2trash import send2trash
            send2trash(str(file_path))
            deleted_to = "recycle_bin"
        except ImportError:
            # send2trash not installed → fall back to permanent delete with
            # a best-effort confirmation. Logged so users can wire up
            # `pip install send2trash` for the safer path.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "send2trash not installed; performing permanent delete. "
                "Install with `pip install send2trash` for Recycle Bin support."
            )
            file_path.unlink()
            deleted_to = "permanent"
        return {"status": "success", "path": rel_str, "deleted_to": deleted_to}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")


@app.post("/api/vault/reveal")
async def api_vault_reveal(
    relative_path: str = Query(..., min_length=3, max_length=500,
                               description="相对 BASE_DIR 的路径"),
):
    """
    Reveal a file in the OS file manager:
      - Windows: `explorer /select,"<abs>"` (highlights the file)
      - macOS:   `open -R "<abs>"`
      - Linux:   best-effort `xdg-open` on the parent folder

    Returns the command that was executed (best-effort, may differ by OS).
    """
    import sys, subprocess
    try:
        file_path = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {relative_path}")

    abs_path = str(file_path)
    cmd = None
    try:
        if sys.platform.startswith("win"):
            # Explorer's /select switch takes ONE argument: everything after
            # the first comma is the path, so a path containing commas is
            # fine as long as it stays a single argv element.
            #
            # This used to be `shell=True` with an f-string. Two problems:
            #   1. cmd.exe is inserted into the chain, and it expands %VAR%
            #      even inside double quotes — a file literally named
            #      "100%PATH%.md" would be opened as a different path.
            #   2. the old backslash-escaping of '"' was meaningless: Windows
            #      filenames cannot contain '"' at all, and cmd does not use
            #      backslash escaping anyway.
            # shell=False keeps the path as a single argv element, so no
            # shell metacharacter of any kind is interpreted.
            cmd = ["explorer", f"/select,{abs_path}"]
            subprocess.Popen(cmd)
        elif sys.platform == "darwin":
            cmd = ["open", "-R", abs_path]
            subprocess.Popen(cmd)
        else:
            # Linux: most file managers don't have a "reveal" verb; just open
            # the parent folder via xdg-open and let the user navigate.
            parent = str(file_path.parent)
            cmd = ["xdg-open", parent]
            subprocess.Popen(cmd)
    except FileNotFoundError as e:
        # If explorer/open is not on PATH for some reason, surface the error
        # rather than silently failing.
        raise HTTPException(status_code=500, detail=f"系统工具缺失: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开失败: {e}")

    return {"status": "success", "command": cmd, "abs_path": abs_path}


@app.get("/api/paper/find_pdf")
async def api_find_pdf(
    title: str = Query(..., min_length=1, max_length=300,
                       description="论文标题 (用于模糊匹配本地 PDF)"),
    year: Optional[int] = Query(default=None, ge=1900, le=2100),
):
    """
    Find a local PDF in the vault that matches a paper title.

    Walks 01_Literature/**.pdf and matches by:
      - exact or prefix normalized match
      - substring overlap on alphanumeric normalized stems
      - keyword token overlap (splits on non-alphanumeric chars)
    Returns the relative path of the best match, or null.
    """
    base = BASE_DIR / "01_Literature"
    if not base.exists():
        return {"found": False}

    def _norm(s: str) -> str:
        return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", s).lower()

    title_clean = _norm(title)
    # Tokenize on non-alphanumeric characters, keeping tokens length >= 3
    title_words = [_norm(w) for w in re.split(r"\W+", title) if len(_norm(w)) >= 3][:8]

    best = None
    best_score = 0
    for cat_dir in base.iterdir():
        if not cat_dir.is_dir():
            continue
        for pdf in cat_dir.rglob("*.pdf"):
            stem_clean = _norm(pdf.stem)
            score = 0

            # Year bonus if matched
            if year and pdf.stem.startswith(f"{year}_"):
                score += 15

            # Prefix or substring match
            if title_clean and stem_clean:
                if title_clean[:18] in stem_clean or stem_clean in title_clean:
                    score += 70
                elif title_clean[:10] in stem_clean:
                    score += 45

            # Word token overlap
            word_hits = sum(1 for w in title_words if w and w in stem_clean)
            if word_hits >= 2:
                score += 25 + word_hits * 10
            elif word_hits == 1 and len(title_words) <= 2:
                score += 35

            if score > best_score:
                best_score = score
                best = pdf

    if best and best_score >= 45:
        return {
            "found": True,
            "relative_path": str(best.relative_to(BASE_DIR)).replace("\\", "/"),
            "score": best_score,
            "file_name": best.name,
            "size_bytes": best.stat().st_size,
        }
    return {"found": False}


@app.post("/api/paper/upload_pdf")
async def api_upload_pdf(
    file: UploadFile = File(..., description="本地论文 PDF 文件"),
    topic_category: str = Query("01_Device_TCAD_器件仿真", description="学术专题归档"),
    title: Optional[str] = Query(default=None, description="论文标题 (可选，留空则自动提取)"),
):
    """
    Upload a local PDF file, extract full text, split into canonical sections,
    and save into 01_Literature for dual-column split-screen reading & AI Q&A.
    """
    if topic_category not in ALLOWED_TOPIC_CATEGORIES:
        topic_category = "01_Device_TCAD_器件仿真"

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="仅支持上传 PDF 文件 (.pdf)")

    # Read in chunks with a running cap. `await file.read()` with no size
    # argument buffers the *entire* body in memory before the limit is
    # checked, so a 2GB upload could exhaust RAM (and the 40MB check only
    # fired after the fact). Streaming keeps the peak at limit + 1 chunk.
    MAX_UPLOAD_BYTES = 40 * 1024 * 1024
    buf = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="PDF 文件不能超过 40MB")
    pdf_bytes = bytes(buf)

    try:
        # pdfplumber parsing is CPU-bound and can take seconds on a large
        # paper — keep it off the event loop.
        pages = await asyncio.to_thread(extract_text_from_pdf, pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF 文本提取失败: {str(e)}")

    if not pages:
        raise HTTPException(status_code=400, detail="未能从 PDF 中识别出有效文字 (可能是扫描图片版 PDF)")

    sections = split_into_sections(pages)
    if not sections:
        full_text = "\n\n".join(p["text"] for p in pages)
        sections = [{
            "section": "full_text",
            "title": "Full Text",
            "text": full_text.strip(),
            "page_start": 1,
            "page_end": len(pages),
        }]

    import datetime
    from .generator import sanitize_filename, _safe_path

    paper_title = (title or "").strip()
    if not paper_title:
        p1_text = pages[0]["text"].strip() if pages else ""
        lines = [l.strip() for l in p1_text.splitlines() if len(l.strip()) > 8]
        if lines and len(lines[0]) < 180:
            paper_title = lines[0]
        else:
            paper_title = Path(file.filename).stem.replace("_", " ").replace("-", " ")

    safe_title = sanitize_filename(paper_title)
    year = datetime.date.today().year
    pdf_name = f"{year}_{safe_title[:50].rstrip('_')}.pdf"

    target_dir = _safe_path(BASE_DIR, "01_Literature", topic_category, "01_论文原文_PDF")
    target_dir.mkdir(parents=True, exist_ok=True)
    save_to = _safe_path(target_dir, pdf_name)
    save_to.write_bytes(pdf_bytes)
    pdf_saved_path = str(save_to.relative_to(BASE_DIR)).replace("\\", "/")

    sections_out = []
    for s in sections:
        sections_out.append({
            "section": s["section"],
            "title": s["title"],
            "number": s.get("number", ""),
            "text": s["text"],
            "page_start": s["page_start"],
            "page_end": s["page_end"],
            "char_count": len(s["text"]),
        })

    return {
        "status": "success",
        "title": paper_title,
        "pdf_path": pdf_saved_path,
        "file_name": pdf_name,
        "total_pages": len(pages),
        "char_count": sum(len(p["text"]) for p in pages),
        "sections": sections_out,
        "topic_category": topic_category,
    }


@app.get("/api/paper/list_vault_pdfs")
async def api_list_vault_pdfs():
    """
    List every PDF already living in the vault's 01_Literature tree.

    Lets the user open a paper that is already archived (or linked in from
    an external library) without re-uploading it — uploading would copy the
    file and duplicate it inside the vault.
    """
    from .generator import _safe_path
    root = _safe_path(BASE_DIR, "01_Literature")
    items: list[dict] = []
    try:
        for pdf in root.rglob("*.pdf"):
            if not pdf.is_file():
                continue
            rel = str(pdf.relative_to(BASE_DIR)).replace("\\", "/")
            try:
                stat = pdf.stat()
                size_kb = round(stat.st_size / 1024)
            except OSError:
                size_kb = 0
            parts = rel.split("/")
            topic = parts[1] if len(parts) > 2 else "01_Literature"
            items.append({
                "relative_path": rel,
                "name": pdf.name,
                "stem": pdf.stem,
                "topic": topic,
                "size_kb": size_kb,
            })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"扫描 vault 失败: {e}")

    items.sort(key=lambda x: (x["topic"], x["name"]))
    return {"status": "success", "count": len(items), "pdfs": items}


@app.post("/api/paper/open_vault_pdf")
async def api_open_vault_pdf(req: OpenVaultPdfRequest):
    """
    Open a PDF that already lives in the vault: parse it into sections and
    hand back exactly what the upload endpoint returns, without copying the
    file anywhere.
    """
    from .generator import _safe_path
    try:
        pdf_path = _safe_path(BASE_DIR, req.relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail=f"vault 中找不到该 PDF: {req.relative_path}")

    try:
        pdf_bytes = await asyncio.to_thread(pdf_path.read_bytes)
        pages = await asyncio.to_thread(extract_text_from_pdf, pdf_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF 文本提取失败: {str(e)}")

    if not pages:
        raise HTTPException(status_code=400, detail="未能从 PDF 中识别出有效文字 (可能是扫描图片版 PDF)")

    sections = split_into_sections(pages)
    if not sections:
        full_text = "\n\n".join(p["text"] for p in pages)
        sections = [{
            "section": "full_text",
            "title": "Full Text",
            "text": full_text.strip(),
            "page_start": 1,
            "page_end": len(pages),
        }]

    title = (req.title or "").strip() or pdf_path.stem
    sections_out = [{
        "section": s["section"],
        "title": s["title"],
        "number": s.get("number", ""),
        "text": s["text"],
        "page_start": s["page_start"],
        "page_end": s["page_end"],
        "char_count": len(s["text"]),
    } for s in sections]

    return {
        "status": "success",
        "title": title,
        "pdf_path": req.relative_path,
        "file_name": pdf_path.name,
        "total_pages": len(pages),
        "char_count": sum(len(p["text"]) for p in pages),
        "sections": sections_out,
    }


@app.get("/api/paper/bibtex")
def api_export_bibtex(
    arxiv: Optional[str] = Query(default=None, max_length=50),
    doi: Optional[str] = Query(default=None, max_length=200),
    bib_key: Optional[str] = Query(default=None, max_length=80),
):
    """Generate a BibTeX entry from arXiv ID or DOI.

    Sync `def` on purpose — the metadata fetch is blocking network I/O.
    """
    if not arxiv and not doi:
        raise HTTPException(status_code=400, detail="必须提供 arxiv 或 doi 之一")
    try:
        # 元数据抓取走 arXiv / Crossref 外网。本端点是同步 def，FastAPI 已把
        # 它放进 anyio 线程池，慢响应不会冻结事件循环，直接调用即可。
        if arxiv:
            meta = smart_fetch_paper(arxiv)
        else:
            meta = fetch_crossref_doi_metadata(doi)
        bib = generate_bibtex(meta, bib_key=bib_key)
        return {"status": "success", "bibtex": bib}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"参数无效: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"生成 BibTeX 失败: {str(e)}")

@app.post("/api/reproduction/create")
async def api_create_reproduction(req: CreateReproductionRequest):
    try:
        res = create_reproduction_workspace(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建复现工程失败: {str(e)}")

@app.post("/api/troubleshoot/add")
async def api_add_troubleshoot(req: TroubleshootRequest):
    try:
        res = record_troubleshoot(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"记录排错日志失败: {str(e)}")

@app.post("/api/weekly/generate")
async def api_generate_weekly():
    try:
        res = generate_weekly_report()
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成周报失败: {str(e)}")

# ---------------------------------------------------------------------------
# AI / RAG endpoints (require LLM_API_KEY or AGNES_API_KEY env var)
# ---------------------------------------------------------------------------

@app.get("/api/llm/status")
async def api_llm_status():
    """Report full LLM dual-model configuration & failover status."""
    st = llm.get_llm_status()
    # Backward compatible fields + rich failover info
    return {
        "configured": st["configured"],
        "model": st["active_model"],
        "base_url": st["primary"]["base_url"] if not st["is_degraded"] else st["fallback"]["base_url"],
        "active_model": st["active_model"],
        "active_provider": st["active_provider"],
        "is_degraded": st["is_degraded"],
        "degraded_reason": st["degraded_reason"],
        "degraded_at": st["degraded_at"],
        "primary": st["primary"],
        "fallback": st["fallback"],
        "next_probe_in_seconds": st["next_probe_in_seconds"],
        "rate_limit": st["rate_limit"],
    }

@app.post("/api/llm/probe")
def api_llm_probe():
    """Manually probe MiniMax to test if quota has reset and trigger auto-upgrade.

    Deliberately a sync `def` (not `async def`): probe_primary_model() is a
    blocking `requests.post(timeout=15)`. FastAPI runs sync endpoints in the
    anyio worker-thread pool, so a probe no longer freezes the event loop
    (and with it every SSE stream and the dashboard's live counters).
    """
    res = llm.probe_primary_model()
    return res


@app.post("/api/paper/summarize")
def api_summarize_paper(req: SummarizeRequest):
    """
    Generate a Chinese TL;DR + 3-5 key points + 1 research takeaway
    from paper metadata via LLM.

    If LLM is not configured, returns 200 with a fallback skeleton + an
    inline error message in the result (so the UI can still render gracefully).

    Sync `def` on purpose — llm.summarize_paper() is a blocking HTTP call.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="LLM 未配置。请在 .env 设置 LLM_API_KEY 或 AGNES_API_KEY 后重启服务。",
        )
    try:
        result = llm.summarize_paper(req.model_dump())
        return {"status": "success", "summary": result}
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成摘要失败: {str(e)}")

@app.post("/api/ask")
def api_ask_question(req: AskRequest):
    """
    RAG-style Q&A over the local vault.

    Pipeline:
      1. search_vault() ranks markdown files by keyword score
      2. Top-k chunks + their snippets are injected into the LLM prompt
      3. LLM answers with [[filename]] citations

    Resilience: even if LLM call fails, the matched chunks are returned so
    the user can still browse what their vault contains on the topic.

    Sync `def` on purpose — both search_vault() (full vault scan) and
    llm.answer_with_context() (blocking HTTP) are synchronous.
    """
    chunks = search_vault(req.question, top_k=req.top_k or 5)
    if not chunks:
        return {
            "status": "success",
            "answer": "vault 中未找到与该问题相关的内容。请尝试更换关键词, 或先录入更多文献。",
            "chunks": [],
            "llm_used": False,
            "llm_error": None,
        }
    if not llm.is_configured():
        return {
            "status": "success",
            "answer": (
                f"🔍 找到 {len(chunks)} 条相关文献片段 (LLM 未配置, "
                "未生成综合答案)。配置 LLM_API_KEY 后可启用 AI 综合回答。"
            ),
            "chunks": chunks,
            "llm_used": False,
            "llm_error": None,
        }
    try:
        answer = llm.answer_with_context(req.question, chunks)
        return {
            "status": "success",
            "answer": answer,
            "chunks": chunks,
            "llm_used": True,
            "llm_error": None,
        }
    except llm.LLMError as e:
        # Resilience: return chunks + a clear note about LLM failure.
        # Don't raise 502 — the user still benefits from the RAG hits.
        return {
            "status": "partial",
            "answer": (
                f"⚠️ LLM 调用失败, 但 RAG 检索到 {len(chunks)} 条相关文献片段。"
                f"请检查 .env 中 LLM_BASE_URL / LLM_MODEL_NAME 配置。错误: {str(e)[:200]}"
            ),
            "chunks": chunks,
            "llm_used": False,
            "llm_error": str(e),
        }
    except Exception as e:
        return {
            "status": "partial",
            "answer": f"⚠️ 问答过程出错: {str(e)[:200]}。以下是 vault 检索结果。",
            "chunks": chunks,
            "llm_used": False,
            "llm_error": str(e),
        }


# ---------------------------------------------------------------------------
# Plot digitization + reproduction alignment
# ---------------------------------------------------------------------------

@app.post("/api/plot/extract")
async def api_plot_extract(
    image: UploadFile = File(..., description="PNG/JPG/GIF/BMP chart screenshot"),
    sat_thresh: int = Query(default=60, ge=10, le=200),
    val_thresh: int = Query(default=230, ge=50, le=255),
):
    """
    Extract the dominant colored curve from a chart screenshot.
    Accepts multipart/form-data with field name "image" (PNG/JPG/GIF/BMP).
    Returns pixel-space (x, y) points plus metadata.
    Frontend then calibrates to real coords via /api/plot/calibrate.
    """
    # Stream the upload with a running cap instead of buffering it whole
    # and checking afterwards.
    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    buf = bytearray()
    while True:
        chunk = await image.read(1024 * 1024)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="图片不能超过 10MB")
    image_bytes = bytes(buf)

    try:
        # The digitiser walks every pixel (it used to be a pure-Python
        # double loop taking seconds on a 4K screenshot) — keep it off the
        # event loop.
        result = await asyncio.to_thread(
            extract_curve_from_image,
            image_bytes, sat_thresh=sat_thresh, val_thresh=val_thresh
        )
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图片解析失败: {str(e)}")

@app.post("/api/plot/calibrate")
async def api_plot_calibrate(req: CalibrateRequest):
    """
    Convert pixel-space points to real data coords using 2 reference points
    (the user clicks on the chart's axes and types in the value at each point).
    """
    try:
        calibrated = calibrate_points(req.pixel_points, req.ref_a, req.ref_b)
        return {"status": "success", "points": calibrated}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"标定失败: {str(e)}")

@app.post("/api/plot/compare")
async def api_plot_compare(req: CompareRequest):
    """
    Compare paper-reported data against reproduction data.

    Input can be either:
      - explicit paper_points / repro_points (list of [x, y])
      - paper_csv / repro_csv strings (parsed on the server)
    """
    paper = req.paper_points
    repro = req.repro_points
    if req.paper_csv:
        paper = paper + _parse_csv_points(req.paper_csv)
    if req.repro_csv:
        repro = repro + _parse_csv_points(req.repro_csv)
    if len(paper) < 2:
        raise HTTPException(status_code=400, detail="paper 数据点不足 (需要 >= 2 个点)")
    if len(repro) < 2:
        raise HTTPException(status_code=400, detail="repro 数据点不足 (需要 >= 2 个点)")

    tolerance = {
        "pass_pct": req.pass_pct or 5.0,
        "warn_pct": req.warn_pct or 20.0,
    }
    try:
        result = compute_metrics(paper, repro, tolerance=tolerance)
        return {"status": "success", **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"对比计算失败: {str(e)}")


# ---------------------------------------------------------------------------
# Paper full-text reading + AI explain (V1.4)
# ---------------------------------------------------------------------------

@app.post("/api/paper/fetch_full")
def api_fetch_full_text(req: FetchFullRequest):
    """
    Fetch the full text of a paper given an arXiv ID, DOI, or URL.
    Extracts PDF, splits into canonical sections (Abstract / Introduction /
    Methods / Results / Discussion / Conclusion / References).

    Response includes:
      - title / authors / year / venue (metadata)
      - source ('arXiv' / 'OpenAccess' / 'URL')
      - sections: list of {section, title, text, page_start, page_end}
      - pdf_size / total_pages / char_count

    Caveats: IEEE / Elsevier / Wiley DOIs are usually paywalled —
    we'll return 400 with a clear message asking the user to either
    paste the PDF URL, use an arXiv preprint, or paste the text directly.

    Sync `def` on purpose — fetch_full_text() does network I/O *and* PDF
    parsing (tens of seconds on a large paper). As `async def` it pinned the
    event loop and froze every other request for the duration.
    """
    try:
        # 全文抓取 = 外网下载 + PDF 解析。本端点是同步 def，FastAPI 已把它放进
        # anyio 线程池执行，慢任务不会冻结事件循环，直接调用即可。
        result = fetch_full_text(req.identifier)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"全文获取失败: {str(e)}")

    # Side-effect: also save the PDF to vault so the dual-column reader can find it.
    # Best-effort: failures here don't fail the overall request.
    pdf_saved_path = None
    if result.get("source") == "arXiv":
        try:
            from .generator import sanitize_filename, _safe_path
            safe_title = sanitize_filename(result["title"])
            topic = "01_Device_TCAD_器件仿真"  # default category for now
            target_dir = _safe_path(BASE_DIR, "01_Literature", topic, "01_论文原文_PDF")
            target_dir.mkdir(parents=True, exist_ok=True)
            pdf_name = f"{result['year']}_{safe_title[:30]}.pdf"
            save_to = _safe_path(target_dir, pdf_name)
            # Re-download to vault only if not already there
            if not save_to.exists():
                from .paper_fetcher import download_arxiv_pdf
                download_arxiv_pdf(result["identifier"], save_to)
            pdf_saved_path = str(save_to.relative_to(BASE_DIR)).replace("\\", "/")
        except Exception:
            # Non-fatal — find_pdf will simply not return a match
            pdf_saved_path = None

    # Strip pdf_bytes from response (too large to JSON-ship)
    sections_out = []
    for s in result["sections"]:
        sections_out.append({
            "section": s["section"],
            "title": s["title"],
            "number": s.get("number", ""),
            "text": s["text"],
            "page_start": s["page_start"],
            "page_end": s["page_end"],
            "char_count": len(s["text"]),
        })
    return {
        "status": "success",
        "identifier": result["identifier"],
        "source": result["source"],
        "title": result["title"],
        "authors": result["authors"],
        "year": result["year"],
        "venue": result["venue"],
        "pdf_size": result["pdf_size"],
        "total_pages": result["total_pages"],
        "char_count": result["char_count"],
        "pdf_path": pdf_saved_path,
        "sections": sections_out,
    }


TRANSLATION_SYSTEM_PROMPT = """你是学术论文翻译助手, 帮助硕博生阅读英文学术论文。

规则:
1. 严格忠于原文, 不要添加原文没有的内容
2. 翻译要学术化、平实, 避免文学化或口语化
3. 必须逐句翻译, 不得省略、跳过或合并任何一句。页眉版权行、作者单位与
   院系大学名称、章节标题、图表说明、编辑与收稿信息, 统统都要翻译
4. 输出必须是完整的中文段落, 不要中英混杂。只有下面这些保留英文:
   - 人名与作者署名
   - 缩略语 / 器件名: ME、PZT、MEMS、FET、GAA-FET、CFET、DIBL、MRAM、FMR、PMN-PT、FeGaB
   - 没有通行中文译法的公认术语 / 模型名: hydrodynamic model、Gilbert damping、
     spin-orbit coupling
   - 图表与文献引用 (Fig. 3、Table II、[14])
   机构与院系名请译成中文, 必要时在括号内附英文原名, 例如:
   "Department of Materials Science and Engineering, Virginia Tech"
   → "弗吉尼亚理工学院材料科学与工程系 (Virginia Tech)"
   其余普通学术名词一律译成中文, 例如:
   magnetization state → 磁化状态 (不要写成 "magnetization state")
   electric field → 电场
   power dissipation → 功率耗散
   multiferroic magnetoelectric materials → 多铁性磁电材料
   ferromagnetic resonance → 铁磁共振
   composite→复合材料, laminate→层状材料, magnetostrictive→磁致伸缩,
   piezoelectric→压电, materials→材料, structure→结构,
   single crystals→单晶, polymers→聚合物, alloys→合金,
   voltage coefficient→电压系数, ceramic→陶瓷
5. 中文输出, Markdown 格式
6. 数学符号保持 LaTeX 形式 (如 "$I_{on}$", "$\\tau$", "$\\lambda$"),
   不要过度转义 (不要写 "\\{" "\\}" "\\/", 直接写 "{" "}" "/")。
   引用上标请写成 "<sup>3,4</sup>" 或 "$^{3,4}$", 不要裸写 "^3,4"
7. 全文的术语、公式写法、符号必须前后一致: 同一个量在全文只能用一种写法
8. 如果原文含图表引用 (如 "Fig. 3", "Table II"), 保留原样
9. 原文是从 PDF 自动抽取的纯文本, 双栏页面的句子顺序可能被打乱、公式可能残缺。
   请直接按语义翻译能读懂的部分, 不要为了重建原文版面结构而反复推敲,
   不要输出分析过程, 只输出译文本身
10. 严禁输出任何解释、推测或括号注释 (例如"原文此处数值缺损, 可能指……")。
    遇到看不懂或残缺的部分, 跳过即可, 绝不要编造数值、公式或术语
11. 原文中的 "[?]" 是 PDF 抽取时无法识别的公式或符号占位符（整段公式通常
    只出现一个 "[?]"，不会出现一串），保留原样即可，不要臆造具体符号，
    也不要展开成多个占位符
12. PDF 抽取常把希腊字母抽成普通字母, 例如磁电电压系数 α_ME 会变成
    "a" + 换行 + "ME" 或 "a_ME"。遇到这种明显失真时, 请按学科标准写法
    还原成规范符号 (如 α_ME), 并统一全文写法
"""


def _translation_deadline_seconds() -> float:
    """单次翻译请求的**总**时限（秒）。

    用户的诉求是「质量优先，但别无限等」：强制思考的模型（M3.1-Flash）整页
    要 45s，一节三段可能要 135s；但不能出现"转十分钟还没完"的情况。

    超时后的行为不是报错，而是**返回已经翻完的部分**并带上 partial 标记，
    前端明确提示「已翻译前 N 段」。可用 LLM_TRANSLATION_DEADLINE 调整
    （秒，30~900，默认 180）。
    """
    raw = (os.getenv("LLM_TRANSLATION_DEADLINE", "") or "").strip()
    if not raw:
        return 180.0
    try:
        return max(30.0, min(float(raw), 900.0))
    except ValueError:
        return 180.0


def _translation_max_tokens(src_len: int, model: str = "") -> int:
    """按源文本长度估算翻译所需的输出预算 (max_tokens)。

    为什么不能固定 4000:
    MiniMax 的思考型模型（M2.7 / M3.1-Flash 强制思考）的**推理 token 与
    正文共用同一个 max_tokens 预算池**。预算给小了，思考先吃光，正文就断在
    半句 —— 前端表现为「已翻译」，但内容是残的。

    实测（2019 IEEE 双栏论文整页，2700 字符）:
        M2.7   开着思考 5.3s，思考文本混在 content 里
        M3     开着思考 16.1s
        M3     关掉思考 3.0s，输出干净
        M3.1-Flash 强制思考，8.9s，且传 thinking.type=disabled 直接 400

    英文 1 字符 ≈ 0.3 个中文 token，译文长度大致是源文的 1/2。1.6 倍系数给
    思考 token 和格式开销留出余量；上下限防止极端输入把预算撑爆。

    强制思考的模型（M3.1-Flash）要再乘 2.5：它的推理量级跟输入长度成正比
    （实测 2627 字符的页面推理了 14688 字符 ≈ 4000 token），按正文需求给
    预算必然被推理吃光 —— 那样会先失败一次再升级重试，白等一倍时间。
    """
    base = max(4000, min(int(src_len * 1.6), 16000))
    if llm.model_forces_thinking(model):
        base = min(int(base * 2.5), llm.MAX_ESCALATION_MAX_TOKENS)
    return base


@app.post("/api/paper/translate_section")
async def api_translate_section(req: TranslateRequest):
    """
    Translate a single section (English → Chinese) via LLM.

    Features (v1.2):
    - Auto-chunks sections >8K chars at paragraph boundaries so the LLM
      sees complete paragraphs (no mid-sentence cutoffs).
    - Concatenates chunk translations in order with blank-line separators.
    - Caches results in-memory by (section_key, text_hash, model) so
      re-clicking translate on an unchanged section returns instantly.

    Falls back gracefully if LLM is not configured: returns 503.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="翻译需要 LLM 配置。请在 .env 中设置 LLM_API_KEY 或 AGNES_API_KEY + LLM_BASE_URL。",
        )

    from .translation import (chunk_section_text, get_cache, merge_translation_parts,
                              sanitize_translation, find_untranslated_runs)
    cache = get_cache()
    curr_st = llm.get_llm_status()
    # 翻译可能走「翻译专用模型」，缓存键和上报都要用它，不能用主模型。
    model = llm.translation_active_model()

    # Cache lookup: same text + same model = same translation
    # force=True 时跳过读缓存（点「重新翻译」必须真的重译，否则用户按了
    # 按钮还是看到同一份坏译文），但写回缓存仍然照常进行。
    if not req.force:
        cached = cache.get(req.section_key or "", req.section_text, model)
        if cached:
            cached["from_cache"] = True
            return cached

    chunks = chunk_section_text(req.section_text)
    was_segmented = len(chunks) > 1
    translated_parts: list[str] = []
    # 单次翻译请求的总时限。质量优先，但不能无限等：超时了就返回已经翻完的
    # 部分 + 明确的「部分完成」标记，而不是继续挂着。
    deadline = time.monotonic() + _translation_deadline_seconds()
    stopped_reason = ""

    try:
        for idx, chunk in enumerate(chunks):
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                stopped_reason = "deadline"
                break
            if was_segmented:
                user_msg = (
                    f"翻译以下学术论文章节 (标题: {req.section_title}, "
                    f"第 {idx + 1}/{len(chunks)} 段):\n\n{chunk}"
                )
            else:
                user_msg = (
                    f"翻译以下学术论文章节 (标题: {req.section_title}):\n\n{chunk}"
                )
            # llm.chat() is a *synchronous* requests-based call with a 120s
            # timeout. Calling it straight from this async def pins the event
            # loop for the whole round trip, so every other endpoint (stats,
            # feed, model probe) stalls while one section translates — and this
            # loop can do it once per chunk. Hand it to a worker thread.
            #
            # NOTE: the `.strip()` must apply to the RESULT, not to the
            # coroutine object returned by asyncio.to_thread. Writing
            # `await asyncio.to_thread(...).strip()` binds .strip() to the
            # coroutine and raises
            #   "'coroutine' object has no attribute 'strip'"
            # — which broke both translation endpoints completely.
            #
            # 单次调用的超时取「剩余时间的一半」：截断后 llm 层还可能抬高预算
            # 重试一次，留一半给它，整条链路才不会越过总时限。
            call_timeout = int(max(20, min(300, remaining / 2)))
            try:
                translated = await asyncio.to_thread(
                    llm.chat,
                    messages=[
                        {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    # 输出预算按分块长度自适应（见 _translation_max_tokens）：
                    # 思考型模型的推理 token 与正文共用同一个 max_tokens 池，固定
                    # 4000 正好是最容易被截断的档位，截断了译文就断在半句。
                    max_tokens=_translation_max_tokens(len(chunk), model),
                    # 翻译要的是「同一段原文 → 同一份译文」，temperature 必须为 0；
                    # 否则每次点「重新翻译」措辞都变，混着未译的元信息看着像时翻时不翻。
                    temperature=0.0,
                    # Generous timeout — MiniMax thinking mode can take 60s
                    # per chunk; multi-chunk translations need headroom.
                    # 强制思考的模型（M3.1-Flash）单次要跑 100~200s，120s 会把
                    # 它砍在半路（砍掉之后拿到的还是残缺译文）。
                    timeout=call_timeout,
                    # 直译任务关掉思考：更快，且不会在推理里打转导致正文为空
                    disable_thinking=llm.translation_disable_thinking(),
                    # 翻译专用模型（LLM_TRANSLATION_MODEL）：M3.1-Flash 强制思考，
                    # 直译会又慢又断句，单独指定一个能关思考的模型更稳。
                    model_name=llm.translation_model_name(),
                    # 整条链路（重试 + 抬预算重试）都受这个时限约束
                    deadline=deadline,
                )
            except llm.LLMError:
                # 前面已经翻完了几段 -> 返回部分结果，别把整节的成果丢掉
                if translated_parts:
                    stopped_reason = "error"
                    break
                raise
            translated_parts.append((translated or "").strip())
    except llm.LLMError as e:
        msg = str(e)
        if ("时限" in msg or "timed out" in msg.lower()
                or time.monotonic() >= deadline):
            # 超时是用户最常遇到的情况，给一句人话，别把 requests 的原始异常
            # 甩到界面上。
            raise HTTPException(
                status_code=504,
                detail=(
                    f"翻译超时（超出单次请求时限 "
                    f"{_translation_deadline_seconds():.0f}s）。"
                    f"可以调大 .env 的 LLM_TRANSLATION_DEADLINE，或换更快的翻译模型。"
                ),
            )
        raise HTTPException(status_code=502, detail=f"LLM 翻译失败: {msg}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"翻译失败: {str(e)}")

    # 合并分块译文（去掉 overlap 造成的重复句）并清洗 LaTeX
    translation = sanitize_translation(merge_translation_parts(translated_parts))
    if not translation:
        raise HTTPException(
            status_code=502,
            detail="翻译结果为空。模型可能把 token 预算耗在推理上没产出正文，"
                   "请重试或点「探测升格」切回主模型。",
        )

    # 完整性校验：模型偶尔会整段漏译（尤其是 PDF 抽取残缺的那些句子）。
    # 提示词管不住，只能在服务端查——发现成段英文就重译一次。
    leaked = find_untranslated_runs(translation)
    if leaked:
        print(
            f"[MicroBench] ⚠️ 译文检测到 {len(leaked)} 段未翻译，重译一次: "
            f"{leaked[0][:60]!r}",
            file=sys.stderr,
        )
        try:
            retry_parts: list[str] = []
            for idx, chunk in enumerate(chunks):
                hint = ""
                if leaked:
                    hint = ("\n\n【注意】上一次翻译漏掉了以下内容，必须完整翻译：\n"
                            + "\n".join(x[:200] for x in leaked[:5]))
                if was_segmented:
                    user_msg = (
                        f"翻译以下学术论文章节 (标题: {req.section_title}, "
                        f"第 {idx + 1}/{len(chunks)} 段):\n\n{chunk}{hint}"
                    )
                else:
                    user_msg = (
                        f"翻译以下学术论文章节 (标题: {req.section_title}):\n\n{chunk}{hint}"
                    )
                retry_parts.append(
                    (
                        await asyncio.to_thread(
                            llm.chat,
                            messages=[
                                {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                                {"role": "user", "content": user_msg},
                            ],
                            max_tokens=_translation_max_tokens(len(chunk), model),
                            temperature=0.0,
                            # 重译同样受总时限约束，别让「重译一次」再拖一轮
                            timeout=int(max(20, min(300, max(1.0, deadline - time.monotonic()) / 2))),
                            disable_thinking=llm.translation_disable_thinking(),
                            # 必须用同一个翻译模型：主模型和翻译模型措辞风格不同，
                            # 混用会让"重译版"看起来像另一份译文
                            model_name=llm.translation_model_name(),
                            deadline=deadline,
                        )
                    ).strip()
                )
            retry_text = sanitize_translation(merge_translation_parts(retry_parts))
            if retry_text and len(retry_text) >= len(translation) * 0.9:
                translation = retry_text
                leaked = find_untranslated_runs(translation)
        except Exception:
            pass  # 重译失败就用第一版，不要因此让整个请求失败

    curr_st = llm.get_llm_status()
    actual_model = llm.translation_active_model()
    result = {
        "status": "success",
        "translation": translation,
        "was_segmented": was_segmented,
        "chunk_count": len(chunks),
        "from_cache": False,
        "model": actual_model,
        "is_degraded": curr_st["is_degraded"],
        # 残留未翻译的英文段数（完整性校验的结果，正常应为 0）
        "untranslated_runs": len(leaked),
        # 部分完成：超出总时限，后面的分段没翻。前端要明确提示，不能让用户
        # 以为整节都翻完了。
        "partial": bool(stopped_reason),
        "translated_chunks": len(translated_parts),
        "stopped_reason": stopped_reason,
    }

    # Store in cache under actual producing model so re-translate is instant
    cache.put(req.section_key or "", req.section_text, actual_model, result)
    return result


EXPLAIN_SYSTEM_PROMPT = """你是学术论文 AI 解读助手, 帮助硕博生深度理解英文学术论文。

回答用户问题时:
1. 基于提供的论文章节内容回答, 不要编造
2. 引用具体章节 (用 [Abstract], [Introduction], [Methods], [Results] 等)
3. 学术化但简洁, 用中文
4. 如果用户问的"创新点"或"方法", 用 bullet 列出
5. 如果章节内容不足以回答, 明确说明
"""


CROSS_QA_SYSTEM_PROMPT = """你是学术论文跨章节综合分析助手, 帮助硕博生综合多个章节信息回答问题。

规则:
1. 综合多个章节回答问题, 不要只复述单一章节
2. 明确标注每个观点来自哪个章节 (用 [Introduction], [Methods], [Results] 等方括号标签)
3. 区分: 论文做了什么 (方法/结果) vs 论文声称了什么 (引言/讨论)
4. 学术化、简洁, 用中文
5. 如果问题涉及对比 (新旧方法对比/预期 vs 实际), 用表格或并列 bullet
6. 如果提供章节不足以回答, 明确指出缺失哪类信息
"""


class CrossQARequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    # Accept either specific section indices OR all sections from the paper
    sections: list = Field(..., min_length=2, max_length=20,
                           description="至少 2 个章节 [{section, title, text}, ...]")
    selected_indices: Optional[list[int]] = Field(default=None,
                                                   description="用户勾选的章节索引列表; 为空则用全部")
    paper_title: Optional[str] = Field(default="", max_length=300)
    per_section_budget: Optional[int] = Field(default=6000, ge=1000, le=12000)


# NOTE: /api/paper/explain and /api/paper/cross_qa non-streaming endpoints
# were removed on 2026-09-24 (audit: 100% dead code — frontend only uses
# the /explain_stream and /cross_qa_stream variants). The Pydantic models
# ExplainRequest and CrossQARequest are kept because the stream endpoints
# reference them.


EXPLAIN_SELECTION_SYSTEM_PROMPT = r"""你是一个微电子与集成电路领域资深学者与论文精读导师。
用户在阅读微电子/半导体器件/TCAD/EDA学术论文时，划选了一段难以理解的文字、公式或技术论述向你提问。

请根据用户划选的内容与论文上下文，提供详实、严谨、易于理解的中文解答：
1. 【直击疑问】：首先明确针对用户提出的问题进行回答，解释重点突出。
2. 【物理背景与器件机制】：如果涉及半导体物理（如载流子输运、能带结构、量子限制、自热效应、短沟道效应、界面缺陷等）或TCAD仿真（SDE几何构建、网格剖分、SDevice物理模型等），请结合专业背景深入透彻剖析。
3. 【术语全称与公式拆解】：对涉及的专业缩写（如 DTCO, GAA-FET, CFET, SDE, SDevice, DIBL, SS）给出中英文全称与物理意义；对公式中的关键变量进行清晰解释，公式使用标准 LaTeX 排版（如 $I_{on}/I_{off}$, $\mu_{eff}$）。
4. 【工程与研究启发】：结合当前先进节点（如 Sub-3nm/2nm）或前沿论文研究范式，指出该段内容对研究者有何启发或实操注意事项。
5. 结构清晰，采用分段 Markdown 格式输出。
"""


# ---------------------------------------------------------------------------
# QA scope router — two-stage LLM call
# ---------------------------------------------------------------------------
# Stage 1 of the selection-QA flow: a small "router" LLM call that reads the
# user's selection + question and decides how much surrounding context to
# attach before the main answer call. Returns one of:
#   - "term" : ~600 chars around the selection (lookups: abbreviations, single
#              symbol definitions, narrow factual questions)
#   - "mid"  : current page/section (single-paragraph explain, mechanism,
#              engineering insight)
#   - "wide" : abstract + active section + conclusion (innovation / method /
#              cross-section reasoning)
#   - "full" : multi-section slice up to 60K chars (whole-paper overview /
#              cross-chapter comparison)
# Why two stages instead of keywords: keyword heuristics (e.g. "创新点" →
# wide) miss open-ended questions like "这种方法在我自己的工艺节点上能用吗"
# which need a full chapter or more. An LLM router costs ~200 tokens and
# 3-8 seconds — far cheaper than attaching a full chapter to a one-line
# abbreviation question and diluting the model's attention.
# ---------------------------------------------------------------------------

QA_SCOPE_ROUTER_PROMPT = r"""你是论文问答的"上下文路由器"。
基于用户划选的片段 + 用户的提问,判断回答这个问题最少需要多少上下文。

可选 mode:
- "term" : 紧邻上下文 ~600-800 字。仅术语查全称、单符号/缩写定义、单值查表。
- "mid"  : 当前章节 4-8K 字。解释单段、物理机制、工程启发、局部推导。
- "wide" : 摘要 + 当前章节 + 结论 12-20K 字。创新点、方法实现、章节间对比、综述式问题。
- "full" : 全文摘要 60K 字。整篇主旨、跨章节贡献对比、"整体讲了什么"。

只看 (selected_text + question) 这两个输入做判断 — 不要编造论文内容。
若问题开放、不能确定 → 选更大一档 (mid 提到 wide,wide 提到 full)。

严格只返回 JSON,不要任何额外文字:
{"mode": "<term|mid|wide|full>", "reason": "<一句话依据>", "budget": <整数字符数>}
"""


class ClassifyQAScopeRequest(BaseModel):
    selected_text: str = Field(..., min_length=1, max_length=15000)
    question: str = Field(..., min_length=1, max_length=1000)
    preset: Optional[str] = Field(default="", max_length=50,
                                   description="当前用户在 UI 上选的 preset (可作 hint,非强制)")


@app.post("/api/paper/classify_qa_scope")
async def api_classify_qa_scope(req: ClassifyQAScopeRequest):
    """
    Two-stage QA flow's stage 1: route a selection-QA request to the right
    context scope (term / mid / wide / full). Uses a small LLM call with
    json_mode so the answer is parseable; falls back to keyword heuristic
    on any parse error or LLM failure so the main QA flow never blocks.
    """
    if not llm.is_configured():
        # No LLM available — caller falls back to its keyword heuristic.
        raise HTTPException(
            status_code=503,
            detail="LLM 未配置,前端将用关键词启发式 fallback",
        )

    preset_hint = (req.preset or "").strip()
    user_msg = (
        f"【用户划选片段】:\n```text\n{req.selected_text.strip()[:2000]}\n```\n\n"
        f"【用户提问】:\n{req.question.strip()}\n\n"
        + (f"【UI preset 提示 (非强制)】: {preset_hint}\n" if preset_hint else "")
        + "\n请按 system prompt 中的 4 档选择最合适的 mode,只返回 JSON。"
    )

    try:
        # Synchronous LLM call from an async endpoint — offload to a thread so
        # the router's 25s budget never blocks the whole server.
        raw = await asyncio.to_thread(
            llm.chat,
            messages=[
                {"role": "system", "content": QA_SCOPE_ROUTER_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            # Thinking-mode LLMs (MiniMax-M2.7) emit a <think> block before
            # the actual JSON; budget must cover both. Empirically ~600
            # output tokens is enough for the think + JSON payload.
            max_tokens=800,
            temperature=0.1,
            json_mode=True,
            timeout=25,
        )
    except Exception as e:
        # Stage 1 failure is non-fatal — caller will fall back to keywords.
        raise HTTPException(status_code=502, detail=f"router LLM 调用失败: {e}")

    # Strip any leading <think>...</think> block (M2.7 always emits one
    # even when response_format=json_object is set). Without this the
    # JSON parser hits `<think>` first and errors out.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    # Strip thinking block if present
    cleaned = re.sub(r"<think>[\s\S]*?</think>\s*", "", cleaned).strip()
    # Some models still emit a stray prefix before the JSON object —
    # find the first '{' and last '}' and parse the slice between them.
    if not cleaned.startswith("{"):
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first >= 0 and last > first:
            cleaned = cleaned[first:last + 1]
    try:
        data = json.loads(cleaned)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"router 返回非 JSON: {cleaned[:200]} (parse err: {e})",
        )

    mode = str(data.get("mode", "")).strip().lower()
    if mode not in ("term", "mid", "wide", "full"):
        raise HTTPException(status_code=502, detail=f"router mode 非法: {mode}")

    try:
        budget = int(data.get("budget", 0))
    except Exception:
        budget = 0
    # Clamp into reasonable bounds so a runaway router can't blow up the
    # downstream prompt budget.
    budget = max(300, min(budget, 80000))

    return {
        "mode": mode,
        "reason": str(data.get("reason", "")).strip()[:200],
        "budget": budget,
        "preset": preset_hint,
    }





# ---------------------------------------------------------------------------
# Streaming variants (Stage 2: SSE for the cancel-button endpoints)
#
# Each stream endpoint returns a StreamingResponse with text/event-stream
# content type. The frontend reads via fetch + ReadableStream and appends
# text chunks progressively, with ✕ 取消 button wired to AbortController.
#
# Non-streaming counterparts were removed on 2026-09-24 (100% dead code —
# frontend only calls the /..._stream variants).
# ---------------------------------------------------------------------------


@app.post("/api/paper/explain_stream")
async def api_explain_stream(req: ExplainRequest, request: Request):
    """Streaming variant of /api/paper/explain using SSE.

    Cancellation note: Starlette's StreamingResponse already cancels the
    body generator when the client closes the connection (via the
    listen_for_disconnect task_group), which propagates CancelledError
    through chat_stream_async → httpx.AsyncClient.stream automatically.
    Adding our own watcher interferes with Starlette's receive channel
    and causes the stream to be cancelled prematurely on first call.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="AI 解读需要 LLM 配置。请在 .env 中设置 LLM_API_KEY。",
        )

    sections = req.sections[:20]
    context_parts = []
    total_chars = 0
    for s in sections:
        title = s.get("title", s.get("section", "?"))
        text = (s.get("text") or "")[:6000]
        chunk = f"[{s.get('section', '?')}] {title}\n{text}"
        if total_chars + len(chunk) > 24000:
            break
        context_parts.append(chunk)
        total_chars += len(chunk)
    context = "\n\n---\n\n".join(context_parts)
    paper_label = req.paper_title or "未命名论文"
    user_msg = (
        f"论文标题: {paper_label}\n\n"
        f"--- 论文内容 (节选) ---\n{context}\n--- 论文内容结束 ---\n\n"
        f"用户问题: {req.question}"
    )

    async def event_gen():
        try:
            chunk_iter = llm.chat_stream_async(
                messages=[
                    {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=1200,
                temperature=0.3,
            )
            async for chunk in _stream_llm_event_gen(chunk_iter):
                yield chunk
        except asyncio.CancelledError:
            # Starlette already closed the response on client disconnect;
            # swallow without emitting (the connection is gone).
            return
        except llm.LLMError as e:
            yield f'data: {json.dumps({"error": str(e)}, ensure_ascii=False)}\n\n'

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/paper/cross_qa_stream")
async def api_cross_qa_stream(req: CrossQARequest, request: Request):
    """Streaming variant of /api/paper/cross_qa using SSE.

    Starlette's StreamingResponse handles client disconnect natively via
    the receive channel — no extra watcher needed (see api_explain_stream).
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="跨章节问答需要 LLM 配置。请在 .env 中设置 LLM_API_KEY。",
        )
    if len(req.sections) < 2:
        raise HTTPException(
            status_code=400,
            detail="跨章节问答至少需要 2 个章节。",
        )

    if req.selected_indices:
        valid_indices = [i for i in req.selected_indices if 0 <= i < len(req.sections)]
        sections_to_use = [req.sections[i] for i in valid_indices]
        if len(sections_to_use) < 2:
            raise HTTPException(
                status_code=400,
                detail="所选章节不足 2 个, 请至少勾选 2 个章节进行跨章节问答。",
            )
    else:
        sections_to_use = req.sections

    context = build_cross_section_context(
        sections_to_use,
        req.question,
        per_section_budget=req.per_section_budget,
    )
    paper_label = req.paper_title or "未命名论文"
    user_msg = (
        f"论文标题: {paper_label}\n\n"
        f"{context}\n\n"
        f"请综合上述多章节内容回答用户问题, 并用 [SECTION_KEY] 标注信息来源。"
    )

    async def event_gen():
        try:
            chunk_iter = llm.chat_stream_async(
                messages=[
                    {"role": "system", "content": CROSS_QA_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4000,
                temperature=0.3,
            )
            async for chunk in _stream_llm_event_gen(chunk_iter):
                yield chunk
        except asyncio.CancelledError:
            return
        except llm.LLMError as e:
            yield f'data: {json.dumps({"error": str(e)}, ensure_ascii=False)}\n\n'

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/api/paper/explain_selection_stream")
async def api_explain_selection_stream(req: ExplainSelectionRequest, request: Request):
    """Streaming variant of /api/paper/explain_selection using SSE.

    Starlette's StreamingResponse handles client disconnect natively via
    the receive channel — no extra watcher needed (see api_explain_stream).
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="AI 划词答疑需要 LLM 配置。请在 .env 中设置 LLM_API_KEY 或 AGNES_API_KEY。",
        )

    paper_label = req.paper_title or "未知论文"
    source_desc = "PDF 英文原文" if req.source_type == "pdf" else ("中文译文" if req.source_type == "translation" else "论文选段")

    user_msg_parts = [
        f"论文标题: 《{paper_label}》",
        f"【用户划选片段 ({source_desc})】:\n```text\n{req.selected_text.strip()}\n```",
    ]
    if req.surrounding_context:
        # Scope budget, mirroring the router's advertised tiers
        # (term ~600-800 / mid 4-8K / wide 12-20K / full up to 60K).
        #
        # The server used to hard-truncate the context to 10 000 chars
        # regardless of scope. That silently defeated the whole two-stage
        # design: the LLM router would allocate an 80K budget for "full"
        # scope and the server would throw away 70 000 characters of it
        # before the model ever saw them. The client is still the one that
        # *builds* the context (the server has no paper text), but the
        # server now enforces the ceiling, so a buggy or hostile client
        # cannot smuggle a full-paper dump into a "term" question.
        scope_caps = {"term": 2000, "mid": 8000, "wide": 20000, "full": 50000}
        cap = scope_caps.get((req.force_scope or "").strip().lower(), 8000)
        ctx_snippet = req.surrounding_context.strip()[:cap]
        truncated = len(req.surrounding_context.strip()) > cap
        user_msg_parts.append(
            f"【该片段所在的章节/页面上下文背景】:\n{ctx_snippet}"
            + ("\n(上下文过长, 已按所选 scope 截断)" if truncated else "")
        )
    # Feedback loop: when the user re-asks because they didn't like the
    # previous answer, thread the prior answer + their feedback through so
    # the LLM can correct course (more depth, narrower focus, different
    # angle, etc.) instead of producing a parallel answer that ignores the
    # user's dissatisfaction.
    if req.feedback and req.feedback.strip():
        fb = req.feedback.strip()[:1500]
        user_msg_parts.append(f"【上一轮回答未能满足,用户反馈】:\n{fb}")
        if req.prior_answer and req.prior_answer.strip():
            prev = req.prior_answer.strip()[:6000]
            user_msg_parts.append(f"【上一轮你给出的回答 (供参考,这次请避免重复)】:\n{prev}")
    user_msg_parts.append(f"【用户的问题】:\n{req.question.strip()}")
    user_msg = "\n\n".join(user_msg_parts)

    async def event_gen():
        try:
            st = llm.get_llm_status()
            meta = {"model": st.get("active_model"), "is_degraded": st.get("is_degraded", False)}
            chunk_iter = llm.chat_stream_async(
                messages=[
                    {"role": "system", "content": EXPLAIN_SELECTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4000,
                temperature=0.3,
                timeout=120,
            )
            async for chunk in _stream_llm_event_gen(chunk_iter, meta=meta):
                yield chunk
        except asyncio.CancelledError:
            return
        except llm.LLMError as e:
            yield f'data: {json.dumps({"error": str(e)}, ensure_ascii=False)}\n\n'

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/translation/cache_stats")
async def api_translation_cache_stats():
    """Return translation cache stats (size/hits/misses) — useful for debug."""
    return get_cache().stats()


@app.post("/api/translation/cache_clear")
async def api_translation_cache_clear():
    """
    Clear the in-memory translation cache.

    Needed whenever the translation prompt or the post-processing changes:
    cached entries were produced by the OLD pipeline and would keep being
    served, so the user would see the old output even after the fix.
    """
    from .translation import get_cache
    cache = get_cache()
    cache.clear()
    return {"status": "success", "message": "翻译缓存已清空"}


@app.post("/api/paper/translate_page")
async def api_translate_page(req: TranslatePageRequest):
    """
    Translate a single PDF page (used by the dual-column reader).

    The page text comes from pdfplumber.extract_text() which gives plain text
    per page. Cached per (paper_id, page) so re-opening is instant.
    """
    if not llm.is_configured():
        raise HTTPException(
            status_code=503,
            detail="按页翻译需要 LLM 配置。请在 .env 中设置 LLM_API_KEY。",
        )

    from .translation import (chunk_section_text, get_cache, merge_translation_parts,
                              sanitize_translation, find_untranslated_runs)
    cache = get_cache()
    cfg = llm._config()
    # 翻译可能走「翻译专用模型」，缓存键和上报都要用它。
    model = llm.translation_active_model()

    # 优先用服务端重新抽取的页面文本：浏览器 PDF.js 抓到的文本在字体子集化
    # 的 PDF 上会丢掉单词空格（"compositestructureshavebeen"），而 pdfplumber
    # 用更小的 x_tolerance 能正确切词，并且能按左栏/右栏顺序输出。
    page_text = req.text
    if req.relative_path:
        try:
            from .generator import BASE_DIR, _safe_path
            from .paper_text import extract_page_text_from_path
            pdf_path = _safe_path(BASE_DIR, req.relative_path)
            extracted = extract_page_text_from_path(pdf_path, req.page)
            if extracted and len(extracted.strip()) >= 100:
                page_text = extracted
        except Exception:
            pass  # 取不到就用客户端传来的文本

    # Cache key includes paper_id + page so each page is cached independently.
    # 缓存键里还带文本哈希（见 translation.TranslationCache._key_for），
    # 换用更好的抽取文本后会自动 miss，不会命中旧的坏结果。
    cache_section_key = f"page:{req.paper_id}:{req.page}"

    if not req.force:
        cached = cache.get(cache_section_key, page_text, model)
        if cached:
            cached["from_cache"] = True
            return cached

    # Translate the whole page in one shot (page text is already < 15K chars)
    # If somehow larger, chunk and translate parts.
    chunks = chunk_section_text(page_text)
    translated_parts = []
    # 单次翻译请求的总时限（见 api_translate_section 的同名逻辑）
    deadline = time.monotonic() + _translation_deadline_seconds()
    stopped_reason = ""
    try:
        for idx, chunk in enumerate(chunks):
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                stopped_reason = "deadline"
                break
            if len(chunks) > 1:
                user_msg = (
                    f"翻译以下学术论文第 {req.page} 页 (第 {idx+1}/{len(chunks)} 段):\n\n{chunk}"
                )
            else:
                user_msg = f"翻译以下学术论文第 {req.page} 页:\n\n{chunk}"
            # 单次调用的超时取剩余时间的一半，给「截断后抬预算重试」留位置
            call_timeout = int(max(20, min(300, remaining / 2)))
            try:
                translated = await asyncio.to_thread(
                    llm.chat,
                    messages=[
                        {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=_translation_max_tokens(len(chunk), model),
                    # 翻译要确定性：同一段原文每次都该得到同一份译文
                    temperature=0.0,
                    # 强制思考的模型单次要跑 100~200s，超时要放开；但不能超过
                    # 这次请求的剩余时间，否则会越过总时限。
                    timeout=call_timeout,
                    # 直译任务关掉思考：更快，且不会在推理里打转导致正文为空
                    disable_thinking=llm.translation_disable_thinking(),
                    # 翻译专用模型（LLM_TRANSLATION_MODEL）
                    model_name=llm.translation_model_name(),
                    # 整条链路（重试 + 抬预算重试）都受这个时限约束
                    deadline=deadline,
                )
            except llm.LLMError:
                # 前面已经翻完了几段 -> 返回部分结果
                if translated_parts:
                    stopped_reason = "error"
                    break
                raise
            # `.strip()` must apply to the awaited result, not to the
            # coroutine (see the same note in api_translate_section).
            translated_parts.append((translated or "").strip())
    except llm.LLMError as e:
        msg = str(e)
        if ("时限" in msg or "timed out" in msg.lower()
                or time.monotonic() >= deadline):
            raise HTTPException(
                status_code=504,
                detail=(
                    f"翻译超时（超出单次请求时限 "
                    f"{_translation_deadline_seconds():.0f}s）。"
                    f"可以调大 .env 的 LLM_TRANSLATION_DEADLINE，或换更快的翻译模型。"
                ),
            )
        raise HTTPException(status_code=502, detail=f"按页翻译失败: {msg}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"按页翻译失败: {str(e)}")

    # 合并分块译文（去掉 overlap 造成的重复句）并清洗 LaTeX
    translation = sanitize_translation(merge_translation_parts(translated_parts))
    if not translation:
        raise HTTPException(
            status_code=502,
            detail="翻译结果为空。模型可能把 token 预算耗在推理上没产出正文，"
                   "请重试或点「探测升格」切回主模型。",
        )

    # 完整性校验：发现成段英文（模型整段漏译）就带着漏译清单重译一次。
    # 提示词管不住模型不漏，只能在服务端查出来并重试。
    leaked = find_untranslated_runs(translation)
    if leaked:
        print(
            f"[MicroBench] ⚠️ 第 {req.page} 页译文检测到 {len(leaked)} 段未翻译，重译一次: "
            f"{leaked[0][:60]!r}",
            file=sys.stderr,
        )
        try:
            retry_parts: list[str] = []
            for idx, chunk in enumerate(chunks):
                hint = ("\n\n【注意】上一次翻译漏掉了以下内容，必须完整翻译：\n"
                        + "\n".join(x[:200] for x in leaked[:5]))
                if len(chunks) > 1:
                    user_msg = (
                        f"翻译以下学术论文第 {req.page} 页 (第 {idx+1}/{len(chunks)} 段):"
                        f"\n\n{hint}\n\n{chunk}"
                    )
                else:
                    user_msg = f"翻译以下学术论文第 {req.page} 页:\n\n{hint}\n\n{chunk}"
                retry_parts.append(
                    (
                        await asyncio.to_thread(
                            llm.chat,
                            messages=[
                                {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                                {"role": "user", "content": user_msg},
                            ],
                            max_tokens=_translation_max_tokens(len(chunk), model),
                            temperature=0.0,
                            # 重译同样受总时限约束，别让「重译一次」再拖一轮
                            timeout=int(max(20, min(300, max(1.0, deadline - time.monotonic()) / 2))),
                            disable_thinking=llm.translation_disable_thinking(),
                            # 必须用同一个翻译模型：主模型和翻译模型措辞风格不同，
                            # 混用会让"重译版"看起来像另一份译文
                            model_name=llm.translation_model_name(),
                            deadline=deadline,
                        )
                    ).strip()
                )
            retry_text = sanitize_translation(merge_translation_parts(retry_parts))
            if retry_text and len(retry_text) >= len(translation) * 0.9:
                translation = retry_text
                leaked = find_untranslated_runs(translation)
        except Exception:
            pass  # 重译失败就用第一版，不要因此让整个请求失败
    curr_st = llm.get_llm_status()
    result = {
        "status": "success",
        "translation": translation,
        "paper_id": req.paper_id,
        "page": req.page,
        "chunk_count": len(chunks),
        "from_cache": False,
        "model": llm.translation_active_model(),
        "is_degraded": curr_st["is_degraded"],
        # 残留未翻译的英文段数（完整性校验的结果，正常应为 0）
        "untranslated_runs": len(leaked),
        # 部分完成：超出总时限，后面的分段没翻
        "partial": bool(stopped_reason),
        "translated_chunks": len(translated_parts),
        "stopped_reason": stopped_reason,
    }
    # 缓存写入必须用与读取时完全一致的文本（page_text），否则哈希对不上，
    # 缓存永远不命中。
    cache.put(cache_section_key, page_text, model, result)
    return result


@app.post("/api/paper/translation_cache_batch_lookup")
async def api_translation_cache_batch_lookup(req: TranslationCacheBatchRequest):
    """
    Batch lookup: returns `{section_key: result}` for cache hits only (misses are skipped).
    Used by the frontend to restore translation state after a page refresh — without
    this, every section shows "未翻译" until the user re-clicks. Hits come back
    instantly from the in-memory LRU cache (no LLM call).
    """
    cache = get_cache()
    hits: dict = {}
    misses = 0
    for item in req.keys:
        cached = cache.get(item.section_key, item.text, item.model)
        if cached:
            cached["from_cache"] = True
            hits[item.section_key] = cached
        else:
            misses += 1
    return {
        "hits": hits,
        "misses": misses,
        "total": len(req.keys),
        "cache_stats": cache.stats(),
    }


@app.post("/api/paper/split_text")
async def api_split_text(req: SplitTextRequest):
    """
    Split user-pasted raw text into sections (no PDF download needed).
    Useful for paywalled IEEE papers where the user has copied text from
    their institution's PDF reader.
    """
    pages = [{"page": 1, "text": req.raw_text}]
    sections = split_into_sections(pages)
    if not sections:
        # No recognizable sections — return whole text as full_text
        sections = [{
            "section": "full_text",
            "title": "Full Text",
            "text": req.raw_text.strip(),
            "page_start": 1,
            "page_end": 1,
            "char_count": len(req.raw_text),
        }]
    sections_out = []
    for s in sections:
        sections_out.append({
            "section": s["section"],
            "title": s["title"],
            "number": s.get("number", ""),
            "text": s["text"],
            "page_start": s["page_start"],
            "page_end": s["page_end"],
            "char_count": len(s["text"]),
        })
    return {
        "status": "success",
        "sections": sections_out,
        "char_count": len(req.raw_text),
    }


@app.post("/api/paper/paste_analyze")
async def api_paste_analyze(req: PasteTextRequest):
    """
    One-shot workflow for paywalled papers:
      1. User pastes raw text (copied from PDF reader in browser)
      2. We split into sections
      3. If `question` is provided, ask LLM immediately
      4. Return sections + (optional) AI answer

    Saves the user from running split + ask in two steps.
    """
    pages = [{"page": 1, "text": req.raw_text}]
    sections = split_into_sections(pages)
    if not sections:
        sections = [{
            "section": "full_text",
            "title": "Full Text",
            "text": req.raw_text.strip(),
            "page_start": 1,
            "page_end": 1,
        }]

    sections_out = [{
        "section": s["section"],
        "title": s["title"],
        "number": s.get("number", ""),
        "text": s["text"],
        "page_start": s["page_start"],
        "page_end": s["page_end"],
        "char_count": len(s["text"]),
    } for s in sections]

    response = {
        "status": "success",
        "title": req.title,
        "sections": sections_out,
        "char_count": len(req.raw_text),
        "ai_answer": None,
        "ai_error": None,
    }

    if req.question:
        if not llm.is_configured():
            response["ai_error"] = "LLM 未配置, 无法回答"
            return response
        try:
            # Build ExplainRequest context
            # Sync LLM call from an async endpoint — offload to a worker thread.
            answer = await asyncio.to_thread(
                llm.chat,
                messages=[
                    {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
                    {"role": "user", "content": (
                        f"论文标题: {req.title}\n\n"
                        f"--- 用户粘贴的全文 ---\n{req.raw_text[:20000]}\n--- 全文结束 ---\n\n"
                        f"用户问题: {req.question}"
                    )},
                ],
                max_tokens=1200,
                temperature=0.3,
            )
            response["ai_answer"] = answer.strip()
        except llm.LLMError as e:
            response["ai_error"] = f"LLM 调用失败: {str(e)}"
    return response


# ---------------------------------------------------------------------------
# PDF translation session snapshots
# ---------------------------------------------------------------------------
# Persists, per paper, the pages that have been translated so the user can
# pick up where they left off after restarting MicroBench, or hop between
# papers in the snapshot history without re-paying LLM cost. Files live in
# `logs/_pdf_snapshots/<paperId>.json` — kept inside the project's own
# `logs/` (gitignored) so they don't leak into the public Obsidian vault.
# Hard per-snapshot limit: 5 MB on save to prevent runaway fills.

import datetime as _dt

SNAPSHOT_DIR_NAME = "_pdf_snapshots"
SNAPSHOT_MAX_BYTES = 5 * 1024 * 1024
SNAPSHOT_SCHEMA_VERSION = 1


class _SnapshotPageItem(BaseModel):
    page_num: int = Field(..., ge=1, le=2000)
    text_hash: str = Field(..., min_length=4, max_length=64,
                           description="SHA-256 hex of the original page text")
    text: str = Field(default="", max_length=30000,
                      description="Optional — original page text for offline restore")
    translation: str = Field(..., min_length=1, max_length=30000)
    model: Optional[str] = Field(default="", max_length=64)
    translated_at: Optional[str] = Field(default="", max_length=40)


class SnapshotSaveRequest(BaseModel):
    paper_id: str = Field(..., min_length=3, max_length=128,
                          description="Stable paper identifier (matches renderPdfPages' paperId)")
    paper_title: str = Field(..., min_length=1, max_length=500)
    relative_path: str = Field(..., min_length=3, max_length=500)
    paper_mtime: float = Field(..., ge=0,
                               description="stat().st_mtime of the source PDF at save time")
    paper_text_signature: str = Field(default="", max_length=64,
                                      description="Optional — fingerprint across all pages")
    # Typed + bounded. This used to be a bare `list`, which meant a client
    # could POST an unbounded number of arbitrary objects: the server parsed
    # the whole thing into memory and only *then* checked the 5MB limit, so
    # the "5MB cap" never actually capped peak memory. `_SnapshotPageItem`
    # was defined for exactly this purpose but never referenced.
    pages: list[_SnapshotPageItem] = Field(..., max_length=2000,
                                            description="List of translated pages")


def _snapshot_dir() -> Path:
    p = BASE_DIR / "logs" / SNAPSHOT_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshot_path(paper_id: str) -> Path:
    # paper_id is computed in JS as `${_paperBase}_${hash}` and is already
    # filename-safe (alphanumeric + underscore). Defensive-normalize anyway.
    safe = "".join(c for c in paper_id if c.isalnum() or c in "_-")[:128] or "snapshot"
    return _snapshot_dir() / f"{safe}.json"


def _safe_read_json(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@app.get("/api/pdf/snapshot/list")
async def api_pdf_snapshot_list():
    """List all saved PDF translation snapshots, newest first."""
    base = _snapshot_dir()
    out = []
    for fp in base.glob("*.json"):
        data = _safe_read_json(fp)
        if not data:
            continue
        pages = data.get("pages") or []
        out.append({
            "paper_id": data.get("paper_id") or fp.stem,
            "paper_title": data.get("paper_title", ""),
            "relative_path": data.get("relative_path", ""),
            "saved_at": data.get("saved_at", ""),
            "paper_mtime": data.get("paper_mtime", 0),
            "total_pages": len(pages),
            "translated_pages": sum(1 for p in pages if p.get("translation")),
            "schema_version": data.get("schema_version", 0),
        })
    out.sort(key=lambda x: x.get("saved_at", ""), reverse=True)
    return {"snapshots": out}


@app.get("/api/pdf/snapshot/load")
async def api_pdf_snapshot_load(
    paper_id: str = Query(..., min_length=3, max_length=128),
):
    """
    Load a snapshot for restore. Returns the raw saved JSON. Strict
    consistency checks (mtime, per-page hash) are performed CLIENT-SIDE
    because the server doesn't re-parse the PDF — the JS layer already
    has pdf.js loaded and can re-extract page text in the same way it
    did on the original read.
    """
    data = _safe_read_json(_snapshot_path(paper_id))
    if not data:
        raise HTTPException(status_code=404, detail=f"快照不存在: {paper_id}")
    return data


@app.post("/api/pdf/snapshot/save")
async def api_pdf_snapshot_save(req: SnapshotSaveRequest):
    """
    Save (or overwrite) a snapshot. Body must fit within 5MB serialized.

    Validates:
      - paper_id filename-safe
      - unique pages (no duplicate page_num)
      - total size <= 5MB

    Writes atomically via a .tmp + os.replace to avoid leaving half-files
    if MicroBench is killed mid-write.
    """
    safe_id = "".join(c for c in req.paper_id if c.isalnum() or c in "_-")[:128]
    if len(safe_id) < 3:
        raise HTTPException(status_code=400, detail="paper_id 非法")

    # Validate page list
    seen = set()
    for p in req.pages:
        pn = p.page_num
        if pn in seen:
            raise HTTPException(status_code=400, detail=f"page {pn} 重复")
        seen.add(pn)

    pages_out = [p.model_dump() for p in req.pages]

    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "paper_id": safe_id,
        "paper_title": req.paper_title,
        "relative_path": req.relative_path,
        "paper_mtime": req.paper_mtime,
        "paper_text_signature": req.paper_text_signature,
        "pages": pages_out,
        "saved_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(serialized.encode("utf-8")) > SNAPSHOT_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"快照过大(>{SNAPSHOT_MAX_BYTES // 1024 // 1024}MB),请减少保存页数",
        )

    target = _snapshot_path(safe_id)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, target)
    except Exception as e:
        # Best-effort cleanup of orphan .tmp
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"写盘失败: {e}")

    return {
        "status": "success",
        "paper_id": safe_id,
        "saved_at": payload["saved_at"],
        "saved_pages": len(req.pages),
        "bytes": len(serialized.encode("utf-8")),
        "path": str(target.relative_to(BASE_DIR)).replace("\\", "/"),
    }


@app.delete("/api/pdf/snapshot/delete")
async def api_pdf_snapshot_delete(
    paper_id: str = Query(..., min_length=3, max_length=128),
):
    """Delete one snapshot file. Idempotent (returns success even if missing)."""
    target = _snapshot_path(paper_id)
    if target.exists():
        target.unlink()
    return {"status": "success", "paper_id": paper_id}


@app.get("/api/pdf/info")
async def api_pdf_info(
    relative_path: str = Query(..., min_length=3, max_length=500),
):
    """
    Quick file-level metadata for a vault PDF: size + mtime.

    Used by the snapshot loader to compare current mtime against the
    mtime recorded at save time, so the user gets warned if the PDF was
    overwritten/replaced between sessions.
    """
    try:
        fp = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {e}")
    if not fp.exists() or not fp.is_file():
        raise HTTPException(status_code=404, detail="PDF 不存在")
    if fp.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="不是 PDF 文件")
    st = fp.stat()
    return {
        "size": st.st_size,
        "mtime": st.st_mtime,
        "mtime_iso": _dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "relative_path": str(fp.relative_to(BASE_DIR)).replace("\\", "/"),
    }