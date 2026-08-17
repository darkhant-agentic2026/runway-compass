"""Tier-1 agent tests: a stubbed model emitting scripted function calls.

docs/08-testing.md#agent-level-tests:

> **Deterministic tool-contract tests** with a stubbed model that emits scripted function
> calls. Asserts that a `split_task` call produces valid subtasks respecting the duration
> budget, that autonomous mode's forbidden tools are actually unavailable, and that
> `post_research_report` writes the right documents and session event.

The third is M4. The first two are here, plus the guards from
docs/03-agent-design.md#domain-tools and the `board_update` push.

**Everything but the model is real.** The turn is started through
`POST /api/sessions/{sid}/turns`, generation runs in the detached task the disconnect
guarantee depends on, the tools go through `TaskService` against the emulator, and the
board is read back through `GET /api/projects/{id}/tasks`. What the stub replaces is the
decision to call a tool — which is the only part that would otherwise be nondeterministic.

That is also what makes these tests about the *prompt* as much as about the tools: the
stub reads its task budget out of the rendered instruction
(`integrations/stub_model.py`), so a subtask sized to a project's override is evidence
that the override reached the model, not just that `split_task` divides.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from coach.agents.context import MAX_TASKS_PER_RUN
from coach.integrations.stub_model import StubModel
from coach.ws.hub import BoardUpdateHub

TURN_TIMEOUT_SECONDS = 20.0


@pytest.fixture
def stub_model(container, monkeypatch: pytest.MonkeyPatch) -> StubModel:
    """The e2e harness's model, with its pacing turned off.

    The delay exists so golden flow #4 has a window to disconnect inside; here it is only
    latency, and a multi-step tool conversation pays it once per chunk per step.
    """
    monkeypatch.setenv("STUB_MODEL_DELAY_MS", "0")
    model = StubModel()
    container.runners.set_model(model)
    return model


async def _project(client: httpx.AsyncClient, title: str, **prefs: Any) -> dict[str, Any]:
    project = (await client.post("/api/projects", json={"title": title})).json()
    if prefs:
        project = (
            await client.patch(f"/api/projects/{project['id']}", json={"prefs": prefs})
        ).json()
    return dict(project)


async def _intake_session(client: httpx.AsyncClient, project_id: str) -> str:
    response = await client.post(f"/api/projects/{project_id}/session")
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def _say(client: httpx.AsyncClient, session_id: str, text: str) -> dict[str, Any]:
    """Take one turn and wait for it to reach a terminal state.

    Polls `GET /api/turns/{turnId}` rather than opening a socket: this suite is about what
    the tools did, and the streaming path has its own suite. The poll is also the honest
    client behaviour for "is it still working" (docs/04-api-contract.md).
    """
    started = await client.post(f"/api/sessions/{session_id}/turns", json={"text": text})
    assert started.status_code == 202, started.text
    turn_id = started.json()["turnId"]

    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            turn = (await client.get(f"/api/turns/{turn_id}")).json()
            if turn["status"] != "running":
                return dict(turn)
            await asyncio.sleep(0.02)


async def _board(client: httpx.AsyncClient, project_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/projects/{project_id}/tasks")
    assert response.status_code == 200, response.text
    return list(response.json()["tasks"])


# --- the coach acts on the board ---------------------------------------------------------


async def test_the_coach_adds_a_task_from_the_conversation(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """Golden flow #1's server half: an intake conversation produces a task list."""
    project = await _project(client, "Learn Rust")
    session_id = await _intake_session(client, project["id"])

    # A message with no duration in it is a conversation, not a plan: nothing is created.
    await _say(client, session_id, "I want to get good at Rust")
    assert await _board(client, project["id"]) == []

    turn = await _say(client, session_id, "I can give it 40 minutes a session")
    assert turn["status"] == "complete"

    board = await _board(client, project["id"])
    assert [task["origin"] for task in board] == ["agent"]
    assert board[0]["estimatedMinutes"] == 40


async def test_a_four_hour_ask_becomes_subtasks_that_fit(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """Golden flow #2, and M3's exit criterion about the parent rollup.

    45 minutes is the global default, so `add_task`'s "no more than 3x the default" guard
    clamps the ask at 135 and the split produces three 45-minute subtasks. The assertion
    worth having is not the specific numbers but the two invariants: no subtask is over
    budget, and the parent's rollup is the sum of its children.
    """
    project = await _project(client, "Build a compiler")
    session_id = await _intake_session(client, project["id"])

    await _say(client, session_id, "The parser is about 4 hours of work")

    board = await _board(client, project["id"])
    assert len(board) == 1
    parent = board[0]
    subtasks = parent["subtasks"]

    assert len(subtasks) >= 2
    assert all(child["estimatedMinutes"] <= 45 for child in subtasks)
    assert parent["rollup"]["subtaskCount"] == len(subtasks)
    assert parent["rollup"]["totalEstimatedMinutes"] == sum(
        child["estimatedMinutes"] for child in subtasks
    )
    assert all(child["origin"] == "agent" for child in subtasks)


async def test_a_project_override_changes_how_the_coach_sizes_work(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """Golden flow #7 and M3's other exit criterion, at the service level.

    Two projects, one message, one difference: `defaultTaskMinutes`. Nothing in the stub
    knows which project it is in — it reads the budget out of the rendered instruction —
    so subtasks that follow the override are evidence that `resolve_prefs` reached the
    prompt, which is the thing docs/02-data-model.md calls one source of truth.
    """
    default = await _project(client, "On the global default")
    overridden = await _project(client, "Two-hour sessions", defaultTaskMinutes=120)

    for project in (default, overridden):
        session_id = await _intake_session(client, project["id"])
        await _say(client, session_id, "This chunk is about 4 hours of work")

    default_board = await _board(client, default["id"])
    overridden_board = await _board(client, overridden["id"])

    assert all(child["estimatedMinutes"] <= 45 for child in default_board[0]["subtasks"])
    assert all(child["estimatedMinutes"] <= 120 for child in overridden_board[0]["subtasks"])
    # The override lets a whole 4 hours in — 240 is inside 3x120 — where the default
    # project's task was clamped to 135.
    assert overridden_board[0]["rollup"]["totalEstimatedMinutes"] == 240
    assert default_board[0]["rollup"]["totalEstimatedMinutes"] == 135


async def test_a_board_update_reaches_the_users_other_tabs(
    client: httpx.AsyncClient, container, stub_model: StubModel
) -> None:
    """The `board_update` push, from the tool that made the change to a listening socket.

    Attached directly to the hub rather than through a `SocketSession`, because what is
    under test is that a *tool* announces its write — the socket's own wiring is asserted
    in the streaming suite.
    """
    frames: list[dict[str, Any]] = []

    async def sink(frame: dict[str, Any]) -> None:
        frames.append(frame)

    container.board_updates.attach("u_alice", sink)
    try:
        project = await _project(client, "Watch the board")
        session_id = await _intake_session(client, project["id"])
        await _say(client, session_id, "Give me 30 minutes of work")
    finally:
        container.board_updates.detach("u_alice", sink)

    assert frames, "the tool made a board change and told nobody"
    assert {frame["type"] for frame in frames} == {"board_update"}
    assert frames[0]["projectId"] == project["id"]
    assert frames[0]["origin"] == "agent"


async def test_another_users_socket_hears_nothing(container) -> None:
    """The hub is keyed by uid, and that is the isolation boundary it carries."""
    hub = BoardUpdateHub()
    heard: list[dict[str, Any]] = []

    async def sink(frame: dict[str, Any]) -> None:
        heard.append(frame)

    hub.attach("u_mallory", sink)
    await hub.publish("u_alice", project_id="p_1", task_ids=["k_1"])
    assert heard == []


async def test_a_dead_socket_does_not_fail_the_tool(container) -> None:
    """A sink that raises is a tab that closed, not a reason to undo a committed write."""
    hub = BoardUpdateHub()

    async def broken(_frame: dict[str, Any]) -> None:
        raise RuntimeError("socket is gone")

    hub.attach("u_alice", broken)
    await hub.publish("u_alice", project_id="p_1", task_ids=["k_1"])


# --- guards -------------------------------------------------------------------------------


async def test_the_tools_are_the_catalogue_the_design_lists(container) -> None:
    """The tool surface, asserted by name.

    docs/03-agent-design.md#domain-tools is the catalogue; `post_research_report` is M4
    and the memory tools are M6, so this list grows twice more. Pinning it by name means
    a tool that is added without a docs row, or removed by a refactor, shows up here
    rather than as a model that quietly stops being able to do something.
    """
    names = {tool.name for tool in container.domain_tools.as_tools()}
    assert names == {
        "list_tasks",
        "add_task",
        "split_task",
        "update_task",
        "set_task_state",
        "set_next_up",
        "reorder_task",
        "discard_task",
        "update_project_prefs",
    }


async def test_discarding_is_the_only_tool_that_needs_confirmation(container) -> None:
    """docs/03-agent-design.md: `discard_task` "requires user confirmation".

    The gate is ADK's `require_confirmation`, so it holds whether or not the model
    cooperates — which is the difference between a gate and an instruction. Asserted
    against the built tool rather than the constructor argument, because the flag is only
    load-bearing once `FunctionTool` is holding it.
    """
    gated = {
        tool.name
        for tool in container.domain_tools.as_tools()
        if getattr(tool, "_require_confirmation", False)
    }
    assert gated == {"discard_task"}


async def test_an_oversized_task_is_refused_rather_than_created(
    client: httpx.AsyncClient, container
) -> None:
    """`add_task`'s "minutes <= 3x default" guard, called directly.

    Through the tool rather than through a turn, because the stub deliberately clamps its
    own ask to what the guard allows — a model that did not would get this, and the tool
    has to answer it as a fact rather than as an exception that ends the turn.
    """
    project = await _project(client, "Sizing")
    result = await container.domain_tools.add_task(
        "A whole afternoon", "", 300, True, _FakeToolContext("u_alice", project["id"])
    )
    assert not result["ok"]
    assert "135" in result["error"]["message"]
    assert await _board(client, project["id"]) == []


async def test_a_subtask_over_the_budget_is_refused(
    client: httpx.AsyncClient, container
) -> None:
    """`split_task`'s stricter guard: each subtask must fit the budget, not 3x it.

    A split whose pieces are still oversized has not done the thing splitting is for, so
    this bound is tighter than `add_task`'s on purpose (docs/03-agent-design.md).
    """
    project = await _project(client, "Splitting")
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Big", "estimatedMinutes": 120},
        )
    ).json()["task"]

    result = await container.domain_tools.split_task(
        task["id"],
        [
            {"title": "First half", "estimatedMinutes": 60},
            {"title": "Second half", "estimatedMinutes": 60},
        ],
        _FakeToolContext("u_alice", project["id"]),
    )
    assert not result["ok"]
    assert "45-minute budget" in result["error"]["message"]
    assert (await _board(client, project["id"]))[0]["subtasks"] == []


async def test_a_run_may_add_only_five_tasks(client: httpx.AsyncClient, container) -> None:
    """docs/03-agent-design.md caps `add_task` at five per run.

    The counter lives in `temp:` state, so it is scoped to the invocation and not to the
    session — a cap that survived the turn would make the sixth task of a *conversation*
    impossible, which is not what the design says.
    """
    project = await _project(client, "Enthusiasm")
    tools = container.domain_tools
    context = _FakeToolContext("u_alice", project["id"])

    for index in range(MAX_TASKS_PER_RUN):
        result = await tools.add_task(f"Task {index}", "", 30, True, context)
        assert result["ok"], result

    refused = await tools.add_task("One too many", "", 30, True, context)
    assert not refused["ok"]
    assert str(MAX_TASKS_PER_RUN) in refused["error"]["message"]
    assert len(await _board(client, project["id"])) == MAX_TASKS_PER_RUN

    # A fresh invocation gets a fresh allowance.
    assert (
        await tools.add_task(
            "Next turn", "", 30, True, _FakeToolContext("u_alice", project["id"])
        )
    )["ok"]


async def test_a_tool_in_an_unlinked_session_refuses_rather_than_guesses(
    container,
) -> None:
    """No project on the session means no board to change, and the tool says so."""
    result = await container.domain_tools.add_task(
        "Orphan", "", 30, True, _FakeToolContext("u_alice", project_id=None)
    )
    assert not result["ok"]
    assert "not linked to a project" in result["error"]["message"]


async def test_a_tool_cannot_touch_another_users_board(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient, container
) -> None:
    """The uid comes from the session, so a project id alone buys nothing.

    This is the same `NotFound`-rather-than-`Forbidden` boundary every service entry point
    holds (docs/02-data-model.md#access-model); the point here is that routing a call
    through a tool does not bypass it.
    """
    victim = await _project(client, "Alice's project")
    result = await container.domain_tools.add_task(
        "Injected", "", 30, True, _FakeToolContext("u_mallory", victim["id"])
    )
    assert not result["ok"]
    assert result["error"]["code"] == "not-found"
    assert await _board(client, victim["id"]) == []


# --- doubles -------------------------------------------------------------------------------


class _FakeState(dict[str, Any]):
    """`Context.state` is a `State`; a dict has the three methods the tools use."""


class _FakeToolContext:
    """The two fields `agents/context.py` reads off a `ToolContext`.

    A double rather than a real `Context`, because building one needs an
    `InvocationContext`, which needs a session, a runner, and a model — none of which
    these guard tests are about. What it *is* faithful to is the surface: if a tool starts
    reading a third field, this stops compiling rather than silently reading `None`.
    """

    def __init__(self, uid: str, project_id: str | None, default_minutes: int = 45) -> None:
        self.user_id = uid
        self.state = _FakeState()
        if project_id is not None:
            self.state["temp:coach_project_id"] = project_id
        self.state["temp:coach_default_minutes"] = default_minutes
