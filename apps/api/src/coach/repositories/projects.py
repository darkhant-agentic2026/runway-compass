"""`projects/{projectId}` access."""

from __future__ import annotations

from typing import Any

from google.cloud.firestore import AsyncTransaction, Query
from google.cloud.firestore_v1.base_query import FieldFilter

from coach.core.clock import now
from coach.repositories.firestore import PROJECTS, Database
from coach.services.models import Project, ProjectStatus


class ProjectRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _collection(self) -> Any:
        return self._db.client.collection(PROJECTS)

    def _doc(self, project_id: str) -> Any:
        return self._collection().document(project_id)

    async def get(
        self, project_id: str, transaction: AsyncTransaction | None = None
    ) -> Project | None:
        snapshot = await self._doc(project_id).get(transaction=transaction)
        if not snapshot.exists:
            return None
        return Project.model_validate({**(snapshot.to_dict() or {}), "id": snapshot.id})

    async def list_for_owner(
        self, owner_uid: str, status: ProjectStatus | None = None
    ) -> list[Project]:
        """Projects owned by `owner_uid`, newest activity first.

        Backed by the `ownerUid ASC, status ASC, updatedAt DESC` index in
        docs/02-data-model.md#indexes.
        """
        query = self._collection().where(filter=FieldFilter("ownerUid", "==", owner_uid))
        if status is not None:
            query = query.where(filter=FieldFilter("status", "==", status.value))
        query = query.order_by("updatedAt", direction=Query.DESCENDING)
        return [
            Project.model_validate({**(doc.to_dict() or {}), "id": doc.id})
            async for doc in query.stream()
        ]

    async def list_autonomous_candidates(self, limit: int = 100) -> list[Project]:
        """Active projects, least-recently-worked-on first — the tick's fairness order.

        Backed by the `status ASC, lastAutonomousRunAt ASC` index in
        docs/02-data-model.md#indexes. Across every owner, because the tick is not a user
        request; the per-owner guards are applied by `SchedulerService`.

        **A project that has never had a run must sort first, and that only works because
        `lastAutonomousRunAt` is written as an explicit `null`.** Firestore omits a
        document from an ordered query when the ordered field is *absent*, so a model that
        dropped its `None` fields on write would make every brand-new project permanently
        invisible to the scheduler — with nothing failing anywhere.
        """
        query = (
            self._collection()
            .where(filter=FieldFilter("status", "==", ProjectStatus.ACTIVE.value))
            .order_by("lastAutonomousRunAt")
            .limit(limit)
        )
        return [
            Project.model_validate({**(doc.to_dict() or {}), "id": doc.id})
            async for doc in query.stream()
        ]

    async def create(self, project: Project) -> Project:
        timestamp = now()
        project = project.model_copy(update={"created_at": timestamp, "updated_at": timestamp})
        document = project.to_document()
        document.pop("id", None)
        await self._doc(project.id).set(document)
        return project

    async def patch(
        self,
        project_id: str,
        patch: dict[str, Any],
        transaction: AsyncTransaction | None = None,
    ) -> None:
        """Apply dotted-path updates, always stamping `updatedAt`."""
        payload = {**patch, "updatedAt": now()}
        if transaction is not None:
            transaction.update(self._doc(project_id), payload)
            return
        await self._doc(project_id).update(payload)
