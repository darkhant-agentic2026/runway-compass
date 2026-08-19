"""Research reports (`/api/tasks/{id}/reports`, `/api/reports/{id}/items/{id}`).

docs/04-api-contract.md#tasks. Two endpoints and a deliberate absence: item *completion*
is not here. It moved onto the task at M4, because a task's checklist is one list and a
task can have several reports — see `api/routers/tasks.py` for where it went.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from coach.api.deps import CurrentUser, Reports
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import ReportItemFeedback, ReportListResponse, ReportResponse

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/tasks/{task_id}/reports", response_model=ReportListResponse)
async def list_reports(
    task_id: str, principal: CurrentUser, reports: Reports
) -> ReportListResponse:
    """Every report for this task, newest first.

    Reports accumulate rather than replacing each other (docs/10-risks.md Q4), so the
    workspace renders the newest expanded and collapses the rest into "3 earlier runs".
    """
    return ReportListResponse(reports=await reports.list_for_task(principal, task_id))


@router.patch(
    "/reports/{report_id}/items/{item_id}",
    response_model=ReportResponse,
    dependencies=[Depends(idempotency_guard)],
)
async def set_item_feedback(
    report_id: str,
    item_id: str,
    body: ReportItemFeedback,
    principal: CurrentUser,
    reports: Reports,
) -> ReportResponse:
    """The thumbs-up/down control on a recommendation.

    Writes `progress.feedback` and nothing else. A body carrying `completed` is refused by
    `ReportItemFeedback`'s `extra="forbid"` — the field used to be accepted here, and a
    client still sending one must fail loudly rather than write nothing and report success
    (docs/08-testing.md#task-items).
    """
    report = await reports.set_feedback(
        principal, report_id, body.task_id, item_id, body.feedback
    )
    return ReportResponse(report=report)
