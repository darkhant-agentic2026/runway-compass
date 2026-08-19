"""`discard_task` asks before it acts.

docs/03-agent-design.md marks `discard_task` "**requires user confirmation** in
interactive mode". The gate is ADK's `require_confirmation`, so it holds whether or not
the model cooperates: the tool call becomes an `adk_request_confirmation` function call,
the invocation ends there, and the body runs only when a matching function *response*
arrives.

Both halves are worth a test, and for different reasons. The refusal half is the security
property — a coach that could delete work on its own initiative is the failure the gate
exists to prevent. The *resume* half is the one that rots quietly: an unanswerable gate
looks exactly like a working one until somebody tries to say yes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from coach.integrations.stub_model import StubModel
from coach.services.turns import CONFIRMATION_FUNCTION_NAME


@pytest.fixture
def stub_model(container, monkeypatch: pytest.MonkeyPatch) -> StubModel:
    monkeypatch.setenv("STUB_MODEL_DELAY_MS", "0")
    model = StubModel()
    container.runners.set_model(model)
    return model


async def _settle(client: httpx.AsyncClient, turn_id: str) -> None:
    async with asyncio.timeout(20):
        while (await client.get(f"/api/turns/{turn_id}")).json()["status"] == "running":
            await asyncio.sleep(0.02)


async def _turn(client: httpx.AsyncClient, session_id: str, **body: Any) -> None:
    response = await client.post(f"/api/sessions/{session_id}/turns", json=body)
    assert response.status_code == 202, response.text
    await _settle(client, response.json()["turnId"])


async def _function_calls(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    events = (await client.get(f"/api/sessions/{session_id}/events?limit=100")).json()
    return [
        part["function_call"]
        for stored in events["events"]
        for part in (stored["event"].get("content") or {}).get("parts", [])
        if "function_call" in part
    ]


async def _states(client: httpx.AsyncClient, project_id: str) -> list[str]:
    board = (
        await client.get(f"/api/projects/{project_id}/tasks?include_discarded=true")
    ).json()
    return [task["state"] for task in board["tasks"]]


@pytest.fixture
async def project_with_a_task(client: httpx.AsyncClient) -> dict[str, Any]:
    project = (await client.post("/api/projects", json={"title": "Housekeeping"})).json()
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Something obsolete", "estimatedMinutes": 45},
        )
    ).json()["task"]
    session_id = (await client.post(f"/api/tasks/{task['id']}/session")).json()["session"]["id"]
    return {"project": project, "task": task, "session_id": session_id}


async def test_asking_to_discard_does_not_discard(
    client: httpx.AsyncClient, stub_model: StubModel, project_with_a_task: dict[str, Any]
) -> None:
    """The turn ends with a question, and the board is untouched."""
    session_id = project_with_a_task["session_id"]
    await _turn(client, session_id, text="Please discard that task")

    calls = await _function_calls(client, session_id)
    assert [call["name"] for call in calls] == ["discard_task", CONFIRMATION_FUNCTION_NAME]
    assert await _states(client, project_with_a_task["project"]["id"]) == ["draft"]


async def test_confirming_lets_it_through(
    client: httpx.AsyncClient, stub_model: StubModel, project_with_a_task: dict[str, Any]
) -> None:
    """Answering the question resumes the original call.

    The answer is a turn like any other, carrying the id of the call it answers — which
    is what `apps/web` reads out of the transcript to render the two buttons.
    """
    session_id = project_with_a_task["session_id"]
    await _turn(client, session_id, text="Please discard that task")

    request = next(
        call
        for call in await _function_calls(client, session_id)
        if call["name"] == CONFIRMATION_FUNCTION_NAME
    )
    await _turn(
        client,
        session_id,
        confirmation={"functionCallId": request["id"], "confirmed": True},
    )

    assert await _states(client, project_with_a_task["project"]["id"]) == ["discarded"]


async def test_refusing_leaves_the_task_alone(
    client: httpx.AsyncClient, stub_model: StubModel, project_with_a_task: dict[str, Any]
) -> None:
    """`confirmed: false` is an answer, not a timeout: the tool is told no and moves on."""
    session_id = project_with_a_task["session_id"]
    await _turn(client, session_id, text="Please discard that task")

    request = next(
        call
        for call in await _function_calls(client, session_id)
        if call["name"] == CONFIRMATION_FUNCTION_NAME
    )
    await _turn(
        client,
        session_id,
        confirmation={"functionCallId": request["id"], "confirmed": False},
    )

    assert await _states(client, project_with_a_task["project"]["id"]) == ["draft"]


async def test_a_confirmation_alone_is_a_valid_turn(
    client: httpx.AsyncClient, stub_model: StubModel, project_with_a_task: dict[str, Any]
) -> None:
    """No text and no attachment, and it still starts.

    `start` rejects an empty turn — "a turn needs text, an attachment, or both" — and a
    confirmation is neither. Answering a question by pressing a button sends exactly that
    and nothing else, so the check has to count it as content.
    """
    session_id = project_with_a_task["session_id"]
    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json={"confirmation": {"functionCallId": "unknown", "confirmed": False}},
    )
    assert response.status_code == 202, response.text
    await _settle(client, response.json()["turnId"])
