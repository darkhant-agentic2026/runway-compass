"""`POST`/`DELETE /api/tasks/{id}/research-request` — the learner's queued research.

docs/04-api-contract.md#post--delete-apitasksidresearch-request. The endpoint is small; what
is worth testing is the *pair* of fields it writes, because the scheduler splits its work on
exactly that pair (docs/02-data-model.md) and a task with one and not the other is a request
nothing can order or a request that already ran.
"""

from __future__ import annotations

from typing import Any

import httpx

from coach.services.models import ResearchStatus, TaskState


async def _task(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    project = (await client.post("/api/projects", json={"title": "Async Python"})).json()
    response = await client.post(
        f"/api/projects/{project['id']}/tasks",
        json={"title": "Structured concurrency", "estimatedMinutes": 45, **body},
    )
    return {"project": project, "task": response.json()["task"]}


async def test_queueing_writes_the_status_and_the_timestamp_together(
    client: httpx.AsyncClient,
) -> None:
    fixture = await _task(client)

    task = (await client.post(f"/api/tasks/{fixture['task']['id']}/research-request")).json()[
        "task"
    ]

    assert task["researchStatus"] == ResearchStatus.PENDING.value
    assert task["researchRequestedAt"] is not None


async def test_cancelling_clears_both(client: httpx.AsyncClient) -> None:
    fixture = await _task(client)
    await client.post(f"/api/tasks/{fixture['task']['id']}/research-request")

    task = (await client.delete(f"/api/tasks/{fixture['task']['id']}/research-request")).json()[
        "task"
    ]

    assert task["researchStatus"] == ResearchStatus.NONE.value
    assert task["researchRequestedAt"] is None


async def test_queueing_twice_keeps_the_original_timestamp(
    client: httpx.AsyncClient,
) -> None:
    """A double-click must not send a task to the back of its own queue.

    The queue is ordered by `researchRequestedAt`, so a second press that refreshed it
    would be indistinguishable from cancelling and re-queueing — a bug that only shows up
    under a backlog, which is the only time the order matters at all.
    """
    fixture = await _task(client)
    first = (await client.post(f"/api/tasks/{fixture['task']['id']}/research-request")).json()[
        "task"
    ]

    second = (await client.post(f"/api/tasks/{fixture['task']['id']}/research-request")).json()[
        "task"
    ]

    assert second["researchRequestedAt"] == first["researchRequestedAt"]


async def test_cancelling_a_task_that_is_not_queued_is_a_no_op(
    client: httpx.AsyncClient,
) -> None:
    """The caller asked for an empty queue and the queue is empty."""
    fixture = await _task(client)

    response = await client.delete(f"/api/tasks/{fixture['task']['id']}/research-request")

    assert response.status_code == 200
    assert response.json()["task"]["researchStatus"] == ResearchStatus.NONE.value


async def test_a_composite_task_is_refused(client: httpx.AsyncClient) -> None:
    """Its subtasks are its plan, and each is researched on its own.

    The same rule the inline trigger applies — stated in both places rather than shared,
    because they are two endpoints and a learner can reach either first.
    """
    fixture = await _task(client)
    await client.post(
        f"/api/projects/{fixture['project']['id']}/tasks",
        json={
            "title": "First piece",
            "estimatedMinutes": 20,
            "parentTaskId": fixture["task"]["id"],
        },
    )

    response = await client.post(f"/api/tasks/{fixture['task']['id']}/research-request")

    assert response.status_code == 422
    assert "subtask" in response.json()["detail"].lower()


async def test_a_discarded_task_is_refused(client: httpx.AsyncClient) -> None:
    fixture = await _task(client)
    await client.post(
        f"/api/tasks/{fixture['task']['id']}/state",
        json={"state": TaskState.DISCARDED.value},
    )

    response = await client.post(f"/api/tasks/{fixture['task']['id']}/research-request")

    assert response.status_code == 422


async def test_a_queued_task_does_not_auto_complete(
    client: httpx.AsyncClient, container, alice
) -> None:
    """Half of invariant 6, with a writer for the first time.

    `researchStatus ∈ {pending, in_progress}` blocks the derivation, because more items are
    about to arrive. Before M5 nothing ever wrote `pending`, so this branch of
    `derive_state` had no caller at all.
    """
    fixture = await _task(client)
    task_id = fixture["task"]["id"]
    added = (
        await client.post(
            f"/api/tasks/{task_id}/items",
            json={"items": [{"shortDescription": "Read the guide"}]},
        )
    ).json()["task"]
    await client.post(f"/api/tasks/{task_id}/research-request")

    ticked = (
        await client.patch(
            f"/api/tasks/{task_id}/items/{added['items'][0]['itemId']}",
            json={"completed": True},
        )
    ).json()["task"]

    assert ticked["state"] != TaskState.COMPLETED.value

    # And it completes the moment research settles.
    await client.delete(f"/api/tasks/{task_id}/research-request")
    settled = await container.tasks.resolve(alice, task_id)
    assert settled.state is TaskState.COMPLETED


async def test_another_users_task_is_not_queueable(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    fixture = await _task(client)

    response = await other_client.post(f"/api/tasks/{fixture['task']['id']}/research-request")

    assert response.status_code == 404
