#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
End-to-end check of LAN mode against a *running* server.

Unlike microbench/_test_lan.py (which drives the ASGI app in-process), this
script talks real HTTP to a real uvicorn bound to 0.0.0.0 and reaches it
through the machine's LAN address — i.e. it arrives from a non-loopback
client, exactly like a tablet would.

Usage:
    python microbench/_e2e_lan.py [host] [port] [token]
"""

import json
import sys
import urllib.error
import urllib.request

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.101"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
TOKEN = sys.argv[3] if len(sys.argv) > 3 else ""
if not TOKEN:
    try:
        from pathlib import Path

        TOKEN = (
            Path(__file__).resolve().parent.parent
            / "logs"
            / "lan_token.txt"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        print("no token given and no token file found")
        sys.exit(2)

BASE = f"http://{HOST}:{PORT}"

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


def req(path, method="GET", headers=None, data=None, redirect=False):
    """Return (status, body_text, headers). Never raises on HTTP errors."""
    url = path if path.startswith("http") else BASE + path
    r = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler if redirect else _NoRedirect
    )
    try:
        with opener.open(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), _ci(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), _ci(e.headers)
    except Exception as e:  # connection refused etc.
        return 0, str(e), {}


def _ci(headers):
    """Header dict with case-insensitive keys.

    urllib lower-cases header names, so `dict(headers)["Set-Cookie"]` is
    always a miss.
    """
    return {k.lower(): v for k, v in headers.items()}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    print(f"\nTarget: {BASE}   token: {TOKEN}\n")

    print("[1] Unauthenticated LAN client")
    st, body, _ = req("/api/stats")
    check(st == 401, f"GET /api/stats -> 401 (got {st})")
    try:
        check(json.loads(body).get("error") == "token_required", "body says token_required")
    except Exception:
        check(False, "body is JSON")

    st, body, _ = req("/")
    check(st == 200, f"GET / -> 200 pairing page (got {st})")
    check("访问口令" in body, "pairing page asks for the access code")
    check("MicroBench" in body, "pairing page is branded")

    print("\n[2] Wrong token")
    st, _, _ = req("/api/stats", headers={"X-MB-Token": "nope"})
    check(st == 401, f"GET /api/stats with bad token -> 401 (got {st})")

    print("\n[3] Correct token (header)")
    st, body, _ = req("/api/stats", headers={"X-MB-Token": TOKEN})
    check(st == 200, f"GET /api/stats with token -> 200 (got {st})")
    st, _, _ = req("/api/auth/status", headers={"X-MB-Token": TOKEN})
    check(st == 200, "GET /api/auth/status -> 200")

    print("\n[4] Correct token (cookie)")
    st, body, hdr = req("/api/auth/verify", method="POST",
                         headers={"Content-Type": "application/json"},
                         data=json.dumps({"token": TOKEN}).encode())
    check(st == 200, f"POST /api/auth/verify -> 200 (got {st})")
    cookie = hdr.get("set-cookie", "")
    check("mb_token=" in cookie, "server set the mb_token cookie")
    check("httponly" in cookie.lower(), "cookie is HttpOnly")
    check("samesite=lax" in cookie.lower(), "cookie is SameSite=Lax")

    cookie_header = cookie.split(";")[0]
    st, _, _ = req("/api/stats", headers={"Cookie": cookie_header})
    check(st == 200, f"GET /api/stats with cookie -> 200 (got {st})")

    print("\n[5] Pairing link (?k=TOKEN)")
    st, body, hdr = req(f"/?k={TOKEN}")
    check(st == 303, f"GET /?k=TOKEN -> 303 redirect (got {st})")
    check("mb_token=" in hdr.get("set-cookie", ""), "redirect sets the cookie")
    st, body, hdr = req(f"/?k=wrong", redirect=True)
    check(st == 200 and "口令不正确" in body, "wrong pairing link -> pairing page with an error")

    print("\n[6] Cross-site requests are still refused")
    st, _, _ = req("/api/__probe__", method="POST",
                   headers={"Content-Type": "application/json",
                            "Origin": "https://evil.com",
                            "X-MB-Token": TOKEN},
                   data=b"{}")
    check(st == 403, f"cross-site POST with token -> 403 (got {st})")

    st, _, _ = req("/api/__probe__", method="POST",
                   headers={"Content-Type": "application/json",
                            "Referer": "https://evil.com/x.html",
                            "X-MB-Token": TOKEN},
                   data=b"{}")
    check(st == 403, f"cross-site Referer with token -> 403 (got {st})")

    st, _, _ = req("/api/__probe__", method="POST",
                   headers={"Content-Type": "application/json",
                            "Origin": f"http://{HOST}:{PORT}",
                            "X-MB-Token": TOKEN},
                   data=b"{}")
    check(st == 404, f"same-origin POST with token -> 404 (got {st}, guard allowed it)")

    print("\n[7] PWA assets")
    st, body, hdr = req("/manifest.webmanifest")
    check(st == 200, f"GET /manifest.webmanifest -> 200 (got {st})")
    try:
        m = json.loads(body)
        check(m.get("display") == "standalone", "manifest display=standalone")
        check(len(m.get("icons", [])) >= 3, "manifest ships at least 3 icons")
    except Exception:
        check(False, "manifest is valid JSON")
    for icon in ("icon-192.png", "icon-512.png", "apple-touch-icon.png",
                 "icon-maskable-512.png"):
        st, _, hdr = req(f"/pwa/{icon}")
        check(st == 200, f"GET /pwa/{icon} -> 200 (got {st})")
    st, _, _ = req("/pwa/../../../app.py")
    check(st in (400, 404), f"path traversal on /pwa/ is refused (got {st})")

    print("\n[8] The app shell itself")
    st, body, _ = req("/", headers={"X-MB-Token": TOKEN})
    check(st == 200, f"GET / with token -> 200 (got {st})")
    check("viewport-fit=cover" in body, "index.html declares viewport-fit=cover")
    check("apple-mobile-web-app-capable" in body, "index.html is iOS standalone capable")
    check("/manifest.webmanifest" in body, "index.html links the manifest")
    check("pointer: coarse" in body, "index.html ships the touch stylesheet")

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    for f in FAIL:
        print(f"  FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
