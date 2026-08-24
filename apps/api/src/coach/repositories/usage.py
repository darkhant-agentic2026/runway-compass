"""`usage/{uid}_{period}` — the per-user counters the quota guards read.

docs/02-data-model.md#usage-quotas-m8-quotas is the specification. Two shapes share one
collection, distinguished by the document id's format:

- `{uid}_{yyyy-mm-dd}` — the daily bucket. Holds `autonomousRuns` only (M5's run-count
  pacing cap on background work, unrelated to points); points are not bucketed daily.
- `{uid}_{yyyy-mm}` — the monthly points bucket.
- `{uid}_{yyyy-mm-dd}-b{0..5}` — the 4-hour points bucket, one of six fixed blocks a day.

**Every document id encodes its own period, so there is no expiry job and no query.** A
counter keyed this way is one point read on the guard path and one `Increment` on the spend
path; a rolling window would need a query over a trailing range, which is either a composite
index or a scan.

The day (and the 4-hour block within it) is computed in the *user's* timezone, not UTC. A
quota that resets at midnight somewhere else is a quota the learner cannot reason about, and
the same preference already decides quiet hours.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.cloud.firestore_v1 import Increment

from coach.core.clock import now
from coach.repositories.firestore import USAGE, Database
from coach.services.models import UsagePoints

#: docs/02-data-model.md#usage-quotas-m8-quotas: 1 point = 1,000 tokens, rounded up.
POINTS_TOKEN_DIVISOR = 1000

#: Six fixed, timezone-local blocks a day rather than a true rolling window — the same
#: tradeoff the daily run-count bucket already makes, for the same reason (see the module
#: docstring).
FOUR_HOUR_BLOCK_HOURS = 4


def _zone(timezone: str) -> ZoneInfo:
    """An unknown or malformed timezone falls back to UTC rather than raising: the value
    comes from a preference the user can type, and a bad string must not take out the
    scheduler, or a turn, for everyone in the same tick."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_day(at: datetime, timezone: str) -> date:
    """The calendar day `at` falls on for a user in `timezone`.

    Still needed for `autonomousRunsPerDay`'s run-count bucket (M5) even though points are
    no longer bucketed daily.
    """
    return at.astimezone(_zone(timezone)).date()


def local_four_hour_block(at: datetime, timezone: str) -> tuple[date, int]:
    """`(day, block)` for the 4-hour window `at` falls in, in the user's timezone.

    `block` is `0..5`: `00:00-04:00` local is block `0`, `20:00-24:00` is block `5`.
    """
    local = at.astimezone(_zone(timezone))
    return local.date(), local.hour // FOUR_HOUR_BLOCK_HOURS


def _month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _four_hour_key(day: date, block: int) -> str:
    return f"{day.isoformat()}-b{block}"


def next_monthly_reset(at: datetime, timezone: str) -> datetime:
    """Local midnight on the 1st of next month, as an aware UTC datetime."""
    zone = _zone(timezone)
    local = at.astimezone(zone)
    year, month = local.year, local.month + 1
    if month > 12:
        year, month = year + 1, 1
    return datetime(year, month, 1, tzinfo=zone).astimezone(UTC)


def next_four_hour_reset(at: datetime, timezone: str) -> datetime:
    """The start of the next 4-hour block, as an aware UTC datetime."""
    zone = _zone(timezone)
    day, block = local_four_hour_block(at, timezone)
    start_hour = (block + 1) * FOUR_HOUR_BLOCK_HOURS
    if start_hour >= 24:
        next_start = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=1)
    else:
        next_start = datetime(day.year, day.month, day.day, start_hour, tzinfo=zone)
    return next_start.astimezone(UTC)


#: Window name -> the function that computes its next reset. Named identically to
#: `UsagePoints.exhausted_window`'s return value, so a caller can dispatch on it directly.
RESET_FUNCS = {
    "monthly": next_monthly_reset,
    "4-hour": next_four_hour_reset,
}


def _points(snapshot: Any) -> int:
    if not snapshot.exists:
        return 0
    return int((snapshot.to_dict() or {}).get("points", 0))


class UsageRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _daily_doc(self, uid: str, day: date) -> Any:
        return self._db.client.collection(USAGE).document(f"{uid}_{day.isoformat()}")

    def _monthly_doc(self, uid: str, day: date) -> Any:
        return self._db.client.collection(USAGE).document(f"{uid}_{_month_key(day)}")

    def _four_hour_doc(self, uid: str, day: date, block: int) -> Any:
        return self._db.client.collection(USAGE).document(f"{uid}_{_four_hour_key(day, block)}")

    async def autonomous_runs(self, uid: str, day: date) -> int:
        snapshot = await self._daily_doc(uid, day).get()
        if not snapshot.exists:
            return 0
        return int((snapshot.to_dict() or {}).get("autonomousRuns", 0))

    async def record_autonomous_run(self, uid: str, day: date) -> None:
        """Count one run against the day's run-count quota.

        Incremented when a run is *created*, not when it succeeds. A run that fails has
        still spent the model calls the quota exists to bound, and a counter that only
        counted successes would let a broken project retry all day.
        """
        await self._daily_doc(uid, day).set(
            {"autonomousRuns": Increment(1), "updatedAt": now()}, merge=True
        )

    async def points_snapshot(self, uid: str, timezone: str, at: datetime) -> UsagePoints:
        """One point read per window — cheap enough for `GET /api/me` and the pre-flight
        check both, in parallel rather than two round trips in sequence."""
        day = local_day(at, timezone)
        block_day, block = local_four_hour_block(at, timezone)
        monthly, four_hour = await asyncio.gather(
            self._monthly_doc(uid, day).get(),
            self._four_hour_doc(uid, block_day, block).get(),
        )
        return UsagePoints(monthly=_points(monthly), four_hour=_points(four_hour))

    async def spend_points(
        self, uid: str, total_tokens: int, *, timezone: str, at: datetime
    ) -> int:
        """Charge `ceil(total_tokens / 1000)` points against both windows at once.

        A batch rather than two sequential writes: two independent counters, but one
        round trip is cheaper on the "a turn just finished" path. Charging nothing for
        `total_tokens <= 0` matters for a turn that failed before any model call — there is
        no spend to record and no documents should be touched.
        """
        if total_tokens <= 0:
            return 0
        points = -(-total_tokens // POINTS_TOKEN_DIVISOR)  # ceil division
        day = local_day(at, timezone)
        block_day, block = local_four_hour_block(at, timezone)
        batch = self._db.client.batch()
        for reference in (
            self._monthly_doc(uid, day),
            self._four_hour_doc(uid, block_day, block),
        ):
            batch.set(reference, {"points": Increment(points), "updatedAt": at}, merge=True)
        await batch.commit()
        return points


__all__ = [
    "FOUR_HOUR_BLOCK_HOURS",
    "POINTS_TOKEN_DIVISOR",
    "RESET_FUNCS",
    "UsageRepository",
    "local_day",
    "local_four_hour_block",
    "next_four_hour_reset",
    "next_monthly_reset",
]
