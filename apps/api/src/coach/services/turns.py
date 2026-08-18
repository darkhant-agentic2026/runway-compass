"""Turns: generation that outlives the socket that asked for it.

docs/04-api-contract.md#surviving-client-disconnects is the specification, and the
requirement it exists for is one sentence: *generation must complete even if the client
disconnects, so inference is not wasted.*

The shape that satisfies it:

1. `start` creates `turns/{turnId}`, hands a coroutine to the `TurnRegistry`, and
   returns. It does **not** await generation. If this method ever grows an `await` on the
   generation task, the guarantee is gone and no test in the disconnect matrix would
   necessarily notice — they would all still pass, more slowly.
2. The detached task publishes to the `StreamBroker` and checkpoints to Firestore. Both
   are indifferent to whether anyone is listening.
3. Cancellation has exactly two sources: the cancel endpoint and process shutdown. A
   closed socket is neither.

The cancel endpoint may be served by an instance that does not own the turn, so it does
two things — cancels locally if it can, and sets a flag on the turn document either way.
`_watch_cancellation` below is what makes the second one arrive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from google.adk.agents._streaming_mode import StreamingMode
from google.adk.agents.run_config import RunConfig
from google.adk.events.event import Event
from google.genai import types

from coach.agents.runner import RunnerFactory
from coach.core.config import Settings
from coach.core.errors import Conflict, NotFound, ValidationProblem
from coach.core.ids import turn_id as new_turn_id
from coach.core.principal import Principal
from coach.repositories.turns import TurnRepository
from coach.services.models import Turn, TurnError, TurnStatus
from coach.services.sessions import SessionService
from coach.services.uploads import UploadService
from coach.ws.broker import StreamBroker
from coach.ws.checkpoints import CheckpointWriter
from coach.ws.protocol import Delta, ToolCall, ToolResult, TurnComplete, TurnStart
from coach.ws.protocol import TurnError as TurnErrorFrame
from coach.ws.registry import TurnRegistry

logger = logging.getLogger(__name__)

#: How often the generation task looks for a cancellation requested by another instance.
#: Off the hot path deliberately — a Firestore read per streamed token would cost more
#: than the generation it is watching.
CANCEL_POLL_SECONDS = 1.0

#: The cancel path's error code. docs/04-api-contract.md has no `turn_cancelled` frame,
#: so a cancelled turn is announced as a `turn_error` that is explicitly not retryable —
#: the user asked for this, and offering them a retry button would be wrong.
CANCELLED_CODE = "cancelled"

MAX_TURN_TEXT = 32_000

#: ADK's own name for the synthetic call a `require_confirmation` tool produces
#: (`google.adk.flows.llm_flows.functions.REQUEST_CONFIRMATION_FUNCTION_CALL_NAME`).
#: Restated rather than imported, deliberately: it is a private module, and the constant
#: is also parsed by `apps/web/src/lib/transcript.ts`, which cannot import it at all. The
#: bump checklist in docs/03-agent-design.md carries the pair.
CONFIRMATION_FUNCTION_NAME = "adk_request_confirmation"


class TurnService:
    def __init__(
        self,
        settings: Settings,
        turns: TurnRepository,
        sessions: SessionService,
        uploads: UploadService,
        runners: RunnerFactory,
        registry: TurnRegistry,
        broker: StreamBroker,
        *,
        instance_id: str,
    ) -> None:
        self._settings = settings
        self._turns = turns
        self._sessions = sessions
        self._uploads = uploads
        self._runners = runners
        self._registry = registry
        self._broker = broker
        self._instance_id = instance_id
        # docs/07-infra-deploy.md: "A per-instance asyncio.Semaphore caps concurrent
        # agent runs (default 8) so a burst of background work cannot starve interactive
        # turns." Acquired inside the detached task, not in the request handler, so an
        # over-quota turn queues instead of making the user wait for a 202.
        self._slots = asyncio.Semaphore(settings.max_concurrent_agent_runs)

    # --- public API ----------------------------------------------------------------

    async def start(
        self,
        principal: Principal,
        session_id: str,
        *,
        text: str = "",
        attachments: list[dict[str, str]] | None = None,
        confirmation: tuple[str, bool] | None = None,
    ) -> Turn:
        """`POST /api/sessions/{sid}/turns` — 202, generation continues in background."""
        if self._registry.draining:
            raise Conflict(
                "This instance is shutting down and is not accepting new turns. "
                "Retry; another instance will take it."
            )
        if len(text) > MAX_TURN_TEXT:
            raise ValidationProblem(f"A turn's text is capped at {MAX_TURN_TEXT} characters.")
        await self._sessions.require_owned(principal, session_id)

        content = await self._build_content(principal, text, attachments or [], confirmation)
        if content is None:
            raise ValidationProblem("A turn needs text, an attachment, or both.")

        turn = await self._turns.create(
            Turn(
                id=new_turn_id(),
                session_id=session_id,
                owner_uid=principal.uid,
                status=TurnStatus.RUNNING,
                instance_id=self._instance_id,
            )
        )
        self._registry.spawn(turn.id, self._generate(turn, principal, content))
        return turn

    async def cancel(self, principal: Principal, session_id: str, turn_id: str) -> Turn:
        """`POST /api/sessions/{sid}/turns/{turnId}/cancel` — the only thing that stops it."""
        await self._sessions.require_owned(principal, session_id)
        turn = await self._turns.get(turn_id)
        if turn is None or turn.owner_uid != principal.uid or turn.session_id != session_id:
            raise NotFound(f"No turn {turn_id!r}.")
        if turn.status.is_terminal:
            # Idempotent: cancelling a finished turn is a no-op, so a double-click or a
            # retry cannot turn a completed answer into an error.
            return turn

        # Both, always. The local cancel is immediate when this instance owns the turn;
        # the flag is what reaches the owner when it does not, and writing it
        # unconditionally means the two paths need no agreement about who is who.
        cancelled_here = self._registry.cancel(turn_id)
        await self._turns.request_cancel(turn_id)
        logger.info(
            "turn cancellation requested",
            extra={"turn_id": turn_id, "local": cancelled_here},
        )
        refreshed = await self._turns.get(turn_id)
        return refreshed or turn

    async def get(self, principal: Principal, turn_id: str) -> Turn:
        turn = await self._turns.get_with_pages(turn_id)
        if turn is None or turn.owner_uid != principal.uid:
            raise NotFound(f"No turn {turn_id!r}.")
        return turn

    async def replay_frames(self, turn: Turn, last_seq: int) -> list[dict[str, Any]]:
        """The frames a client resuming at `last_seq` has not seen.

        Checkpoint slices first, then the terminal frame if the turn has already
        finished. Tool-activity frames are deliberately absent: they are not
        checkpointed, and a resumed client rebuilds chips from the finalized transcript
        rather than from a replayed stream (docs/02-data-model.md#turnsturnid).
        """
        frames: list[dict[str, Any]] = [
            Delta(turn_id=turn.id, seq=seq, text=text).to_wire()
            for seq, text in turn.replay_from(last_seq)
        ]
        if turn.status is TurnStatus.COMPLETE:
            frames.append(TurnComplete(turn_id=turn.id, seq=turn.last_seq).to_wire())
        elif turn.status is TurnStatus.CANCELLED:
            frames.append(
                TurnErrorFrame(
                    turn_id=turn.id,
                    seq=turn.last_seq,
                    code=CANCELLED_CODE,
                    message="This turn was cancelled.",
                    retryable=False,
                ).to_wire()
            )
        elif turn.status is TurnStatus.FAILED:
            error = turn.error or TurnError(code="failed", message="Generation failed.")
            frames.append(
                TurnErrorFrame(
                    turn_id=turn.id,
                    seq=turn.last_seq,
                    code=error.code,
                    message=error.message,
                    retryable=error.retryable,
                ).to_wire()
            )
        return frames

    def owns(self, turn: Turn) -> bool:
        """Whether this instance holds the generation task for `turn`.

        Decides which of the two resume paths a reconnect takes: attach to the live
        broker, or follow the turn document.
        """
        return turn.instance_id == self._instance_id and self._registry.is_running(turn.id)

    async def drain(self, timeout: float) -> None:
        """`SIGTERM`: stop accepting turns, wait, then fail whatever is left.

        A survivor is marked `failed, retryable` **with `endedAt` set**, which is doing
        two jobs: it gives the client a retry affordance, and it is what lets the
        Firestore TTL eventually collect the document — a turn that never reaches a
        terminal state never expires (docs/02-data-model.md#retention).
        """
        survivors = await self._registry.drain(timeout)
        for turn_id in survivors:
            await self._turns.finish(
                turn_id,
                TurnStatus.FAILED,
                error=TurnError(
                    code="instance-shutdown",
                    message="The instance generating this turn was shut down.",
                    retryable=True,
                ),
            )

    # --- generation ------------------------------------------------------------------

    async def _build_content(
        self,
        principal: Principal,
        text: str,
        attachments: list[dict[str, str]],
        confirmation: tuple[str, bool] | None = None,
    ) -> types.Content | None:
        parts: list[types.Part] = []
        if confirmation is not None:
            # The answer to a `require_confirmation` tool. ADK's request-confirmation flow
            # looks for a function *response* to `adk_request_confirmation` on the last
            # user-authored event and resumes the original call from it, so this part has
            # to carry the call id it is answering — which is why the client sends one.
            call_id, confirmed = confirmation
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call_id,
                        name=CONFIRMATION_FUNCTION_NAME,
                        response={"confirmed": confirmed},
                    )
                )
            )
        if text.strip():
            parts.append(types.Part(text=text))
        for attachment in attachments:
            upload = await self._uploads.resolve(principal, attachment["uploadId"])
            part = types.Part.from_uri(file_uri=upload.uri, mime_type=upload.mime_type)
            # `display_name` is the only place the user's own filename survives into the
            # transcript. The artifact is named `user:{uploadId}` and the `gs://` URI has
            # no human segment, so without this a reopened conversation can say that a
            # file was attached but not which one.
            if part.file_data is not None and upload.filename:
                part.file_data.display_name = upload.filename
            parts.append(part)
        return types.Content(role="user", parts=parts) if parts else None

    async def _generate(self, turn: Turn, principal: Principal, content: types.Content) -> None:
        """The detached task. Nothing awaits this; the registry only holds it."""
        seq = 0
        event_ids: list[str] = []
        streamed = ""
        watcher: asyncio.Task[None] | None = None

        await self._broker.publish(
            turn.id, TurnStart(turn_id=turn.id, session_id=turn.session_id).to_wire()
        )

        try:
            async with self._slots:
                watcher = asyncio.create_task(self._watch_cancellation(turn.id))
                async with CheckpointWriter(self._turns, turn.id) as writer:
                    runner = self._runners.runner()
                    async for event in runner.run_async(
                        user_id=principal.uid,
                        session_id=turn.session_id,
                        new_message=content,
                        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
                    ):
                        seq, streamed = await self._emit(turn, event, seq, streamed, writer)
                        if not event.partial and event.id:
                            event_ids.append(event.id)

                seq += 1
                await self._turns.finish(turn.id, TurnStatus.COMPLETE, last_seq=seq)
                await self._broker.publish(
                    turn.id,
                    TurnComplete(turn_id=turn.id, seq=seq, event_ids=event_ids).to_wire(),
                )
        except asyncio.CancelledError:
            # The user's cancel, or shutdown. Either way the turn ends here and the
            # subscribers are told, which is the "notifies subscribers" half of the
            # cancel contract.
            seq += 1
            await self._turns.finish(turn.id, TurnStatus.CANCELLED, last_seq=seq)
            await self._broker.publish(
                turn.id,
                TurnErrorFrame(
                    turn_id=turn.id,
                    seq=seq,
                    code=CANCELLED_CODE,
                    message="This turn was cancelled.",
                    retryable=False,
                ).to_wire(),
            )
            raise
        except Exception as exc:
            logger.exception("turn generation failed", extra={"turn_id": turn.id})
            seq += 1
            error = classify_generation_error(exc)
            await self._turns.finish(turn.id, TurnStatus.FAILED, last_seq=seq, error=error)
            await self._broker.publish(
                turn.id,
                TurnErrorFrame(
                    turn_id=turn.id,
                    seq=seq,
                    code=error.code,
                    message=error.message,
                    retryable=error.retryable,
                ).to_wire(),
            )
        finally:
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            await self._broker.forget(turn.id)

    async def _emit(
        self,
        turn: Turn,
        event: Event,
        seq: int,
        streamed: str,
        writer: CheckpointWriter,
    ) -> tuple[int, str]:
        """Translate one ADK event into stream frames. Returns the new `(seq, streamed)`.

        Text is taken from **partial** events, which carry increments. The aggregated
        non-partial event repeats the whole message, so publishing both would render
        every answer twice — ADK's own `StreamingMode.SSE` documentation calls this out.
        The `streamed` accumulator lets the final event contribute the tail instead: a
        model that did not stream at all (or stopped mid-message) still produces complete
        text rather than silence.
        """
        text = _text_of(event)

        if event.partial:
            if text:
                seq += 1
                await self._broker.publish(
                    turn.id, Delta(turn_id=turn.id, seq=seq, text=text).to_wire()
                )
                await writer.add(seq, text)
                streamed += text
            return seq, streamed

        if text:
            remainder = (
                text[len(streamed) :]
                if streamed and text.startswith(streamed)
                else ("" if streamed else text)
            )
            if remainder:
                seq += 1
                await self._broker.publish(
                    turn.id, Delta(turn_id=turn.id, seq=seq, text=remainder).to_wire()
                )
                await writer.add(seq, remainder)
            streamed = ""

        for call in event.get_function_calls():
            seq += 1
            # Flushed first so the checkpoint slice does not straddle the seq this frame
            # consumes; see `CheckpointWriter.add`.
            await writer.flush()
            await self._broker.publish(
                turn.id,
                ToolCall(
                    turn_id=turn.id,
                    seq=seq,
                    name=call.name or "",
                    args_preview=dict(call.args or {}),
                ).to_wire(),
            )
            await self._turns.advance_seq(turn.id, seq)

        for response in event.get_function_responses():
            seq += 1
            await writer.flush()
            await self._broker.publish(
                turn.id,
                ToolResult(
                    turn_id=turn.id,
                    seq=seq,
                    name=response.name or "",
                    ok=_tool_succeeded(response.response),
                ).to_wire(),
            )
            await self._turns.advance_seq(turn.id, seq)

        return seq, streamed

    async def _watch_cancellation(self, turn_id: str) -> None:
        """Poll for a cancellation requested elsewhere, and act on it here.

        `asyncio.current_task()` is captured by the caller's task, not this one, so the
        cancel goes through the registry — the same path the local cancel takes, which
        keeps there being one way a turn stops.
        """
        while True:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
            try:
                if await self._turns.is_cancellation_requested(turn_id):
                    self._registry.cancel(turn_id)
                    return
            except Exception:  # pragma: no cover - a poll failure must not end the turn
                logger.warning("cancellation poll failed", extra={"turn_id": turn_id})


#: Model-side 4xx codes that a retry can actually resolve. Everything else in the 4xx
#: range describes the *request* — a model that does not exist, a payload that is too
#: large, credentials that are not entitled — and will fail identically forever.
_RETRYABLE_CLIENT_CODES = frozenset({408, 409, 425, 429})


def classify_generation_error(exc: BaseException) -> TurnError:
    """Turn an exception from the model into a `turn_error` the UI can act on.

    `retryable` drives whether the user is offered "You can try again", so getting it
    wrong is not cosmetic — it is the difference between a useful prompt and an
    instruction to keep doing something that cannot work. A misconfigured `MODEL_NAME`
    surfaced exactly that: Vertex answered `404 … Publisher model … was not found`, and
    the UI invited the user to retry it.

    The message is the provider's own sentence rather than the whole response body. For
    the 404 above that sentence names the model, the project, and the docs page for
    regional availability, which is the diagnosis; the full payload is on the log line
    above, where it belongs.
    """
    from google.genai import errors as genai_errors

    if isinstance(exc, genai_errors.APIError):
        code = int(exc.code or 0)
        retryable = code >= 500 or code in _RETRYABLE_CLIENT_CODES
        message = str(exc.message or "").strip() or f"The model returned {code}."
        return TurnError(code=f"model-{code}", message=message, retryable=retryable)

    # Anything else — a bug of ours, a dropped connection — is assumed transient. Being
    # wrong in this direction costs a pointless retry; the other way round it silently
    # strands a user whose next attempt would have worked.
    return TurnError(
        code=type(exc).__name__,
        message=str(exc) or "Generation failed.",
        retryable=True,
    )


def _tool_succeeded(payload: object) -> bool:
    """Whether a tool result reports success, for the `tool_result` frame's `ok`.

    Domain tools answer `{"ok": …}` — a refused guard is a *result*, not an exception
    (`agents/tools.py`) — so a frame that hard-coded `True` told the user their board had
    been changed when the change had been turned down.

    Anything without a boolean `ok` counts as success here, because the alternative reads
    worse: ADK's own placeholder for a call awaiting confirmation would render as a failed
    step. The stored transcript makes the finer distinction, where it has the whole turn
    to look at rather than one frame (`lib/transcript.ts`, `TranscriptTool.ok`).
    """
    if isinstance(payload, dict) and isinstance(payload.get("ok"), bool):
        return bool(payload["ok"])
    return True


def _text_of(event: Event) -> str:
    """Every text part of an event, concatenated. Thought parts are not transcript."""
    if event.content is None or not event.content.parts:
        return ""
    return "".join(
        part.text
        for part in event.content.parts
        if part.text and not getattr(part, "thought", False)
    )


__all__ = [
    "CANCELLED_CODE",
    "CANCEL_POLL_SECONDS",
    "CONFIRMATION_FUNCTION_NAME",
    "MAX_TURN_TEXT",
    "TurnService",
    "classify_generation_error",
]
