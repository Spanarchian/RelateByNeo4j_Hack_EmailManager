"""Health endpoint (unauthenticated; reveals no secrets or personal data)."""

from __future__ import annotations

from fastapi import APIRouter, Response

from email_service import __version__
from email_service.dependencies import ContainerDep

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness and graph readiness")
async def health(container: ContainerDep, response: Response) -> dict[str, object]:
    graph_ok = await container.db.ping()
    if not graph_ok:
        response.status_code = 503
    return {
        "status": "ok" if graph_ok else "degraded",
        "version": __version__,
        "graph": {"engine": "relate", "status": "ok" if graph_ok else "unavailable"},
        "providers": container.provider_service.status(),
        "failover_enabled": container.provider_service.failover_enabled,
    }
