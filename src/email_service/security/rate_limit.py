"""In-process fixed-window rate limiter.

Adequate for the single-replica deployment this service requires (the Relate
store has one writer). Behind a reverse proxy, also rate limit there; see the
security documentation.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from email_service.security.authentication import API_KEY_HEADER

EXEMPT_PATHS = frozenset({"/health"})


class FixedWindowLimiter:
    def __init__(self, limit_per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit_per_minute
        self._clock = clock
        self._windows: dict[str, tuple[int, int]] = {}

    def allow(self, client: str) -> tuple[bool, int]:
        """Returns ``(allowed, seconds_until_reset)``."""
        now = self._clock()
        window = int(now // 60)
        current_window, count = self._windows.get(client, (window, 0))
        if current_window != window:
            count = 0
        count += 1
        self._windows[client] = (window, count)
        if len(self._windows) > 10_000:  # bound memory
            self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
        return count <= self.limit, 60 - int(now % 60)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, limiter: FixedWindowLimiter) -> None:
        super().__init__(app)
        self._limiter = limiter

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if self._limiter.limit <= 0 or request.url.path in EXEMPT_PATHS:
            return await call_next(request)
        key = request.headers.get(API_KEY_HEADER)
        client = (
            "key:" + hashlib.sha256(key.encode()).hexdigest()[:16]
            if key
            else "ip:" + (request.client.host if request.client else "unknown")
        )
        allowed, reset = self._limiter.allow(client)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"error": {"code": "rate_limited", "message": "too many requests"}},
                headers={"Retry-After": str(reset)},
            )
        return await call_next(request)
