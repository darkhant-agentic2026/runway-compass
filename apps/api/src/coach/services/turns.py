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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from google.adk.agents._streaming_mode import StreamingMode
from google.adk.agents.run_config import RunConfig
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.genai import types

from coach.agents.runner import RunnerFactory
from coach.core.config import Settings
from coach.core.errors import Conflict, NotFound, ValidationProblem
from coach.core.ids import turn_id as new_turn_id
from coach.core.principal import Principal
from coach.repositories.turns import TurnRepository
from coach.services.models import TaskState, Turn, TurnError, TurnStatus
from coach.services.quotas import QuotaService
from coach.services.sessions import SessionService
from coach.services.tasks import TaskService
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

#: Which agent a turn's detached task drives. Not a `Settings` value and not something the
#: client chooses: `TurnService.start`'s callers are the turns router (always `coach`),
#: `ResearchService` (always `research`), and M5's `RunExecutor` — which uses `research`
#: for its research step and `propose` for the background pass over the board.
#:
#: `propose` is a *different agent*, not the coach with a different message, and that is
#: the safety rail: it carries `DomainTools.as_autonomous_tools()`, so an unattended run
#: has no `discard_task` to be talked into using
#: (docs/03-agent-design.md#safety-rails-on-autonomy).
#:
#: `"roadmap"` is `research_workflow`'s taskless sibling
#: (`agents/research_workflow.py::build_roadmap_workflow`) — additive plumbing only.
#: `ResearchService`/`RunExecutor` do not pass it: every run they start still uses
#: `"research"`, taskless or not, so this choice is reachable today only from a caller
#: that names it directly (tests, until a later change makes it the taskless dispatch).
AgentChoice = Literal["coach", "research", "roadmap", "propose"]

#: What `"coach"` resolves to, once `start` knows whether the session is linked to a task
#: (docs/09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher).
#: `"research"`, `"roadmap"`, and `"propose"` already name a concrete agent and pass through
#: `_resolve_agent` unchanged, so this is the union `_RUNNERS` actually indexes on.
_ResolvedAgent = Literal["coach_project", "coach_task", "research", "roadmap", "propose"]

#: The resolved agent to the factory method that builds its runner. A mapping rather than a
#: chain of `if`s so that adding a fifth agent without a runner is a `KeyError` at the call
#: site instead of a silent fall-through to one of the coaches — which would put a turn on
#: the wrong tool set and would look entirely normal in a transcript.
_RUNNERS: dict[str, Callable[[RunnerFactory], Runner]] = {
    "coach_project": lambda factory: factory.project_runner(),
    "coach_task": lambda factory: factory.task_runner(),
    "research": lambda factory: factory.research_runner(),
    "roadmap": lambda factory: factory.roadmap_runner(),
    "propose": lambda factory: factory.autonomous_runner(),
}


def _resolve_agent(agent: AgentChoice, task_id: str | None) -> _ResolvedAgent:
    """`"coach"` is the router's request for "the interactive agent"; which one that
    means depends on whether the turn's session is linked to a task — `project_coach` for
    the board-level (and intake) conversation, `task_teacher` for one task's own. Deciding
    it here, once, rather than in each agent's instruction is the structural half of the
    M6 fix: `task_teacher` simply has no `add_task` tool, so a learner describing extra
    work inside a task's conversation cannot land it on the board by construction.

    `"research"`, `"roadmap"`, and `"propose"` already name a concrete agent and are
    untouched.
    """
    if agent != "coach":
        return agent
    return "coach_task" if task_id else "coach_project"


@dataclass(frozen=True, slots=True)
class Confirmation:
    """The learner's answer to a tool that asked first.

    A dataclass rather than the `(call_id, confirmed)` tuple this used to be: `ask_learner`
    answers with a *selection*, not a yes or no, and threading a third element through a
    tuple is how the wrong one gets read.
    """

    function_call_id: str
    confirmed: bool
    payload: dict[str, Any] | None = None


#: ADK's own name for the synthetic call a `require_confirmation` tool produces
#: (`google.adk.flows.llm_flows.functions.REQUEST_CONFIRMATION_FUNCTION_CALL_NAME`).
#: Restated rather than imported, deliberately: it is a private module, and the constant
#: is also parsed by `apps/web/src/lib/transcript.ts`, which cannot import it at all. The
#: bump checklist in docs/03-agent-design.md carries the pair.
CONFIRMATION_FUNCTION_NAME = "adk_request_confirmation"

#: Strong references to `on_finished` callbacks in flight. `asyncio` holds only a weak
#: reference to a task, so one with nothing pointing at it can be collected mid-`await` —
#: which for `ResearchService` would mean a lease left to expire on its own five-minute TTL.
_FINISHERS: set[asyncio.Task[None]] = set()


def _run_detached(coroutine: Awaitable[None]) -> None:
    """Schedule `coroutine` from a done callback, which cannot await it."""
    task = asyncio.ensure_future(coroutine)
    _FINISHERS.add(task)
    task.add_done_callback(_FINISHERS.discard)


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
        quotas: QuotaService,
        tasks: TaskService,
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
        self._quotas = quotas
        self._tasks = tasks
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
        context_attachments: list[dict[str, str]] | None = None,
        confirmation: Confirmation | None = None,
        agent: AgentChoice = "coach",
        state_delta: dict[str, Any] | None = None,
        on_finished: Callable[[], Awaitable[None]] | None = None,
    ) -> Turn:
        """`POST /api/sessions/{sid}/turns` — 202, generation continues in background.

        `context_attachments` is distinct from `attachments`: the latter is `{uploadId,
        mimeType}`, resolved through `UploadService` because the caller only has an id
        the learner just sent. The former already carries a resolved `{uri, mimeType,
        displayName}` — `ResearchService`/`RunExecutor` use it to carry a task's own
        uploads into a research turn automatically
        (`SessionService.list_attachments`), where there is no fresh upload to resolve,
        only files the conversation already has.

        `agent` selects which runner the detached task drives. A research run is a turn
        like any other — same checkpointing, same broker, same disconnect guarantee — and
        differs only in which agent reads the message. Threading it here rather than
        giving research its own generation loop is what keeps
        docs/04-api-contract.md#surviving-client-disconnects true of research as well: a
        second loop would be a second place for the guarantee to be quietly lost.

        `on_finished` runs once the generation task is over, whatever its outcome. It is a
        **done callback, never an await**: the module docstring's first rule is that this
        method must not await generation, and a caller that needed to know when a turn
        ended would otherwise be tempted to. `ResearchService` uses it to close its ledger
        row and drop the project's agent lease at the moment generation stops, rather than
        at the next tick of a poller — a lease outliving its run by even half a second is a
        button the learner can press and the server refuses.

        `state_delta` is invocation state ADK applies to the user event *before* the root
        node runs, which is before `agents/prompt.py`'s callback. M5 carries the run id
        through it, so `post_research_report` can key the report document on the run and a
        retried step overwrites instead of duplicating. Keys must be `temp:`-prefixed —
        ADK trims temp deltas before persistence, and session `state` is stored as a JSON
        string, so a plain key would re-serialize onto the session document forever.
        """
        if self._registry.draining:
            raise Conflict(
                "This instance is shutting down and is not accepting new turns. "
                "Retry; another instance will take it."
            )
        if len(text) > MAX_TURN_TEXT:
            raise ValidationProblem(f"A turn's text is capped at {MAX_TURN_TEXT} characters.")
        linkage = await self._sessions.require_owned(principal, session_id)
        resolved_agent = _resolve_agent(agent, linkage.task_id)
        if resolved_agent == "coach_task" and linkage.task_id is not None:
            await self._start_task_if_not_started(principal, linkage.task_id)
        # docs/02-data-model.md#usage-quotas-m8-quotas: the one gate every interactive
        # turn, research run, and autonomous pass shares, since they all reach here.
        # Raises `QuotaExceeded` before a turn document exists, so a blocked attempt costs
        # nothing and there is no turn to resume.
        await self._quotas.require_available(principal.uid)

        content = await self._build_content(
            principal, text, attachments or [], confirmation, context_attachments or []
        )
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
        task = self._registry.spawn(
            turn.id, self._generate(turn, principal, content, resolved_agent, state_delta)
        )
        if on_finished is not None:
            task.add_done_callback(lambda _: _run_detached(on_finished()))
        return turn

    async def _start_task_if_not_started(self, principal: Principal, task_id: str) -> None:
        """The learner's first message in a task's own session moves it off the board's
        "not started" pile — the same move the row's own "Start" action makes, just
        triggered by opening the conversation and typing rather than by a click.

        Checked against the task's *current* state, not "is this the first message":
        every message into a `coach_task` session comes through here, so the very first
        one is the only one that ever finds `NOT_STARTED` and the transition is
        naturally a no-op afterwards. Silently skipped for any other state — `draft`
        (no plan yet), `completed`, `postponed`, or `discarded` — since only
        `NOT_STARTED -> IN_PROGRESS` is what "starting by talking about it" means; those
        others have their own explicit actions and `set_state` would raise on most of
        them anyway.
        """
        task = await self._tasks.resolve(principal, task_id)
        if task.state is TaskState.NOT_STARTED:
            await self._tasks.set_state(principal, task_id, TaskState.IN_PROGRESS)

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
        confirmation: Confirmation | None = None,
        context_attachments: list[dict[str, str]] | None = None,
    ) -> types.Content | None:
        parts: list[types.Part] = []
        if confirmation is not None:
            # The answer to a `require_confirmation` tool. ADK's request-confirmation flow
            # looks for a function *response* to `adk_request_confirmation` on the last
            # user-authored event and resumes the original call from it, so this part has
            # to carry the call id it is answering — which is why the client sends one.
            # `ToolConfirmation`'s own field names, because ADK validates this dict
            # straight into that model (`from_response_dict`) and it is `extra="forbid"`.
            # `payload` is omitted rather than sent as `None` for the yes/no gates, so
            # their response stays byte-identical to what M3 sent.
            response: dict[str, Any] = {"confirmed": confirmation.confirmed}
            if confirmation.payload is not None:
                response["payload"] = confirmation.payload
            parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=confirmation.function_call_id,
                        name=CONFIRMATION_FUNCTION_NAME,
                        response=response,
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
        for attachment in context_attachments or []:
            # Already resolved — `SessionService.list_attachments` read these straight off
            # stored events, so there is no upload id to look up and no ownership check to
            # repeat: reading the session that produced them already proved that.
            part = types.Part.from_uri(
                file_uri=attachment["uri"], mime_type=attachment.get("mimeType") or None
            )
            if part.file_data is not None and attachment.get("displayName"):
                part.file_data.display_name = attachment["displayName"]
            parts.append(part)
        return types.Content(role="user", parts=parts) if parts else None

    async def _generate(
        self,
        turn: Turn,
        principal: Principal,
        content: types.Content,
        agent: _ResolvedAgent,
        state_delta: dict[str, Any] | None = None,
    ) -> None:
        """The detached task. Nothing awaits this; the registry only holds it."""
        seq = 0
        event_ids: list[str] = []
        streamed = ""
        total_tokens = 0
        watcher: asyncio.Task[None] | None = None

        await self._broker.publish(
            turn.id, TurnStart(turn_id=turn.id, session_id=turn.session_id).to_wire()
        )

        try:
            async with self._slots:
                watcher = asyncio.create_task(self._watch_cancellation(turn.id))
                async with CheckpointWriter(self._turns, turn.id) as writer:
                    runner = _RUNNERS[agent](self._runners)
                    async for event in runner.run_async(
                        user_id=principal.uid,
                        session_id=turn.session_id,
                        new_message=content,
                        state_delta=state_delta or None,
                        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
                    ):
                        # `usage_metadata` lands on the aggregated, non-partial response
                        # each model call ends with (the same one `_emit` already treats
                        # as "this call's whole message") — never on a partial chunk, so
                        # summing it here charges each model call in this turn exactly
                        # once. docs/02-data-model.md#usage-quotas-m8-quotas.
                        if not event.partial and event.usage_metadata is not None:
                            total_tokens += event.usage_metadata.total_token_count or 0
                        seq, streamed = await self._emit(turn, event, seq, streamed, writer)
                        if not event.partial and event.id:
                            event_ids.append(event.id)

                seq += 1
                await self._turns.finish(turn.id, TurnStatus.COMPLETE, last_seq=seq)
                # docs/09-roadmap.md#research-concurrency: a read against the points this
                # turn is *about* to spend, not yet written by `record_spend` below — so
                # the hint reflects the balance the learner will actually see once this
                # turn's own cost lands, without a second write racing that one.
                points = await self._quotas.points_hint(principal.uid, total_tokens)
                await self._broker.publish(
                    turn.id,
                    TurnComplete(
                        turn_id=turn.id,
                        seq=seq,
                        event_ids=event_ids,
                        points_remaining=points[0] if points else None,
                        points_threshold=points[1] if points else None,
                    ).to_wire(),
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
            # Recorded regardless of outcome — tokens already spent are already spent,
            # whether the turn ended `complete`, `cancelled`, or `failed`.
            await self._quotas.record_spend(principal.uid, total_tokens)

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
        # The node that produced this event — the root agent for an ordinary chat turn,
        # one of several node names across a single `research_workflow`/
        # `build_roadmap_workflow` turn. Threaded onto every frame this event produces so
        # the frontend can tell a new author's message apart from a continuation of the
        # previous one within the same turn (`ws/protocol.py`'s `Delta` docstring).
        author = event.author or ""

        if event.partial:
            if text:
                seq += 1
                await self._broker.publish(
                    turn.id, Delta(turn_id=turn.id, seq=seq, text=text, author=author).to_wire()
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
                    turn.id,
                    Delta(turn_id=turn.id, seq=seq, text=remainder, author=author).to_wire(),
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
                    author=author,
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
                    author=author,
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
