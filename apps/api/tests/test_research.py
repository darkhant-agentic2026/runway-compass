"""Research: the report schema, the promotion into a checklist, and the manual trigger.

docs/08-testing.md#unit and #agent-level-tests. Three altitudes, because the failure modes
are at three altitudes:

- `ResearchReport` validation as a pure service call — every guard docs/08-testing.md
  lists, each on its own.
- `post_research_report` through a real turn with the stubbed model, which is the tier-1
  agent test that document asks for: "asserts that `post_research_report` writes the right
  documents and session event".
- `POST /api/sessions/{sid}/research` over HTTP, including the lease contention that makes
  the `409` actionable.

The stub emits the report call, so what these assert is the *tool contract* and the
services behind it, not a model's judgement. Report quality is the nightly evalset's job.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from coach.core.errors import Conflict, ValidationProblem
from coach.integrations.stub_model import StubModel
from coach.services.models import ResearchStatus, TaskState

TURN_TIMEOUT_SECONDS = 20.0


@pytest.fixture
def stub_model(container, monkeypatch: pytest.MonkeyPatch) -> StubModel:
    monkeypatch.setenv("STUB_MODEL_DELAY_MS", "0")
    model = StubModel()
    container.runners.set_model(model)
    return model


async def _task(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    project = (await client.post("/api/projects", json={"title": "Async Python"})).json()
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Structured concurrency", "estimatedMinutes": 45, **body},
        )
    ).json()["task"]
    session = (await client.post(f"/api/tasks/{task['id']}/session")).json()["session"]
    return {"project": project, "task": task, "sessionId": session["id"]}


async def _await_run(client: httpx.AsyncClient, turn_id: str) -> dict[str, Any]:
    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            turn = (await client.get(f"/api/turns/{turn_id}")).json()
            if turn["status"] != "running":
                return dict(turn)
            await asyncio.sleep(0.02)


def _items(budget: int) -> list[dict[str, Any]]:
    half = budget // 2
    return [
        {
            "kind": "article",
            "title": "A guide",
            "url": "https://example.com/a",
            "minutes": half,
            "why": "so you can read the traceback",
            "source": "web",
        },
        {
            "kind": "exercise",
            "title": "An exercise",
            "minutes": budget - half,
            "why": "so you can check it landed",
            "details": "The answer is 42.",
            "source": "generated",
        },
    ]


# --- report validation ------------------------------------------------------------------


async def test_a_report_writes_the_documents_and_the_checklist(
    container, alice, client: httpx.AsyncClient
) -> None:
    fixture = await _task(client)
    report, task = await container.reports.post_report(
        alice,
        fixture["task"]["id"],
        summary="Two things to get through.",
        required=_items(45),
        optional=[
            {
                "kind": "article",
                "title": "Deeper",
                "url": "https://example.com/deep",
                "minutes": 90,
                "why": "",
                "source": "web",
            }
        ],
        budget_minutes=45,
    )

    assert report.total_required_minutes == 45
    assert [i.item_id for i in report.required] == [i.item_id for i in task.items]
    # Order preserved: the required list is a plan, and the tool must not sort it by kind
    # or by minutes (docs/03-agent-design.md#research_agent).
    assert [i.short_description for i in task.items] == [
        "so you can read the traceback",
        "so you can check it landed",
    ]
    # `guided` falls out of `kind` when the model says nothing: an exercise is worked
    # through in conversation, an article is not.
    assert [i.guided for i in task.items] == [False, True]
    # Only `required[]` is promoted. An optional item is material the learner may want and
    # is not a thing they owe the task, which is what a checkbox would erase.
    assert len(task.items) == 2
    assert task.state is TaskState.NOT_STARTED  # promoted out of `draft` by invariant 1
    assert task.research_status is ResearchStatus.DONE
    assert task.latest_report_id == report.id


async def test_a_report_over_budget_is_refused(container, alice, client) -> None:
    """docs/08-testing.md: "rejects `Σ required.minutes > budget`"."""
    fixture = await _task(client)
    with pytest.raises(ValidationProblem, match="60 minutes against a budget of 45"):
        await container.reports.post_report(
            alice,
            fixture["task"]["id"],
            summary="Too much",
            required=_items(60),
            optional=[],
            budget_minutes=45,
        )


async def test_an_item_in_both_lists_is_refused(container, alice, client) -> None:
    """docs/08-testing.md: "rejects an item in both lists"."""
    fixture = await _task(client)
    duplicated = _items(45)[:1]
    with pytest.raises(ValidationProblem, match="in both"):
        await container.reports.post_report(
            alice,
            fixture["task"]["id"],
            summary="Twice",
            required=duplicated,
            optional=list(duplicated),
            budget_minutes=45,
        )


async def test_a_required_item_needs_a_why(container, alice, client) -> None:
    """docs/08-testing.md: "requires `why` on every required item".

    Not decoration: `why` *is* the checklist entry's one line, so an item without one
    produces a step the learner cannot act on. Optional items need none — they are never
    promoted, so they are still rendered by title.
    """
    fixture = await _task(client)
    without = [{**item, "why": ""} for item in _items(45)]
    with pytest.raises(ValidationProblem, match="no `why`"):
        await container.reports.post_report(
            alice,
            fixture["task"]["id"],
            summary="No reasons",
            required=without,
            optional=[],
            budget_minutes=45,
        )


async def test_reports_accumulate_and_the_checklist_does_not(
    container, alice, client: httpx.AsyncClient
) -> None:
    """docs/10-risks.md Q4: reports accumulate, newest first.

    The checklist is the exception, and the contrast is the point of asserting both here:
    two reports' worth of items is a list nobody can finish.
    """
    fixture = await _task(client)
    task_id = fixture["task"]["id"]
    for summary in ("First run", "Second run"):
        await container.reports.post_report(
            alice,
            task_id,
            summary=summary,
            required=_items(45),
            optional=[],
            budget_minutes=45,
        )

    listed = await container.reports.list_for_task(alice, task_id)
    assert [r.summary for r in listed] == ["Second run", "First run"]

    detail = (await client.get(f"/api/tasks/{task_id}")).json()
    assert len(detail["task"]["items"]) == 2
    assert detail["latestReport"]["summary"] == "Second run"


async def test_report_feedback_is_written_and_completion_is_refused(
    container, alice, client: httpx.AsyncClient
) -> None:
    """The endpoint writes `progress.feedback` and nothing else.

    The `completed` half is asserted as a **422 rather than a silent no-op**, because the
    field used to be accepted here before M4 moved completion onto the task
    (docs/04-api-contract.md#tasks). A client that has not caught up must find out.
    """
    fixture = await _task(client)
    report, _ = await container.reports.post_report(
        alice,
        fixture["task"]["id"],
        summary="s",
        required=_items(45),
        optional=[],
        budget_minutes=45,
    )
    item_id = report.required[0].item_id

    ok = await client.patch(
        f"/api/reports/{report.id}/items/{item_id}",
        json={"taskId": fixture["task"]["id"], "feedback": "down"},
    )
    assert ok.status_code == 200
    assert ok.json()["report"]["progress"]["feedback"][item_id] == "down"

    refused = await client.patch(
        f"/api/reports/{report.id}/items/{item_id}",
        json={"taskId": fixture["task"]["id"], "completed": True},
    )
    assert refused.status_code == 422


# --- the tool, through a real turn --------------------------------------------------------


async def test_the_research_agent_posts_a_report_that_fits_the_task(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """Tier-1 agent test: the report is written by the tool, through a real turn.

    The budget the stub sizes its report against is parsed out of the *rendered*
    instruction — `render_budget`'s line, carrying the task's own estimate rather than the
    project default. So a 30-minute task getting a 30-minute checklist is evidence that the
    estimate reached the model, on the same footing as flow #7's duration override.
    """
    fixture = await _task(client, estimatedMinutes=30)

    started = await client.post(
        f"/api/sessions/{fixture['sessionId']}/research", json={"reason": ""}
    )
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["mode"] == "inline"

    turn = await _await_run(client, body["turnId"])
    assert turn["status"] == "complete"

    detail = (await client.get(f"/api/tasks/{fixture['task']['id']}")).json()
    task, report = detail["task"], detail["latestReport"]

    assert report is not None
    assert report["totalRequiredMinutes"] <= 30
    assert report["budgetMinutes"] == 30
    assert task["researchStatus"] == "done"
    assert task["state"] == "not_started"
    assert len(task["items"]) == len(report["required"])
    # One of each rendering, which is what the workspace has to tell apart.
    assert sorted(i["guided"] for i in task["items"]) == [False, True]


async def test_the_run_is_recorded_in_the_ledger(
    client: httpx.AsyncClient, container, alice, stub_model: StubModel
) -> None:
    """docs/00-overview.md decision 8: manual research is *the same* run path.

    Asserted as a ledger row with `trigger: "manual"`, because that is what M5's scheduler
    will find and resume. A manual run carries only the two steps M4 implements — absent
    rather than `pending`, so `cursor` stays truthful.
    """
    fixture = await _task(client)
    body = (await client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})).json()
    await _await_run(client, body["turnId"])

    # The watcher closes the ledger out shortly after the turn goes terminal.
    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            run = await container.research.get(alice, body["runId"])
            if run.status.value != "running":
                break
            await asyncio.sleep(0.05)

    assert run.trigger == "manual"
    assert run.mode == "inline"
    assert run.status.value == "complete"
    assert [step.id for step in run.steps] == ["research", "post_report"]
    assert run.cursor is None
    assert run.turn_id == body["turnId"]


async def test_the_lease_is_released_so_a_second_run_can_start(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """The lease is a `finally`, not a hope.

    A project whose lease leaked would be locked out of research for the full five-minute
    TTL, and the symptom — "your coach is already working on this project" with nothing
    working on it — reads as a bug in the presence guard rather than in the lease.
    """
    fixture = await _task(client)
    first = (
        await client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})
    ).json()
    await _await_run(client, first["turnId"])

    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            again = await client.post(
                f"/api/sessions/{fixture['sessionId']}/research", json={"force": True}
            )
            if again.status_code == 202:
                break
            assert again.status_code == 409, again.text
            await asyncio.sleep(0.05)


async def test_a_second_run_while_one_is_in_flight_is_refused_with_the_run_id(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """docs/08-testing.md: "a manual research request during an autonomous run returns
    `409` with the in-flight `runId`".

    The id is the assertion, not the status: a bare 409 tells the client to give up, where
    a 409 naming the run tells it what to attach to instead
    (docs/04-api-contract.md#post-apisessionssidresearch).
    """
    fixture = await _task(client)
    first = await client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})
    assert first.status_code == 202

    second = await client.post(
        f"/api/sessions/{fixture['sessionId']}/research", json={"force": True}
    )
    assert second.status_code == 409
    assert second.json()["runId"] == first.json()["runId"]

    await _await_run(client, first.json()["turnId"])


async def test_researching_an_already_researched_task_needs_force(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    fixture = await _task(client)
    first = (
        await client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})
    ).json()
    await _await_run(client, first["turnId"])

    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            again = await client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})
            if again.json().get("detail", "").startswith("This task already has"):
                break
            assert again.status_code == 409, again.text
            await asyncio.sleep(0.05)
    assert again.status_code == 409


async def test_a_parent_task_cannot_be_researched(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """Its subtasks are its plan, and each is researched on its own — the same exclusion
    that keeps `items` and `rollup` apart (docs/02-data-model.md#task-items)."""
    fixture = await _task(client)
    await client.post(
        f"/api/tasks/{fixture['task']['id']}/split",
        json={
            "subtasks": [
                {"title": "a", "estimatedMinutes": 20},
                {"title": "b", "estimatedMinutes": 20},
            ]
        },
    )
    refused = await client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})
    assert refused.status_code == 422


async def test_research_on_an_intake_session_is_refused(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    project = (await client.post("/api/projects", json={"title": "No task yet"})).json()
    session = (await client.post(f"/api/projects/{project['id']}/session")).json()
    refused = await client.post(f"/api/sessions/{session['id']}/research", json={})
    assert refused.status_code == 422


async def test_research_is_isolated_per_user(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    fixture = await _task(client)
    refused = await other_client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})
    assert refused.status_code == 404


async def test_a_failed_run_does_not_leave_the_task_researching(
    client: httpx.AsyncClient, container, alice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`researchStatus` is half of invariant 6, so a run that dies must settle it.

    Left at `in_progress`, a task whose checklist was already finished would never
    complete — waiting on a run that no longer exists, with nothing on screen to say so.
    """
    fixture = await _task(client)

    class Exploding(StubModel):
        async def generate_content_async(self, llm_request, stream=False):  # type: ignore[no-untyped-def]
            raise RuntimeError("the model is down")
            yield  # pragma: no cover - unreachable, makes this an async generator

    container.runners.set_model(Exploding())
    body = (await client.post(f"/api/sessions/{fixture['sessionId']}/research", json={})).json()
    await _await_run(client, body["turnId"])

    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            task = (await client.get(f"/api/tasks/{fixture['task']['id']}")).json()["task"]
            if task["researchStatus"] != "in_progress":
                break
            await asyncio.sleep(0.05)
    assert task["researchStatus"] == "failed"

    run = await container.research.get(alice, body["runId"])
    assert run.status.value == "failed"


async def test_the_conflict_carries_a_problem_document(client: httpx.AsyncClient) -> None:
    """A `Conflict` raised by the service renders as `problem+json`, extras included."""
    error = Conflict("held", runId="r_1")
    assert error.to_problem()["runId"] == "r_1"
    assert error.status == 409


# --- the failure that was silent ----------------------------------------------------------


async def test_an_unavailable_youtube_is_logged_rather_than_only_answered(
    container, alice, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The defect that reached the deployed environment at M4.

    `youtube_find_by_duration` answered the model with "recommend written material
    instead" and told nobody else. A deployment whose API key was never seeded therefore
    produced report after report with no videos in them, with no line in Cloud Logging
    naming the cause — the only way to find out was to read the source.

    Asserted on the *log*, because the return value was never the problem: it was correct
    and the model acted on it correctly. What was missing was anyone else being told.
    """
    from coach.agents.context import PROJECT_ID_KEY, TASK_ID_KEY

    fixture = await _task(client)

    class Context:
        """The two fields `agent_context` reads. A stand-in rather than a mock, because a
        real `ToolContext` needs an invocation and this tool touches neither."""

        user_id = alice.uid
        state = {
            PROJECT_ID_KEY: fixture["project"]["id"],
            TASK_ID_KEY: fixture["task"]["id"],
        }

    with caplog.at_level("WARNING", logger="coach.agents.research_tools"):
        result = await container.research_tools.youtube_find_by_duration(
            "structured concurrency", 15, Context()
        )

    assert result["ok"] is False
    assert result["error"]["code"] == "youtube_unavailable"
    assert any(
        "youtube is unavailable" in record.message for record in caplog.records
    ), [r.message for r in caplog.records]


async def test_a_report_can_be_posted_with_no_videos_available(
    container, alice, client: httpx.AsyncClient
) -> None:
    """Videos degrade; research does not fail.

    A project with videos enabled and no API key must still get a report — the tool
    refuses, the model recommends reading, and the checklist is written. Pinned because
    the obvious "fix" for the silent failure above is to make the tool raise, which would
    turn a missing key into a failed research run.
    """
    assert container.youtube.configured is False

    fixture = await _task(client)
    report, task = await container.reports.post_report(
        alice,
        fixture["task"]["id"],
        summary="Reading only, no videos available.",
        required=_items(45),
        optional=[],
        budget_minutes=45,
    )
    assert len(task.items) == 2
    assert report.total_required_minutes == 45
