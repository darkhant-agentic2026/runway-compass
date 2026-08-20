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

    async def enqueue_run(self, run_id: str) -> None:
        self.enqueued.append(run_id)


class CountingStubModel(StubModel):
    """`StubModel`, plus an invocation count and a switchable failure.

    The count is what makes "resume does not re-run research" assertable at all. The ledger
    saying `research` is complete proves only what the ledger says; the number of times a
    model was asked to do the work is the thing that would show up on the bill.

    `fail_with` is a *switch* rather than the stub's own text trigger
    (`_FAILURE_PATTERN`), because the message that starts a research turn is written by the
    executor and a test has no way to put a magic word into it.
    """

    fail_with: str | None = None
    #: Per-instance by construction. A bare `[]` default here would be one list shared by
    #: every model built in a session, so one test's count would include the last one's.
    invocations: list[str] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.invocations.append(getattr(llm_request, "model", "") or self.model)
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        async for response in super().generate_content_async(llm_request, stream):
            yield response


__all__ = ["CountingStubModel", "RecordingQueue"]
