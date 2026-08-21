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

**In-process was a real limit until M5, and this is where it stopped being one.** The hub
reaches the sockets attached to *this* instance. For M3 and M4 that was the whole
population that mattered: a board mutation came from a tool call inside an interactive
turn, the turn ran on the instance that served the request, and Cloud Run session affinity
put that user's socket there too. A scheduled run executes wherever Cloud Tasks lands it,
with no relationship to where the owner is connected — so the hub now also writes each
frame to `board_events/{uid}` and every instance holding a socket for that user polls it
(`repositories/board_events.py`).

The relay is **optional** and the hub works without one, which is what keeps every existing
unit test — and every single-process run — free of a Firestore dependency it does not need.
A missing relay is a hub that behaves exactly as it did at M4.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from coach.ws.protocol import BoardUpdate

logger = logging.getLogger(__name__)

#: What a subscriber gives the hub: a coroutine that puts one frame on one socket.
Sink = Callable[[dict[str, Any]], Awaitable[None]]

#: How often an instance re-reads `board_events/{uid}` while it holds a socket for that
#: user. Seconds rather than the 400 ms the turn follower uses: a board update is an
#: invalidation hint, and a refetch a moment late is invisible where a late token is not.
#: One point read per connected user per interval is the whole cost.
RELAY_POLL_SECONDS = 3.0


class BoardChannel(Protocol):
    """The cross-instance transport. `BoardEventRepository` is the implementation."""

    async def publish(self, uid: str, frame: dict[str, Any], *, instance_id: str) -> int: ...

    async def read_since(
        self, uid: str, after_rev: int
    ) -> tuple[int, list[dict[str, Any]]]: ...


class BoardUpdateHub:
    """Every open socket, indexed by the uid that authenticated it."""

    def __init__(
        self, channel: BoardChannel | None = None, *, instance_id: str = "local"
    ) -> None:
        self._sinks: dict[str, set[Sink]] = {}
        self._channel = channel
        self._instance_id = instance_id
        self._pollers: dict[str, asyncio.Task[None]] = {}

    def attach(self, uid: str, sink: Sink) -> None:
        first = uid not in self._sinks
        self._sinks.setdefault(uid, set()).add(sink)
        if first and self._channel is not None:
            # One poller per *user*, not per socket: three tabs on this instance share the
            # read, which is the difference between a cost proportional to users and one
            # proportional to tabs.
            self._pollers[uid] = asyncio.create_task(
                self._relay(uid), name=f"board-relay:{uid}"
            )

    def detach(self, uid: str, sink: Sink) -> None:
        sinks = self._sinks.get(uid)
        if sinks is None:
            return
        sinks.discard(sink)
        if sinks:
            return
        del self._sinks[uid]
        poller = self._pollers.pop(uid, None)
        if poller is not None:
            poller.cancel()

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
        """Tell `uid`'s tabs that these tasks moved — here and on every other instance.

        Failures are swallowed per sink: a board update is a hint to refetch, so a socket
        that has died between the lookup and the send costs nothing — the client rebuilds
        its board on reconnect anyway. Letting it propagate would abort a tool call that
        has already committed its write, which would be the worst of both. The relay write
        is swallowed on the same argument, and it is attempted **even when this instance
        has no sockets for the user**: that case — a run executing where nobody is
        connected — is the entire reason the relay exists.
        """
        frame = BoardUpdate(
            project_id=project_id, task_ids=task_ids, origin=origin, run_id=run_id
        ).to_wire()
        await self._fan_out(uid, frame)
        if self._channel is not None:
            try:
                await self._channel.publish(uid, frame, instance_id=self._instance_id)
            except Exception:
                logger.warning("board_update relay write failed", exc_info=True)

    async def _fan_out(self, uid: str, frame: dict[str, Any]) -> None:
        sinks = list(self._sinks.get(uid, ()))
        if not sinks:
            return
        results = await asyncio.gather(*(sink(frame) for sink in sinks), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("board_update delivery failed", exc_info=result)

    async def _relay(self, uid: str) -> None:
        """Poll `board_events/{uid}` and fan out what other instances wrote.

        The first read **establishes the cursor without delivering**: a tab that has just
        connected has already fetched the board, so replaying the last twenty updates would
        be twenty refetches of data it is currently rendering.
        """
        if self._channel is None:  # pragma: no cover - guarded by the caller
            return
        cursor = -1
        while True:
            try:
                latest, entries = await self._channel.read_since(uid, max(cursor, 0))
                if cursor < 0:
                    cursor = latest
                else:
                    cursor = max(cursor, latest)
                    for entry in entries:
                        if entry.get("instanceId") == self._instance_id:
                            # Ours. Already delivered locally by `publish`.
                            continue
                        await self._fan_out(uid, dict(entry.get("frame") or {}))
            except asyncio.CancelledError:
                raise
            except Exception:
                # A relay that dies takes cross-instance updates down silently for as long
                # as the tab stays open, so it logs and keeps polling rather than exiting.
                logger.warning("board_update relay read failed", exc_info=True)
            await asyncio.sleep(RELAY_POLL_SECONDS)

    async def aclose(self) -> None:
        """Stop every poller. For tests and for the shutdown path."""
        pollers = list(self._pollers.values())
        self._pollers.clear()
        for poller in pollers:
            poller.cancel()
        for poller in pollers:
            with contextlib.suppress(asyncio.CancelledError):
                await poller


@asynccontextmanager
async def attached(hub: BoardUpdateHub, uid: str, sink: Sink) -> AsyncIterator[None]:
    """Hold a sink on the hub for the lifetime of a socket."""
    hub.attach(uid, sink)
    try:
        yield
    finally:
        hub.detach(uid, sink)


__all__ = ["RELAY_POLL_SECONDS", "BoardChannel", "BoardUpdateHub", "Sink", "attached"]
