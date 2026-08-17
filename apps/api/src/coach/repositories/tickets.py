"""`ws_tickets/{ticket}` — single-use, 60-second WebSocket credentials.

docs/04-api-contract.md#authentication:

> Browsers cannot set headers on `new WebSocket()`, so the client first calls
> `POST /api/ws-ticket` (authenticated by ID token, with the revocation check above) to
> get a **single-use, 60-second ticket**, then connects to `wss://…/ws?ticket=…`. The
> ticket is redeemed and deleted server-side on connect.

**Why Firestore and not a dict.** Session affinity makes it *likely* that the socket
lands on the instance that issued the ticket, but affinity is a preference, not a
guarantee — and it is at its least reliable exactly when it matters, during a
redeploy or a scale event, which is also when a client is most likely to be
reconnecting. An in-process ticket store would fail those reconnects with an
authentication error that looks like a bug in the client.

The collection is not in docs/02-data-model.md's original map and is added there with
this module, on the same footing as `idempotency/*` at M1.

**"Redeemed and deleted" is one operation.** `redeem` deletes first and reads the
deleted snapshot, so two sockets racing on one ticket cannot both be admitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from coach.core.clock import now
from coach.repositories.firestore import Database

WS_TICKETS = "ws_tickets"

#: docs/04-api-contract.md: sixty seconds. Long enough to cover the round trip and a
#: retry, short enough that a ticket captured from a URL is worthless by the time it is
#: read out of a log.
TICKET_TTL = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class Ticket:
    ticket: str
    uid: str
    expires_at: Any


class TicketRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, ticket: str) -> Any:
        return self._db.client.collection(WS_TICKETS).document(ticket)

    async def issue(self, ticket: str, uid: str) -> Ticket:
        expires_at = now() + TICKET_TTL
        await self._doc(ticket).set({"uid": uid, "createdAt": now(), "expiresAt": expires_at})
        return Ticket(ticket=ticket, uid=uid, expires_at=expires_at)

    async def redeem(self, ticket: str) -> str | None:
        """Consume a ticket, returning the uid it authorizes, or `None`.

        The delete carries `return_document`-style semantics by way of reading the
        snapshot the delete produces: `AsyncDocumentReference.delete` returns the commit
        result rather than the old value, so the read-then-delete is done in a
        transaction to keep "exactly one socket per ticket" true across instances.
        """

        from google.cloud import firestore

        reference = self._doc(ticket)

        @firestore.async_transactional
        async def _redeem(transaction: firestore.AsyncTransaction) -> str | None:
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict() or {}
            transaction.delete(reference)
            expires_at = data.get("expiresAt")
            if expires_at is not None and expires_at < now():
                # Expired tickets are still deleted — an expired credential left lying
                # around is exactly what the TTL is for, and the delete above already
                # happened inside this transaction.
                return None
            uid = data.get("uid")
            return str(uid) if uid else None

        return await self._db.run(_redeem)


__all__ = ["TICKET_TTL", "WS_TICKETS", "Ticket", "TicketRepository"]
