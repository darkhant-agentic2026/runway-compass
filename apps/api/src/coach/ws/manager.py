"""One browser tab's socket, and the two ways it can follow a turn.

docs/04-api-contract.md#surviving-client-disconnects, mechanism 4, is the specification
for resume, and it names three cases. Two of them are the same code here, because the
distinction that matters is not "complete vs running" but **"is the generation task on
this instance?"**:

- **running on this instance** — replay from storage and the recent-frame buffer, then
  attach to the live broker, all under one lock, so there is no gap.
- **running on another instance** — replay, then follow `turns/{turnId}` until it
  reaches a terminal state.
- **already complete** — replay; the terminal frame is part of it.

**The cross-instance path polls rather than using a snapshot listener.** The design says
"follows the Firestore document with a snapshot listener"; that is not available here.
`on_snapshot` is implemented on the *synchronous* `DocumentReference` only — on
`AsyncDocumentReference` it inherits `BaseDocumentReference.on_snapshot`, which raises
`NotImplementedError` — and the async client is not optional, since ADK's shipped session
service is async throughout and a sync watch would need a second client plus a thread per
follower. Polling the one document at the checkpoint interval delivers exactly the
granularity the design already accepts for this path ("coarser granularity, 400 ms chunks
instead of token-level, still correct, still no wasted inference"), so the promise is met
and the mechanism differs. Recorded in docs/09-roadmap.md.

Nothing in this module can cancel generation. A socket closing tears down the pump tasks
below and nothing else — which is the guarantee, stated as an absence.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from coach.core.errors import CoachError
from coach.core.principal import Principal
from coach.repositories.presence import PresenceRepository
from coach.services.turns import TurnService
from coach.ws.broker import StreamBroker
from coach.ws.protocol import (
    TERMINAL_TYPES,
    ClientFrame,
    Ping,
    Pong,
    PresenceFrame,
    Resume,
    Subscribe,
    Unsubscribe,
)

logger = logging.getLogger(__name__)

#: How often the cross-instance follower re-reads `turns/{turnId}`. Matches the
#: checkpoint flush interval: reading faster than the writer writes only costs money.
FOLLOW_POLL_SECONDS = 0.4

#: How many turns one tab may follow at once. A ceiling on this connection's pump tasks,
#: not a rate limit — the limits in docs/04-api-contract.md#rate-limits (3 connections and
#: 100 frames per minute per user) are cross-instance counters and land with the rest of
#: the token-bucket work at M7.
MAX_SUBSCRIPTIONS = 32

_CLIENT_FRAME = TypeAdapter[ClientFrame](ClientFrame)


class SocketSession:
    """The lifetime of one `/ws` connection."""

    def __init__(
        self,
        websocket: WebSocket,
        principal: Principal,
        *,
        turns: TurnService,
        broker: StreamBroker,
        presence: PresenceRepository,
    ) -> None:
        self._socket = websocket
        self._principal = principal
        self._turns = turns
        self._broker = broker
        self._presence = presence
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._send_lock = asyncio.Lock()

    async def run(self) -> None:
        """Read client frames until the socket closes, then tear down this tab's pumps."""
        await self._presence.connected(self._principal.uid)
        try:
            while True:
                try:
                    raw = await self._socket.receive_json()
                except WebSocketDisconnect:
                    return
                await self._handle(raw)
        finally:
            for task in list(self._pumps.values()):
                task.cancel()
            if self._pumps:
                await asyncio.gather(*self._pumps.values(), return_exceptions=True)
            self._pumps.clear()
            with contextlib.suppress(Exception):
                await self._presence.disconnected(self._principal.uid)

    # --- inbound -------------------------------------------------------------------

    async def _handle(self, raw: Any) -> None:
        try:
            frame = _CLIENT_FRAME.validate_python(raw)
        except ValidationError:
            # The client is ours, so an unparseable frame is a bug rather than an
            # attack; say so on the socket instead of closing it, so one typo in a
            # presence heartbeat does not tear down an active stream.
            await self._send({"type": "error", "code": "bad-frame"})
            return

        match frame:
            case Ping():
                await self._send(Pong().to_wire())
            case PresenceFrame():
                await self._presence.heartbeat(
                    self._principal.uid,
                    project_id=frame.project_id,
                    task_id=frame.task_id,
                )
            case Subscribe():
                await self._start_pump(frame.turn_id, frame.run_id, last_seq=0)
            case Resume():
                await self._start_pump(frame.turn_id, None, last_seq=frame.last_seq)
            case Unsubscribe():
                self._stop_pump(frame.turn_id or frame.run_id)

    async def _start_pump(
        self, turn_id: str | None, run_id: str | None, *, last_seq: int
    ) -> None:
        if (turn_id is None) == (run_id is None):
            await self._send({"type": "error", "code": "subscribe-needs-one-target"})
            return
        if run_id is not None:
            # `subscribe` by runId carries `run_status` frames only. Runs arrive with the
            # ledger at M5; accepting the frame now and answering plainly beats a silent
            # no-op that would look like a dropped subscription.
            await self._send({"type": "error", "code": "runs-not-available-until-m5"})
            return
        assert turn_id is not None

        if turn_id in self._pumps and not self._pumps[turn_id].done():
            # Re-subscribing to a turn this tab already follows is what a reconnect race
            # looks like; restart the pump at the newer cursor rather than running two.
            self._stop_pump(turn_id)
        if len(self._pumps) >= MAX_SUBSCRIPTIONS:
            await self._send({"type": "error", "code": "too-many-subscriptions"})
            return

        self._pumps[turn_id] = asyncio.create_task(
            self._pump(turn_id, last_seq), name=f"ws-pump:{turn_id}"
        )

    def _stop_pump(self, key: str | None) -> None:
        if key is None:
            return
        task = self._pumps.pop(key, None)
        if task is not None:
            task.cancel()

    # --- outbound ------------------------------------------------------------------

    async def _pump(self, turn_id: str, last_seq: int) -> None:
        """Follow one turn until it ends or the client stops caring."""
        try:
            turn = await self._turns.get(self._principal, turn_id)
        except CoachError as error:
            # Ownership is checked here, at subscribe time, exactly as the contract
            # requires — a turn belonging to someone else is indistinguishable from one
            # that does not exist.
            await self._send({"type": "error", "code": error.code, "turnId": turn_id})
            return

        try:
            if self._turns.owns(turn):
                await self._pump_live(turn_id, last_seq)
            else:
                await self._pump_remote(turn_id, last_seq)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - a pump failure must not close the socket
            logger.exception("stream pump failed", extra={"turn_id": turn_id})
        finally:
            self._pumps.pop(turn_id, None)

    async def _pump_live(self, turn_id: str, last_seq: int) -> None:
        async def replay() -> list[dict[str, Any]]:
            turn = await self._turns.get(self._principal, turn_id)
            return await self._turns.replay_frames(turn, last_seq)

        async with self._broker.attach_with_replay(
            turn_id, after_seq=last_seq, replay=replay
        ) as queue:
            while True:
                frame = await queue.get()
                seq = frame.get("seq")
                if seq is not None and int(seq) <= last_seq:
                    continue
                await self._send(frame)
                if seq is not None:
                    last_seq = int(seq)
                if frame.get("type") in TERMINAL_TYPES:
                    return

    async def _pump_remote(self, turn_id: str, last_seq: int) -> None:
        """Replay, then follow the document. See the module docstring on polling."""
        while True:
            turn = await self._turns.get(self._principal, turn_id)
            for frame in await self._turns.replay_frames(turn, last_seq):
                seq = frame.get("seq")
                if seq is not None and int(seq) <= last_seq:
                    continue
                await self._send(frame)
                if seq is not None:
                    last_seq = int(seq)
            if turn.status.is_terminal:
                return
            await asyncio.sleep(FOLLOW_POLL_SECONDS)

    async def _send(self, frame: dict[str, Any]) -> None:
        """Serialize sends. Several pumps share one socket and `send_json` is not
        reentrant — two interleaved writes would produce one corrupt frame."""
        async with self._send_lock:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                await self._socket.send_json(frame)


__all__ = ["FOLLOW_POLL_SECONDS", "MAX_SUBSCRIPTIONS", "SocketSession"]
