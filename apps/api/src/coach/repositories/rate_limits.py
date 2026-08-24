"""`rate_limits/{key}` — a generic sliding-window abuse counter.

docs/02-data-model.md#rate_limitskey. Two call sites today, both M8-quotas: new account
creation (`key = "new_users"`, global — a not-yet-created account has no uid to key on) and
coupon-claim attempts (`key = "coupon_claim:{uid}"`, per account, recording a wrong guess
too — brute-forcing codes is exactly what that one exists to slow down).

One document per key, holding the timestamps still inside the window, on the same footing
as `board_events/{uid}`'s frame list: this is read and written by low-volume,
latency-insensitive paths, so a transactional list is simpler than a token-bucket service
built for two call sites.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from google.cloud import firestore

from coach.core.clock import now
from coach.repositories.firestore import RATE_LIMITS, Database


class RateLimitRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, key: str) -> Any:
        return self._db.client.collection(RATE_LIMITS).document(key)

    async def check_and_record(self, key: str, *, limit: int, window: timedelta) -> bool:
        """`True` and recorded if `key` is under `limit` events in the trailing `window`;
        `False` and **not** recorded otherwise.

        Not recording a refused attempt is deliberate: appending it anyway would advance
        the window's own oldest timestamp, letting a caller who keeps retrying shorten
        their own wait. The window only ever moves forward at the rate real, admitted
        attempts create it.
        """
        reference = self._doc(key)

        @firestore.async_transactional
        async def _check(transaction: firestore.AsyncTransaction) -> bool:
            snapshot = await reference.get(transaction=transaction)
            document = snapshot.to_dict() or {} if snapshot.exists else {}
            cutoff = now() - window
            recent = [t for t in document.get("timestamps", []) if t > cutoff]
            if len(recent) >= limit:
                return False
            recent.append(now())
            transaction.set(reference, {"timestamps": recent})
            return True

        return await self._db.run(_check)


__all__ = ["RateLimitRepository"]
