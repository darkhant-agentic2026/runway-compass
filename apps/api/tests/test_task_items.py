"""A leaf task's checklist, and the two states derived from it.

docs/02-data-model.md#task-items and invariants 1 and 6 of
docs/02-data-model.md#task-state-machine. The derivation lives in
`coach.services.rollups.derive_state` and is applied by every `TaskService` mutation, so it
is tested at both altitudes: as a pure function over a board, where every clause of the
rule can be isolated, and over HTTP, where the thing being checked is that the derived
state actually reaches the response the client reconciles against.

**Each clause of invariant 6 gets its own assertion, deliberately.** The rule is
`all(items.completed) and items != [] and researchStatus not in {pending, in_progress}`,
and every one of those three conjuncts is a way to complete a task the learner has not
finished. A single happy-path test would pass with any two of them.
"""

from __future__ import annotations

import httpx
import pytest

from coach.core.errors import ValidationProblem
from coach.services.models import ResearchStatus, Task, TaskItem, TaskState
from coach.services.rollups import derive_state


def _item(**overrides: object) -> TaskItem:
    return TaskItem.model_validate(
        {"itemId": "i_1", "shortDescription": "Read the docs", **overrides}
    )


def _task(**overrides: object) -> Task:
    return Task.model_validate(
        {
            "id": "k_1",
            "projectId": "p_1",
            "ownerUid": "u_alice",
            "title": "A task",
            "order": "a0",
            **overrides,
        }
    )


# --- the derivation, as a pure function ------------------------------------------------


def test_a_draft_with_no_plan_stays_a_draft() -> None:
    assert derive_state(_task(), []) is TaskState.DRAFT


def test_the_first_item_promotes_a_draft() -> None:
    """Invariant 1, by items."""
    task = _task(items=[_item()])
    assert derive_state(task, []) is TaskState.NOT_STARTED


def test_the_first_subtask_promotes_a_draft() -> None:
    """Invariant 1, by subtasks. The other half of "acquires a plan"."""
    task = _task()
    child = _task(id="k_2", parentTaskId="k_1")
    assert derive_state(task, [child]) is TaskState.NOT_STARTED


def test_losing_every_item_does_not_send_a_task_back_to_draft() -> None:
    """Invariant 1 runs in one direction only.

    By the time the items are gone the learner has seen a plan, and a task silently
    regressing to "no plan yet" is worse than a stale state.
    """
    task = _task(state=TaskState.NOT_STARTED, items=[])
    assert derive_state(task, []) is TaskState.NOT_STARTED


def test_a_finished_checklist_completes_the_task() -> None:
    """Invariant 6, the whole rule holding."""
    task = _task(
        state=TaskState.IN_PROGRESS,
        items=[_item(completed=True), _item(itemId="i_2", completed=True)],
    )
    assert derive_state(task, []) is TaskState.COMPLETED


def test_a_finished_checklist_does_not_complete_while_research_is_running() -> None:
    """Invariant 6's second conjunct, isolated.

    Without it, the first thin report's list gets ticked off and the task completes while
    the run that was about to add five more items is still going.
    """
    for status in (ResearchStatus.PENDING, ResearchStatus.IN_PROGRESS):
        task = _task(
            state=TaskState.IN_PROGRESS,
            researchStatus=status,
            items=[_item(completed=True)],
        )
        assert derive_state(task, []) is TaskState.IN_PROGRESS, status


def test_an_empty_checklist_never_completes_a_task() -> None:
    """Invariant 6's third conjunct, isolated.

    `all([])` is `True`, so leaving this out completes every task on the board the moment
    anything touches it — and every task starts with an empty list.
    """
    task = _task(state=TaskState.NOT_STARTED, items=[])
    assert derive_state(task, []) is TaskState.NOT_STARTED


def test_a_parent_never_auto_completes() -> None:
    """A parent's plan is its subtasks, and invariant 4 makes completing one with
    unfinished children a decision the UI puts to the learner rather than a rule."""
    parent = _task(state=TaskState.IN_PROGRESS)
    children = [
        _task(id="k_2", parentTaskId="k_1", state=TaskState.COMPLETED),
        _task(id="k_3", parentTaskId="k_1", state=TaskState.COMPLETED),
    ]
    assert derive_state(parent, children) is TaskState.IN_PROGRESS


def test_un_ticking_an_item_reopens_a_completed_task() -> None:
    """The derivation runs both ways, or a mis-click becomes a state to fix by hand."""
    task = _task(
        state=TaskState.COMPLETED,
        items=[_item(completed=True), _item(itemId="i_2", completed=False)],
    )
    assert derive_state(task, []) is TaskState.IN_PROGRESS


@pytest.mark.parametrize(
    "state", [TaskState.POSTPONED, TaskState.POSTPONED_UNTIL, TaskState.DISCARDED]
)
def test_a_deliberate_state_is_never_overridden(state: TaskState) -> None:
    """Postponing and discarding are the learner's decisions.

    A derivation that overrode them would bring a discarded task back onto the board
    because someone ticked a stale checkbox.
    """
    task = _task(state=state, items=[_item(completed=True)])
    assert derive_state(task, []) is state


# --- over HTTP -------------------------------------------------------------------------


async def _project(client: httpx.AsyncClient) -> str:
    return str((await client.post("/api/projects", json={"title": "Board"})).json()["id"])


async def _task_with_items(client: httpx.AsyncClient, *count: str) -> tuple[str, list[dict]]:
    project_id = await _project(client)
    task = (
        await client.post(
            f"/api/projects/{project_id}/tasks", json={"title": "Structured concurrency"}
        )
    ).json()["task"]
    response = await client.post(
        f"/api/tasks/{task['id']}/items",
        json={"items": [{"shortDescription": title} for title in count]},
    )
    assert response.status_code == 201, response.text
    return task["id"], response.json()["task"]["items"]


async def test_adding_items_promotes_the_task_and_returns_the_whole_task(
    client: httpx.AsyncClient,
) -> None:
    task_id, items = await _task_with_items(client, "Read §3", "Do the exercise")

    detail = (await client.get(f"/api/tasks/{task_id}")).json()["task"]
    assert detail["state"] == "not_started"
    assert [i["shortDescription"] for i in items] == ["Read §3", "Do the exercise"]
    assert all(i["completed"] is False for i in items)
    assert all(i["sourceReportId"] is None for i in items)


async def test_ticking_every_item_completes_the_task(client: httpx.AsyncClient) -> None:
    """Invariant 6 end to end, and the reason `PATCH` returns the task rather than the item.

    Nothing here touches a state control: the learner ticks two checkboxes and the task
    finishes. That is the product behaviour M4 added, and it is also what keeps Q1 honest —
    the last thing to happen before the task completed was still a click.
    """
    task_id, items = await _task_with_items(client, "Read §3", "Do the exercise")

    first = await client.patch(
        f"/api/tasks/{task_id}/items/{items[0]['itemId']}", json={"completed": True}
    )
    assert first.status_code == 200
    assert first.json()["task"]["state"] == "not_started"

    second = await client.patch(
        f"/api/tasks/{task_id}/items/{items[1]['itemId']}", json={"completed": True}
    )
    assert second.json()["task"]["state"] == "completed"
    assert second.json()["task"]["completedAt"] is not None
    # The project's derived numbers move on the same write, which is why they are in the
    # response at all.
    assert second.json()["project"]["counts"]["completed"] == 1


async def test_un_ticking_reopens_the_task(client: httpx.AsyncClient) -> None:
    task_id, items = await _task_with_items(client, "Read §3")
    await client.patch(
        f"/api/tasks/{task_id}/items/{items[0]['itemId']}", json={"completed": True}
    )

    reopened = await client.patch(
        f"/api/tasks/{task_id}/items/{items[0]['itemId']}", json={"completed": False}
    )
    assert reopened.json()["task"]["state"] == "in_progress"
    assert reopened.json()["task"]["completedAt"] is None


async def test_reopening_by_hand_is_not_undone_by_the_derivation(
    client: httpx.AsyncClient,
) -> None:
    """The `state_set_explicitly` exemption, which exists for exactly this.

    Without it the learner presses "Reopen" on a task whose checklist is fully ticked, the
    derivation puts it straight back to `completed` inside the same transaction, and the
    button appears to do nothing at all — visibly, repeatably, with no error anywhere.
    """
    task_id, items = await _task_with_items(client, "Read §3")
    await client.patch(
        f"/api/tasks/{task_id}/items/{items[0]['itemId']}", json={"completed": True}
    )

    reopened = await client.post(f"/api/tasks/{task_id}/state", json={"state": "not_started"})
    assert reopened.status_code == 200
    assert reopened.json()["task"]["state"] == "not_started"
    assert (await client.get(f"/api/tasks/{task_id}")).json()["task"]["state"] == "not_started"


async def test_items_are_reordered_and_deleted(client: httpx.AsyncClient) -> None:
    task_id, items = await _task_with_items(client, "a", "b", "c")

    moved = await client.post(
        f"/api/tasks/{task_id}/items/{items[2]['itemId']}/reorder",
        json={"beforeItemId": items[0]["itemId"]},
    )
    assert [i["shortDescription"] for i in moved.json()["task"]["items"]] == ["c", "a", "b"]

    removed = await client.delete(f"/api/tasks/{task_id}/items/{items[0]['itemId']}")
    assert [i["shortDescription"] for i in removed.json()["task"]["items"]] == ["c", "b"]


async def test_a_task_with_subtasks_refuses_items(client: httpx.AsyncClient) -> None:
    """`items` and `rollup` are mutually exclusive: a parent's plan is its subtasks."""
    project_id = await _project(client)
    parent = (
        await client.post(f"/api/projects/{project_id}/tasks", json={"title": "Big"})
    ).json()["task"]
    await client.post(
        f"/api/tasks/{parent['id']}/split",
        json={
            "subtasks": [
                {"title": "a", "estimatedMinutes": 30},
                {"title": "b", "estimatedMinutes": 30},
            ]
        },
    )

    refused = await client.post(
        f"/api/tasks/{parent['id']}/items",
        json={"items": [{"shortDescription": "sneak one in"}]},
    )
    assert refused.status_code == 422


async def test_a_task_with_items_refuses_a_split(client: httpx.AsyncClient) -> None:
    """The same exclusion from the other side, and the more damaging direction.

    Splitting silently would drop a checklist the learner may already have worked through.
    """
    task_id, _ = await _task_with_items(client, "Read §3")

    refused = await client.post(
        f"/api/tasks/{task_id}/split",
        json={
            "subtasks": [
                {"title": "a", "estimatedMinutes": 30},
                {"title": "b", "estimatedMinutes": 30},
            ]
        },
    )
    assert refused.status_code == 409


async def test_items_are_isolated_per_user(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    task_id, items = await _task_with_items(client, "Read §3")
    item_id = items[0]["itemId"]

    assert (
        await other_client.patch(
            f"/api/tasks/{task_id}/items/{item_id}", json={"completed": True}
        )
    ).status_code == 404
    assert (
        await other_client.delete(f"/api/tasks/{task_id}/items/{item_id}")
    ).status_code == 404
    assert (
        await other_client.post(
            f"/api/tasks/{task_id}/items", json={"items": [{"shortDescription": "x"}]}
        )
    ).status_code == 404


# --- replacement by a research re-run ---------------------------------------------------


async def test_a_re_run_keeps_what_the_learner_has_already_done(
    container, alice, client: httpx.AsyncClient
) -> None:
    """docs/02-data-model.md#task-items: a re-run replaces the checklist, and completion
    survives for an item whose `shortDescription` and `url` are unchanged.

    Asked of the service rather than over HTTP because `replace_items` is the research
    path's entry point and has no endpoint of its own — `post_research_report` is its only
    caller.
    """
    project_id = await _project(client)
    task = (await client.post(f"/api/projects/{project_id}/tasks", json={"title": "t"})).json()[
        "task"
    ]
    task_id = task["id"]

    # The first run's checklist, carrying a `sourceReportId` — which is what makes these
    # two items the run's to replace, where the hand-added one below is not.
    first_run = await container.tasks.replace_items(
        alice,
        task_id,
        [{"shortDescription": "Read §3"}, {"shortDescription": "Watch the talk"}],
        source_report_id="rep_1",
    )
    await client.patch(
        f"/api/tasks/{task_id}/items/{first_run.items[0].item_id}",
        json={"completed": True},
    )
    await container.tasks.add_items(alice, task_id, [{"shortDescription": "My own note"}])

    task = await container.tasks.replace_items(
        alice,
        task_id,
        [
            {"shortDescription": "Read §3"},
            {"shortDescription": "Do the new exercise"},
        ],
        source_report_id="rep_2",
    )

    by_description = {i.short_description: i for i in task.items}
    # Kept, still ticked, and still the same id — so a thumbs-down recorded against the
    # original recommendation still lines up.
    assert by_description["Read §3"].completed is True
    assert by_description["Read §3"].item_id == first_run.items[0].item_id
    # Superseded, and gone.
    assert "Watch the talk" not in by_description
    # Hand-added, and never dropped by a run that knows nothing about it.
    assert by_description["My own note"].source_report_id is None
    assert [i.short_description for i in task.items] == [
        "Read §3",
        "Do the new exercise",
        "My own note",
    ]


async def test_an_item_needs_a_short_description(container, alice, client) -> None:
    project_id = await _project(client)
    task = (await client.post(f"/api/projects/{project_id}/tasks", json={"title": "t"})).json()[
        "task"
    ]
    with pytest.raises(ValidationProblem):
        await container.tasks.add_items(alice, task["id"], [{"details": "no title"}])


# --- a subtask inherits the checklist ------------------------------------------------------


async def test_the_first_subtask_inherits_the_parents_checklist(
    client: httpx.AsyncClient,
) -> None:
    """docs/02-data-model.md#task-items: a task's plan is its items or its subtasks.

    So the moment a leaf gains a child it has to give the items away, and the child is the
    only place they can go — dropping them would take a checklist the learner may have
    half-finished off the screen, on a write they asked for for an unrelated reason.
    """
    task_id, items = await _task_with_items(client, "Read §3", "Do the exercise")
    await client.patch(
        f"/api/tasks/{task_id}/items/{items[0]['itemId']}", json={"completed": True}
    )

    project_id = (await client.get(f"/api/tasks/{task_id}")).json()["task"]["projectId"]
    child = (
        await client.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "The first half", "parentTaskId": task_id},
        )
    ).json()["task"]

    assert [i["shortDescription"] for i in child["items"]] == ["Read §3", "Do the exercise"]
    # Including what the learner had already done. Same ids, so nothing that referenced an
    # item — a report's feedback, a chip in the transcript — is left pointing at nothing.
    assert [i["itemId"] for i in child["items"]] == [i["itemId"] for i in items]
    assert child["items"][0]["completed"] is True

    parent = (await client.get(f"/api/tasks/{task_id}")).json()["task"]
    assert parent["items"] == []
    assert parent["rollup"]["subtaskCount"] == 1


async def test_a_second_subtask_inherits_nothing(client: httpx.AsyncClient) -> None:
    """"Parent has items" implies "parent has no children", because the first child took
    them. The second must not take the *second* child's."""
    task_id, _ = await _task_with_items(client, "Read §3")
    project_id = (await client.get(f"/api/tasks/{task_id}")).json()["task"]["projectId"]

    first = (
        await client.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "First", "parentTaskId": task_id},
        )
    ).json()["task"]
    second = (
        await client.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "Second", "parentTaskId": task_id},
        )
    ).json()["task"]

    assert len(first["items"]) == 1
    assert second["items"] == []


async def test_a_subtask_that_inherits_a_finished_checklist_completes_itself(
    client: httpx.AsyncClient,
) -> None:
    """Invariant 6 applies to the child on the write that creates it.

    Worth pinning because the derivation runs over the *post-change* board: the child is
    created with items already ticked, so it must arrive `completed` in the create's own
    response rather than waiting for something else to touch it. That response used to be
    the pre-derivation object, which is how this test found a real bug.

    The parent is `completed` throughout — a one-item checklist, ticked, completes it
    before the child exists — and stays that way. Gaining a subtask is not a reason to
    reopen work the learner already finished, and a parent never auto-completes *or*
    auto-reopens: its plan is its subtasks now, and invariant 4 makes that the learner's
    call.
    """
    task_id, items = await _task_with_items(client, "Read §3")
    await client.patch(
        f"/api/tasks/{task_id}/items/{items[0]['itemId']}", json={"completed": True}
    )
    project_id = (await client.get(f"/api/tasks/{task_id}")).json()["task"]["projectId"]

    child = (
        await client.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "Already done", "parentTaskId": task_id},
        )
    ).json()["task"]

    assert child["state"] == "completed"
    parent = (await client.get(f"/api/tasks/{task_id}")).json()["task"]
    assert parent["state"] == "completed"
    assert parent["items"] == []
