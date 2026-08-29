"""Study plans: validation, storage, and materializing a plan into board tasks.

docs/02-data-model.md#projectsprojectidstudy_plansplanid and
docs/03-agent-design.md#the-research-pipeline-since-m9. `post_plan` is what `write_study_plan`
calls — `plan_tailor`'s one write, the taskless pipeline's analogue of
`ReportService.post_report`. `materialize` is what the *standalone*, not-yet-wired
`materialize_study_plan` tool calls to turn a written plan into real tasks with items.

Kept as its own module, and its own repository, rather than folded into `reports.py`: a
`StudyPlan` is a project-level roadmap proposal, not one task's checklist, and the two
documents' lifecycles genuinely differ — a report is written once and promotes into a task
in the same transaction; a plan is written once and may or may not ever be materialized,
by a different caller, in a different conversation, later.
"""

from __future__ import annotations

import contextlib
from typing import Any

from coach.core.clock import now
from coach.core.errors import NotFound, ValidationProblem
from coach.core.ids import study_plan_id as new_study_plan_id
from coach.core.principal import Principal
from coach.repositories.study_plans import StudyPlanRepository
from coach.services.models import (
    GUIDED_KINDS,
    Origin,
    PlanDecision,
    PlanTaskEntry,
    ProposedItem,
    ProposedTask,
    ReportItemKind,
    StudyPlan,
    Task,
)
from coach.services.projects import ProjectService
from coach.services.reports import MAX_ITEMS_PER_LIST
from coach.services.tasks import TaskService

#: Guard on the size of one plan, so a looping model cannot propose more tasks than a
#: learner could ever act on. `reports.MAX_ITEMS_PER_LIST` (15) is reused per-task below —
#: this is the *task count* cap, the plan-level analogue.
MAX_PROPOSED_TASKS = 12

#: What `materialize` creates board tasks for by default — the choice confirmed with the
#: user: `include` and `additional` (deep-dive) both become real tasks, `exclude`/`reject`
#: stay recorded on the plan document only.
DEFAULT_MATERIALIZE_DECISIONS: frozenset[PlanDecision] = frozenset({"include", "additional"})

_DECISIONS = frozenset({"include", "additional", "exclude", "reject"})


class StudyPlanService:
    def __init__(
        self,
        plans: StudyPlanRepository,
        tasks: TaskService,
        projects: ProjectService,
    ) -> None:
        self._plans = plans
        self._tasks = tasks
        self._projects = projects

    async def get(self, project_id: str, plan_id: str) -> StudyPlan | None:
        return await self._plans.get(project_id, plan_id)

    async def get_latest(self, principal: Principal, project_id: str) -> StudyPlan | None:
        """The plan `view_study_plan` shows `project_coach` — a run's own write, or a
        later coach revision of one, whichever was written most recently.

        `None` when the project has no plan yet, the same as `get`/`get_for_run`.
        """
        plan = await self._plans.get_latest(project_id)
        if plan is None or not principal.owns(plan.owner_uid):
            return None
        return plan

    async def get_for_run(self, project_id: str, run_id: str) -> StudyPlan | None:
        """The plan a roadmap run wrote, if it has finished writing one.

        `plan_{runId}` is deterministic for every roadmap turn (`agents/research_tools.py`
        mints the id the same way `report_{runId}` does), so this is a point read, on the
        same reasoning as `ReportService.get_for_run`. `None` while the run is still
        `running` or if it never reaches `write_study_plan` at all. Backs
        `GET /api/runs/{runId}/plan`.
        """
        return await self._plans.get(project_id, f"plan_{run_id}")

    async def post_plan(
        self,
        principal: Principal,
        *,
        project_id: str,
        title: str,
        short_description: str,
        long_description: str,
        memo: str,
        proposed_tasks: list[dict[str, Any]],
        plan: list[dict[str, Any]],
        run_id: str | None = None,
        session_id: str | None = None,
        plan_id: str | None = None,
    ) -> StudyPlan:
        """Validate `task_proposer`'s tasks and `plan_tailor`'s tailoring, and store both.

        Raises:
            ValidationProblem: on any of the shape/reference problems documented on
                `_validate_proposed_tasks` and `_validate_plan` below.
        """
        tasks = _validate_proposed_tasks(proposed_tasks)
        entries = _validate_plan(plan, {task.slug for task in tasks})

        study_plan = StudyPlan(
            id=plan_id or new_study_plan_id(),
            project_id=project_id,
            owner_uid=principal.uid,
            run_id=run_id,
            session_id=session_id,
            title=title.strip(),
            short_description=short_description.strip(),
            long_description=long_description,
            memo=memo,
            proposed_tasks=tasks,
            plan=entries,
        )
        return await self._plans.create(study_plan)

    async def materialize(
        self,
        principal: Principal,
        *,
        project_id: str,
        plan_id: str,
        decisions: frozenset[PlanDecision] = DEFAULT_MATERIALIZE_DECISIONS,
    ) -> list[Task]:
        """Turn a written plan into real board tasks, with items, in dependency order.

        Idempotent: a plan that has already been materialized returns the tasks it created
        the first time rather than creating a second set — the same reason
        `post_research_report` keys its report `report_{runId}`, applied to a tool call
        that might legitimately be retried.

        Raises:
            NotFound: no such plan, or it belongs to another owner.
            ValidationProblem: the chosen entries' `prerequisite_tasks`/`after` edges
                contain a cycle.
        """
        plan = await self._plans.get(project_id, plan_id)
        if plan is None or not principal.owns(plan.owner_uid):
            raise NotFound(f"No study plan {plan_id!r}.")

        if plan.materialized_at is not None:
            resolved: list[Task] = []
            for task_id in plan.materialized_task_ids:
                with contextlib.suppress(NotFound):
                    resolved.append(await self._tasks.resolve(principal, task_id))
            return resolved

        by_slug = {task.slug: task for task in plan.proposed_tasks}
        chosen = [
            entry
            for entry in plan.plan
            if entry.decision in decisions and entry.task_slug in by_slug
        ]
        ordered = _topological_order(chosen)

        prefs = await self._projects.effective_prefs(principal, project_id)
        created: dict[str, Task] = {}
        previous_task_id: str | None = None
        for entry in ordered:
            proposed = by_slug[entry.task_slug]
            total_minutes = sum(item.minutes for item in proposed.required)
            task = await self._tasks.create_task(
                principal,
                project_id,
                title=proposed.title,
                description=proposed.description,
                estimated_minutes=total_minutes or prefs.default_task_minutes,
                after_task_id=previous_task_id,
                # It arrives with its materials already attached — there is nothing left
                # for a research run to do.
                needs_research=False,
                origin=Origin.AGENT,
                # The plan's own `optional[]` never becomes a checklist item (below), so
                # this is what lets the task workspace still reach it after the fact —
                # `docs/02-data-model.md`'s "a plan carries no ongoing relationship once
                # its tasks exist" is about `prerequisiteTasks`/`after`, not this pointer.
                study_plan_run_id=plan.run_id,
            )
            if proposed.required:
                task = await self._tasks.add_items(
                    principal,
                    task.id,
                    [_as_checklist_draft(item) for item in proposed.required],
                    # `None`: no `ResearchReport` backs a materialized item — it came from
                    # a `StudyPlan` instead, which `TaskItem` has no field for yet.
                    source_report_id=None,
                )
            created[entry.task_slug] = task
            previous_task_id = task.id

        task_ids = [created[entry.task_slug].id for entry in ordered]
        await self._plans.patch(
            project_id, plan_id, {"materializedAt": now(), "materializedTaskIds": task_ids}
        )
        return [created[entry.task_slug] for entry in ordered]

    async def reset_materialization(self, project_id: str) -> int:
        """Troubleshooting only: clears `materializedAt`/`materializedTaskIds` on every
        plan in the project, so a later `materialize` call rebuilds the board instead of
        hitting its own idempotency guard and returning nothing (the guard's whole point
        otherwise — this is the one caller meant to defeat it).

        Paired with `TaskService.delete_all_tasks`, never called alone: a plan reset
        without the board wipe just makes the next `materialize` call double the tasks,
        and a board wipe without this reset leaves `materialize` resolving ids that no
        longer exist and creating nothing.

        Returns how many plans were reset.
        """
        plans = await self._plans.list_all(project_id)
        reset = [plan for plan in plans if plan.materialized_at is not None]
        for plan in reset:
            await self._plans.patch(
                project_id, plan.id, {"materializedAt": None, "materializedTaskIds": []}
            )
        return len(reset)

    async def revise(
        self,
        principal: Principal,
        *,
        project_id: str,
        plan_id: str,
        plan: list[dict[str, Any]],
    ) -> StudyPlan:
        """`revise_study_plan`'s write — `project_coach`'s own copy of a plan, re-deciding
        which proposed tasks to include and where each sits.

        A new document, not a patch to `plan_id`:
        docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer
        scopes a revision to be a copy, "so the original plan_tailor verdict stays legible
        against whatever replaces it." `proposedTasks` — the material `task_proposer`
        actually found — carries over unchanged; only `plan[]`, the tailoring, is new.

        Raises:
            NotFound: no such plan, or it belongs to another owner.
            ValidationProblem: `plan`'s shape is invalid (the same checks `post_plan`
                applies to `plan_tailor`'s own write), or `plan_id` has already been
                materialized — revise before materializing, not after, since a
                materialized plan's board tasks would not reflect the revision.
        """
        original = await self._plans.get(project_id, plan_id)
        if original is None or not principal.owns(original.owner_uid):
            raise NotFound(f"No study plan {plan_id!r}.")
        if original.materialized_at is not None:
            raise ValidationProblem(
                "This plan has already been materialized into board tasks. Revise a plan "
                "before materializing it, not after — the board would not reflect the "
                "revision."
            )

        entries = _validate_plan(plan, {task.slug for task in original.proposed_tasks})

        revised = StudyPlan(
            id=new_study_plan_id(),
            project_id=project_id,
            owner_uid=principal.uid,
            run_id=original.run_id,
            session_id=original.session_id,
            title=original.title,
            short_description=original.short_description,
            long_description=original.long_description,
            memo=original.memo,
            proposed_tasks=original.proposed_tasks,
            plan=entries,
            revised_from_plan_id=original.id,
        )
        return await self._plans.create(revised)


# --- proposed-task validation -----------------------------------------------------------


def _validate_proposed_tasks(drafts: list[dict[str, Any]]) -> list[ProposedTask]:
    """`task_proposer`'s `tasks[]`, checked the way `reports._validate_items` checks a
    report's items — same rules (kind, minutes, `why` on required, at least one required
    item per task), reimplemented rather than shared: a `ProposedItem` has no `item_id`
    yet, so the two models genuinely differ and a forced-generic shared validator would
    cost more clarity than it saves.
    """
    if not drafts:
        raise ValidationProblem("A study plan needs at least one proposed task.")
    if len(drafts) > MAX_PROPOSED_TASKS:
        raise ValidationProblem(
            f"{len(drafts)} proposed tasks is more than a single plan can hold "
            f"({MAX_PROPOSED_TASKS} at most). Group the research more tightly."
        )

    tasks: list[ProposedTask] = []
    slugs: set[str] = set()
    for draft in drafts:
        slug = str(draft.get("slug") or "").strip()
        if not slug:
            raise ValidationProblem("Every proposed task needs a `slug`.")
        if slug in slugs:
            raise ValidationProblem(
                f"{slug!r} is used by more than one proposed task. Slugs must be unique."
            )
        slugs.add(slug)
        title = str(draft.get("title") or "").strip()
        if not title:
            raise ValidationProblem(f"The proposed task {slug!r} has no title.")

        required = _validate_items(draft.get("required") or [], slug, "required")
        if not required:
            raise ValidationProblem(
                f"The proposed task {slug!r} has no required items. Every task needs at "
                "least one — a task's duration is the combined duration of its required "
                "items, so a task with none has no size. Put optional deep-dive material "
                "in `optional[]` instead."
            )
        optional = _validate_items(draft.get("optional") or [], slug, "optional")
        _assert_disjoint(required, optional, slug)

        prerequisites = [
            str(p)
            for p in (draft.get("prerequisite_tasks") or draft.get("prerequisiteTasks") or [])
        ]
        if slug in prerequisites:
            raise ValidationProblem(f"{slug!r} lists itself as its own prerequisite.")

        tasks.append(
            ProposedTask(
                slug=slug,
                title=title,
                description=str(draft.get("description") or ""),
                required=required,
                optional=optional,
                prerequisite_tasks=prerequisites,
            )
        )

    for task in tasks:
        unknown = [p for p in task.prerequisite_tasks if p not in slugs]
        if unknown:
            raise ValidationProblem(
                f"{task.slug!r} lists {unknown[0]!r} as a prerequisite, but no proposed "
                "task has that slug."
            )
    return tasks


def _validate_items(
    drafts: list[dict[str, Any]], task_slug: str, label: str
) -> list[ProposedItem]:
    if len(drafts) > MAX_ITEMS_PER_LIST:
        raise ValidationProblem(
            f"{task_slug!r} has {len(drafts)} {label} items, more than one task can carry "
            f"({MAX_ITEMS_PER_LIST} at most). Split it into more tasks instead."
        )
    items: list[ProposedItem] = []
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
            raise ValidationProblem(
                f"The required item {draft.get('title')!r} on {task_slug!r} has no `why`. "
                "Every required item needs one line, addressed to the learner, saying what "
                "it gives them — it is what they will see on their checklist."
            )
        minutes = draft.get("minutes")
        if not isinstance(minutes, int) or minutes < 1:
            raise ValidationProblem(
                f"The item {draft.get('title')!r} on {task_slug!r} needs `minutes` as a "
                "whole number of minutes."
            )
        items.append(
            ProposedItem(
                kind=parsed_kind,
                title=str(draft.get("title") or "").strip() or "Untitled",
                url=str(draft.get("url") or "") or None,
                minutes=minutes,
                why=why,
                details=str(draft.get("details") or ""),
                source=draft.get("source") or "web",
                guided=draft.get("guided"),
            )
        )
    return items


def _assert_disjoint(
    required: list[ProposedItem], optional: list[ProposedItem], task_slug: str
) -> None:
    keys = {(item.title, item.url) for item in required}
    duplicated = [item.title for item in optional if (item.title, item.url) in keys]
    if duplicated:
        raise ValidationProblem(
            f"{duplicated[0]!r} is in both the required and the optional list on "
            f"{task_slug!r}. An item is either something the learner has to do for this "
            "task or something they may do if they want more; it cannot be both."
        )


# --- plan validation ---------------------------------------------------------------------


def _validate_plan(drafts: list[dict[str, Any]], slugs: set[str]) -> list[PlanTaskEntry]:
    entries: list[PlanTaskEntry] = []
    seen: set[str] = set()
    for draft in drafts:
        task_slug = str(draft.get("task_slug") or draft.get("taskSlug") or "").strip()
        if task_slug not in slugs:
            raise ValidationProblem(
                f"The plan names {task_slug!r}, which is not one of the proposed tasks."
            )
        if task_slug in seen:
            raise ValidationProblem(f"{task_slug!r} appears more than once in the plan.")
        seen.add(task_slug)

        decision = str(draft.get("decision") or "").strip()
        if decision not in _DECISIONS:
            allowed = ", ".join(sorted(_DECISIONS))
            raise ValidationProblem(
                f"{decision!r} is not a plan decision. Use one of: {allowed}."
            )

        relevance = draft.get("relevance")
        if not isinstance(relevance, int) or not (0 <= relevance <= 4):
            raise ValidationProblem(
                f"The plan entry for {task_slug!r} needs `relevance` as a whole number "
                "from 0 to 4."
            )

        after_raw = draft.get("after")
        after = str(after_raw).strip() if after_raw else None
        if after is not None and after not in slugs:
            raise ValidationProblem(
                f"The plan entry for {task_slug!r} names {after!r} as `after`, which is "
                "not one of the proposed tasks."
            )

        prerequisites = [
            str(p)
            for p in (draft.get("prerequisite_tasks") or draft.get("prerequisiteTasks") or [])
        ]
        unknown = [p for p in prerequisites if p not in slugs]
        if unknown:
            raise ValidationProblem(
                f"The plan entry for {task_slug!r} names {unknown[0]!r} as a prerequisite, "
                "which is not one of the proposed tasks."
            )

        why = str(draft.get("why") or "").strip()
        if not why:
            raise ValidationProblem(
                f"The plan entry for {task_slug!r} has no `why`. Every task needs one line "
                "explaining the decision, including an excluded or rejected one."
            )

        entries.append(
            PlanTaskEntry(
                task_slug=task_slug,
                after=after,
                prerequisite_tasks=prerequisites,
                relevance=relevance,
                decision=decision,  # type: ignore[arg-type]
                why=why,
            )
        )

    missing = slugs - seen
    if missing:
        raise ValidationProblem(
            f"The plan has no entry for {sorted(missing)[0]!r}. Every proposed task needs "
            "a decision, even an excluded or rejected one."
        )
    return entries


# --- materialization ordering and projection ----------------------------------------------


def _topological_order(entries: list[PlanTaskEntry]) -> list[PlanTaskEntry]:
    """`entries` (already filtered to the decisions being materialized), ordered so that
    every `prerequisite_tasks` and `after` dependency comes before its dependent.

    `after` is treated as an ordering dependency exactly like `prerequisite_tasks` — a
    task named in *either* has to be materialized first — rather than as a weaker hint,
    because a weaker hint that this function then silently ignores under contention would
    be a worse surprise than the stronger, predictable rule. Ties among tasks with no
    outstanding dependency are broken by the plan's own array order, so a plan with no
    dependencies at all materializes in exactly the order it was written.

    Raises:
        ValidationProblem: the dependency graph has a cycle — named by two of the tasks
            in it, rather than looping or raising an opaque error.
    """
    by_slug = {entry.task_slug: entry for entry in entries}
    slugs = list(by_slug)
    order_index = {slug: index for index, slug in enumerate(slugs)}
    indegree = dict.fromkeys(slugs, 0)
    children: dict[str, list[str]] = {slug: [] for slug in slugs}
    for entry in entries:
        deps = set(entry.prerequisite_tasks)
        if entry.after:
            deps.add(entry.after)
        for dep in deps:
            if dep in by_slug and dep != entry.task_slug:
                children[dep].append(entry.task_slug)
                indegree[entry.task_slug] += 1

    remaining = set(slugs)
    result: list[str] = []
    while remaining:
        ready = sorted((s for s in remaining if indegree[s] == 0), key=lambda s: order_index[s])
        if not ready:
            a, b = sorted(remaining, key=lambda s: order_index[s])[:2]
            raise ValidationProblem(
                f"{a!r} and {b!r} have a prerequisite cycle between them, so there is no "
                "order to materialize them in."
            )
        chosen = ready[0]
        result.append(chosen)
        remaining.discard(chosen)
        for child in children[chosen]:
            indegree[child] -= 1
    return [by_slug[slug] for slug in result]


def _as_checklist_draft(item: ProposedItem) -> dict[str, Any]:
    """A proposed item as the checklist entry it becomes — the same projection
    `reports.as_checklist_item` applies to a `ReportItem`, adapted for a `ProposedItem`,
    which has no `item_id` yet (`TaskService.add_items` mints one) and resolves `guided`
    itself rather than through `ReportItem.is_guided`.
    """
    details = item.details.strip()
    if not details:
        details = f"{item.title} — {item.url}" if item.url else item.title
    guided = item.kind in GUIDED_KINDS if item.guided is None else item.guided
    return {
        "shortDescription": item.why or item.title,
        "details": details,
        "kind": item.kind,
        "guided": guided,
        "minutes": item.minutes,
        "url": item.url,
    }


__all__ = [
    "DEFAULT_MATERIALIZE_DECISIONS",
    "MAX_PROPOSED_TASKS",
    "StudyPlanService",
]
