"""Contention handling around Firestore transactions.

Task mutations read a project's whole board and write the parent rollup and the project
counts, so concurrent writes to one project contend on the same documents by
construction. The Firestore client's own retry loop re-runs an aborted transaction
immediately, with no delay, so contenders can collide in lockstep until the attempt
budget runs out — which surfaces as a 500 from a perfectly valid write.

`Database.run` adds the backoff and jitter that loop lacks. These tests pin both halves:
that a contended burst succeeds, and that a non-contention error is still raised rather
than being swallowed by the retry.
"""

from __future__ import annotations

import asyncio

import pytest
from google.api_core.exceptions import Aborted

from coach.api.deps import Container
from coach.repositories.firestore import TRANSACTION_RETRIES, Database
from coach.services.models import TaskState


async def test_a_burst_of_concurrent_writes_all_succeed(container, alice) -> None:
    """Eight concurrent edits to one project, through one service instance.

    `TaskService` holds a per-project `asyncio.Lock`, so these queue rather than collide
    and the result is deterministic — which is the point. Firestore resolves contention by
    aborting and retrying, and under this much concurrency the retry budget runs out and a
    valid write surfaces as a 500. This asserts that the common case, several writes to
    one project on one instance, does not depend on that budget at all.
    """
    project = await container.projects.create(alice, title="Contention")
    parent = await container.tasks.create_task(
        alice, project.id, title="Parent", estimated_minutes=120
    )
    children = [
        await container.tasks.create_task(
            alice,
            project.id,
            title=f"Sub {index}",
            estimated_minutes=30,
            parent_task_id=parent.id,
        )
        for index in range(8)
    ]

    await asyncio.gather(
        *(
            container.tasks.update_task(alice, child.id, estimated_minutes=45)
            for child in children
        )
    )

    refreshed = await container.task_repository.get(project.id, parent.id)
    assert refreshed is not None
    assert refreshed.rollup is not None
    assert refreshed.rollup.subtask_count == 8
    assert refreshed.rollup.total_estimated_minutes == 8 * 45


async def test_two_instances_contend_on_the_same_project(settings, alice) -> None:
    """The guarantee the lock is *not* providing.

    A second Cloud Run instance has its own locks and knows nothing of the first one's, so
    cross-instance safety rests entirely on the Firestore transaction and the backoff
    around it. Two independent `Container`s stand in for two instances: same emulator,
    separate `TaskService`s, separate locks.

    Kept to three writers rather than eight, because this one really does go through
    contention and retry, and the point is to prove the path works — not to find the
    concurrency at which a shared CI runner gives up.
    """
    left = Container(settings)
    right = Container(settings)

    project = await left.projects.create(alice, title="Cross-instance")
    tasks = [
        await left.tasks.create_task(alice, project.id, title=f"Task {index}")
        for index in range(3)
    ]

    # Three starts from two instances. Each one rewrites the *project* document — `counts`
    # and `nextUpTaskId` are recomputed on every task mutation — and the instances have
    # separate `asyncio.Lock`s, so nothing but the Firestore transaction is serializing
    # these. That is the whole point: the lock is an optimization, the transaction is the
    # guarantee.
    await asyncio.gather(
        left.tasks.set_state(alice, tasks[0].id, TaskState.IN_PROGRESS),
        right.tasks.set_state(alice, tasks[1].id, TaskState.IN_PROGRESS),
        left.tasks.set_state(alice, tasks[2].id, TaskState.IN_PROGRESS),
    )

    board = await left.task_repository.list_all(project.id)
    started = [t for t in board if t.state is TaskState.IN_PROGRESS]
    assert len(started) == 3, [f"{t.id}={t.state}" for t in board]

    # Not "whichever committed last": the pointer is derived from `order`, so contention
    # cannot leave it naming a task that lost a race.
    refreshed = await left.project_repository.get(project.id)
    assert refreshed is not None
    assert refreshed.next_up_task_id == min(started, key=lambda t: t.order).id


async def test_concurrent_state_changes_all_land(container, alice) -> None:
    project = await container.projects.create(alice, title="Concurrent states")
    tasks = [
        await container.tasks.create_task(alice, project.id, title=f"Task {index}")
        for index in range(6)
    ]

    await asyncio.gather(
        *(container.tasks.set_state(alice, task.id, TaskState.DISCARDED) for task in tasks)
    )

    board = await container.task_repository.list_all(project.id)
    assert all(task.state is TaskState.DISCARDED for task in board)


async def test_run_retries_a_contended_transaction(settings) -> None:
    """The retry path itself: fail with the library's exhaustion error, then succeed."""
    db = Database.from_settings(settings)
    attempts = 0

    async def operation(_transaction: object) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ValueError("Failed to commit transaction in 5 attempts.")
        return "committed"

    assert await db.run(operation) == "committed"
    assert attempts == 3


async def test_run_retries_on_aborted(settings) -> None:
    db = Database.from_settings(settings)
    attempts = 0

    async def operation(_transaction: object) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise Aborted("too much contention")
        return "committed"

    assert await db.run(operation) == "committed"


async def test_run_gives_up_eventually(settings) -> None:
    """Retrying forever would turn a genuinely stuck write into a hung request."""
    db = Database.from_settings(settings)
    attempts = 0

    async def operation(_transaction: object) -> str:
        nonlocal attempts
        attempts += 1
        raise ValueError("Failed to commit transaction in 5 attempts.")

    with pytest.raises(ValueError, match="Failed to commit"):
        await db.run(operation)
    assert attempts == TRANSACTION_RETRIES


async def test_run_does_not_swallow_unrelated_errors(settings) -> None:
    """The exhaustion case is matched by message, so a real bug must still propagate —
    immediately, and without being retried."""
    db = Database.from_settings(settings)
    attempts = 0

    async def operation(_transaction: object) -> str:
        nonlocal attempts
        attempts += 1
        raise ValueError("a genuine programming error")

    with pytest.raises(ValueError, match="genuine programming error"):
        await db.run(operation)
    assert attempts == 1
