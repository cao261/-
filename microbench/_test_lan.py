# -*- coding: utf-8 -*-
"""
Regression guards for LAN (tablet) mode.

The workbench used to be reachable only from 127.0.0.1, and its cross-origin
guard hard-coded "the Origin host must be a loopback name". Both changed when
the tablet became a first-class target:

  * a shared token now gates every non-loopback client
  * the origin guard has two paths — strict loopback rules for this PC,
    same-origin rules for LAN clients

Both paths must hold at the same time. A too-strict guard breaks the tablet;
a too-loose one re-opens the CSRF hole the audit closed.

Run:  python -m microbench._test_lan
"""

import sys

from fastapi.testclient import TestClient

from . import app as app_module
from .app import app

PASS, FAIL = [], []


def check(cond, label):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")


LOOPBACK = ("127.0.0.1", 55555)
TABLET = ("192.168.1.77", 55555)

TOKEN = "test-token-123"


def client_for(peer, token=None, base="http://192.168.1.101:5000"):
    """Build a TestClient whose peer address we control."""
    headers = {}
    if token:
        headers["X-MB-Token"] = token
    return TestClient(app, client=peer, base_url=base, headers=headers)


def main() -> int:
    # The token is read at import time, so patch the module attribute the
    # middleware actually consults.
    app_module._LAN_TOKEN = TOKEN

    print("\n[1] Loopback client (this PC) — desktop behaviour must be untouched")
    with client_for(LOOPBACK) as c:
        r = c.get("/api/auth/status")
        check(r.status_code == 200, "GET /api/auth/status -> 200")
        body = r.json()
        check(body["auth_required"] is False, "loopback never requires a token")
        check(body["loopback"] is True, "loopback is reported as loopback")

        r = c.get("/api/stats")
        check(r.status_code == 200, "GET /api/stats without token -> 200")

        # DNS-rebinding defence must survive: a hostile page that resolved
        # evil.com to 127.0.0.1 sends Origin: https://evil.com.
        r = c.post(
            "/api/__lan_probe__",
            json={"content": "x"},
            headers={"Origin": "https://evil.com"},
        )
        check(r.status_code == 403, "loopback + evil Origin -> 403 (rebinding blocked)")

    print("\n[2] LAN client without a token — must be refused")
    with client_for(TABLET) as c:
        r = c.get("/api/stats")
        check(r.status_code == 401, "GET /api/stats without token -> 401")
        check(
            r.json().get("error") == "token_required",
            "401 body carries error=token_required",
        )

        r = c.get("/api/auth/status")
        check(r.status_code == 200, "GET /api/auth/status is reachable without token")
        body = r.json()
        check(body["auth_required"] is True, "LAN client is told a token is required")
        check(body["ok"] is False, "LAN client without token reports ok=false")

    print("\n[3] LAN client with a wrong token — must be refused")
    with client_for(TABLET, token="wrong-token") as c:
        r = c.get("/api/stats")
        check(r.status_code == 401, "GET /api/stats with wrong token -> 401")

    print("\n[4] LAN client with the right token — must work")
    with client_for(TABLET, token=TOKEN) as c:
        r = c.get("/api/auth/status")
        check(r.json()["ok"] is True, "status reports ok=true with the right token")

        r = c.get("/api/stats")
        check(r.status_code == 200, "GET /api/stats with token -> 200")

        # Same-origin write: Origin must equal the Host the browser used.
        # We POST to a route that does not exist on purpose — the origin
        # guard is middleware, so it runs before routing. 404 proves the
        # guard let the request through, 403 would prove it blocked it.
        # Nothing is written to the vault either way.
        r = c.post(
            "/api/__lan_probe__",
            json={"content": "x"},
            headers={"Origin": "http://192.168.1.101:5000"},
        )
        check(r.status_code == 404, "same-origin POST passes the origin guard (404, not 403)")

    print("\n[5] LAN client with a valid token but a cross-site origin — must be refused")
    with client_for(TABLET, token=TOKEN) as c:
        r = c.post(
            "/api/__lan_probe__",
            json={"content": "x"},
            headers={"Origin": "https://evil.com"},
        )
        check(r.status_code == 403, "cross-site POST even with token -> 403")

        r = c.post(
            "/api/__lan_probe__",
            json={"content": "x"},
            headers={"Referer": "https://evil.com/x.html"},
        )
        check(r.status_code == 403, "cross-site Referer even with token -> 403")

    print("\n[6] Origin parsing")
    from .app import _origin_matches_host

    check(
        _origin_matches_host("http://192.168.1.101:5000", "192.168.1.101:5000"),
        "origin == host header (ip:port) matches",
    )
    check(
        _origin_matches_host("http://192.168.1.101:5000", "192.168.1.101"),
        "origin with port matches a host header without one",
    )
    check(
        not _origin_matches_host("http://192.168.1.101:5000", "192.168.1.102:5000"),
        "different ip does not match",
    )
    check(
        not _origin_matches_host("http://192.168.1.101:5000", "192.168.1.101:5001"),
        "different port does not match",
    )
    check(
        not _origin_matches_host("https://evil.com", "192.168.1.101:5000"),
        "foreign origin does not match",
    )
    check(
        not _origin_matches_host("null", "192.168.1.101:5000"),
        "origin 'null' (sandboxed iframe) does not match",
    )
    check(
        _origin_matches_host("http://[::1]:5000", "[::1]:5000"),
        "ipv6 literal host header matches",
    )

    print("\n[7] Token comparison")
    from .app import _is_loopback_host

    check(_is_loopback_host("127.0.0.1"), "127.0.0.1 is loopback")
    check(_is_loopback_host("127.1.2.3"), "127.0.0.0/8 is loopback")
    check(_is_loopback_host("::1"), "::1 is loopback")
    check(not _is_loopback_host("192.168.1.5"), "192.168.1.5 is not loopback")
    check(not _is_loopback_host("10.0.0.1"), "10.0.0.1 is not loopback")
    check(not _is_loopback_host(""), "empty host is not loopback")

    print("\n[8] Missing token file means LAN stays closed (fail closed)")
    app_module._LAN_TOKEN = ""
    with client_for(TABLET, token=TOKEN) as c:
        r = c.get("/api/stats")
        check(r.status_code == 401, "no configured token -> LAN denied even with a guess")
    app_module._LAN_TOKEN = TOKEN

    print("\n[9] In-process client (peer is not an IP) must stay local")
    # Starlette's TestClient reports the literal string "testclient" as the
    # peer address. It is not a machine on the Wi-Fi, so it must not be
    # asked for a token — otherwise the whole pytest suite (which talks to
    # the app in-process) starts failing with 401.
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/api/stats")
        check(r.status_code == 200, f"in-process client GET /api/stats -> 200 (got {r.status_code})")
        r = c.get("/api/auth/status")
        check(r.json()["auth_required"] is False,
              "in-process client is told no token is required")

    from .app import _is_remote_peer

    check(not _is_remote_peer("testclient"), "'testclient' is not a remote peer")
    check(not _is_remote_peer(""), "empty host is not a remote peer")
    check(not _is_remote_peer("127.0.0.1"), "127.0.0.1 is not a remote peer")
    check(not _is_remote_peer("::1"), "::1 is not a remote peer")
    check(_is_remote_peer("192.168.1.77"), "192.168.1.77 is a remote peer")
    check(_is_remote_peer("10.0.0.5"), "10.0.0.5 is a remote peer")
    check(_is_remote_peer("2.2.2.2"), "2.2.2.2 is a remote peer")

    print(f"\n{'=' * 60}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print(f"  FAILED: {f}")
        return 1
    print("All LAN-mode regression guards passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
