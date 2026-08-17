"""`CheckpointWriter` — the rolling buffer behind `turns/{turnId}.checkpoints`.

docs/04-api-contract.md#surviving-client-disconnects, mechanism 3:

> Deltas accumulate in a buffer flushed to `turns/{turnId}.checkpoints` every 400 ms or
> 512 characters, whichever first.

Both halves of "whichever first" matter, and for different failure modes:

- **512 characters** bounds how much a fast stream can accumulate between writes.
- **400 ms** bounds how *stale* a slow one can get. Without a timer, a model that pauses
  for five seconds mid-answer would leave the last few tokens unwritten for that whole
  pause, and a client resuming inside it would be told the turn is further behind than
  it is. So the interval is enforced by a background task rather than only being checked
  when the next delta happens to arrive.

Batching is also the main lever on Firestore write cost (docs/07-infra-deploy.md#cost-notes):
one write per 400 ms rather than one per token.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from coach.repositories.turns import TurnRepository
from coach.services.models import CheckpointSlice

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 0.4
FLUSH_CHARACTERS = 512


class CheckpointWriter:
    """Accumulates deltas for one turn and writes them out in slices.

    Not reusable across turns: it holds one turn's buffer and one timer, and a writer per
    turn is what makes `close()` unambiguous.
    """

    def __init__(
        self,
        turns: TurnRepository,
        turn_id: str,
        *,
        interval: float = FLUSH_INTERVAL_SECONDS,
        characters: int = FLUSH_CHARACTERS,
    ) -> None:
        self._turns = turns
        self._turn_id = turn_id
        self._interval = interval
        self._characters = characters

        self._buffer: list[str] = []
        self._from_seq: int | None = None
        self._to_seq: int | None = None
        self._lock = asyncio.Lock()
        self._ticker: asyncio.Task[None] | None = None

    async def __aenter__(self) -> CheckpointWriter:
        self._ticker = asyncio.create_task(self._tick(), name=f"checkpoints:{self._turn_id}")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def add(self, seq: int, text: str) -> None:
        """Buffer one published delta, flushing if the size threshold is reached.

        A slice describes a *contiguous* run of delta seqs — `lengths[i]` is the length
        of the delta at `fromSeq + i`, which is what lets the resume path cut a slice at
        an arbitrary `lastSeq`. Non-delta frames (a tool call, an artifact) consume seqs
        too, so when one has intervened the run is closed here rather than silently
        producing a slice whose `lengths` no longer line up with its seq range.
        """
        async with self._lock:
            if self._to_seq is not None and seq != self._to_seq + 1:
                await self._flush_locked()
            if self._from_seq is None:
                self._from_seq = seq
            self._to_seq = seq
            self._buffer.append(text)
            if sum(len(chunk) for chunk in self._buffer) >= self._characters:
                await self._flush_locked()

    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    async def close(self) -> None:
        """Stop the timer and write whatever is left.

        Called on every exit path — completion, error, and cancellation — because the
        unflushed tail is precisely what a client resuming after a crash would otherwise
        never see.
        """
        ticker, self._ticker = self._ticker, None
        if ticker is not None:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
        await self.flush()

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.flush()
            except Exception:  # pragma: no cover - a flush failure must not kill the turn
                logger.exception("checkpoint flush failed", extra={"turn_id": self._turn_id})

    async def _flush_locked(self) -> None:
        if not self._buffer or self._from_seq is None or self._to_seq is None:
            return
        slice_ = CheckpointSlice(
            from_seq=self._from_seq,
            to_seq=self._to_seq,
            text="".join(self._buffer),
            lengths=[len(chunk) for chunk in self._buffer],
        )
        self._buffer = []
        self._from_seq = None
        self._to_seq = None
        await self._turns.append_checkpoint(self._turn_id, slice_)
        await self._turns.spill_if_needed(self._turn_id)
