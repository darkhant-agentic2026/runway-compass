"""Research reports: validation, storage, and promotion into the task's checklist.

docs/02-data-model.md#projectsprojectidresearch_reportsreportid and
docs/03-agent-design.md#domain-tools. This is what `post_research_report` calls, and — like
every other service here — it is also what any REST route would call, so the agent and the
API cannot diverge (docs/01-architecture.md).

**Writing a report is two documents, and the second one is the point.** The report is the
record of what a run found; `tasks/{id}.items[]` is the plan the learner works through. A
report with no checklist behind it is a bibliography, which is the thing the pre-M4 design
produced and the thing docs/10-risks.md Q4 is now explicit about not wanting.
"""

from __future__ import annotations

import logging
from typing import Any

from coach.core.errors import ValidationProblem
from coach.core.ids import item_id as new_item_id
from coach.core.ids import report_id as new_report_id
from coach.core.principal import Principal
from coach.repositories.reports import ReportRepository
from coach.services.models import (
    Citation,
    ItemFeedback,
    ReportItem,
    ReportItemKind,
    ResearchReport,
    ResearchStatus,
    Task,
)
from coach.services.projects import ProjectService
from coach.services.tasks import TaskService

logger = logging.getLogger(__name__)

#: Guard on the size of one report, so a looping model cannot write a checklist nobody
#: could finish. Generous enough that a legitimate deep-research run is never clipped.
MAX_ITEMS_PER_LIST = 15


class ReportService:
    def __init__(
        self,
        reports: ReportRepository,
        tasks: TaskService,
        projects: ProjectService,
    ) -> None:
        self._reports = reports
        self._tasks = tasks
        self._projects = projects

    async def list_for_task(self, principal: Principal, task_id: str) -> list[ResearchReport]:
        """`GET /api/tasks/{id}/reports` — newest first.

        Reports accumulate rather than replacing each other (docs/10-risks.md Q4), so this
        is a list and the UI collapses everything after the first.
        """
        task = await self._tasks.resolve(principal, task_id)
        return await self._reports.list_for_task(task.project_id, task_id)

    async def latest_for_task(self, principal: Principal, task: Task) -> ResearchReport | None:
        """The `latestReport` on `GET /api/tasks/{id}`.

        Read by `latestReportId` rather than by re-running the ordered query: the pointer
        is on the task, and a point read is one lookup against no index.
        """
        if task.latest_report_id is None:
            return None
        return await self._reports.get(task.project_id, task.latest_report_id)

    async def set_feedback(
        self,
        principal: Principal,
        report_id: str,
        task_id: str,
        item_id: str,
        feedback: ItemFeedback | None,
    ) -> ResearchReport:
        """`PATCH /api/reports/{reportId}/items/{itemId}`.

        Feedback only. Completion moved to the task at M4
        (docs/04-api-contract.md#tasks); the router refuses a `completed` field outright
        rather than ignoring it, because a client still sending one must fail loudly rather
        than write nothing and report success.
        """
        task = await self._tasks.resolve(principal, task_id)
        report = await self._reports.get(task.project_id, report_id)
        if report is None or report.task_id != task_id:
            raise ValidationProblem(f"No report {report_id!r} for this task.")
        if not any(item.item_id == item_id for item in (*report.required, *report.optional)):
            raise ValidationProblem(f"No item {item_id!r} in this report.")

        progress = dict(report.progress.feedback)
        if feedback is None:
            progress.pop(item_id, None)
        else:
            progress[item_id] = feedback
        await self._reports.patch(
            task.project_id, report_id, {"progress": {"feedback": progress}}
        )
        return report.model_copy(
            update={"progress": report.progress.model_copy(update={"feedback": progress})}
        )

    async def post_report(
        self,
        principal: Principal,
        task_id: str,
        *,
        summary: str,
        required: list[dict[str, Any]],
        optional: list[dict[str, Any]],
        budget_minutes: int,
        citations: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        report_id: str | None = None,
    ) -> tuple[ResearchReport, Task]:
        """Validate, store, and promote. The whole of `post_research_report`'s body.

        Returns the report and the task as it stands afterwards — the caller needs the
        second to tell the model whether the task left `draft`.
        """
        task = await self._tasks.resolve(principal, task_id)

        required_items = _validate_items(required, "required")
        optional_items = _validate_items(optional, "optional")
        _assert_disjoint(required_items, optional_items)

        total = sum(item.minutes for item in required_items)
        if total > budget_minutes:
            raise ValidationProblem(
                f"The required list adds up to {total} minutes against a budget of "
                f"{budget_minutes}. Move the least essential items to the optional list "
                "until the required ones fit — the required list is what the learner has "
                "to get through, so it has to be doable in the time the task has."
            )

        report = ResearchReport(
            id=report_id or new_report_id(),
            project_id=task.project_id,
            owner_uid=task.owner_uid,
            task_id=task_id,
            run_id=run_id,
            session_id=session_id,
            summary=summary,
            required=required_items,
            optional=optional_items,
            total_required_minutes=total,
            budget_minutes=budget_minutes,
            citations=[Citation.model_validate(c) for c in (citations or [])],
        )
        stored = await self._reports.create(report)

        # Promotion, and then the pointer. In that order: `replace_items` runs the
        # derivation that takes the task out of `draft` (invariant 1), and a task pointing
        # at a report whose items had not landed yet would render an empty checklist beside
        # a "materials ready" badge.
        updated = await self._tasks.replace_items(
            principal,
            task_id,
            [_as_checklist_item(item) for item in required_items],
            source_report_id=stored.id,
        )
        updated = await self._tasks.set_research(
            principal,
            task_id,
            status=ResearchStatus.DONE,
            latest_report_id=stored.id,
        )
        logger.info(
            "research report posted",
            extra={
                "task_id": task_id,
                "report_id": stored.id,
                "run_id": run_id,
                "required": len(required_items),
                "optional": len(optional_items),
                "minutes": total,
            },
        )
        return stored, updated


def _validate_items(drafts: list[dict[str, Any]], label: str) -> list[ReportItem]:
    """Model output to `ReportItem`s, with the ids assigned here rather than there.

    docs/02-data-model.md: "`itemId` is assigned server-side by `post_research_report`, not
    by the model — the model returns items, the tool numbers them."
    """
    if len(drafts) > MAX_ITEMS_PER_LIST:
        raise ValidationProblem(
            f"{len(drafts)} {label} items is more than a single task can carry "
            f"({MAX_ITEMS_PER_LIST} at most). Keep the ones that matter."
        )
    items: list[ReportItem] = []
    for draft in drafts:
        kind = str(draft.get("kind") or "").strip()
        try:
            parsed_kind = ReportItemKind(kind)
        except ValueError:
            allowed = ", ".join(k.value for k in ReportItemKind)
            raise ValidationProblem(
                f"{kind!r} is not a kind of material. Use one of: {allowed}."
            ) from None
        why = str(draft.get("why") or "").strip()
        if label == "required" and not why:
            # docs/08-testing.md: "requires `why` on every required item". It is not
            # decoration — it becomes the checklist entry's one line, so an item without
            # one produces a step the learner cannot act on.
            raise ValidationProblem(
                f"The required item {draft.get('title')!r} has no `why`. Every required "
                "item needs one line, addressed to the learner, saying what it gives them "
                "for *this* task — it is what they will see on their checklist."
            )
        minutes = draft.get("minutes")
        if not isinstance(minutes, int) or minutes < 1:
            raise ValidationProblem(
                f"The item {draft.get('title')!r} needs `minutes` as a whole number of minutes."
            )
        items.append(
            ReportItem(
                item_id=new_item_id(),
                kind=parsed_kind,
                title=str(draft.get("title") or "").strip() or "Untitled",
                url=str(draft.get("url") or "") or None,
                minutes=minutes,
                why=why,
                details=str(draft.get("details") or ""),
                source=draft.get("source") or "web",
                meta={str(k): str(v) for k, v in (draft.get("meta") or {}).items()},
                guided=draft.get("guided"),
            )
        )
    return items


def _assert_disjoint(required: list[ReportItem], optional: list[ReportItem]) -> None:
    """docs/08-testing.md: "rejects an item in both lists".

    Matched on title and url rather than on id, because the ids were minted a moment ago
    and are distinct by construction — the duplicate the model actually produces is the
    same recommendation written out twice.
    """
    keys = {(item.title, item.url) for item in required}
    duplicated = [item.title for item in optional if (item.title, item.url) in keys]
    if duplicated:
        raise ValidationProblem(
            f"{duplicated[0]!r} is in both the required and the optional list. An item is "
            "either something the learner has to do for this task or something they may "
            "do if they want more; it cannot be both."
        )


def _as_checklist_item(item: ReportItem) -> dict[str, Any]:
    """A report item as the checklist entry it becomes.

    `why` is the `shortDescription` and the title is folded into the details, not the other
    way round. A checklist reads as things to do — "Read sections 3 and 4 for the
    cancellation rules you will need" — where a list of titles reads as a bibliography, and
    the learner has to work out what each one is for.
    """
    details = item.details.strip()
    if not details:
        details = f"{item.title} — {item.url}" if item.url else item.title
    return {
        "itemId": item.item_id,
        "shortDescription": item.why or item.title,
        "details": details,
        "guided": item.is_guided,
        "minutes": item.minutes,
        "url": item.url,
    }


__all__ = ["MAX_ITEMS_PER_LIST", "ReportService"]
