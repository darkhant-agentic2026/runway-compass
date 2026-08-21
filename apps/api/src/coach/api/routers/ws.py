"""`POST /api/ws-ticket` and the `/ws` socket itself.

docs/04-api-contract.md#authentication:

> Browsers cannot set headers on `new WebSocket()`, so the client first calls
> `POST /api/ws-ticket` (authenticated by ID token, with the revocation check above) to
> get a **single-use, 60-second ticket**, then connects to `wss://…/ws?ticket=…`. The
> ticket is redeemed and deleted server-side on connect. […] **This handshake is the
> socket's *only* authorization point — nothing re-verifies mid-connection** — which is
> why it is one of the two endpoints that pays for a revocation check.

That last clause is why `require_user_revocation_checked` appears here and almost nowhere
else: a ticket authorizes a socket that may live for the full 3600-second request
timeout, so this one round-trip to identitytoolkit covers a credential with a much longer
reach than an ordinary request's.

A rejected ticket closes with 1008 (policy violation) rather than accepting and then
sending an error frame. Accepting first would mean an unauthenticated peer holds an open
socket, however briefly.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, WebSocket, status

from coach.api.auth import require_user_revocation_checked
from coach.api.deps import Container, get_container
from coach.api.schemas import WsTicketResponse
from coach.core.ids import ticket_id as new_ticket_id
from coach.core.principal import Principal
from coach.ws.manager import SocketSession

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])

#: RFC 6455 policy violation. What an unusable ticket gets.
WS_POLICY_VIOLATION = 1008


@router.post(
    "/api/ws-ticket",
    response_model=WsTicketResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_ws_ticket(
    principal: Principal = Depends(require_user_revocation_checked),
    container: Container = Depends(get_container),
) -> WsTicketResponse:
    """Mint a single-use ticket. Revocation-checked; see the module docstring."""
    ticket = await container.tickets.issue(new_ticket_id(), principal.uid)
    return WsTicketResponse(ticket=ticket.ticket, expires_at=ticket.expires_at)


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    ticket: str = Query(default=""),
) -> None:
    """One connection per browser tab, multiplexed across sessions."""
    container: Container = websocket.app.state.container

    uid = await container.tickets.redeem(ticket) if ticket else None
    if uid is None:
        # Not accepted, so nothing is open to close gracefully; `close` before `accept`
        # is how Starlette declines a handshake.
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    await websocket.accept()
    principal = Principal(uid=uid, source="ws_ticket")
    session = SocketSession(
        websocket,
        principal,
        turns=container.turns,
        broker=container.broker,
        presence=container.presence_repository,
        board_updates=container.board_updates,
        runs=container.runs,
    )
    logger.info("websocket connected", extra={"uid": uid})
    try:
        await session.run()
    finally:
        logger.info("websocket disconnected", extra={"uid": uid})


__all__ = ["WS_POLICY_VIOLATION", "router"]
