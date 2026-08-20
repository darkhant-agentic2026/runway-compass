"""`JobQueue` — how `/internal/tick` hands a run to `/internal/runs/{runId}/execute`.

docs/05-autonomous-runs.md#trigger-chain. The tick is a fast, idempotent *planner*: it
decides which runs should happen and enqueues them, and every model call happens in a
Cloud Tasks delivery. That split is what buys per-job retry, backoff, dispatch rate
limiting, and the property that one poisonous project cannot wedge the tick.

Two implementations behind one interface:

- `CloudTasksQueue` — the deployed path. Creates an HTTP task carrying an OIDC token for
  `TASKS_INVOKER_SA`, which the executor endpoint verifies.
- `InProcessQueue` — `ENV=local`. docs/05-autonomous-runs.md#local-development: "In local
  mode the Cloud Tasks enqueue is replaced by a direct in-process call behind the same
  `JobQueue` interface", so the whole autonomous path is exercisable on a laptop against
  the emulator with no Cloud Scheduler and no Cloud Tasks.

**The client is built through a provider, not a proxy, and not in the constructor.**
Building a Google client resolves credentials (`coach/core/lazy.py`), so constructing one
while assembling the container makes the app unimportable without them — which CI has and
a developer's machine usually hides. A proxy is the other half of that lesson and is wrong
here for a different reason: `CloudTasksClient` is handed nothing that type-checks it, but
it *is* reached for underscore attributes internally by the library, and the M2 incident
that cost `artifact_part_uri` its `_get_blob_name` is enough precedent to prefer the
provider everywhere the object is not purely ours to call.

**Cloud Tasks is called with the async client**, because everything that reaches it does so
from inside a request handler on the event loop. The synchronous one would block the loop
for the length of a round trip to Google, once per enqueued run, inside a tick that is
specified to finish in 30 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from coach.core.config import Settings

logger = logging.getLogger(__name__)

#: The path `/internal/tick` enqueues against. Formatted with the run id.
EXECUTE_PATH = "/internal/runs/{run_id}/execute"


class JobQueue(Protocol):
    """Hand one run to whatever will execute it."""

    async def enqueue_run(self, run_id: str) -> None: ...


class CloudTasksQueue:
    """The deployed queue. One HTTP task per run, authenticated with OIDC."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None

    def _resolve(self) -> Any:
        if self._client is None:
            # Imported here rather than at module scope so that a process which never
            # enqueues anything — every test, and `ENV=local` — does not pay for the
            # import or its gRPC stubs.
            from google.cloud import tasks_v2

            self._client = tasks_v2.CloudTasksAsyncClient()
        return self._client

    async def enqueue_run(self, run_id: str) -> None:
        from google.cloud import tasks_v2

        settings = self._settings
        if not (
            settings.tasks_queue and settings.tasks_target_url and settings.tasks_invoker_sa
        ):
            # `Settings` refuses to start a deployed revision without these, so reaching
            # here means `ENV=local` chose the wrong queue — a wiring bug, and one that
            # would otherwise present as runs that are created and never executed.
            raise RuntimeError(
                "CloudTasksQueue needs TASKS_QUEUE, TASKS_TARGET_URL and TASKS_INVOKER_SA."
            )
        url = f"{settings.tasks_target_url.rstrip('/')}{EXECUTE_PATH.format(run_id=run_id)}"
        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=url,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"runId": run_id}).encode(),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=settings.tasks_invoker_sa,
                    # The audience is the *service* URL, not the full path: the executor
                    # verifies one audience for every path it serves, and a per-path
                    # audience would make adding a second internal endpoint a Terraform
                    # change as well as a code change.
                    audience=settings.tasks_target_url,
                ),
            ),
            # Deduplication by name. Cloud Tasks keeps a completed task's name for about
            # an hour, so a tick that runs twice — a Cloud Scheduler retry, say — cannot
            # queue the same run twice within that window. Belt and braces beside the
            # ledger's own idempotency, and free.
            name=f"{settings.tasks_queue}/tasks/{run_id}",
        )
        await self._resolve().create_task(
            request=tasks_v2.CreateTaskRequest(parent=settings.tasks_queue, task=task)
        )
        logger.info("run enqueued", extra={"run_id": run_id, "url": url})


#: How many local runs execute at once.
#:
#: The same number as the Cloud Tasks queue's `max_concurrent_dispatches`
#: (`infra/terraform/modules/scheduler_tasks/main.tf`), and it is here because **without it
#: the local path is not the deployed path**. Cloud Tasks rate-limits dispatch; the local
#: queue started every run the tick scheduled simultaneously, so a tick that picked ten
#: projects ran ten research turns and their transactions at once against one database.
#:
#: It surfaced as the e2e suite degrading over consecutive runs: `Aborted: Transaction lock
#: timeout` on an ordinary `POST /tasks` the browser had just made, which reads as a
#: product bug in the board and is nothing of the kind. Anything that bounds concurrency in
#: production has to bound it locally too, or "the same code path" is only true of the code.
LOCAL_MAX_CONCURRENT_RUNS = 5


class InProcessQueue:
    """`ENV=local`: run the executor here, detached, instead of queueing it.

    The handoff is deliberately still asynchronous. A tick that *awaited* the executor
    would take minutes rather than the specified 30 seconds, and — more to the point — it
    would make the local path structurally different from the deployed one at exactly the
    place the two are supposed to be identical.

    Tasks are held in a set until they finish. Without a reference, the event loop is
    free to garbage-collect a running task, which is a failure mode that shows up as work
    silently not happening under load and never in a test.
    """

    def __init__(
        self,
        execute: Callable[[str], Awaitable[None]],
        *,
        max_concurrent: int = LOCAL_MAX_CONCURRENT_RUNS,
    ) -> None:
        self._execute = execute
        self._tasks: set[asyncio.Task[None]] = set()
        self._slots = asyncio.Semaphore(max_concurrent)

    async def enqueue_run(self, run_id: str) -> None:
        task = asyncio.create_task(self._run(run_id), name=f"local-run:{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, run_id: str) -> None:
        # Acquired *inside* the task, not in `enqueue_run`, so the tick still returns
        # immediately — a queue whose enqueue blocks is not a queue.
        async with self._slots:
            try:
                await self._execute(run_id)
            except Exception:
                # Nothing is waiting on this task, so an exception would otherwise be
                # reported only when the loop closes — long after the run it belongs to.
                logger.exception("local run failed", extra={"run_id": run_id})

    # No `drain`. An earlier version had one and nothing called it: the shutdown path
    # drains the *turn registry*, which is what actually holds generation, and a local run
    # is only ever waited on by a test that waits for its effect. The same reasoning that
    # deleted M2's uncalled `turns` queries — an uncalled method is a claim nothing checks.


def build_job_queue(
    settings: Settings, execute: Callable[[str], Awaitable[None]]
) -> JobQueue | InProcessQueue:
    """The queue this environment uses.

    `execute` is only reached on the local path, and is passed unconditionally so that the
    choice of queue is the *only* thing that differs between environments — a local branch
    that also changed what the executor is would be two differences to keep in sync.
    """
    return InProcessQueue(execute) if settings.is_local else CloudTasksQueue(settings)


__all__ = [
    "EXECUTE_PATH",
    "CloudTasksQueue",
    "InProcessQueue",
    "JobQueue",
    "build_job_queue",
]
