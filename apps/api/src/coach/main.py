"""FastAPI application factory.

**Route registration order is load-bearing.** The SPA catch-all is registered last, after
every router, or it shadows `/api/*`, `/ws`, `/internal/*`, `/livez`, and `/readyz`
(docs/07-infra-deploy.md#container). `tests/test_spa_catchall.py` asserts this and is the
reason a future refactor cannot quietly reorder the calls at the bottom of `create_app`.

Paths to the built SPA are relative (`static/index.html`), which is why the Dockerfile
sets `WORKDIR /app`: without it the process starts in `/`, the mount raises at import
time, and the image fails on first boot rather than silently serving nothing.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from coach.api.idempotency import IdempotencyMiddleware, ReplayedResponse
from coach.api.routers import (
    health,
    me,
    projects,
    reports,
    sessions,
    tasks,
    uploads,
    ws,
)
from coach.core.config import Settings, get_settings
from coach.core.errors import PROBLEM_CONTENT_TYPE, CoachError
from coach.core.ids import trace_id as new_trace_id
from coach.core.logging import configure_logging

logger = logging.getLogger(__name__)

#: How long the shutdown path waits for in-flight turns.
#:
#: docs/04-api-contract.md, mechanism 5: "On `SIGTERM` Cloud Run gives a termination
#: grace period. The app stops accepting new turns, waits up to the grace period for
#: in-flight turns, and marks any survivors `failed` with `retryable: true`."
#:
#: Cloud Run's default grace period is 10 s, and this is deliberately shorter: the
#: platform sends `SIGKILL` when the period expires, so a drain budget equal to the
#: period would be racing the kill for the very writes that mark survivors failed —
#: the writes that keep a turn from being left `running` forever with no `endedAt` and
#: therefore no TTL (docs/02-data-model.md#retention).
DRAIN_TIMEOUT_SECONDS = 8.0

#: Relative on purpose — see the module docstring.
STATIC_DIR = Path("static")

#: Prefixes owned by a router. Anything under one of these must never reach the SPA
#: fallback; the catch-all test enumerates exactly this list.
# `/livez` rather than `/healthz`: Google's frontend intercepts the latter on Cloud
# Run and never forwards it (coach.api.routers.health).
API_PREFIXES = ("/api", "/ws", "/internal", "/livez", "/readyz")


def _trace_id(request: Request) -> str:
    """Cloud Run's own trace id when it is there, otherwise one of ours.

    The platform sets `X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1`, and the leading
    segment is what `gcloud logging read 'trace:"…"'` matches — so echoing it turns a
    user's screenshot into a log query. Off Cloud Run there is no such header, and a
    generated id is still better than none: it appears in both the response and the log
    line, which is all the correlation this needs.
    """
    header = request.headers.get("X-Cloud-Trace-Context", "")
    platform_id = header.split("/", 1)[0].strip()
    return platform_id or new_trace_id()


def _problem_response(request: Request, error: CoachError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content=error.to_problem(instance=request.url.path),
        media_type=PROBLEM_CONTENT_TYPE,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info(
        "coach-api starting",
        extra={
            "env": settings.env,
            "project": settings.google_cloud_project,
            "database": settings.firestore_database,
            "emulator": bool(settings.firestore_emulator_host),
            # Loud on purpose. Every optional integration that silently degrades gets a
            # line here, because the alternative is what happened to YouTube at M4: the
            # feature was simply absent from every research report and the only way to
            # find out was to read the source.
            "youtube": "configured" if settings.youtube_api_key else "NOT CONFIGURED",
        },
    )
    if settings.placeholder_secrets:
        # `terraform apply` creates each secret with a placeholder first version; the real
        # values are RUNBOOK §4, a human step that is easy to skip because nothing fails
        # at boot when it is skipped. This is the line that says so.
        logger.error(
            "secrets still hold the Terraform placeholder and are being treated as unset",
            extra={
                "secrets": settings.placeholder_secrets,
                "fix": "infra/terraform/RUNBOOK.md section 4",
            },
        )
    elif not settings.youtube_api_key and not settings.is_local:
        logger.warning(
            "no YOUTUBE_API_KEY: research will recommend no videos",
            extra={"fix": "infra/terraform/RUNBOOK.md section 4"},
        )
    yield

    # Uvicorn runs lifespan shutdown on SIGTERM, so this is the drain hook. It runs
    # before the event loop closes, which is the only window in which an in-flight
    # generation task can still finish or be marked failed.
    container = app.state.container
    await container.turns.drain(DRAIN_TIMEOUT_SECONDS)
    logger.info("coach-api stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_output=not settings.is_local)

    app = FastAPI(
        title="Self-Study Coach API",
        version="0.1.0",
        lifespan=lifespan,
        # No CORS configuration anywhere: the SPA is served from this same origin
        # (docs/01-architecture.md), so there is no cross-origin surface to allow.
    )
    app.state.settings = settings

    # Deferred so that importing this module does not construct a Firestore client,
    # which would make `--help`-style imports and some tests need credentials.
    from coach.api.deps import Container

    app.state.container = Container(settings)

    app.add_middleware(IdempotencyMiddleware)

    # --- error rendering -------------------------------------------------------------

    @app.exception_handler(CoachError)
    async def _coach_error(request: Request, exc: CoachError) -> JSONResponse:
        return _problem_response(request, exc)

    @app.exception_handler(ReplayedResponse)
    async def _replayed(request: Request, exc: ReplayedResponse) -> JSONResponse:
        from coach.api.idempotency import REPLAY_HEADER

        return JSONResponse(
            status_code=exc.stored.status_code,
            content=exc.stored.body,
            headers={REPLAY_HEADER: "true"},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Render an *unplanned* failure as `problem+json`, like every planned one.

        Without this, a bug is the only thing in the system that answers with a bare
        `Internal Server Error` in `text/plain`. The client's error parser expects a
        problem document, falls back to the HTTP status text, and shows the user
        "request failed" — while a perfectly good traceback sits in the logs, unlinked to
        the request that produced it. That gap cost a full diagnosis round on a 403 from
        IAM signBlob: the UI could say nothing except that something had gone wrong.

        `traceId` is the fix for the linking half. On Cloud Run the platform's own trace
        id is in the request, so quoting it back means a support report can be turned
        into `gcloud logging read 'trace:"…"'` directly.

        `detail` carries the exception outside production, because in `local` and `dev`
        the person reading the toast is the person fixing the bug. In `prod` it is a
        fixed string: an exception message can carry a bucket name, a query, or a row of
        data, and none of that belongs in a browser. M7's error-handling pass owns the
        wider question of retryability and user-facing wording
        (docs/09-roadmap.md#m7--hardening-and-launch-readiness-15-weeks).
        """
        trace_id = _trace_id(request)
        logger.exception(
            "unhandled exception", extra={"trace_id": trace_id, "path": request.url.path}
        )
        settings: Settings = request.app.state.settings
        detail = (
            "Something went wrong on our side. Quote the trace id if you report this."
            if settings.env == "prod"
            else f"{type(exc).__name__}: {exc}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "type": "/problems/internal-error",
                "title": "Internal Server Error",
                "status": 500,
                "detail": detail,
                "instance": request.url.path,
                "traceId": trace_id,
            },
            media_type=PROBLEM_CONTENT_TYPE,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "type": "/problems/validation-error",
                "title": "Unprocessable entity",
                "status": 422,
                "detail": "The request body did not validate.",
                "instance": request.url.path,
                "errors": _serializable_errors(exc.errors()),
            },
            media_type=PROBLEM_CONTENT_TYPE,
        )

    # --- routers ---------------------------------------------------------------------
    # Everything with a real path is registered here, BEFORE the catch-all below.

    app.include_router(health.router)
    app.include_router(me.router)
    app.include_router(projects.router)
    app.include_router(tasks.router)
    app.include_router(reports.router)
    app.include_router(sessions.router)
    app.include_router(uploads.router)
    app.include_router(ws.router)

    if settings.is_local:
        # ------------------------------------------------------------------------------
        # DELIBERATE LOCAL-ONLY SURFACE. It stands in for the GCS bucket a signed PUT
        # targets, so the upload path is reachable from a Playwright flow at all — see
        # `api/routers/local_storage.py`. Guarded by this one check, exactly like the
        # `Bearer dev:<uid>` path, and pinned for every other `ENV` by
        # `tests/test_local_storage_guard.py`.
        # ------------------------------------------------------------------------------
        from coach.api.routers import local_storage

        app.include_router(local_storage.router)

    # --- SPA, registered LAST ---------------------------------------------------------
    _mount_spa(app, settings)

    return app


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    """Mount the built SPA and its fallback. Must be called after every router.

    In a deployed environment the mount is unconditional, so a missing `static/assets`
    fails the container at import time — that is the documented early-failure behaviour
    that catches a wrong `WORKDIR`. Locally the SPA is served by the Vite dev server and
    `static/` does not exist, so the mount is skipped rather than crashing `dev.sh up`.
    """
    assets = STATIC_DIR / "assets"
    if not settings.is_local or assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    index = STATIC_DIR / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def spa(full_path: str) -> FileResponse | JSONResponse:
        # Registration order stops the catch-all shadowing routes that *exist*. This
        # stops it swallowing paths under a router's prefix that do not — a typo'd
        # endpoint should be a 404, not an HTML document returned with a 200 for the
        # client to try to parse as JSON.
        if any(f"/{full_path}".startswith(prefix) for prefix in API_PREFIXES):
            return JSONResponse(
                status_code=404,
                content={
                    "type": "/problems/not-found",
                    "title": "Not found",
                    "status": 404,
                    "detail": f"No route matches /{full_path}.",
                },
                media_type=PROBLEM_CONTENT_TYPE,
            )
        if not index.is_file():
            return JSONResponse(
                status_code=404,
                content={
                    "type": "/problems/not-found",
                    "title": "Not found",
                    "status": 404,
                    "detail": (
                        "No SPA build is present. Run the Vite dev server "
                        "(./scripts/dev.sh up) or build the image."
                    ),
                },
                media_type=PROBLEM_CONTENT_TYPE,
            )
        return FileResponse(index)


def _serializable_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip the non-JSON-serializable `ctx` payloads pydantic attaches to errors."""
    return [{key: value for key, value in error.items() if key != "ctx"} for error in errors]


app = create_app()
