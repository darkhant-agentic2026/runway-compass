"""`autonomous_runs/{runId}` and `projects/{projectId}/locks/agent`.

docs/05-autonomous-runs.md. Two collections, one module, because they are always used
together: nothing writes a run without first holding the lease, and the lease is
meaningless without a run to name as its holder.

**M4 builds the manual half of this.** The endpoint, the lease, the ledger document, and
the `research` and `post_report` steps are what `POST /api/sessions/{sid}/research` needs.
The scheduler, Cloud Tasks, recovery, and the two board-reshaping steps are M5, which
extends these two classes rather than replacing them — the document shape here is the one
that document specifies, so a manual run and a scheduled one are the same row in the same
ledger.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from google.cloud.firestore import AsyncTransaction, Query, async_transactional
from google.cloud.firestore_v1.base_query import FieldFilter

from coach.core.clock import now
from coach.repositories.firestore import AUTONOMOUS_RUNS, PROJECTS, Database
from coach.services.models import AutonomousRun, RunStatus

#: docs/05-autonomous-runs.md: "renewed every 60 s by the executing task with a 5-minute
#: TTL". A crashed instance's lease simply expires, which is why nothing needs to detect
#: the crash.
LEASE_TTL = timedelta(minutes=5)

#: docs/02-data-model.md#retention: "autonomous_runs/* — TTL 60 days on expiresAt". A
#: **separate** field from `updatedAt`, on the same reasoning `idempotency.py`'s own
#: `RETENTION` is: the Firestore TTL policy deletes a document once the *value stored in
#: its TTL field* is in the past, which means the app has to write the absolute future
#: expiry itself — writing `updatedAt: now()` there (the original M5 shape) put the
#: current moment in the field, which is already "in the past" the instant the write
#: lands, so the row became eligible for Firestore's TTL sweep within about a day of
#: *any* write to it rather than 30 (now 60) days after the last one. Invisible locally:
#: the Firestore emulator does not enforce TTL policies at all
#: (docs/09-roadmap.md#what-a-green-local-run-does-not-prove), so this only ever showed up
#: as a completed research report's card disappearing from a deployed project's board
#: after about a day.
RETENTION = timedelta(days=60)

LOCKS = "locks"
AGENT_LOCK = "agent"


def _to_run(doc: Any) -> AutonomousRun:
    return AutonomousRun.model_validate({**(doc.to_dict() or {}), "id": doc.id})


class LeaseHeld(RuntimeError):
    """Somebody else holds the project's agent lease.

    Carries the holding run's id, because that is what makes the `409` from
    `POST /api/sessions/{sid}/research` actionable — the client attaches to the in-flight
    run rather than starting a duplicate (docs/04-api-contract.md).
    """

    def __init__(self, run_id: str | None) -> None:
        super().__init__(f"the project agent lease is held by {run_id or 'another run'}")
        self.run_id = run_id


class RunRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, run_id: str) -> Any:
        return self._db.client.collection(AUTONOMOUS_RUNS).document(run_id)

    def _lock(self, project_id: str) -> Any:
        return (
            self._db.client.collection(PROJECTS)
            .document(project_id)
            .collection(LOCKS)
            .document(AGENT_LOCK)
        )

    async def get(self, run_id: str) -> AutonomousRun | None:
        snapshot = await self._doc(run_id).get()
        if not snapshot.exists:
            return None
        return _to_run(snapshot)

    async def create(self, run: AutonomousRun) -> AutonomousRun:
        timestamp = now()
        run = run.model_copy(
            update={
                "created_at": timestamp,
                "updated_at": timestamp,
                "expires_at": timestamp + RETENTION,
            }
        )
        await self._doc(run.id).set(run.to_document())
        return run

    async def patch(self, run_id: str, patch: dict[str, Any]) -> None:
        # `expiresAt` is refreshed on every touch, same as `updatedAt` — a run still
        # making progress keeps pushing its own expiry out, exactly as the pre-fix comment
        # on `updatedAt` always intended, just on a field that actually behaves that way.
        timestamp = now()
        await self._doc(run_id).update(
            {**patch, "updatedAt": timestamp, "expiresAt": timestamp + RETENTION}
        )

    async def list_for_project(self, project_id: str, limit: int = 20) -> list[AutonomousRun]:
        """Recent runs for one project, newest first — the "Updated by your coach" banner.

        Two indexed fields (`projectId ASC, createdAt DESC`), so it needs the composite in
        docs/02-data-model.md#indexes. Ownership is asserted by the caller against the run's
        own `ownerUid`, as everywhere else in `repositories/`.
        """
        query = (
            self._db.client.collection(AUTONOMOUS_RUNS)
            .where(filter=FieldFilter("projectId", "==", project_id))
            .order_by("createdAt", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [_to_run(doc) async for doc in query.stream()]

    async def list_for_task(self, task_id: str, limit: int = 20) -> list[AutonomousRun]:
        """Recent runs for one task, newest first — the task workspace's research card,
        added at M8 on the same shape `list_for_project` already uses.

        Two indexed fields (`taskId ASC, createdAt DESC`), so it needs the composite
        docs/02-data-model.md#indexes adds for this query.
        """
        query = (
            self._db.client.collection(AUTONOMOUS_RUNS)
            .where(filter=FieldFilter("taskId", "==", task_id))
            .order_by("createdAt", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [_to_run(doc) async for doc in query.stream()]

    async def list_stuck(self, at: datetime, limit: int = 50) -> list[AutonomousRun]:
        """Runs whose executing instance died: `running` with an expired lease.

        Invariant 1 — "interrupted work is finished before new work is started" — is this
        query plus `list_retryable` below. Backed by `status ASC, leaseExpiresAt ASC`,
        which docs/02-data-model.md has carried since M1 for exactly this caller.

        Returns **every** stuck run, including ones that have burned their attempts; the
        caller decides which to re-enqueue and which to bury. Splitting that here would put
        the poison-pill policy in a repository.
        """
        query = (
            self._db.client.collection(AUTONOMOUS_RUNS)
            .where(filter=FieldFilter("status", "==", RunStatus.RUNNING.value))
            .where(filter=FieldFilter("leaseExpiresAt", "<", at))
            .order_by("leaseExpiresAt")
            .limit(limit)
        )
        return [_to_run(doc) async for doc in query.stream()]

    async def list_retryable(self, limit: int = 50) -> list[AutonomousRun]:
        """`failed` runs with attempts left.

        One equality filter, so no composite: `attempts < maxAttempts` is compared in
        Python. A range filter on a second field would need an index for a list already
        bounded by how many runs can fail between two ticks, and `maxAttempts` is per-run
        rather than a constant the query could inline.
        """
        query = (
            self._db.client.collection(AUTONOMOUS_RUNS)
            .where(filter=FieldFilter("status", "==", RunStatus.FAILED.value))
            .limit(limit)
        )
        return [
            run
            async for run in (_to_run(doc) async for doc in query.stream())
            if run.attempts < run.max_attempts and run.mode == "queued"
        ]

    async def lease_holder(self, project_id: str) -> str | None:
        """Which run holds the project's lease, if any is live.

        A *read*, for the tick's guard. The tick decides whether to enqueue; taking the
        lease is the executor's job, minutes later — and taking it here would mean holding
        it across a queue.
        """
        snapshot = await self._lock(project_id).get()
        if not snapshot.exists:
            return None
        held = snapshot.to_dict() or {}
        expires = held.get("expiresAt")
        if expires is None or expires <= now():
            return None
        return str(held.get("holder", "")).removeprefix("run:") or None

    async def renew_lease(self, project_id: str, run_id: str) -> bool:
        """Push the lease's expiry out, if it is still ours.

        Returns whether we still hold it. A `False` is a real answer rather than an error:
        the executing task has lost its lease — its instance stalled long enough for the
        TTL to pass and somebody else to take it — and what it should do about that is stop,
        which is a decision for the caller and not for a repository.
        """
        reference = self._lock(project_id)
        held = {"still_ours": False}

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> None:
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return
            if (snapshot.to_dict() or {}).get("holder") != f"run:{run_id}":
                return
            held["still_ours"] = True
            transaction.update(reference, {"expiresAt": now() + LEASE_TTL})

        await self._db.run(txn)
        return held["still_ours"]

    async def acquire_lease(self, project_id: str, run_id: str, instance_id: str) -> None:
        """Take the project's agent lease, or raise `LeaseHeld`.

        Create-if-absent-or-expired, in a transaction. The expiry check is inside it
        deliberately: a lease that has run out is *taken*, not merely reported, and doing
        that outside a transaction is how two runs both decide the old lease is dead.

        This is the same lease an autonomous run takes at M5, which is what makes "manual
        and autonomous never collide" true rather than hopeful
        (docs/05-autonomous-runs.md#the-lease).
        """
        reference = self._lock(project_id)

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> None:
            snapshot = await reference.get(transaction=transaction)
            at = now()
            if snapshot.exists:
                held = snapshot.to_dict() or {}
                expires = held.get("expiresAt")
                if expires is not None and expires > at:
                    raise LeaseHeld(str(held.get("holder", "")).removeprefix("run:") or None)
            transaction.set(
                reference,
                {
                    "holder": f"run:{run_id}",
                    "acquiredAt": at,
                    "expiresAt": at + LEASE_TTL,
                    "instanceId": instance_id,
                },
            )

        await self._db.run(txn)

    async def release_lease(self, project_id: str, run_id: str) -> None:
        """Give the lease back, if it is still ours.

        Guarded by holder rather than deleted outright: a run whose lease had already
        expired and been taken by someone else must not delete *their* lease on its way
        out. Called from a `finally`, so it runs on the failure path too.
        """
        reference = self._lock(project_id)

        @async_transactional
        async def txn(transaction: AsyncTransaction) -> None:
            snapshot = await reference.get(transaction=transaction)
            if not snapshot.exists:
                return
            if (snapshot.to_dict() or {}).get("holder") != f"run:{run_id}":
                return
            transaction.delete(reference)

        await self._db.run(txn)
