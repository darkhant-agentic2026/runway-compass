"""`RunRepository`'s `expiresAt` — the Firestore TTL field, separate from `updatedAt`.

docs/02-data-model.md#retention. Found from a user report: a completed research report's
card was disappearing from the project board within about a day, because the TTL policy
was pointed at `updatedAt`, which this repository sets to the *current* time on every
write — already "in the past" the instant it lands, which is what a Firestore TTL policy
treats as "delete this". The fix is a dedicated `expiresAt`, computed as `now() + RETENTION`
on every write, the same pattern `repositories/idempotency.py` already used correctly.

The Firestore emulator does not enforce TTL policies at all, so nothing here can assert
that a run is ever actually deleted — what these tests pin is the one thing local can
verify: the *value written* to `expiresAt` is a future timestamp that advances on every
touch, independent of `updatedAt`.
"""

from __future__ import annotations

from datetime import timedelta

from coach.core.clock import now
from coach.repositories.runs import RETENTION
from coach.services.models import AutonomousRun, RunStatus


def _run(run_id: str) -> AutonomousRun:
    return AutonomousRun(
        id=run_id,
        owner_uid="u_alice",
        project_id="p_1",
        task_id=None,
        trigger="manual",
        mode="queued",
        status=RunStatus.RUNNING,
        instance_id="test",
        steps=[],
    )


async def test_create_sets_a_future_expires_at_distinct_from_updated_at(container) -> None:
    before = now()
    created = await container.run_repository.create(_run("r_ttl_create"))

    assert created.updated_at is not None
    assert created.expires_at is not None
    # `updated_at` is the current time; `expires_at` is `RETENTION` out from it. Asserting
    # both, rather than just that `expires_at` is "in the future", is what would have
    # caught the original bug — `updated_at: now()` is also, trivially, "close to now".
    assert abs((created.updated_at - before).total_seconds()) < 5
    assert abs((created.expires_at - before - RETENTION).total_seconds()) < 5


async def test_patch_refreshes_expires_at_on_every_touch(container) -> None:
    created = await container.run_repository.create(_run("r_ttl_patch"))
    assert created.expires_at is not None

    # A run "still making progress" pushes its own expiry out — the behaviour the stale
    # comment on `updated_at` always claimed, now actually true of the field Firestore
    # reads.
    before_second_write = now()
    await container.run_repository.patch(created.id, {"status": RunStatus.COMPLETE.value})
    patched = await container.run_repository.get(created.id)

    assert patched is not None
    assert patched.expires_at is not None
    assert patched.expires_at > created.expires_at
    assert abs((patched.expires_at - before_second_write - RETENTION).total_seconds()) < 5


async def test_expires_at_is_sixty_days_not_thirty(container) -> None:
    """Pins the retention window the user asked to double, not just that it exists."""
    assert timedelta(days=60) == RETENTION
