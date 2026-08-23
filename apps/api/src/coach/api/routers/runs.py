"""The autonomous run ledger (`/api/runs`, `/api/projects/{id}/runs`).

docs/04-api-contract.md#runs. Read access to what the coach did while the learner was
away, and the one-click undo that reverses it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from coach.api.deps import ContainerDep, CurrentUser, Reports, Runs
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import ReportResponse, RunListResponse, RunResponse, RunUndoResponse
from coach.core.errors import NotFound

router = APIRouter(prefix="/api", tags=["runs"])


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, principal: CurrentUser, runs: Runs) -> RunResponse:
    """One ledger row: `status`, `steps[]`, `cursor`, `taskId`, `turnId`, `sessionId`.

    Backs the `['run', runId]` query and the attach path behind
    `POST /api/sessions/{sid}/research`'s `409` — a client told "your coach is already
    working on this project" reads the in-flight run here instead of starting a second one.
    """
    return RunResponse(run=await runs.get(principal, run_id))


@router.get("/runs/{run_id}/report", response_model=ReportResponse)
async def get_run_report(
    run_id: str, principal: CurrentUser, runs: Runs, reports: Reports
) -> ReportResponse:
    """The report this run wrote, once there is one.

    Added at M8 for the research view — the way to find a run's report when there is no
    task to read `latestReportId` off of (a project-scoped run has none). `404` while the
    run is still working or if it never posted a report.
    """
    run = await runs.get(principal, run_id)
    report = await reports.get_for_run(run.project_id, run.id)
    if report is None:
        raise NotFound(f"Run {run_id!r} has not posted a report.")
    return ReportResponse(report=report)


@router.get("/projects/{project_id}/runs", response_model=RunListResponse)
async def list_project_runs(
    project_id: str, principal: CurrentUser, runs: Runs
) -> RunListResponse:
    """Recent runs for the project, newest first — the "Updated by your coach" banner and,
    since M8, the board's "latest research" card."""
    return RunListResponse(runs=await runs.list_for_project(principal, project_id))


@router.get("/tasks/{task_id}/runs", response_model=RunListResponse)
async def list_task_runs(task_id: str, principal: CurrentUser, runs: Runs) -> RunListResponse:
    """+ M8. Recent runs for one task, newest first — the task workspace's research card."""
    return RunListResponse(runs=await runs.list_for_task(principal, task_id))


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
