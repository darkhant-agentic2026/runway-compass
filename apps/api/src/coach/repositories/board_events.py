"""`board_events/{uid}` — `board_update` across Cloud Run instances.

Until M5 the `BoardUpdateHub` reached the sockets attached to *this* process, and for M3
and M4 that was the whole population that mattered: a board mutation came from a tool call
inside a turn the user's own request had started, and session affinity put their socket on
the same instance. A scheduled run breaks that. It executes wherever Cloud Tasks lands it,
with no relationship to where the owner is connected — so a run can add three tasks and
the tab watching the board hears nothing at all.

docs/09-roadmap.md#status-after-m3 names the fix and the reason for it: "the ledger needs
a cross-instance channel — Firestore, since one already exists". This is that channel.

## Shape, and why it is a document rather than a subcollection

```jsonc
{ "rev": 42,
  "frames": [ { "rev": 41, "instanceId": "…", "frame": { … } }, … ] }   // last 20
```

One document per user, read by a poller and written by whoever changed the board. A
subcollection would be a query per poll instead of a point read, and this is read on a
timer by every connected instance — the cheapest read available is the right one.

**Polling, not a snapshot listener**, for exactly the reason `ws/manager.py` gives for the
cross-instance resume path: `on_snapshot` exists only on the *synchronous*
`DocumentReference`; the async one inherits a `NotImplementedError`, and the async client
is not optional here. The interval is seconds rather than milliseconds because a board
update is an invalidation hint — a refetch a moment late is invisible, where a late token
is not.

**A frame carries the instance that wrote it** so the writer's own poller can skip it. The
writer has already delivered it locally; without the tag every board mutation would reach
the originating tab twice, and "harmless duplicate invalidation" is the kind of thing that
stops being harmless when somebody hangs a toast off it.
"""

from __future__ import annotations

import logging
from typing import Any

from google.cloud.firestore import AsyncTransaction, async_transactional

from coach.core.clock import now
from coach.repositories.firestore import Database

logger = logging.getLogger(__name__)

BOARD_EVENTS = "board_events"

#: How many frames one user's document keeps. A poller that has been away longer than this
#: many board changes loses the intermediate ones — which costs nothing, because every
#: frame is the same instruction ("refetch") and the last one is enough.
MAX_FRAMES = 20


class BoardEventRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, uid: str) -> Any:
        return self._db.client.collection(BOARD_EVENTS).document(uid)

    async def publish(self, uid: str, frame: dict[str, Any], *, instance_id: str) -> int:
        """Append one frame and return its revision.

        Transactional because `rev` is a counter and two instances finishing runs for the
        same user in the same second would otherwise both write the same number — and a
        poller that has seen `rev` skips everything at or below it, so a lost increment is
        a board update nobody ever gets.
        """
        reference = self._doc(uid)
        assigned = {"rev": 0}

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> None:
            snapshot = await reference.get(transaction=transaction)
            current = snapshot.to_dict() or {} if snapshot.exists else {}
            rev = int(current.get("rev", 0)) + 1
            assigned["rev"] = rev
            frames = list(current.get("frames", []))
            frames.append({"rev": rev, "instanceId": instance_id, "frame": frame})
            transaction.set(
                reference,
                {"rev": rev, "frames": frames[-MAX_FRAMES:], "updatedAt": now()},
            )

        await self._db.run(txn)
        return assigned["rev"]

    async def read_since(self, uid: str, after_rev: int) -> tuple[int, list[dict[str, Any]]]:
        """`(latest_rev, entries newer than after_rev)`. One point read.

        Returns the entries rather than the frames so the caller can filter on
        `instanceId`; unwrapping here would throw away the only thing that makes the
        writer's own delivery distinguishable from everyone else's.
        """
        snapshot = await self._doc(uid).get()
        if not snapshot.exists:
            return after_rev, []
        document = snapshot.to_dict() or {}
        latest = int(document.get("rev", 0))
        if latest <= after_rev:
            return latest, []
        entries = [
            entry
            for entry in document.get("frames", [])
            if int(entry.get("rev", 0)) > after_rev
        ]
        return latest, entries


__all__ = ["BOARD_EVENTS", "MAX_FRAMES", "BoardEventRepository"]
