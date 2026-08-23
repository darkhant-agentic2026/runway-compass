"""M8-quotas: usage windows, plan presets, coupons, and the two abuse-prevention rate
limits. docs/02-data-model.md#usage-quotas-m8-quotas is the specification.

The scheduler's own points-guard test lives beside its run-count sibling in
`test_scheduler.py` rather than here — same fixtures, same guard table.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from coach.core.clock import now
from coach.core.principal import Principal
from coach.repositories.usage import (
    local_four_hour_block,
    next_daily_reset,
    next_four_hour_reset,
    next_monthly_reset,
)
from coach.services.models import CouponLimits, PlanLimits, TurnStatus
from streaming_doubles import ScriptedModel


async def _me(app, uid: str) -> httpx.Response:
    """A fresh, unauthenticated-until-now client for `uid` — deliberately not the
    `client`/`alice` fixture, which would itself spend one of the four new-account slots
    the registration-rate-limit tests are counting."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer dev:{uid}"},
    ) as http_client:
        return await http_client.get("/api/me")


# --- usage bucketing (repositories/usage.py) --------------------------------------------


async def test_spend_points_charges_all_three_windows_by_the_same_ceil_division(
    container,
) -> None:
    at = now()
    charged = await container.usage_repository.spend_points(
        "u_probe", 2001, timezone="UTC", at=at
    )
    assert charged == 3  # ceil(2001 / 1000)

    snapshot = await container.usage_repository.points_snapshot("u_probe", "UTC", at)
    assert (snapshot.monthly, snapshot.daily, snapshot.four_hour) == (3, 3, 3)


async def test_spending_zero_or_negative_tokens_writes_nothing(container) -> None:
    at = now()
    charged = await container.usage_repository.spend_points("u_probe", 0, timezone="UTC", at=at)
    assert charged == 0
    snapshot = await container.usage_repository.points_snapshot("u_probe", "UTC", at)
    assert (snapshot.monthly, snapshot.daily, snapshot.four_hour) == (0, 0, 0)


def test_four_hour_blocks_are_six_fixed_slices_of_the_local_day() -> None:
    just_before = datetime(2026, 8, 24, 15, 59, tzinfo=UTC)
    just_after = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)
    assert local_four_hour_block(just_before, "UTC")[1] == 3
    assert local_four_hour_block(just_after, "UTC")[1] == 4


def test_monthly_reset_rolls_the_year_over_in_december() -> None:
    at = datetime(2026, 12, 15, 10, 0, tzinfo=UTC)
    assert next_monthly_reset(at, "UTC") == datetime(2027, 1, 1, tzinfo=UTC)


def test_daily_reset_is_local_midnight_not_utc_midnight() -> None:
    # 23:00 in UTC+2 is already the next day locally.
    at = datetime(2026, 8, 24, 23, 0, tzinfo=UTC)
    reset = next_daily_reset(at, "Europe/Berlin")
    assert reset == datetime(2026, 8, 25, 22, 0, tzinfo=UTC)


def test_four_hour_reset_crosses_midnight_from_the_last_block() -> None:
    at = datetime(2026, 8, 24, 21, 0, tzinfo=UTC)  # block 5, 20:00-24:00
    reset = next_four_hour_reset(at, "UTC")
    assert reset == datetime(2026, 8, 25, 0, 0, tzinfo=UTC)


# --- the sliding-window rate limiter (repositories/rate_limits.py) ----------------------


async def test_rate_limit_allows_up_to_the_limit_then_refuses(container) -> None:
    key = "test:allow-then-refuse"
    window = timedelta(minutes=30)
    for _ in range(3):
        assert await container.rate_limit_repository.check_and_record(
            key, limit=3, window=window
        )
    assert not await container.rate_limit_repository.check_and_record(
        key, limit=3, window=window
    )


async def test_a_refused_attempt_is_not_recorded(container) -> None:
    """A rejection must not advance the window — otherwise a caller that keeps retrying
    shortens its own wait, which is the opposite of what a rate limit is for."""
    key = "test:refusal-does-not-record"
    window = timedelta(minutes=30)
    assert await container.rate_limit_repository.check_and_record(key, limit=1, window=window)
    assert not await container.rate_limit_repository.check_and_record(
        key, limit=1, window=window
    )

    from coach.repositories.firestore import RATE_LIMITS

    snapshot = await container.db.client.collection(RATE_LIMITS).document(key).get()
    assert len((snapshot.to_dict() or {}).get("timestamps", [])) == 1


# --- a new account's plan (services/users.py, repositories/plans.py) -------------------


async def test_a_new_account_starts_on_plans_free(app) -> None:
    response = await _me(app, "u_fresh")
    assert response.status_code == 200
    assert response.json()["plan"]["limits"] == {
        "autonomousRunsPerDay": 20,
        "monthlyPoints": 500,
        "dailyPoints": 200,
        "fourHourPoints": 80,
    }


async def test_a_seeded_preset_is_copied_onto_the_account_not_referenced(
    app, container
) -> None:
    await container.plan_repository.set_preset(
        "free",
        PlanLimits(
            autonomous_runs_per_day=7,
            monthly_points=999,
            daily_points=111,
            four_hour_points=22,
        ),
    )

    first = await _me(app, "u_fresh_seeded")
    assert first.json()["plan"]["limits"]["monthlyPoints"] == 999

    # Changing the preset afterward must not move an account that already copied it.
    await container.plan_repository.set_preset("free", PlanLimits(monthly_points=1))
    second = await _me(app, "u_fresh_seeded")
    assert second.json()["plan"]["limits"]["monthlyPoints"] == 999


async def test_get_me_reports_usage_for_all_three_windows(app) -> None:
    response = await _me(app, "u_usage_shape")
    usage = response.json()["usage"]
    assert usage["monthly"]["spent"] == 0
    assert usage["monthly"]["limit"] == 500
    assert usage["daily"]["limit"] == 200
    assert usage["fourHour"]["limit"] == 80
    for window in ("monthly", "daily", "fourHour"):
        assert usage[window]["spent"] == 0
        assert usage[window]["resetsAt"]  # present and non-empty


# --- new-account rate limiting (services/users.py) --------------------------------------


async def test_more_than_four_new_accounts_in_the_window_are_refused(app) -> None:
    for i in range(4):
        response = await _me(app, f"u_new_{i}")
        assert response.status_code == 200, response.text

    fifth = await _me(app, "u_new_4")
    assert fifth.status_code == 429
    assert fifth.json()["type"] == "/problems/rate-limited"


async def test_an_existing_account_is_never_rate_limited_by_the_new_account_guard(
    app,
) -> None:
    for i in range(4):
        assert (await _me(app, f"u_seat_{i}")).status_code == 200
    # The four seats for this window are taken, but an *existing* account (not a creation)
    # must still work — it never touches `check_and_record` at all.
    again = await _me(app, "u_seat_0")
    assert again.status_code == 200


# --- coupons (services/coupons.py, repositories/coupons.py) -----------------------------


async def test_claiming_a_coupon_replaces_points_limits_and_leaves_run_count_alone(
    client: httpx.AsyncClient, container
) -> None:
    await container.coupon_repository.create(
        "BETA-GOOD", CouponLimits(monthly_points=5000, daily_points=2000, four_hour_points=800)
    )

    response = await client.post("/api/coupons/claim", json={"code": "BETA-GOOD"})
    assert response.status_code == 200, response.text
    limits = response.json()["plan"]["limits"]
    assert limits["monthlyPoints"] == 5000
    assert limits["dailyPoints"] == 2000
    assert limits["fourHourPoints"] == 800
    assert limits["autonomousRunsPerDay"] == 20  # a coupon is about spend, not pacing

    me = await client.get("/api/me")
    assert me.json()["plan"]["limits"]["monthlyPoints"] == 5000


async def test_claiming_the_same_coupon_twice_is_a_conflict(
    client: httpx.AsyncClient, container
) -> None:
    await container.coupon_repository.create(
        "BETA-ONCE", CouponLimits(monthly_points=1, daily_points=1, four_hour_points=1)
    )
    first = await client.post("/api/coupons/claim", json={"code": "BETA-ONCE"})
    assert first.status_code == 200
    second = await client.post("/api/coupons/claim", json={"code": "BETA-ONCE"})
    assert second.status_code == 409


async def test_claiming_an_unknown_code_is_not_found(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/coupons/claim", json={"code": "NOPE"})
    assert response.status_code == 404


async def test_coupon_claim_attempts_are_rate_limited_including_wrong_guesses(
    client: httpx.AsyncClient,
) -> None:
    """Brute-forcing codes is exactly what this limit exists to slow down, so a wrong
    guess must count against it too — not only a successful claim."""
    for _ in range(5):
        response = await client.post("/api/coupons/claim", json={"code": "GUESS"})
        assert response.status_code == 404

    sixth = await client.post("/api/coupons/claim", json={"code": "GUESS"})
    assert sixth.status_code == 429
    assert sixth.json()["type"] == "/problems/rate-limited"


# --- the pre-flight gate and post-turn spend (services/turns.py) -----------------------


@pytest.fixture
async def drain_turns(container) -> AsyncIterator[None]:
    """Same reasoning as `test_streaming.py`'s fixture of the same name: without this a
    detached generation task can still be writing when the next test wipes the database."""
    yield
    await container.registry.drain(timeout=5.0)


async def _await_terminal(container, principal: Principal, turn_id: str, timeout: float = 5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        turn = await container.turns.get(principal, turn_id)
        if turn.status in (TurnStatus.COMPLETE, TurnStatus.FAILED, TurnStatus.CANCELLED):
            return turn
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"turn {turn_id} did not finish in time")
        await asyncio.sleep(0.05)


async def test_a_completed_turn_spends_points_and_an_exhausted_window_blocks_the_next(
    client: httpx.AsyncClient,
    container,
    session_id: str,
    scripted_model: ScriptedModel,
    alice: Principal,
    drain_turns: None,
) -> None:
    scripted_model.usage_tokens = 2500  # ceil(2500 / 1000) = 3 points
    await container.user_repository.patch("u_alice", {"plan.limits.dailyPoints": 2})

    first = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})
    assert first.status_code == 202, first.text
    await _await_terminal(container, alice, str(first.json()["turnId"]))

    snapshot = await container.usage_repository.points_snapshot("u_alice", "UTC", now())
    assert snapshot.daily == 3  # over the patched daily limit of 2

    second = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "again"})
    assert second.status_code == 429
    problem = second.json()
    assert problem["type"] == "/problems/quota-exceeded"
    assert problem["window"] == "daily"
    assert problem["resetAt"]


async def test_a_blocked_turn_creates_no_turn_document_and_charges_nothing(
    client: httpx.AsyncClient, container, session_id: str
) -> None:
    await container.user_repository.patch("u_alice", {"plan.limits.dailyPoints": 0})

    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})

    assert response.status_code == 429
    snapshot = await container.usage_repository.points_snapshot("u_alice", "UTC", now())
    assert snapshot.daily == 0  # nothing was ever spent — there was no turn to spend from
