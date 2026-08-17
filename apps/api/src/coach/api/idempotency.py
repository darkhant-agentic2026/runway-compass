"""`Idempotency-Key` support for mutating endpoints (docs/04-api-contract.md).

Split across a dependency and a middleware because neither half can do the job alone:

- The **dependency** runs after authentication, so it knows the uid and can look the key
  up. On a hit it raises `ReplayedResponse`, which an exception handler renders — the
  route body never executes, so the mutation does not happen twice.
- The **middleware** wraps the response, so it is the only place that can see the body
  worth storing. It stores only on a 2xx, keyed by what the dependency stashed on
  `request.state`.

`request.state` is backed by the ASGI scope, which the dependency and the middleware
share, which is what lets the two halves communicate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from coach.api.deps import Container, get_container
from coach.repositories.idempotency import StoredResponse

logger = logging.getLogger(__name__)

HEADER = "Idempotency-Key"

#: Marks a replayed response so a client (and a test) can tell one from a fresh write.
REPLAY_HEADER = "Idempotent-Replay"

_STATE_ATTR = "idempotency_record"


class ReplayedResponse(Exception):
    """Raised by the dependency when a stored response should be returned as-is."""

    def __init__(self, stored: StoredResponse) -> None:
        self.stored = stored
        super().__init__("replaying a stored idempotent response")


@dataclass(frozen=True)
class PendingRecord:
    uid: str
    method: str
    path: str
    key: str


async def idempotency_guard(request: Request) -> None:
    """Route dependency. Declare it on every mutating endpoint.

    Depends on the authenticated principal indirectly: the router declares
    `CurrentUser` too, and FastAPI resolves it first only by accident of ordering — so
    this reads the principal from the auth dependency itself rather than relying on that.
    """
    key = request.headers.get(HEADER)
    if not key:
        return

    from coach.api.auth import require_user

    principal = await require_user(request)
    container: Container = get_container(request)

    stored = await container.idempotency_repository.get(
        principal.uid, request.method, request.url.path, key
    )
    if stored is not None:
        raise ReplayedResponse(stored)

    setattr(
        request.state,
        _STATE_ATTR,
        PendingRecord(principal.uid, request.method, request.url.path, key),
    )


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Stores the response of a successful first-time idempotent request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        record: PendingRecord | None = getattr(request.state, _STATE_ATTR, None)
        if record is None or not 200 <= response.status_code < 300:
            return response

        # `call_next` hands back a streaming response whose body can only be read once,
        # so it is drained here and re-wrapped below. Detected by capability rather than
        # by class: which private Response subclass Starlette uses here is an internal
        # detail that has changed between versions.
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:  # pragma: no cover - defensive
            return response
        chunks: list[bytes] = []
        async for chunk in body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else bytes(chunk))
        body = b"".join(chunks)
        rebuilt = Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

        try:
            payload: dict[str, Any] = json.loads(body) if body else {}
        except json.JSONDecodeError:
            # A non-JSON response has nothing worth replaying; the mutation still
            # happened, so failing here would be worse than not storing.
            logger.warning("skipping idempotency store for a non-JSON response")
            return rebuilt

        container: Container = get_container(request)
        await container.idempotency_repository.put(
            record.uid,
            record.method,
            record.path,
            record.key,
            StoredResponse(status_code=response.status_code, body=payload),
        )
        return rebuilt
