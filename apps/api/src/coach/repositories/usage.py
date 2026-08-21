"""`usage/{uid}_{day}` — the per-user daily counters the quota guards read.

docs/02-data-model.md#collection-map reserves this collection and
docs/05-autonomous-runs.md#candidate-selection-and-guards names the one field M5 uses:
`autonomousRuns`, checked against `plan.limits.autonomousRunsPerDay`. The rest of the
counters (tokens, turns) belong to M7's quota pass and are deliberately not invented here.

**The document id encodes the day, so there is no expiry job and no query.** A counter
keyed `{uid}_{2026-08-20}` is one point read on the guard path and one `Increment` on the
spend path; a single rolling document would need a "which day is this" compare-and-reset,
which is a transaction on the hottest write in the system to save one document per user
per day.

The day is computed in the *user's* timezone, not UTC. A quota that resets at midnight
somewhere else is a quota the learner cannot reason about, and the same preference already
decides quiet hours.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.cloud.firestore_v1 import Increment

from coach.core.clock import now
from coach.repositories.firestore import USAGE, Database


def local_day(at: datetime, timezone: str) -> date:
    """The calendar day `at` falls on for a user in `timezone`.

    An unknown or malformed timezone falls back to UTC rather than raising: the value
    comes from a preference the user can type, and a bad string must not take out the
    scheduler for everyone in the same tick.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    return at.astimezone(zone).date()


class UsageRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, uid: str, day: date) -> Any:
        return self._db.client.collection(USAGE).document(f"{uid}_{day.isoformat()}")

    async def autonomous_runs(self, uid: str, day: date) -> int:
        snapshot = await self._doc(uid, day).get()
        if not snapshot.exists:
            return 0
        return int((snapshot.to_dict() or {}).get("autonomousRuns", 0))

    async def record_autonomous_run(self, uid: str, day: date) -> None:
        """Count one run against the day's quota.

        Incremented when a run is *created*, not when it succeeds. A run that fails has
        still spent the model calls the quota exists to bound, and a counter that only
        counted successes would let a broken project retry all day.
        """
        await self._doc(uid, day).set(
            {"autonomousRuns": Increment(1), "updatedAt": now()}, merge=True
        )


__all__ = ["UsageRepository", "local_day"]
