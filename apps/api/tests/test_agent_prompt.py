"""The `before_agent_callback` that assembles the coach's prompt.

docs/03-agent-design.md lists what the callback injects; this suite asserts each of those
made it into the state, and — the one that would otherwise fail only in production — that
**every placeholder in each instruction has a writer**.

That last one is not a style check. `inject_session_state` raises `KeyError` on a
placeholder with no state key, and it does so while assembling the LLM request, which is
inside the detached generation task: the user sees a failed turn, and nothing before the
first real turn of a deployed revision would notice. It is the same class of defect as
the composite index and the proxied artifact service
(docs/09-roadmap.md#what-a-green-local-run-does-not-prove) — invisible locally, total in
production — so it gets a test that reads the template rather than a convention.

Two instructions, `project_coach`'s and `task_teacher`'s, share one `PromptBuilder`
(docs/09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher), so
one `WRITTEN` set covers both.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from coach.agents import prompt as prompt_module
from coach.agents.context import (
    DEFAULT_MINUTES_KEY,
    PROJECT_ID_KEY,
    TASK_ID_KEY,
)
from coach.agents.project_coach import INSTRUCTION as PROJECT_INSTRUCTION
from coach.agents.project_coach import static_instruction as project_static_instruction
from coach.agents.prompt import (
    BOARD_KEY,
    BUDGET_TEXT_KEY,
    FOCUS_KEY,
    LEARNER_KEY,
    OUTCOMES_KEY,
    PREFS_KEY,
    format_minutes,
)
from coach.agents.research_agent import SEARCH_INSTRUCTION
from coach.agents.research_workflow import (
    RESEARCH_PLANNER_INSTRUCTION,
    REVIEWER_WRITER_INSTRUCTION,
    TOPIC_RESEARCHER_INSTRUCTION,
)
from coach.agents.task_teacher import INSTRUCTION as TASK_INSTRUCTION
from coach.agents.task_teacher import static_instruction as task_static_instruction

#: The same pattern `google.adk.utils.instructions_utils` substitutes with.
_PLACEHOLDER = re.compile(r"{+[^{}]*}+")


def _placeholders(template: str) -> set[str]:
    return {match.group().strip("{}").strip() for match in _PLACEHOLDER.finditer(template)}


#: Every key `PromptBuilder` writes. One set for both agents deliberately: they share the
#: callback, so a placeholder either has a writer or does not, whichever instruction it is
#: in.
WRITTEN = {
    PREFS_KEY,
    BOARD_KEY,
    FOCUS_KEY,
    OUTCOMES_KEY,
    LEARNER_KEY,
    BUDGET_TEXT_KEY,
}


def test_every_placeholder_in_the_project_coach_instruction_has_a_writer() -> None:
    """Read the template, not the code that fills it. See the module docstring.

    `project_coach` does not reference `{FOCUS_KEY}` — its session is never linked to one
    task, so the line would only ever render the same "not attached to one task"
    boilerplate — which is why this is `<=` rather than the `task_teacher` equality below.
    """
    assert _placeholders(PROJECT_INSTRUCTION) <= WRITTEN
    assert _placeholders(PROJECT_INSTRUCTION) == WRITTEN - {BUDGET_TEXT_KEY, FOCUS_KEY}


def test_every_placeholder_in_the_task_teacher_instruction_has_a_writer() -> None:
    """Read the template, not the code that fills it. See the module docstring."""
    assert _placeholders(TASK_INSTRUCTION) <= WRITTEN
    assert _placeholders(TASK_INSTRUCTION) == WRITTEN - {BUDGET_TEXT_KEY}


def test_every_placeholder_in_the_research_pipeline_instructions_has_a_writer() -> None:
    """The same check for the three `research_workflow` nodes, which share `PromptBuilder`.

    It matters more here, not less: a research run happens inside a detached task with no
    client necessarily attached, so a `KeyError` while assembling the request would show up
    as a run that failed for no visible reason rather than as a message on screen.
    """
    for template in (
        RESEARCH_PLANNER_INSTRUCTION,
        TOPIC_RESEARCHER_INSTRUCTION,
        REVIEWER_WRITER_INSTRUCTION,
    ):
        assert _placeholders(template) <= WRITTEN
    # The duration budget is deliberately withheld from the planner and the topic
    # researchers (docs/03-agent-design.md#the-research-pipeline-since-m9) — only the
    # reviewer-writer, which sizes the final report, is shown it.
    assert BUDGET_TEXT_KEY not in _placeholders(RESEARCH_PLANNER_INSTRUCTION)
    assert BUDGET_TEXT_KEY not in _placeholders(TOPIC_RESEARCHER_INSTRUCTION)
    assert BUDGET_TEXT_KEY in _placeholders(REVIEWER_WRITER_INSTRUCTION)


def test_the_search_agents_instruction_has_no_placeholders() -> None:
    """`search_agent` runs under `AgentTool`, so it does not go through the coach's
    `before_agent_callback` at all — a placeholder here would have no writer by
    construction."""
    assert _placeholders(SEARCH_INSTRUCTION) == set()


def test_no_placeholder_is_optional() -> None:
    """`{key?}` renders a missing key as empty; none of these may be missing.

    Every key is written unconditionally by `PromptBuilder`, including on the failure
    path, so an optional marker here would only hide a builder that stopped writing one.
    """
    for template in (
        PROJECT_INSTRUCTION,
        TASK_INSTRUCTION,
        RESEARCH_PLANNER_INSTRUCTION,
        TOPIC_RESEARCHER_INSTRUCTION,
        REVIEWER_WRITER_INSTRUCTION,
    ):
        assert not any(name.endswith("?") for name in _placeholders(template))


class _FakeState(dict[str, Any]):
    pass


class _FakeCallbackContext:
    """`Context`'s three fields the builder touches, and nothing else."""

    def __init__(self, uid: str, session_id: str) -> None:
        self.user_id = uid
        self.state = _FakeState()
        self.session = type("_Session", (), {"id": session_id})()


async def _build(container, uid: str, session_id: str) -> dict[str, Any]:
    context = _FakeCallbackContext(uid, session_id)
    assert await container.prompt_builder(callback_context=context) is None
    return dict(context.state)


async def test_the_state_carries_the_board_the_prefs_and_the_focus(
    client: httpx.AsyncClient, container
) -> None:
    project = (await client.post("/api/projects", json={"title": "Concurrency"})).json()
    await client.patch(
        f"/api/projects/{project['id']}", json={"prefs": {"defaultTaskMinutes": 90}}
    )
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Read about locks", "estimatedMinutes": 60},
        )
    ).json()["task"]
    session_id = (await client.post(f"/api/tasks/{task['id']}/session")).json()["session"]["id"]

    state = await _build(container, "u_alice", session_id)

    assert "90 minutes" in state[PREFS_KEY]
    assert "Read about locks" in state[BOARD_KEY]
    assert task["id"] in state[FOCUS_KEY]
    assert state[PROJECT_ID_KEY] == project["id"]
    assert state[TASK_ID_KEY] == task["id"]
    assert state[DEFAULT_MINUTES_KEY] == 90


async def test_an_intake_session_is_labelled_as_one(
    client: httpx.AsyncClient, container
) -> None:
    """`taskId: null` is what makes a session intake, and the state has to say so.

    `TurnService._resolve_agent` reads the same `task_id is None` fact to route to
    `project_coach` rather than `task_teacher` (docs/09-roadmap.md#m6), so what this
    builder writes into `FOCUS_KEY` is only ever the boilerplate line below for that
    agent — never "the task in front of the learner" while no task exists, which is how
    golden flow #1 used to turn into a conversation that never proposed anything.
    """
    project = (await client.post("/api/projects", json={"title": "Learn Rust"})).json()
    session_id = (await client.post(f"/api/projects/{project['id']}/session")).json()["id"]

    state = await _build(container, "u_alice", session_id)

    assert state[TASK_ID_KEY] == ""
    assert "not attached to one task" in state[FOCUS_KEY]
    assert "board is empty" in state[BOARD_KEY]


async def test_an_unreadable_session_leaves_the_coach_without_a_board(container) -> None:
    """A session that cannot be read is a coach with no board, not a failed turn.

    Returning `Content` from a `before_agent_callback` *replaces* the model call and ends
    the invocation, so a builder that surfaced its problem that way would answer the
    learner in the coach's own voice with an internal error. The defaults are what the
    template renders instead, and the tools refuse on their own terms.
    """
    state = await _build(container, "u_alice", "s_does_not_exist")

    assert state[PROJECT_ID_KEY] == ""
    # The template still renders: every key either instruction names is present.
    assert _placeholders(PROJECT_INSTRUCTION) <= set(state)
    assert _placeholders(TASK_INSTRUCTION) <= set(state)


async def test_a_rendered_instruction_has_no_placeholders_left(
    client: httpx.AsyncClient, container
) -> None:
    project = (await client.post("/api/projects", json={"title": "Rendering"})).json()
    session_id = (await client.post(f"/api/projects/{project['id']}/session")).json()["id"]
    state = await _build(container, "u_alice", session_id)
    string_state = {k: str(v) for k, v in state.items()}

    assert _placeholders(project_static_instruction(string_state)) == set()

    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "A task", "estimatedMinutes": 45},
        )
    ).json()["task"]
    task_session_id = (await client.post(f"/api/tasks/{task['id']}/session")).json()["session"][
        "id"
    ]
    task_state = await _build(container, "u_alice", task_session_id)
    task_string_state = {k: str(v) for k, v in task_state.items()}
    assert _placeholders(task_static_instruction(task_string_state)) == set()


# --- the renderers, as pure functions -----------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, "0 min"), (45, "45 min"), (60, "1 h"), (90, "1 h 30 m"), (150, "2 h 30 m")],
)
def test_format_minutes_matches_the_frontends(minutes: int, expected: str) -> None:
    """The same shape `apps/web/src/lib/format.ts` renders, and its test's own cases."""
    assert format_minutes(minutes) == expected


def test_a_completed_task_list_is_capped() -> None:
    """ "Last N task outcomes" is bounded, or a long project crowds out the live board."""
    from coach.services.models import Task, TaskState, TaskWithSubtasks

    board = [
        TaskWithSubtasks(
            **Task(
                id=f"k_{index}",
                project_id="p_1",
                owner_uid="u_alice",
                title=f"Task {index}",
                order=f"{index:03d}",
                state=TaskState.COMPLETED,
            ).model_dump(),
            subtasks=[],
        )
        for index in range(12)
    ]
    lines = prompt_module.render_outcomes(board).splitlines()
    assert len(lines) == prompt_module.RECENT_OUTCOMES
    assert lines[-1].startswith("- Task 11")
