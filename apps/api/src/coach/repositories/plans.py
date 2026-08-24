"""`plans/{tier}` — the preset a new account's `plan.limits` is copied from.

docs/02-data-model.md#plansttier. Read exactly once per account, at creation
(`UserService.get_or_create`); an existing user's limits live on their own `users/{uid}`
document and never move because this preset changed.
"""

from __future__ import annotations

from typing import Any

from coach.repositories.firestore import PLANS, Database
from coach.services.models import PlanLimits

#: The tier every new account starts on. Not user-selectable — there is no tier picker in
#: v1, only what `plans/free` (or, absent that, `PlanLimits`'s own defaults) grants.
FREE_TIER = "free"


class PlanRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, tier: str) -> Any:
        return self._db.client.collection(PLANS).document(tier)

    async def get_preset(self, tier: str = FREE_TIER) -> PlanLimits:
        """The tier's preset limits, or `PlanLimits`'s Python defaults if unseeded.

        The fallback is what lets a fresh, unseeded emulator behave the same as a seeded
        one: `PlanLimits`'s defaults are kept numerically equal to `plans/free`'s document
        for exactly this reason. It is not an error for the document to be absent — a
        deployment that never seeded it is still a legitimate one, same reasoning
        `Settings._unset_placeholder_secrets` gives for an unseeded YouTube key.
        """
        snapshot = await self._doc(tier).get()
        if not snapshot.exists:
            return PlanLimits()
        document = snapshot.to_dict() or {}
        return PlanLimits.model_validate(document.get("limits") or {})

    async def set_preset(self, tier: str, limits: PlanLimits) -> None:
        """Write (or overwrite) a tier's preset. Used by `scripts/seed.py` and, in
        production, a human editing Firestore directly during beta — there is no endpoint
        for this, on the same footing as a coupon document."""
        await self._doc(tier).set({"limits": limits.to_document()})


__all__ = ["FREE_TIER", "PlanRepository"]
