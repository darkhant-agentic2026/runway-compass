"""`projects/{projectId}/study_plans/{planId}` access.

A subcollection of the project, on the same reasoning as `research_reports`
(`repositories/reports.py`): one collection query, one security boundary
(docs/02-data-model.md).
"""

from __future__ import annotations

from typing import Any

from coach.core.clock import now
from coach.repositories.firestore import PROJECTS, STUDY_PLANS, Database
from coach.services.models import StudyPlan


def _to_plan(doc: Any) -> StudyPlan:
    return StudyPlan.model_validate({**(doc.to_dict() or {}), "id": doc.id})


class StudyPlanRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _collection(self, project_id: str) -> Any:
        return self._db.client.collection(PROJECTS).document(project_id).collection(STUDY_PLANS)

    def _doc(self, project_id: str, plan_id: str) -> Any:
        return self._collection(project_id).document(plan_id)

    async def get(self, project_id: str, plan_id: str) -> StudyPlan | None:
        snapshot = await self._doc(project_id, plan_id).get()
        if not snapshot.exists:
            return None
        return _to_plan(snapshot)

    async def list_all(self, project_id: str) -> list[StudyPlan]:
        """Every plan in the project, unordered — the troubleshooting reset's own read.
        `get_latest`'s ordered, single-document query covers the ordinary "what does
        `project_coach` show" case; this one genuinely needs all of them."""
        return [_to_plan(doc) async for doc in self._collection(project_id).stream()]

    async def get_latest(self, project_id: str) -> StudyPlan | None:
        """The most recently written plan for this project — a `plan_tailor` run's own
        write, or a later `project_coach` revision of one, whichever is newer.

        A single-field `order_by` with no `where`, so — unlike `ReportRepository`'s
        `taskId`-filtered query — this needs no composite index: the emulator and
        deployed Firestore agree on it already.
        """
        query = (
            self._collection(project_id).order_by("createdAt", direction="DESCENDING").limit(1)
        )
        async for doc in query.stream():
            return _to_plan(doc)
        return None

    async def create(self, plan: StudyPlan) -> StudyPlan:
        timestamp = now()
        plan = plan.model_copy(update={"created_at": timestamp, "updated_at": timestamp})
        # `set`, not `create`: like `report_{runId}`, a plan keyed by run id lets a retried
        # `write_study_plan` call overwrite rather than duplicate.
        await self._doc(plan.project_id, plan.id).set(plan.to_document())
        return plan

    async def patch(self, project_id: str, plan_id: str, patch: dict[str, Any]) -> None:
        await self._doc(project_id, plan_id).update({**patch, "updatedAt": now()})


__all__ = ["StudyPlanRepository"]
