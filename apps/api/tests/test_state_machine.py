"""The task state machine.

docs/08-testing.md: a table-driven test over every (from_state, transition) pair asserting
allowed/denied and the resulting invariants, against the emulator and real transactions.

The table below is written out longhand rather than derived from `_TRANSITIONS`, on
purpose: a test that recomputes the thing it is testing proves only that the code is
self-consistent. This one is transcribed from the diagram in docs/02-data-model.md, so it
fails if the code and the diagram diverge.

The concurrency test here used to assert that two simultaneous promotions left exactly one
`current` task. M4 removed the singleton — `in_progress` describes the task rather than the
learner's attention — so what is left to check is that the contended write to
`projects/{id}` retries rather than losing an update, and that is what
`test_two_simultaneous_starts_leave_both_in_progress` now does.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from coach.core.clock import now
from coach.core.errors import InvalidTransition, ValidationProblem
from coach.services.models import TaskState
from coach.services.state_machine import (
    Transition,
    allowed_transitions,
    transition_for,
    validate_transition,
)

S = TaskState

#: (from, to) pairs the diagram permits, and the transition each one is.
ALLOWED: dict[tuple[TaskState, TaskState], Transition] = {
    # `draft` carries every arrow `not_started` has, plus the automatic `plan`.
    (S.DRAFT, S.NOT_STARTED): Transition.PLAN,
    (S.DRAFT, S.IN_PROGRESS): Transition.START,
    (S.DRAFT, S.COMPLETED): Transition.COMPLETE,
    (S.NOT_STARTED, S.IN_PROGRESS): Transition.START,
    (S.NOT_STARTED, S.COMPLETED): Transition.COMPLETE,
    (S.IN_PROGRESS, S.COMPLETED): Transition.COMPLETE,
    (S.IN_PROGRESS, S.POSTPONED): Transition.DEFER,
    (S.IN_PROGRESS, S.POSTPONED_UNTIL): Transition.DEFER_UNTIL,
    (S.COMPLETED, S.NOT_STARTED): Transition.REOPEN,
    (S.COMPLETED, S.IN_PROGRESS): Transition.RESUME,
    (S.POSTPONED, S.NOT_STARTED): Transition.RESTORE,
    (S.POSTPONED_UNTIL, S.NOT_STARTED): Transition.RESTORE,
    (S.DISCARDED, S.NOT_STARTED): Transition.RESTORE,
    **{(state, S.DISCARDED): Transition.DISCARD for state in S},
}

ALL_PAIRS = [(a, b) for a in S for b in S]


@pytest.mark.parametrize(("from_state", "to_state"), ALL_PAIRS)
def test_every_state_pair_is_allowed_or_denied_as_the_diagram_says(
    from_state: TaskState, to_state: TaskState
) -> None:
    expected = ALLOWED.get((from_state, to_state))
    if expected is None:
        with pytest.raises(InvalidTransition):
            transition_for(from_state, to_state)
    else:
        assert transition_for(from_state, to_state) is expected


def test_discard_is_reachable_from_every_state() -> None:
    """ "any state ──discard──▶ discarded" in the diagram."""
    for state in S:
        assert transition_for(state, S.DISCARDED) is Transition.DISCARD


def test_discarded_is_terminal_except_by_restore() -> None:
    """Invariant 3: `discarded` is terminal except by explicit user restore."""
    reachable = allowed_transitions(S.DISCARDED)
    assert set(reachable) == {S.NOT_STARTED, S.DISCARDED}
    assert reachable[S.NOT_STARTED] is Transition.RESTORE


def test_completing_is_possible_only_from_the_open_states() -> None:
    """A task can be finished without being explicitly started — ticking the last checklist
    item is the ordinary way it happens (invariant 6) — but a postponed or discarded one
    cannot jump straight to `completed`."""
    for state in (S.POSTPONED, S.POSTPONED_UNTIL, S.DISCARDED, S.COMPLETED):
        with pytest.raises(InvalidTransition):
            transition_for(state, S.COMPLETED)
    for state in (S.DRAFT, S.NOT_STARTED, S.IN_PROGRESS):
        assert transition_for(state, S.COMPLETED) is Transition.COMPLETE


def test_draft_is_not_a_lock() -> None:
    """docs/02-data-model.md: "`draft` carries every transition `not_started` has, plus the
    automatic one." A board that refused to let someone begin work until an LLM had written
    them a checklist would be worse than the one that existed before M4."""
    from_draft = set(allowed_transitions(S.DRAFT))
    from_not_started = set(allowed_transitions(S.NOT_STARTED))
    assert from_draft >= from_not_started
    assert from_draft - from_not_started == {S.NOT_STARTED}


def test_nothing_transitions_back_into_draft() -> None:
    """`draft` is a state a task leaves, never one it is put into: a task that has had a
    plan and lost it does not go back to having never had one."""
    for state in S:
        with pytest.raises(InvalidTransition):
            transition_for(state, S.DRAFT)


def test_both_postponed_states_return_to_not_started() -> None:
    """`postponed` waits for the user, `postponed_until` waits for a clock; both come
    back to the same place."""
    assert transition_for(S.POSTPONED, S.NOT_STARTED) is Transition.RESTORE
    assert transition_for(S.POSTPONED_UNTIL, S.NOT_STARTED) is Transition.RESTORE


def test_postponed_until_requires_a_timestamp() -> None:
    """Invariant 2, first half."""
    with pytest.raises(ValidationProblem):
        validate_transition(S.IN_PROGRESS, S.POSTPONED_UNTIL, postponed_until=None)
    validate_transition(
        S.IN_PROGRESS, S.POSTPONED_UNTIL, postponed_until=now() + timedelta(days=1)
    )


def test_a_timestamp_is_refused_on_any_other_state() -> None:
    """Invariant 2, second half: the field is meaningful for exactly one state."""
    with pytest.raises(ValidationProblem):
        validate_transition(S.IN_PROGRESS, S.POSTPONED, postponed_until=now())


def test_the_two_postponed_states_are_distinct() -> None:
    """docs/02-data-model.md is explicit that this is not one state with a nullable
    field, so a refactor collapsing them should fail here."""
    assert S.POSTPONED != S.POSTPONED_UNTIL
    assert allowed_transitions(S.IN_PROGRESS).keys() >= {S.POSTPONED, S.POSTPONED_UNTIL}


# --- against the emulator, with real transactions ------------------------------------


async def _project_with_tasks(container, principal, count: int):
    project = await container.projects.create(principal, title="Concurrency")
    tasks = [
        await container.tasks.create_task(
            principal, project.id, title=f"Task {i}", estimated_minutes=30
        )
        for i in range(count)
    ]
    return project, tasks


async def test_two_simultaneous_starts_leave_both_in_progress(container, alice) -> None:
    """The concurrency test docs/08-testing.md asks for, against real transactions.

    Three callers start three tasks at once. Every one of them writes the *same* project
    document — `counts` and `nextUpTaskId` are recomputed on every task mutation — so this
    is a genuine three-way contention on one document, and what it asserts is that the
    losers retry and land rather than one of them being silently dropped.

    Before M4 this test asserted the opposite outcome: exactly one task `current`, the
    other two demoted. That invariant is gone, and the way it is gone matters — a run that
    left two tasks `in_progress` used to be a bug and is now the specification.
    """
    project, tasks = await _project_with_tasks(container, alice, 4)

    await asyncio.gather(
        container.tasks.set_state(alice, tasks[0].id, TaskState.IN_PROGRESS),
        container.tasks.set_state(alice, tasks[1].id, TaskState.IN_PROGRESS),
        container.tasks.set_state(alice, tasks[2].id, TaskState.IN_PROGRESS),
    )

    board = {t.id: t for t in await container.task_repository.list_all(project.id)}
    started = [board[t.id] for t in tasks[:3]]
    assert all(t.state is TaskState.IN_PROGRESS for t in started), [
        f"{t.id}={t.state}" for t in board.values()
    ]

    # The pointer is derived, so it names the lowest-`order` in-progress task rather than
    # whichever write happened to commit last.
    refreshed = await container.project_repository.get(project.id)
    assert refreshed is not None
    assert refreshed.next_up_task_id == min(started, key=lambda t: t.order).id


async def test_starting_a_task_demotes_nothing(container, alice) -> None:
    """The removed invariant, asserted as an absence.

    A learner who opens a second task has not abandoned the first, and the board used to
    say they had.
    """
    project, tasks = await _project_with_tasks(container, alice, 2)

    await container.tasks.set_state(alice, tasks[0].id, TaskState.IN_PROGRESS)
    await container.tasks.set_state(alice, tasks[1].id, TaskState.IN_PROGRESS)

    board = {t.id: t for t in await container.task_repository.list_all(project.id)}
    assert board[tasks[0].id].state is TaskState.IN_PROGRESS
    assert board[tasks[1].id].state is TaskState.IN_PROGRESS


async def test_postponed_until_in_the_past_is_refused(container, alice) -> None:
    _, tasks = await _project_with_tasks(container, alice, 1)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.IN_PROGRESS)
    with pytest.raises(ValidationProblem):
        await container.tasks.set_state(
            alice,
            tasks[0].id,
            TaskState.POSTPONED_UNTIL,
            postponed_until=now() - timedelta(minutes=1),
        )


async def test_completing_stamps_completed_at_and_reopening_clears_it(container, alice) -> None:
    _, tasks = await _project_with_tasks(container, alice, 1)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.IN_PROGRESS)

    completed = await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
    assert completed.completed_at is not None

    reopened = await container.tasks.set_state(alice, tasks[0].id, TaskState.NOT_STARTED)
    assert reopened.completed_at is None


async def test_re_issuing_the_same_state_is_a_no_op_not_a_conflict(container, alice) -> None:
    """A double-clicked row action must not 409."""
    _, tasks = await _project_with_tasks(container, alice, 1)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.IN_PROGRESS)
    first = await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
    second = await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
    assert first.state is second.state is TaskState.COMPLETED


async def test_an_illegal_transition_is_refused_end_to_end(container, alice) -> None:
    _, tasks = await _project_with_tasks(container, alice, 1)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.IN_PROGRESS)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
    with pytest.raises(InvalidTransition):
        await container.tasks.set_state(alice, tasks[0].id, TaskState.POSTPONED)


async def test_a_new_task_starts_in_draft(container, alice) -> None:
    """Invariant 1's starting point: on the board, with no plan yet."""
    _, tasks = await _project_with_tasks(container, alice, 1)
    assert tasks[0].state is TaskState.DRAFT
