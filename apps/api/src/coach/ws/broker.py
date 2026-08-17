"""`StreamBroker` — fan-out from one generation task to zero or more sockets.

docs/04-api-contract.md#surviving-client-disconnects, mechanism 2:

> A `StreamBroker` maps `turnId → set[asyncio.Queue]`. Zero subscribers is a normal
> state: chunks still increment `seq` and still get checkpointed.

Two properties are load-bearing:

- **Publishing never depends on there being a subscriber.** `publish` with an empty set
  is a no-op that returns immediately, so a disconnected client cannot slow generation
  down, let alone stop it.
- **Replay and attach happen under the same per-turn lock.** A resuming client reads
  checkpoints and then joins the live stream; if a chunk could be published between
  those two steps it would be in neither, and the client would have a hole in the middle
  of a message with no way to detect it. `attach_with_replay` closes that window.

The lock is per turn rather than global so that a Firestore read for one resuming client
does not stall generation for every other turn on the instance.

**The recent-frame buffer is not a cache.** Deltas are published the moment they arrive
but checkpointed up to 400 ms later, so between those two events a frame exists only in
this process. A client attaching in that window would find it in neither the checkpoints
nor the live queue — a hole in the middle of a sentence, invisible to a `seq` check
because the sequence either side of it is intact. The buffer is what covers exactly that
window, and it is why `attach_with_replay` takes `after_seq` rather than leaving the
caller to merge two sources itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

#: Per-subscriber buffer. A socket that stops draining — a wedged client, a paused
#: browser tab — is dropped rather than allowed to apply backpressure to generation.
#: Deep enough that an ordinary slow consumer is absorbed; the resume path recovers
#: anything a dropped subscriber missed, so the failure mode is a reconnect, not a loss.
QUEUE_MAXSIZE = 512

#: How many recently published frames to keep per turn. Only has to span the checkpoint
#: flush interval — anything older is durable in Firestore — so this is generous by two
#: orders of magnitude rather than tuned.
RECENT_FRAMES = 1024


class StreamBroker:
    """In-process fan-out, keyed by turn id."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._recent: dict[str, deque[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def _lock_for(self, turn_id: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(turn_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[turn_id] = lock
            return lock

    def subscriber_count(self, turn_id: str) -> int:
        return len(self._subscribers.get(turn_id, ()))

    async def publish(self, turn_id: str, frame: dict[str, Any]) -> None:
        """Deliver `frame` to every current subscriber of `turn_id`.

        Zero subscribers is the normal, uninteresting case and costs one dict lookup.
        """
        lock = await self._lock_for(turn_id)
        async with lock:
            self._recent.setdefault(turn_id, deque(maxlen=RECENT_FRAMES)).append(frame)
            for queue in list(self._subscribers.get(turn_id, ())):
                try:
                    queue.put_nowait(frame)
                except asyncio.QueueFull:
                    # Dropping the subscriber is the right trade: generation must not
                    # block on a consumer, and the client can resume from checkpoints.
                    logger.warning(
                        "dropping a wedged stream subscriber", extra={"turn_id": turn_id}
                    )
                    self._subscribers.get(turn_id, set()).discard(queue)

    @asynccontextmanager
    async def subscribe(self, turn_id: str) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Attach a queue for the duration of the block, with no replay."""
        async with self.attach_with_replay(turn_id, after_seq=0, replay=None) as queue:
            yield queue

    @asynccontextmanager
    async def attach_with_replay(
        self,
        turn_id: str,
        *,
        after_seq: int,
        replay: Callable[[], Awaitable[list[dict[str, Any]]]] | None,
    ) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        """Attach a queue seeded with everything after `after_seq`, atomically.

        Three sources are stitched together here, in order, and the ordering is the whole
        trick:

        1. `replay()` — the durable history, read from `turns/{turnId}`.
        2. this turn's recent-frame buffer, for anything published but not yet
           checkpointed.
        3. the live queue, from the moment of registration.

        All three are joined while this turn's publishes are held, so the boundaries are
        exact rather than probabilistic — that is the "no gap, because replay and attach
        happen under the broker lock" clause of the resume path. The Firestore read is
        awaited inside the lock deliberately: it is one document, and blocking one turn's
        stream for its duration is far cheaper than reasoning about an overlap window.

        Frames are handed over strictly increasing in `seq`, so a client that drops
        `seq <= lastSeq` sees each one exactly once no matter which source it came from.
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        lock = await self._lock_for(turn_id)
        async with lock:
            highest = after_seq
            for frame in await replay() if replay is not None else []:
                queue.put_nowait(frame)
                highest = max(highest, int(frame.get("seq", highest)))
            for frame in self._recent.get(turn_id, ()):
                seq = frame.get("seq")
                if seq is None or int(seq) <= highest:
                    continue
                queue.put_nowait(frame)
                highest = int(seq)
            self._subscribers.setdefault(turn_id, set()).add(queue)
        try:
            yield queue
        finally:
            async with lock:
                subscribers = self._subscribers.get(turn_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        del self._subscribers[turn_id]

    async def forget(self, turn_id: str) -> None:
        """Release a finished turn's bookkeeping.

        Called once a turn reaches a terminal state. Subscribers are left alone — they
        drop themselves when their `subscribe` block exits — and the recent-frame buffer
        goes, because everything in it is now durable and a client resuming after this
        point is served entirely from `turns/{turnId}`.
        """
        async with self._guard:
            self._locks.pop(turn_id, None)
            self._recent.pop(turn_id, None)
