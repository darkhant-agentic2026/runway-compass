"""`/internal/tick`: the guard table, the priority order, the sweep, and recovery.

docs/05-autonomous-runs.md#candidate-selection-and-guards. Two things about how this is
written are deliberate:

**The queue is a recorder, not the real one.** Every assertion here is about *which runs the
tick decides to create*, and executing them would put a model call inside a scheduling
test. `RunExecutor` has its own module.

**The negative half of every guard is asserted beside the positive one.** A scheduler bug
that queues everything passes every "it ran" test, and one that queues nothing passes every
"it was skipped" test. Neither half means anything alone — which is also why golden flow #8
asserts the run that was *not* created (docs/08-testing.md).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest

from autonomy_doubles import RecordingQueue
from coach.core.clock import now
from coach.core.principal import Principal
from coach.repositories.usage import local_day
from coach.services.models import ResearchStatus, RunStatus, TaskState
from coach.services.scheduler import COOLDOWN, SchedulerService


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


@pytest.fixture(autouse=True)
async def no_quiet_hours(client: httpx.AsyncClient) -> None:
    """Take the wall clock out of every test below.

    The default quiet window is 23:00 to 07:00, so without this the auto-scheduled half of
    this module passes or fails **by the hour it is run** — which is exactly how the first
    version of this file failed, at 00:44 UTC, in a way that read as a broken guard rather
    than a working one. `start == end` is an empty window (`_in_quiet_hours`).

    The quiet-hours test below sets its own window and is the one place that does.
    """
    await client.patch(
        "/api/me/prefs",
        json={"autonomousQuietHours": {"start": "00:00", "end": "00:00"}, "timezone": "UTC"},
    )


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


async def _project(client: httpx.AsyncClient, title: str = "Async Python") -> dict[str, Any]:
    return dict((await client.post("/api/projects", json={"title": title})).json())


async def _task(
    client: httpx.AsyncClient, project_id: str, title: str = "Structured concurrency"
) -> dict[str, Any]:
    response = await client.post(
        f"/api/projects/{project_id}/tasks",
        json={"title": title, "estimatedMinutes": 45},
    )
    return dict(response.json()["task"])


# --- the four owner-facing guards, each with both halves ---------------------------------


async def test_an_idle_project_with_research_to_do_is_scheduled(
    client: httpx.AsyncClient, scheduler: SchedulerService, queue: RecordingQueue
) -> None:
    project = await _project(client)
    await _task(client, project["id"])

    result = await scheduler.tick()

    assert len(result.scheduled) == 1
    assert queue.enqueued == result.scheduled


async def test_the_owner_being_present_skips_auto_scheduled_work(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, queue: RecordingQueue
) -> None:
    """The guard M5's exit criterion is about — for the work the *coach* signed up for."""
    project = await _project(client)
    await _task(client, project["id"])
    await container.presence_repository.heartbeat(
        "u_alice", project_id=project["id"], task_id=None
    )

    result = await scheduler.tick()

    assert result.scheduled == []
    assert queue.enqueued == []
    assert result.skipped["owner_present"] == 1


async def test_a_requested_task_runs_even_with_its_owner_present(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, queue: RecordingQueue
) -> None:
    """The half of the guard that M5 changed.

    A learner who presses "have my coach prepare this" and then stays on the page is the
    case the old rule refused — and their being there is the *reason* the run exists
    (docs/09-roadmap.md#the-presence-guard-applies-to-auto-scheduled-work-only--decided-at-the-start-of-m5).
    """
    project = await _project(client)
    task = await _task(client, project["id"])
    await client.post(f"/api/tasks/{task['id']}/research-request")
    await container.presence_repository.heartbeat(
        "u_alice", project_id=project["id"], task_id=task["id"]
    )

    result = await scheduler.tick()

    assert len(result.scheduled) == 1
    run = await container.run_repository.get(result.scheduled[0])
    assert run is not None
    assert run.trigger == "requested"
    assert run.task_id == task["id"]


async def test_the_cooldown_skips_auto_work_and_not_a_request(
    client: httpx.AsyncClient, container, scheduler: SchedulerService
) -> None:
    project = await _project(client)
    task = await _task(client, project["id"])
    await container.project_repository.patch(
        project["id"], {"lastAutonomousRunAt": now() - COOLDOWN + timedelta(minutes=5)}
    )

    assert (await scheduler.tick()).skipped.get("cooldown") == 1

    await client.post(f"/api/tasks/{task['id']}/research-request")
    assert len((await scheduler.tick()).scheduled) == 1


async def test_autonomy_disabled_skips_auto_work_and_not_a_request(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, alice: Principal
) -> None:
    project = await _project(client)
    task = await _task(client, project["id"])
    await client.patch("/api/me/prefs", json={"autonomousEnabled": False})

    assert (await scheduler.tick()).skipped.get("autonomy_disabled") == 1

    await client.post(f"/api/tasks/{task['id']}/research-request")
    result = await scheduler.tick()
    assert len(result.scheduled) == 1


async def test_quiet_hours_skip_auto_work_and_not_a_request(
    client: httpx.AsyncClient, container, scheduler: SchedulerService
) -> None:
    """A window covering the whole day, so the test does not depend on the clock."""
    project = await _project(client)
    task = await _task(client, project["id"])
    await client.patch(
        "/api/me/prefs",
        json={"autonomousQuietHours": {"start": "00:00", "end": "23:59"}, "timezone": "UTC"},
    )

    assert (await scheduler.tick()).skipped.get("quiet_hours") == 1

    await client.post(f"/api/tasks/{task['id']}/research-request")
    assert len((await scheduler.tick()).scheduled) == 1


# --- the two guards that apply to both ---------------------------------------------------


async def test_a_held_lease_defers_both_kinds(
    client: httpx.AsyncClient, container, scheduler: SchedulerService
) -> None:
    project = await _project(client)
    task = await _task(client, project["id"])
    await client.post(f"/api/tasks/{task['id']}/research-request")
    await container.run_repository.acquire_lease(project["id"], "r_other", "instance-b")

    result = await scheduler.tick()

    assert result.scheduled == []
    assert result.skipped["lease_held"] >= 1


async def test_an_exhausted_quota_stops_a_request_too(
    client: httpx.AsyncClient, container, scheduler: SchedulerService
) -> None:
    """The quota is a cost ceiling, not a policy about who asked."""
    project = await _project(client)
    task = await _task(client, project["id"])
    await client.post(f"/api/tasks/{task['id']}/research-request")
    me = (await client.get("/api/me")).json()
    day = local_day(now(), "UTC")
    for _ in range(me["plan"]["limits"]["autonomousRunsPerDay"]):
        await container.usage_repository.record_autonomous_run("u_alice", day)

    result = await scheduler.tick()

    assert result.scheduled == []
    assert result.skipped["quota_exhausted"] >= 1


# --- priority ----------------------------------------------------------------------------


async def test_a_request_is_scheduled_before_auto_work_and_the_cap_does_not_starve_it(
    client: httpx.AsyncClient, container, scheduler: SchedulerService
) -> None:
    """Golden flow #9, asserted on the ledger.

    The cap is applied *after* the sort, which is the whole guarantee: with the cap at one
    and three projects of auto-scheduled work already waiting, the single slot has to go to
    the learner who pressed a button.
    """
    for index in range(3):
        auto = await _project(client, f"Auto {index}")
        await _task(client, auto["id"])
    wanted = await _project(client, "Requested")
    task = await _task(client, wanted["id"])
    await client.post(f"/api/tasks/{task['id']}/research-request")

    result = await scheduler.tick(cap=1)

    assert len(result.scheduled) == 1
    run = await container.run_repository.get(result.scheduled[0])
    assert run is not None
    assert run.project_id == wanted["id"]
    assert run.trigger == "requested"


async def test_one_run_per_project_per_tick(
    client: httpx.AsyncClient, container, scheduler: SchedulerService
) -> None:
    """A project with a request *and* auto-scheduled work is one candidate, not two.

    The lease would refuse the second run anyway; scheduling it would be a wasted enqueue
    and a ledger row that exists only to be deferred.
    """
    project = await _project(client)
    first = await _task(client, project["id"], "Requested")
    await _task(client, project["id"], "Auto")
    await client.post(f"/api/tasks/{first['id']}/research-request")

    result = await scheduler.tick()

    assert len(result.scheduled) == 1


# --- work exists -------------------------------------------------------------------------


async def test_a_project_whose_research_is_done_is_not_a_candidate(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, alice: Principal
) -> None:
    project = await _project(client)
    task = await _task(client, project["id"])
    await container.tasks.set_research(alice, task["id"], status=ResearchStatus.DONE)

    result = await scheduler.tick()

    assert result.scheduled == []
    assert result.skipped.get("no_work") == 1


async def test_a_task_that_wants_no_research_is_not_work(
    client: httpx.AsyncClient, scheduler: SchedulerService, project_with_unresearched_task
) -> None:
    """`needsResearch: false` is a decision, not an absence.

    A scheduler that ignored it would research every task on the board forever, which is
    the failure mode of "has research run?" as the whole test.
    """
    client_, _project, task = project_with_unresearched_task
    await client_.patch(f"/api/tasks/{task['id']}", json={"needsResearch": False})

    result = await scheduler.tick()

    assert result.scheduled == []


@pytest.fixture
async def project_with_unresearched_task(client: httpx.AsyncClient):
    project = await _project(client)
    task = await _task(client, project["id"])
    return client, project, task


# --- sweep and recovery -------------------------------------------------------------------


async def test_the_sweep_un_postpones_a_task_whose_date_has_passed(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, alice: Principal
) -> None:
    """Phase 1. Written through `set_state`, so the state machine and the derivation run."""
    project = await _project(client)
    task = await _task(client, project["id"])
    # `postponed_until` is reachable from `in_progress` alone (`services/state_machine.py`),
    # and a task starts in `draft` — so it needs a plan to reach `not_started` and a start
    # to reach `in_progress` before it can be deferred to a date at all.
    await client.post(
        f"/api/tasks/{task['id']}/items",
        json={"items": [{"shortDescription": "Read the guide"}]},
    )
    await client.post(f"/api/tasks/{task['id']}/state", json={"state": "in_progress"})
    await client.post(
        f"/api/tasks/{task['id']}/state",
        json={
            "state": "postponed_until",
            "postponedUntil": (now() + timedelta(days=1)).isoformat(),
        },
    )
    # Reach past the endpoint's "must be in the future" guard, which exists precisely so a
    # past timestamp cannot be written by hand — the sweep is what moves the clock.
    await container.task_repository.patch(
        project["id"], task["id"], {"postponedUntil": now() - timedelta(minutes=1)}
    )

    result = await scheduler.tick()

    assert result.swept == 1
    restored = await container.tasks.resolve(alice, task["id"])
    assert restored.state is TaskState.NOT_STARTED


async def test_a_run_whose_instance_died_is_re_enqueued(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, queue: RecordingQueue
) -> None:
    """Phase 2, invariant 1: interrupted work is finished before new work is started."""
    project = await _project(client)
    await _task(client, project["id"])
    created = (await scheduler.tick()).scheduled[0]
    queue.enqueued.clear()
    await container.run_repository.patch(
        created,
        {"status": "running", "leaseExpiresAt": now() - timedelta(minutes=1)},
    )

    result = await scheduler.tick()

    assert result.recovered == [created]
    # Only the recovered run. Invariant 1 is "interrupted work is finished *before* new
    # work is started", and the lease guard cannot enforce it here: the dead run's lease
    # has expired, so without `_recover`'s `busy` set the project reads as idle and this
    # tick queues a second run to race the first.
    assert queue.enqueued == [created]
    run = await container.run_repository.get(created)
    assert run is not None
    assert run.attempts == 2


async def test_a_stuck_run_that_has_burned_its_attempts_is_buried_rather_than_retried(
    client: httpx.AsyncClient, container, scheduler: SchedulerService, queue: RecordingQueue
) -> None:
    """Poison-pill protection, and the reason it cannot live in the query.

    "Running with an expired lease" stays true of a crashed run forever, so without an
    attempts bound a run that reliably kills its instance is re-enqueued once per tick for
    the life of the ledger row — paying for whatever step its cursor is on each time. The
    e2e suite found it as a wall of transaction lock timeouts on its third consecutive run.
    """
    project = await _project(client)
    await _task(client, project["id"])
    created = (await scheduler.tick()).scheduled[0]
    queue.enqueued.clear()
    await container.run_repository.patch(
        created,
        {
            "status": "running",
            "attempts": 3,
            "leaseExpiresAt": now() - timedelta(minutes=1),
        },
    )

    result = await scheduler.tick()

    assert result.recovered == []
    # Not "nothing was enqueued": the project is idle again once the run is buried, so this
    # tick legitimately schedules a *fresh* one. What must never happen is the dead run
    # being handed out again.
    assert created not in queue.enqueued
    run = await container.run_repository.get(created)
    assert run is not None
    assert run.status is RunStatus.FAILED
    assert run.error is not None
    # And it stays buried: a second tick finds nothing to recover.
    assert (await scheduler.tick()).recovered == []


async def test_a_failed_run_with_attempts_left_is_retried_and_one_without_is_not(
    client: httpx.AsyncClient, container, scheduler: SchedulerService
) -> None:
    project = await _project(client)
    await _task(client, project["id"])
    created = (await scheduler.tick()).scheduled[0]
    await container.run_repository.patch(created, {"status": "failed", "attempts": 1})

    assert (await scheduler.tick()).recovered == [created]

    await container.run_repository.patch(created, {"status": "failed", "attempts": 3})
    assert (await scheduler.tick()).recovered == []
