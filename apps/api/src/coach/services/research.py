"""Manual research: `POST /api/sessions/{sid}/research`.

docs/04-api-contract.md#post-apisessionssidresearch and
docs/05-autonomous-runs.md. The endpoint is M4's; the scheduler that will drive the *same*
path is M5's. Decision 8 of docs/00-overview.md is what this module exists to make true:

> **Manual and autonomous research are the same code path.** [It] creates a run with
> `trigger: "manual"` and executes the identical workflow. No second implementation to
> keep in sync.

So the work is expressed as a run in the ledger, behind the project's agent lease, even
though nothing schedules one yet. Doing it any other way now would mean M5 either
re-implementing it or refactoring it, and the second is only cheaper if someone remembers
to do it.

**A research run is an ordinary turn.** It gets a `turns/{turnId}` document, a detached
generation task, checkpoints, and a broker subscription — so the disconnect guarantee
covers it for free, the tool-activity chips render from the same frames, and the report
lands in the transcript the learner is reading. What differs is one argument to
`TurnService.start`.
"""

from __future__ import annotations

import contextlib
import logging

from coach.core.clock import now
from coach.core.errors import Conflict, NotFound, ValidationProblem
from coach.core.ids import run_id as new_run_id
from coach.core.principal import Principal
from coach.repositories.runs import LeaseHeld, RunRepository
from coach.services.models import (
    AutonomousRun,
    ResearchStatus,
    RunStatus,
    RunStep,
    StepStatus,
    Task,
    TurnStatus,
)
from coach.services.sessions import SessionService
from coach.services.tasks import TaskService
from coach.services.turns import TurnService

logger = logging.getLogger(__name__)

#: The steps M4 implements. docs/05-autonomous-runs.md lists five; `select_next_task` is
#: not one of them for a manual run — the learner picked the task by opening it — and
#: `propose_tasks`/`reprioritize` are M5. Absent rather than `pending`, so `cursor` stays
#: truthful.
MANUAL_STEPS = ("research", "post_report")


class ResearchService:
    def __init__(
        self,
        runs: RunRepository,
        tasks: TaskService,
        sessions: SessionService,
        turns: TurnService,
        *,
        instance_id: str,
    ) -> None:
        self._runs = runs
        self._tasks = tasks
        self._sessions = sessions
        self._turns = turns
        self._instance_id = instance_id

    async def get(self, principal: Principal, run_id: str) -> AutonomousRun:
        run = await self._runs.get(run_id)
        if run is None or run.owner_uid != principal.uid:
            raise NotFound(f"No run {run_id!r}.")
        return run

    async def start_manual(
        self,
        principal: Principal,
        session_id: str,
        *,
        reason: str = "",
        budget_minutes_override: int | None = None,
        force: bool = False,
    ) -> AutonomousRun:
        """Research the session's task now.

        Raises:
            ValidationProblem: if the session is not attached to a task, or the task is a
                parent (its subtasks are its plan, and each is researched on its own).
            Conflict: if the project's agent lease is held — carrying the in-flight
                `runId`, so the client can attach instead of starting a duplicate — or if
                the task already has materials and `force` was not set.
        """
        linkage = await self._sessions.require_owned(principal, session_id)
        if linkage.task_id is None or linkage.project_id is None:
            raise ValidationProblem(
                "This conversation is about the project as a whole. Open a task to research it."
            )
        task = await self._tasks.get_with_subtasks(principal, linkage.task_id)
        if task.subtasks:
            raise ValidationProblem(
                "This task has been split, and its subtasks are its plan. Research the "
                "subtasks instead."
            )
        if task.research_status is ResearchStatus.DONE and not force:
            raise Conflict(
                "This task already has materials. Research it again to replace them."
            )

        run_id = new_run_id()
        project_id = linkage.project_id
        try:
            await self._runs.acquire_lease(project_id, run_id, self._instance_id)
        except LeaseHeld as held:
            # `runId` rides on the problem document, which is what makes the 409
            # actionable: the client subscribes to the in-flight run rather than showing
            # the learner an error and inviting them to press the button again.
            raise Conflict(
                "Your coach is already working on this project. Watch that run instead of "
                "starting a second one.",
                runId=held.run_id,
            ) from held

        # Created before the turn, because the turn's completion callback closes it and a
        # very fast run could otherwise finish before the row existed.
        run = await self._runs.create(
            AutonomousRun(
                id=run_id,
                owner_uid=principal.uid,
                project_id=project_id,
                task_id=task.id,
                trigger="manual",
                mode="inline",
                status=RunStatus.RUNNING,
                instance_id=self._instance_id,
                steps=[
                    RunStep(id="research", status=StepStatus.RUNNING, started_at=now()),
                    RunStep(id="post_report"),
                ],
            )
        )
        # `in_progress` before the first model call, and this is half of invariant 6
        # (docs/02-data-model.md#task-state-machine): a task whose checklist is already
        # ticked must not complete itself while a run is about to rewrite that checklist.
        await self._tasks.set_research(principal, task.id, status=ResearchStatus.IN_PROGRESS)

        try:
            turn = await self._turns.start(
                principal,
                session_id,
                text=_opening_message(task, reason),
                agent="research",
                on_finished=lambda: self._close(principal, run_id, project_id, task.id),
            )
        except Exception:
            # The lease is only worth holding while something is using it. A turn that
            # failed to start leaves nothing to release it later, so a project would be
            # locked out of research for the full five-minute TTL because of a validation
            # error.
            await self._runs.release_lease(project_id, run_id)
            await self._runs.patch(run_id, {"status": RunStatus.FAILED.value})
            await self._tasks.set_research(principal, task.id, status=ResearchStatus.FAILED)
            raise

        run = run.model_copy(update={"turn_id": turn.id})
        await self._runs.patch(run_id, {"turnId": turn.id})

        logger.info(
            "manual research run started",
            extra={
                "run_id": run.id,
                "turn_id": turn.id,
                "task_id": task.id,
                "project_id": project_id,
                "budget_override": budget_minutes_override,
            },
        )
        return run

    async def _close(
        self, principal: Principal, run_id: str, project_id: str, task_id: str
    ) -> None:
        """Mark the run terminal, settle `researchStatus`, and release the lease.

        Runs as `TurnService.start`'s `on_finished`, so it fires the moment the generation
        task is over — not at the next tick of a poller. The difference is not cosmetic: the
        report renders as soon as `post_research_report` pushes `board_update`, which is
        *before* the turn finishes streaming its closing prose, so any gap between "the turn
        ended" and "the lease is free" is a window in which the learner can press "Research
        again" and be told their coach is already busy.

        `researchStatus` is only moved on the failure paths. On the happy path
        `post_research_report` has already set it to `done` *and* written the checklist, in
        the transaction that also promoted the task out of `draft` — overwriting it here
        would be a second writer for one field, and the one with the least information.

        Every step is `suppress`ed individually rather than wrapped as one: this runs
        detached with nothing to report a failure to, and giving up on the lease because
        the ledger write failed would leave the project locked for the full TTL.

        The run is **re-read** rather than captured. The callback is built before
        `TurnService.start` returns, so a captured `AutonomousRun` would not yet carry the
        `turnId` this method needs — in practice it would, because generation cannot finish
        inside one loop tick, and "in practice" is not a thing to rest a lease on.
        """
        outcome = RunStatus.FAILED
        detail: str | None = "generation failed"
        run = await self._runs.get(run_id)
        with contextlib.suppress(Exception):
            turn = await self._turns.get(principal, (run.turn_id if run else None) or "")
            if turn.status is TurnStatus.COMPLETE:
                outcome, detail = RunStatus.COMPLETE, None
            elif turn.status is TurnStatus.CANCELLED:
                outcome, detail = RunStatus.CANCELLED, "cancelled"
            elif turn.error is not None:
                detail = turn.error.message

        succeeded = outcome is RunStatus.COMPLETE
        steps = [
            RunStep(
                id="research",
                status=StepStatus.COMPLETE if succeeded else StepStatus.FAILED,
                ended_at=now(),
                error=detail,
            ),
            RunStep(
                id="post_report",
                status=StepStatus.COMPLETE if succeeded else StepStatus.SKIPPED,
                ended_at=now(),
            ),
        ]
        with contextlib.suppress(Exception):
            await self._runs.patch(
                run_id,
                {
                    "status": outcome.value,
                    "steps": [step.to_document() for step in steps],
                    "error": detail,
                },
            )
        if not succeeded:
            # A turn that failed or was cancelled leaves the task looking like research is
            # still running, and invariant 6 reads that field — so a task whose checklist
            # was already finished would stay open forever waiting for a run that is gone.
            with contextlib.suppress(Exception):
                await self._tasks.set_research(principal, task_id, status=ResearchStatus.FAILED)
        await self._runs.release_lease(project_id, run_id)
        logger.info(
            "manual research run finished",
            extra={"run_id": run_id, "status": outcome.value, "detail": detail},
        )


def _opening_message(task: Task, reason: str) -> str:
    """The message that starts the research turn.

    It is a real user-authored message in the task's transcript, not a hidden prompt, and
    that is deliberate: the learner pressed a button, and the conversation should show
    that they did. A run whose materials appear from nowhere reads as the coach acting
    unprompted, which is the thing docs/10-risks.md#r6 is about.
    """
    lines = [
        f"Please prepare the materials I need for this task: {task.title}.",
    ]
    if task.description:
        lines.append(f"What done looks like: {task.description}")
    if reason.strip():
        lines.append(reason.strip())
    return "\n".join(lines)


__all__ = ["MANUAL_STEPS", "ResearchService"]
