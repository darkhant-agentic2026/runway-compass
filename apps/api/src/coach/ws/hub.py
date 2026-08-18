"""`BoardUpdateHub` — fan-out of `board_update` to one user's open tabs.

docs/04-api-contract.md gives `board_update` as the invalidation push that keeps the
board live while the agent works, and docs/06-frontend.md#the-bridge says what the client
does with it: turn it into a TanStack Query invalidation, never a cache patch.

**This is addressed by user, not by turn**, and that is the whole reason it is not the
`StreamBroker`. A board mutation is interesting to every tab the user has open — the one
watching the conversation *and* the one left on the board in another window — while the
broker's keyspace is a turn id that only the chat pane knows. Routing board updates
through the broker would deliver them to exactly the tab that is already about to refetch
and to none of the others.

**In-process, and that is a real limit.** The hub reaches the sockets attached to *this*
instance. For M3 that is the whole population that matters: a board mutation comes from a
tool call inside an interactive turn, the turn runs on the instance that served the
request, and Cloud Run session affinity puts that user's socket there too. From M5 an
autonomous run executes wherever Cloud Tasks lands it, with no relationship to where the
owner's socket is — so the ledger's `board_update` needs a cross-instance channel
(Firestore, since one already exists). Recorded in docs/09-roadmap.md rather than
pre-built here, because building it now would mean a second delivery path with no caller.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from coach.ws.protocol import BoardUpdate

logger = logging.getLogger(__name__)

#: What a subscriber gives the hub: a coroutine that puts one frame on one socket.
Sink = Callable[[dict[str, Any]], Awaitable[None]]


class BoardUpdateHub:
    """Every open socket, indexed by the uid that authenticated it."""

    def __init__(self) -> None:
        self._sinks: dict[str, set[Sink]] = {}

    def attach(self, uid: str, sink: Sink) -> None:
        self._sinks.setdefault(uid, set()).add(sink)

    def detach(self, uid: str, sink: Sink) -> None:
        sinks = self._sinks.get(uid)
        if sinks is None:
            return
        sinks.discard(sink)
        if not sinks:
            del self._sinks[uid]

    def connection_count(self, uid: str) -> int:
        return len(self._sinks.get(uid, ()))

    async def publish(
        self,
        uid: str,
        *,
        project_id: str,
        task_ids: list[str],
        origin: str = "agent",
        run_id: str | None = None,
    ) -> None:
        """Tell `uid`'s tabs that these tasks moved.

        Failures are swallowed per sink: a board update is a hint to refetch, so a socket
        that has died between the lookup and the send costs nothing — the client rebuilds
        its board on reconnect anyway. Letting it propagate would abort a tool call that
        has already committed its write, which would be the worst of both.
        """
        frame = BoardUpdate(
            project_id=project_id, task_ids=task_ids, origin=origin, run_id=run_id
        ).to_wire()
        sinks = list(self._sinks.get(uid, ()))
        if not sinks:
            return
        results = await asyncio.gather(*(sink(frame) for sink in sinks), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("board_update delivery failed", exc_info=result)


@asynccontextmanager
async def attached(hub: BoardUpdateHub, uid: str, sink: Sink) -> AsyncIterator[None]:
    """Hold a sink on the hub for the lifetime of a socket."""
    hub.attach(uid, sink)
    try:
        yield
    finally:
        hub.detach(uid, sink)


__all__ = ["BoardUpdateHub", "Sink", "attached"]
