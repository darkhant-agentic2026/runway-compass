"""`TokenRateLimiter` and `ThrottledLlm` — the process-wide token ceiling.

docs/03-agent-design.md#llm-throttling. `ModelThrottle` (`test_research_workflow.py`) is
tested by racing real concurrent calls against a real semaphore; this does the same thing
for the token ceiling, with `window_seconds`/`limit` shrunk to fractions of a second so the
suite pins the *decision* — a call waits once the trailing window is at capacity, and stops
waiting once an old entry ages back out of it — without a real 2-minute sleep.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from coach.integrations.model import ThrottledLlm, TokenRateLimiter


class _FakeInner(BaseLlm):
    """Yields one partial chunk (no usage) and one final response (with usage)."""

    model: str = "fake-model"

    async def generate_content_async(self, llm_request: Any, stream: bool = False) -> Any:
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="partial")]),
            partial=True,
        )
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="done")]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=42),
        )


async def test_wait_for_capacity_is_immediate_under_the_limit() -> None:
    limiter = TokenRateLimiter(window_seconds=5, limit=100)
    limiter.record(50)
    await asyncio.wait_for(limiter.wait_for_capacity(), timeout=1.0)


async def test_wait_for_capacity_waits_out_the_window_once_at_the_limit() -> None:
    limiter = TokenRateLimiter(window_seconds=0.05, limit=100)
    limiter.record(100)

    started = time.monotonic()
    await asyncio.wait_for(limiter.wait_for_capacity(), timeout=1.0)

    assert time.monotonic() - started >= 0.05


async def test_an_entry_already_aged_out_of_the_window_does_not_count() -> None:
    limiter = TokenRateLimiter(window_seconds=0.02, limit=100)
    limiter.record(100)
    await asyncio.sleep(0.03)

    started = time.monotonic()
    await asyncio.wait_for(limiter.wait_for_capacity(), timeout=1.0)

    assert time.monotonic() - started < 0.02


async def test_calls_under_the_limit_do_not_serialize_behind_each_other() -> None:
    """A generous window must not turn concurrent under-budget calls into a queue."""
    limiter = TokenRateLimiter(window_seconds=5, limit=1_000_000)

    started = time.monotonic()
    await asyncio.gather(*(limiter.wait_for_capacity() for _ in range(5)))

    assert time.monotonic() - started < 0.1


async def test_throttled_llm_waits_before_calling_the_inner_model() -> None:
    limiter = TokenRateLimiter(window_seconds=0.05, limit=10)
    limiter.record(10)
    wrapped = ThrottledLlm(model="fake-model", inner=_FakeInner(), limiter=limiter)

    started = time.monotonic()
    responses = [r async for r in wrapped.generate_content_async(cast(Any, LlmRequest()))]

    assert time.monotonic() - started >= 0.05
    assert len(responses) == 2


async def test_throttled_llm_records_only_the_final_non_partial_response() -> None:
    """The partial chunk carries no `usage_metadata`; only the 42-token final response
    should land in the window — enough on its own to reach this limiter's ceiling."""
    limiter = TokenRateLimiter(window_seconds=0.05, limit=42)
    wrapped = ThrottledLlm(model="fake-model", inner=_FakeInner(), limiter=limiter)

    [r async for r in wrapped.generate_content_async(cast(Any, LlmRequest()))]

    started = time.monotonic()
    await asyncio.wait_for(limiter.wait_for_capacity(), timeout=1.0)
    assert time.monotonic() - started >= 0.05
