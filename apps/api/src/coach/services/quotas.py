"""Usage quotas: the pre-flight gate and the post-turn spend.

docs/02-data-model.md#usage-quotas-m8-quotas is the specification. `TurnService.start` is
the one place every interactive turn, research run, and autonomous pass converges
(docs/09-roadmap.md#status-after-m4), so it is the only call site `require_available` and
`record_spend` need — nothing else calls the model.
"""

from __future__ import annotations

from coach.core.clock import now
from coach.core.errors import QuotaBelowThreshold, QuotaExceeded
from coach.repositories.usage import (
    POINTS_TOKEN_DIVISOR,
    RESET_FUNCS,
    UsageRepository,
    next_four_hour_reset,
    next_monthly_reset,
)
from coach.repositories.users import UserRepository
from coach.services.models import UsagePoints, UsageStatus, UsageWindow, User

#: docs/09-roadmap.md#research-concurrency: `turn_complete` carries a low-points hint once
#: remaining monthly points drop under `runStartPointsThreshold + LOW_POINTS_NAG_MARGIN` —
#: enough headroom above the run-start gate itself that the learner sees the nag before a
#: run they try to start is actually refused.
LOW_POINTS_NAG_MARGIN = 100


class QuotaService:
    def __init__(self, users: UserRepository, usage: UsageRepository) -> None:
        self._users = users
        self._usage = usage

    async def require_available(self, uid: str) -> None:
        """Raise `QuotaExceeded` if either window is spent.

        A missing user is not this method's problem to report — every real caller reaches
        it after `UserService.get_or_create`, which is the one place a user document is
        created — so it silently allows rather than raising a confusing error of its own.
        """
        user = await self._users.get(uid)
        if user is None:
            return
        at = now()
        snapshot = await self._usage.points_snapshot(uid, user.global_prefs.timezone, at)
        window = snapshot.exhausted_window(user.plan.limits)
        if window is None:
            return
        reset_at = RESET_FUNCS[window](at, user.global_prefs.timezone)
        raise QuotaExceeded(window, reset_at)

    async def record_spend(self, uid: str, total_tokens: int) -> None:
        """Charge `total_tokens` worth of points, whatever the turn's outcome was.

        Called from `TurnService._generate`'s `finally`, so tokens already spent are
        recorded on `complete`, `cancelled`, and `failed` alike — the same "counted when
        spent, not when the outcome is good" reasoning `record_autonomous_run` already
        applies to the run-count quota.
        """
        if total_tokens <= 0:
            return
        user = await self._users.get(uid)
        timezone = user.global_prefs.timezone if user is not None else "UTC"
        await self._usage.spend_points(uid, total_tokens, timezone=timezone, at=now())

    async def require_room_to_start_run(self, uid: str) -> None:
        """Raise `QuotaBelowThreshold` if the owner's remaining monthly points are under
        their own `runStartPointsThreshold`.

        Called before a research/roadmap run is created or enqueued — manual, roadmap, and
        (via `SchedulerService`'s own version of this check) scheduled and requested
        triggers alike — so a run unlikely to finish inside the real quota is refused
        before it spends anything, rather than after. A missing user allows, for the same
        reason `require_available` does.
        """
        user = await self._users.get(uid)
        if user is None:
            return
        remaining = await self._monthly_remaining(user)
        threshold = user.plan.limits.run_start_points_threshold
        if remaining < threshold:
            raise QuotaBelowThreshold(threshold, remaining)

    async def points_hint(self, uid: str, extra_tokens: int = 0) -> tuple[int, int] | None:
        """`(remaining, threshold)` to attach to `turn_complete`, or `None` if there is no
        need to nag.

        A read, never a write: `extra_tokens` (a turn's own token spend, not yet recorded
        by `record_spend`) is folded into the projection so the hint reflects the points
        this turn is *about* to cost, without a second Firestore write racing the one
        `record_spend` makes moments later in `TurnService._generate`'s `finally`.
        """
        user = await self._users.get(uid)
        if user is None:
            return None
        at = now()
        snapshot = await self._usage.points_snapshot(uid, user.global_prefs.timezone, at)
        extra_points = -(-extra_tokens // POINTS_TOKEN_DIVISOR) if extra_tokens > 0 else 0
        projected = UsagePoints(monthly=snapshot.monthly + extra_points)
        threshold = user.plan.limits.run_start_points_threshold
        remaining = projected.monthly_remaining(user.plan.limits)
        if remaining >= threshold + LOW_POINTS_NAG_MARGIN:
            return None
        return remaining, threshold

    async def _monthly_remaining(self, user: User) -> int:
        at = now()
        snapshot = await self._usage.points_snapshot(user.uid, user.global_prefs.timezone, at)
        return snapshot.monthly_remaining(user.plan.limits)

    async def status(self, user: User) -> UsageStatus:
        """`GET /api/me`'s `usage` field: spend, limit, and reset time for both windows,
        regardless of whether either is exhausted."""
        at = now()
        timezone = user.global_prefs.timezone
        snapshot = await self._usage.points_snapshot(user.uid, timezone, at)
        limits = user.plan.limits
        return UsageStatus(
            monthly=UsageWindow(
                spent=snapshot.monthly,
                limit=limits.monthly_points,
                resets_at=next_monthly_reset(at, timezone),
            ),
            four_hour=UsageWindow(
                spent=snapshot.four_hour,
                limit=limits.four_hour_points,
                resets_at=next_four_hour_reset(at, timezone),
            ),
        )


__all__ = ["QuotaService"]
