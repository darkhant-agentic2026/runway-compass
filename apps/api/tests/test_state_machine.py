"""The task state machine.

docs/08-testing.md:

    A table-driven test over every (from_state, transition) pair asserting allowed/denied
    and the resulting invariants — plus a concurrency test that two simultaneous
    `set_next_up` calls leave exactly one `current` task (run against the emulator, real
    transactions).

The table below is written out longhand rather than derived from `_TRANSITIONS`, on
purpose: a test that recomputes the thing it is testing proves only that the code is
self-consistent. This one is transcribed from the diagram in docs/02-data-model.md, so it
fails if the code and the diagram diverge.
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
    (S.NOT_STARTED, S.CURRENT): Transition.START,
    (S.CURRENT, S.COMPLETED): Transition.COMPLETE,
    (S.CURRENT, S.POSTPONED): Transition.DEFER,
    (S.CURRENT, S.POSTPONED_UNTIL): Transition.DEFER_UNTIL,
    (S.COMPLETED, S.NOT_STARTED): Transition.REOPEN,
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


def test_completing_is_only_possible_from_current() -> None:
    """Nothing jumps straight to `completed`; the board only offers what is legal."""
    for state in S:
        if state is S.CURRENT:
            continue
        with pytest.raises(InvalidTransition):
            transition_for(state, S.COMPLETED)


def test_both_postponed_states_return_to_not_started() -> None:
    """`postponed` waits for the user, `postponed_until` waits for a clock; both come
    back to the same place."""
    assert transition_for(S.POSTPONED, S.NOT_STARTED) is Transition.RESTORE
    assert transition_for(S.POSTPONED_UNTIL, S.NOT_STARTED) is Transition.RESTORE


def test_postponed_until_requires_a_timestamp() -> None:
    """Invariant 2, first half."""
    with pytest.raises(ValidationProblem):
        validate_transition(S.CURRENT, S.POSTPONED_UNTIL, postponed_until=None)
    validate_transition(S.CURRENT, S.POSTPONED_UNTIL, postponed_until=now() + timedelta(days=1))


def test_a_timestamp_is_refused_on_any_other_state() -> None:
    """Invariant 2, second half: the field is meaningful for exactly one state."""
    with pytest.raises(ValidationProblem):
        validate_transition(S.CURRENT, S.POSTPONED, postponed_until=now())


def test_the_two_postponed_states_are_distinct() -> None:
    """docs/02-data-model.md is explicit that this is not one state with a nullable
    field, so a refactor collapsing them should fail here."""
    assert S.POSTPONED != S.POSTPONED_UNTIL
    assert allowed_transitions(S.CURRENT).keys() >= {S.POSTPONED, S.POSTPONED_UNTIL}


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


async def test_two_simultaneous_promotions_leave_exactly_one_current(container, alice) -> None:
    """The concurrency test docs/08-testing.md asks for, against real transactions.

    Both callers read a board with no `current` task, and both try to become it. The
    single-`current` invariant is enforced inside the transaction, so Firestore aborts
    and retries the loser, which then sees the winner and demotes it.
    """
    project, tasks = await _project_with_tasks(container, alice, 4)

    await asyncio.gather(
        container.tasks.set_state(alice, tasks[0].id, TaskState.CURRENT),
        container.tasks.set_state(alice, tasks[1].id, TaskState.CURRENT),
        container.tasks.set_state(alice, tasks[2].id, TaskState.CURRENT),
    )

    board = await container.task_repository.list_all(project.id)
    current = [t for t in board if t.state is TaskState.CURRENT]
    assert len(current) == 1, [f"{t.id}={t.state}" for t in board]

    refreshed = await container.project_repository.get(project.id)
    assert refreshed is not None
    assert refreshed.next_up_task_id == current[0].id


async def test_promoting_demotes_the_previous_current_task(container, alice) -> None:
    project, tasks = await _project_with_tasks(container, alice, 2)

    await container.tasks.set_state(alice, tasks[0].id, TaskState.CURRENT)
    await container.tasks.set_state(alice, tasks[1].id, TaskState.CURRENT)

    board = {t.id: t for t in await container.task_repository.list_all(project.id)}
    assert board[tasks[0].id].state is TaskState.NOT_STARTED
    assert board[tasks[1].id].state is TaskState.CURRENT


async def test_postponed_until_in_the_past_is_refused(container, alice) -> None:
    _, tasks = await _project_with_tasks(container, alice, 1)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.CURRENT)
    with pytest.raises(ValidationProblem):
        await container.tasks.set_state(
            alice,
            tasks[0].id,
            TaskState.POSTPONED_UNTIL,
            postponed_until=now() - timedelta(minutes=1),
        )


async def test_completing_stamps_completed_at_and_reopening_clears_it(container, alice) -> None:
    _, tasks = await _project_with_tasks(container, alice, 1)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.CURRENT)

    completed = await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
    assert completed.completed_at is not None

    reopened = await container.tasks.set_state(alice, tasks[0].id, TaskState.NOT_STARTED)
    assert reopened.completed_at is None


async def test_re_issuing_the_same_state_is_a_no_op_not_a_conflict(container, alice) -> None:
    """A double-clicked row action must not 409."""
    _, tasks = await _project_with_tasks(container, alice, 1)
    await container.tasks.set_state(alice, tasks[0].id, TaskState.CURRENT)
    first = await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
    second = await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
    assert first.state is second.state is TaskState.COMPLETED


async def test_an_illegal_transition_is_refused_end_to_end(container, alice) -> None:
    _, tasks = await _project_with_tasks(container, alice, 1)
    with pytest.raises(InvalidTransition):
        await container.tasks.set_state(alice, tasks[0].id, TaskState.COMPLETED)
