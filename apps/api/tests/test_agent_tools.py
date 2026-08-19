"""Tier-1 agent tests: a stubbed model emitting scripted function calls.

docs/08-testing.md#agent-level-tests:

> **Deterministic tool-contract tests** with a stubbed model that emits scripted function
> calls. Asserts that a `split_task` call produces valid subtasks respecting the duration
> budget, that autonomous mode's forbidden tools are actually unavailable, and that
> `post_research_report` writes the right documents and session event.

`split_task` was removed after M4 — it made the model commit to a whole breakdown before
discussing any of it — so the first of those is now about `add_subtask`, one child at a
time, and the budget it has to respect is the same one.

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
that the override reached the model, not just that the arithmetic divides.
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

    docs/03-agent-design.md#domain-tools is the catalogue; the memory tools are M6, so
    this list grows once more. Pinning it by name means a tool that is added without a
    docs row, or removed by a refactor, shows up here rather than as a model that quietly
    stops being able to do something.
    """
    names = {tool.name for tool in container.domain_tools.as_tools()}
    assert names == {
        "list_tasks",
        "add_task",
        "add_subtask",
        "update_task",
        "set_task_state",
        "set_next_up",
        "reorder_task",
        "discard_task",
        "add_task_items",
        "update_task_item",
        "reorder_task_item",
        "move_task_items",
        "delete_task_item",
        "complete_task_item",
        "ask_learner",
        "update_project_prefs",
    }


async def test_exactly_three_tools_are_gated_on_the_learners_confirmation(container) -> None:
    """docs/03-agent-design.md: `discard_task`, `complete_task_item`, and
    `delete_task_item` "require user confirmation".

    The gate is ADK's `require_confirmation`, so it holds whether or not the model
    cooperates — which is the difference between a gate and an instruction. Asserted
    against the built tool rather than the constructor argument, because the flag is only
    load-bearing once `FunctionTool` is holding it.

    Both directions matter, which is why this is an equality and not three `in` checks. An
    extra gated tool makes the coach ask permission for something routine; a missing one is
    silent — and two of these three are missing *task completion*, since the last item
    finishing a checklist finishes the task (docs/02-data-model.md#task-items). Deleting an
    item does it just as effectively as ticking one: remove the only outstanding step and
    the task completes. That is why `delete_task_item` is here and `update_task_item` and
    `reorder_task_item`, which cannot change what is outstanding, are not.

    `ask_learner` is deliberately *absent*. It asks the learner a question rather than for
    approval, so it requests its own confirmation from inside the tool body with a payload
    carrying the question — the static flag would post ADK's generic hint and no payload.
    """
    gated = {
        tool.name
        for tool in container.domain_tools.as_tools()
        if getattr(tool, "_require_confirmation", False)
    }
    assert gated == {"discard_task", "complete_task_item", "delete_task_item"}


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
    """`add_subtask`'s stricter guard: a subtask must fit the budget, not 3x it.

    A breakdown whose pieces are still oversized has not done the thing breaking up is
    for, so this bound is tighter than `add_task`'s on purpose (docs/03-agent-design.md).
    It carried over from `split_task`, which applied it per subtask; one child at a time
    means it now applies per call, which is the same rule and a better error message.
    """
    project = await _project(client, "Breaking up")
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Big", "estimatedMinutes": 120},
        )
    ).json()["task"]

    result = await container.domain_tools.add_subtask(
        task["id"],
        "First half",
        "",
        60,
        True,
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


async def test_the_coach_cannot_mark_a_task_complete(
    client: httpx.AsyncClient, container
) -> None:
    """docs/10-risks.md Q1: completion is always the learner's click.

    A guard rather than a line in the instruction, on the same reasoning as
    `discard_task`'s confirmation: a rule the model can decline to follow is not a rule.
    Whether a piece of work is finished is the learner's judgement of their own work, and
    a coach that could tick it off would be marking its own homework.
    """
    project = await _project(client, "Judgement")
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "The exercise", "estimatedMinutes": 45},
        )
    ).json()["task"]
    context = _FakeToolContext("u_alice", project["id"])
    await container.domain_tools.set_task_state(task["id"], "in_progress", context)

    refused = await container.domain_tools.set_task_state(task["id"], "completed", context)

    assert not refused["ok"]
    assert "learner" in refused["error"]["message"]
    assert (await _board(client, project["id"]))[0]["state"] == "in_progress"


async def test_the_coach_cannot_discard_around_the_confirmation(
    client: httpx.AsyncClient, container
) -> None:
    """`set_task_state(state="discarded")` would be the gate's back door.

    `discard_task` is the tool ADK holds behind `require_confirmation`; a second route to
    the same state would make that gate a matter of which tool the model happened to pick.
    """
    project = await _project(client, "The back door")
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Still wanted", "estimatedMinutes": 45},
        )
    ).json()["task"]

    refused = await container.domain_tools.set_task_state(
        task["id"], "discarded", _FakeToolContext("u_alice", project["id"])
    )

    assert not refused["ok"]
    assert "discard_task" in refused["error"]["message"]
    assert (await _board(client, project["id"]))[0]["state"] == "draft"


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

    def __init__(
        self,
        uid: str,
        project_id: str | None,
        default_minutes: int = 45,
        task_id: str | None = None,
    ) -> None:
        self.user_id = uid
        self.state = _FakeState()
        if project_id is not None:
            self.state["temp:coach_project_id"] = project_id
        if task_id is not None:
            self.state["temp:coach_task_id"] = task_id
        self.state["temp:coach_default_minutes"] = default_minutes


# --- the checklist of a task that has been broken down --------------------------------------


async def _composite(client: httpx.AsyncClient, container) -> dict[str, Any]:
    """A task with one subtask holding the whole checklist.

    Reached the way a learner reaches it: give a task some steps, then add a subtask, which
    inherits them (docs/02-data-model.md#task-items).
    """
    project = await _project(client, "Compilers")
    parent = (
        await client.post(
            f"/api/projects/{project['id']}/tasks", json={"title": "Write the parser"}
        )
    ).json()["task"]
    items = (
        await client.post(
            f"/api/tasks/{parent['id']}/items",
            json={
                "items": [
                    {"shortDescription": "Read the grammar"},
                    {"shortDescription": "Write the tokenizer"},
                ]
            },
        )
    ).json()["task"]["items"]
    first = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Tokenizing", "parentTaskId": parent["id"]},
        )
    ).json()["task"]
    second = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Parsing", "parentTaskId": parent["id"]},
        )
    ).json()["task"]
    return {
        "project": project,
        "parent": parent,
        "first": first,
        "second": second,
        "items": items,
        "context": _FakeToolContext("u_alice", project["id"], task_id=parent["id"]),
    }


async def test_an_item_tool_can_reach_a_subtasks_checklist(
    client: httpx.AsyncClient, container
) -> None:
    """The defect this fixes, and it made the coach useless on any broken-down task.

    Item tools took no task id, on the reasoning that a task-scoped session is the only
    place they are useful. But breaking a task down makes the session's task a *parent*, and
    a parent holds no checklist — so `_write_items` refused every call with "this task has
    subtasks", and the steps sitting on the subtask were unreachable. The coach could see
    them (from M4's prompt change) and could not touch them.
    """
    fixture = await _composite(client, container)

    result = await container.domain_tools.complete_task_item(
        fixture["items"][0]["itemId"],
        "you talked me through it",
        fixture["context"],
        subtask_id=fixture["first"]["id"],
    )
    assert result["ok"], result
    assert [item["completed"] for item in result["items"]] == [True, False]


async def test_an_item_tool_refuses_a_task_outside_the_conversation(
    client: httpx.AsyncClient, container
) -> None:
    """The property the no-argument design was protecting, kept.

    The id is bounded rather than free: this conversation's task, or one of its children.
    An unrelated task in the same project is refused — which is what stops the argument
    becoming a way to point a tool at whatever the model names.
    """
    fixture = await _composite(client, container)
    elsewhere = (
        await client.post(
            f"/api/projects/{fixture['project']['id']}/tasks",
            json={"title": "Someone else's task"},
        )
    ).json()["task"]

    result = await container.domain_tools.add_task_items(
        [{"shortDescription": "sneak one in"}],
        fixture["context"],
        subtask_id=elsewhere["id"],
    )
    assert not result["ok"]
    assert "not the one this conversation is about" in result["error"]["message"]


async def test_moving_items_between_subtasks_takes_one_call(
    client: httpx.AsyncClient, container
) -> None:
    """What the learner asked for.

    Redistributing a checklist used to mean deleting from one subtask and re-adding to the
    other — which loses the ticks and the ids, and asks for approval on every removal. One
    call, several items, nothing lost.
    """
    fixture = await _composite(client, container)

    result = await container.domain_tools.move_task_items(
        [fixture["items"][1]["itemId"]],
        fixture["second"]["id"],
        fixture["context"],
        from_subtask_id=fixture["first"]["id"],
    )

    assert result["ok"], result
    assert result["moved"] == 1
    assert [i["shortDescription"] for i in result["from"]["items"]] == ["Read the grammar"]
    assert [i["shortDescription"] for i in result["to"]["items"]] == ["Write the tokenizer"]


async def test_moving_items_is_not_gated_on_the_learners_approval(container) -> None:
    """Unlike `delete_task_item`, and the difference is that nothing is lost.

    Deleting the last outstanding step completes a task by making work *vanish*; moving it
    completes the source because the work is now visibly on another task. Gating it would
    also make redistributing a ten-step checklist ten approvals — the cost that sent people
    back to deleting in the first place.
    """
    gated = {
        tool.name
        for tool in container.domain_tools.as_tools()
        if getattr(tool, "_require_confirmation", False)
    }
    assert "move_task_items" not in gated
    assert "delete_task_item" in gated


async def test_a_subtask_change_also_names_the_task_the_learner_has_open(
    client: httpx.AsyncClient, container
) -> None:
    """The push has to reach the screen that is actually open.

    The client turns each named id into an invalidation of `['task', id]`, and the task
    workspace is keyed on the task the *conversation* is about. `move_task_items` touches
    two subtasks and nothing else, so a frame naming only those would refresh a key nothing
    on screen reads — the learner would watch the coach say it had moved their steps and see
    nothing move.

    Asserted on the frame rather than on the UI, because the frame is the decision: both
    spellings look identical from a test that only checks the items ended up in the right
    place (CLAUDE.md — pin the decision, not the result).
    """
    frames: list[dict[str, Any]] = []

    async def sink(frame: dict[str, Any]) -> None:
        frames.append(frame)

    fixture = await _composite(client, container)
    container.board_updates.attach("u_alice", sink)
    try:
        result = await container.domain_tools.move_task_items(
            [fixture["items"][1]["itemId"]],
            fixture["second"]["id"],
            fixture["context"],
            from_subtask_id=fixture["first"]["id"],
        )
        assert result["ok"], result
    finally:
        container.board_updates.detach("u_alice", sink)

    named = frames[0]["taskIds"]
    assert fixture["first"]["id"] in named
    assert fixture["second"]["id"] in named
    # The one the workspace is keyed on.
    assert fixture["parent"]["id"] in named
