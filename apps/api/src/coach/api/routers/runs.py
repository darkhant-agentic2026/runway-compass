"""The autonomous run ledger (`/api/runs`, `/api/projects/{id}/runs`).

docs/04-api-contract.md#runs. Read access to what the coach did while the learner was
away, and the one-click undo that reverses it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from coach.api.deps import ContainerDep, CurrentUser, Runs
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import RunListResponse, RunResponse, RunUndoResponse

router = APIRouter(prefix="/api", tags=["runs"])


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, principal: CurrentUser, runs: Runs) -> RunResponse:
    """One ledger row: `status`, `steps[]`, `cursor`, `taskId`, `turnId`.

    Backs the `['run', runId]` query and the attach path behind
    `POST /api/sessions/{sid}/research`'s `409` — a client told "your coach is already
    working on this project" reads the in-flight run here instead of starting a second one.
    """
    return RunResponse(run=await runs.get(principal, run_id))


@router.get("/projects/{project_id}/runs", response_model=RunListResponse)
async def list_project_runs(
    project_id: str, principal: CurrentUser, runs: Runs
) -> RunListResponse:
    """Recent runs for the project, newest first — the "Updated by your coach" banner."""
    return RunListResponse(runs=await runs.list_for_project(principal, project_id))


@router.post(
    "/runs/{run_id}/undo",
    response_model=RunUndoResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def undo_run(
    run_id: str, principal: CurrentUser, runs: Runs, container: ContainerDep
) -> RunUndoResponse:
    """Reverse the run's writes. Idempotent; returns the affected `taskIds`.

    The `board_update` push carries `origin: "user"` even though it is undoing agent
    writes, because the *learner* asked for this one — and the board's "updated by your
    coach" affordances key on that field.
    """
    run, task_ids = await runs.undo(principal, run_id)
    if task_ids:
        await container.board_updates.publish(
            principal.uid,
            project_id=run.project_id,
            task_ids=task_ids,
            origin="user",
            run_id=run.id,
        )
    return RunUndoResponse(run=run, task_ids=task_ids)
