"""Task use cases: the state machine, ordering, and rollups, all transactional.

This is the module docs/01-architecture.md means when it says a tool that wants to
complete a task calls the same `TaskService.complete_task()` the user's button calls.
Everything here takes a `Principal`, and the invariants in
docs/02-data-model.md#task-state-machine are enforced inside Firestore transactions
rather than by convention.

One shape recurs throughout, and it is dictated by Firestore's rule that **every read in
a transaction must precede every write**:

1. read the project document and *all* of its tasks,
2. apply the change to that in-memory list,
3. recompute the derived numbers from the result,
4. write.

Reading the whole board per write is O(tasks in project) and deliberate — it is a single
query, the rollup and counts recomputation needs the list anyway, and it makes every
invariant a property of one consistent snapshot instead of a sequence of point reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from google.cloud.firestore import AsyncTransaction, async_transactional

from coach.core.clock import now
from coach.core.errors import Conflict, NotFound, ValidationProblem
from coach.core.ids import task_id as new_task_id
from coach.core.principal import Principal
from coach.repositories.firestore import Database
from coach.repositories.projects import ProjectRepository
from coach.repositories.tasks import TaskRepository
from coach.services.models import (
    Origin,
    Project,
    Rollup,
    Task,
    TaskState,
    TaskWithSubtasks,
)
from coach.services.ordering import OrderKeyError, key_between, rebalance
from coach.services.projects import ProjectService
from coach.services.rollups import compute_counts, compute_next_up, compute_rollup
from coach.services.state_machine import validate_transition

#: docs/03-agent-design.md guards `split_task` to 2-8 subtasks. The same bound applies to
#: the manual endpoint, so a hand split and an agent split cannot produce different shapes.
MIN_SPLIT_SUBTASKS = 2
MAX_SPLIT_SUBTASKS = 8

#: Placeholder key used by `plan_insert` for the not-yet-created task.
NEW_TASK_SLOT = ""


def _sorted_siblings(tasks: list[Task], parent_task_id: str | None) -> list[Task]:
    return sorted(
        (t for t in tasks if t.parent_task_id == parent_task_id), key=lambda t: t.order
    )


def _apply(tasks: list[Task], task_id: str, updates: dict[str, Any]) -> Task:
    """Apply a wire-shaped (camelCase) patch to the in-memory task list.

    Re-validates through the model rather than using `model_copy`, so that a patch
    carrying `{"state": "completed"}` produces a real `TaskState` and not a bare string —
    otherwise every downstream `is TaskState.COMPLETED` check would quietly be false.
    """
    for index, task in enumerate(tasks):
        if task.id != task_id:
            continue
        updated = Task.model_validate({**task.to_document(), **updates})
        tasks[index] = updated
        return updated
    raise NotFound(f"No task {task_id!r}.")


def plan_insert(siblings: list[Task], after_task_id: str | None) -> dict[str, str]:
    """Order keys to write when inserting one new task among `siblings`.

    Returns a map of task id to new `order`. Normally it has a single entry, keyed by
    `NEW_TASK_SLOT`, holding the newcomer's key. When the key space cannot produce a
    midpoint — adjacent keys exhausted, or two siblings sharing an `order` after some
    earlier bug — it instead returns a full re-key of the sibling list with the newcomer
    in place: the "rebalances the whole project in one batch" path from
    docs/02-data-model.md#ordering.
    """
    index = len(siblings)
    if after_task_id is not None:
        matches = [i for i, t in enumerate(siblings) if t.id == after_task_id]
        if not matches:
            raise ValidationProblem(
                f"afterTaskId {after_task_id!r} is not a sibling of the new task."
            )
        index = matches[0] + 1

    previous = siblings[index - 1].order if index > 0 else None
    following = siblings[index].order if index < len(siblings) else None
    try:
        return {NEW_TASK_SLOT: key_between(previous, following)}
    except OrderKeyError:
        keys = rebalance(len(siblings) + 1)
        planned = {t.id: keys[i] for i, t in enumerate(siblings[:index])}
        planned[NEW_TASK_SLOT] = keys[index]
        planned.update({t.id: keys[i + index + 1] for i, t in enumerate(siblings[index:])})
        return planned


def plan_move(
    siblings: list[Task],
    moving_task_id: str,
    *,
    after_task_id: str | None,
    before_task_id: str | None,
) -> dict[str, str]:
    """Order keys to write when moving an existing task within `siblings`.

    `apps/web/src/lib/ordering.ts` mirrors this so the board's optimistic drag-and-drop
    computes the same key the server will (docs/08-testing.md).
    """
    if (after_task_id is None) == (before_task_id is None):
        raise ValidationProblem("Provide exactly one of afterTaskId or beforeTaskId.")

    anchor_id = after_task_id if after_task_id is not None else before_task_id
    if anchor_id == moving_task_id:
        raise ValidationProblem("A task cannot be positioned relative to itself.")

    others = [t for t in siblings if t.id != moving_task_id]
    matches = [i for i, t in enumerate(others) if t.id == anchor_id]
    if not matches:
        raise ValidationProblem(f"{anchor_id!r} is not a sibling of the task being reordered.")
    index = matches[0] + 1 if after_task_id is not None else matches[0]

    previous = others[index - 1].order if index > 0 else None
    following = others[index].order if index < len(others) else None
    try:
        return {moving_task_id: key_between(previous, following)}
    except OrderKeyError:
        moving = next(t for t in siblings if t.id == moving_task_id)
        final = [*others[:index], moving, *others[index:]]
        keys = rebalance(len(final))
        return {task.id: keys[i] for i, task in enumerate(final)}


class TaskService:
    def __init__(
        self,
        db: Database,
        tasks: TaskRepository,
        projects: ProjectRepository,
        project_service: ProjectService,
    ) -> None:
        self._db = db
        self._tasks = tasks
        self._projects = projects
        self._project_service = project_service

    # --- reads ---------------------------------------------------------------------

    async def resolve(self, principal: Principal, task_id: str) -> Task:
        """Find a task by bare id and assert the caller owns it.

        Ownership is checked against the task's own denormalized `ownerUid` rather than
        by loading the project, so the hot path stays one query. A task owned by someone
        else raises `NotFound`, not `Forbidden`, so ids cannot be probed for existence.
        """
        task = await self._tasks.find_by_id(task_id)
        if task is None or not principal.owns(task.owner_uid):
            raise NotFound(f"No task {task_id!r}.")
        return task

    async def list_board(
        self,
        principal: Principal,
        project_id: str,
        *,
        include_completed: bool = False,
        include_discarded: bool = False,
        include_postponed: bool = True,
    ) -> list[TaskWithSubtasks]:
        """`GET /api/projects/{id}/tasks` — parents with nested `subtasks[]` and `rollup`.

        Filtering happens after nesting, so a hidden parent stays on the board while it
        still has visible children — otherwise completing a parent would take its
        unfinished subtasks off the board with it.
        """
        await self._project_service.require_owned(principal, project_id)
        tasks = await self._tasks.list_all(project_id)

        hidden: set[TaskState] = set()
        if not include_completed:
            hidden.add(TaskState.COMPLETED)
        if not include_discarded:
            hidden.add(TaskState.DISCARDED)
        if not include_postponed:
            hidden |= {TaskState.POSTPONED, TaskState.POSTPONED_UNTIL}

        board: list[TaskWithSubtasks] = []
        for parent in _sorted_siblings(tasks, None):
            children = [
                child
                for child in _sorted_siblings(tasks, parent.id)
                if child.state not in hidden
            ]
            if parent.state in hidden and not children:
                continue
            board.append(TaskWithSubtasks(**parent.model_dump(), subtasks=children))
        return board

    async def get_with_subtasks(self, principal: Principal, task_id: str) -> TaskWithSubtasks:
        """`GET /api/tasks/{id}` — the task plus its subtasks.

        The `latestReport` the API contract also returns arrives at M4 with the research
        report collection; `latestReportId` is already on the task for it to hang off.
        """
        task = await self.resolve(principal, task_id)
        children = (
            []
            if task.parent_task_id is not None
            else _sorted_siblings(await self._tasks.list_all(task.project_id), task.id)
        )
        return TaskWithSubtasks(**task.model_dump(), subtasks=children)

    async def mutation_context(self, task: Task) -> tuple[Task | None, Project]:
        """The affected parent and the project, re-read after a mutation.

        docs/04-api-contract.md wants a mutation to return enough for the client to
        reconcile optimistically without a refetch: the task, its parent (whose `rollup`
        just moved), and the project (whose `counts` and `nextUpTaskId` just moved).
        """
        parent = (
            await self._tasks.get(task.project_id, task.parent_task_id)
            if task.parent_task_id is not None
            else None
        )
        project = await self._projects.get(task.project_id)
        if project is None:  # pragma: no cover - ownership was asserted upstream
            raise NotFound(f"No project {task.project_id!r}.")
        return parent, project

    # --- writes --------------------------------------------------------------------

    async def create_task(
        self,
        principal: Principal,
        project_id: str,
        *,
        title: str,
        description: str = "",
        estimated_minutes: int = 45,
        parent_task_id: str | None = None,
        after_task_id: str | None = None,
        needs_research: bool = True,
        origin: Origin = Origin.USER,
    ) -> Task:
        await self._project_service.require_owned(principal, project_id)
        # Generated outside the transaction so that a Firestore-driven retry of the
        # closure below reuses the same id rather than minting a second one.
        created_id = new_task_id()

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> Task:
            project, tasks = await self._read_board(transaction, project_id)

            if parent_task_id is not None:
                parent = next((t for t in tasks if t.id == parent_task_id), None)
                if parent is None:
                    raise NotFound(f"No task {parent_task_id!r} in this project.")
                if parent.parent_task_id is not None:
                    raise ValidationProblem(
                        "Task nesting is one level deep: a subtask cannot have subtasks. "
                        "Split it into siblings instead."
                    )

            planned = plan_insert(_sorted_siblings(tasks, parent_task_id), after_task_id)
            task = Task(
                id=created_id,
                project_id=project_id,
                owner_uid=principal.uid,
                parent_task_id=parent_task_id,
                title=title,
                description=description,
                estimated_minutes=estimated_minutes,
                order=planned.pop(NEW_TASK_SLOT),
                needs_research=needs_research,
                origin=origin,
            )
            created = await self._tasks.create(task, transaction=transaction)
            tasks.append(created)

            for sibling_id, order in planned.items():  # only set on the rebalance path
                await self._tasks.patch(
                    project_id, sibling_id, {"order": order}, transaction=transaction
                )
                _apply(tasks, sibling_id, {"order": order})

            await self._write_derived(transaction, project, tasks)
            return created

        return await self._db.run(txn)

    async def update_task(
        self,
        principal: Principal,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        estimated_minutes: int | None = None,
        needs_research: bool | None = None,
    ) -> Task:
        """`PATCH /api/tasks/{id}`.

        An `estimatedMinutes` change recomputes the parent rollup.
        """
        task = await self.resolve(principal, task_id)
        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if description is not None:
            updates["description"] = description
        if estimated_minutes is not None:
            updates["estimatedMinutes"] = estimated_minutes
        if needs_research is not None:
            updates["needsResearch"] = needs_research
        if not updates:
            return task

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> Task:
            project, tasks = await self._read_board(transaction, task.project_id)
            if not any(t.id == task_id for t in tasks):
                raise NotFound(f"No task {task_id!r}.")
            await self._tasks.patch(task.project_id, task_id, updates, transaction=transaction)
            updated = _apply(tasks, task_id, updates)
            await self._write_derived(transaction, project, tasks)
            return updated

        return await self._db.run(txn)

    async def set_state(
        self,
        principal: Principal,
        task_id: str,
        state: TaskState,
        *,
        postponed_until: datetime | None = None,
    ) -> Task:
        """`POST /api/tasks/{id}/state`, validated against the state machine.

        Invariant 1 lives here: promoting a task to `current` demotes whatever was
        `current` to `not_started`, and `project.nextUpTaskId` moves, in one transaction.
        Two simultaneous promotions therefore leave exactly one `current` task — Firestore
        aborts and retries the loser, which then observes the winner and demotes it.

        Invariant 4 is the *absence* of a check: completing a task with unfinished
        subtasks is allowed. The confirmation ("3 subtasks not done — complete anyway?")
        is a UI affordance, not a server rule.
        """
        task = await self.resolve(principal, task_id)
        project_id = task.project_id

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> Task:
            project, tasks = await self._read_board(transaction, project_id)
            current = next((t for t in tasks if t.id == task_id), None)
            if current is None:
                raise NotFound(f"No task {task_id!r}.")

            if current.state is state and state is not TaskState.CURRENT:
                # Re-issuing the same state is a no-op rather than a 409, so a
                # double-clicked row action cannot fail. `current` is excluded because
                # re-promoting is how the board repairs a duplicated `current`.
                return current

            validate_transition(current.state, state, postponed_until=postponed_until)
            if state is TaskState.POSTPONED_UNTIL and (
                postponed_until is None or postponed_until <= now()
            ):
                raise ValidationProblem(
                    "postponedUntil must be in the future; a past timestamp would be "
                    "swept straight back to 'not_started'."
                )

            updates: dict[str, Any] = {
                "state": state.value,
                "postponedUntil": postponed_until,
                "completedAt": now() if state is TaskState.COMPLETED else None,
            }

            # Invariant 1: at most one `current` task per project.
            if state is TaskState.CURRENT:
                for other in list(tasks):
                    if other.id != task_id and other.state is TaskState.CURRENT:
                        demotion = {"state": TaskState.NOT_STARTED.value}
                        await self._tasks.patch(
                            project_id, other.id, demotion, transaction=transaction
                        )
                        _apply(tasks, other.id, demotion)

            await self._tasks.patch(project_id, task_id, updates, transaction=transaction)
            updated = _apply(tasks, task_id, updates)
            await self._write_derived(transaction, project, tasks)
            return updated

        return await self._db.run(txn)

    async def reorder(
        self,
        principal: Principal,
        task_id: str,
        *,
        after_task_id: str | None = None,
        before_task_id: str | None = None,
    ) -> Task:
        """`POST /api/tasks/{id}/reorder`.

        Normally a single-document write of one fractional index. `plan_move` falls back
        to re-keying the whole sibling list when the key space is exhausted; that write
        is inside this transaction, so the board is never observed half-rebalanced.
        """
        task = await self.resolve(principal, task_id)
        project_id = task.project_id

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> Task:
            project, tasks = await self._read_board(transaction, project_id)
            moving = next((t for t in tasks if t.id == task_id), None)
            if moving is None:
                raise NotFound(f"No task {task_id!r}.")

            planned = plan_move(
                _sorted_siblings(tasks, moving.parent_task_id),
                task_id,
                after_task_id=after_task_id,
                before_task_id=before_task_id,
            )
            for moved_id, order in planned.items():
                await self._tasks.patch(
                    project_id, moved_id, {"order": order}, transaction=transaction
                )
                _apply(tasks, moved_id, {"order": order})

            await self._write_derived(transaction, project, tasks)
            return next(t for t in tasks if t.id == task_id)

        return await self._db.run(txn)

    async def split_task(
        self,
        principal: Principal,
        task_id: str,
        subtasks: list[dict[str, Any]],
        *,
        origin: Origin = Origin.USER,
    ) -> TaskWithSubtasks:
        """`POST /api/tasks/{id}/split` — turn a leaf task into a parent with subtasks."""
        if not MIN_SPLIT_SUBTASKS <= len(subtasks) <= MAX_SPLIT_SUBTASKS:
            raise ValidationProblem(
                f"A split produces between {MIN_SPLIT_SUBTASKS} and "
                f"{MAX_SPLIT_SUBTASKS} subtasks; got {len(subtasks)}."
            )
        parent = await self.resolve(principal, task_id)
        if parent.parent_task_id is not None:
            raise ValidationProblem(
                "Task nesting is one level deep: split a subtask into siblings instead."
            )
        project_id = parent.project_id
        new_ids = [new_task_id() for _ in subtasks]

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> TaskWithSubtasks:
            project, tasks = await self._read_board(transaction, project_id)
            if not any(t.id == task_id for t in tasks):
                raise NotFound(f"No task {task_id!r}.")
            if any(t.parent_task_id == task_id for t in tasks):
                raise Conflict(
                    "This task has already been split. Add subtasks individually instead."
                )

            orders = rebalance(len(subtasks))
            created: list[Task] = []
            for child_id, order, draft in zip(new_ids, orders, subtasks, strict=True):
                child = Task(
                    id=child_id,
                    project_id=project_id,
                    owner_uid=principal.uid,
                    parent_task_id=task_id,
                    title=draft["title"],
                    description=draft.get("description", ""),
                    estimated_minutes=draft["estimatedMinutes"],
                    order=order,
                    needs_research=draft.get("needsResearch", True),
                    origin=origin,
                )
                created.append(await self._tasks.create(child, transaction=transaction))

            tasks.extend(created)
            await self._write_derived(transaction, project, tasks)
            updated_parent = next(t for t in tasks if t.id == task_id)
            return TaskWithSubtasks(
                **updated_parent.model_dump(),
                subtasks=sorted(created, key=lambda t: t.order),
            )

        return await self._db.run(txn)

    # --- transaction helpers -------------------------------------------------------

    async def _read_board(
        self, transaction: AsyncTransaction, project_id: str
    ) -> tuple[Project, list[Task]]:
        """Every read a mutation needs, done up front.

        Firestore requires all transactional reads to precede all transactional writes,
        so this is called first in every transaction and nothing reads afterwards.
        """
        project = await self._projects.get(project_id, transaction=transaction)
        if project is None:
            raise NotFound(f"No project {project_id!r}.")
        tasks = await self._tasks.list_all(project_id, transaction=transaction)
        return project, tasks

    async def _write_derived(
        self, transaction: AsyncTransaction, project: Project, tasks: list[Task]
    ) -> None:
        """Recompute rollups, project counts, and `nextUpTaskId` from the post-change board.

        Called by every mutation, inside the same transaction, so invariant 5 holds
        whatever the entry point was. Only documents whose derived values actually
        changed are written.
        """
        by_parent: dict[str, list[Task]] = {}
        for task in tasks:
            if task.parent_task_id is not None:
                by_parent.setdefault(task.parent_task_id, []).append(task)

        for task in list(tasks):
            children = by_parent.get(task.id)
            desired: Rollup | None = compute_rollup(children) if children else None
            if desired == task.rollup:
                continue
            document = desired.to_document() if desired is not None else None
            await self._tasks.patch(
                project.id, task.id, {"rollup": document}, transaction=transaction
            )
            _apply(tasks, task.id, {"rollup": document})

        counts = compute_counts(tasks)
        next_up = compute_next_up(tasks, at=now())
        updates: dict[str, Any] = {}
        if counts != project.counts:
            updates["counts"] = counts.to_document()
        if next_up != project.next_up_task_id:
            updates["nextUpTaskId"] = next_up
        if updates:
            await self._projects.patch(project.id, updates, transaction=transaction)


__all__ = [
    "MAX_SPLIT_SUBTASKS",
    "MIN_SPLIT_SUBTASKS",
    "TaskService",
    "plan_insert",
    "plan_move",
]
