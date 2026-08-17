"""Cloud Run probes.

`/livez` is liveness: the process is up and serving. It must not touch a dependency, or a
Firestore blip would get healthy instances killed.

`/readyz` is readiness: this instance can serve traffic, which does mean reaching
Firestore. docs/07-infra-deploy.md puts an uptime check on `/livez`.

**Why `/livez` and not `/healthz`.** Google's frontend intercepts `/healthz` on Cloud Run
and answers it itself, with its own HTML 404 — "The requested URL /healthz was not found
on this server" — without ever forwarding the request to the container. Observed on
`coach-api` in `us-central1`: `/healthz` returned a Google 404 while `/readyz`, `/api/me`,
and every unmatched path reached the app normally, and `/livez`, `/health`, `/_health`,
`/ping`, and `/status` were all reachable.

The failure is nastier than it sounds because it is invisible from inside:

- Cloud Run's own startup and liveness probes are issued **to the container**, bypassing
  the frontend, so `/healthz` passes them and the revision reports healthy.
- The local container, the e2e harness, and every test also bypass the frontend, so
  `/healthz` passes everywhere except the one place that matters.

What breaks is anything reaching the service from outside: the deploy smoke test, and the
Cloud Monitoring uptime check, which would have failed silently forever.

`tests/test_liveness_path.py` pins the name so this is not quietly renamed back.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from coach.api.deps import Container, get_container

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

#: The liveness path. Not `/healthz` — see the module docstring.
LIVENESS_PATH = "/livez"

#: Reserved by Google's frontend on Cloud Run; a request to it never reaches the app.
INTERCEPTED_LIVENESS_PATH = "/healthz"


@router.get(LIVENESS_PATH, include_in_schema=False)
async def livez() -> dict[str, str]:
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
