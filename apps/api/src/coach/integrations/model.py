"""The model backend, behind one switch.

docs/00-overview.md#model-configuration: primary model `gemini-3.7-flash`, via **Vertex
AI** in production (IAM-based auth, no API key to rotate) and the **Gemini API** for
local development (fastest onboarding). One abstraction, selected by `MODEL_BACKEND`.

**Gemini 3.x changed generation config in ways that break older snippets.** The
`GenerateContentConfig` built here therefore sends none of the parameters that a
pre-3.x example would reach for:

- `temperature`, `top_p`, `top_k` are **not** sent.
- `thinking_level` (`low | medium | high`) replaces `thinking_budget`. Default `medium`;
  `low` for mechanical tool steps (task splitting, reordering — M3) and `high` for
  research synthesis and the Socratic intake conversation.
- `candidate_count` is unsupported.

Adding any of them back is not a tuning decision, it is an API error waiting for the
first real request — which is why they are named here rather than merely omitted.

## Token throttling: a process-wide ceiling per Vertex model

docs/03-agent-design.md#llm-throttling. `ModelThrottle` (`agents/research_workflow.py`)
bounds *concurrency* — at most one in-flight call per research run — which shapes a
fan-out's own burst but says nothing about Vertex's own token-per-minute ceiling for the
model itself, a limit shared by every caller in the process, interactive traffic
included. `TokenRateLimiter` is the second, independent layer: an in-memory sliding
window over `total_token_count` from every completed call to the configured model,
keyed to nothing but the process (this app has exactly one `MODEL_NAME`), that makes a
call wait rather than reject once the trailing window is already at capacity.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator
from typing import Final, Literal

from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import ConfigDict

from coach.core.config import Settings

ThinkingLevel = Literal["low", "medium", "high"]

_THINKING_LEVELS: dict[ThinkingLevel, types.ThinkingLevel] = {
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}

#: 2 minutes. A constant rather than a derived number so it can be widened or narrowed
#: against Vertex's actual observed window without hunting through the limiter's logic.
TOKEN_WINDOW_SECONDS: Final[float] = 120.0

#: Vertex's per-model input-token ceiling, conservatively below the real quota so this
#: throttle acts before Vertex's own `429 RESOURCE_EXHAUSTED` does.
TOKEN_WINDOW_LIMIT: Final[int] = 200_000


def generation_config(thinking_level: ThinkingLevel = "medium") -> types.GenerateContentConfig:
    """The only place a `GenerateContentConfig` is built. See the module docstring."""
    return types.GenerateContentConfig(
        http_options=types.HttpOptions(
            retry_options=types.HttpRetryOptions(initial_delay=2, attempts=5, max_delay=120)
        ),
        thinking_config=types.ThinkingConfig(thinking_level=_THINKING_LEVELS[thinking_level]),
    )


class TokenRateLimiter:
    """Keeps this process under Vertex's own token-per-window ceiling for the one
    configured model. See the module docstring's "Token throttling" section.

    In-memory only, a deque of `(monotonic_timestamp, total_token_count)` entries pruned
    to the trailing `window_seconds` on every check. `wait_for_capacity` does not reserve
    room for the call about to be made — the size of a call is unknown until it
    completes — so this bounds the *rate* of completed usage, not a hard instantaneous
    ceiling: several calls admitted while the window still had room can still land
    concurrently and push the recorded total past `limit` once they all finish. That is
    an accepted gap, not an oversight: closing it would mean holding a call's own worth
    of budget in reserve before knowing what it costs, which is not a number this project
    (or Vertex's own response) has before the call returns.

    One instance is shared by the whole process (`ThrottledLlm` below), never one per
    run or per agent like `ModelThrottle` — the ceiling this stands in for is Vertex's
    own, shared by every caller regardless of which agent placed the call.
    """

    def __init__(
        self, *, window_seconds: float = TOKEN_WINDOW_SECONDS, limit: int = TOKEN_WINDOW_LIMIT
    ) -> None:
        self._window_seconds = window_seconds
        self._limit = limit
        self._usage: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def wait_for_capacity(self) -> None:
        """Blocks until the trailing window's recorded usage is under the limit."""
        async with self._lock:
            while (wait_seconds := self._seconds_until_capacity()) > 0:
                await asyncio.sleep(wait_seconds)

    def record(self, tokens: int) -> None:
        """Records one completed call's cost. Safe to call without a preceding wait."""
        if tokens > 0:
            self._usage.append((time.monotonic(), tokens))

    def _seconds_until_capacity(self) -> float:
        now = time.monotonic()
        while self._usage and self._usage[0][0] <= now - self._window_seconds:
            self._usage.popleft()
        if sum(tokens for _, tokens in self._usage) < self._limit:
            return 0.0
        oldest_timestamp, _ = self._usage[0]
        return oldest_timestamp + self._window_seconds - now


class ThrottledLlm(BaseLlm):
    """Wraps a real model with a shared `TokenRateLimiter`.

    Composition around `generate_content_async` rather than a `before_model_callback` /
    `after_model_callback` pair like `ModelThrottle`'s: this has to see every call from
    every agent the process builds — `project_coach`, `task_teacher`, every
    `research_workflow`/`roadmap_workflow` node, and the `search_agent` sub-agent none of
    those nodes' callbacks reach today — and `generate_content_async` is the one place
    all of them already pass through, rather than a callback this project would have to
    remember to wire onto every `LlmAgent` it ever adds.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: BaseLlm
    limiter: TokenRateLimiter

    @property
    def capabilities(self) -> LlmCapabilities:
        return self.inner.capabilities

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        await self.limiter.wait_for_capacity()
        async for response in self.inner.generate_content_async(llm_request, stream=stream):
            if response.usage_metadata is not None:
                self.limiter.record(response.usage_metadata.total_token_count or 0)
            yield response


def build_model(settings: Settings, token_limiter: TokenRateLimiter) -> BaseLlm:
    """The configured model, constructed but not yet connected.

    `Gemini` resolves its `google.genai.Client` lazily through a cached property, so
    building one costs nothing and — importantly — does not resolve credentials. That is
    what lets the app start, serve `/livez`, and run its tests without a model backend
    being reachable.

    `token_limiter` is injected rather than built here, the same reason `ModelThrottle`
    is injected into `research_workflow`'s builders: the process owns exactly one, and
    `RunnerFactory` calls this function once per agent it builds — a limiter constructed
    inside would be a fresh, unshared window per agent instead of one ceiling shared by
    all of them.
    """
    if settings.model_backend == "stub":
        # The end-to-end harness. `Settings` has already refused this for any non-local
        # `ENV`, so reaching here in a deployed environment is impossible. Left
        # unwrapped: it never calls a real model, so there is no Vertex ceiling to
        # respect, and the harness wants deterministic timing, not an extra wait.
        from coach.integrations.stub_model import StubModel

        return StubModel()
    if settings.model_backend == "gemini_api":
        # Local development. The key is validated by `Settings`, which refuses this
        # backend without one.
        inner: BaseLlm = Gemini(
            model=settings.model_name,
            client_kwargs={"api_key": settings.gemini_api_key, "vertexai": False},
        )
    else:
        inner = Gemini(
            model=settings.model_name,
            client_kwargs={
                "vertexai": True,
                "project": settings.google_cloud_project,
                "location": settings.vertex_location,
            },
        )
    return ThrottledLlm(model=settings.model_name, inner=inner, limiter=token_limiter)


__all__ = [
    "TOKEN_WINDOW_LIMIT",
    "TOKEN_WINDOW_SECONDS",
    "ThinkingLevel",
    "ThrottledLlm",
    "TokenRateLimiter",
    "build_model",
    "generation_config",
]
