"""`TurnRegistry` — where a generation task lives while it runs.

docs/04-api-contract.md#surviving-client-disconnects, mechanism 1:

> The generation coroutine is created with `asyncio.create_task` and held in an
> app-level `TurnRegistry`, **not in the WebSocket handler's scope**. Closing a socket
> drops a *subscriber*; nothing cancels the task. The only cancellation paths are the
> explicit cancel endpoint and process shutdown.

That "not in the WebSocket handler's scope" is the entire point of this class, and it is
the thing that is easy to undo by accident: awaiting a generation coroutine from a
request handler, or holding its task in a local, reintroduces exactly the coupling the
design removes. The registry is owned by `app.state` and outlives every request.

Keeping a strong reference is also load-bearing for a duller reason — asyncio only holds
a weak reference to a running task, so a task nobody keeps can be garbage-collected
mid-flight.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class TurnRegistry:
    """The instance's in-flight generation tasks, keyed by turn id."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        #: Set on SIGTERM. New turns are refused from that moment, so the drain below
        #: has a finite worklist rather than a moving target.
        self._draining = False

    @property
    def draining(self) -> bool:
        return self._draining

    def __len__(self) -> int:
        return len(self._tasks)

    def is_running(self, turn_id: str) -> bool:
        task = self._tasks.get(turn_id)
        return task is not None and not task.done()

    def spawn(self, turn_id: str, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Detach `coroutine` and keep it alive until it finishes on its own."""
        task = asyncio.create_task(coroutine, name=f"turn:{turn_id}")
        self._tasks[turn_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(turn_id, None))
        return task

    def cancel(self, turn_id: str) -> bool:
        """Stop a turn. Returns whether there was anything to stop *here*.

        A `False` is not an error: the cancel request may have landed on an instance that
        does not own the turn, in which case the owner learns about it through the
        `cancelRequested` flag on the turn document instead.
        """
        task = self._tasks.get(turn_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def drain(self, timeout: float) -> list[str]:
        """Stop accepting new turns, then wait out the in-flight ones.

        docs/04-api-contract.md, mechanism 5: on `SIGTERM` Cloud Run gives a termination
        grace period; the app stops accepting new turns, waits up to that period for
        in-flight turns, and marks any survivors `failed` with `retryable: true`.

        Returns the turn ids that did **not** finish in time. The caller marks those
        failed in Firestore — deliberately not done here, so this class stays free of
        storage and can be unit-tested on its own.
        """
        self._draining = True
        pending = dict(self._tasks)
        if not pending:
            return []

        logger.info(
            "draining in-flight turns", extra={"count": len(pending), "timeout": timeout}
        )
        _finished, not_done = await asyncio.wait(set(pending.values()), timeout=timeout)
        survivors = [turn_id for turn_id, task in pending.items() if task in not_done]
        for task in not_done:
            task.cancel()
        if survivors:
            logger.warning("turns outlived the grace period", extra={"turn_ids": survivors})
        return survivors
