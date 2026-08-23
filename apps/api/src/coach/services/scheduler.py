"""`/internal/tick` — the planner half of the autonomous trigger chain.

docs/05-autonomous-runs.md#trigger-chain. Three phases, in this order and for this reason:

1. **Sweep** expired `postponed_until` tasks back to `not_started`, so the work they
   represent is visible to the phases below in the same tick that un-postpones it.
2. **Recover** interrupted runs — invariant 1, "interrupted work is finished before new
   work is started". A run whose instance died leaves a `running` row with an expired
   lease; a run that failed with attempts left is re-enqueued.
3. **Schedule** new runs, requested work first.

**No agent work happens here.** The tick is specified as cheap and bounded (≤ 30 s) and
every model call belongs to a Cloud Tasks delivery, which is what gives per-job retry and
backoff and keeps one poisonous project from wedging the planner for everyone.

## The two kinds of work

docs/05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them is the
specification, and the difference is exactly two things:

- **Requested** — `researchStatus == "pending"` with a `researchRequestedAt`, written by
  the learner pressing "Have my coach prepare this". It skips the four owner-facing guards
  (presence, cooldown, `autonomousEnabled`, quiet hours), because all four are defaults
  about *unprompted* work and this run was prompted. It sorts ahead of everything else.
- **Auto-scheduled** — `needsResearch` with `researchStatus ∈ {none, failed}`, which is
  what the coach signed the task up for when it created it. Every guard applies.

The lease and the daily quota apply to both: one is mutual exclusion and the other is a
cost ceiling, and neither is a policy about who asked.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from coach.core.clock import now
from coach.core.ids import run_id as new_run_id
from coach.core.principal import Principal
from coach.integrations.queue import JobQueue
from coach.repositories.presence import PresenceRepository
from coach.repositories.projects import ProjectRepository
from coach.repositories.runs import RunRepository
from coach.repositories.tasks import TaskRepository
from coach.repositories.usage import UsageRepository, local_day
from coach.repositories.users import UserRepository
from coach.services.models import (
    AutonomousRun,
    Project,
    ProjectStatus,
    QuietHours,
    ResearchStatus,
    RunStatus,
    RunStep,
    Task,
    TaskState,
    User,
)
from coach.services.tasks import TaskService

logger = logging.getLogger(__name__)

#: docs/05-autonomous-runs.md: "capped at N projects per tick to bound cost". Applied
#: *after* the sort, so a backlog of auto-scheduled work can never starve a learner who
#: pressed a button.
TICK_PROJECT_CAP = 10

#: How many active projects one tick examines before giving up.
#:
#: Much larger than `TICK_PROJECT_CAP`, and that gap is the point. The candidate query is
#: ordered by `lastAutonomousRunAt ASC` — nulls first, so a project that has never had a
#: run sorts to the front — and **a project that is always *skipped* never gets a
#: `lastAutonomousRunAt`**, so it stays at the front forever. A population of those (users
#: in quiet hours, users with autonomy off, projects whose owner is always present) will
#: sit at the head of the order permanently and starve everyone behind them if the window
#: is only as big as the cap.
#:
#: Found in the e2e, where a hundred projects belonging to users in quiet hours filled the
#: window and the test's own brand-new project was never examined at all — the tick
#: reported a hundred `quiet_hours` skips and nothing else. A slow-growing starvation that
#: a small deployment would never show, and that scanning past the skips fixes outright.
TICK_CANDIDATE_SCAN = 500

#: How many interrupted runs one tick re-enqueues.
#:
#: Small, and much smaller than the query's own limit, because recovery is the phase that
#: can arrive with a backlog: a deploy that restarted every instance leaves every in-flight
#: run stuck at once. Draining a few per tick keeps the tick inside its 30-second budget
#: and keeps the executor from being handed fifty concurrent runs against one database —
#: which is exactly what the e2e suite produced on its third consecutive run, as a wall of
#: `Aborted: Transaction lock timeout` on writes that had nothing to do with any run.
TICK_RECOVERY_CAP = 5

#: `now - project.lastAutonomousRunAt > 6 h`. Auto-scheduled work only.
COOLDOWN = timedelta(hours=6)

#: "a heartbeat in the last 120 s" (docs/02-data-model.md#presenceuid).
PRESENCE_WINDOW = timedelta(seconds=120)

#: The states a task can be in and still have work worth preparing. `draft` is first on
#: purpose: from M4 it is the state a task *starts* in and leaves when research gives it a
#: checklist, so a project full of drafts is the project with the most for a run to do.
WORKABLE_STATES = frozenset({TaskState.DRAFT, TaskState.NOT_STARTED, TaskState.IN_PROGRESS})

#: Auto-scheduled work: research the coach signed the task up for and that has not run.
#: `failed` is included because a failed run should be retried on a later tick — bounded
#: by the run's own `maxAttempts` while it exists, and by the cooldown afterwards.
AUTO_RESEARCH_STATUSES = frozenset({ResearchStatus.NONE, ResearchStatus.FAILED})

#: The five steps of `autonomous_workflow` (docs/03-agent-design.md#autonomous_workflow).
#: A queued run carries all of them, unlike a manual one — which is why `MANUAL_STEPS`
#: exists separately in `services/research.py` rather than being a slice of this.
SCHEDULED_STEPS = (
    "select_next_task",
    "research",
    "post_report",
    "propose_tasks",
    "reprioritize",
)


@dataclass(frozen=True)
class Candidate:
    """One project the tick has decided to work on."""

    project: Project
    owner: User
    #: `"requested"` or `"scheduled"` — the run's `trigger`, decided here rather than by
    #: the executor, because it is the reason the candidate was chosen at all.
    trigger: str
    #: Set for a requested candidate: the task the learner asked about. `None` for an
    #: auto-scheduled one, where `select_next_task` chooses inside the executor.
    task_id: str | None = None
    #: Which guards were skipped, for the log line. "Why did my coach work at 2 a.m. when
    #: I set quiet hours" has to have an answer that names the press.
    bypassed: tuple[str, ...] = ()


@dataclass
class TickResult:
    """What one tick did. Returned by `/internal/tick` and asserted by the e2e."""

    swept: int = 0
    recovered: list[str] = field(default_factory=list)
    scheduled: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def to_wire(self) -> dict[str, object]:
        return {
            "swept": self.swept,
            "recovered": self.recovered,
            "scheduled": self.scheduled,
            "skipped": self.skipped,
        }


class SchedulerService:
    def __init__(
        self,
        *,
        tasks: TaskService,
        task_repository: TaskRepository,
        projects: ProjectRepository,
        users: UserRepository,
        runs: RunRepository,
        presence: PresenceRepository,
        usage: UsageRepository,
        queue: JobQueue,
    ) -> None:
        self._tasks = tasks
        self._task_repository = task_repository
        self._projects = projects
        self._users = users
        self._runs = runs
        self._presence = presence
        self._usage = usage
        self._queue = queue

    async def tick(self, *, cap: int = TICK_PROJECT_CAP) -> TickResult:
        result = TickResult()
        result.swept = await self._sweep_postponements()
        result.recovered, busy = await self._recover()
        await self._schedule(result, cap=cap, busy=busy)
        logger.info("tick complete", extra=result.to_wire())
        return result

    # --- 1. sweep -------------------------------------------------------------------

    async def _sweep_postponements(self) -> int:
        """`postponed_until` tasks whose date has arrived become `not_started`.

        Through `TaskService.set_state` rather than a bulk field write, because the state
        machine and the derivation are the whole point: a task coming back with a fully
        ticked checklist should land where invariant 6 says it lands, not on
        `not_started` because that is what the sweep hard-coded.
        """
        at = now()
        due = await self._task_repository.list_expired_postponements(at)
        swept = 0
        for task in due:
            principal = Principal(uid=task.owner_uid, source="system")
            try:
                await self._tasks.set_state(principal, task.id, TaskState.NOT_STARTED)
                swept += 1
            except Exception:
                # One task's failure must not abort the sweep — and the tick's later
                # phases matter more than any single un-postponement.
                logger.exception("postponement sweep failed", extra={"task_id": task.id})
        return swept

    # --- 2. recovery ----------------------------------------------------------------

    async def _recover(self) -> tuple[list[str], set[str]]:
        """Re-enqueue runs that were interrupted. Invariant 1.

        Two populations, and they are different failures: a `running` row with an expired
        lease is an instance that died mid-run, and a `failed` row with attempts left is a
        step that raised. Both are re-delivered to the same executor, which resumes at
        `cursor` — so the expensive `research` step is never repeated for work that
        already committed its output.

        Returns the recovered run ids **and the projects they belong to**, because
        invariant 1 is "interrupted work is finished *before* new work is started" and the
        lease guard cannot enforce that here: a run whose instance died has, by definition,
        an expired lease, so the scheduling phase would find the project unlocked and queue
        a second run for it in the same tick. The two would then race for the lease, one
        would be deferred, and the recovered run's remaining steps would have been
        duplicated in a row that exists only to be thrown away.
        """
        at = now()
        recovered: list[str] = []
        busy: set[str] = set()
        stuck = await self._runs.list_stuck(at)
        retryable = await self._runs.list_retryable()

        # Poison-pill protection, and the reason it has to be here rather than in the
        # query: a run whose instance died has an expired lease *by definition*, so
        # "running with an expired lease" is true of it on every tick forever. Without
        # this, a run that reliably kills its instance is re-enqueued once per tick for
        # the life of the ledger row — each time paying for whatever step its cursor is
        # on, which is usually the expensive one.
        exhausted = [run for run in stuck if run.attempts >= run.max_attempts]
        for run in exhausted:
            with contextlib.suppress(Exception):
                await self._runs.patch(
                    run.id,
                    {
                        "status": RunStatus.FAILED.value,
                        "error": "the run was interrupted more times than it may be retried",
                    },
                )
                logger.warning(
                    "run buried after exhausting its attempts",
                    extra={"run_id": run.id, "attempts": run.attempts, "cursor": run.cursor},
                )

        live = [run for run in stuck if run.attempts < run.max_attempts]
        for run in [*live, *retryable][:TICK_RECOVERY_CAP]:
            if run.id in recovered:
                continue
            next_attempt = run.attempts + 1
            try:
                await self._runs.patch(
                    run.id,
                    {
                        "status": RunStatus.PENDING.value,
                        "attempts": next_attempt,
                        "error": None,
                    },
                )
            except Exception:
                logger.exception("run recovery failed", extra={"run_id": run.id})
                continue
            try:
                await self._queue.enqueue_run(run.id, attempts=next_attempt)
            except Exception:
                # The patch above already committed `pending`, and neither recovery query
                # matches that status (`list_stuck` wants `running`, `list_retryable` wants
                # `failed`), so a row left here is an orphan no future tick will ever find.
                # Put it back exactly where this tick found it so the next tick's
                # `list_stuck`/`list_retryable` sees it again.
                logger.exception(
                    "run recovery enqueue failed; reverting to previous status",
                    extra={"run_id": run.id, "status": run.status.value},
                )
                with contextlib.suppress(Exception):
                    await self._runs.patch(
                        run.id,
                        {
                            "status": run.status.value,
                            "attempts": run.attempts,
                            "error": run.error,
                        },
                    )
                continue
            recovered.append(run.id)
            busy.add(run.project_id)
            logger.info(
                "interrupted run re-enqueued",
                extra={
                    "run_id": run.id,
                    "cursor": run.cursor,
                    "attempts": next_attempt,
                    "was": run.status.value,
                },
            )
        return recovered, busy

    # --- 3. scheduling --------------------------------------------------------------

    async def _schedule(self, result: TickResult, *, cap: int, busy: set[str]) -> None:
        candidates = await self._candidates(result, cap=cap, busy=busy)
        for candidate in candidates:
            try:
                run = await self._create_run(candidate)
            except Exception:
                logger.exception(
                    "could not create run", extra={"project_id": candidate.project.id}
                )
                continue
            try:
                await self._queue.enqueue_run(run.id, attempts=run.attempts)
            except Exception:
                # The ledger row already exists as `pending` with no lease behind it —
                # `list_stuck` only matches `running` — so left alone it is an orphan no
                # future tick will touch. Fail it in place instead, which
                # `list_retryable` *does* match, so recovery offers it again next tick
                # rather than losing it and rather than this one exception aborting every
                # other candidate still waiting in this loop.
                logger.exception("could not enqueue run", extra={"run_id": run.id})
                with contextlib.suppress(Exception):
                    await self._runs.patch(
                        run.id,
                        {
                            "status": RunStatus.FAILED.value,
                            "error": "the run could not be enqueued",
                        },
                    )
                continue
            result.scheduled.append(run.id)
            logger.info(
                "run scheduled",
                extra={
                    "run_id": run.id,
                    "project_id": candidate.project.id,
                    "task_id": candidate.task_id,
                    "trigger": candidate.trigger,
                    "guards_bypassed": list(candidate.bypassed),
                },
            )

    async def _candidates(
        self, result: TickResult, *, cap: int, busy: set[str]
    ) -> list[Candidate]:
        """Requested work first, then auto-scheduled, then cut to `cap`.

        The order is the whole of the priority guarantee, and the cut comes last: cutting
        a merged list before sorting it would let a backlog of auto-scheduled projects
        push out a learner who pressed a button thirty seconds ago.

        `busy` are the projects this tick just re-enqueued an interrupted run for. See
        `_recover`: their leases have expired *because* the run was interrupted, so without
        this they look idle to every guard below.
        """
        chosen: list[Candidate] = []
        claimed: set[str] = set(busy)

        for candidate in await self._requested_candidates(result):
            if candidate.project.id in claimed:
                result.skip("recovering")
                continue
            claimed.add(candidate.project.id)
            chosen.append(candidate)

        remaining = max(cap - len(chosen), 0)
        for candidate in await self._auto_candidates(result, exclude=claimed, cap=remaining):
            claimed.add(candidate.project.id)
            chosen.append(candidate)

        return chosen[:cap]

    async def _requested_candidates(self, result: TickResult) -> list[Candidate]:
        """Tasks the learner queued, oldest request first.

        One collection-group query across every owner, which is why the queue is ordered
        globally rather than per project: fairness among *learners* is what a shared
        worker pool owes, and a per-project pass would serve whoever has the most projects.
        """
        candidates: list[Candidate] = []
        for task in await self._task_repository.list_requested_research():
            if task.state not in WORKABLE_STATES:
                # Filtered here rather than in the query — a third indexed field for a
                # list already bounded by how many requests can be outstanding. A queued
                # task that has since been discarded or completed is simply skipped; the
                # flag is cleared when a run claims it, and cancelling clears it too.
                result.skip("requested_task_not_workable")
                continue
            context = await self._context(task.project_id, task.owner_uid)
            if context is None:
                result.skip("requested_owner_or_project_missing")
                continue
            project, owner = context
            if project.status is not ProjectStatus.ACTIVE:
                result.skip("project_not_active")
                continue
            if not await self._shared_guards(project, owner, result):
                continue
            candidates.append(
                Candidate(
                    project=project,
                    owner=owner,
                    trigger="requested",
                    task_id=task.id,
                    bypassed=("owner_present", "cooldown", "autonomous_enabled", "quiet_hours"),
                )
            )
        return candidates

    async def _auto_candidates(
        self, result: TickResult, *, exclude: set[str], cap: int
    ) -> list[Candidate]:
        """Active projects with research the coach signed up for, fairest first.

        `cap` is honoured here as well as by the caller's final slice, so that a tick which
        has already filled its budget stops *reading* rather than evaluating hundreds of
        projects it will discard.
        """
        candidates: list[Candidate] = []
        at = now()
        # The scan is deliberately wider than the cap — see `TICK_CANDIDATE_SCAN`. The loop
        # below stops early once the cap is filled, so the wide window costs one larger
        # query and nothing else on an ordinary tick.
        for project in await self._projects.list_autonomous_candidates(TICK_CANDIDATE_SCAN):
            if len(candidates) >= cap:
                break
            if project.id in exclude:
                # Already taken by a requested candidate this tick. One run per project
                # per tick, because the lease would refuse the second anyway.
                continue
            owner = await self._users.get(project.owner_uid)
            if owner is None:
                result.skip("owner_missing")
                continue
            prefs = owner.global_prefs
            if not prefs.autonomous_enabled:
                result.skip("autonomy_disabled")
                continue
            if _in_quiet_hours(at, prefs.timezone, prefs.autonomous_quiet_hours):
                result.skip("quiet_hours")
                continue
            if (
                project.last_autonomous_run_at is not None
                and at - project.last_autonomous_run_at < COOLDOWN
            ):
                result.skip("cooldown")
                continue
            if await self._owner_present(project, at):
                # The guard this milestone's exit criterion is about. Auto-scheduled work
                # only: a run the learner asked for reaches here through
                # `_requested_candidates`, which never consults presence.
                result.skip("owner_present")
                continue
            if not await self._shared_guards(project, owner, result):
                continue
            if not await self._has_auto_research(project.id):
                result.skip("no_work")
                continue
            candidates.append(Candidate(project=project, owner=owner, trigger="scheduled"))
        return candidates

    async def _shared_guards(self, project: Project, owner: User, result: TickResult) -> bool:
        """The three guards both kinds of work are subject to: the lease, the run-count
        quota, and — since M8-quotas — the points quota."""
        holder = await self._runs.lease_holder(project.id)
        if holder is not None:
            result.skip("lease_held")
            return False
        at = now()
        day = local_day(at, owner.global_prefs.timezone)
        spent = await self._usage.autonomous_runs(owner.uid, day)
        if spent >= owner.plan.limits.autonomous_runs_per_day:
            result.skip("quota_exhausted")
            return False
        points = await self._usage.points_snapshot(owner.uid, owner.global_prefs.timezone, at)
        if points.exhausted_window(owner.plan.limits) is not None:
            # Not retried specially: an exhausted project is simply a candidate again on
            # the tick after its window resets, same as `cooldown` or `quiet_hours`.
            result.skip("points_quota_exhausted")
            return False
        return True

    async def _owner_present(self, project: Project, at: datetime) -> bool:
        presence = await self._presence.get(project.owner_uid)
        if presence is None or presence.last_heartbeat_at is None:
            return False
        if presence.active_project_id != project.id:
            return False
        return at - presence.last_heartbeat_at < PRESENCE_WINDOW

    async def _has_auto_research(self, project_id: str) -> bool:
        tasks = await self._task_repository.list_all(project_id)
        return any(wants_auto_research(task) for task in tasks)

    async def _context(self, project_id: str, owner_uid: str) -> tuple[Project, User] | None:
        project = await self._projects.get(project_id)
        owner = await self._users.get(owner_uid)
        if project is None or owner is None:
            return None
        return project, owner

    async def _create_run(self, candidate: Candidate) -> AutonomousRun:
        """The ledger row, before anything is enqueued.

        Created `pending` rather than `running`: the executor is what takes the lease and
        moves it, and a row that claimed to be running before any instance had it would
        make `list_stuck` — "running with an expired lease" — report a run that had never
        started as one that had crashed.
        """
        run = await self._runs.create(
            AutonomousRun(
                id=new_run_id(),
                owner_uid=candidate.owner.uid,
                project_id=candidate.project.id,
                task_id=candidate.task_id,
                trigger=candidate.trigger,  # type: ignore[arg-type]
                mode="queued",
                status=RunStatus.PENDING,
                steps=[RunStep(id=step) for step in SCHEDULED_STEPS],
            )
        )
        await self._usage.record_autonomous_run(
            candidate.owner.uid, local_day(now(), candidate.owner.global_prefs.timezone)
        )
        return run


def wants_auto_research(task: Task) -> bool:
    """Whether this task is work the *coach* signed up for.

    Deliberately not "research has not run": a task with `needsResearch: false` is one the
    learner or the coach decided needs no prepared material, and a scheduler that ignored
    that would research every task on the board forever.
    """
    return (
        task.state in WORKABLE_STATES
        and task.needs_research
        and task.research_status in AUTO_RESEARCH_STATUSES
    )


def _in_quiet_hours(at: datetime, timezone: str, quiet: QuietHours) -> bool:
    """Whether `at` falls inside the user's quiet window, in their own timezone.

    The window **wraps midnight** in the default case (23:00 to 07:00), so this cannot be a
    simple `start <= t < end`: that comparison is false for every hour of a wrapping
    window, which would have silently disabled quiet hours for everyone on the default.

    **`start == end` is an empty window and means quiet hours are off.** It is the only way
    to turn them off — there is no separate flag — and it is also how a test asks for a
    scheduler that does not depend on what time it is run. That is not a hypothetical
    convenience: the first run of `tests/test_scheduler.py` failed every auto-scheduled
    assertion because it happened to be 00:44 UTC, which is inside the *default* window. A
    guard evaluated against the wall clock makes a suite pass or fail by the hour.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("UTC")
    start = _parse_hhmm(quiet.start)
    end = _parse_hhmm(quiet.end)
    if start is None or end is None or start == end:
        return False
    current = at.astimezone(zone).time()
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _parse_hhmm(value: str) -> time | None:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except (ValueError, TypeError):
        return None


__all__ = [
    "AUTO_RESEARCH_STATUSES",
    "COOLDOWN",
    "PRESENCE_WINDOW",
    "SCHEDULED_STEPS",
    "TICK_CANDIDATE_SCAN",
    "TICK_PROJECT_CAP",
    "TICK_RECOVERY_CAP",
    "WORKABLE_STATES",
    "Candidate",
    "SchedulerService",
    "TickResult",
    "wants_auto_research",
]
