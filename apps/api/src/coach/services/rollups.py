"""Derived board numbers: parent rollups, project counts, and the next-up pointer.

Pure functions over an in-memory task list, so they are unit-testable without the
emulator and so `TaskService` can apply them to the *post-change* state inside a
transaction before writing anything.

docs/02-data-model.md invariant 5: "Any subtask write recomputes the parent's `rollup` in
the same transaction. Parent cards therefore render counts and summed minutes with no
extra reads."
"""

from __future__ import annotations

from datetime import datetime

from coach.services.models import ProjectCounts, Rollup, Task, TaskState
from coach.services.state_machine import OPEN_STATES


def compute_rollup(children: list[Task]) -> Rollup:
    """The `rollup` for a parent, from its children.

    Discarded subtasks are excluded from all three numbers: a discarded subtask is not
    part of the work, and counting it would make "4 subtasks · 2 h 30 m" disagree with
    what the expanded card shows.
    """
    live = [c for c in children if c.state is not TaskState.DISCARDED]
    return Rollup(
        subtask_count=len(live),
        completed_subtasks=sum(1 for c in live if c.state is TaskState.COMPLETED),
        total_estimated_minutes=sum(c.estimated_minutes for c in live),
    )


def compute_counts(tasks: list[Task]) -> ProjectCounts:
    """`project.counts`, from every task in the project.

    `openMinutes` sums **leaf** tasks only. A parent that has been split carries its own
    estimate *and* its subtasks' estimates; summing both would double-count the same
    work, and the parent's estimate is the stale one once a split has happened.
    """
    live = [t for t in tasks if t.state is not TaskState.DISCARDED]
    parents_with_children = {t.parent_task_id for t in live if t.parent_task_id is not None}
    open_minutes = sum(
        t.estimated_minutes
        for t in live
        if t.state in OPEN_STATES and t.id not in parents_with_children
    )
    return ProjectCounts(
        total=len(live),
        completed=sum(1 for t in live if t.state is TaskState.COMPLETED),
        open_minutes=open_minutes,
    )


def compute_next_up(tasks: list[Task], *, at: datetime) -> str | None:
    """`project.nextUpTaskId`.

    The `current` task if there is one — that is what the board pins as "Next up" — and
    otherwise the lowest-`order` top-level task that is available to start. Selection is
    restricted to top-level tasks because `order` is only comparable within a parent;
    a subtask can still *be* the next-up task by being the `current` one.

    Deliberately the same rule as `select_next_task` in
    docs/05-autonomous-runs.md#execution-semantics: skip completed, discarded, postponed,
    and `postponed_until` that has not expired.
    """
    for task in tasks:
        if task.state is TaskState.CURRENT:
            return task.id

    available = [
        t for t in tasks if t.parent_task_id is None and t.state is TaskState.NOT_STARTED
    ]
    if not available:
        # An expired `postponed_until` is available in principle, but the sweep in
        # /internal/tick is what returns it to `not_started`; anticipating that here
        # would let the pointer name a task the board still shows as postponed.
        return None
    return min(available, key=lambda t: t.order).id


def is_expired_postponement(task: Task, *, at: datetime) -> bool:
    """Whether the nightly sweep should return this task to `not_started`."""
    return (
        task.state is TaskState.POSTPONED_UNTIL
        and task.postponed_until is not None
        and task.postponed_until <= at
    )
