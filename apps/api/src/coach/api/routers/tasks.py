"""Tasks (`/api/tasks`, plus the create-under-a-project route)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from coach.api.deps import CurrentUser, Reports, Tasks
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import (
    ProjectDerived,
    TaskCreate,
    TaskDetailResponse,
    TaskItemPatch,
    TaskItemReorder,
    TaskItemsAdd,
    TaskMutationResponse,
    TaskPatch,
    TaskReorder,
    TaskSplit,
    TaskStateChange,
)
from coach.services.models import Task
from coach.services.tasks import TaskService

router = APIRouter(prefix="/api", tags=["tasks"])


async def _mutation_response(tasks: TaskService, task: Task) -> TaskMutationResponse:
    parent, project = await tasks.mutation_context(task)
    return TaskMutationResponse(
        task=task,
        parent=parent,
        project=ProjectDerived(
            id=project.id,
            next_up_task_id=project.next_up_task_id,
            counts=project.counts.to_document(),
        ),
    )


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskMutationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(idempotency_guard)],
)
async def create_task(
    project_id: str, body: TaskCreate, principal: CurrentUser, tasks: Tasks
) -> TaskMutationResponse:
    task = await tasks.create_task(
        principal,
        project_id,
        title=body.title,
        description=body.description,
        estimated_minutes=body.estimated_minutes,
        parent_task_id=body.parent_task_id,
        after_task_id=body.after_task_id,
        needs_research=body.needs_research,
    )
    return await _mutation_response(tasks, task)


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str, principal: CurrentUser, tasks: Tasks, reports: Reports
) -> TaskDetailResponse:
    """Task + `items[]` + subtasks + `latestReport` (docs/04-api-contract.md).

    The report is read by `latestReportId` rather than by re-running the ordered query —
    one point read against no index — and is `None` until a run has posted one.
    """
    task = await tasks.get_with_subtasks(principal, task_id)
    return TaskDetailResponse(
        task=task, latest_report=await reports.latest_for_task(principal, task)
    )


@router.patch(
    "/tasks/{task_id}",
    response_model=TaskMutationResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def patch_task(
    task_id: str, body: TaskPatch, principal: CurrentUser, tasks: Tasks
) -> TaskMutationResponse:
    task = await tasks.update_task(
        principal,
        task_id,
        title=body.title,
        description=body.description,
        estimated_minutes=body.estimated_minutes,
        needs_research=body.needs_research,
    )
    return await _mutation_response(tasks, task)


@router.post(
    "/tasks/{task_id}/state",
    response_model=TaskMutationResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def set_task_state(
    task_id: str, body: TaskStateChange, principal: CurrentUser, tasks: Tasks
) -> TaskMutationResponse:
    """Validated against the state machine in docs/02-data-model.md."""
    task = await tasks.set_state(
        principal, task_id, body.state, postponed_until=body.postponed_until
    )
    return await _mutation_response(tasks, task)


@router.post(
    "/tasks/{task_id}/reorder",
    response_model=TaskMutationResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def reorder_task(
    task_id: str, body: TaskReorder, principal: CurrentUser, tasks: Tasks
) -> TaskMutationResponse:
    task = await tasks.reorder(
        principal,
        task_id,
        after_task_id=body.after_task_id,
        before_task_id=body.before_task_id,
    )
    return await _mutation_response(tasks, task)


# --- checklist items -------------------------------------------------------------------
# docs/02-data-model.md#task-items. Every one of these returns the *whole task* rather than
# the item, because a write to the checklist can move the task's `state` (invariant 6) and
# the project's `counts` — a client that patched only the item into its cache would show a
# finished checklist above a task still badged "in progress" until the next refetch.


@router.post(
    "/tasks/{task_id}/items",
    response_model=TaskMutationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(idempotency_guard)],
)
async def add_task_items(
    task_id: str, body: TaskItemsAdd, principal: CurrentUser, tasks: Tasks
) -> TaskMutationResponse:
    task = await tasks.add_items(
        principal, task_id, [item.model_dump(by_alias=True) for item in body.items]
    )
    return await _mutation_response(tasks, task)


@router.patch(
    "/tasks/{task_id}/items/{item_id}",
    response_model=TaskMutationResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def patch_task_item(
    task_id: str,
    item_id: str,
    body: TaskItemPatch,
    principal: CurrentUser,
    tasks: Tasks,
) -> TaskMutationResponse:
    """The checkbox and the inline edit."""
    task = await tasks.patch_item(
        principal,
        task_id,
        item_id,
        completed=body.completed,
        short_description=body.short_description,
        details=body.details,
        guided=body.guided,
    )
    return await _mutation_response(tasks, task)


@router.post(
    "/tasks/{task_id}/items/{item_id}/reorder",
    response_model=TaskMutationResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def reorder_task_item(
    task_id: str,
    item_id: str,
    body: TaskItemReorder,
    principal: CurrentUser,
    tasks: Tasks,
) -> TaskMutationResponse:
    task = await tasks.reorder_item(
        principal,
        task_id,
        item_id,
        after_item_id=body.after_item_id,
        before_item_id=body.before_item_id,
    )
    return await _mutation_response(tasks, task)


@router.delete(
    "/tasks/{task_id}/items/{item_id}",
    response_model=TaskMutationResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def delete_task_item(
    task_id: str, item_id: str, principal: CurrentUser, tasks: Tasks
) -> TaskMutationResponse:
    task = await tasks.delete_item(principal, task_id, item_id)
    return await _mutation_response(tasks, task)


@router.post(
    "/tasks/{task_id}/split",
    response_model=TaskDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(idempotency_guard)],
)
async def split_task(
    task_id: str, body: TaskSplit, principal: CurrentUser, tasks: Tasks
) -> TaskDetailResponse:
    """Manual split. The agent's `split_task` tool calls the same service method (M3)."""
    result = await tasks.split_task(
        principal,
        task_id,
        [draft.model_dump(by_alias=True) for draft in body.subtasks],
    )
    return TaskDetailResponse(task=result)
