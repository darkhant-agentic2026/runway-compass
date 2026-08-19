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

import asyncio
from datetime import datetime
from typing import Any

from google.cloud.firestore import AsyncTransaction, async_transactional

from coach.core.clock import now
from coach.core.errors import Conflict, NotFound, ValidationProblem
from coach.core.ids import item_id as new_item_id
from coach.core.ids import task_id as new_task_id
from coach.core.principal import Principal
from coach.repositories.firestore import Database
from coach.repositories.projects import ProjectRepository
from coach.repositories.tasks import TaskRepository
from coach.services.models import (
    Origin,
    Project,
    ResearchStatus,
    Rollup,
    Task,
    TaskItem,
    TaskState,
    TaskWithSubtasks,
)
from coach.services.ordering import OrderKeyError, key_between, rebalance
from coach.services.projects import ProjectService
from coach.services.rollups import (
    compute_counts,
    compute_next_up,
    compute_rollup,
    derive_state,
)
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
        self._project_locks: dict[str, asyncio.Lock] = {}

    def _project_lock(self, project_id: str) -> asyncio.Lock:
        """Serialize this instance's writes to one project.

        Every mutation reads the project's whole board and writes the parent rollup and
        the project counts, so two concurrent writes to the same project always contend
        on the same one or two documents — that is what invariant 5 in
        docs/02-data-model.md asks for, not an accident of implementation. Firestore
        resolves that contention by aborting and retrying, which is correct but expensive,
        and under enough concurrency the retry budget runs out and a perfectly valid write
        surfaces as a 500.

        Queueing them locally costs a few milliseconds and removes the collision
        altogether. This is the same move ADK's shipped `FirestoreSessionService` makes
        for the identical problem — a per-`(app, user, session)` lock around
        `append_event`, so "concurrent appends within one instance queue rather than
        collide" (docs/03-agent-design.md).

        It is an optimization, not the guarantee. The transaction remains the thing that
        makes the invariants true, because a second Cloud Run instance has its own locks
        and knows nothing of this one's — which `tests/test_transactions.py` exercises by
        driving two service instances at once.
        """
        # Bounded in practice by the number of distinct projects an instance touches
        # before it is recycled; a lock is a few dozen bytes.
        lock = self._project_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._project_locks[project_id] = lock
        return lock

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

        async with self._project_lock(project_id):
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
        project_id = task.project_id

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> Task:
            project, tasks = await self._read_board(transaction, project_id)
            if not any(t.id == task_id for t in tasks):
                raise NotFound(f"No task {task_id!r}.")
            await self._tasks.patch(project_id, task_id, updates, transaction=transaction)
            updated = _apply(tasks, task_id, updates)
            await self._write_derived(transaction, project, tasks)
            return updated

        async with self._project_lock(project_id):
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

        **Starting a task demotes nothing.** Until M4 this method enforced "at most one
        `current` task per project" by moving the previous one back to `not_started`;
        `in_progress` is not singular, so the demotion is gone and `project.nextUpTaskId`
        is derived from the board instead (`compute_next_up`). Two simultaneous starts now
        leave both tasks `in_progress`, and what the transaction still buys is that the
        contended write to `projects/{id}` retries rather than losing an update.

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

            if current.state is state:
                # Re-issuing the same state is a no-op rather than a 409, so a
                # double-clicked row action cannot fail.
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

            await self._tasks.patch(project_id, task_id, updates, transaction=transaction)
            updated = _apply(tasks, task_id, updates)
            await self._write_derived(transaction, project, tasks, state_set_explicitly=task_id)
            return updated

        async with self._project_lock(project_id):
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

        async with self._project_lock(project_id):
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
            # `items` and `rollup` are mutually exclusive by construction
            # (docs/02-data-model.md#task-items), and this is the construction: splitting is
            # the only way a leaf becomes a parent. Refusing here rather than silently
            # dropping the checklist, because the learner would lose a plan — and possibly
            # work they have already ticked off — to a reshape they did not ask for.
            parent_now = next(t for t in tasks if t.id == task_id)
            if parent_now.items:
                raise Conflict(
                    "This task already has a checklist, and a task's plan is either its "
                    "items or its subtasks. Clear the items first, or add the subtasks to "
                    "a different task."
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

        async with self._project_lock(project_id):
            return await self._db.run(txn)

    async def set_research(
        self,
        principal: Principal,
        task_id: str,
        *,
        status: ResearchStatus,
        latest_report_id: str | None = None,
    ) -> Task:
        """Move `researchStatus`, and optionally the `latestReportId` pointer.

        Goes through the board transaction rather than being a one-field patch, because
        `researchStatus` is half of invariant 6: a task whose checklist is fully ticked
        completes the moment research stops being outstanding, so the write that sets
        `done` is exactly the write that can complete a task. A bare `patch` would leave
        that task sitting finished-but-open until something unrelated touched it.
        """
        task = await self.resolve(principal, task_id)
        project_id = task.project_id
        updates: dict[str, Any] = {"researchStatus": status.value}
        if latest_report_id is not None:
            updates["latestReportId"] = latest_report_id

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> Task:
            project, tasks = await self._read_board(transaction, project_id)
            if not any(t.id == task_id for t in tasks):
                raise NotFound(f"No task {task_id!r}.")
            await self._tasks.patch(project_id, task_id, updates, transaction=transaction)
            _apply(tasks, task_id, updates)
            await self._write_derived(transaction, project, tasks)
            return next(t for t in tasks if t.id == task_id)

        async with self._project_lock(project_id):
            return await self._db.run(txn)

    # --- task items ----------------------------------------------------------------

    async def add_items(
        self,
        principal: Principal,
        task_id: str,
        drafts: list[dict[str, Any]],
        *,
        source_report_id: str | None = None,
    ) -> Task:
        """`POST /api/tasks/{id}/items` — append to the checklist, keeping the given order.

        Appending rather than replacing: `replace_items` is the research path, and a
        conversation that turns up an extra step should not discard the checklist to add it.
        """
        if not drafts:
            raise ValidationProblem("No items given to add.")
        items = [_task_item(draft, source_report_id=source_report_id) for draft in drafts]
        return await self._write_items(principal, task_id, lambda existing: [*existing, *items])

    async def replace_items(
        self,
        principal: Principal,
        task_id: str,
        drafts: list[dict[str, Any]],
        *,
        source_report_id: str,
    ) -> Task:
        """Rewrite the checklist from a research run, preserving what the learner has done.

        Two rules, both from docs/02-data-model.md#task-items, and both about not making
        someone redo work because the coach changed its mind:

        - An incoming item that matches one already on the list — same `shortDescription`
          and same `url` — inherits its `completed` flag and its `itemId`. A re-run that
          keeps a reading the learner has already done does not ask for it again.
        - A hand-added item (`sourceReportId is None`) is never dropped. It is the
          learner's own note about their own work, and a research run has no standing to
          delete it. Hand-added items keep their relative order and follow the report's.
        """
        incoming = [_task_item(draft, source_report_id=source_report_id) for draft in drafts]

        def rewrite(existing: list[TaskItem]) -> list[TaskItem]:
            by_identity = {(i.short_description, i.url): i for i in existing}
            merged: list[TaskItem] = []
            for item in incoming:
                previous = by_identity.get((item.short_description, item.url))
                if previous is None:
                    merged.append(item)
                    continue
                merged.append(
                    item.model_copy(
                        update={
                            "item_id": previous.item_id,
                            "completed": previous.completed,
                            "completed_at": previous.completed_at,
                        }
                    )
                )
            kept = {i.item_id for i in merged}
            merged.extend(
                i for i in existing if i.source_report_id is None and i.item_id not in kept
            )
            return merged

        return await self._write_items(principal, task_id, rewrite)

    async def patch_item(
        self,
        principal: Principal,
        task_id: str,
        item_id: str,
        *,
        completed: bool | None = None,
        short_description: str | None = None,
        details: str | None = None,
        guided: bool | None = None,
    ) -> Task:
        """`PATCH /api/tasks/{id}/items/{itemId}` — the checkbox and the inline edit.

        Returns the whole task rather than the item, because a write that completes the
        last item also moves the task's `state` (invariant 6) and the project's `counts`.
        """
        candidates: dict[str, Any] = {
            "completed": completed,
            "short_description": short_description,
            "details": details,
            "guided": guided,
        }
        applied: dict[str, Any] = {k: v for k, v in candidates.items() if v is not None}
        if not applied:
            raise ValidationProblem("No item field given to change.")
        if completed is not None:
            applied["completed_at"] = now() if completed else None

        def rewrite(existing: list[TaskItem]) -> list[TaskItem]:
            if not any(i.item_id == item_id for i in existing):
                raise NotFound(f"No item {item_id!r} on this task.")
            return [
                i.model_copy(update=applied) if i.item_id == item_id else i for i in existing
            ]

        return await self._write_items(principal, task_id, rewrite)

    async def delete_item(self, principal: Principal, task_id: str, item_id: str) -> Task:
        """`DELETE /api/tasks/{id}/items/{itemId}`."""

        def rewrite(existing: list[TaskItem]) -> list[TaskItem]:
            if not any(i.item_id == item_id for i in existing):
                raise NotFound(f"No item {item_id!r} on this task.")
            return [i for i in existing if i.item_id != item_id]

        return await self._write_items(principal, task_id, rewrite)

    async def reorder_item(
        self,
        principal: Principal,
        task_id: str,
        item_id: str,
        *,
        after_item_id: str | None = None,
        before_item_id: str | None = None,
    ) -> Task:
        """`POST /api/tasks/{id}/items/{itemId}/reorder`.

        Items have no fractional index: the array *is* the order, and the whole list is
        rewritten. That is the right trade at this size — a checklist is a handful of
        entries read and written as one document field, where tasks are a collection whose
        reordering has to avoid renumbering a board.
        """
        if (after_item_id is None) == (before_item_id is None):
            raise ValidationProblem("Give exactly one of afterItemId and beforeItemId.")

        def rewrite(existing: list[TaskItem]) -> list[TaskItem]:
            moving = next((i for i in existing if i.item_id == item_id), None)
            if moving is None:
                raise NotFound(f"No item {item_id!r} on this task.")
            anchor_id = after_item_id or before_item_id
            rest = [i for i in existing if i.item_id != item_id]
            positions = [i for i, item in enumerate(rest) if item.item_id == anchor_id]
            if not positions:
                raise ValidationProblem(f"No item {anchor_id!r} to place this next to.")
            index = positions[0] + 1 if after_item_id is not None else positions[0]
            return [*rest[:index], moving, *rest[index:]]

        return await self._write_items(principal, task_id, rewrite)

    async def _write_items(
        self,
        principal: Principal,
        task_id: str,
        rewrite: Any,
    ) -> Task:
        """Apply `rewrite` to a leaf task's checklist, transactionally.

        Every item path funnels through here so that the refusal on a parent, the derived
        state (invariant 6), and the project's `counts` are decided in one place rather
        than five. `rewrite` runs *inside* the transaction, against the list as the
        transaction read it, so a concurrent tick and edit cannot lose each other.
        """
        task = await self.resolve(principal, task_id)
        project_id = task.project_id

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> Task:
            project, tasks = await self._read_board(transaction, project_id)
            current = next((t for t in tasks if t.id == task_id), None)
            if current is None:
                raise NotFound(f"No task {task_id!r}.")
            if any(t.parent_task_id == task_id for t in tasks):
                raise ValidationProblem(
                    "This task has subtasks, and its subtasks are its plan. Add items to "
                    "the subtasks instead."
                )

            items: list[TaskItem] = rewrite(list(current.items))
            document = [item.to_document() for item in items]
            await self._tasks.patch(
                project_id, task_id, {"items": document}, transaction=transaction
            )
            _apply(tasks, task_id, {"items": document})
            await self._write_derived(transaction, project, tasks)
            return next(t for t in tasks if t.id == task_id)

        async with self._project_lock(project_id):
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
        self,
        transaction: AsyncTransaction,
        project: Project,
        tasks: list[Task],
        *,
        state_set_explicitly: str | None = None,
    ) -> None:
        """Recompute derived state, rollups, counts, and `nextUpTaskId` from the board.

        Called by every mutation, inside the same transaction, so invariants 1, 5, and 6
        hold whatever the entry point was. Only documents whose derived values actually
        changed are written.

        **The order of the three passes is load-bearing.** States are derived first,
        because a subtask that auto-completes changes its parent's `rollup` and a task that
        auto-completes changes the project's `counts` — running rollups first would publish
        numbers describing the board as it was a moment ago, and nothing would recompute
        them until the next unrelated write.

        `state_set_explicitly` names a task whose state the caller has just written by hand,
        and exempts it from the derivation *for this transaction only*. Without it, invariant
        6 and the `reopen` action fight: reopening a task whose checklist is fully ticked
        would land on `not_started`, be re-derived to `completed` in the same transaction,
        and the button would do nothing at all — visibly, repeatably, with no error. The
        exemption is per-transaction rather than a stored flag because the learner's next
        checklist write is exactly when the derivation should take over again.
        """

        def children_of(task_id: str) -> list[Task]:
            return [t for t in tasks if t.parent_task_id == task_id]

        for task in list(tasks):
            if task.id == state_set_explicitly:
                continue
            desired_state = derive_state(task, children_of(task.id))
            if desired_state is task.state:
                continue
            transition: dict[str, Any] = {"state": desired_state.value}
            if desired_state is TaskState.COMPLETED:
                transition["completedAt"] = now()
            elif task.state is TaskState.COMPLETED:
                transition["completedAt"] = None
            await self._tasks.patch(project.id, task.id, transition, transaction=transaction)
            _apply(tasks, task.id, transition)

        # Recomputed against the post-derivation list, not the one captured above: a
        # subtask that just auto-completed has to be counted as completed in its parent's
        # rollup, in this same transaction.
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


def _task_item(draft: dict[str, Any], *, source_report_id: str | None) -> TaskItem:
    """One checklist item from a wire-shaped or tool-shaped draft.

    `itemId` is honoured when the caller supplies one — `post_research_report` passes the
    report item's id through so a recommendation and its checklist entry share an identity
    (`coach.core.ids.item_id`) — and minted otherwise.
    """
    short = str(draft.get("shortDescription") or draft.get("short_description") or "").strip()
    if not short:
        raise ValidationProblem("Every checklist item needs a short description.")
    return TaskItem(
        item_id=str(draft.get("itemId") or draft.get("item_id") or "") or new_item_id(),
        short_description=short,
        details=str(draft.get("details") or ""),
        guided=bool(draft.get("guided", False)),
        completed=bool(draft.get("completed", False)),
        minutes=draft.get("minutes"),
        url=draft.get("url") or None,
        source_report_id=source_report_id,
    )


__all__ = [
    "MAX_SPLIT_SUBTASKS",
    "MIN_SPLIT_SUBTASKS",
    "TaskService",
    "plan_insert",
    "plan_move",
]
