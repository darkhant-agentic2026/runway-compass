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

from coach.services.models import ProjectCounts, ResearchStatus, Rollup, Task, TaskState
from coach.services.state_machine import OPEN_STATES, STARTABLE_STATES

#: `researchStatus` values that mean "the checklist is still being written". Half of
#: invariant 6, and the half that is easy to leave out: items alone would complete a task
#: the moment the first thin report's list was ticked off, while a run was still going and
#: about to add five more items to it.
RESEARCH_OUTSTANDING = frozenset({ResearchStatus.PENDING, ResearchStatus.IN_PROGRESS})

#: States `derive_state` is willing to move a task *out of*. Everything else — postponed,
#: discarded — is a deliberate act by the learner, and a derivation that overrode it would
#: bring a discarded task back onto the board because someone ticked a stale checkbox.
_DERIVABLE_FROM = frozenset(
    {
        TaskState.DRAFT,
        TaskState.NOT_STARTED,
        TaskState.IN_PROGRESS,
        TaskState.COMPLETED,
    }
)


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


def derive_state(task: Task, children: list[Task]) -> TaskState:
    """The state this task should be in, given its plan and its checklist.

    Invariants 1 and 6 of docs/02-data-model.md#task-state-machine, as one pure function so
    that `TaskService` can apply them to the post-change board inside the transaction that
    caused the change. Returns `task.state` unchanged when neither applies, which is the
    common case and is what keeps `_write_derived` from writing every task on every mutation.

    The two rules, in the order they are evaluated:

    1. A `draft` task that has acquired a plan — its first items, or its first subtasks —
       becomes `not_started`. Only in that direction: losing every item again does not send
       it back, because by then the learner has seen a plan and a task silently regressing
       is worse than a stale state.
    2. A **leaf** task whose items are all complete, with no research outstanding, becomes
       `completed`; un-ticking an item reopens it as `in_progress`.

    Rule 1 runs first so that a task that gains a fully-ticked checklist in one write lands
    on `completed` rather than stalling in `draft` for a turn.

    Three exclusions are load-bearing, and each is a way this could complete something the
    learner did not finish:

    - **Parents never auto-complete.** A parent's plan is its subtasks, and invariant 4
      already makes completing one with unfinished children a decision the UI puts to the
      learner rather than a rule.
    - **An empty checklist never completes.** No items is the absence of a plan, not a
      finished one — and every task starts that way.
    - **Postponed and discarded tasks are left alone.** See `_DERIVABLE_FROM`.
    """
    if task.state not in _DERIVABLE_FROM:
        return task.state

    state = task.state
    if state is TaskState.DRAFT and (children or task.items):
        state = TaskState.NOT_STARTED

    if children or not task.items:
        return state

    done = all(item.completed for item in task.items)
    if done and task.research_status not in RESEARCH_OUTSTANDING:
        return TaskState.COMPLETED
    if not done and state is TaskState.COMPLETED:
        return TaskState.IN_PROGRESS
    return state


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

    The lowest-`order` top-level task that is `in_progress` — that is what the board pins as
    "Next up" — and otherwise the lowest-`order` one available to start.

    **This became a derivation at M4 rather than a pointer with an invariant behind it.**
    Before, `current` was singular by construction and this function simply found it; the
    first match won because there could only be one. `in_progress` is not singular, so the
    tie has to be broken, and `order` breaks it: the pin follows the board's own sequence
    rather than whichever task happened to be started last.

    Selection is restricted to top-level tasks because `order` is only comparable within a
    parent. Deliberately the same rule as `select_next_task` in
    docs/05-autonomous-runs.md#execution-semantics: skip completed, discarded, postponed,
    and `postponed_until` that has not expired.
    """
    top_level = [t for t in tasks if t.parent_task_id is None]

    in_progress = [t for t in top_level if t.state is TaskState.IN_PROGRESS]
    if in_progress:
        return min(in_progress, key=lambda t: t.order).id

    available = [t for t in top_level if t.state in STARTABLE_STATES]
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
