"""Cloud Run probes.

`/healthz` is liveness: the process is up and serving. It must not touch a dependency,
or a Firestore blip would get healthy instances killed.

`/readyz` is readiness: this instance can serve traffic, which does mean reaching
Firestore. docs/07-infra-deploy.md puts an uptime check on `/healthz`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from coach.api.deps import Container, get_container

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz", include_in_schema=False)
async def readyz(request: Request) -> JSONResponse:
    container: Container = get_container(request)
    try:
        # The cheapest possible round-trip that proves credentials, network, and the
        # database name are all right: read one document that need not exist. The id is
        # deliberately plain — Firestore rejects any id matching `__.*__`, so the
        # obvious-looking `__probe__` would fail the probe for the wrong reason.
        await container.user_repository.get("readiness-probe")
    except Exception as exc:
        logger.warning("readiness probe failed", extra={"error": str(exc)})
        return JSONResponse(
            status_code=503, content={"status": "unavailable", "dependency": "firestore"}
        )
    return JSONResponse(status_code=200, content={"status": "ok"})
