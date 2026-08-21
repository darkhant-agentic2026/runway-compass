"""User profile and global preferences."""

from __future__ import annotations

from typing import Any

from coach.core.clock import now
from coach.core.principal import Principal
from coach.repositories.users import UserRepository
from coach.services.models import GlobalPrefs, LearnerProfile, User
from coach.services.models import snake_case as _snake


class UserService:
    def __init__(self, users: UserRepository) -> None:
        self._users = users

    async def get_or_create(self, principal: Principal) -> User:
        """The caller's user document, created on first sight.

        There is no separate registration step: a verified Identity Platform token is
        the account. Profile fields come from the token's claims and are refreshed when
        they change, so a new Google display name or avatar follows along.
        """
        existing = await self._users.get(principal.uid)
        if existing is None:
            return await self._users.create(
                User(
                    uid=principal.uid,
                    email=principal.email,
                    display_name=principal.display_name,
                    photo_url=principal.photo_url,
                )
            )

        refresh: dict[str, Any] = {"lastSeenAt": now()}
        for wire_name, value in (
            ("email", principal.email),
            ("displayName", principal.display_name),
            ("photoUrl", principal.photo_url),
        ):
            if value is not None and getattr(existing, _snake(wire_name)) != value:
                refresh[wire_name] = value
        await self._users.patch(principal.uid, refresh)
        return existing.model_copy(update={_snake(k): v for k, v in refresh.items()})

    async def global_prefs(self, principal: Principal) -> GlobalPrefs:
        user = await self.get_or_create(principal)
        return user.global_prefs

    async def patch_global_prefs(self, principal: Principal, patch: dict[str, Any]) -> User:
        """Partial update of `globalPrefs` (`PATCH /api/me/prefs`).

        Written as dotted field paths so that sending only `defaultTaskMinutes` cannot
        reset `timezone` to its default.
        """
        user = await self.get_or_create(principal)
        if not patch:
            return user
        await self._users.patch(
            principal.uid, {f"globalPrefs.{key}": value for key, value in patch.items()}
        )
        # Re-*validated* rather than `model_copy`-ed. `model_copy(update=…)` assigns without
        # validating, so a nested patch — `autonomousQuietHours`, the only one there is —
        # would leave a plain `dict` where a `QuietHours` belongs. The document in Firestore
        # is correct either way and re-validates on the next read, so the only casualty is
        # *this* response, which pydantic serializes with a warning and no error. A shape
        # that is right in the database and wrong in the reply is worse than either.
        merged = GlobalPrefs.model_validate(
            {**user.global_prefs.to_document(), **patch},
        )
        return user.model_copy(update={"global_prefs": merged})

    async def patch_learner_profile(
        self, principal: Principal, patch: dict[str, Any]
    ) -> LearnerProfile:
        """User edits to the coach's beliefs (`PATCH /api/me/learner-profile`).

        Every write bumps `version` and records who made it, which is what lets the
        Settings screen show why the coach changed its approach. The agent's own writes
        go through the `update_learner_profile` tool at M7, never through this path.
        """
        user = await self.get_or_create(principal)
        if not patch:
            return user.learner_profile

        timestamp = now()
        version = user.learner_profile.version + 1
        updates: dict[str, Any] = {
            f"learnerProfile.{key}": value for key, value in patch.items()
        }
        updates["learnerProfile.version"] = version
        updates["learnerProfile.updatedAt"] = timestamp
        updates["learnerProfile.updatedBy"] = "user"
        await self._users.patch(principal.uid, updates)
        return user.learner_profile.model_copy(
            update={
                **{_snake(k): v for k, v in patch.items()},
                "version": version,
                "updated_by": "user",
                "updated_at": timestamp,
            }
        )
