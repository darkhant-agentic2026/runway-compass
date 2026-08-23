"""Beta-testing coupons (`/api/coupons`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from coach.api.deps import Coupons, CurrentUser
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import CouponClaimRequest, CouponClaimResponse

router = APIRouter(prefix="/api", tags=["coupons"])


@router.post(
    "/coupons/claim",
    response_model=CouponClaimResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def claim_coupon(
    body: CouponClaimRequest, principal: CurrentUser, coupons: Coupons
) -> CouponClaimResponse:
    """Claim a beta coupon, replacing this account's points limits.

    docs/02-data-model.md#couponscode. `404` for an unknown code, `409` for one already
    claimed, `429` if this account has attempted too many claims recently
    (docs/04-api-contract.md#abuse-prevention-limits-implemented-m8-quotas).
    """
    plan = await coupons.claim(principal, body.code)
    return CouponClaimResponse(plan=plan)
