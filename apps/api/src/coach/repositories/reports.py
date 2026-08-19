"""`projects/{projectId}/research_reports/{reportId}` access.

A subcollection of the project, on the same reasoning as tasks: one collection query, one
security boundary (docs/02-data-model.md).
"""

from __future__ import annotations

from typing import Any

from google.cloud.firestore import AsyncTransaction
from google.cloud.firestore_v1.base_query import FieldFilter

from coach.core.clock import now
from coach.repositories.firestore import PROJECTS, RESEARCH_REPORTS, Database
from coach.services.models import ResearchReport


def _to_report(doc: Any) -> ResearchReport:
    return ResearchReport.model_validate({**(doc.to_dict() or {}), "id": doc.id})


class ReportRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _collection(self, project_id: str) -> Any:
        return (
            self._db.client.collection(PROJECTS)
            .document(project_id)
            .collection(RESEARCH_REPORTS)
        )

    def _doc(self, project_id: str, report_id: str) -> Any:
        return self._collection(project_id).document(report_id)

    async def get(self, project_id: str, report_id: str) -> ResearchReport | None:
        snapshot = await self._doc(project_id, report_id).get()
        if not snapshot.exists:
            return None
        return _to_report(snapshot)

    async def list_for_task(self, project_id: str, task_id: str) -> list[ResearchReport]:
        """`GET /api/tasks/{id}/reports` — newest first.

        **Two fields, so a composite index** (`taskId ASC, createdAt DESC`), written into
        `infra/terraform/modules/firestore/main.tf` in the same change as this query. The
        emulator answers it without one and Firestore returns `FAILED_PRECONDITION` on the
        first deployed call — the first row of
        docs/09-roadmap.md#what-a-green-local-run-does-not-prove, and the reason the index
        is not a follow-up.
        """
        query = (
            self._collection(project_id)
            .where(filter=FieldFilter("taskId", "==", task_id))
            .order_by("createdAt", direction="DESCENDING")
        )
        return [_to_report(doc) async for doc in query.stream()]

    async def create(
        self, report: ResearchReport, transaction: AsyncTransaction | None = None
    ) -> ResearchReport:
        timestamp = now()
        report = report.model_copy(update={"created_at": timestamp, "updated_at": timestamp})
        reference = self._doc(report.project_id, report.id)
        document = report.to_document()
        if transaction is not None:
            # `set`, not `create`: docs/05-autonomous-runs.md gives a run's report the
            # deterministic id `report_{runId}` precisely so that a retried step overwrites
            # rather than duplicating. Refusing an existing id would turn that into an
            # error on exactly the path the id was designed for.
            transaction.set(reference, document)
        else:
            await reference.set(document)
        return report

    async def patch(self, project_id: str, report_id: str, patch: dict[str, Any]) -> None:
        await self._doc(project_id, report_id).update({**patch, "updatedAt": now()})
