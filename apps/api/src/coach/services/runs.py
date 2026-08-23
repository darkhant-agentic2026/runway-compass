"""Reading the run ledger, and reversing a run.

docs/04-api-contract.md#runs. Three operations, all owner-scoped:

- `GET /api/runs/{runId}` — one row, for the client attaching to an in-flight run after a
  `409` and for the socket's `subscribe` by `runId`.
- `GET /api/projects/{id}/runs` — the recent rows behind the "Updated by your coach"
  banner.
- `POST /api/runs/{runId}/undo` — put back what the run changed.

**Undo reverses the ledger's record, not a diff.**
docs/05-autonomous-runs.md#what-the-run-is-allowed-to-change says the ledger "records
enough to reverse: created task ids and previous `order`/`nextUpTaskId` values", and it
has to be that way: a diff taken at undo time could not tell
the coach's writes from the learner's own, and the learner has had the board in front of
them since the banner appeared.

**It is idempotent, and deliberately forgiving.** A second call finds `undoneAt` set and
answers with the same task ids rather than trying again. A task the run created but the
learner has since deleted is not an error — it is already in the state undo wants it in.
That tolerance is what lets `_record_changes` write its record *before* the write it
describes, which is the ordering that cannot lose a change.
"""

from __future__ import annotations

import contextlib
import logging

from coach.core.clock import now
from coach.core.errors import NotFound
from coach.core.principal import Principal
from coach.repositories.runs import RunRepository
from coach.services.models import AutonomousRun, TaskState
from coach.services.projects import ProjectService
from coach.services.tasks import TaskService

logger = logging.getLogger(__name__)


class RunService:
    def __init__(
        self,
        runs: RunRepository,
        tasks: TaskService,
        projects: ProjectService,
    ) -> None:
        self._runs = runs
        self._tasks = tasks
        self._projects = projects

    async def get(self, principal: Principal, run_id: str) -> AutonomousRun:
        run = await self._runs.get(run_id)
        if run is None or not principal.owns(run.owner_uid):
            # `NotFound` rather than `Forbidden`, as everywhere: an id must not be probeable
            # for existence by the shape of the refusal.
            raise NotFound(f"No run {run_id!r}.")
        return run

    async def list_for_project(
        self, principal: Principal, project_id: str, *, limit: int = 20
    ) -> list[AutonomousRun]:
        await self._projects.require_owned(principal, project_id)
        runs = await self._runs.list_for_project(project_id, limit)
        return [run for run in runs if principal.owns(run.owner_uid)]

    async def list_for_task(
        self, principal: Principal, task_id: str, *, limit: int = 20
    ) -> list[AutonomousRun]:
        """Recent runs for one task, newest first — the task workspace's research card.

        + M8. Ownership checked through the task, on the same footing
        `list_for_project` checks it through the project.
        """
        await self._tasks.resolve(principal, task_id)
        runs = await self._runs.list_for_task(task_id, limit)
        return [run for run in runs if principal.owns(run.owner_uid)]

    async def undo(self, principal: Principal, run_id: str) -> tuple[AutonomousRun, list[str]]:
        """Reverse the run's board writes. Returns the run and the affected task ids.

        Reversed in the opposite order to the way they were made — reorders first, then
        deletions — so that restoring a task's `order` happens while its neighbours are
        still on the board. Doing it the other way would restore a fractional key relative
        to siblings that had already gone.

        A created task is **discarded**, not deleted from Firestore. Discarding is the
        board's own word for "this is not work I am doing", it is reversible from the UI,
        and it keeps any conversation the learner already had about that task reachable.
        """
        run = await self.get(principal, run_id)
        touched: list[str] = []
        if run.undone_at is not None:
            return run, [change.task_id for change in run.changes]

        for change in run.changes:
            if change.kind != "task_reordered" or change.previous_order is None:
                continue
            with contextlib.suppress(Exception):
                task = await self._tasks.resolve(principal, change.task_id)
                await self._tasks.restore_order(principal, task.id, change.previous_order)
                touched.append(task.id)

        for change in run.changes:
            if change.kind != "task_created":
                continue
            with contextlib.suppress(NotFound):
                task = await self._tasks.resolve(principal, change.task_id)
                if task.state is not TaskState.DISCARDED:
                    await self._tasks.set_state(principal, task.id, TaskState.DISCARDED)
                touched.append(task.id)

        await self._runs.patch(run.id, {"undoneAt": now()})
        logger.info("run undone", extra={"run_id": run.id, "task_ids": touched})
        return await self.get(principal, run_id), touched


__all__ = ["RunService"]
