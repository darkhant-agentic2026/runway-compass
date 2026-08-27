"""Test doubles for the autonomous chain, shared by the scheduler and executor suites.

A sibling of `streaming_doubles.py` and for the same reason: a double imported from another
*test* module is a dependency between two files that are supposed to be independent, and it
is the kind that only breaks when somebody runs one of them alone.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.llm_response import LlmResponse
from pydantic import Field

from coach.integrations.stub_model import StubModel


class RecordingQueue:
    """A `JobQueue` that remembers rather than runs.

    Every scheduling assertion is about *which runs the tick decides to create*, and
    executing them would put a model call inside a scheduling test.
    """

    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.attempts: dict[str, int] = {}

    async def enqueue_run(self, run_id: str, *, attempts: int) -> None:
        self.enqueued.append(run_id)
        self.attempts[run_id] = attempts


class FailingQueue:
    """A `JobQueue` that always refuses — Cloud Tasks quota, IAM, or the `ALREADY_EXISTS`
    collision the RUNBOOK's tick job hit in production.

    What matters about *how* it fails is that `SchedulerService` must not leave a ledger
    row that no future tick's `list_stuck`/`list_retryable` can find, and must not let one
    candidate's failure abort every other candidate still waiting in the same tick.
    """

    async def enqueue_run(self, run_id: str, *, attempts: int) -> None:
        raise RuntimeError(f"queue refused run {run_id} attempt {attempts}")


class CountingStubModel(StubModel):
    """`StubModel`, plus an invocation count and a switchable failure.

    The count is what makes "resume does not re-run research" assertable at all. The ledger
    saying `research` is complete proves only what the ledger says; the number of times a
    model was asked to do the work is the thing that would show up on the bill.

    `fail_with` is a *switch* rather than the stub's own text trigger
    (`_FAILURE_PATTERN`), because the message that starts a research turn is written by the
    executor and a test has no way to put a magic word into it.

    `fail_after` is the M9 addition: raise starting on the call *after* the `fail_after`th,
    so a test can let `research_planner`'s call succeed and then fail every call after it —
    simulating a crash mid-`topic_researcher`-fan-out without needing to know which of the
    fanned-out branches happens to go first.
    """

    fail_with: str | None = None
    fail_after: int | None = None
    #: Per-instance by construction. A bare `[]` default here would be one list shared by
    #: every model built in a session, so one test's count would include the last one's.
    invocations: list[str] = Field(default_factory=list)
    #: How many of those invocations were `research_planner`'s — identified by the
    #: `list[str]` `response_schema` only that node's `output_schema` produces
    #: (`agents/research_workflow.py`). What makes "the retry did not repeat the planner's
    #: call" assertable, since `invocations` alone cannot tell nodes apart.
    planner_invocations: int = 0

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.invocations.append(getattr(llm_request, "model", "") or self.model)
        config = getattr(llm_request, "config", None)
        if config is not None and getattr(config, "response_schema", None) == list[str]:
            self.planner_invocations += 1
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        if self.fail_after is not None and len(self.invocations) > self.fail_after:
            raise RuntimeError("stub failure: fail_after threshold reached")
        async for response in super().generate_content_async(llm_request, stream):
            yield response


__all__ = ["CountingStubModel", "FailingQueue", "RecordingQueue"]
