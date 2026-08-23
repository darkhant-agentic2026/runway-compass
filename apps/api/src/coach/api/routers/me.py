"""Identity and global preferences (`/api/me`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from coach.api.deps import CurrentUser, Quotas, Users
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import GlobalPrefsPatch, LearnerProfilePatch, MeResponse
from coach.services.models import User

router = APIRouter(prefix="/api", tags=["me"])


async def _me_response(user: User, quotas: Quotas) -> MeResponse:
    return MeResponse(
        uid=user.uid,
        email=user.email,
        display_name=user.display_name,
        photo_url=user.photo_url,
        global_prefs=user.global_prefs,
        learner_profile=user.learner_profile,
        plan=user.plan,
        usage=await quotas.status(user),
    )


@router.get("/me", response_model=MeResponse)
async def get_me(principal: CurrentUser, users: Users, quotas: Quotas) -> MeResponse:
    """The verified principal's profile, preferences, plan limits, and usage.

    This is M0's proof of life: a signed-in user seeing their own email come back from
    the server means token verification, the user document, and the SPA's auth wiring
    all work.
    """
    user = await users.get_or_create(principal)
    return await _me_response(user, quotas)


@router.patch("/me/prefs", response_model=MeResponse, dependencies=[Depends(idempotency_guard)])
async def patch_prefs(
    patch: GlobalPrefsPatch, principal: CurrentUser, users: Users, quotas: Quotas
) -> MeResponse:
    user = await users.patch_global_prefs(
        principal, patch.model_dump(by_alias=True, exclude_none=True)
    )
    return await _me_response(user, quotas)


@router.patch("/me/learner-profile", dependencies=[Depends(idempotency_guard)])
async def patch_learner_profile(
    patch: LearnerProfilePatch, principal: CurrentUser, users: Users
) -> dict[str, object]:
    """User edits to "what your coach knows about you".

    The Settings screen this backs is an M7 deliverable; the endpoint lands here because
    the profile document and its versioning are part of the M1 user model.
    """
    profile = await users.patch_learner_profile(
        principal, patch.model_dump(by_alias=True, exclude_none=True)
    )
    return {"learnerProfile": profile.model_dump(by_alias=True)}
