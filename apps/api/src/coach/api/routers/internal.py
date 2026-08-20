"""`/internal/*` — the two endpoints Google's own services call.

docs/04-api-contract.md#internal and docs/05-autonomous-runs.md#trigger-chain:

| Path | Caller | Work |
| --- | --- | --- |
| `POST /internal/tick` | Cloud Scheduler, every 15 min | Plan. Cheap, bounded, ≤ 30 s |
| `POST /internal/runs/{runId}/execute` | Cloud Tasks | Do. Up to 15 min, one run |

**Two service accounts, verified separately.** `Settings` carries
`ALLOWED_SCHEDULER_SA` and `ALLOWED_TASKS_SA` as distinct values, and collapsing them
into one allow-list would let Cloud Scheduler invoke the executor or the reverse — which
is not a hypothetical privilege, since the executor is the endpoint that spends money.

**The token is verified for issuer, audience, *and* caller email.** Any Google account
can mint an OIDC token for any audience, so the audience alone proves only that the caller
knew the service URL. The email claim is what proves it is our scheduler.

**`ENV=local` skips OIDC entirely**, which is the third deliberate local-only surface in
this project, on the same terms as `Bearer dev:<uid>` and `MODEL_BACKEND=stub`: one
`settings.is_local` check, and `tests/test_internal_oidc_guard.py` pins that it is inert
for every other `ENV`. Without it `dev.sh tick` could not reach the tick at all, and the
whole autonomous path would be unexercisable on a laptop
(docs/05-autonomous-runs.md#local-development).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from coach.api.deps import Container, get_container
from coach.core.config import Settings
from coach.core.errors import NotAuthenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

#: Google's OIDC issuer for service-account identity tokens.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class InternalCaller:
    """Verifies one inbound OIDC token against one named service account.

    A class rather than a function so that *which* account an endpoint accepts is visible
    in the route signature and assertable by inspection — the same reasoning as
    `api/auth.py`'s two `Authenticated` instances.
    """

    def __init__(self, setting_name: str) -> None:
        self._setting_name = setting_name

    async def __call__(self, request: Request) -> str:
        settings: Settings = request.app.state.settings
        if settings.is_local:
            # -------------------------------------------------------------------------
            # DELIBERATE LOCAL-ONLY SURFACE. There is no Cloud Scheduler and no Cloud
            # Tasks on a laptop, so without this the autonomous path is unreachable
            # locally and by the e2e suite. Guarded by this one check;
            # `tests/test_internal_oidc_guard.py` asserts it is inert for every other ENV.
            # -------------------------------------------------------------------------
            return "local"
        expected = getattr(settings, self._setting_name, None)
        if not expected:  # pragma: no cover - `Settings` refuses to boot without these
            raise NotAuthenticated(f"{self._setting_name.upper()} is not configured.")
        return _verify_oidc(request, settings, expected_email=expected)


def _verify_oidc(request: Request, settings: Settings, *, expected_email: str) -> str:
    """Issuer, audience, and caller email. All three, or the request is not ours."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise NotAuthenticated("Expected an 'Authorization: Bearer <token>' header.")

    # Imported here rather than at module scope: `google.oauth2.id_token` pulls in a
    # transport that resolves credentials, and this module is imported while the app is
    # being constructed (`coach/core/lazy.py` on why that matters).
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    audience = settings.tasks_target_url
    try:
        # `verify_oauth2_token` is untyped in google-auth, so mypy sees an untyped call in
        # a typed context. Annotating the result is the whole of the fix: the claims are a
        # plain dict and every field this function reads is checked below.
        claims: dict[str, Any] = google_id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token.strip(), google_requests.Request(), audience
        )
    except Exception as exc:
        logger.info("internal oidc token rejected", extra={"reason": type(exc).__name__})
        raise NotAuthenticated("The supplied OIDC token is not valid.") from exc

    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise NotAuthenticated("The supplied OIDC token was not issued by Google.")
    email = str(claims.get("email", ""))
    if not claims.get("email_verified") or email != expected_email:
        # The audience alone proves only that the caller knew the URL — any Google
        # account can mint a token for any audience. This line is the actual guard.
        logger.warning("internal call from an unexpected identity", extra={"email": email})
        raise NotAuthenticated("That identity is not allowed to call this endpoint.")
    return email


require_scheduler = InternalCaller("allowed_scheduler_sa")
require_tasks = InternalCaller("allowed_tasks_sa")


@router.post("/tick", include_in_schema=False)
async def tick(request: Request) -> JSONResponse:
    """Cloud Scheduler's entry point. Plans; never runs an agent.

    Answers with what it did — swept, recovered, scheduled, and a histogram of why
    candidates were skipped. That body is not decoration: it is what golden flow #8
    asserts against, and "no run was created for project A" is the half of a presence
    guard that a screen cannot show.
    """
    caller = await require_scheduler(request)
    container: Container = get_container(request)
    result = await container.scheduler.tick()
    logger.info("tick served", extra={"caller": caller, **result.to_wire()})
    return JSONResponse(status_code=200, content=result.to_wire())


@router.post("/runs/{run_id}/execute", include_in_schema=False)
async def execute_run(run_id: str, request: Request) -> JSONResponse:
    """Cloud Tasks' entry point. Runs or resumes one ledger row.

    Always `200` for a run that is already terminal, and that is what stops a redelivery
    becoming a second execution: Cloud Tasks retries whenever it does not see a response,
    including when the work succeeded and the reply was lost.
    """
    caller = await require_tasks(request)
    container: Container = get_container(request)
    run = await container.executor.execute(run_id)
    if run is None:
        # An unknown run id is not worth retrying — answering 404 tells Cloud Tasks to
        # stop rather than burning three attempts on a row that does not exist.
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"runId": run_id, "status": "unknown"},
        )
    logger.info(
        "run execution served",
        extra={"caller": caller, "run_id": run_id, "status": run.status.value},
    )
    return JSONResponse(
        status_code=200,
        content={"runId": run.id, "status": run.status.value, "cursor": run.cursor},
    )


__all__ = ["InternalCaller", "require_scheduler", "require_tasks", "router"]
