"""Task endpoints, over HTTP: CRUD, ordering, splitting, and idempotency."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from coach.core.clock import now


async def _project(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/projects", json={"title": "Board"})
    project_id: str = response.json()["id"]
    return project_id


async def _add(client: httpx.AsyncClient, project_id: str, title: str, **body: object) -> dict:
    response = await client.post(
        f"/api/projects/{project_id}/tasks", json={"title": title, **body}
    )
    assert response.status_code == 201, response.text
    return response.json()["task"]


async def _board(client: httpx.AsyncClient, project_id: str, **params: object) -> list[dict]:
    response = await client.get(f"/api/projects/{project_id}/tasks", params=params)
    assert response.status_code == 200, response.text
    tasks: list[dict] = response.json()["tasks"]
    return tasks


async def test_creating_tasks_appends_in_order(client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    for title in ("first", "second", "third"):
        await _add(client, project_id, title)

    assert [t["title"] for t in await _board(client, project_id)] == [
        "first",
        "second",
        "third",
    ]


async def test_a_new_task_carries_the_documented_defaults(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    task = await _add(client, project_id, "t")
    assert task["state"] == "draft"
    assert task["origin"] == "user"
    assert task["needsResearch"] is True
    assert task["researchStatus"] == "none"
    assert task["estimatedMinutes"] == 45
    assert task["rollup"] is None


async def test_after_task_id_inserts_rather_than_appends(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    first = await _add(client, project_id, "first")
    await _add(client, project_id, "third")
    await _add(client, project_id, "second", afterTaskId=first["id"])

    assert [t["title"] for t in await _board(client, project_id)] == [
        "first",
        "second",
        "third",
    ]


async def test_reorder_moves_a_task_and_returns_the_updated_document(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    a = await _add(client, project_id, "a")
    b = await _add(client, project_id, "b")
    c = await _add(client, project_id, "c")

    response = await client.post(
        f"/api/tasks/{c['id']}/reorder", json={"beforeTaskId": a["id"]}
    )
    assert response.status_code == 200
    assert response.json()["task"]["order"] < a["order"]

    assert [t["title"] for t in await _board(client, project_id)] == ["c", "a", "b"]

    await client.post(f"/api/tasks/{a['id']}/reorder", json={"afterTaskId": b["id"]})
    assert [t["title"] for t in await _board(client, project_id)] == ["c", "b", "a"]


async def test_reorder_requires_exactly_one_anchor(client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    a = await _add(client, project_id, "a")
    b = await _add(client, project_id, "b")

    assert (await client.post(f"/api/tasks/{a['id']}/reorder", json={})).status_code == 422
    both = await client.post(
        f"/api/tasks/{a['id']}/reorder",
        json={"afterTaskId": b["id"], "beforeTaskId": b["id"]},
    )
    assert both.status_code == 422


async def test_reorder_against_itself_is_refused(client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    a = await _add(client, project_id, "a")
    response = await client.post(f"/api/tasks/{a['id']}/reorder", json={"afterTaskId": a["id"]})
    assert response.status_code == 422


async def test_only_one_document_is_rewritten_by_a_reorder(
    client: httpx.AsyncClient,
) -> None:
    """ "a single-document write, not a renumbering of the board"
    (docs/02-data-model.md#ordering)."""
    project_id = await _project(client)
    tasks = [await _add(client, project_id, t) for t in "abcdef"]
    before = {t["id"]: t["order"] for t in await _board(client, project_id)}

    await client.post(
        f"/api/tasks/{tasks[-1]['id']}/reorder", json={"beforeTaskId": tasks[0]["id"]}
    )
    after = {t["id"]: t["order"] for t in await _board(client, project_id)}

    changed = [task_id for task_id in before if before[task_id] != after[task_id]]
    assert changed == [tasks[-1]["id"]]


async def test_state_changes_go_through_the_state_machine(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    task = await _add(client, project_id, "t")

    # A brand-new task is `draft`, and deferring is only reachable from `in_progress`.
    illegal = await client.post(f"/api/tasks/{task['id']}/state", json={"state": "postponed"})
    assert illegal.status_code == 409
    assert illegal.json()["type"] == "/problems/invalid-transition"

    started = await client.post(f"/api/tasks/{task['id']}/state", json={"state": "in_progress"})
    assert started.status_code == 200
    assert started.json()["task"]["state"] == "in_progress"
    assert started.json()["project"]["nextUpTaskId"] == task["id"]


async def test_postponing_until_a_future_time(client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    task = await _add(client, project_id, "t")
    await client.post(f"/api/tasks/{task['id']}/state", json={"state": "in_progress"})

    when = (now() + timedelta(days=2)).isoformat()
    response = await client.post(
        f"/api/tasks/{task['id']}/state",
        json={"state": "postponed_until", "postponedUntil": when},
    )
    assert response.status_code == 200
    assert response.json()["task"]["state"] == "postponed_until"
    assert response.json()["task"]["postponedUntil"] is not None


async def test_postponed_until_without_a_timestamp_is_refused(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    task = await _add(client, project_id, "t")
    await client.post(f"/api/tasks/{task['id']}/state", json={"state": "in_progress"})
    response = await client.post(
        f"/api/tasks/{task['id']}/state", json={"state": "postponed_until"}
    )
    assert response.status_code == 422


async def test_board_filters_default_to_hiding_completed_and_discarded(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    keep = await _add(client, project_id, "keep")
    done = await _add(client, project_id, "done")
    gone = await _add(client, project_id, "gone")

    await client.post(f"/api/tasks/{done['id']}/state", json={"state": "in_progress"})
    await client.post(f"/api/tasks/{done['id']}/state", json={"state": "completed"})
    await client.post(f"/api/tasks/{gone['id']}/state", json={"state": "discarded"})

    assert [t["id"] for t in await _board(client, project_id)] == [keep["id"]]

    everything = await _board(
        client, project_id, include_completed=True, include_discarded=True
    )
    assert len(everything) == 3


async def test_hiding_postponed_is_opt_in(client: httpx.AsyncClient) -> None:
    """docs/06-frontend.md: "Hide postponed" defaults off."""
    project_id = await _project(client)
    task = await _add(client, project_id, "t")
    await client.post(f"/api/tasks/{task['id']}/state", json={"state": "in_progress"})
    await client.post(f"/api/tasks/{task['id']}/state", json={"state": "postponed"})

    assert len(await _board(client, project_id)) == 1
    assert await _board(client, project_id, include_postponed=False) == []


async def test_a_hidden_parent_stays_on_the_board_while_it_has_visible_children(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    parent = await _add(client, project_id, "parent")
    await client.post(
        f"/api/tasks/{parent['id']}/split",
        json={
            "subtasks": [
                {"title": "a", "estimatedMinutes": 30},
                {"title": "b", "estimatedMinutes": 30},
            ]
        },
    )
    await client.post(f"/api/tasks/{parent['id']}/state", json={"state": "in_progress"})
    await client.post(f"/api/tasks/{parent['id']}/state", json={"state": "completed"})

    board = await _board(client, project_id)
    assert [t["title"] for t in board] == ["parent"]
    assert [s["title"] for s in board[0]["subtasks"]] == ["a", "b"]


async def test_splitting_nests_subtasks_and_is_capped(client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    parent = await _add(client, project_id, "big", estimatedMinutes=240)

    too_few = await client.post(
        f"/api/tasks/{parent['id']}/split",
        json={"subtasks": [{"title": "only", "estimatedMinutes": 30}]},
    )
    assert too_few.status_code == 422

    too_many = await client.post(
        f"/api/tasks/{parent['id']}/split",
        json={"subtasks": [{"title": f"s{i}", "estimatedMinutes": 15} for i in range(9)]},
    )
    assert too_many.status_code == 422

    ok = await client.post(
        f"/api/tasks/{parent['id']}/split",
        json={
            "subtasks": [
                {"title": "a", "estimatedMinutes": 60},
                {"title": "b", "estimatedMinutes": 60},
            ]
        },
    )
    assert ok.status_code == 201
    assert ok.json()["task"]["rollup"]["subtaskCount"] == 2


async def test_nesting_stops_at_one_level(client: httpx.AsyncClient) -> None:
    """ "A subtask cannot have subtasks" (docs/02-data-model.md)."""
    project_id = await _project(client)
    parent = await _add(client, project_id, "parent")
    split = await client.post(
        f"/api/tasks/{parent['id']}/split",
        json={
            "subtasks": [
                {"title": "a", "estimatedMinutes": 30},
                {"title": "b", "estimatedMinutes": 30},
            ]
        },
    )
    child_id = split.json()["task"]["subtasks"][0]["id"]

    nested = await client.post(
        f"/api/projects/{project_id}/tasks",
        json={"title": "grandchild", "parentTaskId": child_id},
    )
    assert nested.status_code == 422

    resplit = await client.post(
        f"/api/tasks/{child_id}/split",
        json={
            "subtasks": [
                {"title": "x", "estimatedMinutes": 10},
                {"title": "y", "estimatedMinutes": 10},
            ]
        },
    )
    assert resplit.status_code == 422


async def test_splitting_twice_is_refused(client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    parent = await _add(client, project_id, "parent")
    body = {
        "subtasks": [
            {"title": "a", "estimatedMinutes": 30},
            {"title": "b", "estimatedMinutes": 30},
        ]
    }
    assert (await client.post(f"/api/tasks/{parent['id']}/split", json=body)).status_code == 201
    second = await client.post(f"/api/tasks/{parent['id']}/split", json=body)
    assert second.status_code == 409


async def test_tasks_are_isolated_per_user(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    task = await _add(client, project_id, "secret")

    assert (await other_client.get(f"/api/tasks/{task['id']}")).status_code == 404
    assert (
        await other_client.patch(f"/api/tasks/{task['id']}", json={"title": "pwned"})
    ).status_code == 404
    assert (
        await other_client.post(f"/api/tasks/{task['id']}/state", json={"state": "discarded"})
    ).status_code == 404
    assert (await other_client.get(f"/api/projects/{project_id}/tasks")).status_code == 404

    unchanged = (await client.get(f"/api/tasks/{task['id']}")).json()["task"]
    assert unchanged["title"] == "secret"
    assert unchanged["state"] == "draft"


async def test_a_mutation_returns_the_parent_and_project_for_optimistic_reconciliation(
    client: httpx.AsyncClient,
) -> None:
    """docs/04-api-contract.md: enough to reconcile without a refetch."""
    project_id = await _project(client)
    parent = await _add(client, project_id, "parent")
    split = await client.post(
        f"/api/tasks/{parent['id']}/split",
        json={
            "subtasks": [
                {"title": "a", "estimatedMinutes": 30},
                {"title": "b", "estimatedMinutes": 60},
            ]
        },
    )
    child_id = split.json()["task"]["subtasks"][0]["id"]

    response = await client.patch(f"/api/tasks/{child_id}", json={"estimatedMinutes": 120})
    body = response.json()
    assert body["task"]["estimatedMinutes"] == 120
    assert body["parent"]["id"] == parent["id"]
    assert body["parent"]["rollup"]["totalEstimatedMinutes"] == 180
    assert body["project"]["counts"]["openMinutes"] == 180


async def test_unknown_task_ids_are_not_found(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/tasks/k_nope")).status_code == 404


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/projects/{project_id}/tasks", {"title": "made once"}),
        ("/api/me/prefs", {"defaultTaskMinutes": 60}),
    ],
)
async def test_idempotency_key_replays_the_first_response(
    client: httpx.AsyncClient, path: str, body: dict
) -> None:
    """docs/04-api-contract.md: "All mutating endpoints accept `Idempotency-Key`"."""
    project_id = await _project(client)
    url = path.format(project_id=project_id)
    headers = {"Idempotency-Key": "key-abc-123"}

    first = await client.request(
        "POST" if "tasks" in path else "PATCH", url, json=body, headers=headers
    )
    second = await client.request(
        "POST" if "tasks" in path else "PATCH", url, json=body, headers=headers
    )

    assert first.status_code == second.status_code
    assert first.json() == second.json()
    assert second.headers.get("Idempotent-Replay") == "true"
    assert first.headers.get("Idempotent-Replay") is None


async def test_a_replayed_create_does_not_create_a_second_task(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    headers = {"Idempotency-Key": "create-once"}
    for _ in range(3):
        await client.post(
            f"/api/projects/{project_id}/tasks", json={"title": "once"}, headers=headers
        )
    assert len(await _board(client, project_id)) == 1


async def test_the_same_key_on_a_different_endpoint_is_a_different_operation(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    headers = {"Idempotency-Key": "shared"}
    await client.post(f"/api/projects/{project_id}/tasks", json={"title": "a"}, headers=headers)
    await client.patch("/api/me/prefs", json={"defaultTaskMinutes": 15}, headers=headers)

    assert (await client.get("/api/me")).json()["globalPrefs"]["defaultTaskMinutes"] == 15
    assert len(await _board(client, project_id)) == 1


async def test_idempotency_keys_do_not_leak_between_users(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    headers = {"Idempotency-Key": "same-key"}
    mine = await client.post("/api/projects", json={"title": "mine"}, headers=headers)
    theirs = await other_client.post("/api/projects", json={"title": "theirs"}, headers=headers)
    assert mine.json()["id"] != theirs.json()["id"]
    assert theirs.json()["ownerUid"] == "u_mallory"


async def test_requests_without_a_key_are_not_deduplicated(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client)
    for _ in range(2):
        await client.post(f"/api/projects/{project_id}/tasks", json={"title": "dup"})
    assert len(await _board(client, project_id)) == 2
