"""Parent rollups and project counts.

docs/08-testing.md:

    Parent `rollup` correctness after: add subtask, edit estimate, complete subtask,
    delete subtask, and a batch of concurrent subtask writes.

"Delete" is `discard` here — nothing in this design hard-deletes a task
(docs/02-data-model.md), so the discarded case is the one that matters.
"""

from __future__ import annotations

import asyncio

import pytest

from coach.core.clock import now
from coach.services.models import Rollup, Task, TaskState
from coach.services.rollups import compute_counts, compute_next_up, compute_rollup

# --- pure functions ------------------------------------------------------------------


def _task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "k_1",
        "project_id": "p_1",
        "owner_uid": "u_1",
        "title": "t",
        "order": "a0",
        "estimated_minutes": 30,
    }
    return Task(**{**base, **overrides})  # type: ignore[arg-type]


def test_rollup_of_no_children_is_empty() -> None:
    assert compute_rollup([]) == Rollup()


def test_rollup_sums_minutes_and_counts_completions() -> None:
    children = [
        _task(id="a", parent_task_id="k_1", estimated_minutes=60),
        _task(id="b", parent_task_id="k_1", estimated_minutes=90, state=TaskState.COMPLETED),
    ]
    assert compute_rollup(children) == Rollup(
        subtask_count=2, completed_subtasks=1, total_estimated_minutes=150
    )


def test_discarded_subtasks_are_excluded_from_the_rollup() -> None:
    """A discarded subtask is not part of the work, so "4 subtasks · 2 h 30 m" must not
    count it — otherwise the card disagrees with the expanded list."""
    children = [
        _task(id="a", parent_task_id="k_1", estimated_minutes=60),
        _task(id="b", parent_task_id="k_1", estimated_minutes=999, state=TaskState.DISCARDED),
    ]
    assert compute_rollup(children) == Rollup(
        subtask_count=1, completed_subtasks=0, total_estimated_minutes=60
    )


def test_counts_sum_open_minutes_over_leaves_only() -> None:
    """A split parent carries a stale estimate alongside its subtasks' estimates.

    Counting both would double-count the same work.
    """
    tasks = [
        _task(id="parent", estimated_minutes=240),
        _task(id="a", parent_task_id="parent", estimated_minutes=60),
        _task(id="b", parent_task_id="parent", estimated_minutes=90),
        _task(id="loner", estimated_minutes=45, order="a1"),
    ]
    counts = compute_counts(tasks)
    assert counts.open_minutes == 60 + 90 + 45
    assert counts.total == 4
    assert counts.completed == 0


def test_counts_ignore_discarded_tasks() -> None:
    tasks = [
        _task(id="a", estimated_minutes=30),
        _task(id="b", estimated_minutes=30, state=TaskState.DISCARDED, order="a1"),
    ]
    counts = compute_counts(tasks)
    assert counts.total == 1
    assert counts.open_minutes == 30


def test_completed_tasks_do_not_count_as_open_minutes() -> None:
    tasks = [
        _task(id="a", estimated_minutes=30, state=TaskState.COMPLETED),
        _task(id="b", estimated_minutes=45, order="a1"),
    ]
    counts = compute_counts(tasks)
    assert counts.completed == 1
    assert counts.open_minutes == 45


def test_next_up_prefers_the_current_task() -> None:
    tasks = [
        _task(id="a", order="a0"),
        _task(id="b", order="a1", state=TaskState.IN_PROGRESS),
    ]
    assert compute_next_up(tasks, at=now()) == "b"


def test_next_up_falls_back_to_the_lowest_order_available_task() -> None:
    tasks = [
        _task(id="a", order="a2"),
        _task(id="b", order="a1"),
        _task(id="done", order="a0", state=TaskState.COMPLETED),
    ]
    assert compute_next_up(tasks, at=now()) == "b"


def test_next_up_skips_postponed_and_discarded() -> None:
    tasks = [
        _task(id="p", order="a0", state=TaskState.POSTPONED),
        _task(id="d", order="a1", state=TaskState.DISCARDED),
        _task(id="ok", order="a2"),
    ]
    assert compute_next_up(tasks, at=now()) == "ok"


def test_next_up_is_none_when_there_is_nothing_to_do() -> None:
    assert compute_next_up([], at=now()) is None
    assert compute_next_up([_task(state=TaskState.COMPLETED)], at=now()) is None


def test_next_up_only_considers_top_level_tasks() -> None:
    """`order` is only comparable within a parent, so a subtask cannot win the
    lowest-order comparison against a top-level task."""
    tasks = [
        _task(id="sub", order="a0", parent_task_id="parent"),
        _task(id="parent", order="a1"),
    ]
    assert compute_next_up(tasks, at=now()) == "parent"


# --- against the emulator ------------------------------------------------------------


@pytest.fixture
async def project(container, alice):
    return await container.projects.create(alice, title="Rollups", description="g")


async def _parent_with_children(container, alice, project, minutes: list[int]):
    parent = await container.tasks.create_task(
        alice, project.id, title="Parent", estimated_minutes=240
    )
    children = [
        await container.tasks.create_task(
            alice,
            project.id,
            title=f"Sub {i}",
            estimated_minutes=m,
            parent_task_id=parent.id,
        )
        for i, m in enumerate(minutes)
    ]
    return parent, children


async def test_adding_a_subtask_updates_the_parent_rollup(container, alice, project) -> None:
    parent, _ = await _parent_with_children(container, alice, project, [60, 90])
    refreshed = await container.task_repository.get(project.id, parent.id)
    assert refreshed is not None
    assert refreshed.rollup == Rollup(
        subtask_count=2, completed_subtasks=0, total_estimated_minutes=150
    )


async def test_a_leaf_task_has_no_rollup(container, alice, project) -> None:
    task = await container.tasks.create_task(alice, project.id, title="Leaf")
    assert task.rollup is None


async def test_editing_a_subtask_estimate_recomputes_the_parent(
    container, alice, project
) -> None:
    parent, children = await _parent_with_children(container, alice, project, [60, 90])
    await container.tasks.update_task(alice, children[0].id, estimated_minutes=15)

    refreshed = await container.task_repository.get(project.id, parent.id)
    assert refreshed is not None
    assert refreshed.rollup is not None
    assert refreshed.rollup.total_estimated_minutes == 105


async def test_completing_a_subtask_recomputes_the_parent(container, alice, project) -> None:
    parent, children = await _parent_with_children(container, alice, project, [60, 90])
    await container.tasks.set_state(alice, children[0].id, TaskState.IN_PROGRESS)
    await container.tasks.set_state(alice, children[0].id, TaskState.COMPLETED)

    refreshed = await container.task_repository.get(project.id, parent.id)
    assert refreshed is not None
    assert refreshed.rollup is not None
    assert refreshed.rollup.completed_subtasks == 1
    assert refreshed.rollup.subtask_count == 2


async def test_discarding_a_subtask_removes_it_from_the_parent_rollup(
    container, alice, project
) -> None:
    parent, children = await _parent_with_children(container, alice, project, [60, 90])
    await container.tasks.set_state(alice, children[1].id, TaskState.DISCARDED)

    refreshed = await container.task_repository.get(project.id, parent.id)
    assert refreshed is not None
    assert refreshed.rollup == Rollup(
        subtask_count=1, completed_subtasks=0, total_estimated_minutes=60
    )


async def test_concurrent_subtask_writes_leave_a_consistent_rollup(
    container, alice, project
) -> None:
    """The batch-of-concurrent-writes case.

    Each write recomputes the rollup from the whole board inside its own transaction, so
    whichever order they commit in, the last one to commit sees them all.
    """
    parent, children = await _parent_with_children(container, alice, project, [10, 20, 30, 40])
    await asyncio.gather(
        *(
            container.tasks.update_task(alice, child.id, estimated_minutes=100)
            for child in children
        )
    )

    refreshed = await container.task_repository.get(project.id, parent.id)
    assert refreshed is not None
    assert refreshed.rollup is not None
    assert refreshed.rollup.total_estimated_minutes == 400
    assert refreshed.rollup.subtask_count == 4


async def test_project_counts_track_the_board(container, alice, project) -> None:
    first = await container.tasks.create_task(
        alice, project.id, title="One", estimated_minutes=45
    )
    await container.tasks.create_task(alice, project.id, title="Two", estimated_minutes=90)

    refreshed = await container.project_repository.get(project.id)
    assert refreshed is not None
    assert refreshed.counts.total == 2
    assert refreshed.counts.open_minutes == 135

    await container.tasks.set_state(alice, first.id, TaskState.IN_PROGRESS)
    await container.tasks.set_state(alice, first.id, TaskState.COMPLETED)

    refreshed = await container.project_repository.get(project.id)
    assert refreshed is not None
    assert refreshed.counts.completed == 1
    assert refreshed.counts.open_minutes == 90


async def test_adding_subtasks_produces_a_rollup(container, alice, project) -> None:
    """Golden-flow #2's data shape: "the parent card shows subtask count and summed
    duration".

    Three separate creates rather than one `split_task`, which was removed after M4. The
    rollup is recomputed on every one of them (invariant 5), so the assertion is about the
    state after the last write rather than about a single transaction doing it all.
    """
    parent = await container.tasks.create_task(
        alice, project.id, title="Four hours of work", estimated_minutes=240
    )
    for title, minutes in (("a", 60), ("b", 45), ("c", 45)):
        await container.tasks.create_task(
            alice,
            project.id,
            title=title,
            estimated_minutes=minutes,
            parent_task_id=parent.id,
        )

    result = await container.tasks.get_with_subtasks(alice, parent.id)
    assert result.rollup == Rollup(
        subtask_count=3, completed_subtasks=0, total_estimated_minutes=150
    )
    assert [s.title for s in result.subtasks] == ["a", "b", "c"]
