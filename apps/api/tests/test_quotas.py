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
from coach.core.errors import QuotaBelowThreshold
from coach.core.principal import Principal
from coach.repositories.usage import (
    local_four_hour_block,
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


async def test_spend_points_charges_both_windows_by_the_same_ceil_division(container) -> None:
    at = now()
    charged = await container.usage_repository.spend_points(
        "u_probe", 2001, timezone="UTC", at=at
    )
    assert charged == 3  # ceil(2001 / 1000)

    snapshot = await container.usage_repository.points_snapshot("u_probe", "UTC", at)
    assert (snapshot.monthly, snapshot.four_hour) == (3, 3)


async def test_spending_zero_or_negative_tokens_writes_nothing(container) -> None:
    at = now()
    charged = await container.usage_repository.spend_points("u_probe", 0, timezone="UTC", at=at)
    assert charged == 0
    snapshot = await container.usage_repository.points_snapshot("u_probe", "UTC", at)
    assert (snapshot.monthly, snapshot.four_hour) == (0, 0)


def test_four_hour_blocks_are_six_fixed_slices_of_the_local_day() -> None:
    just_before = datetime(2026, 8, 24, 15, 59, tzinfo=UTC)
    just_after = datetime(2026, 8, 24, 16, 0, tzinfo=UTC)
    assert local_four_hour_block(just_before, "UTC")[1] == 3
    assert local_four_hour_block(just_after, "UTC")[1] == 4


def test_monthly_reset_rolls_the_year_over_in_december() -> None:
    at = datetime(2026, 12, 15, 10, 0, tzinfo=UTC)
    assert next_monthly_reset(at, "UTC") == datetime(2027, 1, 1, tzinfo=UTC)


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
        "monthlyPoints": 1200,
        "fourHourPoints": 80,
        "runStartPointsThreshold": 800,
    }


async def test_a_seeded_preset_is_copied_onto_the_account_not_referenced(
    app, container
) -> None:
    await container.plan_repository.set_preset(
        "free",
        PlanLimits(autonomous_runs_per_day=7, monthly_points=999, four_hour_points=22),
    )

    first = await _me(app, "u_fresh_seeded")
    assert first.json()["plan"]["limits"]["monthlyPoints"] == 999

    # Changing the preset afterward must not move an account that already copied it.
    await container.plan_repository.set_preset("free", PlanLimits(monthly_points=1))
    second = await _me(app, "u_fresh_seeded")
    assert second.json()["plan"]["limits"]["monthlyPoints"] == 999


async def test_get_me_reports_usage_for_both_windows(app) -> None:
    response = await _me(app, "u_usage_shape")
    usage = response.json()["usage"]
    assert usage["monthly"]["limit"] == 1200
    assert usage["fourHour"]["limit"] == 80
    for window in ("monthly", "fourHour"):
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
        "BETA-GOOD", CouponLimits(monthly_points=5000, four_hour_points=800)
    )

    response = await client.post("/api/coupons/claim", json={"code": "BETA-GOOD"})
    assert response.status_code == 200, response.text
    limits = response.json()["plan"]["limits"]
    assert limits["monthlyPoints"] == 5000
    assert limits["fourHourPoints"] == 800
    assert limits["autonomousRunsPerDay"] == 20  # a coupon is about spend, not pacing

    me = await client.get("/api/me")
    assert me.json()["plan"]["limits"]["monthlyPoints"] == 5000


async def test_claiming_the_same_coupon_twice_is_a_conflict(
    client: httpx.AsyncClient, container
) -> None:
    await container.coupon_repository.create(
        "BETA-ONCE", CouponLimits(monthly_points=1, four_hour_points=1)
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
    await container.user_repository.patch("u_alice", {"plan.limits.fourHourPoints": 2})

    first = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})
    assert first.status_code == 202, first.text
    await _await_terminal(container, alice, str(first.json()["turnId"]))

    snapshot = await container.usage_repository.points_snapshot("u_alice", "UTC", now())
    assert snapshot.four_hour == 3  # over the patched 4-hour limit of 2

    second = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "again"})
    assert second.status_code == 429
    problem = second.json()
    assert problem["type"] == "/problems/quota-exceeded"
    assert problem["window"] == "4-hour"
    assert problem["resetAt"]


async def test_a_blocked_turn_creates_no_turn_document_and_charges_nothing(
    client: httpx.AsyncClient, container, session_id: str
) -> None:
    await container.user_repository.patch("u_alice", {"plan.limits.fourHourPoints": 0})

    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})

    assert response.status_code == 429
    snapshot = await container.usage_repository.points_snapshot("u_alice", "UTC", now())
    assert snapshot.four_hour == 0  # nothing was ever spent — there was no turn to spend from


# --- the run-start points threshold, M10 (services/quotas.py) --------------------------


async def test_require_room_to_start_run_raises_under_the_threshold(
    client: httpx.AsyncClient, container
) -> None:
    await client.get("/api/me")  # materializes u_alice on the free preset
    # 401 points spent leaves 1200 - 401 = 799 monthly remaining, one under the 800
    # default `runStartPointsThreshold`.
    await container.usage_repository.spend_points("u_alice", 401_000, timezone="UTC", at=now())

    with pytest.raises(QuotaBelowThreshold) as excinfo:
        await container.quotas.require_room_to_start_run("u_alice")
    assert excinfo.value.extra == {"threshold": 800, "remaining": 799}


async def test_require_room_to_start_run_allows_with_enough_headroom(
    client: httpx.AsyncClient, container
) -> None:
    await client.get("/api/me")
    await container.quotas.require_room_to_start_run("u_alice")  # 1200 remaining; no raise


async def test_points_hint_is_none_with_comfortable_headroom(
    client: httpx.AsyncClient, container
) -> None:
    await client.get("/api/me")
    assert await container.quotas.points_hint("u_alice") is None


async def test_points_hint_appears_once_under_threshold_plus_margin(
    client: httpx.AsyncClient, container
) -> None:
    """850 remaining is inside the +100 nag margin above the 800 threshold, but not yet
    under the threshold itself — the two are deliberately different numbers."""
    await client.get("/api/me")
    await container.usage_repository.spend_points("u_alice", 350_000, timezone="UTC", at=now())

    assert await container.quotas.points_hint("u_alice") == (850, 800)


async def test_points_hint_folds_in_a_turns_own_unspent_tokens(
    client: httpx.AsyncClient, container
) -> None:
    """The hint has to reflect what a turn is *about* to cost, read before `record_spend`
    writes it — not the balance from before this turn ran."""
    await client.get("/api/me")
    await container.usage_repository.spend_points("u_alice", 300_000, timezone="UTC", at=now())

    hint = await container.quotas.points_hint("u_alice", extra_tokens=100_000)

    assert hint == (800, 800)


@pytest.fixture
def socket_for(container, alice: Principal):
    """Same construction as `test_streaming.py`'s fixture of the same name — duplicated
    locally rather than shared, on the same footing as this file's own `drain_turns`
    above: a Firestore-backed `SocketSession` is what proves `turn_complete` carries the
    hint on the wire, not merely that `QuotaService.points_hint` returns one."""
    from coach.ws.manager import SocketSession
    from streaming_doubles import FakeWebSocket

    class _Opener:
        def __init__(self) -> None:
            self.tasks: list[asyncio.Task[None]] = []

        def open(self) -> FakeWebSocket:
            websocket = FakeWebSocket()
            session = SocketSession(
                websocket,  # type: ignore[arg-type]
                alice,
                turns=container.turns,
                broker=container.broker,
                presence=container.presence_repository,
                board_updates=container.board_updates,
                runs=container.runs,
            )
            self.tasks.append(asyncio.create_task(session.run()))
            return websocket

    opener = _Opener()
    yield opener
    for task in opener.tasks:
        task.cancel()


async def test_turn_complete_carries_a_low_points_hint_once_under_the_margin(
    client: httpx.AsyncClient,
    container,
    session_id: str,
    scripted_model: ScriptedModel,
    socket_for,
    drain_turns: None,
) -> None:
    """docs/09-roadmap.md#research-concurrency: the frontend's whole signal for the
    low-points nag, so the field has to actually reach the wire — not just
    `QuotaService.points_hint`, already asserted above in isolation."""
    scripted_model.usage_tokens = 350_000  # 350 points spent by this very turn
    websocket = socket_for.open()

    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})
    assert response.status_code == 202, response.text
    websocket.send({"type": "subscribe", "turnId": str(response.json()["turnId"])})

    frame = await websocket.wait_for("turn_complete")

    # 1200 - 350 = 850 remaining, under the 800 threshold's +100 margin.
    assert frame["pointsRemaining"] == 850
    assert frame["pointsThreshold"] == 800


async def test_turn_complete_carries_no_hint_with_comfortable_headroom(
    client: httpx.AsyncClient,
    container,
    session_id: str,
    scripted_model: ScriptedModel,
    socket_for,
    drain_turns: None,
) -> None:
    websocket = socket_for.open()

    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})
    websocket.send({"type": "subscribe", "turnId": str(response.json()["turnId"])})

    frame = await websocket.wait_for("turn_complete")

    assert frame["pointsRemaining"] is None
    assert frame["pointsThreshold"] is None
