"""`/internal/runs/{runId}/execute`: resume at cursor, the second presence check, and undo.

docs/05-autonomous-runs.md#execution-semantics. What each test here is really about:

- **Resume never repeats `research`.** That is the concrete meaning of "complete previously
  interrupted work", and it is the one property of this module worth the whole ledger. It is
  asserted by *counting model invocations across two executions*, not by reading the ledger:
  a ledger that says `research` is complete while the agent ran twice would pass an
  assertion on the ledger and fail the user's bill.
- **The presence guard is checked twice, and the second check is scheduled-only.** A Cloud
  Tasks delivery arrives minutes after the tick, and a requested run whose learner has sat
  down is the expected case rather than the race.
- **A failed run leaves the task retryable.** A run that died with `researchStatus` stuck on
  `in_progress` leaves a task invariant 6 will never complete, waiting on a run that no
  longer exists.

The stub model does the research (`integrations/stub_model.py` answers `research_agent` with
one report call), so what is under test is the executor's control flow, not a model's
judgement.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from autonomy_doubles import CountingStubModel, RecordingQueue
from coach.core.clock import now
from coach.services.models import ResearchStatus, RunStatus, StepStatus, TaskState
from coach.services.scheduler import SCHEDULED_STEPS, SchedulerService


@pytest.fixture
def stub_model(container, monkeypatch: pytest.MonkeyPatch) -> CountingStubModel:
    monkeypatch.setenv("STUB_MODEL_DELAY_MS", "0")
    model = CountingStubModel()
    container.runners.set_model(model)
    return model


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture
def scheduler(container, queue: RecordingQueue) -> SchedulerService:
    return SchedulerService(
        tasks=container.tasks,
        task_repository=container.task_repository,
        projects=container.project_repository,
        users=container.user_repository,
        runs=container.run_repository,
        presence=container.presence_repository,
        usage=container.usage_repository,
        queue=queue,
    )


@pytest.fixture(autouse=True)
async def no_quiet_hours(client: httpx.AsyncClient) -> None:
    """See `test_scheduler.no_quiet_hours`: otherwise this module passes by the hour."""
    await client.patch(
        "/api/me/prefs",
        json={"autonomousQuietHours": {"start": "00:00", "end": "00:00"}, "timezone": "UTC"},
    )


async def _board(
    client: httpx.AsyncClient, titles: tuple[str, ...] = ("Structured concurrency",)
) -> dict[str, Any]:
    project = (await client.post("/api/projects", json={"title": "Async Python"})).json()
    tasks = []
    for title in titles:
        response = await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": title, "estimatedMinutes": 45},
        )
        tasks.append(response.json()["task"])
    return {"project": project, "tasks": tasks}


# --- the happy path ----------------------------------------------------------------------


async def test_a_scheduled_run_researches_the_next_task_and_completes_every_step(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    board = await _board(client)
    task_id = board["tasks"][0]["id"]
    # Opened before the run, as a learner who has visited the task at least once would
    # have — this is what lets the assertion below tell "no session" apart from "a
    # different session".
    conversation = (await client.post(f"/api/tasks/{task_id}/session")).json()["session"]
    run_id = (await scheduler.tick()).scheduled[0]

    run = await container.executor.execute(run_id)

    assert run is not None
    assert run.status is RunStatus.COMPLETE
    assert [step.id for step in run.steps] == list(SCHEDULED_STEPS)
    assert all(step.status in {StepStatus.COMPLETE, StepStatus.SKIPPED} for step in run.steps)
    assert run.cursor is None

    task = await container.tasks.resolve(alice, task_id)
    assert task.research_status is ResearchStatus.DONE
    # The checklist it promoted is what takes the task out of `draft`.
    assert task.items
    assert task.state is TaskState.NOT_STARTED
    assert task.latest_report_id == f"report_{run_id}"

    # Since M8, the research turn ran in a session of its own, not the task's own
    # conversation (docs/02-data-model.md#sessions--events-adk-owned-layout) — so the
    # learner's own transcript for this task stays empty even though a report exists.
    assert run.session_id is not None
    assert run.session_id != conversation["id"]
    conversation_events = await container.sessions.list_events(alice, conversation["id"])
    assert conversation_events == []


async def test_the_report_is_keyed_on_the_run_so_a_retry_overwrites(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    """The M5 obligation docs/05-autonomous-runs.md flags on `post_report`.

    Asserted as *one* report for one run rather than as an id shape: the id is the
    mechanism, and a mechanism that stopped deduplicating while keeping its naming scheme
    would pass the weaker assertion.
    """
    board = await _board(client)
    run_id = (await scheduler.tick()).scheduled[0]
    await container.executor.execute(run_id)

    # Re-run the research step by hand, exactly as a Cloud Tasks retry of a step that
    # posted its report and then failed would.
    await container.run_repository.patch(
        run_id,
        {
            "status": RunStatus.PENDING.value,
            "steps": [
                {**step.to_document(), "status": StepStatus.PENDING.value}
                if step.id in {"research", "post_report"}
                else step.to_document()
                for step in (await container.run_repository.get(run_id)).steps  # type: ignore[union-attr]
            ],
        },
    )
    await container.executor.execute(run_id)

    reports = await container.reports.list_for_task(alice, board["tasks"][0]["id"])
    assert len(reports) == 1


# --- resume ------------------------------------------------------------------------------


async def test_resuming_at_the_cursor_does_not_re_run_research(
    client: httpx.AsyncClient,
    container,
    scheduler: SchedulerService,
    stub_model: CountingStubModel,
) -> None:
    """Killing the process mid-run and re-ticking resumes without re-running research.

    M5's exit criterion, and the reason the ledger exists at all. The evidence is the
    **model invocation count**: the ledger saying `research` is complete proves only what
    the ledger says.
    """
    await _board(client)
    run_id = (await scheduler.tick()).scheduled[0]
    await container.executor.execute(run_id)
    invocations_after_first = len(stub_model.invocations)
    assert invocations_after_first > 0

    # What a crash between `post_report` and `propose_tasks` leaves behind: the completed
    # steps still complete, the run back in flight, the cursor past the expensive step.
    run = await container.run_repository.get(run_id)
    assert run is not None
    steps = [
        {**step.to_document(), "status": StepStatus.PENDING.value}
        if step.id in {"propose_tasks", "reprioritize"}
        else step.to_document()
        for step in run.steps
    ]
    await container.run_repository.patch(
        run_id, {"status": RunStatus.RUNNING.value, "steps": steps, "leaseExpiresAt": now()}
    )
    resumed_from = (await container.run_repository.get(run_id)).cursor  # type: ignore[union-attr]
    assert resumed_from == "propose_tasks"

    stub_model.invocations.clear()
    resumed = await container.executor.execute(run_id)

    assert resumed is not None
    assert resumed.status is RunStatus.COMPLETE
    # One further generation — `propose_tasks` — and no second research turn.
    assert len(stub_model.invocations) <= 1


# --- the second presence check -----------------------------------------------------------


async def test_a_scheduled_run_is_abandoned_if_the_owner_sat_down_after_the_tick(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, stub_model
) -> None:
    board = await _board(client)
    run_id = (await scheduler.tick()).scheduled[0]
    await container.presence_repository.heartbeat(
        "u_alice", project_id=board["project"]["id"], task_id=None
    )

    run = await container.executor.execute(run_id)

    assert run is not None
    assert run.status is RunStatus.SKIPPED_OWNER_PRESENT
    # Abandoned, not failed: the guard worked. A `failed` row would surface a red banner
    # and burn one of three attempts on nothing going wrong.
    assert run.error is None
    assert stub_model.invocations == []


async def test_a_requested_run_executes_with_the_owner_present(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    """The second half of the guard, at the second checkpoint.

    The execution-time check runs inside the lease transaction, minutes after the tick and
    with none of the tick's reasoning in scope — so "requested runs ignore presence" has to
    be true *here* as well, and that is a separate line of code from the one the scheduler
    test covers.
    """
    board = await _board(client)
    task = board["tasks"][0]
    await client.post(f"/api/tasks/{task['id']}/research-request")
    run_id = (await scheduler.tick()).scheduled[0]
    await container.presence_repository.heartbeat(
        "u_alice", project_id=board["project"]["id"], task_id=task["id"]
    )

    run = await container.executor.execute(run_id)

    assert run is not None
    assert run.status is RunStatus.COMPLETE
    assert (await container.tasks.resolve(alice, task["id"])).research_status is (
        ResearchStatus.DONE
    )


async def test_a_requested_run_still_researches_a_task_the_coach_marked_needs_no_research(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    """The RUNBOOK's #10 tick run: every attempt failed with "the research turn produced
    no report", on a task `propose_tasks` had authored with `needsResearch: false`.

    `select_next_task` resolves a requested task unconditionally — "a run that took the
    project because something was requested has to research that thing"
    (docs/03-agent-design.md, step 1) — but `_research` used to skip whenever
    `needsResearch` was false with no exception for that, so a learner pressing "prepare
    this" on such a task got a run that could never succeed no matter how many of its
    three attempts it burned. `research` must actually run, not skip, and `post_report`
    must not treat that skip as a failure.
    """
    board = await _board(client)
    task = board["tasks"][0]
    await client.patch(f"/api/tasks/{task['id']}", json={"needsResearch": False})
    await client.post(f"/api/tasks/{task['id']}/research-request")
    run_id = (await scheduler.tick()).scheduled[0]

    run = await container.executor.execute(run_id)

    assert run is not None
    assert run.status is RunStatus.COMPLETE
    steps = {step.id: step for step in run.steps}
    assert steps["research"].status is StepStatus.COMPLETE
    assert steps["post_report"].status is StepStatus.COMPLETE
    assert (await container.tasks.resolve(alice, task["id"])).research_status is (
        ResearchStatus.DONE
    )


# --- the request flag --------------------------------------------------------------------


async def test_claiming_a_requested_task_clears_the_flag_as_the_run_starts(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    """docs/05-autonomous-runs.md#when-the-request-flag-is-cleared.

    Left up, the next tick would find the same task and enqueue it again — a queue that
    re-offers work it has already handed out.
    """
    board = await _board(client)
    task = board["tasks"][0]
    await client.post(f"/api/tasks/{task['id']}/research-request")
    run_id = (await scheduler.tick()).scheduled[0]

    await container.executor.execute(run_id)

    settled = await container.tasks.resolve(alice, task["id"])
    assert settled.research_requested_at is None
    assert settled.research_status is ResearchStatus.DONE
    # And a second tick finds nothing to re-offer.
    assert (await scheduler.tick()).scheduled == []


async def test_a_failed_run_leaves_the_task_retryable_and_the_request_cleared(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    board = await _board(client)
    task = board["tasks"][0]
    await client.post(f"/api/tasks/{task['id']}/research-request")
    run_id = (await scheduler.tick()).scheduled[0]
    stub_model.fail_with = RuntimeError("the model is down")

    run = await container.executor.execute(run_id)

    assert run is not None
    assert run.status is RunStatus.FAILED
    settled = await container.tasks.resolve(alice, task["id"])
    # `failed`, not `in_progress`: invariant 6 reads this field, so a task left mid-research
    # by a run that no longer exists could never complete itself again.
    assert settled.research_status is ResearchStatus.FAILED
    assert settled.research_requested_at is None


# --- the board changes, the banner, and undo ---------------------------------------------


async def test_reprioritize_moves_the_researched_task_first_and_undo_puts_it_back(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    """Golden flow #6's mechanism, at the service altitude.

    The reordered task's *previous* `order` is what makes undo exact: a fractional index
    cannot be inverted, so a recomputed key would land the task in the right position
    relative to whatever the board looks like now, which is not where it was.
    """
    board = await _board(client, ("First", "Second"))
    second = board["tasks"][1]
    original_order = second["order"]
    await client.post(f"/api/tasks/{second['id']}/research-request")
    run_id = (await scheduler.tick()).scheduled[0]

    run = await container.executor.execute(run_id)

    assert run is not None
    moved = [change for change in run.changes if change.kind == "task_reordered"]
    assert [change.task_id for change in moved] == [second["id"]]
    assert (await container.tasks.resolve(alice, second["id"])).order < board["tasks"][0][
        "order"
    ]

    _, touched = await container.runs.undo(alice, run_id)

    assert touched == [second["id"]]
    assert (await container.tasks.resolve(alice, second["id"])).order == original_order


async def test_undo_is_idempotent(
    client: httpx.AsyncClient, container, alice, scheduler: SchedulerService, stub_model
) -> None:
    board = await _board(client, ("First", "Second"))
    await client.post(f"/api/tasks/{board['tasks'][1]['id']}/research-request")
    run_id = (await scheduler.tick()).scheduled[0]
    await container.executor.execute(run_id)

    _, first = await container.runs.undo(alice, run_id)
    run, second = await container.runs.undo(alice, run_id)

    assert run.undone_at is not None
    assert second == first


# --- delivery semantics ------------------------------------------------------------------


async def test_re_delivering_a_finished_run_does_nothing(
    client: httpx.AsyncClient,
    container,
    scheduler: SchedulerService,
    stub_model: CountingStubModel,
) -> None:
    """Cloud Tasks retries whenever it does not see a response, including on success."""
    await _board(client)
    run_id = (await scheduler.tick()).scheduled[0]
    await container.executor.execute(run_id)
    stub_model.invocations.clear()

    again = await container.executor.execute(run_id)

    assert again is not None
    assert again.status is RunStatus.COMPLETE
    assert stub_model.invocations == []


async def test_a_run_whose_project_is_leased_elsewhere_is_deferred_not_failed(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, stub_model
) -> None:
    board = await _board(client)
    run_id = (await scheduler.tick()).scheduled[0]
    await container.run_repository.acquire_lease(
        board["project"]["id"], "r_someone_else", "instance-b"
    )

    run = await container.executor.execute(run_id)

    assert run is not None
    # Still pending, so the next tick's recovery offers it again. Failing here would burn
    # one of three attempts on a queue collision.
    assert run.status is RunStatus.PENDING
    assert stub_model.invocations == []


async def test_an_unknown_run_is_not_an_error(container) -> None:
    assert await container.executor.execute("r_does_not_exist") is None
