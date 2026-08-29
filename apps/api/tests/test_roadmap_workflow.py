"""`roadmap_workflow`: `task_proposer` -> `plan_tailor`, and `POST /api/sessions/{sid}/roadmap`.

docs/03-agent-design.md#the-research-pipeline-since-m9. Two altitudes, same split
`test_research.py` uses for `reviewer_writer`: the first two tests drive a turn directly
with `agent="roadmap"` — the same low-level `TurnService.start` call
`ResearchService.start_roadmap` itself makes — to assert the *pipeline's* contract (that
`write_study_plan` writes the right document) independent of the HTTP layer; the tests
below that exercise the real endpoint, asserting the lease, the refusals, and the ledger
shape `ResearchService.start_roadmap` owns. Content is never asserted on — the stub's
reply is fixed, same rule `test_research.py` states for `reviewer_writer`'s.

The first two tests start their turn directly on the project's own intake session rather
than a fresh one created for the run, since they bypass `ResearchService` entirely — a
real caller (the endpoint tests below) always gets a fresh session, same as
`research_workflow`.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from coach.agents.context import RUN_ID_KEY
from coach.agents.research_workflow import TOPIC_RESEARCHER_NAME
from coach.core.clock import now
from coach.core.ids import run_id as new_run_id
from coach.core.principal import Principal
from coach.integrations.stub_model import StubModel
from coach.services.models import AutonomousRun

TURN_TIMEOUT_SECONDS = 20.0


@pytest.fixture
def stub_model(container, monkeypatch: pytest.MonkeyPatch) -> StubModel:
    monkeypatch.setenv("STUB_MODEL_DELAY_MS", "0")
    model = StubModel()
    container.runners.set_model(model)
    return model


async def _await_turn(client: httpx.AsyncClient, turn_id: str) -> dict:
    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            turn = (await client.get(f"/api/turns/{turn_id}")).json()
            if turn["status"] != "running":
                return dict(turn)
            await asyncio.sleep(0.02)


async def _await_queued_turn(client: httpx.AsyncClient, run_id: str) -> dict:
    """Since M9, `POST /api/sessions/{sid}/roadmap` is queued — `turnId` in the 202 body
    is `null` until `RunExecutor` (via the local in-process queue, `ENV=local`, same as
    this suite) actually starts the turn. Poll the run for it, same as `test_research.py`.
    """
    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            run = (await client.get(f"/api/runs/{run_id}")).json()["run"]
            if run["turnId"]:
                return await _await_turn(client, run["turnId"])
            await asyncio.sleep(0.02)


async def _await_run_status(client: httpx.AsyncClient, run_id: str) -> dict:
    """The `roadmap` step's turn finishing (`_await_queued_turn`) is not the *run*
    finishing: `RunExecutor._run_steps` still has `write_plan` to run after it — a plain
    Firestore read confirming `write_study_plan`'s document landed — and only then does it
    mark the ledger row itself complete. Polling the run here too, not just the turn, is
    what makes an assertion on `run.status` race-free.
    """
    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            run = (await client.get(f"/api/runs/{run_id}")).json()["run"]
            if run["status"] != "running":
                return run
            await asyncio.sleep(0.02)


async def test_a_roadmap_turn_writes_a_study_plan(
    client: httpx.AsyncClient, container, alice: Principal, stub_model: StubModel
) -> None:
    project = (
        await client.post("/api/projects", json={"title": "Become a data engineer"})
    ).json()
    intake = (await client.post(f"/api/projects/{project['id']}/session")).json()

    run_id = new_run_id()
    turn = await container.turns.start(
        alice,
        intake["id"],
        text="What do I need to learn to become a data engineer?",
        agent="roadmap",
        state_delta={RUN_ID_KEY: run_id},
    )
    finished = await _await_turn(client, turn.id)
    assert finished["status"] == "complete", finished

    plan = await container.study_plans.get(project["id"], f"plan_{run_id}")
    assert plan is not None
    assert plan.project_id == project["id"]
    assert plan.owner_uid == alice.uid
    assert plan.run_id == run_id

    # The contract, not the content: `task_proposer` proposed the stub's one task, and
    # `plan_tailor` wrote exactly one plan entry covering it.
    assert [task.slug for task in plan.proposed_tasks] == ["stub-task"]
    assert [entry.task_slug for entry in plan.plan] == ["stub-task"]
    assert plan.plan[0].decision == "include"
    assert plan.materialized_at is None


async def test_the_roadmap_run_fans_out_one_topic_researcher_branch_per_subtopic(
    client: httpx.AsyncClient, container, alice: Principal, stub_model: StubModel
) -> None:
    """`build_roadmap_workflow` shares `research_planner` and the `topic_researcher`
    fan-out with `build_research_workflow`, but its own terminal nodes —
    `research_findings` and `task_proposer_scope` — both sit *after* the fan-out rather
    than between `research_planner` and it. That placement is load-bearing, not cosmetic:
    `Workflow`'s edges are a sequential chain, so the fan-out's own `node_input` has to stay
    `research_planner`'s `list[str]` output — a node placed between them would become the
    fan-out's `node_input` instead, and `_ParallelWorker._run_impl`
    (`google/adk/workflow/_parallel_worker.py`) wraps a non-list `node_input` in a
    single-item list rather than rejecting it, silently collapsing "one branch per
    sub-topic" to exactly one branch fed that node's own output as if it were the one
    sub-topic. Nothing about that fails construction — the test above only checks the node
    set, not runtime breadth — so this asserts the fan-out's actual shape: one
    `topic_researcher`-authored event per stub sub-topic (3 — `stub_model.py`'s
    `_STUB_SUBTOPICS`), not one for the whole job.
    """
    project = (
        await client.post("/api/projects", json={"title": "Become a data engineer"})
    ).json()
    intake = (await client.post(f"/api/projects/{project['id']}/session")).json()

    run_id = new_run_id()
    turn = await container.turns.start(
        alice,
        intake["id"],
        text="What do I need to learn to become a data engineer?",
        agent="roadmap",
        state_delta={RUN_ID_KEY: run_id},
    )
    finished = await _await_turn(client, turn.id)
    assert finished["status"] == "complete", finished

    events = await container.sessions.list_events(alice, intake["id"], limit=200)
    # Each branch writes two `topic_researcher`-authored events (one content-less, one
    # carrying the actual reply) — `content` is what tells "one real branch" from "one
    # bookkeeping event a real branch also produces", not just the author name.
    topic_researcher_replies = [
        event
        for event in events
        if event.event_data.get("author") == TOPIC_RESEARCHER_NAME
        and event.event_data.get("content")
    ]
    branches = {event.event_data.get("branch") for event in topic_researcher_replies}
    assert len(topic_researcher_replies) == 3
    assert len(branches) == 3


async def _staged_upload(
    client: httpx.AsyncClient, container, content: bytes = b"the rubric"
) -> str:
    """Create an upload, pretend the browser's PUT landed, and finalize it. Same pattern as
    `test_run_executor.py`'s own helper of the same name."""
    created = (
        await client.post(
            "/api/uploads",
            json={
                "filename": "rubric.pdf",
                "mimeType": "application/pdf",
                "sizeBytes": max(len(content), 1),
            },
        )
    ).json()
    record = await container.upload_repository.get(created["uploadId"])
    container.uploads._store.declare(
        record["objectName"], len(content), "application/pdf", content
    )
    await client.post(f"/api/uploads/{created['uploadId']}/finalize")
    return str(created["uploadId"])


async def test_a_roadmap_run_with_an_attachment_still_completes(
    client: httpx.AsyncClient, container, alice: Principal, stub_model: StubModel
) -> None:
    """`_carry_attachments_into_subtopics` (`agents/research_workflow.py`,
    `test_research_workflow.py` for the unit coverage of what it actually builds) turns
    every `topic_researcher` branch's own node_input into a multi-part `types.Content` —
    text plus the run's attachment parts — rather than the bare string it was before. This
    is the smoke test that the wiring survives contact with a real turn: the branch's own
    input event is session-local, built only to feed the LLM request
    (`prepare_llm_agent_input`), and is never itself a persisted Firestore event, so it is
    not independently observable here — see the unit test for the part-by-part assertion.
    """
    fixture = await _intake(client)
    upload_id = await _staged_upload(client, container)

    run_id = new_run_id()
    turn = await container.turns.start(
        alice,
        fixture["intake"]["id"],
        text="What do I need to learn to become a data engineer?",
        agent="roadmap",
        attachments=[{"uploadId": upload_id, "mimeType": "application/pdf"}],
        state_delta={RUN_ID_KEY: run_id},
    )
    finished = await _await_turn(client, turn.id)
    assert finished["status"] == "complete", finished

    plan = await container.study_plans.get(fixture["project"]["id"], f"plan_{run_id}")
    assert plan is not None


async def test_a_second_roadmap_turn_writes_a_second_plan(
    client: httpx.AsyncClient, container, alice: Principal, stub_model: StubModel
) -> None:
    """Two runs, two plans — `plan_{runId}` keys them apart the same way
    `report_{runId}` keys research reports apart, so two roadmap requests for the same
    project never collide."""
    project = (
        await client.post("/api/projects", json={"title": "Become a data engineer"})
    ).json()
    intake = (await client.post(f"/api/projects/{project['id']}/session")).json()

    run_ids = [new_run_id(), new_run_id()]
    for run_id in run_ids:
        turn = await container.turns.start(
            alice,
            intake["id"],
            text="What do I need to learn?",
            agent="roadmap",
            state_delta={RUN_ID_KEY: run_id},
        )
        finished = await _await_turn(client, turn.id)
        assert finished["status"] == "complete", finished

    plans = [await container.study_plans.get(project["id"], f"plan_{rid}") for rid in run_ids]
    assert all(plan is not None for plan in plans)
    assert plans[0].id != plans[1].id


# --- POST /api/sessions/{sid}/roadmap ----------------------------------------------------


async def _intake(client: httpx.AsyncClient, title: str = "Become a data engineer") -> dict:
    project = (await client.post("/api/projects", json={"title": title})).json()
    intake = (await client.post(f"/api/projects/{project['id']}/session")).json()
    return {"project": project, "intake": intake}


async def test_starting_a_roadmap_run_over_http(
    client: httpx.AsyncClient, container, alice: Principal, stub_model: StubModel
) -> None:
    fixture = await _intake(client)

    started = await client.post(
        f"/api/sessions/{fixture['intake']['id']}/roadmap",
        json={"reason": "What do I need to learn to become a data engineer?"},
    )
    assert started.status_code == 202, started.text
    body = started.json()
    # The run's own fresh session, never the intake conversation it was requested from —
    # same invariant `start_manual` gives every research run.
    assert body["sessionId"] != fixture["intake"]["id"]
    assert body["turnId"] is None
    assert body["mode"] == "queued"

    finished = await _await_queued_turn(client, body["runId"])
    assert finished["status"] == "complete", finished
    await _await_run_status(client, body["runId"])

    run = await container.research.get(alice, body["runId"])
    assert run.task_id is None
    assert run.trigger == "manual"
    # `ROADMAP_STEPS`, not `MANUAL_STEPS` — what a client tells the two kinds of taskless
    # run apart by, since neither carries a field naming its pipeline.
    assert [step.id for step in run.steps] == ["roadmap", "write_plan"]
    assert run.status.value == "complete"

    plan = await container.study_plans.get(fixture["project"]["id"], f"plan_{body['runId']}")
    assert plan is not None
    assert plan.run_id == body["runId"]

    # The intake conversation itself never saw the roadmap turn.
    intake_events = (
        await client.get(f"/api/sessions/{fixture['intake']['id']}/events")
    ).json()["events"]
    assert intake_events == []

    # `GET /api/runs/{runId}/plan` — `get_run_report`'s sibling — reads back the same
    # document the tool wrote, over HTTP.
    plan_response = await client.get(f"/api/runs/{body['runId']}/plan")
    assert plan_response.status_code == 200, plan_response.text
    plan_body = plan_response.json()["plan"]
    assert plan_body["id"] == plan.id
    assert plan_body["runId"] == body["runId"]
    assert [task["slug"] for task in plan_body["proposedTasks"]] == ["stub-task"]
    assert [entry["taskSlug"] for entry in plan_body["plan"]] == ["stub-task"]
    assert plan_body["plan"][0]["decision"] == "include"


async def test_get_run_plan_404s_before_the_run_has_written_one(
    client: httpx.AsyncClient, container, alice: Principal
) -> None:
    """A run that has not reached `write_study_plan` yet — still running, or a plain
    research run that will never call it at all — has no plan to read back, the same
    404 `GET /api/runs/{runId}/report` gives a roadmap run
    (docs/04-api-contract.md#post-apisessionssidroadmap)."""
    fixture = await _intake(client)
    run_id = new_run_id()
    project_id = fixture["project"]["id"]
    await container.run_repository.create(
        AutonomousRun(
            id=run_id,
            owner_uid=alice.uid,
            project_id=project_id,
            task_id=None,
            steps=[],
        )
    )

    refused = await client.get(f"/api/runs/{run_id}/plan")
    assert refused.status_code == 404


async def test_roadmap_refuses_a_task_linked_session(client: httpx.AsyncClient) -> None:
    project = (await client.post("/api/projects", json={"title": "X"})).json()
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "A task", "estimatedMinutes": 30},
        )
    ).json()["task"]
    session = (await client.post(f"/api/tasks/{task['id']}/session")).json()["session"]

    refused = await client.post(
        f"/api/sessions/{session['id']}/roadmap", json={"reason": "Anything"}
    )
    assert refused.status_code == 422, refused.text


async def test_roadmap_refuses_an_empty_reason(client: httpx.AsyncClient) -> None:
    fixture = await _intake(client)
    refused = await client.post(
        f"/api/sessions/{fixture['intake']['id']}/roadmap", json={"reason": ""}
    )
    assert refused.status_code == 422


async def test_roadmap_refuses_when_below_the_run_start_threshold(
    client: httpx.AsyncClient, container
) -> None:
    """M10: the same gate `SchedulerService` applies to a tick's candidates, checked
    before `start_roadmap` even acquires the lease (`ResearchService._create_and_enqueue`
    is shared by `start_manual` and `start_roadmap` alike)."""
    fixture = await _intake(client)
    me = (await client.get("/api/me")).json()
    threshold = me["plan"]["limits"]["runStartPointsThreshold"]
    monthly_limit = me["plan"]["limits"]["monthlyPoints"]
    await container.usage_repository.spend_points(
        "u_alice", (monthly_limit - threshold + 1) * 1000, timezone="UTC", at=now()
    )

    refused = await client.post(
        f"/api/sessions/{fixture['intake']['id']}/roadmap", json={"reason": "Anything"}
    )

    assert refused.status_code == 429, refused.text
    assert refused.json()["type"] == "/problems/quota-below-threshold"
    assert await container.run_repository.lease_holder(fixture["project"]["id"]) is None


async def test_a_roadmap_run_and_a_research_run_share_the_project_lease(
    client: httpx.AsyncClient, stub_model: StubModel
) -> None:
    """The two pipelines both take the project's one agent lease
    (`ResearchService.start_roadmap`/`start_manual` both call `RunRepository.acquire_lease`
    on `project_id`), so a roadmap run and a research run cannot run concurrently — the
    same "manual and autonomous research never collide" guarantee, extended to the new
    pipeline rather than given its own, second lease.
    """
    fixture = await _intake(client)

    first = await client.post(
        f"/api/sessions/{fixture['intake']['id']}/roadmap",
        json={"reason": "Plan my whole goal"},
    )
    assert first.status_code == 202, first.text

    second = await client.post(
        f"/api/sessions/{fixture['intake']['id']}/research",
        json={"reason": "Something else"},
    )
    assert second.status_code == 409
    assert second.json()["runId"] == first.json()["runId"]

    await _await_queued_turn(client, first.json()["runId"])
