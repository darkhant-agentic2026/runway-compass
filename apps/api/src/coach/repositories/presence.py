"""`presence/{uid}` — "the owner is working here right now".

docs/02-data-model.md#presenceuid. Written by the WebSocket's 30-second heartbeat and
read by the autonomous tick, which skips a project whose owner is present
(docs/05-autonomous-runs.md). The read side arrives at M5; the write side is here because
the heartbeat is a WebSocket frame and the WebSocket is M2.

`connections` is maintained with an increment rather than a set count: one user can have
several tabs open, and a tab closing must not read as the user leaving.
"""

from __future__ import annotations

from typing import Any

from google.cloud.firestore_v1 import Increment

from coach.core.clock import now
from coach.repositories.firestore import PRESENCE, Database
from coach.services.models import Presence


class PresenceRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, uid: str) -> Any:
        return self._db.client.collection(PRESENCE).document(uid)

    async def get(self, uid: str) -> Presence | None:
        snapshot = await self._doc(uid).get()
        if not snapshot.exists:
            return None
        return Presence.model_validate({**(snapshot.to_dict() or {}), "uid": uid})

    async def heartbeat(self, uid: str, *, project_id: str | None, task_id: str | None) -> None:
        await self._doc(uid).set(
            {
                "activeProjectId": project_id,
                "activeTaskId": task_id,
                "lastHeartbeatAt": now(),
            },
            merge=True,
        )

    async def connected(self, uid: str) -> None:
        await self._doc(uid).set(
            {"connections": Increment(1), "lastHeartbeatAt": now()}, merge=True
        )

    async def disconnected(self, uid: str) -> None:
        """One socket closed.

        `activeProjectId` is deliberately *not* cleared: presence is judged by the
        heartbeat's age (docs/02-data-model.md — "now - lastHeartbeatAt < 120s"), so a
        stale pointer with an old heartbeat already reads as absent. Clearing it would
        add a write on every disconnect for no change in meaning.
        """
        await self._doc(uid).set({"connections": Increment(-1)}, merge=True)


__all__ = ["PresenceRepository"]
