"""`coupons/{code}` — single-use beta-testing grants.

docs/02-data-model.md#couponscode. The document id **is** the code a learner types in, on
the same footing as `ws_tickets/{ticket}` using the ticket itself as the key: claiming is
one point read plus one transactional check-and-set, never a query.

There is no endpoint that creates a coupon — these are written by hand (or a small operator
script) during beta, on the same footing as seeding a Secret Manager value by hand.
"""

from __future__ import annotations

from typing import Any

from google.cloud import firestore

from coach.core.clock import now
from coach.core.errors import Conflict, NotFound
from coach.repositories.firestore import COUPONS, Database
from coach.services.models import Coupon, CouponLimits


class CouponRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, code: str) -> Any:
        return self._db.client.collection(COUPONS).document(code)

    async def create(self, code: str, limits: CouponLimits) -> None:
        """Write an unclaimed coupon. No endpoint calls this — see the module docstring;
        it exists for `scripts/` and for tests to set one up without touching Firestore
        collection paths directly."""
        await self._doc(code).set(
            {
                "claimed": False,
                "claimedByUid": None,
                "claimedAt": None,
                "limits": limits.to_document(),
                "createdAt": now(),
            }
        )

    async def claim(self, code: str, uid: str) -> Coupon:
        """Claim `code` for `uid`, or raise.

        Read-check-write in one transaction — `repositories/tickets.py`'s "redeemed and
        deleted is one operation" is the same shape, minus the delete: a claimed coupon is
        kept, not consumed, since it is also the record of who redeemed it and when. Two
        requests racing the same code cannot both win.
        """
        reference = self._doc(code)

        @firestore.async_transactional
        async def _claim(transaction: firestore.AsyncTransaction) -> Coupon:
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                raise NotFound(f"No coupon {code!r}.")
            document = snapshot.to_dict() or {}
            if document.get("claimed"):
                raise Conflict("This coupon has already been claimed.")
            timestamp = now()
            transaction.update(
                reference, {"claimed": True, "claimedByUid": uid, "claimedAt": timestamp}
            )
            document.update({"claimed": True, "claimedByUid": uid, "claimedAt": timestamp})
            return Coupon.model_validate({**document, "code": code})

        return await self._db.run(_claim)


__all__ = ["CouponRepository"]
