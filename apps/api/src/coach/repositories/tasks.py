"""`projects/{projectId}/tasks/{taskId}` access.

Tasks are a subcollection of the project, never global, so a project's board is one
collection query and one security boundary (docs/02-data-model.md).
"""

from __future__ import annotations

from typing import Any

from google.cloud.firestore import AsyncTransaction, AsyncWriteBatch
from google.cloud.firestore_v1.base_query import FieldFilter

from coach.core.clock import now
from coach.repositories.firestore import PROJECTS, TASKS, Database
from coach.services.models import Task, TaskState


def _to_task(doc: Any) -> Task:
    return Task.model_validate({**(doc.to_dict() or {}), "id": doc.id})


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _collection(self, project_id: str) -> Any:
        return self._db.client.collection(PROJECTS).document(project_id).collection(TASKS)

    def _doc(self, project_id: str, task_id: str) -> Any:
        return self._collection(project_id).document(task_id)

    # --- reads ---------------------------------------------------------------------

    async def get(
        self, project_id: str, task_id: str, transaction: AsyncTransaction | None = None
    ) -> Task | None:
        snapshot = await self._doc(project_id, task_id).get(transaction=transaction)
        if not snapshot.exists:
            return None
        return _to_task(snapshot)

    async def list_all(
        self, project_id: str, transaction: AsyncTransaction | None = None
    ) -> list[Task]:
        """Every task in the project, in `order`.

        Callers filter by state in Python. Reading the whole board is deliberate: it is
        one query, it is what the rollup and counts recomputation needs anyway, and a
        board is tens of documents. If projects ever grow to thousands of tasks, this is
        the read to split — see `list_by_states`, which uses the documented composite
        index.
        """
        query = self._collection(project_id).order_by("order")
        return [_to_task(doc) async for doc in query.stream(transaction=transaction)]

    async def list_by_states(self, project_id: str, states: list[TaskState]) -> list[Task]:
        """Tasks in any of `states`, in `order`.

        Uses the `state ASC, order ASC` composite index from
        docs/02-data-model.md#indexes.
        """
        query = (
            self._collection(project_id)
            .where(filter=FieldFilter("state", "in", [s.value for s in states]))
            .order_by("order")
        )
        return [_to_task(doc) async for doc in query.stream()]

    async def find_by_id(self, task_id: str) -> Task | None:
        """Resolve a bare task id without knowing its project.

        `GET /api/tasks/{id}`, `PATCH /api/tasks/{id}`, and friends address a task by id
        alone (docs/04-api-contract.md), but tasks are a subcollection of their project.
        This is the collection-group lookup that docs/02-data-model.md anticipates when
        it denormalizes `projectId` and `ownerUid` onto the task "for collection-group
        queries"; the `id` field is written alongside them for the same reason, since a
        collection-group query cannot filter on a document key by its trailing segment.

        Ownership is *not* checked here — `repositories/` never filters by owner
        implicitly (docs/01-architecture.md). `TaskService` does it explicitly.
        """
        query = (
            self._db.client.collection_group(TASKS)
            .where(filter=FieldFilter("id", "==", task_id))
            .limit(1)
        )
        async for doc in query.stream():
            return _to_task(doc)
        return None

    # `find_current` lived here until M4. It existed so `TaskService` could observe a
    # duplicated `current` task in order to repair it; `in_progress` is not singular, so
    # there is no violation left to observe and no caller. Deleted rather than left as a
    # query nobody runs against an index nobody needs.

    # --- writes --------------------------------------------------------------------

    async def create(self, task: Task, transaction: AsyncTransaction | None = None) -> Task:
        timestamp = now()
        task = task.model_copy(update={"created_at": timestamp, "updated_at": timestamp})
        # `id` is written into the document as well as being the key: see `find_by_id`.
        document = task.to_document()
        reference = self._doc(task.project_id, task.id)
        if transaction is not None:
            transaction.set(reference, document)
        else:
            await reference.set(document)
        return task

    async def patch(
        self,
        project_id: str,
        task_id: str,
        patch: dict[str, Any],
        transaction: AsyncTransaction | None = None,
    ) -> None:
        payload = {**patch, "updatedAt": now()}
        reference = self._doc(project_id, task_id)
        if transaction is not None:
            transaction.update(reference, payload)
            return
        await reference.update(payload)

    def patch_in_batch(
        self, batch: AsyncWriteBatch, project_id: str, task_id: str, patch: dict[str, Any]
    ) -> None:
        """Queue a patch onto an existing batch — the rebalance path.

        A rebalance rewrites every `order` in the project, which is one batch rather
        than one transaction because it reads nothing.
        """
        batch.update(self._doc(project_id, task_id), {**patch, "updatedAt": now()})

    def batch(self) -> AsyncWriteBatch:
        return self._db.client.batch()
