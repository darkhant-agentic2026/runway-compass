"""`/internal/runs/{runId}/execute` — the working half of the autonomous chain.

docs/05-autonomous-runs.md#execution-semantics. The executor loads the run, resumes at
`cursor`, and runs the five steps of `autonomous_workflow` in order:

| # | Step | Kind |
| --- | --- | --- |
| 1 | `select_next_task` | code |
| 2 | `research` | `research_agent`, as an ordinary turn |
| 3 | `post_report` | code — settles what the tool wrote |
| 4 | `propose_tasks` | its own agent, with the reduced tool set |
| 5 | `reprioritize` | code |

**Each step commits its output to the ledger before the next begins.** That sentence is
the whole design: a crash during `propose_tasks` never repeats `research`, which is the
expensive step, and "complete previously interrupted work" means resuming at `cursor`
rather than starting again.

**A research step is an ordinary turn, and the executor waits on an `asyncio.Event` rather
than on the generation task.** `TurnService.start` must never grow an `await` on
generation — that is the disconnect guarantee, stated as an absence
(docs/04-api-contract.md#surviving-client-disconnects) — so the executor passes an
`on_finished` callback that sets an event and waits for *that*. The generation task still
belongs to the `TurnRegistry`, so a Cloud Tasks delivery that times out and is retried
leaves inference running rather than killing it, exactly as a browser tab closing does.

**The presence guard is checked a second time here**, at execution rather than scheduling
time, because a Cloud Tasks delivery can arrive minutes after the tick and the learner may
have sat down in the meantime — and **only for `trigger: "scheduled"`**. A requested run
whose learner is sitting on the page is the expected case, not the race
(docs/05-autonomous-runs.md#presence-guard-details).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from coach.agents.context import RUN_ID_KEY
from coach.core.clock import now
from coach.core.principal import Principal
from coach.repositories.presence import PresenceRepository
from coach.repositories.projects import ProjectRepository
from coach.repositories.runs import LeaseHeld, RunRepository
from coach.repositories.tasks import TaskRepository
from coach.services.models import (
    AutonomousRun,
    ResearchStatus,
    RunChange,
    RunStatus,
    RunStep,
    StepStatus,
    Task,
    Turn,
    TurnStatus,
)
from coach.services.scheduler import PRESENCE_WINDOW, WORKABLE_STATES, wants_auto_research
from coach.services.sessions import SessionService
from coach.services.tasks import TaskService
from coach.services.turns import AgentChoice, TurnService
from coach.ws.hub import BoardUpdateHub

logger = logging.getLogger(__name__)

#: How often the executor pushes the lease's expiry out while a step runs.
#: docs/05-autonomous-runs.md#the-lease: "renewed every 60 s by the executing task with a
#: 5-minute TTL".
LEASE_RENEW_SECONDS = 60.0

#: A ceiling on one step's generation, well inside Cloud Tasks' 15-minute dispatch
#: deadline. A step that blows through it is marked failed and retried rather than holding
#: the project's lease until the TTL expires.
STEP_TIMEOUT_SECONDS = 600.0


class RunExecutor:
    def __init__(
        self,
        *,
        runs: RunRepository,
        tasks: TaskService,
        task_repository: TaskRepository,
        projects: ProjectRepository,
        sessions: SessionService,
        turns: TurnService,
        presence: PresenceRepository,
        board_updates: BoardUpdateHub,
        instance_id: str,
    ) -> None:
        self._runs = runs
        self._tasks = tasks
        self._task_repository = task_repository
        self._projects = projects
        self._sessions = sessions
        self._turns = turns
        self._presence = presence
        self._board_updates = board_updates
        self._instance_id = instance_id

    async def execute(self, run_id: str) -> AutonomousRun | None:
        """Run or resume one ledger row. Returns it as it stands afterwards.

        `None` means there was nothing to do — an unknown run, or one already terminal.
        A re-delivery of a completed task is not an error: Cloud Tasks retries at least
        once in situations where the response never made it back, and answering `200` for
        a run that is already finished is what stops that becoming a second execution.
        """
        run = await self._runs.get(run_id)
        if run is None:
            logger.warning("execute called for unknown run", extra={"run_id": run_id})
            return None
        terminal = {
            RunStatus.COMPLETE,
            RunStatus.CANCELLED,
            RunStatus.SKIPPED_OWNER_PRESENT,
        }
        if run.status in terminal:
            return run

        principal = Principal(uid=run.owner_uid, source="system")
        try:
            await self._runs.acquire_lease(run.project_id, run.id, self._instance_id)
        except LeaseHeld as held:
            if held.run_id != run.id:
                # Somebody else is working this project. Leave the row `pending` rather
                # than failing it: the tick's recovery pass will offer it again once the
                # other run has released the lease, and a `failed` row here would burn one
                # of this run's three attempts on a queue collision.
                logger.info(
                    "run deferred: project lease held",
                    extra={"run_id": run.id, "held_by": held.run_id},
                )
                return run
            # Our own lease, from an earlier delivery of this same run. Renewing it is the
            # resume path, and `acquire_lease` refuses to re-take a live lease even for
            # its own holder.
            await self._runs.renew_lease(run.project_id, run.id)

        if run.trigger == "scheduled" and await self._owner_present(run):
            # The second of the two checks. Abandoned, not failed: the learner sat down,
            # which is the guard working rather than anything going wrong.
            await self._runs.patch(
                run.id,
                {"status": RunStatus.SKIPPED_OWNER_PRESENT.value, "error": None},
            )
            await self._runs.release_lease(run.project_id, run.id)
            logger.info("run skipped: owner present", extra={"run_id": run.id})
            return await self._runs.get(run.id)

        await self._runs.patch(
            run.id,
            {
                "status": RunStatus.RUNNING.value,
                "instanceId": self._instance_id,
                "leaseExpiresAt": now() + timedelta(seconds=LEASE_RENEW_SECONDS * 5),
                "error": None,
            },
        )
        renewer = asyncio.create_task(
            self._renew_lease(run.project_id, run.id), name=f"lease:{run.id}"
        )
        try:
            return await self._run_steps(principal, run)
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer
            await self._runs.release_lease(run.project_id, run.id)

    # --- the step loop ---------------------------------------------------------------

    async def _run_steps(self, principal: Principal, run: AutonomousRun) -> AutonomousRun:
        steps = {step.id: step for step in run.steps}
        for step in run.steps:
            if step.status is StepStatus.COMPLETE or step.status is StepStatus.SKIPPED:
                continue
            await self._mark(run, step.id, StepStatus.RUNNING, started_at=now())
            try:
                output = await asyncio.wait_for(
                    self._dispatch(principal, run, step.id, steps),
                    timeout=STEP_TIMEOUT_SECONDS,
                )
            except NothingToDo as nothing:
                # Not a failure. The tick chose this project and the board moved between
                # the two — an ordinary outcome, and marking it `failed` would put a red
                # banner in front of a learner for a run that did the right thing.
                logger.info("run had no work", extra={"run_id": run.id, "detail": str(nothing)})
                await self._skip_remaining(run, from_step=step.id)
                await self._complete(run)
                return await self._reload(run)
            except Exception as exc:
                logger.exception("run step failed", extra={"run_id": run.id, "step": step.id})
                await self._mark(run, step.id, StepStatus.FAILED, error=str(exc))
                await self._fail(principal, run, str(exc))
                return await self._reload(run)
            if output is _SKIPPED:
                await self._mark(run, step.id, StepStatus.SKIPPED, ended_at=now())
                continue
            await self._mark(run, step.id, StepStatus.COMPLETE, ended_at=now(), output=output)
            steps[step.id] = steps[step.id].model_copy(
                update={"status": StepStatus.COMPLETE, "output": output}
            )
            # Re-read so the next step sees the ids and changes its predecessor recorded.
            # Cheap — one document — and the alternative is two in-memory copies of the
            # ledger drifting apart across a step boundary that can span minutes.
            run = await self._reload(run)

        await self._complete(run)
        return await self._reload(run)

    async def _dispatch(
        self,
        principal: Principal,
        run: AutonomousRun,
        step: str,
        steps: dict[str, RunStep],
    ) -> dict[str, object] | object:
        match step:
            case "select_next_task":
                return await self._select_next_task(principal, run)
            case "research":
                return await self._research(principal, run, steps)
            case "post_report":
                return await self._post_report(principal, run, steps)
            case "propose_tasks":
                return await self._propose_tasks(principal, run, steps)
            case "reprioritize":
                return await self._reprioritize(principal, run, steps)
            case _:  # pragma: no cover - the step list is a module constant
                raise ValueError(f"Unknown run step {step!r}.")

    # --- 1. select_next_task ----------------------------------------------------------

    async def _select_next_task(
        self, principal: Principal, run: AutonomousRun
    ) -> dict[str, object]:
        """Deterministic, no LLM. A *requested* task outranks the ordinary choice.

        A run that took the project because the learner asked has to research the thing
        they asked about; otherwise the press would move the queue and then prepare
        something else (docs/03-agent-design.md#autonomous_workflow).

        `output.requested` is recorded because the flag it selected on is cleared by this
        same step — so by the time a later step, a recovery, or a reader of the ledger
        asks, the task document no longer says who wanted this run.
        """
        task = await self._chosen_task(principal, run)
        if task is None:
            raise NothingToDo("No task in this project needs research.")
        requested = task.research_status is ResearchStatus.PENDING
        if requested:
            # Clears `researchRequestedAt` in the same write. See
            # docs/05-autonomous-runs.md#when-the-request-flag-is-cleared for why this is
            # at the start and not at the end.
            await self._tasks.claim_research_request(principal, task.id)
        else:
            await self._tasks.set_research(
                principal, task.id, status=ResearchStatus.IN_PROGRESS
            )
        return {
            "taskId": task.id,
            "budgetMinutes": task.estimated_minutes,
            "requested": requested,
        }

    async def _chosen_task(self, principal: Principal, run: AutonomousRun) -> Task | None:
        if run.task_id is not None:
            # A requested run names its task at creation, and a resumed run reuses the id
            # `select_next_task` already recorded. Both land here.
            with contextlib.suppress(Exception):
                return await self._tasks.resolve(principal, run.task_id)
            return None
        board = await self._task_repository.list_all(run.project_id)
        children = {task.parent_task_id for task in board if task.parent_task_id}
        workable = [
            task for task in board if wants_auto_research(task) and task.id not in children
        ]
        return min(workable, key=lambda task: task.order, default=None)

    # --- 2. research ------------------------------------------------------------------

    async def _research(
        self, principal: Principal, run: AutonomousRun, steps: dict[str, RunStep]
    ) -> dict[str, object] | object:
        """One `research_agent` turn in a session created fresh for this run.

        **Since M8**, this is a dedicated session — never the task's own conversation —
        created once and recorded on the ledger (`run.sessionId`), the same way `turnId`
        is: docs/02-data-model.md#sessions--events-adk-owned-layout. A resumed run (a
        retried step, after a crash) must not create a second session for the same run,
        which is what the `run.session_id` check below is for.
        """
        task_id = _task_id_of(steps, run)
        task = await self._tasks.resolve(principal, task_id)
        if _skip_research(task, run):
            # docs/03-agent-design.md: "Skipped if `task.needsResearch == false`" — except
            # for a run that took the project *because* this task was requested, which
            # "has to research that thing" regardless (same table, step 1). Otherwise a
            # learner pressing "prepare this" on a task the coach itself marked
            # `needsResearch: false` gets a run that can never succeed: select_next_task
            # resolves the request unconditionally, but this guard would skip it anyway.
            return _SKIPPED
        session_id = await self._ensure_session(principal, run, task_id)
        # Whatever the learner has already uploaded in the task's own conversation,
        # carried into the research turn the same way the manual trigger does
        # (`ResearchService.start_manual`) — a scheduled run reads the same task
        # description a learner does, and a description that mentions an attached file
        # deserves the same access to it whether or not anyone was watching this run
        # start.
        context_attachments = (
            await self._sessions.list_attachments(principal, task.session_id)
            if task.session_id
            else []
        )
        turn = await self._run_turn(
            principal,
            session_id,
            text=_research_message(task),
            agent="research",
            run_id=run.id,
            context_attachments=context_attachments,
        )
        if turn.status is not TurnStatus.COMPLETE:
            raise StepFailed(
                (turn.error.message if turn.error else None)
                or "the research turn did not finish"
            )
        return {"turnId": turn.id, "sessionId": session_id}

    # --- 3. post_report ---------------------------------------------------------------

    async def _post_report(
        self, principal: Principal, run: AutonomousRun, steps: dict[str, RunStep]
    ) -> dict[str, object] | object:
        """Settle what the research turn's tool wrote, and say so on the board.

        The *writing* is `post_research_report`'s, inside the turn — this step is the
        ledger's record that it happened and the point at which a task whose research
        never produced a report is marked `failed` rather than left `in_progress` forever.

        `researchStatus` is only moved on the failure path, for the reason `ResearchService`
        gives: on the happy path the tool has already set `done` *and* written the
        checklist, in the transaction that promoted the task out of `draft`, and a second
        writer for one field is the one with the least information.

        Re-derives the same skip `_research` used rather than trusting that step's recorded
        status: `_run_steps` never refreshes its local `steps` copy after a skip (only after
        a completion), so reading it back here would be reading stale data.
        """
        task_id = _task_id_of(steps, run)
        task = await self._tasks.resolve(principal, task_id)
        if _skip_research(task, run):
            return _SKIPPED
        if task.research_status is not ResearchStatus.DONE:
            await self._tasks.set_research(principal, task_id, status=ResearchStatus.FAILED)
            raise StepFailed("the research turn produced no report")
        await self._board_updates.publish(
            run.owner_uid,
            project_id=run.project_id,
            task_ids=[task_id],
            origin="agent",
            run_id=run.id,
        )
        return {"reportId": task.latest_report_id, "checklistLength": len(task.items)}

    # --- 4. propose_tasks -------------------------------------------------------------

    async def _propose_tasks(
        self, principal: Principal, run: AutonomousRun, steps: dict[str, RunStep]
    ) -> dict[str, object]:
        """A bounded `propose_tasks` turn that may add prerequisites research turned up.

        Runs with the **reduced tool set** (docs/03-agent-design.md#safety-rails-on-autonomy):
        no `discard_task`, no `update_learner_profile`, no `update_project_prefs`.
        `agents/tools.py` builds that subset; nothing here decides it.

        **Since M8, this turn lands in the run's own research session, not the task's
        conversation** — the same reasoning that moved `research` there applies here: this
        step's tool calls are the coach maintaining the board on its own initiative, not a
        conversation with the learner, and the task session should read as the latter.

        Tasks the run creates are recorded on the ledger as `task_created` changes, which
        is what the "Updated by your coach" banner lists and what undo deletes. Recorded by
        **diffing the project's task ids around the turn** rather than by asking the model
        what it did: a model that under-reports its own writes would leave the learner with
        tasks undo cannot remove.
        """
        task_id = _task_id_of(steps, run)
        session_id = await self._ensure_session(principal, run, task_id)
        before = {task.id for task in await self._task_repository.list_all(run.project_id)}
        turn = await self._run_turn(
            principal,
            session_id,
            text=_propose_message(),
            agent="propose",
            run_id=run.id,
        )
        after = await self._task_repository.list_all(run.project_id)
        created = [task.id for task in after if task.id not in before]
        if created:
            await self._record_changes(
                run, [RunChange(kind="task_created", task_id=task_id) for task_id in created]
            )
            await self._board_updates.publish(
                run.owner_uid,
                project_id=run.project_id,
                task_ids=created,
                origin="agent",
                run_id=run.id,
            )
        return {"turnId": turn.id, "createdTaskIds": created, "status": turn.status.value}

    # --- 5. reprioritize --------------------------------------------------------------

    async def _reprioritize(
        self, principal: Principal, run: AutonomousRun, steps: dict[str, RunStep]
    ) -> dict[str, object]:
        """Put the researched task at the front of the board. Deterministic, no LLM.

        docs/03-agent-design.md keeps steps 1 and 5 out of the model deliberately:
        "ordering and selection are rules, and making them rules removes a whole class of
        nondeterminism from background behaviour". The rule is that a task whose materials
        are now ready is the one to sit down to.

        Writing a fractional index is naturally idempotent, so this step needs no guard of
        its own — but the *previous* order is recorded first, because undo has to put the
        board back and a fractional key cannot be inverted.
        """
        task_id = _task_id_of(steps, run)
        task = await self._tasks.resolve(principal, task_id)
        if task.parent_task_id is not None:
            # A subtask's position is inside its parent, and promoting it to the front of
            # the board is not a thing the board can express.
            return {"moved": False, "reason": "subtask"}
        siblings = [
            candidate
            for candidate in await self._task_repository.list_all(run.project_id)
            if candidate.parent_task_id is None and candidate.state in WORKABLE_STATES
        ]
        first = min(siblings, key=lambda candidate: candidate.order, default=None)
        if first is None or first.id == task.id:
            return {"moved": False, "reason": "already first"}
        project = await self._projects.get(run.project_id)
        await self._record_changes(
            run,
            [RunChange(kind="task_reordered", task_id=task.id, previous_order=task.order)],
            previous_next_up_task_id=project.next_up_task_id if project else None,
        )
        await self._tasks.reorder(principal, task.id, before_task_id=first.id)
        await self._board_updates.publish(
            run.owner_uid,
            project_id=run.project_id,
            task_ids=[task.id],
            origin="agent",
            run_id=run.id,
        )
        return {"moved": True, "beforeTaskId": first.id}

    # --- turn plumbing ----------------------------------------------------------------

    async def _ensure_session(
        self, principal: Principal, run: AutonomousRun, task_id: str
    ) -> str:
        """This run's dedicated session, creating it on first use.

        Both `_research` and `_propose_tasks` land their turns in the same session — one
        run, one session — but `_research` may be skipped (`needsResearch: false`)
        without ever creating one, so `_propose_tasks` cannot assume `run.session_id` is
        set even though it runs later in the sequence. Created once and patched onto the
        ledger the same way `turnId` is, so a resumed run (a retried step, after a crash)
        reuses it rather than creating a second session for one run.
        """
        if run.session_id is not None:
            return run.session_id
        session = await self._sessions.create_research_session(
            principal, project_id=run.project_id, task_id=task_id, run_id=run.id
        )
        await self._runs.patch(run.id, {"sessionId": session.id})
        return session.id

    async def _run_turn(
        self,
        principal: Principal,
        session_id: str,
        *,
        text: str,
        agent: AgentChoice,
        run_id: str,
        context_attachments: list[dict[str, str]] | None = None,
    ) -> Turn:
        """Start a turn and wait for it, without ever awaiting the generation task.

        The event is set by `TurnService.start`'s `on_finished` done callback. Waiting on
        the task itself would put generation in this request's scope, and a Cloud Tasks
        deadline would then cancel inference the run has already paid for — which is the
        exact failure the disconnect guarantee exists to prevent, arriving through a door
        nobody was watching.
        """
        finished = asyncio.Event()

        async def signal() -> None:
            finished.set()

        turn = await self._turns.start(
            principal,
            session_id,
            text=text,
            context_attachments=context_attachments,
            agent=agent,
            state_delta={RUN_ID_KEY: run_id},
            on_finished=signal,
        )
        await finished.wait()
        return await self._turns.get(principal, turn.id)

    async def _renew_lease(self, project_id: str, run_id: str) -> None:
        """Hold the lease for as long as the run is working.

        A run that loses its lease — its instance stalled past the TTL and somebody else
        took over — is logged and left alone. Stopping here would race the new holder for
        the same writes, and the `finally` in `execute` already declines to delete a lease
        it no longer owns.
        """
        while True:
            await asyncio.sleep(LEASE_RENEW_SECONDS)
            if not await self._runs.renew_lease(project_id, run_id):
                logger.warning("lease lost mid-run", extra={"run_id": run_id})
                return

    # --- ledger writes ----------------------------------------------------------------

    async def _mark(
        self,
        run: AutonomousRun,
        step_id: str,
        status: StepStatus,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        output: object = None,
        error: str | None = None,
    ) -> None:
        """Write one step's status, re-reading the row first.

        The whole `steps` array is rewritten because Firestore cannot update an array
        element in place. Re-reading rather than patching a captured copy is what keeps a
        step boundary honest across the minutes a generation step can take.
        """
        current = await self._runs.get(run.id)
        if current is None:  # pragma: no cover - the row was read moments ago
            return
        steps = []
        for step in current.steps:
            if step.id != step_id:
                steps.append(step)
                continue
            steps.append(
                step.model_copy(
                    update={
                        "status": status,
                        "started_at": started_at or step.started_at,
                        "ended_at": ended_at or step.ended_at,
                        "output": output if isinstance(output, dict) else step.output,
                        "error": error,
                    }
                )
            )
        await self._runs.patch(run.id, {"steps": [step.to_document() for step in steps]})

    async def _record_changes(
        self,
        run: AutonomousRun,
        changes: list[RunChange],
        *,
        previous_next_up_task_id: str | None = None,
    ) -> None:
        """Append to the ledger's undo record, *before* making the write it describes.

        Before, not after: a crash between the write and its record leaves a change undo
        cannot reverse, while a crash the other way leaves a record of something that never
        happened — and undo is written to tolerate the second (a task it cannot find is a
        task already gone).
        """
        current = await self._runs.get(run.id)
        if current is None:  # pragma: no cover
            return
        existing = {(change.kind, change.task_id) for change in current.changes}
        merged = [*current.changes]
        merged.extend(
            change for change in changes if (change.kind, change.task_id) not in existing
        )
        patch: dict[str, object] = {"changes": [change.to_document() for change in merged]}
        if previous_next_up_task_id is not None and current.previous_next_up_task_id is None:
            patch["previousNextUpTaskId"] = previous_next_up_task_id
        await self._runs.patch(run.id, patch)

    async def _complete(self, run: AutonomousRun) -> None:
        await self._runs.patch(run.id, {"status": RunStatus.COMPLETE.value, "error": None})
        # The cooldown's clock starts when the work finished, not when it was scheduled.
        await self._projects.patch(run.project_id, {"lastAutonomousRunAt": now()})
        logger.info("run complete", extra={"run_id": run.id, "project_id": run.project_id})

    async def _skip_remaining(self, run: AutonomousRun, *, from_step: str) -> None:
        """Mark this step and everything after it `skipped`.

        `cursor` is "the first non-complete step", so a run that stopped early with its
        remaining steps still `pending` would be picked up by the recovery pass forever —
        a row that says it has work left, on a project that has none.
        """
        current = await self._runs.get(run.id)
        if current is None:  # pragma: no cover
            return
        reached = False
        steps = []
        for step in current.steps:
            reached = reached or step.id == from_step
            steps.append(
                step.model_copy(update={"status": StepStatus.SKIPPED, "ended_at": now()})
                if reached and step.status is not StepStatus.COMPLETE
                else step
            )
        await self._runs.patch(run.id, {"steps": [step.to_document() for step in steps]})

    async def _fail(self, principal: Principal, run: AutonomousRun, detail: str) -> None:
        """Mark the run failed and leave the task where the UI can offer a retry.

        The task matters as much as the ledger row: a run that died with `researchStatus`
        still `in_progress` leaves a task that invariant 6 will never let complete, waiting
        on a run that no longer exists. `failed` is the state
        docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid renders "your coach
        couldn't prepare this — try again" against.
        """
        await self._runs.patch(run.id, {"status": RunStatus.FAILED.value, "error": detail})
        await self._projects.patch(run.project_id, {"lastAutonomousRunAt": now()})
        task_id = _selected_task_id(run)
        if task_id is None:
            return
        with contextlib.suppress(Exception):
            task = await self._tasks.resolve(principal, task_id)
            if task.research_status in {ResearchStatus.IN_PROGRESS, ResearchStatus.PENDING}:
                await self._tasks.set_research(principal, task.id, status=ResearchStatus.FAILED)

    async def _reload(self, run: AutonomousRun) -> AutonomousRun:
        return await self._runs.get(run.id) or run

    async def _owner_present(self, run: AutonomousRun) -> bool:
        presence = await self._presence.get(run.owner_uid)
        if presence is None or presence.last_heartbeat_at is None:
            return False
        if presence.active_project_id != run.project_id:
            return False
        return now() - presence.last_heartbeat_at < PRESENCE_WINDOW


#: Sentinel for a step that decided it had nothing to do — `research` on a task with
#: `needsResearch: false`. Distinct from `None`, which is a step whose output is empty.
_SKIPPED = object()


class StepFailed(RuntimeError):
    """A step that failed for a reason worth retrying."""


class NothingToDo(RuntimeError):
    """The run has no work. Not a failure — see the step loop."""


def _skip_research(task: Task, run: AutonomousRun) -> bool:
    """`task.needsResearch == false` skips research — unless the run exists *because*
    this task was requested, which `select_next_task` resolves unconditionally
    (docs/03-agent-design.md's step-1 row: "has to research that thing"). Manual research
    (`services/research.py`) never consults `needsResearch` at all; a requested queued run
    is meant to behave the same way.
    """
    return not task.needs_research and run.trigger != "requested"


def _selected_task_id(run: AutonomousRun) -> str | None:
    """The task this run settled on, from the ledger. `None` if it never got that far."""
    for step in run.steps:
        if step.id == "select_next_task" and step.output:
            chosen = step.output.get("taskId")
            if chosen:
                return str(chosen)
    return run.task_id


def _task_id_of(steps: dict[str, RunStep], run: AutonomousRun) -> str:
    """The task `select_next_task` chose, from the ledger rather than from memory.

    Reading it back out of the recorded step output is what makes resume work: a delivery
    that picks the run up at `research` never ran step 1 in this process, and the id it
    needs is the one the *earlier* process committed.
    """
    output = (steps.get("select_next_task") or RunStep(id="select_next_task")).output or {}
    task_id = output.get("taskId") or run.task_id
    if not task_id:
        raise StepFailed("this run has no task to work on")
    return str(task_id)


def _research_message(task: Task) -> str:
    """The message that opens the research turn.

    A real user-authored message in the task's transcript, as the manual path's is, and
    for the same reason: materials that appear from nowhere read as the coach acting
    unprompted (docs/10-risks.md#r6). It says the run was a background one, because that
    is a true and useful thing for the learner to find in their transcript later.
    """
    lines = [
        f"While you were away, prepare the materials for this task: {task.title}.",
    ]
    if task.description:
        lines.append(f"What done looks like: {task.description}")
    return "\n".join(lines)


def _propose_message() -> str:
    """The `propose_tasks` prompt.

    Deliberately narrow. This step exists for prerequisites the research turned up — "this
    tutorial assumes generics" — and not as a second chance to redesign the board, which is
    why it names a bound the tool layer also enforces.
    """
    return (
        "You have just finished preparing materials for this task. If that research "
        "revealed work the board is missing — a prerequisite the material assumes, or a "
        "step this task turns out to need first — add it now with add_task or add_subtask, "
        "at most two items, and say in one line why. If the board already covers "
        "everything, say so and add nothing."
    )


__all__ = [
    "LEASE_RENEW_SECONDS",
    "STEP_TIMEOUT_SECONDS",
    "NothingToDo",
    "RunExecutor",
    "StepFailed",
]
