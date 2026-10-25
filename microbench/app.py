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
import sys
import json
import re
import sys
import hmac
import hashlib
import ipaddress
import uuid
import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, FileResponse, StreamingResponse,
                                RedirectResponse, Response)
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
from .paper_text import (fetch_full_text, split_into_sections,
                          extract_text_from_pdf, extract_page_text_with_glyphs)
from . import glyphs as glyphs_mod
from . import lan as lan_helpers
from .lan import resolve_token as _resolve_lan_token


def _safe_err(exc: BaseException) -> str:
    """给用户看的异常摘要：脱敏 + 抹掉本机绝对路径 + 限长。

    为什么不能直接 str(e)（安全审计 M2）：
    OSError / FileNotFoundError 的消息里带着本机绝对路径
    （如 'E:\\zaiwu\\个人工作台\\logs\\xxx.json'），原样送到浏览器
    = 内部路径泄露；而 requests 的异常消息里可能带带凭据的 URL
    （base_url 里的 user:pass@host）。
    """
    return llm.mask_paths(llm.redact_secrets(str(exc)))[:200]


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
        yield f'data: {json.dumps({"error": llm.friendly_error_text(e)}, ensure_ascii=False)}\n\n'


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
    model: str = Field(default="", max_length=60,
                       description="临时换模型翻译：空=跟随当前设置, 'agnes'=备用模型, "
                                   "否则是 MiniMax 系模型名")

def _resolve_translation_model(requested: str):
    """解析前端传来的 model 参数。

    返回 (model_name, force_fallback, cache_key_model)：
      - model_name：传给 llm.chat 的临时模型名（空 = 用默认的翻译专用设置）
      - force_fallback：True = 强制走备用模型（Agnes）
      - cache_key_model：缓存键里用的模型名 —— 必须反映实际用的模型，
        否则换模型翻译会命中旧模型的缓存（换模型等于没换）

    自定义模型（.env 里 LLM_CUSTOM_N_*）的 id 形如 "custom:我的模型"，
    原样传下去：llm.chat 会按 id 找到它自己的 base_url / key / 限流档。
    """
    req = (requested or "").strip()
    if not req:
        return "", False, llm.translation_active_model()
    if req.lower() in ("agnes", "fallback", "agnes-3.0-flash"):
        fb_name = llm.model_manager.get_fallback_config().get("model") or "agnes-3.0-flash"
        return "", True, fb_name
    if req.startswith(llm.CUSTOM_MODEL_PREFIX):
        cfg = llm.resolve_model_config(req)
        if cfg:
            return req, False, req
    return req, False, req


# 可供界面选择的翻译模型。MiniMax 系共用同一套 key/base_url；
# 'agnes' 走备用模型（免费无限，但更慢）；
# 用户自己的 API 模型由 .env 的 LLM_CUSTOM_N_* 追加（见 llm.custom_model_configs）。
TRANSLATION_MODEL_CHOICES = (
    {"id": "", "label": "跟随当前设置", "hint": "默认：LLM_TRANSLATION_MODEL 或主模型"},
    {"id": "MiniMax-M3", "label": "MiniMax-M3", "hint": "直译最快最稳（推荐）"},
    {"id": "MiniMax-M3.1-Flash", "label": "MiniMax-M3.1-Flash", "hint": "强制思考，很慢且易截断"},
    {"id": "MiniMax-M2.7", "label": "MiniMax-M2.7", "hint": "会混入思考文本"},
    {"id": "agnes", "label": "Agnes 3.0 Flash", "hint": "免费无限额度，较慢"},
)


def _qa_model_args(model: str = "") -> dict:
    """把前端传来的模型 id 转成 llm.chat / llm.chat_stream_async 的参数。

    ''        → 跟随默认（主模型，降级时自动走备用）
    'agnes'  → 强制走备用模型
    'custom:x' → 用户自己的 API 模型（自己的 base_url/key/限流档）
    其它      → 同一套 key/base_url 下的另一个模型名
    """
    m_name, m_fallback, _ = _resolve_translation_model(model)
    return {"model_name": m_name or None, "force_fallback": m_fallback}


def _effective_model_name(requested: str = "") -> str:
    """这次请求**实际**会用哪个模型（给界面上的模型徽标显示）。

    不能直接用 llm.get_llm_status()["active_model"] —— 那是全局状态，
    用户手动选了自定义模型时它显示的还是主模型，会让人以为"选模型没生效"。
    """
    m_name, m_fallback, _ = _resolve_translation_model(requested)
    if m_fallback:
        return llm.model_manager.get_fallback_config().get("model") or "agnes-3.0-flash"
    if m_name:
        cfg = llm.resolve_model_config(m_name)
        if cfg:
            return cfg.get("label") or cfg.get("model") or m_name
        return m_name
    return llm.get_llm_status().get("active_model") or "default"


def translation_model_choices() -> list[dict]:
    """界面上能选的模型 = 内置的 + 用户自己配的（.env LLM_CUSTOM_N_*）。"""
    out = [dict(m) for m in TRANSLATION_MODEL_CHOICES]
    for c in llm.custom_model_configs():
        out.append({
            "id": c["id"],
            "label": c["label"],
            "hint": (f"你自己的 API 模型 · {c['model']} · {c['rpm']} RPM"
                     + ("" if c["configured"] else " · ⚠ 没配 API key")),
            "custom": True,
        })
    return out


async def _resolve_page_glyphs(text: str, glyphs: list, pdf_source, page_no: int,
                               model: str = "") -> str:
    """把文本里的 ⟦G:n⟧ 换成真实符号（× ≈ ≥ …），认不出来的退回 "[?]"

    为什么需要：TeX 数学字体在 PDF 里没有 ToUnicode 映射，pdfplumber 解不出来
    （2026-10-25 用户反馈："有 × 的它都显示成个？"）。
    这些字形在 PDF 里是画出来的 → 裁一小块图让多模态认字，认一次存进词典，
    之后查表即可（零成本、跨论文复用）。

    词典放 vault/logs/glyph_dict.json → 两台设备通过坚果云共用一份。

    ⚠️ 本函数只用于「按页翻译」那条路径（一次一页，天然只有一个符号集）。
    **打开/上传论文走 _resolve_document_glyphs**（整篇一次）——
    逐页调用会让长论文的打开耗时 8 分钟以上。

    ⚠️ 这里**只查词典、不调模型**：
    翻译请求本身就要等几十秒，再叠一次 30~90s 的认字会让用户以为卡死；
    而且认字失败会触发**全局降格**，连累后续所有翻译。
    词典由「打开论文」时的后台补齐任务慢慢填满
    （见 _schedule_glyph_backfill）—— 打开一次，之后翻译都是真符号。
    """
    if not text or not glyphs:
        return text
    try:
        pages = [{"page": page_no, "text": text, "glyphs": glyphs}]
        await glyphs_mod.resolve_document_glyphs_async(
            pages, pdf_source, dict_path=GLYPH_DICT_PATH, recognize=False)
        return pages[0]["text"]
    except Exception as e:
        print(f"[MicroBench] 符号解析失败（退回 [?]）: {e}", file=sys.stderr)
        return glyphs_mod.strip_markers(text)


# 整篇符号解析的**总时间预算**（秒）。
# 为什么要有硬上限：多模态认字实测 29~48s/次，而模型降格/限流时会更慢。
# 用户要的是"能打开论文"，符号只是锦上添花 —— 超时就退回 "[?]",
# 绝不让用户对着一个转圈的界面等好几分钟。
GLYPH_RESOLVE_BUDGET_SEC = 90

# 后台补齐词典的预算（秒）——比前台宽松，因为它不挡着用户。
GLYPH_BACKFILL_BUDGET_SEC = 180


async def _resolve_document_glyphs(pages: list, pdf_source,
                                   recognize: bool = False) -> dict:
    """整篇一次性解析解不出的数学符号（× ≈ ≥ …）。

    修的是「上传本地 pdf / 从论文库打开 半天没反应」：
    旧代码逐页调用认字 → 36 页的论文发 35 次**串行**多模态请求 → 8 分钟以上。
    现在按语义键全局去重（6184 个实例 → 101 个不同符号），
    整篇通常 1~3 次批量调用；词典命中时**零模型调用**。

    **recognize=False（默认，打开/上传走这条）= 只查词典、不调模型**：
    用户要的是"能打开看全文"，符号认不出来显示 [?] 完全可接受
    （契约：宁可空着也不能指错）。调模型认字是锦上添花，不该挡住打开；
    而且认字失败会触发**全局降格**，连累后续所有翻译
    （AGENTS.md：一次任务的毛病不该让别的请求都改走备用模型）。
    """
    has_glyphs = any(p.get("glyphs") for p in pages)
    if not has_glyphs:
        return {}
    import time as _time
    deadline = _time.monotonic() + GLYPH_RESOLVE_BUDGET_SEC
    try:
        stats = await glyphs_mod.resolve_document_glyphs_async(
            pages, pdf_source, dict_path=GLYPH_DICT_PATH, deadline=deadline,
            recognize=recognize)
    except Exception as e:
        print(f"[MicroBench] 整篇符号解析失败（退回 [?]）: {e}", file=sys.stderr)
        # 出错也要保证内部标记不外泄（模型绝不能看见 ⟦G:n⟧）
        for _p in pages:
            if _p.get("glyphs"):
                _p["text"] = glyphs_mod.strip_markers(_p.get("text") or "")
        return {}
    if stats.get("model_calls"):
        print(f"[MicroBench] 符号认字：{stats.get('unique', 0)} 个不同符号 / "
              f"{stats.get('model_calls', 0)} 次模型调用 / "
              f"识别成功 {stats.get('resolved', 0)}", file=sys.stderr)
    return stats


def _schedule_glyph_backfill(pdf_bytes: bytes) -> None:
    """**后台**补齐符号词典（不阻塞用户的打开操作）。

    为什么需要：打开时只查词典（亚秒级），所以新论文的符号一开始是 [?]；
    但这些符号认一次就能进词典（跨论文复用）→ 后台慢慢认，
    下次打开 / 翻译时就是真符号了。

    在**后台线程**里跑（不占事件循环），并且**不触发全局降格** ——
    认字失败只影响这一次，不该连累用户的翻译。

    守卫：词典已经覆盖这篇论文的全部符号时**不重复认** ——
    每次打开都去问模型既浪费配额，又可能触发降格连累翻译。
    """
    def _worker():
        try:
            from microbench.paper_text import extract_text_from_pdf
            pages = extract_text_from_pdf(pdf_bytes)
            if not any(p.get("glyphs") for p in pages):
                return
            # 守卫：这篇论文的符号是不是都已经在词典里了
            dictionary = glyphs_mod.GlyphDictionary(GLYPH_DICT_PATH).load()
            need = set()
            for _p in pages:
                for _g in _p.get("glyphs") or []:
                    need.add("s:" + glyphs_mod.semantic_key(
                        _g.get("font", ""), _g.get("cid", "")))
            if need and all(dictionary.get(k) for k in need):
                return      # 全都认过了 → 不用再问模型
            import time as _time
            stats = glyphs_mod.resolve_document_glyphs(
                pages, pdf_bytes, dict_path=GLYPH_DICT_PATH,
                deadline=_time.monotonic() + GLYPH_BACKFILL_BUDGET_SEC,
                recognize=True, isolated=True)
            print(f"[MicroBench] 后台补齐符号词典：{stats.get('resolved', 0)} 个符号 "
                  f"（{stats.get('model_calls', 0)} 次模型调用）", file=sys.stderr)
        except Exception as e:
            print(f"[MicroBench] 后台补齐符号词典失败: {e}", file=sys.stderr)

    try:
        import threading
        t = threading.Thread(target=_worker, daemon=True, name="glyph-backfill")
        t.start()
    except Exception as e:
        print(f"[MicroBench] 后台补齐符号词典启动失败: {e}", file=sys.stderr)


# 符号词典：字形 → 符号 的缓存。放 vault/logs 下（坚果云会同步，两台设备共用）
GLYPH_DICT_PATH = Path(BASE_DIR) / "logs" / "glyph_dict.json"


@app.get("/api/llm/translation_models")
async def api_llm_translation_models():
    """界面上的「换模型翻译」下拉框选项。"""
    active = llm.translation_active_model()
    return {
        "models": [
            dict(m, active=(m["id"] == active))
            for m in translation_model_choices()
        ],
        "active_model": active,
        "primary": llm.model_manager.get_primary_config().get("model", ""),
        "fallback": llm.model_manager.get_fallback_config().get("model", ""),
    }


class OpenVaultPdfRequest(BaseModel):
    """Open a PDF that already exists inside the vault (no upload, no copy)."""
    relative_path: str = Field(..., min_length=3, max_length=500)
    title: Optional[str] = Field(default=None, max_length=300)


class ExplainRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=500)
    sections: list = Field(..., min_length=1, max_length=20,
                            description="paper sections [{title, text}, ...]")
    paper_title: Optional[str] = Field(default="", max_length=300)
    model: Optional[str] = Field(default="", max_length=64,
                                 description="用哪个模型回答：'' = 跟随默认, "
                                             "'agnes' = 备用模型, 'custom:xxx' = 自定义模型")


class ExplainSelectionRequest(BaseModel):
    selected_text: str = Field(..., min_length=2, max_length=15000,
                               description="用户划选的原文或译文片段")
    question: str = Field(..., min_length=1, max_length=1000,
                          description="用户针对该片段提出的问题")
    paper_title: Optional[str] = Field(default="", max_length=300)
    model: Optional[str] = Field(default="", max_length=64,
                                 description="用哪个模型回答：'' = 跟随默认, "
                                             "'agnes' = 备用模型, 'custom:xxx' = 自定义模型")
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
    page_image: Optional[str] = Field(default=None, max_length=4_000_000,
                                       description="所选片段所在页面的 PNG 截图 (base64, 不含 data URL 前缀)。"
                                                   "表格/曲线图这类「不好文字化」的内容, 抽出来的文本只是碎片 "
                                                   "(上下标/坐标轴标签), 模型看到的是一堆没意义的字符。"
                                                   "MiniMax-M3 是多模态模型, 把截图一起给它, 它就能自己看图读出数值。")
    pdf_relative_path: Optional[str] = Field(default=None, max_length=500,
                                              description="PDF 在 vault 中的相对路径。提供 page_image 时用于"
                                                          "校验图片来源 (只允许 vault 内的 PDF)。")


class ReadTableRequest(BaseModel):
    """把 PDF 里的一个表格区域读成 Markdown（多模态抄表）。"""
    relative_path: str = Field(..., min_length=3, max_length=500)
    page: int = Field(..., ge=1, le=2000)
    rect: str = Field(..., min_length=5, max_length=120,
                      description="表格区域 x0,y0,x1,y1（PDF 点，左上角原点）")
    model: str = Field(default="", max_length=64)


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
    model: str = Field(default="", max_length=60,
                       description="临时换模型翻译：空=跟随当前设置, 'agnes'=备用模型, "
                                   "否则是 MiniMax 系模型名")


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


# ---------------------------------------------------------------------------
# 看板「真实产出」统计
# ---------------------------------------------------------------------------
# 看板原来只数「用工具生成的结构化产物」：.md 笔记卡片、复现工程目录、
# 踩坑日记、周报。但用户实际的主线是「翻论文 → 翻译 → 划词 → 批注」——
# 这条线上的产出（论文原文、已译页数、批注）一个都没被计入，于是看板永远
# 是 0，看着像坏了。下面把真实产出也统计出来。
#
# 译文/批注住在同步目录（坚果云），所以两台设备看到的是同一份数字。

_REAL_STATS_CACHE: dict = {}
_REAL_STATS_TS: float = 0.0
_REAL_STATS_TTL = 15.0   # 秒


def _real_output_stats() -> dict:
    """论文原文数 / 笔记数 / 已译页数 / 批注条数。"""
    global _REAL_STATS_CACHE, _REAL_STATS_TS
    now = time.monotonic()
    if _REAL_STATS_CACHE and (now - _REAL_STATS_TS) < _REAL_STATS_TTL:
        return dict(_REAL_STATS_CACHE)

    pdf_total = note_total = 0
    lit = BASE_DIR / "01_Literature"
    if lit.is_dir():
        for p in lit.rglob("*"):
            if not p.is_file() or p.name.startswith("."):
                continue
            suf = p.suffix.lower()
            if suf == ".pdf":
                pdf_total += 1
            elif suf in (".md", ".markdown"):
                note_total += 1

    translated_pages = 0
    snap_dir = _sync_root() / "snapshots"
    if snap_dir.is_dir():
        for fp in snap_dir.glob("*.json"):
            data = _safe_read_json(fp) or {}
            pages = data.get("pages")
            if isinstance(pages, list):
                translated_pages += len(pages)

    annotation_count = 0
    ann_dir = _sync_root() / "annotations"
    if ann_dir.is_dir():
        for fp in ann_dir.glob("*.json"):
            data = _safe_read_json(fp) or {}
            items = data.get("annotations")
            if isinstance(items, list):
                annotation_count += len(items)

    out = {
        "pdf_total": pdf_total,
        "note_total": note_total,
        "translated_pages": translated_pages,
        "annotation_count": annotation_count,
    }
    _REAL_STATS_CACHE = out
    _REAL_STATS_TS = now
    return dict(out)


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
        real = _real_output_stats()
        return {
            # 旧字段（工具产出的结构化产物）保留，别的地方还在用
            "literature_total": stats.get("literature_total", 0),
            "reproduction_total": stats.get("reproduction_total", 0),
            "troubleshoot_count": stats.get("troubleshoot_count", 0),
            "weekly_count": stats.get("weekly_count", 0),
            # 真实产出：看板现在显示的是这几个
            **real,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 vault 统计失败: {_safe_err(e)}")


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
        raise HTTPException(status_code=500, detail=f"读取文献列表失败: {_safe_err(e)}")

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
        raise HTTPException(status_code=400, detail=f"参数无效: {_safe_err(e)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"抓取文献失败: {_safe_err(e)}")

@app.post("/api/paper/save")
async def api_save_paper(req: SavePaperRequest):
    try:
        res = save_literature_card(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存文献卡片失败: {_safe_err(e)}")


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
        raise HTTPException(status_code=500, detail=f"导出双语卡片失败: {_safe_err(e)}")


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
        raise HTTPException(status_code=500, detail=f"导出答疑笔记失败: {_safe_err(e)}")




@app.post("/api/paper/download_pdf")
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

    ⚠️ 必须是 POST（安全审计 M1）：这个端点**有写副作用**
    （mkdir + 写 PDF 文件 + 发起一次外网请求），而 CSRF 守卫只查
    非安全方法（POST/DELETE/PUT/PATCH）→ 原来是 GET 的话，
    恶意网页一个 `<img src="http://127.0.0.1:5000/api/paper/download_pdf?...">`
    就能让用户机器凭空多出文件（且 `<img>` 不带 Origin，守卫直接放行）。
    改成 POST 后，非浏览器客户端仍可正常调用。
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
            raise HTTPException(status_code=400, detail=f"参数无效: {_safe_err(e)}")
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"无法解析 DOI 落地页: {_safe_err(e)}",
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
        raise HTTPException(status_code=502, detail=f"下载 PDF 失败: {_safe_err(e)}")


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
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"PDF 不存在: {relative_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail=f"不是 PDF 文件: {relative_path}")
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _pdf_clip_png(pdf_path, page: int, rect: tuple, scale: float = 4.0,
                  max_pixels: int = 4_000_000) -> bytes:
    """把 PDF 某一页的一块区域裁成 PNG 字节（PyMuPDF）。

    公式原图、表格截图都走这里。rect = (x0, y0, x1, y1)（PDF 点）。

    max_pixels 是**必需的**护栏：裁剪框虽然被限制在页面内，但客户端可以
    把 scale 调到 8、裁整页 → 4752×6264 ≈ 3000 万像素，一次请求就是
    ~90MB 的原始 RGB，PNG 编码还会再占一份 → 本机服务被打爆。
    超了就自动降档（和 paper_text.render_page_png 同一套做法）。
    """
    import fitz  # PyMuPDF
    with fitz.open(str(pdf_path)) as doc:
        if page < 1 or page > doc.page_count:
            raise ValueError(f"页码越界: {page} (共 {doc.page_count} 页)")
        pg = doc.load_page(page - 1)
        pr = pg.rect
        cx0 = max(pr.x0, min(rect[0], pr.x1))
        cy0 = max(pr.y0, min(rect[1], pr.y1))
        cx1 = min(pr.x1, max(rect[2], pr.x0))
        cy1 = min(pr.y1, max(rect[3], pr.y0))
        if cx1 - cx0 <= 0 or cy1 - cy0 <= 0:
            raise ValueError("裁剪区域无效")
        if (cx1 - cx0) > pr.width or (cy1 - cy0) > pr.height:
            raise ValueError("裁剪区域超出页面")
        # 像素总量保护：超出就按比例降 scale（上限 1.0，保证一定能出来）
        w_pt, h_pt = cx1 - cx0, cy1 - cy0
        pixels = (w_pt * scale) * (h_pt * scale)
        if pixels > max_pixels:
            scale = max(1.0, scale * (max_pixels / pixels) ** 0.5)
        pix = pg.get_pixmap(clip=fitz.Rect(cx0, cy0, cx1, cy1),
                            matrix=fitz.Matrix(scale, scale), alpha=False)
        return pix.tobytes("png")


# ---------------------------------------------------------------------------
# 表格：截图交给多模态模型抄
# ---------------------------------------------------------------------------
# 表格在 PDF 文本层是碎的："d (pC/N) (cid:4)90 (cid:4)175 (cid:4)109 B700 16.5"
# —— 连负号都认不出，文本模型只能靠上下文猜对齐，结果是**整行数值错位**
# （实测 JACERS Table I：d31 行的 16.5 跑到了 d33 行，7200 变成 B700）。
#
# 用户要求（2026-10-25）："个别单元都抽错的话你也给我直接上这个多模态读图吧。
# 我要的是阅读的……至少顺序要是很正常的"。
# → 表格整块裁成图，交给多模态模型逐格抄：图上什么样，表格就什么样。

TABLE_VISION_PROMPT = """你是一个学术论文表格的抄录员。下面这张截图是一篇论文里的一个表格（可能带表题和表注）。

请把它**逐格**抄下来，只输出抄录结果本身（不要解释、不要用代码块围栏）：

1. 如果截图上方有表题（"Table I. ..."），先单独用一行输出它的中文翻译
2. 然后输出 Markdown 表格：第一行是表头，第二行是 |---|---| 分隔行
3. **行列顺序、每一个数值都必须和截图完全一致** —— 不要推算、不要补全、
   不要"修正"你认为不对的数字、不要按常识改单位
4. 单元格里的上下标写成 LaTeX：d$_{31}$、ε$_{33}$、T$_{c}$、ρ$_{v}$；
   单位（pC/N、g/cm$^{3}$、°C、ppm、Ω·m）保持原样
5. 表头和第一列（参数名）译成中文，其余内容保持原样
6. 截图下方如果有表注，另起一段译成中文
7. 看不清的单元格写 [?]，**不要猜**"""


def _clean_table_markdown(raw: str) -> str:
    """清掉模型可能加的代码块围栏和多余说明。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _looks_like_markdown_table(text: str) -> bool:
    """这段输出像不像一张 Markdown 表格。

    为什么必须校验：模型有时会回一句"截图里没有表格"之类的说明（**成功返回，
    不是异常**）。旧逻辑只判 `if not md.strip()`，于是这段说明被当成表格内容
    拼进译文 —— 用户在正文里读到一句莫名其妙的话，还以为是自己看错了。

    判据：正文里得有竖线（至少 2 个）且至少有一行含竖线。
    不要求必须有 `|---|` 分隔行 —— 模型偶尔会漏掉它，但数据还是对的，
    宁可放行也不要整张表丢掉。
    """
    t = (text or "").strip()
    if not t:
        return False
    lines = [ln.strip() for ln in t.split("\n") if ln.strip()]
    if not any("|" in ln for ln in lines):
        return False
    return t.count("|") >= 2


async def _read_table_vision(pdf_path, page: int, rect: tuple,
                             model: str = "") -> str:
    """裁图 → 多模态模型抄表 → 返回 Markdown。失败抛异常（由调用方兜底）。

    model：用户在前端手动选的模型。**必须跟着走** —— 正文用新模型、表格用
    旧模型会让人以为"换模型没生效"（2026-10-25 独立审查）。
    """
    import base64
    png = await asyncio.to_thread(_pdf_clip_png, pdf_path, page, rect, 4.0)
    b64 = base64.b64encode(png).decode("ascii")
    content = [
        {"type": "text", "text": TABLE_VISION_PROMPT},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]
    m_name, m_fallback, _cache_key = _resolve_translation_model(model)
    raw = await asyncio.to_thread(
        llm.chat,
        messages=[
            {"role": "system", "content": "你是表格抄录助手，只输出抄录结果。"},
            {"role": "user", "content": content},
        ],
        max_tokens=4000,
        temperature=0.0,
        timeout=180,
        disable_thinking=llm.translation_disable_thinking(),
        model_name=m_name or llm.translation_model_name() or None,
        force_fallback=m_fallback,
    )
    md = _clean_table_markdown(raw)
    # 模型有时会"成功"回一句"截图里没有表格"的说明 —— 那不是表格，
    # 直接拼进译文会让用户读到一句莫名其妙的话。判失败 → 走兜底提示。
    if not _looks_like_markdown_table(md):
        raise RuntimeError(
            f"模型没有抄出表格（返回 {len(md)} 字符，"
            f"开头：{(md or '(空)')[:60]!r}）"
        )
    return md


@app.post("/api/paper/read_table")
async def api_read_table(req: ReadTableRequest):
    """把 PDF 里的一个表格区域读成 Markdown（多模态抄表）。"""
    try:
        pdf_path = _safe_path(BASE_DIR, req.relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail=f"PDF 不存在: {req.relative_path}")
    try:
        rect = tuple(float(x) for x in req.rect.split(","))
        if len(rect) != 4:
            raise ValueError("rect 应为 x0,y0,x1,y1")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"rect 无效: {_safe_err(e)}")
    try:
        table = await _read_table_vision(pdf_path, req.page, rect, req.model)
    except HTTPException:
        raise
    except Exception as e:
        # 可能是 LLMError（上游 429/401…）→ 走人话化，别把英文原文甩给界面
        msg = llm.friendly_error_text(e) if isinstance(e, llm.LLMError) else str(e)
        raise HTTPException(status_code=502, detail=f"表格读取失败: {msg}")
    return {"table": table}


async def _prepare_tables(page_text: str, pdf_path, page: int, model: str = "") -> tuple[str, dict]:
    """把页面文本里的 ⟦TABLE:...⟧ 标记换成中性的 ⟦TABLE_n⟧，
    并把多模态抄出来的表格存起来（翻译完再按位置拼回去）。

    为什么走多模态：表格在 PDF 文本层是碎的（"d (pC/N) (cid:4)90 ..."），
    文本模型只能猜对齐 → 整行数值错位。截图给模型看，顺序和数值都对。

    model：用户在前端选的模型。**必须跟着走** —— 正文用新模型、表格用旧模型
    会让人以为"换模型没生效"（2026-10-25 独立审查）。之前只有独立接口
    /api/paper/read_table 传了，整页翻译这条主路径漏了 → 手动选 Agnes 翻译时
    表格还偷跑 M3，既不一致又烧 M3 的额度。
    """
    from .paper_text import TABLE_MARKER_RE
    tables: dict[str, str] = {}
    matches = list(TABLE_MARKER_RE.finditer(page_text))
    if not matches:
        return page_text, tables

    # 多张表并行读（每张一次多模态调用；一页通常 1~2 张）
    async def _read(idx: int, m) -> tuple[str, str]:
        rect = tuple(float(m.group(i)) for i in range(1, 5))
        token = f"⟦TABLE_{idx}⟧"
        try:
            md = await _read_table_vision(pdf_path, page, rect, model)
            if not md.strip():
                raise RuntimeError("模型返回空内容")
            print(f"[MicroBench] 第 {page} 页表格已用多模态抄出 "
                  f"({len(md)} 字符, rect={['%.0f' % v for v in rect]})",
                  file=sys.stderr)
            return token, md
        except Exception as e:
            print(f"[MicroBench] 第 {page} 页表格读取失败 ({rect}): {e}",
                  file=sys.stderr)
            return token, "【表格未能读出，请对照左侧 PDF 原文】"

    results = await asyncio.gather(*[_read(i, m) for i, m in enumerate(matches)])
    for token, md in results:
        tables[token] = md

    counter = {"i": 0}

    def _repl(_m) -> str:
        token = f"⟦TABLE_{counter['i']}⟧"
        counter["i"] += 1
        return token

    out = TABLE_MARKER_RE.sub(_repl, page_text)
    return out, tables


@app.get("/api/paper/formula_image")
async def api_formula_image(
    relative_path: str = Query(..., min_length=3, max_length=500),
    page: int = Query(..., ge=1, le=2000),
    rect: str = Query(..., min_length=5, max_length=120,
                      description="裁剪区域 x0,y0,x1,y1（PDF 点，左上角原点）"),
    scale: float = Query(4.0, ge=1.0, le=8.0),
):
    """
    按页坐标裁一块区域，返回 PNG —— 给「公式原图」用。

    为什么需要这个接口：公式在 PDF 里是矢量排版（分子一行、分母一行、
    上下标各一行），文本层抽出来是乱序碎片。硬拼 LaTeX 极容易把分子分母
    装反（用户原话："分子分母分不清谁是谁"），而错公式比没公式危险得多
    —— 用户会照着它去算。所以抽取层只记下公式在页面上的坐标，由这里裁
    原图，前端贴进译文：和论文里长得一模一样，绝不会错。
    """
    try:
        pdf_path = _safe_path(BASE_DIR, relative_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail=f"PDF 不存在: {relative_path}")

    try:
        parts = [float(p) for p in rect.split(",")]
    except ValueError:
        raise HTTPException(status_code=400, detail="rect 格式应为 x0,y0,x1,y1")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="rect 格式应为 x0,y0,x1,y1")
    x0, y0, x1, y1 = parts
    if x1 <= x0 or y1 <= y0:
        raise HTTPException(status_code=400, detail="rect 区域无效")

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - env dependent
        raise HTTPException(status_code=500,
                            detail="公式裁图需要 PyMuPDF (fitz), 请先 pip install pymupdf") from exc

    try:
        png = _pdf_clip_png(pdf_path, page, (x0, y0, x1, y1), scale)
    except ValueError as e:
        msg = str(e)
        code = 404 if "页码越界" in msg else 400
        raise HTTPException(status_code=code, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"公式裁图失败: {_safe_err(e)}")

    return Response(
        content=png,
        media_type="image/png",
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
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
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
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
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
        raise HTTPException(status_code=500, detail=f"删除失败: {_safe_err(e)}")


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
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
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
        raise HTTPException(status_code=500, detail=f"系统工具缺失: {_safe_err(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开失败: {_safe_err(e)}")

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
        raise HTTPException(status_code=500, detail=f"PDF 文本提取失败: {_safe_err(e)}")

    if not pages:
        raise HTTPException(status_code=400, detail="未能从 PDF 中识别出有效文字 (可能是扫描图片版 PDF)")

    # 解不出的数学符号（× ≈ ≥ …）：裁图让多模态认字，章节视图也能看到真符号
    #
    # ⚠️ 必须是**整篇一次**（resolve_document_glyphs），不能逐页调
    # _resolve_page_glyphs：逐页 = 36 页的论文发 35 次串行多模态请求
    # （实测 8 分钟以上，用户看到的就是"半天没反应"）。整篇做则全局按语义键
    # 去重（6184 个实例 → 101 个不同符号），通常 1~3 次批量调用就够。
    await _resolve_document_glyphs(pages, pdf_bytes)
    # 后台慢慢把符号认进词典（不挡着用户打开；下次打开/翻译就是真符号）
    _schedule_glyph_backfill(pdf_bytes)

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
    # ⚠ tmp + os.replace —— 直接 write_bytes 的话，写到一半被坚果云上传，
    #   另一台设备会拿到半截 PDF；而同名重复上传还会**静默覆盖**已有那篇。
    _tmp_pdf = save_to.with_suffix(".pdf.tmp")
    _tmp_pdf.write_bytes(pdf_bytes)
    os.replace(_tmp_pdf, save_to)
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
        raise HTTPException(status_code=500, detail=f"扫描 vault 失败: {_safe_err(e)}")

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
        raise HTTPException(status_code=500, detail=f"PDF 文本提取失败: {_safe_err(e)}")

    if not pages:
        raise HTTPException(status_code=400, detail="未能从 PDF 中识别出有效文字 (可能是扫描图片版 PDF)")

    # 解不出的数学符号（× ≈ ≥ …）：裁图让多模态认字，章节视图也能看到真符号
    # ⚠️ 整篇一次（不是逐页）—— 逐页会让 36 页的论文发 35 次串行多模态请求
    await _resolve_document_glyphs(pages, pdf_bytes)
    # 后台慢慢把符号认进词典（不挡着用户打开；下次打开/翻译就是真符号）
    _schedule_glyph_backfill(pdf_bytes)

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
        raise HTTPException(status_code=400, detail=f"参数无效: {_safe_err(e)}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"生成 BibTeX 失败: {_safe_err(e)}")

@app.post("/api/reproduction/create")
async def api_create_reproduction(req: CreateReproductionRequest):
    try:
        res = create_reproduction_workspace(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建复现工程失败: {_safe_err(e)}")

@app.post("/api/troubleshoot/add")
async def api_add_troubleshoot(req: TroubleshootRequest):
    try:
        res = record_troubleshoot(req.model_dump())
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"记录排错日志失败: {_safe_err(e)}")

@app.post("/api/weekly/generate")
async def api_generate_weekly():
    try:
        res = generate_weekly_report()
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成周报失败: {_safe_err(e)}")

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
        # 降格**分类** + **人话原因**：界面必须照它说，不能一律说"额度用完了"
        # （真实反馈：用户额度明明还有，却被告知额度用完）。
        "degraded_kind": st.get("degraded_kind", ""),
        "degraded_text": st.get("degraded_text", ""),
        "degraded_at": st["degraded_at"],
        "primary": st["primary"],
        "fallback": st["fallback"],
        "next_probe_in_seconds": st["next_probe_in_seconds"],
        "rate_limit": st["rate_limit"],
        # 用户自己配的模型（.env LLM_CUSTOM_N_*）：前端按它算限流间隔
        "custom_models": st.get("custom_models", []),
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
        raise HTTPException(status_code=502, detail=f"LLM 调用失败: {llm.friendly_error_text(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"生成摘要失败: {_safe_err(e)}")

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
                f"请检查 .env 中 LLM_BASE_URL / LLM_MODEL_NAME 配置。错误: {llm.friendly_error_text(e)}"
            ),
            "chunks": chunks,
            "llm_used": False,
            "llm_error": llm.friendly_error_text(e),
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
        raise HTTPException(status_code=500, detail=f"图片解析失败: {_safe_err(e)}")

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
        raise HTTPException(status_code=500, detail=f"标定失败: {_safe_err(e)}")

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
        raise HTTPException(status_code=500, detail=f"对比计算失败: {_safe_err(e)}")


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
        # 符号：⟦G:n⟧ 绝不能原样进模型（内部标记 → 照抄/瞎编）。
        # fetch_full_text 内部只查词典（不调模型）→ 拉取不会被认字拖慢。
        result = fetch_full_text(req.identifier)
        # 后台慢慢把符号认进词典（不挡着用户）
        if result.get("pdf_bytes"):
            _schedule_glyph_backfill(result["pdf_bytes"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"全文获取失败: {_safe_err(e)}")

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
13. 原文中的 "⟦FORMULA⟧" 或 "⟦FORMULA:x0,y0,x1,y1⟧" 表示这里是一个公式, 但 PDF 的
    排版（分子分母、上下标各占一行）导致文本无法完整提取。
    **请原样输出这几个字符 (连同冒号后面的坐标一起, 一个数字都不要改)**,
    绝对不要根据上下文猜测、补全, 更不要用你记忆里的公式替代它 ——
    编出来的公式比没有公式危险得多, 用户会照着它去算。
    系统会把带坐标的那种标记换成论文里的公式原图, 所以坐标务必逐字照抄。
14. 原文里的表格请用 **Markdown 表格** 输出: 第一行是表头, 第二行是
    "|---|---|" 这样的分隔行, 之后每行一条记录, 单元格之间用 " | " 分隔。
    不要把表格摊成一行行的普通文字, 也不要自己加 "参数/单位" 之类的列。
15. 表格里的量纲、单位、上下标保持原样 (如 "d31 (pC/N)"、"Tc (°C)"),
    不要翻译成中文单位, 也不要改写数值
16. 原文里出现的 "⟦TABLE_0⟧"、"⟦TABLE_1⟧" 这类标记, 表示那里有一张表格
    (系统已经用截图把表格逐格读好了, 翻译时会原样放回去)。
    **请原样输出这个标记**, 不要改写、不要展开、不要自己编一张表、
    也不要解释它是什么 —— 它的位置就是表格在原文里的位置。
"""


def _translation_deadline_seconds(model: str = "") -> float:
    """单次翻译请求的**总**时限（秒）。

    用户的诉求是「质量优先，但别无限等」：强制思考的模型（M3.1-Flash）整页
    要 45s，一节三段可能要 135s；但不能出现"转十分钟还没完"的情况。

    超时后的行为不是报错，而是**返回已经翻完的部分**并带上 partial 标记，
    前端明确提示「已翻译前 N 段」。可用 LLM_TRANSLATION_DEADLINE 调整
    （秒，30~900，默认 180）。
    """
    raw = (os.getenv("LLM_TRANSLATION_DEADLINE", "") or "").strip()
    if not raw:
        base = 180.0
    else:
        try:
            base = max(30.0, min(float(raw), 900.0))
        except ValueError:
            base = 180.0
    # 用户手动挑了更慢的模型 → 放宽总时限，否则慢模型必然撞线（返回 504）
    if model == "agnes":
        return min(base * 2.5, 900.0)
    if model and model != llm.translation_active_model():
        return min(base * 1.8, 900.0)
    return base


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
    # 用户手动换了模型时，缓存键必须跟着换，否则换模型翻译会命中旧译文。
    _m_name, _m_fallback, model = _resolve_translation_model(req.model)

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
    deadline = time.monotonic() + _translation_deadline_seconds(req.model)
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
                    # 用户手动换了模型时以用户的为准。
                    model_name=_m_name or llm.translation_model_name(),
                    force_fallback=_m_fallback,
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
                    f"{_translation_deadline_seconds(req.model):.0f}s）。"
                    f"可以调大 .env 的 LLM_TRANSLATION_DEADLINE，或换更快的翻译模型。"
                ),
            )
        raise HTTPException(status_code=502, detail=f"LLM 翻译失败: {llm.friendly_error_text(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"翻译失败: {_safe_err(e)}")

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
                            model_name=_m_name or llm.translation_model_name(),
                            force_fallback=_m_fallback,
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
    actual_model = model
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
    model: Optional[str] = Field(default="", max_length=64,
                                 description="用哪个模型回答：'' = 跟随默认, "
                                             "'agnes' = 备用模型, 'custom:xxx' = 自定义模型")


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
        # LLMError（上游 429/401…）要人话化，别把英文原文甩给界面。
        msg = llm.friendly_error_text(e) if isinstance(e, llm.LLMError) else str(e)
        raise HTTPException(status_code=502, detail=f"router LLM 调用失败: {msg}")

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
            detail=f"router 返回非 JSON: {cleaned[:200]} (parse err: {_safe_err(e)})",
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
                **_qa_model_args(req.model),
            )
            async for chunk in _stream_llm_event_gen(chunk_iter):
                yield chunk
        except asyncio.CancelledError:
            # Starlette already closed the response on client disconnect;
            # swallow without emitting (the connection is gone).
            return
        except llm.LLMError as e:
            yield f'data: {json.dumps({"error": llm.friendly_error_text(e)}, ensure_ascii=False)}\n\n'

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
                **_qa_model_args(req.model),
            )
            async for chunk in _stream_llm_event_gen(chunk_iter):
                yield chunk
        except asyncio.CancelledError:
            return
        except llm.LLMError as e:
            yield f'data: {json.dumps({"error": llm.friendly_error_text(e)}, ensure_ascii=False)}\n\n'

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

    # --- 多模态兜底: 表格/曲线图不好文字化时, 把页面截图一起给模型 -----
    # 用户原话: "minimaxm3 是多模态模型, 如果表或者图不好文字化给他的话,
    # 完全可以截图给它, 让它回答问题"。
    #
    # 抽取层对表格/图注只能拿到碎片 ("2 4 3" 这种上下标、坐标轴刻度),
    # 模型看到的是一堆没有意义的字符 —— 这正是「这个表需要注意什么」
    # 问不出答案的原因。截图走 OpenAI 兼容的 image_url (data URL)。
    image_b64 = (req.page_image or "").strip()
    if image_b64.startswith("data:"):
        image_b64 = image_b64.split(",", 1)[-1]
    image_b64 = "".join(image_b64.split())
    if image_b64:
        user_content = [
            {"type": "text", "text": user_msg},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{image_b64}",
                },
            },
        ]
        user_msg = user_content
    else:
        user_content = user_msg

    async def event_gen():
        try:
            st = llm.get_llm_status()
            meta = {"model": _effective_model_name(req.model),
                    "is_degraded": st.get("is_degraded", False)}
            chunk_iter = llm.chat_stream_async(
                messages=[
                    {"role": "system", "content": EXPLAIN_SELECTION_SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=4000,
                temperature=0.3,
                timeout=120,
                **_qa_model_args(req.model),
            )
            async for chunk in _stream_llm_event_gen(chunk_iter, meta=meta):
                yield chunk
        except asyncio.CancelledError:
            return
        except llm.LLMError as e:
            yield f'data: {json.dumps({"error": llm.friendly_error_text(e)}, ensure_ascii=False)}\n\n'

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
    # 用户手动换了模型时，缓存键必须跟着换，否则换模型翻译会命中旧译文。
    _m_name, _m_fallback, model = _resolve_translation_model(req.model)

    # 优先用服务端重新抽取的页面文本：浏览器 PDF.js 抓到的文本在字体子集化
    # 的 PDF 上会丢掉单词空格（"compositestructureshavebeen"），而 pdfplumber
    # 用更小的 x_tolerance 能正确切词，并且能按左栏/右栏顺序输出。
    page_text = req.text
    pdf_path = None
    if req.relative_path:
        try:
            from .generator import BASE_DIR, _safe_path
            pdf_path = _safe_path(BASE_DIR, req.relative_path)
            extracted, page_glyphs = extract_page_text_with_glyphs(pdf_path, req.page)
            if extracted and len(extracted.strip()) >= 100:
                page_text = extracted
                # 解不出的数学符号（× ≈ ≥ …）→ 裁图让多模态认字
                if page_glyphs:
                    page_text = await _resolve_page_glyphs(
                        page_text, page_glyphs, pdf_path, req.page, req.model)
        except Exception:
            pdf_path = None
            pass  # 取不到就用客户端传来的文本

    # Cache key includes paper_id + page so each page is cached independently.
    # 缓存键里还带文本哈希（见 translation.TranslationCache._key_for），
    # 换用更好的抽取文本后会自动 miss，不会命中旧的坏结果。
    cache_section_key = f"page:{req.paper_id}:{req.page}"

    # ⚠️ 顺序很重要：**先查缓存，再做表格读图**（2026-10-25 独立审查）。
    # 表格读图每张要 10~30 秒 + 一次多模态调用；放在缓存查询之前的话，
    # 缓存命中也得先花这个钱和时间 —— 含表格的页等于缓存完全失效。
    # 缓存键用**原始** page_text 的哈希（表格标记替换前后保持一致），
    # 否则存和取会用不同的文本哈希，缓存永远 miss。
    cache_text = page_text
    if not req.force:
        cached = cache.get(cache_section_key, cache_text, model)
        if cached:
            cached["from_cache"] = True
            return cached

    # 表格：文本层抽出来的表格是碎的（数值会整行错位），改成裁图让多模态
    # 模型逐格抄。抄好的表格在翻译完成后按位置拼回去。
    table_map: dict[str, str] = {}
    if pdf_path is not None and "⟦TABLE:" in page_text:
        page_text, table_map = await _prepare_tables(page_text, pdf_path, req.page, req.model)

    # Translate the whole page in one shot (page text is already < 15K chars)
    # If somehow larger, chunk and translate parts.
    chunks = chunk_section_text(page_text)
    translated_parts = []
    # 实际用了哪个模型（用户选的模型连不上时会自动退回主模型/备用模型，
    # 这时必须报告实际那个，不能报告"用户选的"）
    call_info: dict = {}
    # 单次翻译请求的总时限（见 api_translate_section 的同名逻辑）
    deadline = time.monotonic() + _translation_deadline_seconds(req.model)
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
                    # 翻译专用模型（LLM_TRANSLATION_MODEL）；用户手动换了模型时以用户的为准
                    model_name=_m_name or llm.translation_model_name(),
                    force_fallback=_m_fallback,
                    # 整条链路（重试 + 抬预算重试）都受这个时限约束
                    deadline=deadline,
                    # 回填实际用的模型（自定义模型连不上会自动退回）
                    ret=call_info,
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
                    f"{_translation_deadline_seconds(req.model):.0f}s）。"
                    f"可以调大 .env 的 LLM_TRANSLATION_DEADLINE，或换更快的翻译模型。"
                ),
            )
        raise HTTPException(status_code=502, detail=f"按页翻译失败: {llm.friendly_error_text(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"按页翻译失败: {_safe_err(e)}")

    # 合并分块译文（去掉 overlap 造成的重复句）并清洗 LaTeX
    translation = sanitize_translation(merge_translation_parts(translated_parts))
    # 把多模态抄好的表格拼回译文（位置由 ⟦TABLE_n⟧ 标记决定）
    # 拼回译文时前后补空行：模型如果把标记写在句中，Markdown 表格会被插进
    # 段落中间，前端的表格解析会把这一行前面的正文吞成表头第一格
    # （2026-10-25 独立审查）。
    if table_map:
        for token, md in table_map.items():
            if token in translation:
                translation = translation.replace(token, f"\n\n{md}\n\n")
            elif md and "未能读出" not in md:
                # 模型偶尔会把标记弄丢 → 兜底：漏掉的表格追加到译文末尾，绝不丢内容
                translation = f"{translation}\n\n{md}" if translation else md
    translation = re.sub(r"\n{3,}", "\n\n", translation).strip()
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
                            model_name=_m_name or llm.translation_model_name(),
                            force_fallback=_m_fallback,
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
        # 实际用的模型（用户选的模型连不上时会自动退回 → 这里显示实际那个）
        "model": call_info.get("model") or model,
        "model_fell_back": bool(call_info.get("fallback_used")),
        "is_degraded": curr_st["is_degraded"],
        # 残留未翻译的英文段数（完整性校验的结果，正常应为 0）
        "untranslated_runs": len(leaked),
        # 部分完成：超出总时限，后面的分段没翻
        "partial": bool(stopped_reason),
        "translated_chunks": len(translated_parts),
        "stopped_reason": stopped_reason,
    }
    # 缓存写入必须用与读取时完全一致的文本（cache_text = 替换表格标记**之前**
    # 的原始文本），否则哈希对不上，缓存永远不命中。
    cache.put(cache_section_key, cache_text, model, result)
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
            response["ai_error"] = f"LLM 调用失败: {llm.friendly_error_text(e)}"
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
            "extractor_version": data.get("extractor_version"),
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

    # 抽取算法版本对不上 → 整份快照作废。
    #
    # 为什么必须作废而不是照用：快照里的译文是**按当时的抽取算法翻出来的**。
    # 2026-10-24 实测踩坑：10-23 14:17 存的快照（栏序修复之前）被 10-23 16:36
    # 的修复甩在后面，读者照旧恢复旧快照，用户看到的还是"先翻译右边那栏"，
    # 还以为修复没生效 —— 其实代码早就修好了。
    #
    # 作废后返回空页列表，阅读器会照常把没翻的页重新翻一遍（旧的译文本来
    # 就是坏的，重翻不亏）。
    from .paper_text import EXTRACTOR_VERSION
    saved_ver = data.get("extractor_version")
    if saved_ver != EXTRACTOR_VERSION:
        data = dict(data)
        data["pages"] = []
        data["stale_extractor"] = True
        data["saved_extractor_version"] = saved_ver
        data["current_extractor_version"] = EXTRACTOR_VERSION
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

    # ⚠⚠ 合并，不是整份替换。
    #   前端只上传**自己翻过的那几页**（static/index.html 里
    #   `pages.filter(p => p.translated && p.translation)`）。
    #   整份替换的话：A 翻了 1–5 页、B 翻了 6–10 页，
    #   B 同步一次就把 A 的 1–5 页清空了 —— A 下次打开要重翻 5 页，
    #   **重新烧 token**。
    #   （docs/SYNC_CONTRACT.md 声称快照是「整份替换」且「不会互相踩坏」，
    #    实测不成立 —— 快照是整个同步层里唯一会互相踩坏的一路。）
    existing = _safe_read_json(_snapshot_path(safe_id)) or {}
    pages_out = _merge_snapshot_pages(existing, req, pages_out)

    from .paper_text import EXTRACTOR_VERSION
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        # 抽取算法版本：加载时对不上就整份作废（见 api_pdf_snapshot_load）
        "extractor_version": EXTRACTOR_VERSION,
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
        raise HTTPException(status_code=500, detail=f"写盘失败: {_safe_err(e)}")

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


# ---------------------------------------------------------------------------
# PDF reading annotations (人工批注 / 笔记)
# ---------------------------------------------------------------------------
# 用户在读论文时划选一段原文/译文，写下自己的理解。这些批注要能跨会话保留，
# 所以落在 logs/_pdf_snapshots/annotations/<paperId>.json —— 跟翻译快照同一个
# 目录（logs/ 已被 gitignore，不会泄进公开的 Obsidian vault）。
#
# 存储结构：整篇论文一个文件，list of items。前端做增删改后整份 PUT，
# 避免每条批注一次往返（划词标注是高频操作）。

ANNOTATION_MAX_BYTES = 2 * 1024 * 1024
ANNOTATION_MAX_ITEMS = 500
ANNOTATION_COLORS = ("yellow", "green", "blue", "pink", "purple")
# vault 内的默认位置。放在 vault 里 = 跟着 vault 的同步走
# （坚果云 / Obsidian Sync / OneDrive 都一样），不用额外配置。
ANNOTATION_VAULT_SUBDIR = ("01_Literature", "_批注与笔记")
# 旧版本（只在本机 logs/ 下）的位置，保留用于迁移。
ANNOTATION_LEGACY_SUBDIR = ("logs", "_pdf_snapshots", "annotations")


# ---------------------------------------------------------------------------
# 跨设备同步（一键「☁ 同步」）
# ---------------------------------------------------------------------------
# 用户要的是："在一个设备上看了论文、做了标注、划了关键词，点个同步，
# 另一个设备点下同步就能接着看"。所以同步的不只是批注，还有：
#   snapshots/  翻译快照 —— 另一台设备直接拿到已翻好的页，不用重翻、不烧额度
#   annotations/ 批注（高亮 + 笔记）
#   progress/   阅读进度 —— 翻到第几页、缩放多少，直接跳过去
#
# 同步根目录 = ANNOTATIONS_DIR（坚果云目录）→ 那里已经被坚果云客户端同步了。
# 没配的话用 vault 内的 01_Literature/_同步（跟着 vault 自己的同步走）。

SYNC_KINDS = ("snapshots", "annotations", "progress")
SYNC_MAX_FILE_BYTES = 8 * 1024 * 1024
# 坚果云同步空间里的目录名（两台设备必须用同一个名字才能对上）
NUTSTORE_SUBDIR = "MicroBench同步"
# 坚果云客户端自带的云端沙箱名（每台设备都有，名字固定）
NUTSTORE_CLOUD_NAMES = ("我的坚果云", "My Nutstore")
# 客户端自己的缓存目录 —— 不是用户空间，放东西不会上云
NUTSTORE_CACHE_PREFIXES = ("dlcache", "sigs", "tmp")


def _nutstore_users_map() -> dict:
    """读坚果云客户端的 UsersMap.json → {"用户邮箱": "镜像id"}。

    镜像 id 就是同步目录名：Nutstore\\<id>\\ 才是真正的同步空间。
    """
    appdata = os.environ.get("APPDATA") or ""
    if not appdata:
        return {}
    p = Path(appdata) / "Nutstore" / "config" / "UsersMap.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _nutstore_dir():
    """自动探测坚果云里「真正会上云」的目录。

    两个实测踩出来的坑（另一台设备先踩出来的，本机已复现）：

    1. %USERPROFILE%\\Nutstore 本身是**客户端工作目录**，东西放这儿不上云。
       真正的同步空间在 Nutstore\\<镜像id>\\ 里面。
    2. Nutstore\\<镜像id>\\ 下新建顶层文件夹也会被坚果云忽略
       （本地库记 sync_errors reason=7、traffic_history.totalUploadSize 恒为 0），
       必须放进已存在的云端沙箱「我的坚果云」。

    所以探测顺序：
      1. Nutstore\\<镜像id>\\我的坚果云      ← 官方沙箱，两台设备都有
      2. Nutstore\\<镜像id>\\ 下唯一的非缓存目录（用户自建的名字）
      3. 退回 Nutstore 根（老行为，不保证上云）

    有了它，"另一台设备克隆下来就能用"是真的：不用改 .env，只要装了坚果云
    客户端并登录同一账号，同步目录就自动落在真正会同步的地方。
    """
    home = Path(os.path.expanduser("~"))
    roots = []
    for name in ("Nutstore", "坚果云", "Nutstore 同步空间"):
        p = home / name
        if p.is_dir():
            roots.append(p)
    if not roots:
        return None

    um = _nutstore_users_map()
    active = str(um.get("activeUser") or "").strip()
    uid = str((um.get("users") or {}).get(active) or "").strip()

    for root in roots:
        # 镜像 id 目录：优先用 UsersMap.json 里查到的 id
        id_dirs = []
        if uid:
            id_dirs.append(root / uid)
        try:
            for d in sorted(root.iterdir()):
                if d.is_dir() and d.name.isdigit() and d not in id_dirs:
                    id_dirs.append(d)
        except OSError:
            pass

        for id_dir in id_dirs:
            if not id_dir.is_dir():
                continue
            # 1) 官方云端沙箱「我的坚果云」
            for cn in NUTSTORE_CLOUD_NAMES:
                cloud = id_dir / cn
                if cloud.is_dir():
                    return cloud
            # 2) 沙箱被改名了：id_dir 下恰好只有一个非缓存目录
            try:
                subs = [d for d in sorted(id_dir.iterdir())
                        if d.is_dir()
                        and not d.name.lower().startswith(NUTSTORE_CACHE_PREFIXES)]
            except OSError:
                subs = []
            if len(subs) == 1:
                return subs[0]

    # 3) 退回 Nutstore 根（老行为：这是客户端工作目录，不保证上云）
    return roots[0]


def _sync_root() -> Path:
    """同步根目录。优先级：

    1. .env 的 ANNOTATIONS_DIR（显式指定）
    2. 坚果云**云端沙箱**里的 MicroBench同步（自动探测 → 跨设备零配置）
    3. vault 内的 01_Literature/_同步（跟着 vault 自己的同步走）
    """
    raw = (os.getenv("ANNOTATIONS_DIR") or "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = BASE_DIR / p
        return p
    nut = _nutstore_dir()
    if nut is not None:
        return nut / NUTSTORE_SUBDIR
    return BASE_DIR.joinpath(*ANNOTATION_VAULT_SUBDIR)


def _sync_dir(kind: str) -> Path:
    p = _sync_root() / kind
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_paper_key(paper_id: str) -> str:
    """论文 id → 可用作文件名/字典键的安全串。"""
    return "".join(c for c in (paper_id or "") if c.isalnum() or c in "_-")[:128] or "progress"


# ---------------------------------------------------------------------------
# 论文 PDF 实体通道（files/）
#
# 用户要的是「全部直接同步」：PDF 本体也不用手工拷，点一次同步就增量对齐。
# 共享目录里每个 PDF 存两份东西：
#   files/<safePaperId>.pdf   PDF 实体（坚果云负责传到另一台设备）
#   files/<safePaperId>.json  清单：relative_path / size / mtime / sha256
#                              / deleted（墓碑）/ propagate（墓碑是否传播）/ blob
#
# 增量靠 (size, mtime)：两者都没变 → 零传输；变了才算 sha256；
# sha256 相同说明内容其实没变（只更新清单），不同才真的重传。
#
# 删除用墓碑：一方删了本地 PDF → 清单标 deleted。
#   propagate=false（默认）→ 对方只从云端释放实体，本地文件保留（安全）
#   propagate=true          → 对方也删本地 PDF（联动删除）
#
# 空间管理：释放云端实体但保留清单；批注/译文快照/进度一律不动
# （批注和译文比 PDF 值钱，PDF 随时能重新下）。
# ---------------------------------------------------------------------------

SYNC_FILE_SUBDIR = "files"
PDF_MAX_BYTES = 256 * 1024 * 1024
_JS_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _js_base36(n: int) -> str:
    """JS 的 Number.prototype.toString(36)。"""
    if n == 0:
        return "0"
    out = []
    while n:
        out.append(_JS_BASE36[n % 36])
        n //= 36
    return "".join(reversed(out))


def _djb2_base36_utf16(s: str) -> str:
    """JS 的 djb2 + toString(36)，**按 UTF-16 码元**迭代。

    ⚠ JS 的 s.charCodeAt(i) 取 UTF-16 码元，Python 的 ord(ch) 取码点 ——
      路径里有 emoji（代理对）时两端算出的哈希不一样，
      paper_id / 批注 / 快照 / 阅读进度就**静默**对不上。
      实现与 merge_changes.djb2_base36 保持一致（那边本来就写对了）。
    """
    h = 5381
    raw = s.encode("utf-16-le", errors="surrogatepass")
    for i in range(0, len(raw), 2):
        unit = raw[i] | (raw[i + 1] << 8)
        h = ((h << 5) + h + unit) & 0xFFFFFFFF
    return _js_base36(h)


def _merge_snapshot_files(dst: Path, src: Path) -> str | None:
    """合并两个快照文件（按 page_num），返回合并后的 JSON 文本。

    dst 是「已有的一份」，src 是「要并进去的一份」，src 的页优先。
    合并结果与 dst 相同（或没法合）时返回 None —— 不用写盘。

    ⚠ 不能整份 copy：整份替换会让「页数更少的那一份」赢 ——
      A 翻了 1–5 页、B 翻了 6–10 页，B 同步一次，A 的 1–5 页就没了。
    """
    try:
        new = _safe_read_json(src) or {}
        cur = _safe_read_json(dst) or {}
    except OSError:
        return None
    if not isinstance(new, dict) or not new.get("pages"):
        return None
    old_sig = (cur or {}).get("paper_text_signature") or ""
    new_sig = new.get("paper_text_signature") or ""
    if old_sig and new_sig and old_sig != new_sig:
        # 原文变了 —— 旧译文作废，整份以新的为准
        merged = dict(new)
    elif not (_snapshot_extractor_ok(cur) and _snapshot_extractor_ok(new)):
        # ⚠ 有一边是旧抽取算法翻出来的 —— 不能按页合并
        #   （合并会把坏的旧译文合回来，读取时的作废逻辑管不到）。
        if _snapshot_extractor_ok(new):
            merged = dict(new)
        elif _snapshot_extractor_ok(cur):
            merged = dict(cur)
        else:
            # ⚠⚠ 两边**都是**旧的 —— 别因为「都旧」就把内容丢了。
            #   取**有内容**的那一份（本机刚建立、还没内容时，
            #   旧写法会选中空的那份 → 拉一次同步就把本机快照写成 0 页，
            #   看着像「这篇没翻过」，其实是被合并逻辑吃掉了）。
            #   留着旧内容不危险：桌面端读取时会整份作废重翻，
            #   平板端有版本闸门也会拒用 —— 它们都看版本，不看有没有内容。
            merged = dict(new) if new.get("pages") else dict(cur)
    else:
        pages: dict = {}
        for src_pages in ((cur or {}).get("pages") or [], new.get("pages") or []):
            for p in src_pages:
                try:
                    pages[int(p.get("page_num"))] = p
                except (TypeError, ValueError):
                    continue
        if not pages:
            return None
        merged = dict(cur or {})
        merged.update(new)
        merged["pages"] = [pages[k] for k in sorted(pages)]
    text = json.dumps(merged, ensure_ascii=False, indent=2)
    try:
        if dst.exists() and dst.read_text(encoding="utf-8") == text:
            return None
    except OSError:
        pass
    return text


def _snapshot_extractor_ok(data: dict) -> bool:
    """快照的抽取算法版本必须与当前一致。

    ⚠ 这是 2026-10-25 合并两端改动时挖出来的坑：
    对方（10-24）给快照加了 `extractor_version` —— 抽取算法一改，
    旧快照里的译文就是**坏的**（那是按旧算法翻的），读取时整份作废。
    但**磁盘上的旧页还在**，而按 page_num 合并的逻辑会把它们合回来
    ⇒ 过期译文被复活，而读取时的作废逻辑管不到它（它只看最终那一份）。
    所以合并之前必须先看版本：对不上就**不合**。
    """
    try:
        from .paper_text import EXTRACTOR_VERSION
    except Exception:
        return True
    return (data or {}).get("extractor_version") == EXTRACTOR_VERSION


def _merge_snapshot_pages(existing: dict, req, incoming: list) -> list:
    """把新上传的页与**磁盘上已有的页**按 page_num 合并。

    为什么不能整份替换：前端只上传自己翻过的那几页。
    整份替换的话，A 翻了 1–5 页、B 翻了 6–10 页，
    B 同步一次就把 A 的 1–5 页清空了 —— A 要重翻 5 页、重新烧 token。

    源文件变了（paper_text_signature 不同）时不能合并 ——
    老的译文对应的是旧文本，留着就是错的。
    """
    # ⚠ 抽取算法版本对不上 —— 旧译文是按旧算法翻的（坏的），不能合进来
    if not _snapshot_extractor_ok(existing):
        return incoming
    old_pages: dict = {}
    for p in (existing or {}).get("pages") or []:
        try:
            old_pages[int(p.get("page_num"))] = p
        except (TypeError, ValueError):
            continue
    if not old_pages:
        return incoming
    old_sig = (existing or {}).get("paper_text_signature") or ""
    new_sig = getattr(req, "paper_text_signature", "") or ""
    if old_sig and new_sig and old_sig != new_sig:
        # 原文变了 —— 老的译文作废，只留新传的
        return incoming
    merged = dict(old_pages)
    for p in incoming:
        try:
            merged[int(p.get("page_num"))] = p
        except (TypeError, ValueError):
            continue
    return [merged[k] for k in sorted(merged)]


def _paper_id_for(relative_path: str) -> str:
    """复刻前端 index.html 的 paperId 生成：base + djb2(完整相对路径)。

    必须逐字复刻 —— 两台设备算出的 paper_id 不一致，批注/快照/进度就全对不上。
    任何一边改了这条规则，另一边都得同步改。

    ⚠⚠ 哈希必须按 **UTF-16 码元** 算，不是码点。
       JS 的 s.charCodeAt(i) 取 UTF-16 码元（emoji = 代理对，算 2 个），
       Python 的 ord(ch) 取码点（emoji 算 1 个）—— 路径里只要有 emoji
       （或扩展 B 区的汉字），两端算出的 paper_id 就不同，
       批注/快照/阅读进度**静默**认不出来。
       下面直接复用 merge_changes.djb2_base36 —— 那边已经写对了。
    """
    path = (relative_path or "").replace("\\", "/")
    base = path.split("/")[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    base = base[:70] or "paper"
    return f"{base}_{_djb2_base36_utf16(path)}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _files_dir() -> Path:
    d = _sync_root() / SYNC_FILE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _file_meta_path(safe: str) -> Path:
    return _files_dir() / f"{safe}.json"


def _file_blob_path(safe: str) -> Path:
    return _files_dir() / f"{safe}.pdf"


def _safe_local_pdf(relative_path: str):
    """相对路径 → vault 内的绝对路径；越界返回 None。"""
    if not relative_path:
        return None
    try:
        from .generator import _safe_path
        return _safe_path(BASE_DIR, relative_path)
    except Exception:
        return None


def _vault_pdfs() -> list:
    """vault 里所有 PDF：[(相对路径, 绝对路径)]。"""
    out = []
    try:
        for pdf in sorted(BASE_DIR.rglob("*.pdf")):
            if pdf.is_file():
                out.append((str(pdf.relative_to(BASE_DIR)).replace("\\", "/"), pdf))
    except OSError:
        pass
    return out


def _write_file_meta(safe: str, meta: dict) -> None:
    meta["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    path = _file_meta_path(safe)
    # ⚠ 必须 tmp + os.replace。
    #   这个目录在坚果云的同步范围内 —— 直接 write_text 的话，
    #   写到一半就被坚果云上传了，另一台设备读到半截 JSON，
    #   _safe_read_json 静默返回 None → 那篇 PDF 的清单凭空消失。
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _device_id() -> str:
    """本机稳定标识：用来区分「这篇是我发布的」还是「对方发布的」。

    没有它就会出这种事故：A 设备有 1、2 篇，B 设备有 2、3 篇。
    B 同步时看到 1 不在自己机器上 → 误判成「B 把 1 删了」→ 打上墓碑
    → A 下次同步时把自己的 1 删掉。发布者标记让墓碑只对「自己删的」生效。
    """
    p = BASE_DIR / "logs" / "_device_id.json"
    try:
        data = _safe_read_json(p) or {}
        did = (data.get("device_id") or "").strip()
        if did:
            return did
    except OSError:
        pass
    did = uuid.uuid4().hex[:12]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {"device_id": did,
             "created_at": _dt.datetime.now().isoformat(timespec="seconds")},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return did


def _pdf_body_enabled() -> bool:
    """是否通过同步通道传输论文 PDF 本体。

    关掉（.env 里 ``SYNC_PDF_BODY=0``）适用于「vault 整体搬进坚果云 + junction」
    的部署：那时两台设备读的是**同一份**物理文件，PDF 根本不需要再传一份 ——
    传了只会让坚果云多占一倍空间（76MB → 152MB）。

    关闭时 files 通道整体跳过 PDF 相关操作（发布 / 还原 / 墓碑）：
    同一份文件被删，两台天然都看不到，不需要墓碑再通知一次。
    """
    raw = (os.getenv("SYNC_PDF_BODY") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _sync_push_files() -> dict:
    """把 vault 里的 PDF 增量发布到共享目录。

    返回 {"published", "updated", "unchanged", "deleted", "skipped", "errors"}。
    """
    res = {"published": [], "updated": [], "unchanged": 0,
           "deleted": [], "skipped": [], "errors": []}
    if not _pdf_body_enabled():
        res["disabled"] = True
        return res
    files_dir = _files_dir()
    seen = set()
    my_id = _device_id()

    for rel, pdf in _vault_pdfs():
        try:
            st = pdf.stat()
        except OSError:
            continue
        if st.st_size > PDF_MAX_BYTES:
            res["skipped"].append({"name": rel, "reason": "文件过大"})
            continue
        pid = _paper_id_for(rel)
        safe = _safe_paper_key(pid)
        seen.add(safe)
        old = _safe_read_json(_file_meta_path(safe)) or {}
        blob = _file_blob_path(safe)

        # 快速判定：大小和修改时间都没变 → 零传输
        # （blob 已释放也算"没变" —— 尊重用户的释放操作，不偷偷传回去）
        if (old.get("size") == st.st_size
                and abs(float(old.get("mtime") or 0) - st.st_mtime) < 1e-6
                and (blob.exists() or old.get("blob") is False)):
            if not old.get("owner"):
                # 旧清单没有 owner：本机确实持有这份 PDF → 补上归属
                old["owner"] = my_id
                _write_file_meta(safe, old)
            res["unchanged"] += 1
            continue

        try:
            digest = _sha256_file(pdf)
        except OSError as e:
            res["errors"].append({"name": rel, "reason": str(e)})
            continue

        if old.get("sha256") == digest:
            # 内容其实没变（只是 mtime 动了，比如被 touch 过）→ 只更新清单
            old.update({"size": st.st_size, "mtime": st.st_mtime})
            if not old.get("owner"):
                old["owner"] = my_id
            _write_file_meta(safe, old)
            res["unchanged"] += 1
            continue

        # 真的变了 → 传实体 + 更新清单
        try:
            tmp = files_dir / f"{safe}.pdf.tmp"
            shutil.copyfile(pdf, tmp)
            os.replace(tmp, blob)
        except OSError as e:
            res["errors"].append({"name": rel, "reason": str(e)})
            continue
        now = _dt.datetime.now().isoformat(timespec="seconds")
        meta = {
            "paper_id": pid,
            "relative_path": rel,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "sha256": digest,
            "blob": True,
            "deleted": False,
            "propagate": False,
            "owner": my_id,                     # 谁发布的（墓碑只对发布者生效）
            "published_at": old.get("published_at") or now,
            "updated_at": now,
        }
        _write_file_meta(safe, meta)
        (res["published"] if not old else res["updated"]).append(rel)

    # 本地已经没有、但清单还在 → 墓碑
    # ⚠️ 只处理「本机自己发布的」：对方发布的论文本机没下，那不是"被删了"，
    #    那是"还没拿过来"。误标墓碑会让对方把自己的论文删掉。
    for fp in sorted(files_dir.glob("*.json")):
        safe = fp.stem
        if safe in seen:
            continue
        meta = _safe_read_json(fp) or {}
        if meta.get("deleted"):
            continue
        if (meta.get("owner") or "") != my_id:
            continue
        rel = meta.get("relative_path") or ""
        local = _safe_local_pdf(rel) if rel else None
        if local is not None and local.exists():
            continue        # 本机其实还有（可能只是路径挪了）→ 不标墓碑
        meta["deleted"] = True
        meta["deleted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        _write_file_meta(safe, meta)
        res["deleted"].append(rel or safe)

    return res


def _sync_pull_files() -> dict:
    """把共享目录里的 PDF 还原到本机 vault。

    返回 {"restored", "conflicts", "removed", "retained", "skipped"}。
    """
    res = {"restored": [], "conflicts": [], "removed": [],
           "retained": [], "skipped": []}
    if not _pdf_body_enabled():
        res["disabled"] = True
        return res
    files_dir = _files_dir()

    for fp in sorted(files_dir.glob("*.json")):
        safe = fp.stem
        meta = _safe_read_json(fp) or {}
        rel = meta.get("relative_path") or ""
        if not rel:
            continue
        blob = _file_blob_path(safe)

        if meta.get("deleted"):
            local = _safe_local_pdf(rel)
            if meta.get("propagate"):
                # 联动删除：对方也把本地 PDF 删掉
                if local is not None and local.exists():
                    try:
                        local.unlink()
                        res["removed"].append(rel)
                    except OSError as e:
                        res["skipped"].append({"name": rel, "reason": str(e)})
            else:
                res["retained"].append(rel)     # 默认：本机保留
            if blob.exists():
                try:
                    blob.unlink()                # 释放云端空间
                except OSError:
                    pass
            continue

        if not blob.exists():
            res["skipped"].append({"name": rel, "reason": "云端已释放实体"})
            continue

        local = _safe_local_pdf(rel)
        if local is None:
            res["skipped"].append({"name": rel, "reason": "路径越界"})
            continue
        if local.exists():
            try:
                if local.stat().st_size == blob.stat().st_size:
                    res["skipped"].append({"name": rel, "reason": "本机已有"})
                    continue
            except OSError:
                pass
            # 本机有但内容不同 → 不覆盖，交给用户决定（论文被改过不能默默替换）
            res["conflicts"].append({"name": rel, "reason": "本机版本不同，未覆盖"})
            continue
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(blob, local)
            res["restored"].append(rel)
        except OSError as e:
            res["skipped"].append({"name": rel, "reason": str(e)})

    return res


def _local_progress_path() -> Path:
    return BASE_DIR / "logs" / "_reading_state.json"


def _read_local_progress() -> dict:
    return _safe_read_json(_local_progress_path()) or {}


def _write_local_progress(data: dict) -> None:
    p = _local_progress_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _merge_annotation_lists(existing: list, incoming: list, deleted: list) -> list:
    """按 id 合并两边的批注（跨设备不互相覆盖）。

    磁盘没有的 id 新增；两边都有按 updated_at 取新的；deleted 里的删掉
    （墓碑必须最后应用，否则同 id 会被 upsert 又加回来）。
    """
    merged: dict = {}
    for a in existing or []:
        if isinstance(a, dict) and a.get("id"):
            merged[a["id"]] = a
    for a in incoming or []:
        item = a if isinstance(a, dict) else {}
        if not item.get("id"):
            continue
        if item.get("color") not in ANNOTATION_COLORS:
            item = dict(item, color="yellow")
        old = merged.get(item["id"])
        if old is None or _anno_is_newer(item.get("updated_at", ""), old.get("updated_at", "")):
            merged[item["id"]] = item
    for did in deleted or []:
        merged.pop(did, None)
    return sorted(merged.values(),
                  key=lambda x: (x.get("created_at") or "", x.get("id") or ""))


class SyncRequest(BaseModel):
    paper_id: Optional[str] = Field(default=None, max_length=128)
    progress: Optional[dict] = Field(default=None,
                                      description="本机的阅读进度（页码/缩放/滚动位置）")
    push_only: bool = Field(default=False)
    pull_only: bool = Field(default=False)
    include_files: bool = Field(default=True,
                                description="是否连同 PDF 本体一起同步（增量）")


@app.post("/api/sync")
async def api_sync(req: SyncRequest):
    """一键同步：推送本机状态 + 拉取共享目录里的状态（两边都做，幂等）。

    两台设备点同一个按钮即可：推的是自己，拉的是对方，最后合并。
    """
    _t0 = time.monotonic()
    root = _sync_root()
    root.mkdir(parents=True, exist_ok=True)
    pushed: dict = {k: [] for k in SYNC_KINDS}
    pulled: dict = {k: [] for k in SYNC_KINDS}
    pushed["files"] = []
    pulled["files"] = []
    files_report: dict = {"push": {}, "pull": {}}
    snapshot_updated = False

    # ---------- 推送：翻译快照 ----------
    local_snap_dir = BASE_DIR / "logs" / "_pdf_snapshots"
    if not req.pull_only and local_snap_dir.is_dir():
        dst_dir = _sync_dir("snapshots")
        for fp in local_snap_dir.glob("*.json"):
            try:
                if fp.stat().st_size > SYNC_MAX_FILE_BYTES:
                    continue
                dst = dst_dir / fp.name
                # ⚠ 按 page_num 合并，不整份覆盖 ——
                #   否则「页数更少的那一份」会赢，另一台已翻的页被清空。
                merged = _merge_snapshot_files(dst, fp)
                if merged is None:
                    continue
                tmp = dst.with_suffix(".json.tmp")
                tmp.write_text(merged, encoding="utf-8")
                os.replace(tmp, dst)
                pushed["snapshots"].append(fp.name)
            except OSError:
                continue

    # ---------- 推送：批注 ----------
    # 批注现在是"写入即同步"（直接落在共享目录里），所以这里不再从本机
    # logs/ 复制，而是把共享目录里已有的批注列出来，让面板显示真实状态。
    ann_shared_dir = _annotations_dir()
    if not req.pull_only:
        if ann_shared_dir.is_dir():
            for fp in sorted(ann_shared_dir.glob("*.json")):
                pushed["annotations"].append(fp.name)
        # ⚠⚠ 原来这里还有一段「兼容旧版本」：把本机 logs/_pdf_snapshots/annotations/
        #    下的**陈旧副本**读回来，用 _merge_annotation_lists(cur, new, [])
        #    （deleted 传空）合并写回共享目录。
        #    删除墓碑只存在于保存请求的瞬时参数里、**从不落盘**，
        #    于是「已删除的 id」因为不在 cur 里被当成「新增」加回去 ——
        #    A 设备删掉批注，B 设备同步一次，批注就复活了，还回灌给 A。
        #    （实测：A delete -> shared ids: [] -> B sync -> shared ids: ['anno-1']）
        #    本机副本现在只是共享目录的**镜像**（拉取时照抄），
        #    真正的数据源永远是共享目录，所以这段回灌是纯负债 —— 删掉。

    # ---------- 推送：阅读进度 ----------
    if not req.pull_only and req.progress:
        pid = (req.paper_id or req.progress.get("paper_id") or "").strip()
        if pid:
            safe = "".join(c for c in pid if c.isalnum() or c in "_-")[:128] or "progress"
            rec = dict(req.progress)
            rec["paper_id"] = pid
            rec["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            ( _sync_dir("progress") / f"{safe}.json").write_text(
                json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            pushed["progress"].append(safe + ".json")

    # ---------- 推送：论文 PDF 本体（增量，只传变化过的） ----------
    if req.include_files and not req.pull_only:
        rep = _sync_push_files()
        files_report["push"] = rep
        pushed["files"] = list(rep.get("published", [])) + list(rep.get("updated", []))

    # ---------- 拉取：翻译快照（共享 → 本机） ----------
    if not req.push_only:
        src_dir = _sync_dir("snapshots")
        local_snap_dir.mkdir(parents=True, exist_ok=True)
        for fp in src_dir.glob("*.json"):
            try:
                if fp.stat().st_size > SYNC_MAX_FILE_BYTES:
                    continue
                dst = local_snap_dir / fp.name
                # ⚠ 同样按 page_num 合并 —— 共享目录那份不该把本机
                #   已翻的页整块清掉。
                merged = _merge_snapshot_files(dst, fp)
                if merged is None:
                    continue
                tmp = dst.with_suffix(".json.tmp")
                tmp.write_text(merged, encoding="utf-8")
                os.replace(tmp, dst)
                pulled["snapshots"].append(fp.name)
                snapshot_updated = True
            except OSError:
                continue

        # ---------- 拉取：批注（共享 → 本机，本机只是**镜像**） ----------
        # ⚠ 本机副本必须**整体照抄**共享目录，不能按 id 合并 ——
        #   合并会把共享目录里已经删掉的批注又加回来（没有删除墓碑），
        #   而且原来那个「长度没变就跳过」的判断让本机副本**永远不更新**
        #   （改内容但条数不变时更新被丢弃）。
        #   本机副本只是给「升级前的旧位置」当回落用的镜像，
        #   真源永远是共享目录。
        src_ann = _sync_dir("annotations")
        ann_local_dir = BASE_DIR / "logs" / "_pdf_snapshots" / "annotations"
        ann_local_dir.mkdir(parents=True, exist_ok=True)
        for fp in src_ann.glob("*.json"):
            try:
                if fp.stat().st_size > SYNC_MAX_FILE_BYTES:
                    continue
                dst = ann_local_dir / fp.name
                new = _safe_read_json(fp) or {}
                cur = _safe_read_json(dst) or {}
                new_items = new.get("annotations") or []
                if (cur.get("annotations") or []) == new_items:
                    continue
                payload = {"paper_id": new.get("paper_id", fp.stem),
                           "updated_at": (new.get("updated_at")
                                          or _dt.datetime.now().isoformat(timespec="seconds")),
                           "schema_version": 2, "annotations": new_items}
                dst.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                pulled["annotations"].append(fp.name)
            except OSError:
                continue

        # ---------- 拉取：阅读进度 ----------
        prog = _read_local_progress()
        target_pid = (req.paper_id or "").strip()
        if target_pid:
            safe = "".join(c for c in target_pid if c.isalnum() or c in "_-")[:128] or "progress"
            src = _sync_dir("progress") / f"{safe}.json"
            remote = _safe_read_json(src)
            if remote and _anno_is_newer(remote.get("updated_at", ""),
                                         (prog.get(safe) or {}).get("updated_at", "")):
                prog[safe] = remote
                pulled["progress"].append(safe + ".json")
        try:
            _write_local_progress(prog)
        except OSError:
            pass

        # ---------- 拉取：论文 PDF 本体（还原到 vault 原来的相对路径） ----------
        if req.include_files and not req.push_only:
            rep = _sync_pull_files()
            files_report["pull"] = rep
            pulled["files"] = list(rep.get("restored", []))

    return {
        "status": "success",
        "root": str(root),
        "pushed": pushed,
        "pulled": pulled,
        "pushed_total": sum(len(v) for v in pushed.values()),
        "pulled_total": sum(len(v) for v in pulled.values()),
        "snapshot_updated": snapshot_updated,
        "files": files_report,
        "elapsed_ms": int((time.monotonic() - _t0) * 1000) if _t0 else 0,
        "progress": (
            prog.get(_safe_paper_key(req.paper_id)) if req.paper_id else None
        ) if not req.push_only else None,
    }


class FileReleaseRequest(BaseModel):
    """释放云端 PDF 实体（清单和批注保留）。"""
    paper_id: Optional[str] = Field(default=None, max_length=300,
                                    description="要释放的论文 id；留空则配合 all=True")
    all: bool = Field(default=False, description="释放全部 PDF 实体")


@app.post("/api/sync/files/release")
async def api_sync_files_release(req: FileReleaseRequest):
    """释放云端 PDF 实体，腾出坚果云空间。

    只删 files/*.pdf 实体，保留清单（files/*.json）以及批注、译文快照、阅读进度
    —— 那些是真正攒下来的东西，PDF 随时能重新下。
    """
    files_dir = _files_dir()
    targets = []
    if req.all:
        targets = sorted(files_dir.glob("*.json"))
    elif req.paper_id:
        safe = _safe_paper_key(req.paper_id)
        p = _file_meta_path(safe)
        if p.exists():
            targets = [p]
    else:
        raise HTTPException(status_code=400, detail="缺少 paper_id")

    freed = 0
    released = []
    for fp in targets:
        safe = fp.stem
        blob = _file_blob_path(safe)
        if blob.exists():
            try:
                freed += blob.stat().st_size
                blob.unlink()
            except OSError:
                continue
        meta = _safe_read_json(fp) or {}
        meta["blob"] = False
        meta["released_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        _write_file_meta(safe, meta)
        released.append(meta.get("relative_path") or safe)

    return {
        "status": "success",
        "released": len(released),
        "freed_bytes": freed,
        "items": released,
    }


@app.post("/api/sync/files/propagate_delete")
async def api_sync_files_propagate_delete(paper_id: str = Query(..., min_length=3,
                                                                max_length=300)):
    """把某篇论文标成「联动删除」：对方下次同步时也删掉本地 PDF。

    不勾这个开关时，对方只会从云端释放实体，本地 PDF 保留（默认，安全）。
    """
    safe = _safe_paper_key(paper_id)
    p = _file_meta_path(safe)
    meta = _safe_read_json(p) or {}
    if not meta:
        raise HTTPException(status_code=404, detail="共享目录里没有这篇论文")
    meta["propagate"] = True
    meta["deleted"] = True
    meta["deleted_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    _write_file_meta(safe, meta)
    return {"status": "success", "paper_id": paper_id,
            "relative_path": meta.get("relative_path", "")}


def _sync_files_status() -> dict:
    """files/ 通道的状态：云端存了多少 PDF、本机有几篇、哪些是墓碑。"""
    fdir = _sync_root() / SYNC_FILE_SUBDIR
    items = []
    total = 0
    count = 0
    if fdir.is_dir():
        for fp in sorted(fdir.glob("*.json")):
            meta = _safe_read_json(fp) or {}
            rel = meta.get("relative_path") or ""
            safe = fp.stem
            blob = fdir / f"{safe}.pdf"
            blob_exists = blob.exists()
            size = 0
            if blob_exists:
                try:
                    size = blob.stat().st_size
                except OSError:
                    size = 0
                total += size
                count += 1
            local = _safe_local_pdf(rel) if rel else None
            items.append({
                "paper_id": meta.get("paper_id", safe),
                "relative_path": rel,
                "size": meta.get("size", size),
                "blob": blob_exists,
                "deleted": bool(meta.get("deleted")),
                "propagate": bool(meta.get("propagate")),
                "local": bool(local and local.exists()),
                "sha256": (meta.get("sha256") or "")[:12],
            })
    return {"count": count, "total_bytes": total, "items": items}


@app.get("/api/sync/status")
async def api_sync_status():
    """同步目录里有什么、什么时候更新的。"""
    root = _sync_root()
    out = {"root": str(root), "exists": root.is_dir(), "kinds": {}}
    for kind in SYNC_KINDS:
        d = root / kind
        files = []
        if d.is_dir():
            for fp in sorted(d.glob("*.json")):
                try:
                    files.append({"name": fp.name, "size": fp.stat().st_size,
                                  "mtime": _dt.datetime.fromtimestamp(
                                      fp.stat().st_mtime).isoformat(timespec="seconds")})
                except OSError:
                    continue
        out["kinds"][kind] = files
    out["files"] = _sync_files_status()
    out["pdf_body_enabled"] = _pdf_body_enabled()
    return out


class AnnotationItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    page: int = Field(default=0, ge=0, le=2000,
                      description="页码；0 = 章节视图（没有页码）")
    source: str = Field(default="translation", max_length=20,
                        description="translation=译文里的划选, pdf=原文侧划选")
    quote: str = Field(..., min_length=1, max_length=4000,
                       description="被划选的原文片段")
    note: str = Field(default="", max_length=8000, description="用户自己写的笔记")
    color: str = Field(default="yellow", max_length=16)
    rects: list = Field(default_factory=list, max_length=200,
                        description="原文侧高亮矩形（归一化 0~1：x/y/w/h），"
                                    "只在 PDF 原文划选时才有")
    strokes: list = Field(default_factory=list, max_length=200,
                          description="手写笔迹（归一化坐标的点序列 [[x,y,x,y,...],...]），"
                                      "平板上手写标注用；与 rects 互斥")
    created_at: str = Field(default="", max_length=40)
    updated_at: str = Field(default="", max_length=40)


class AnnotationSaveRequest(BaseModel):
    paper_id: str = Field(..., min_length=3, max_length=128)
    annotations: list[AnnotationItem] = Field(default_factory=list,
                                              max_length=ANNOTATION_MAX_ITEMS)
    deleted: list[str] = Field(default_factory=list, max_length=ANNOTATION_MAX_ITEMS,
                               description="本次删除掉的批注 id（跨设备合并不复活）")


def _annotations_dir() -> Path:
    """批注存放目录 = 同步根目录下的 annotations/（和其他同步内容在一起）。"""
    d = _sync_root() / "annotations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _legacy_annotation_paths(paper_id: str) -> list:
    """更早的批注存放位置（读时回落，升级不丢数据）。"""
    safe = "".join(c for c in paper_id if c.isalnum() or c in "_-")[:128] or "annotations"
    return [
        _sync_root() / f"{safe}.json",                                   # 扁平布局
        BASE_DIR.joinpath(*ANNOTATION_LEGACY_SUBDIR) / f"{safe}.json",   # 只在本机 logs/ 下
    ]


def _annotation_path(paper_id: str) -> Path:
    safe = "".join(c for c in paper_id if c.isalnum() or c in "_-")[:128] or "annotations"
    return _annotations_dir() / f"{safe}.json"


def _anno_is_newer(a: str, b: str) -> bool:
    """Compare two ISO-8601 timestamps (both produced by toISOString())."""
    return (a or "") > (b or "")


@app.get("/api/pdf/annotations/load")
async def api_pdf_annotations_load(
    paper_id: str = Query(..., min_length=3, max_length=128),
):
    """Load every annotation of one paper. Missing file -> empty list (not 404).

    新位置（vault 内 / ANNOTATIONS_DIR）没有时，回落到旧的本机位置 ——
    这样升级前的批注不会消失，下次保存就自动迁到新位置。
    """
    data = _safe_read_json(_annotation_path(paper_id))
    if not data:
        for legacy in _legacy_annotation_paths(paper_id):
            data = _safe_read_json(legacy)
            if data:
                break
    if not data:
        return {"paper_id": paper_id, "annotations": []}
    items = data.get("annotations")
    if not isinstance(items, list):
        items = []
    return {"paper_id": paper_id, "annotations": items, "updated_at": data.get("updated_at", "")}


@app.post("/api/pdf/annotations/save")
async def api_pdf_annotations_save(req: AnnotationSaveRequest):
    """Upsert annotations for one paper (write-through, atomic).

    **跨设备合并**：这台机器发来的列表不是"最终状态"，而是"我这边知道的部分"。
    服务端会把它与磁盘上已有的内容按 id 合并：
      - 磁盘上没有的 id → 新增
      - 两边都有 → 谁的 updated_at 新用谁
      - deleted 里的 id → 从磁盘上删掉（否则另一台设备会把已删的批注"复活"）
    这样在两台设备上分别标注不会互相覆盖。
    """
    safe_id = "".join(c for c in req.paper_id if c.isalnum() or c in "_-")[:128] or "annotations"
    now = _dt.datetime.now().isoformat(timespec="seconds")

    target = _annotation_path(safe_id)
    existing = _safe_read_json(target)
    if not existing:
        for legacy in _legacy_annotation_paths(safe_id):
            existing = _safe_read_json(legacy)
            if existing:
                break
    incoming = []
    for a in req.annotations:
        item = a.model_dump()
        if item.get("color") not in ANNOTATION_COLORS:
            item["color"] = "yellow"
        incoming.append(item)

    # 合并规则（删除墓碑最后应用，优先级高于 upsert）
    items = _merge_annotation_lists(
        (existing or {}).get("annotations") or [], incoming, req.deleted or [])

    payload = {
        "paper_id": safe_id,
        "saved_at": now,
        "updated_at": now,
        "schema_version": 2,
        "annotations": items,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=1)
    if len(serialized.encode("utf-8")) > ANNOTATION_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"批注过多(>{ANNOTATION_MAX_BYTES // 1024 // 1024}MB),请先删掉一些",
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, target)
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"批注写盘失败: {_safe_err(e)}")

    return {
        "status": "success",
        "paper_id": safe_id,
        "saved": len(items),
        "updated_at": now,
        "dir": str(target.parent),
    }


@app.delete("/api/pdf/annotations/delete")
async def api_pdf_annotations_delete(
    paper_id: str = Query(..., min_length=3, max_length=128),
    annotation_id: str = Query(..., min_length=1, max_length=64),
):
    """Delete one annotation by id. Idempotent."""
    deleted = 0
    for path in [_annotation_path(paper_id)] + _legacy_annotation_paths(paper_id):
        data = _safe_read_json(path)
        if not data:
            continue
        items = data.get("annotations") or []
        kept = [a for a in items if a.get("id") != annotation_id]
        if len(kept) == len(items):
            continue
        deleted += len(items) - len(kept)
        payload = dict(data)
        payload["annotations"] = kept
        payload["updated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"批注删除失败: {_safe_err(e)}")
    return {"status": "success", "paper_id": paper_id, "deleted": deleted}


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
        raise HTTPException(status_code=400, detail=f"非法路径: {_safe_err(e)}")
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