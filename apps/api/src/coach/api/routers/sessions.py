"""Sessions and turns (`/api/sessions`, plus the get-or-create route under a task).

docs/04-api-contract.md#sessions--turns. The one shape worth pointing at is
`POST /api/sessions/{sid}/turns`, which returns **202** and does not await generation:

> The handler creates `turns/{turnId}`, spawns a detached `asyncio.Task`, and returns. It
> does **not** await generation. Streaming is observed over the WebSocket.

`POST /api/sessions/{sid}/research` and `.../roadmap` are the other two endpoints in that
table, and 202 for a different reason since M9: each takes the project's agent lease, opens
a run in the ledger, and hands it to the same Cloud Tasks queue a scheduled run goes
through, rather than starting a turn in this process — `services/research.py`'s module
docstring explains why. The client watches the run (`GET /api/runs/{runId}`), which carries
`turnId`/`sessionId` once the queue actually starts it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response, status

from coach.api.deps import CurrentUser, Research, Sessions, Turns
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import (
    ResearchRequest,
    ResearchResponse,
    RoadmapRequest,
    SessionEventsResponse,
    SessionEventView,
    SessionResponse,
    TurnAcceptedResponse,
    TurnRequest,
    TurnStatusResponse,
)
from coach.services.models import SessionSummary
from coach.services.turns import Confirmation

router = APIRouter(prefix="/api", tags=["sessions"])


@router.post(
    "/tasks/{task_id}/session",
    response_model=SessionResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def get_or_create_task_session(
    task_id: str, principal: CurrentUser, sessions: Sessions
) -> SessionResponse:
    """Get-or-create the task's session. 200 either way — this is not a create endpoint.

    A task has at most one session for its whole life, so calling this twice is normal
    (every workspace open does it) and must not be a conflict.
    """
    summary: SessionSummary = await sessions.get_or_create_for_task(principal, task_id)
    return SessionResponse(session=summary)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: str, principal: CurrentUser, sessions: Sessions
) -> SessionResponse:
    return SessionResponse(session=await sessions.get(principal, session_id))


@router.get("/sessions/{session_id}/events", response_model=SessionEventsResponse)
async def list_session_events(
    session_id: str,
    principal: CurrentUser,
    sessions: Sessions,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> SessionEventsResponse:
    """Transcript hydration, paged by `seq`.

    `nextAfterSeq` is echoed rather than computed by the client so that an empty page and
    a full one are handled the same way: keep asking with the value you were given.
    """
    events = await sessions.list_events(principal, session_id, after_seq=after_seq, limit=limit)
    return SessionEventsResponse(
        events=[
            SessionEventView(seq=event.seq, event_id=event.event_id, event=event.event_data)
            for event in events
        ],
        next_after_seq=events[-1].seq if events else after_seq,
        has_more=len(events) == limit,
    )


@router.get(
    "/sessions/{session_id}/events/{seq}/attachments/{index}",
    response_class=Response,
    responses={200: {"content": {"*/*": {}}}},
)
async def get_event_attachment(
    session_id: str, seq: int, index: int, principal: CurrentUser, sessions: Sessions
) -> Response:
    """The bytes of one attachment, for the transcript's image previews.

    Not in the contract's endpoint table: previews are not in it either, and rendering one
    needs the bytes. Addressed by `(session, seq, index)` because a session lives under the
    caller's uid, so reaching an event at all already proves ownership — no storage path
    arrives from the client and none is validated.

    Authenticated like every other `/api/*` route, which is why the SPA fetches this and
    turns it into a blob URL rather than putting it in an `<img src>`: an `<img>` cannot
    carry a bearer token, and inventing a second, URL-based way in would undo
    docs/00-overview.md's "one auth path".
    """
    data, mime_type, filename = await sessions.attachment_bytes(
        principal, session_id, seq, index
    )
    return Response(
        content=data,
        media_type=mime_type or "application/octet-stream",
        headers={
            # `inline` so a preview renders rather than downloading, and the real filename
            # so "save as" offers something recognisable.
            "Content-Disposition": f'inline; filename="{filename or "attachment"}"',
            # The artifact is immutable once written, so this can be cached hard. Private:
            # it is one user's file behind an authenticated request.
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.post(
    "/sessions/{session_id}/turns",
    response_model=TurnAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(idempotency_guard)],
)
async def start_turn(
    session_id: str, body: TurnRequest, principal: CurrentUser, turns: Turns
) -> TurnAcceptedResponse:
    """202, immediately. Generation continues in a detached task."""
    turn = await turns.start(
        principal,
        session_id,
        text=body.text,
        attachments=[attachment.model_dump(by_alias=True) for attachment in body.attachments],
        confirmation=(
            Confirmation(
                function_call_id=body.confirmation.function_call_id,
                confirmed=body.confirmation.confirmed,
                payload=body.confirmation.payload,
            )
            if body.confirmation is not None
            else None
        ),
    )
    return TurnAcceptedResponse(
        turn_id=turn.id, session_id=session_id, status=turn.status, start_seq=0
    )


@router.post(
    "/sessions/{session_id}/research",
    response_model=ResearchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(idempotency_guard)],
)
async def start_research(
    session_id: str,
    body: ResearchRequest,
    principal: CurrentUser,
    research: Research,
) -> ResearchResponse:
    """Research this task now — the manual trigger, on the shared run path.

    202, but since M9 for a different reason than `POST /turns`: the run is created and
    handed to the same Cloud Tasks queue a scheduled run goes through, not started as a
    detached task in this process — a multi-minute research run has to survive the learner
    closing the tab, which a queued Cloud Run request does and a background asyncio task on
    a scaled-to-zero instance does not. `turnId` in the response is `null`; the client polls
    `GET /api/runs/{runId}` for it. A `409` means the project's agent lease is held, and
    carries the in-flight `runId` so the client can attach to that run rather than starting
    a duplicate (docs/04-api-contract.md#post-apisessionssidresearch).
    """
    run = await research.start_manual(
        principal,
        session_id,
        reason=body.reason,
        budget_minutes_override=body.budget_minutes_override,
        force=body.force,
        attachments=[attachment.model_dump(by_alias=True) for attachment in body.attachments],
    )
    return ResearchResponse(
        run_id=run.id, turn_id=run.turn_id, session_id=run.session_id or "", mode=run.mode
    )


@router.post(
    "/sessions/{session_id}/roadmap",
    response_model=ResearchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(idempotency_guard)],
)
async def start_roadmap(
    session_id: str,
    body: RoadmapRequest,
    principal: CurrentUser,
    research: Research,
) -> ResearchResponse:
    """Build a study plan for the project as a whole — `task_proposer` -> `plan_tailor`.

    202 like `POST /research`, queued the same way and for the same reason
    (`ResearchService.start_roadmap`); the response shape is identical, which is why it
    reuses `ResearchResponse` rather than a new one. Taskless only: `session_id` must not
    be linked to a task
    (docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer).
    A `409` means the project's agent lease is held and carries the in-flight `runId`, same
    as `/research`.
    """
    run = await research.start_roadmap(
        principal,
        session_id,
        reason=body.reason,
        attachments=[attachment.model_dump(by_alias=True) for attachment in body.attachments],
    )
    return ResearchResponse(
        run_id=run.id, turn_id=run.turn_id, session_id=run.session_id or "", mode=run.mode
    )


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/cancel",
    response_model=TurnStatusResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def cancel_turn(
    session_id: str, turn_id: str, principal: CurrentUser, turns: Turns
) -> TurnStatusResponse:
    """Explicit user cancel — the *only* thing that stops generation."""
    turn = await turns.cancel(principal, session_id, turn_id)
    return TurnStatusResponse(turn_id=turn.id, status=turn.status, last_seq=turn.last_seq)


@router.get("/turns/{turn_id}", response_model=TurnStatusResponse)
async def get_turn(turn_id: str, principal: CurrentUser, turns: Turns) -> TurnStatusResponse:
    """Turn status without a socket.

    Not in the contract's endpoint table, and deliberately small: it exists so a client
    whose WebSocket is down can still tell a running turn from a finished one — which is
    what the "still working" state needs in order to be truthful rather than hopeful.
    """
    turn = await turns.get(principal, turn_id)
    return TurnStatusResponse(turn_id=turn.id, status=turn.status, last_seq=turn.last_seq)
