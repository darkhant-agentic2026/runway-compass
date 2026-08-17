"""`turns/{turnId}` — the streaming checkpoint ledger.

docs/02-data-model.md#turnsturnid. This is the document that makes the disconnect
guarantee real: it is written by the generation task and read by any instance a
reconnecting client happens to land on, so it is the only channel between the two.

Every write here is a blind `update`/`set` rather than a transaction. Exactly one
coroutine — the detached generation task — writes a given turn, and it writes only
append-shaped changes, so there is nothing to contend on. The one exception is
`request_cancel`, which is a single field an unrelated request handler sets.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from google.cloud.firestore_v1 import ArrayUnion

from coach.core.clock import now
from coach.repositories.firestore import TURNS, Database
from coach.services.models import CheckpointSlice, Turn, TurnError, TurnStatus

#: How long a turn document keeps its instance's claim. Refreshed on every checkpoint
#: flush, so a turn whose owning instance vanished stops being renewed and the M5 ledger
#: sweep can pick it up. Comfortably longer than the flush interval so an ordinary pause
#: in generation does not look like a death.
LEASE = timedelta(minutes=5)

#: Firestore documents cap at 1 MiB. docs/02-data-model.md caps `checkpoints` at ~400 KB
#: and spills older slices to `turns/{turnId}/checkpoint_pages/{page}` beyond that.
CHECKPOINT_BUDGET_BYTES = 400_000
CHECKPOINT_PAGES = "checkpoint_pages"


def _to_turn(document: Any) -> Turn:
    return Turn.model_validate({**(document.to_dict() or {}), "id": document.id})


class TurnRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, turn_id: str) -> Any:
        return self._db.client.collection(TURNS).document(turn_id)

    # --- reads ---------------------------------------------------------------------

    async def get(self, turn_id: str) -> Turn | None:
        snapshot = await self._doc(turn_id).get()
        if not snapshot.exists:
            return None
        return _to_turn(snapshot)

    async def get_with_pages(self, turn_id: str) -> Turn | None:
        """The turn with any spilled checkpoint pages folded back in, oldest first.

        Resume needs the *whole* history, so it reads through the spill; the live path
        never does, which is why this is a separate method rather than the default.
        """
        turn = await self.get(turn_id)
        if turn is None:
            return None
        pages = self._doc(turn_id).collection(CHECKPOINT_PAGES).order_by("page")
        spilled: list[CheckpointSlice] = []
        async for document in pages.stream():
            data = document.to_dict() or {}
            spilled.extend(
                CheckpointSlice.model_validate(entry) for entry in data.get("checkpoints", [])
            )
        if not spilled:
            return turn
        return turn.model_copy(update={"checkpoints": [*spilled, *turn.checkpoints]})

    async def is_cancellation_requested(self, turn_id: str) -> bool:
        """Whether some request handler has asked this turn to stop.

        Read by the generation loop between chunks. The cancel endpoint may be served by
        an instance that does not own the turn, so a flag in the document is the only
        instruction channel that reaches the owner.
        """
        snapshot = await self._doc(turn_id).get()
        return (
            bool((snapshot.to_dict() or {}).get("cancelRequested"))
            if snapshot.exists
            else False
        )

    # --- writes --------------------------------------------------------------------

    async def create(self, turn: Turn) -> Turn:
        timestamp = now()
        turn = turn.model_copy(
            update={"started_at": timestamp, "lease_expires_at": timestamp + LEASE}
        )
        document = turn.to_document()
        document.pop("id", None)
        await self._doc(turn.id).set(document)
        return turn

    async def append_checkpoint(self, turn_id: str, slice_: CheckpointSlice) -> None:
        """Append one flushed slice and advance `lastSeq`.

        `ArrayUnion` rather than a read-modify-write: the append is the whole operation,
        so there is no reason to read the document back on every flush of every turn.
        """
        await self._doc(turn_id).update(
            {
                "checkpoints": ArrayUnion([slice_.to_document()]),
                "lastSeq": slice_.to_seq,
                "leaseExpiresAt": now() + LEASE,
            }
        )

    async def advance_seq(self, turn_id: str, last_seq: int) -> None:
        """Record a seq consumed by a non-delta frame (a tool call, an artifact).

        Those frames are not checkpointed — a resumed client rebuilds tool chips from the
        finalized transcript rather than from the stream — but `lastSeq` still has to move
        or the next flush would look like it skipped.
        """
        await self._doc(turn_id).update({"lastSeq": last_seq, "leaseExpiresAt": now() + LEASE})

    async def spill_if_needed(self, turn_id: str) -> bool:
        """Move accumulated slices into a page when the document nears its budget.

        Returns whether a spill happened. Checked on flush rather than on a schedule
        because the only thing that grows the document is a flush.
        """
        turn = await self.get(turn_id)
        if turn is None:
            return False
        size = sum(len(slice_.text) for slice_ in turn.checkpoints)
        if size < CHECKPOINT_BUDGET_BYTES or not turn.checkpoints:
            return False

        pages = self._doc(turn_id).collection(CHECKPOINT_PAGES)
        existing = [document async for document in pages.stream()]
        await pages.document(f"{len(existing):06d}").set(
            {
                "page": len(existing),
                "checkpoints": [slice_.to_document() for slice_ in turn.checkpoints],
                "createdAt": now(),
            }
        )
        await self._doc(turn_id).update({"checkpoints": []})
        return True

    async def request_cancel(self, turn_id: str) -> None:
        """Ask the owning instance to stop. See `is_cancellation_requested`."""
        await self._doc(turn_id).update({"cancelRequested": True, "updatedAt": now()})

    async def finish(
        self,
        turn_id: str,
        status: TurnStatus,
        *,
        last_seq: int | None = None,
        error: TurnError | None = None,
    ) -> None:
        """Move a turn to a terminal state.

        `endedAt` is the Firestore TTL field, so setting it is what eventually removes
        the document — a turn left `running` forever would also live forever
        (docs/02-data-model.md#retention).
        """
        updates: dict[str, Any] = {
            "status": status.value,
            "endedAt": now(),
            "cancelRequested": False,
        }
        if last_seq is not None:
            updates["lastSeq"] = last_seq
        if error is not None:
            updates["error"] = error.to_document()
        await self._doc(turn_id).update(updates)

    # No query here reads more than one indexed field, deliberately.
    #
    # Two multi-filter queries used to live at the bottom of this class — "turns this
    # instance still owns" (`instanceId` + `status`) and "turns whose lease expired while
    # running" (`status` + `leaseExpiresAt`). Both were written ahead of a caller, and
    # both needed composite indexes that are in neither `modules/firestore/main.tf` nor
    # docs/02-data-model.md#indexes, so both would have returned `FAILED_PRECONDITION` the
    # first time anything called them in a deployed environment — and passed locally,
    # because the emulator does not enforce index requirements.
    #
    # They are gone rather than indexed: nothing called them. `SIGTERM` drain works off
    # the in-process `TurnRegistry` (`services/turns.py`), and the lease sweep belongs to
    # the M5 ledger, which should add each query together with its index and its row in
    # the design's index table.
