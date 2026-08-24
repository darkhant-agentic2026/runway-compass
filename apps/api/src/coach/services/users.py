"""User profile and global preferences."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from coach.core.clock import now
from coach.core.errors import RateLimited, ValidationProblem
from coach.core.principal import Principal
from coach.repositories.plans import PlanRepository
from coach.repositories.rate_limits import RateLimitRepository
from coach.repositories.users import UserRepository
from coach.services.models import (
    CouponLimits,
    GlobalPrefs,
    LearnerProfile,
    Plan,
    TechnologyBelief,
    User,
)
from coach.services.models import snake_case as _snake

#: docs/04-api-contract.md#abuse-prevention-limits-implemented-m8-quotas. Global, not
#: per-user: a not-yet-created account has no uid to key the counter on. Defaults match
#: `Settings`' own — restated here because a caller building this service directly (a
#: test, `scripts/seed.py`) should not have to construct `Settings` just to get them.
NEW_USER_LIMIT = 4
NEW_USER_WINDOW = timedelta(minutes=30)
NEW_USERS_RATE_LIMIT_KEY = "new_users"


class UserService:
    def __init__(
        self,
        users: UserRepository,
        plans: PlanRepository,
        rate_limits: RateLimitRepository,
        *,
        new_user_limit: int = NEW_USER_LIMIT,
        new_user_window: timedelta = NEW_USER_WINDOW,
    ) -> None:
        self._users = users
        self._plans = plans
        self._rate_limits = rate_limits
        # Configurable rather than the module constant, so the e2e harness — which mints a
        # fresh, never-reused uid per *test* as its whole isolation strategy
        # (`e2e/fixtures.ts`) — can raise this without the production default moving.
        self._new_user_limit = new_user_limit
        self._new_user_window = new_user_window

    async def get_or_create(self, principal: Principal) -> User:
        """The caller's user document, created on first sight.

        There is no separate registration step: a verified Identity Platform token is
        the account. Profile fields come from the token's claims and are refreshed when
        they change, so a new Google display name or avatar follows along.

        Creation is rate-limited globally
        (docs/04-api-contract.md#abuse-prevention-limits-implemented-m8-quotas) and starts
        the account on `plans/free`'s preset limits — copied onto the document rather than
        referenced, so a later change to the preset never moves an existing account's
        limits (docs/02-data-model.md#plansttier).
        """
        existing = await self._users.get(principal.uid)
        if existing is None:
            allowed = await self._rate_limits.check_and_record(
                NEW_USERS_RATE_LIMIT_KEY,
                limit=self._new_user_limit,
                window=self._new_user_window,
            )
            if not allowed:
                raise RateLimited(
                    "Too many new accounts were created recently. Try again in a while."
                )
            limits = await self._plans.get_preset()
            return await self._users.create(
                User(
                    uid=principal.uid,
                    email=principal.email,
                    display_name=principal.display_name,
                    photo_url=principal.photo_url,
                    plan=Plan(limits=limits),
                )
            )

        token_fields: list[tuple[str, str | None]] = [
            ("email", principal.email),
            ("displayName", principal.display_name),
            ("photoUrl", principal.photo_url),
        ]
        if existing.display_name_customized:
            # The learner has chosen their own — otherwise the very next request would
            # re-sync it from the sign-in token and silently discard the choice.
            token_fields = [
                (name, value) for name, value in token_fields if name != "displayName"
            ]

        refresh: dict[str, Any] = {"lastSeenAt": now()}
        for wire_name, value in token_fields:
            if value is not None and getattr(existing, _snake(wire_name)) != value:
                refresh[wire_name] = value
        await self._users.patch(principal.uid, refresh)
        return existing.model_copy(update={_snake(k): v for k, v in refresh.items()})

    async def global_prefs(self, principal: Principal) -> GlobalPrefs:
        user = await self.get_or_create(principal)
        return user.global_prefs

    async def patch_display_name(self, principal: Principal, display_name: str) -> User:
        """User-chosen display name (`PATCH /api/me`).

        Once set, `get_or_create`'s refresh-from-token loop above stops touching this
        field for this account — the whole reason `display_name_customized` exists.
        """
        trimmed = display_name.strip()
        if not trimmed:
            raise ValidationProblem("Display name cannot be empty.")
        user = await self.get_or_create(principal)
        await self._users.patch(
            principal.uid, {"displayName": trimmed, "displayNameCustomized": True}
        )
        return user.model_copy(
            update={"display_name": trimmed, "display_name_customized": True}
        )

    async def apply_coupon_limits(self, principal: Principal, limits: CouponLimits) -> Plan:
        """A claimed coupon **replaces** the account's points limits outright.

        `autonomousRunsPerDay` is untouched — a coupon is about spend, not the pacing cap
        on background work (docs/02-data-model.md#couponscode). Called only after
        `CouponRepository.claim` has already committed the coupon as claimed, so this is
        never asked to apply the same grant twice for one coupon.
        """
        user = await self.get_or_create(principal)
        await self._users.patch(
            principal.uid,
            {
                "plan.limits.monthlyPoints": limits.monthly_points,
                "plan.limits.fourHourPoints": limits.four_hour_points,
            },
        )
        merged_limits = user.plan.limits.model_copy(
            update={
                "monthly_points": limits.monthly_points,
                "four_hour_points": limits.four_hour_points,
            }
        )
        return user.plan.model_copy(update={"limits": merged_limits})

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
