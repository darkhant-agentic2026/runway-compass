"""The task state machine, as a table.

docs/02-data-model.md#task-state-machine. The diagram there is the specification; this
module is a transcription of it, and `tests/test_state_machine.py` walks every
(from_state, transition) pair.

Transitions are named because the name is what the UI offers as a row action — the board
only shows the actions that are legal from the row's current state, which is why there is
no "complete" affordance on a task that has not been started.
"""

from __future__ import annotations

from enum import StrEnum

from coach.core.errors import InvalidTransition, ValidationProblem
from coach.services.models import TaskState


class Transition(StrEnum):
    PLAN = "plan"
    START = "start"
    COMPLETE = "complete"
    DEFER = "defer"
    DEFER_UNTIL = "defer_until"
    REOPEN = "reopen"
    RESUME = "resume"
    RESTORE = "restore"
    DISCARD = "discard"


#: (from state, transition) -> to state. Exactly the arrows in the diagram.
_TRANSITIONS: dict[tuple[TaskState, Transition], TaskState] = {
    # `draft` carries every transition `not_started` has, plus `plan`. It is a statement
    # about the task ("no plan yet"), not a lock on the learner: a board that refused to
    # let someone begin work until an LLM had written them a checklist would be worse than
    # the one that existed before M4.
    (TaskState.DRAFT, Transition.PLAN): TaskState.NOT_STARTED,
    (TaskState.DRAFT, Transition.START): TaskState.IN_PROGRESS,
    (TaskState.DRAFT, Transition.COMPLETE): TaskState.COMPLETED,
    (TaskState.NOT_STARTED, Transition.START): TaskState.IN_PROGRESS,
    # A task can be finished without ever being explicitly started — ticking the last
    # checklist item is the ordinary way it happens (invariant 6), and refusing the arrow
    # would make the derivation illegal in the machine that is supposed to describe it.
    (TaskState.NOT_STARTED, Transition.COMPLETE): TaskState.COMPLETED,
    (TaskState.IN_PROGRESS, Transition.COMPLETE): TaskState.COMPLETED,
    (TaskState.IN_PROGRESS, Transition.DEFER): TaskState.POSTPONED,
    (TaskState.IN_PROGRESS, Transition.DEFER_UNTIL): TaskState.POSTPONED_UNTIL,
    (TaskState.COMPLETED, Transition.REOPEN): TaskState.NOT_STARTED,
    # `resume` is what un-ticking a checklist item does to a task that auto-completed:
    # back to `in_progress` rather than `not_started`, because the learner is evidently
    # working on it. Declared here rather than special-cased in `derive_state`, so that a
    # derived move is a move the machine allows.
    (TaskState.COMPLETED, Transition.RESUME): TaskState.IN_PROGRESS,
    # "un-postpone / restore" — by user action from either postponed state, and for
    # `postponed_until` also by the nightly sweep in /internal/tick once the timestamp
    # is in the past.
    (TaskState.POSTPONED, Transition.RESTORE): TaskState.NOT_STARTED,
    (TaskState.POSTPONED_UNTIL, Transition.RESTORE): TaskState.NOT_STARTED,
    # Invariant 3: `discarded` is terminal *except by explicit user restore*.
    (TaskState.DISCARDED, Transition.RESTORE): TaskState.NOT_STARTED,
}

# `discard` is reachable from any state ("any state ──discard──▶ discarded"), including
# from `discarded` itself, where it is a no-op rather than an error — a double-click on
# the row action must not 409.
for _state in TaskState:
    _TRANSITIONS[(_state, Transition.DISCARD)] = TaskState.DISCARDED


#: The reverse view: which target states are reachable from a given state, and by which
#: transition. This is what the API accepts, since `POST /api/tasks/{id}/state` takes a
#: target *state* rather than a transition name (docs/04-api-contract.md).
_BY_TARGET: dict[TaskState, dict[TaskState, Transition]] = {}
for (_from, _transition), _to in _TRANSITIONS.items():
    _BY_TARGET.setdefault(_from, {})[_to] = _transition


def allowed_transitions(state: TaskState) -> dict[TaskState, Transition]:
    """Target states reachable from `state`, keyed by target."""
    return dict(_BY_TARGET.get(state, {}))


def transition_for(from_state: TaskState, to_state: TaskState) -> Transition:
    """The transition that moves `from_state` to `to_state`.

    Raises:
        InvalidTransition: if the state machine has no such arrow.
    """
    try:
        return _BY_TARGET[from_state][to_state]
    except KeyError:
        raise InvalidTransition(from_state.value, to_state.value) from None


def validate_transition(
    from_state: TaskState,
    to_state: TaskState,
    *,
    postponed_until: object | None = None,
) -> Transition:
    """Check a requested state change and return the transition it corresponds to.

    Invariant 2 (docs/02-data-model.md) lives here: `postponed_until` requires a
    timestamp, and every other state requires its absence. `TaskService` checks that the
    timestamp is in the future, since only it has the clock.
    """
    transition = transition_for(from_state, to_state)
    if to_state is TaskState.POSTPONED_UNTIL and postponed_until is None:
        raise ValidationProblem(
            "Moving a task to 'postponed_until' requires a postponedUntil timestamp."
        )
    if to_state is not TaskState.POSTPONED_UNTIL and postponed_until is not None:
        raise ValidationProblem(
            f"postponedUntil is only meaningful for state 'postponed_until', not "
            f"{to_state.value!r}."
        )
    return transition


#: States hidden from the board by the default filters (docs/06-frontend.md: "Hide
#: completed" and "Hide discarded" default on, "Hide postponed" defaults off).
DEFAULT_HIDDEN_STATES = frozenset({TaskState.COMPLETED, TaskState.DISCARDED})

#: States that count as "open work" for `project.counts.openMinutes` and for autonomous
#: next-task selection. `draft` is open work: from M4 it is the state a task *starts* in, so
#: a guard written without it would read a board of brand-new tasks as having nothing to do
#: (docs/05-autonomous-runs.md#candidate-selection-and-guards).
OPEN_STATES = frozenset({TaskState.DRAFT, TaskState.NOT_STARTED, TaskState.IN_PROGRESS})

#: States a task can be *started from*, and therefore the ones `compute_next_up` will pin
#: when nothing is in progress.
STARTABLE_STATES = frozenset({TaskState.DRAFT, TaskState.NOT_STARTED})
