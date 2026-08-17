"""The two test doubles the streaming suite needs, and nothing else.

docs/08-testing.md#streaming-and-disconnect-resilience-the-critical-suite: "Using
`httpx.ASGITransport` + a fake WebSocket client, with a scripted fake model that emits a
known delta sequence at controlled intervals."

Both doubles are deliberately shallow. The value of the disconnect matrix comes from
everything *else* being real — the real `TurnService`, the real `StreamBroker`, the real
`CheckpointWriter`, the real `CoachSessionService` against the emulator — so the only
things replaced here are the two that cannot be: a model that would cost money and vary,
and a browser.

`ScriptedModel` counts its own invocations because one assertion in that matrix is about
the model rather than about the stream: when a client disconnects mid-generation, the
turn must still complete **and the model must have been invoked exactly once**. A retry
loop that quietly re-ran generation would satisfy every other assertion in the suite.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field


class ScriptedModel(BaseLlm):
    """A `BaseLlm` that emits a known delta sequence at a controlled interval."""

    model: str = "scripted-test-model"
    #: Emitted one per partial `LlmResponse`, in order.
    chunks: list[str] = Field(default_factory=list)
    #: Seconds between chunks. Non-zero is how a test gets a window to disconnect in.
    delay: float = 0.0
    #: Raised instead of finishing, for the error path.
    fail_with: str | None = None
    #: One entry per `generate_content_async` call. See the module docstring.
    invocations: list[str] = Field(default_factory=list)

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncIterator[LlmResponse]:
        self.invocations.append(getattr(llm_request, "model", "") or self.model)
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=chunk)]),
                partial=True,
            )
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)
        # The aggregated, non-partial response every streaming turn ends with. ADK's own
        # SSE contract: partial chunks, then one final event carrying the whole message.
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="".join(self.chunks))])
        )

    @property
    def full_text(self) -> str:
        return "".join(self.chunks)


class FakeWebSocket:
    """Enough of Starlette's `WebSocket` for `SocketSession` to run against.

    Deliberately not a real socket: the disconnect tests need to drop the *client* at an
    exact point in the stream, which is awkward with a real connection and trivial here —
    `disconnect()` makes the next `receive_json` raise, which is precisely what a closed
    browser tab looks like from the server's side.
    """

    def __init__(self, app: Any = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self._inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._arrived = asyncio.Event()
        self.closed = False
        #: Set when the handshake was accepted, and the close code when it was not. The
        #: ticket test needs both: a refused ticket must close *without* accepting, or an
        #: unauthenticated peer briefly holds an open socket.
        self.accepted = False
        self.close_code: int | None = None
        self.app = app

    # --- the server's view ---------------------------------------------------------

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, Any]:
        from fastapi import WebSocketDisconnect

        frame = await self._inbound.get()
        if frame is None:
            raise WebSocketDisconnect(code=1000)
        return frame

    async def send_json(self, frame: dict[str, Any]) -> None:
        if self.closed:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect(code=1006)
        self.sent.append(frame)
        self._arrived.set()

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code

    # --- the test's view -----------------------------------------------------------

    def send(self, frame: dict[str, Any]) -> None:
        """Queue a client→server frame."""
        self._inbound.put_nowait(frame)

    def disconnect(self) -> None:
        """Close the client end. The server sees `WebSocketDisconnect`."""
        self.closed = True
        self._inbound.put_nowait(None)

    def frames(self, frame_type: str) -> list[dict[str, Any]]:
        return [frame for frame in self.sent if frame.get("type") == frame_type]

    def text(self) -> str:
        return "".join(str(frame["text"]) for frame in self.frames("delta"))

    def seqs(self) -> list[int]:
        return [int(frame["seq"]) for frame in self.sent if frame.get("seq") is not None]

    async def wait_for(self, frame_type: str, timeout: float = 5.0) -> dict[str, Any]:
        """Block until a frame of `frame_type` has been sent."""

        async def _wait() -> dict[str, Any]:
            while True:
                matching = self.frames(frame_type)
                if matching:
                    return matching[-1]
                self._arrived.clear()
                await self._arrived.wait()

        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def wait_for_seq(self, seq: int, timeout: float = 5.0) -> None:
        """Block until a frame with `seq >= seq` has been sent."""

        async def _wait() -> None:
            while True:
                if any(value >= seq for value in self.seqs()):
                    return
                self._arrived.clear()
                await self._arrived.wait()

        await asyncio.wait_for(_wait(), timeout=timeout)


__all__ = ["FakeWebSocket", "ScriptedModel"]
