"""FastAPI application factory.

Run with ``uvicorn --factory email_service.main:create_app`` (see ``task run``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from email_service import __version__
from email_service.api import approvals, emails, health, onboarding
from email_service.api.errors import register_error_handlers
from email_service.config import Settings, get_settings
from email_service.dependencies import Container, build_container
from email_service.security.rate_limit import FixedWindowLimiter, RateLimitMiddleware
from email_service.security.redaction import configure_logging


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    if container is None:
        configure_logging(settings.log_level)
    resolved = container or build_container(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await resolved.start()
        app.state.container = resolved
        try:
            yield
        finally:
            await resolved.stop()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Prepare, review, approve and send onboarding emails. Authenticate with the "
            "`X-API-Key` header. No email is sent without explicit human approval."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.add_middleware(
        RateLimitMiddleware, limiter=FixedWindowLimiter(settings.rate_limit_per_minute)
    )
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(emails.router)
    app.include_router(approvals.router)
    app.include_router(onboarding.router)
    return app
