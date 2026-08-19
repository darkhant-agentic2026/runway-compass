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

from datetime import timedelta
from typing import Any

from google.cloud.firestore import AsyncTransaction, async_transactional

from coach.core.clock import now
from coach.repositories.firestore import AUTONOMOUS_RUNS, PROJECTS, Database
from coach.services.models import AutonomousRun

#: docs/05-autonomous-runs.md: "renewed every 60 s by the executing task with a 5-minute
#: TTL". A crashed instance's lease simply expires, which is why nothing needs to detect
#: the crash.
LEASE_TTL = timedelta(minutes=5)

LOCKS = "locks"
AGENT_LOCK = "agent"


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
        return AutonomousRun.model_validate({**(snapshot.to_dict() or {}), "id": snapshot.id})

    async def create(self, run: AutonomousRun) -> AutonomousRun:
        timestamp = now()
        run = run.model_copy(update={"created_at": timestamp, "updated_at": timestamp})
        await self._doc(run.id).set(run.to_document())
        return run

    async def patch(self, run_id: str, patch: dict[str, Any]) -> None:
        await self._doc(run_id).update({**patch, "updatedAt": now()})

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
