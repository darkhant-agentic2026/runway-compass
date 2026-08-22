"""User profile and global preferences."""

from __future__ import annotations

from typing import Any

from coach.core.clock import now
from coach.core.principal import Principal
from coach.repositories.users import UserRepository
from coach.services.models import GlobalPrefs, LearnerProfile, TechnologyBelief, User
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
        go through `agent_update_learner_profile` via the `update_learner_profile` tool.
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

        merged = {
            **user.learner_profile.to_document(),
            **patch,
            "version": version,
            "updatedAt": timestamp,
            "updatedBy": "user",
        }
        return LearnerProfile.model_validate(merged)

    async def agent_update_learner_profile(
        self,
        principal: Principal,
        *,
        thinking_style: str | None = None,
        strengths: list[str] | None = None,
        gaps: list[str] | None = None,
        technologies: list[dict[str, Any] | TechnologyBelief] | None = None,
        pacing: str | None = None,
        feedback_note: str | None = None,
        session_id: str | None = None,
    ) -> LearnerProfile:
        """Agent updates to the learner profile from the `update_learner_profile` tool.

        Bumps `version`, records `updatedBy: 'agent'`, stamps `updatedAt`, and appends
        `feedback_note` with session attribution to the 20-item ring buffer.
        """
        user = await self.get_or_create(principal)
        timestamp = now()
        version = user.learner_profile.version + 1
        current = user.learner_profile

        updates: dict[str, Any] = {
            "learnerProfile.version": version,
            "learnerProfile.updatedAt": timestamp,
            "learnerProfile.updatedBy": "agent",
        }

        new_thinking_style = (
            thinking_style[:500] if thinking_style is not None else current.thinking_style
        )
        if thinking_style is not None:
            updates["learnerProfile.thinkingStyle"] = new_thinking_style

        new_strengths = strengths if strengths is not None else current.strengths
        if strengths is not None:
            updates["learnerProfile.strengths"] = new_strengths

        new_gaps = gaps if gaps is not None else current.gaps
        if gaps is not None:
            updates["learnerProfile.gaps"] = new_gaps

        new_pacing = pacing if pacing is not None else current.pacing
        if pacing is not None:
            updates["learnerProfile.pacing"] = new_pacing

        if technologies is not None:
            tech_models = [
                t if isinstance(t, TechnologyBelief) else TechnologyBelief.model_validate(t)
                for t in technologies
            ]
            updates["learnerProfile.technologies"] = [t.to_document() for t in tech_models]
            new_technologies = tech_models
        else:
            new_technologies = current.technologies

        new_feedback_notes = list(current.feedback_notes)
        if feedback_note:
            note_text = f"[{session_id}] {feedback_note}" if session_id else feedback_note
            new_feedback_notes.append(note_text)
            if len(new_feedback_notes) > 20:
                new_feedback_notes = new_feedback_notes[-20:]
            updates["learnerProfile.feedbackNotes"] = new_feedback_notes

        await self._users.patch(principal.uid, updates)

        return LearnerProfile(
            thinking_style=new_thinking_style,
            strengths=new_strengths,
            gaps=new_gaps,
            technologies=new_technologies,
            pacing=new_pacing,
            feedback_notes=new_feedback_notes,
            updated_at=timestamp,
            updated_by="agent",
            version=version,
        )
