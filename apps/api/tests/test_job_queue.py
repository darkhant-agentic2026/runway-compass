"""`CloudTasksQueue.enqueue_run` names its task per attempt, not just per run.

The RUNBOOK's §10 manual tick run hit `ALREADY_EXISTS` from Cloud Tasks on a recovered
run: `SchedulerService._recover` patches a stuck or failed run back to `pending` and
increments `attempts`, then re-enqueues it under the *same* task name the first attempt
used — `{queue}/tasks/{run_id}`. Cloud Tasks keeps a completed task's name reserved for
about an hour, so the retry's `create_task` collided with its own predecessor's stale name
and failed, indistinguishable from the caller's point of view from a genuine duplicate
tick. The name has to carry the attempt as well as the run id.

This is exactly the "pin a decision, not a result" case CLAUDE.md calls out: nothing about
*executing* a run depends on the task's name, so a test that only exercises the executor
would stay green with the old, colliding name.
"""

from __future__ import annotations

from typing import Any

import pytest

from coach.core.config import Settings
from coach.integrations.queue import CloudTasksQueue
from test_import_without_credentials import DEPLOYED


class FakeTasksClient:
    """Enough of `CloudTasksAsyncClient` to capture what `enqueue_run` asked for.

    Resolves no credentials, unlike the real GAPIC client — the point of faking it out.
    """

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def create_task(self, *, request: Any) -> None:
        self.requests.append(request)


@pytest.fixture
def fake_tasks_client(monkeypatch: pytest.MonkeyPatch) -> FakeTasksClient:
    from google.cloud import tasks_v2

    fake = FakeTasksClient()
    monkeypatch.setattr(tasks_v2, "CloudTasksAsyncClient", lambda: fake)
    # `Settings` refuses a deployed `ENV` while the emulator host is set, and
    # `dev.sh test api` exports it for every other test in the suite. Nothing here
    # touches Firestore.
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    return fake


def _settings() -> Settings:
    return Settings(env="dev", google_cloud_project="coach-dev", **DEPLOYED)


async def test_a_retry_gets_a_different_task_name_than_the_attempt_before_it(
    fake_tasks_client: FakeTasksClient,
) -> None:
    queue = CloudTasksQueue(_settings())

    await queue.enqueue_run("run_1", attempts=1)
    await queue.enqueue_run("run_1", attempts=2)

    names = [request.task.name for request in fake_tasks_client.requests]
    assert names == [
        "projects/p/locations/l/queues/q/tasks/run_1-1",
        "projects/p/locations/l/queues/q/tasks/run_1-2",
    ]


async def test_the_same_attempt_delivered_twice_keeps_the_same_task_name(
    fake_tasks_client: FakeTasksClient,
) -> None:
    """The dedup this is *for*: one tick delivered twice by Cloud Scheduler must still
    collide on the same attempt, or a retried tick could double-queue one attempt."""
    queue = CloudTasksQueue(_settings())

    await queue.enqueue_run("run_1", attempts=1)
    await queue.enqueue_run("run_1", attempts=1)

    names = {request.task.name for request in fake_tasks_client.requests}
    assert names == {"projects/p/locations/l/queues/q/tasks/run_1-1"}
