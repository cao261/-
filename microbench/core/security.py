# -*- coding: utf-8 -*-
"""后端安全辅助：鉴权暴力破解限流、Host/Origin IPv6 校验、PDF 路径白名单。"""
from __future__ import annotations

import html
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from ..generator import is_vault_content_path


def escape_html_text(value: Optional[str]) -> str:
    """对写回 403/HTML 错误页的请求头内容做 HTML 转义，防止反射 XSS。"""
    return html.escape(value or "", quote=True)


def extract_host_port(host_header: str) -> Optional[int]:
    """安全解析 Host 请求头里的端口，正确处理裸 IPv6 `[::1]`（无端口）。"""
    hh = (host_header or "").strip()
    if not hh:
        return None
    if hh.startswith("["):
        end = hh.find("]")
        if end == -1:
            return None
        rest = hh[end + 1:].strip()
        if not rest:
            return 80
        if rest.startswith(":"):
            try:
                return int(rest[1:])
            except ValueError:
                return None
        return None
    if ":" in hh:
        _, _, port_s = hh.rpartition(":")
        try:
            return int(port_s)
        except ValueError:
            return None
    return 80


class AuthAttemptLimiter:
    """/api/auth/verify 按来源 IP 统计连续失败次数，防局域网口令暴力枚举。"""

    def __init__(self, max_failures: int = 10, window_seconds: float = 60.0):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def is_blocked(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = [t for t in self._failures.get(key, ()) if t > cutoff]
            if hits:
                self._failures[key] = hits
            else:
                self._failures.pop(key, None)
            if len(hits) >= self.max_failures:
                retry = max(1, int(hits[0] + self.window_seconds - now + 1))
                return True, retry
            return False, 0

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            hits = [t for t in self._failures.get(key, ()) if t > cutoff]
            hits.append(now)
            self._failures[key] = hits
            # 防止恶意伪造大量源键导致内存无限增长
            if len(self._failures) > 512:
                stale = [k for k, v in self._failures.items() if not any(t > cutoff for t in v)]
                for k in stale[:256]:
                    self._failures.pop(k, None)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


AUTH_LIMITER = AuthAttemptLimiter()


def assert_vault_pdf_scope(base_dir: Path, path: Path, detail: str = "只能访问 vault 内容目录下的 PDF") -> None:
    """将 PDF 读写入口收紧到 VAULT_CONTENT_DIRS（01_Literature 等），阻断仓库内探测。"""
    if not is_vault_content_path(base_dir, path):
        raise HTTPException(status_code=400, detail=detail)
