"""`users/{uid}` access."""

from __future__ import annotations

from typing import Any

from google.cloud.firestore import AsyncTransaction

from coach.core.clock import now
from coach.repositories.firestore import USERS, Database
from coach.services.models import User


class UserRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, uid: str) -> Any:
        return self._db.client.collection(USERS).document(uid)

    async def get(self, uid: str, transaction: AsyncTransaction | None = None) -> User | None:
        snapshot = await self._doc(uid).get(transaction=transaction)
        if not snapshot.exists:
            return None
        return User.model_validate({**(snapshot.to_dict() or {}), "uid": uid})

    async def create(self, user: User) -> User:
        """Write a fresh user document.

        Called on first sight of a principal. `createdAt` is set here rather than by the
        caller so that "when did this account appear" has exactly one writer.
        """
        timestamp = now()
        user = user.model_copy(update={"created_at": timestamp, "last_seen_at": timestamp})
        document = user.to_document()
        document.pop("uid", None)  # the uid is the document key, not a field
        await self._doc(user.uid).set(document)
        return user

    async def touch_last_seen(self, uid: str) -> None:
        await self._doc(uid).update({"lastSeenAt": now()})

    async def patch(self, uid: str, patch: dict[str, Any]) -> None:
        """Apply a patch of dotted field paths, e.g. `{"globalPrefs.verbosity": "terse"}`.

        Dotted paths are what keep a partial preference update from clobbering sibling
        keys, which a whole-map `set` would do.
        """
        if not patch:
            return
        await self._doc(uid).update(patch)
