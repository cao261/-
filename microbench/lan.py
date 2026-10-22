"""
MicroBench LAN mode — helpers for running the workbench on a tablet.

Background
----------
By default MicroBench binds 127.0.0.1 and is reachable only from this PC.
Running it on a tablet over Wi-Fi means binding 0.0.0.0, which exposes the
whole API to every device on the LAN: the vault (all notes), the write
endpoints (they create files on disk) and the LLM endpoints (they spend the
user's API quota).

This module owns the two pieces of that puzzle that are *not* request
handling:

  * provisioning / reading the shared access token
  * discovering the LAN address to print in the pairing instructions

The request-time enforcement lives in app.py (token guard + same-origin
guard) because it needs the Request object.
"""

from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path

# Token file lives under logs/ which is already gitignored, so the token
# never lands in version control.
TOKEN_FILENAME = "lan_token.txt"


def _repo_root() -> Path:
    # microbench/lan.py -> microbench/ -> repo root
    return Path(__file__).resolve().parent.parent


def token_path(base_dir: Path | None = None) -> Path:
    """Absolute path of the persisted LAN token."""
    root = Path(base_dir) if base_dir else _repo_root()
    return root / "logs" / TOKEN_FILENAME


def read_token(base_dir: Path | None = None) -> str:
    """Return the persisted token, or "" when none has been provisioned yet."""
    path = token_path(base_dir)
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def ensure_token(base_dir: Path | None = None) -> str:
    """Return the persisted token, creating it on first use.

    The token survives restarts on purpose: if it rotated every time the
    server came up, the tablet would ask for the password again after every
    reboot, which is exactly the friction that makes people turn the whole
    thing off.
    """
    existing = read_token(base_dir)
    if existing:
        return existing
    token = secrets.token_urlsafe(9)  # 12 chars, URL-safe, no ambiguous 0/O
    path = token_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write atomically-ish: a truncated token file would lock the tablet out.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token, encoding="utf-8")
    tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows / non-POSIX: best effort only
    return token


def resolve_token(base_dir: Path | None = None) -> str:
    """The token the server should enforce.

    Precedence:
      1. MB_LAN_TOKEN env var  (explicit override, wins)
      2. the persisted token file
      3. "" -> LAN access stays closed (fail closed, never "no token = open")
    """
    env = os.environ.get("MB_LAN_TOKEN", "").strip()
    if env:
        return env
    return read_token(base_dir)


def get_lan_ip() -> str:
    """Best-effort primary LAN IPv4 address of this machine.

    Opens a UDP socket toward a public address — no packets are actually
    sent, this only asks the OS which local interface would be used. If the
    machine is offline or blocked, we fall back to parsing the hostname.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.3)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        sock.close()

    # Fallback: resolve the hostname and pick a private address.
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                return ip
    except OSError:
        pass
    return "127.0.0.1"


def pairing_url(host: str, port: int, token: str) -> str:
    """URL the user types once on the tablet to pair it.

    The token travels in the query string so the tablet can authenticate
    itself without the user having to find and paste a separate code.
    """
    return f"http://{host}:{port}/?k={token}"


def lan_hosts() -> list[str]:
    """Loopback names that always count as "this machine"."""
    return ["127.0.0.1", "localhost", "::1", "[::1]"]


if __name__ == "__main__":
    # `python -m microbench.lan [port] [--emit-env <file>]`
    #
    # --emit-env writes a .cmd file with the three values as SET lines, so
    # the launcher script can `call` it and pick the values up in the same
    # shell. Parsing three separate `for /f` loops out of stdout would
    # work too, but it spawns Python three times and is easy to get wrong.
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = int(args[0]) if args else 5000
    emit = None
    if "--emit-env" in sys.argv:
        emit = sys.argv[sys.argv.index("--emit-env") + 1]

    tok = ensure_token()
    ip = get_lan_ip()
    url = pairing_url(ip, port, tok)

    if emit:
        path = Path(emit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'set "LAN_IP={ip}"\n'
            f'set "LAN_TOKEN={tok}"\n'
            f'set "PAIRING_URL={url}"\n',
            encoding="utf-8",
        )
    print(f"LAN_IP={ip}")
    print(f"LAN_TOKEN={tok}")
    print(f"PAIRING_URL={url}")
