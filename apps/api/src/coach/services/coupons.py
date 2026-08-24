"""Claiming a beta coupon.

docs/02-data-model.md#couponscode: a coupon replaces the claiming account's points limits
outright — "the new … quotas it grants" — and leaves `autonomousRunsPerDay` untouched,
since a coupon is about spend, not pacing.
"""

from __future__ import annotations

from datetime import timedelta

from coach.core.errors import RateLimited
from coach.core.principal import Principal
from coach.repositories.coupons import CouponRepository
from coach.repositories.rate_limits import RateLimitRepository
from coach.services.models import Plan
from coach.services.users import UserService

#: docs/04-api-contract.md#abuse-prevention-limits-implemented-m8-quotas. Recording every
#: attempt, including a wrong code, since brute-forcing codes is exactly what this exists
#: to slow down — see `RateLimitRepository.check_and_record`.
CLAIM_LIMIT = 5
CLAIM_WINDOW = timedelta(hours=1)


class CouponService:
    def __init__(
        self,
        coupons: CouponRepository,
        users: UserService,
        rate_limits: RateLimitRepository,
    ) -> None:
        self._coupons = coupons
        self._users = users
        self._rate_limits = rate_limits

    async def claim(self, principal: Principal, code: str) -> Plan:
        """`POST /api/coupons/claim`. Raises `RateLimited`, `NotFound`, or `Conflict`."""
        allowed = await self._rate_limits.check_and_record(
            f"coupon_claim:{principal.uid}", limit=CLAIM_LIMIT, window=CLAIM_WINDOW
        )
        if not allowed:
            raise RateLimited(
                "Too many coupon attempts from this account. Try again in a while."
            )

        coupon = await self._coupons.claim(code.strip(), principal.uid)
        return await self._users.apply_coupon_limits(principal, coupon.limits)


__all__ = ["CLAIM_LIMIT", "CLAIM_WINDOW", "CouponService"]
