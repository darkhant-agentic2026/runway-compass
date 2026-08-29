"""Manual research: `POST /api/sessions/{sid}/research`, and manual roadmap generation:
`POST /api/sessions/{sid}/roadmap`.

docs/04-api-contract.md#post-apisessionssidresearch and
docs/05-autonomous-runs.md. Decision 8 of docs/00-overview.md is what this module exists to
make true:

> **Manual and autonomous research are the same code path.** [It] creates a run with
> `trigger: "manual"` and executes the identical workflow. No second implementation to
> keep in sync.

So the work is expressed as a run in the ledger, behind the project's agent lease, and —
since M9 — handed to the same `JobQueue`/`RunExecutor` a scheduled run goes through, rather
than run in this request's own process.

**Why a manual run is queued rather than started inline, since M9.** Every turn used to be
a detached `asyncio.Task` in the process that accepted the request — fine for a chat turn
the learner is watching, but a multi-minute research or roadmap run is exactly the kind of
work a learner starts and then closes the laptop on. `docs/05-autonomous-runs.md`'s trigger
chain already solves "keep working after the thing that started this is gone" for scheduled
runs — a Cloud Tasks delivery is a real in-flight HTTP request for as long as the run takes,
independent of any browser tab — so a manual run takes the same path instead of relying on
its own request surviving. What this method still does synchronously, because it is cheap
and the client needs it back in the response: lease acquisition, the ledger row, and the
research session (`sessionId` is in the 202 body immediately; `turnId` is not — the client
already polls `GET /api/runs/{runId}` for it, the same way it does for a scheduled run's).
"""

from __future__ import annotations

import logging

from coach.core.errors import Conflict, NotFound, ValidationProblem
from coach.core.ids import run_id as new_run_id
from coach.core.principal import Principal
from coach.integrations.queue import JobQueue
from coach.repositories.runs import LeaseHeld, RunRepository
from coach.services.models import AutonomousRun, ResearchStatus, RunStatus, RunStep, Task
from coach.services.quotas import QuotaService
from coach.services.sessions import SessionService
from coach.services.tasks import TaskService

logger = logging.getLogger(__name__)

#: The steps a manual research run's ledger row carries. `select_next_task` is not one of
#: them — the learner picked the task by opening it — and `propose_tasks`/`reprioritize`
#: are the autonomous pipeline's own board-reshaping steps, absent rather than `pending` so
#: `cursor` stays truthful.
MANUAL_STEPS = ("research", "post_report")

#: `start_roadmap`'s own step ids — distinct from `MANUAL_STEPS` so a client can tell the
#: two kinds of run apart from `steps[0].id` alone (docs/03-agent-design.md, "the taskless
#: case: task_proposer and plan_tailor replace reviewer_writer"), without a new field on
#: `AutonomousRun`.
ROADMAP_STEPS = ("roadmap", "write_plan")


class ResearchService:
    def __init__(
        self,
        runs: RunRepository,
        tasks: TaskService,
        sessions: SessionService,
        queue: JobQueue,
        quotas: QuotaService,
        *,
        instance_id: str,
    ) -> None:
        self._runs = runs
        self._tasks = tasks
        self._sessions = sessions
        self._queue = queue
        self._quotas = quotas
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
        attachments: list[dict[str, str]] | None = None,
    ) -> AutonomousRun:
        """Research the session's task, or — since M8 — the project as a whole.

        `session_id` names the conversation the request came from (a task's own session,
        or the project's intake session); it is used only to resolve *what* to research
        and to check ownership. The turn itself always runs in a fresh session created for
        this run, never in `session_id`
        (docs/03-agent-design.md#the-research-pipeline-since-m9).

        Raises:
            ValidationProblem: if the task is a parent (its subtasks are its plan, and
                each is researched on its own), or if the session has no task and `reason`
                is empty — there is no task description to research instead.
            Conflict: if the project's agent lease is held — carrying the in-flight
                `runId`, so the client can attach instead of starting a duplicate — or if
                the task already has materials and `force` was not set.
            QuotaBelowThreshold: if the owner's remaining monthly points are under their
                own `runStartPointsThreshold` (docs/09-roadmap.md#research-concurrency).
        """
        linkage = await self._sessions.require_owned(principal, session_id)
        if linkage.project_id is None:
            raise ValidationProblem(
                "This conversation is not linked to a project, so there is nothing to research."
            )
        project_id = linkage.project_id

        task: Task | None = None
        if linkage.task_id is not None:
            task = await self._tasks.get_with_subtasks(principal, linkage.task_id)
            if task.subtasks:
                raise ValidationProblem(
                    "This task has been split, and its subtasks are its plan. Research "
                    "the subtasks instead."
                )
            if task.research_status is ResearchStatus.DONE and not force:
                raise Conflict(
                    "This task already has materials. Research it again to replace them."
                )
        elif not reason.strip():
            raise ValidationProblem(
                "This conversation is about the project as a whole, so say what to "
                "research — there is no task description to fall back on."
            )

        run = await self._create_and_enqueue(
            principal,
            project_id=project_id,
            task_id=task.id if task is not None else None,
            session_id=session_id,
            steps=MANUAL_STEPS,
            opening_text=_opening_message(task, reason),
            attachments=attachments,
        )
        if task is not None:
            # `in_progress` before the queue even picks this up, and this is half of
            # invariant 6 (docs/02-data-model.md#task-state-machine): a task whose
            # checklist is already ticked must not complete itself while a run is about to
            # rewrite it.
            await self._tasks.set_research(
                principal, task.id, status=ResearchStatus.IN_PROGRESS
            )
        logger.info(
            "manual research run queued",
            extra={
                "run_id": run.id,
                "session_id": run.session_id,
                "task_id": task.id if task else None,
                "project_id": project_id,
                # Accepted and recorded, same as before M8 — still not threaded into the
                # model's own budget (docs/02-data-model.md's pre-existing, documented gap).
                "budget_override": budget_minutes_override,
            },
        )
        return run

    async def start_roadmap(
        self,
        principal: Principal,
        session_id: str,
        *,
        reason: str,
        attachments: list[dict[str, str]] | None = None,
        attachment_names: list[str] | None = None,
    ) -> AutonomousRun:
        """Build a study plan for the project as a whole — `build_roadmap_workflow`
        (docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer).

        Taskless only, unlike `start_manual`: `task_proposer`/`plan_tailor` size several
        tasks to the learner's own preferred length, which is a property of the whole
        project, not of one task already on the board. `session_id` names the conversation
        the request came from — the project's own intake session, or any other
        `taskId: null` session — and, as with `start_manual`, is used only to resolve
        ownership and to carry over its own uploads; the turn itself always runs in a
        fresh session created for this run.

        `attachment_names`, given, narrows the conversation's own uploads
        (`_create_and_enqueue`'s `context_attachments`) to the ones named — display names,
        matched case-insensitively. `propose_roadmap_brief` passes the brief's own
        referenced attachments here rather than letting every upload the coach
        conversation has ever seen ride along: unlike a task's own session, a project
        coach conversation is not scoped to one topic, so "everything this conversation
        has seen" can span requests unrelated to the roadmap being started. `None` (the
        button-triggered path, `StartProjectRoadmap`) keeps the old behaviour — every
        upload the session has seen carries over, since that request has no brief to
        narrow it against.

        Raises:
            ValidationProblem: the session has no project, is linked to a task, or
                `reason` is empty.
            Conflict: the project's agent lease is held — carries the in-flight `runId`,
                so the client can attach to that run instead of starting a duplicate.
            QuotaBelowThreshold: if the owner's remaining monthly points are under their
                own `runStartPointsThreshold` (docs/09-roadmap.md#research-concurrency).
        """
        linkage = await self._sessions.require_owned(principal, session_id)
        if linkage.project_id is None:
            raise ValidationProblem(
                "This conversation is not linked to a project, so there is nothing to plan."
            )
        if linkage.task_id is not None:
            raise ValidationProblem(
                "Roadmap generation is for the project as a whole. Ask from the "
                "project's own conversation, not from inside one task."
            )
        if not reason.strip():
            raise ValidationProblem(
                "Say what the roadmap should cover — there is no task description to "
                "fall back on."
            )

        run = await self._create_and_enqueue(
            principal,
            project_id=linkage.project_id,
            task_id=None,
            session_id=session_id,
            steps=ROADMAP_STEPS,
            opening_text=reason.strip(),
            attachments=attachments,
            attachment_names=attachment_names,
        )
        logger.info(
            "roadmap run queued",
            extra={
                "run_id": run.id,
                "session_id": run.session_id,
                "project_id": linkage.project_id,
            },
        )
        return run

    async def _create_and_enqueue(
        self,
        principal: Principal,
        *,
        project_id: str,
        task_id: str | None,
        session_id: str,
        steps: tuple[str, str],
        opening_text: str,
        attachments: list[dict[str, str]] | None,
        attachment_names: list[str] | None = None,
    ) -> AutonomousRun:
        """Shared by `start_manual` and `start_roadmap`: the points threshold, lease,
        ledger row, session, enqueue. What differs between the two callers is entirely in
        their arguments — which steps, what the turn should open with, and whether a task
        is involved — not in how the run gets from "accepted" to "handed to the queue".
        """
        # docs/09-roadmap.md#research-concurrency: the same gate `SchedulerService`
        # applies to a scheduled or requested run, checked first and cheaply so a run
        # unlikely to finish inside the real quota is refused before the lease, the
        # ledger row, or the research session are ever created.
        await self._quotas.require_room_to_start_run(principal.uid)

        run_id = new_run_id()
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

        # Whatever the learner has already uploaded in *this* conversation — the task's
        # own, or the project's intake one — carried over automatically, so a scope that
        # mentions a file already sent does not also require re-attaching it here. Resolved
        # now, not by the executor: `session_id` is the *caller's* conversation, not this
        # run's own, and is not reachable from the ledger once this request returns.
        context_attachments = await self._sessions.list_attachments(principal, session_id)
        if attachment_names is not None:
            # Narrowed to the referenced subset (`start_roadmap`'s own docstring) rather
            # than trusted wholesale — `None` (every other caller) keeps every upload the
            # conversation has seen.
            wanted = {name.strip().lower() for name in attachment_names if name.strip()}
            context_attachments = [
                attachment
                for attachment in context_attachments
                if attachment.get("displayName", "").strip().lower() in wanted
            ]

        run = await self._runs.create(
            AutonomousRun(
                id=run_id,
                owner_uid=principal.uid,
                project_id=project_id,
                task_id=task_id,
                trigger="manual",
                mode="queued",
                status=RunStatus.PENDING,
                instance_id=self._instance_id,
                steps=[RunStep(id=steps[0]), RunStep(id=steps[1])],
                pending_text=opening_text,
                pending_attachments=attachments,
                pending_context_attachments=context_attachments,
            )
        )

        # A fresh session for this run, never `session_id` — docs/02-data-model.md:
        # "A research session is minted fresh for every run, and never reused." Created
        # here rather than left to the executor so the 202 response can carry it
        # immediately — cheap (one Firestore write, no model call), unlike the turn itself.
        research_session = await self._sessions.create_research_session(
            principal, project_id=project_id, task_id=task_id, run_id=run_id
        )
        await self._runs.patch(run_id, {"sessionId": research_session.id})
        run = run.model_copy(update={"session_id": research_session.id})

        await self._queue.enqueue_run(run_id, attempts=1)
        return run


def _opening_message(task: Task | None, reason: str) -> str:
    """The message that starts the research turn.

    It is a real user-authored message in the research session's transcript, not a hidden
    prompt, and that is deliberate: the learner pressed a button, and the conversation
    should show that they did. A run whose materials appear from nowhere reads as the
    coach acting unprompted, which is the thing docs/10-risks.md#r6 is about.

    `task` is `None` for research kicked off from the project coach's own conversation
    (M8): the whole message is then the learner's own `reason`, already asserted non-empty
    by the caller.
    """
    if task is None:
        return reason.strip()
    lines = [
        f"Please prepare the materials I need for this task: {task.title}.",
    ]
    if task.description:
        lines.append(f"What done looks like: {task.description}")
    if reason.strip():
        lines.append(reason.strip())
    return "\n".join(lines)


__all__ = ["MANUAL_STEPS", "ROADMAP_STEPS", "ResearchService"]
