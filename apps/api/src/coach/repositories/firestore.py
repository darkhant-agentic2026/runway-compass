"""Firestore client construction and the collection-path constants.

docs/01-architecture.md: `repositories/` is the ONLY module that knows collection paths.
Every path in the system is named here or in a sibling repository module; nothing in
`services/`, `api/`, or `agents/` may contain a collection string.

The client is async throughout (`firestore.AsyncClient`), matching ADK's shipped
Firestore services, so no Firestore call ever blocks the event loop.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from google.api_core.exceptions import Aborted
from google.cloud.firestore import AsyncClient, AsyncTransaction

from coach.core.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# --- Collection names -------------------------------------------------------------
# docs/02-data-model.md#collection-map. App-owned collections only; the ADK-owned
# layout (`adk-session/…`, `app_states/…`, `user_states/…`) belongs to the shipped
# session service and is addressed through `adk_firestore/`, not here.

USERS = "users"
PROJECTS = "projects"
TASKS = "tasks"  # subcollection of a project
RESEARCH_REPORTS = "research_reports"  # subcollection of a project
TURNS = "turns"
AUTONOMOUS_RUNS = "autonomous_runs"
PRESENCE = "presence"
USAGE = "usage"
IDEMPOTENCY = "idempotency"

#: Attempts *inside* one `AsyncTransaction`. Firestore's own default is 5 and that is
#: kept: the library retries an aborted transaction immediately, with no delay, so raising
#: this number alone just spends attempts faster. The useful retrying happens in
#: :meth:`Database.run`, which adds the backoff the inner loop lacks.
TRANSACTION_MAX_ATTEMPTS = 5

#: How many times to re-run a whole transaction after the inner loop gives up.
TRANSACTION_RETRIES = 8

#: Full-jitter backoff: the wait is drawn from `[0, min(cap, base * 2**attempt))` rather
#: than from a narrow band around a growing mean. Contenders that collided are, by
#: definition, running in lockstep; a *wide* random interval is what pulls them apart,
#: and a narrow jitter band around a shared mean leaves them nearly as synchronized as no
#: jitter at all.
TRANSACTION_BACKOFF_SECONDS = 0.05
TRANSACTION_BACKOFF_CAP_SECONDS = 2.0

#: The message `AsyncTransaction._pre_commit` wraps its last `Aborted` in. There is no
#: dedicated exception type for it, so the string is the only handle available; it is
#: matched narrowly so an unrelated ValueError still propagates.
_ATTEMPTS_EXHAUSTED = "Failed to commit transaction in"


@functools.lru_cache(maxsize=4)
def _client_for(project: str, database: str) -> AsyncClient:
    # Cached because a Firestore client owns a gRPC channel; building one per request
    # would leak connections. The client reads FIRESTORE_EMULATOR_HOST itself and
    # switches to anonymous credentials when it is set, which is the whole of the local
    # wiring.
    return AsyncClient(project=project, database=database)


def get_client(settings: Settings) -> AsyncClient:
    return _client_for(settings.google_cloud_project, settings.firestore_database)


class Database:
    """Thin handle passed to repositories.

    Exists so that `services/` can open a transaction without importing
    `google.cloud.firestore` — the service layer orchestrates transactions but must not
    know what backs them.

    The client is built on first use, not in the constructor. Constructing one resolves
    Application Default Credentials, and doing that at import time would mean the app
    could not even be *imported* without credentials — which would break `/livez`'s
    promise not to touch a dependency, and would make a missing credential look like a
    startup crash instead of a `/readyz` failure.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def client(self) -> AsyncClient:
        return get_client(self._settings)

    def transaction(self) -> AsyncTransaction:
        return self.client.transaction(max_attempts=TRANSACTION_MAX_ATTEMPTS)

    async def run(self, operation: Callable[[AsyncTransaction], Awaitable[T]]) -> T:
        """Run an `@async_transactional` operation, retrying with backoff on contention.

        Task mutations read a project's whole board and write the parent rollup and the
        project counts, so concurrent writes to one project contend on the same documents
        *by construction* — docs/02-data-model.md invariant 5 requires exactly that.

        The library's own retry loop re-runs an aborted transaction **immediately**, with
        no delay between attempts, so several coroutines contending on one project can
        collide, retry in lockstep, and collide again until the attempt budget is gone.
        That surfaces as `ValueError: Failed to commit transaction in N attempts` — a 500
        for the user, from a write that was perfectly valid.

        This adds the missing ingredient: an outer retry with exponential backoff and
        jitter, so the losers of a collision come back at different times instead of
        together. Retrying is safe because the whole operation re-reads and recomputes
        from scratch; nothing is carried over from the failed attempt.
        """
        for attempt in range(TRANSACTION_RETRIES):
            try:
                return await operation(self.transaction())
            except Aborted:
                if attempt == TRANSACTION_RETRIES - 1:
                    raise
            except ValueError as exc:
                if _ATTEMPTS_EXHAUSTED not in str(exc):
                    raise
                if attempt == TRANSACTION_RETRIES - 1:
                    raise
            ceiling = min(
                TRANSACTION_BACKOFF_CAP_SECONDS,
                TRANSACTION_BACKOFF_SECONDS * (2**attempt),
            )
            delay = random.uniform(0, ceiling)
            logger.info(
                "retrying a contended transaction",
                extra={"attempt": attempt + 1, "delay_seconds": round(delay, 3)},
            )
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        return cls(settings)
