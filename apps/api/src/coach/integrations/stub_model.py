"""A deterministic model, for the end-to-end harness.

docs/08-testing.md: "Runs against the gcloud Firestore emulator and **a stubbed model
server**, so e2e is deterministic."

This is a stubbed *model* rather than a stubbed *server*. A server would have to speak
the Gemini wire protocol convincingly enough for `google.genai` to parse it, which is a
large surface to maintain for no extra confidence — the thing under test in golden flow #4
is the socket, the checkpoints, and the resume path, none of which can tell where the
tokens came from.

**Guarded to `ENV=local`.** `Settings` refuses `MODEL_BACKEND=stub` for any other `ENV`,
so a deployed revision cannot silently serve canned answers — which would be a far worse
failure than not starting, because it would look like the product working.

The reply is derived from the prompt so that a test can assert an exact string, and it is
emitted in many small chunks with a pause between them so that a test has a window in
which to kill the socket mid-stream. That pacing is the whole reason this exists rather
than a one-shot canned response.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

#: Milliseconds between chunks. Overridable so a slow CI machine can widen the window
#: golden flow #4 disconnects inside without the test having to guess.
DELAY_ENV_VAR = "STUB_MODEL_DELAY_MS"
DEFAULT_DELAY_MS = 40

PREFIX = "Here is what I think about "
SUFFIX = (
    " Let us break it down together, one step at a time, and check your understanding as we go."
)


def stub_reply(prompt: str) -> str:
    """The exact text the stub will produce for `prompt`.

    Exported so a test can assert against it without hard-coding the same string twice.
    """
    return f"{PREFIX}{prompt.strip() or 'your task'}.{SUFFIX}"


class StubModel(BaseLlm):
    """Echoes the prompt back, slowly, in many pieces."""

    model: str = "stub-model"

    @property
    def _delay_seconds(self) -> float:
        try:
            return int(os.environ.get(DELAY_ENV_VAR, DEFAULT_DELAY_MS)) / 1000
        except ValueError:
            return DEFAULT_DELAY_MS / 1000

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        reply = stub_reply(_last_user_text(llm_request))
        # Split on spaces, keeping them, so the concatenation of the chunks is exactly
        # `reply` — the assertion golden flow #4 rests on is character equality between
        # an interrupted run and an uninterrupted one.
        chunks = [word + " " for word in reply.split(" ")]
        chunks[-1] = chunks[-1].rstrip()

        for chunk in chunks:
            await asyncio.sleep(self._delay_seconds)
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=chunk)]),
                partial=True,
            )
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="".join(chunks))])
        )


def _last_user_text(llm_request: Any) -> str:
    """The most recent user message in the request, or an empty string."""
    contents = getattr(llm_request, "contents", None) or []
    for content in reversed(list(contents)):
        if getattr(content, "role", None) != "user":
            continue
        parts = getattr(content, "parts", None) or []
        text = "".join(part.text for part in parts if getattr(part, "text", None))
        if text:
            return text
    return ""


__all__ = ["DEFAULT_DELAY_MS", "DELAY_ENV_VAR", "StubModel", "stub_reply"]
