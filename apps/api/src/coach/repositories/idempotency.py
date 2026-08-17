"""`idempotency/{uid}__{fingerprint}` — replay records for `Idempotency-Key`.

docs/04-api-contract.md: "All mutating endpoints accept `Idempotency-Key`." The
mechanism is not specified there, so this is the smallest thing that satisfies it across
instances: the first request stores its response, a replay returns the stored one.

The collection is not in docs/02-data-model.md's original map; it is added there with
this module. Scoped by uid so one user's key cannot collide with another's, and
fingerprinted by method and path so the same key reused on a different endpoint is
treated as a different operation rather than replaying an unrelated response.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from coach.core.clock import now
from coach.repositories.firestore import IDEMPOTENCY, Database

#: How long a key is honoured. Long enough to cover a client's retry window, short
#: enough that the collection does not grow without bound; the TTL policy on `expiresAt`
#: does the deleting.
RETENTION = timedelta(hours=24)


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


class IdempotencyRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _document_id(uid: str, method: str, path: str, key: str) -> str:
        fingerprint = hashlib.sha256(f"{method}\n{path}\n{key}".encode()).hexdigest()[:32]
        return f"{uid}__{fingerprint}"

    def _doc(self, uid: str, method: str, path: str, key: str) -> Any:
        return self._db.client.collection(IDEMPOTENCY).document(
            self._document_id(uid, method, path, key)
        )

    async def get(self, uid: str, method: str, path: str, key: str) -> StoredResponse | None:
        snapshot = await self._doc(uid, method, path, key).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        expires_at = data.get("expiresAt")
        if expires_at is not None and expires_at < now():
            # The TTL policy deletes lazily; do not honour an expired record in the
            # window before it is collected.
            return None
        return StoredResponse(status_code=int(data["statusCode"]), body=data.get("body") or {})

    async def put(
        self, uid: str, method: str, path: str, key: str, response: StoredResponse
    ) -> None:
        await self._doc(uid, method, path, key).set(
            {
                "uid": uid,
                "method": method,
                "path": path,
                "statusCode": response.status_code,
                "body": response.body,
                "createdAt": now(),
                "expiresAt": now() + RETENTION,
            }
        )
