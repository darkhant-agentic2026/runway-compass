"""Sessions and turns (`/api/sessions`, plus the get-or-create route under a task).

docs/04-api-contract.md#sessions--turns. The one shape worth pointing at is
`POST /api/sessions/{sid}/turns`, which returns **202** and does not await generation:

> The handler creates `turns/{turnId}`, spawns a detached `asyncio.Task`, and returns. It
> does **not** await generation. Streaming is observed over the WebSocket.

`POST /api/sessions/{sid}/research` — the manual research trigger — is the other endpoint
in that table and arrives at M4 with the research workflow it runs.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from coach.api.deps import CurrentUser, Sessions, Turns
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import (
    SessionEventsResponse,
    SessionEventView,
    SessionResponse,
    TurnAcceptedResponse,
    TurnRequest,
    TurnStatusResponse,
)
from coach.services.models import SessionSummary

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
    )
    return TurnAcceptedResponse(
        turn_id=turn.id, session_id=session_id, status=turn.status, start_seq=0
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
