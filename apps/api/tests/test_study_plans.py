"""`StudyPlanService`: `post_plan`'s validation and `materialize`'s board write.

docs/03-agent-design.md#the-research-pipeline-since-m9. `post_plan` is what
`write_study_plan` calls — `plan_tailor`'s one write — so its guards are tested the same
way `test_research.py` tests `ReportService.post_report`'s: as a pure service call, one
guard per test. `materialize` is what the standalone, not-yet-wired
`materialize_study_plan` tool calls — tested here directly against the service, since no
agent's catalogue reaches it yet.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from coach.core.errors import NotFound, ValidationProblem
from coach.core.ids import run_id as new_run_id
from coach.services.models import Origin, ReportItemKind, TaskState


def _item(*, why: str = "so you know what to do", minutes: int = 10) -> dict[str, Any]:
    return {
        "kind": "article",
        "title": "A guide",
        "url": "https://example.com/guide",
        "minutes": minutes,
        "why": why,
        "source": "web",
    }


def _task_draft(slug: str, *, prerequisite_tasks: list[str] | None = None) -> dict[str, Any]:
    return {
        "slug": slug,
        "title": f"Task {slug}",
        "description": "What done looks like.",
        "required": [_item()],
        "optional": [],
        "prerequisite_tasks": prerequisite_tasks or [],
    }


def _plan_entry(
    slug: str,
    *,
    decision: str = "include",
    after: str | None = None,
    prerequisite_tasks: list[str] | None = None,
    relevance: int = 3,
) -> dict[str, Any]:
    return {
        "task_slug": slug,
        "after": after,
        "prerequisite_tasks": prerequisite_tasks or [],
        "relevance": relevance,
        "decision": decision,
        "why": "Because the learner needs it — or doesn't.",
    }


async def _project(client: httpx.AsyncClient) -> str:
    created = await client.post("/api/projects", json={"title": "Become a data engineer"})
    return str(created.json()["id"])


# --- post_plan validation -----------------------------------------------------------------


async def test_post_plan_stores_proposed_tasks_and_plan(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    plan = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A roadmap",
        short_description="Two sentences.",
        long_description="A longer write-up.",
        memo="How the research was grouped.",
        proposed_tasks=[
            _task_draft("intro"),
            _task_draft("advanced", prerequisite_tasks=["intro"]),
        ],
        plan=[
            _plan_entry("intro"),
            _plan_entry("advanced", after="intro", prerequisite_tasks=["intro"]),
        ],
    )
    assert plan.project_id == project_id
    assert plan.owner_uid == alice.uid
    assert [t.slug for t in plan.proposed_tasks] == ["intro", "advanced"]
    assert [e.task_slug for e in plan.plan] == ["intro", "advanced"]
    assert plan.materialized_at is None

    fetched = await container.study_plans.get(project_id, plan.id)
    assert fetched is not None
    assert fetched.title == "A roadmap"


async def test_post_plan_rejects_no_tasks(container, alice, client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    with pytest.raises(ValidationProblem, match="at least one proposed task"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Empty",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[],
            plan=[],
        )


async def test_post_plan_rejects_a_duplicate_slug(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    with pytest.raises(ValidationProblem, match="more than one proposed task"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Dup",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[_task_draft("intro"), _task_draft("intro")],
            plan=[_plan_entry("intro")],
        )


async def test_post_plan_rejects_an_unknown_item_kind(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    bad = _task_draft("intro")
    bad["required"][0]["kind"] = "podcast"
    with pytest.raises(ValidationProblem, match="not a kind of material"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Bad kind",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[bad],
            plan=[_plan_entry("intro")],
        )


async def test_post_plan_rejects_a_required_item_with_no_why(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    bad = _task_draft("intro")
    bad["required"][0]["why"] = ""
    with pytest.raises(ValidationProblem, match="no `why`"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="No reason",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[bad],
            plan=[_plan_entry("intro")],
        )


async def test_post_plan_rejects_a_task_with_no_required_items(
    container, alice, client: httpx.AsyncClient
) -> None:
    """A task's duration is the combined duration of its `required[]` items
    (`TASK_PROPOSER_INSTRUCTION`), so a task with none has no size — and, before this
    guard, `task_proposer` occasionally produced exactly that: a task carrying only
    `optional[]` material, or `required[]` omitted entirely."""
    project_id = await _project(client)
    bad = _task_draft("intro")
    bad["required"] = []
    bad["optional"] = [_item()]
    with pytest.raises(ValidationProblem, match="no required items"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="No required items",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[bad],
            plan=[_plan_entry("intro")],
        )


async def test_post_plan_rejects_a_task_missing_the_required_key(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    bad = _task_draft("intro")
    del bad["required"]
    with pytest.raises(ValidationProblem, match="no required items"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Missing required",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[bad],
            plan=[_plan_entry("intro")],
        )


async def test_post_plan_rejects_a_prerequisite_naming_an_unknown_slug(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    with pytest.raises(ValidationProblem, match="no proposed task has that slug"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Bad prereq",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[_task_draft("intro", prerequisite_tasks=["nope"])],
            plan=[_plan_entry("intro")],
        )


async def test_post_plan_rejects_a_plan_missing_a_task(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    with pytest.raises(ValidationProblem, match="no entry for"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Missing decision",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[_task_draft("intro"), _task_draft("advanced")],
            plan=[_plan_entry("intro")],
        )


async def test_post_plan_rejects_an_invalid_decision(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    with pytest.raises(ValidationProblem, match="not a plan decision"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Bad decision",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[_task_draft("intro")],
            plan=[_plan_entry("intro", decision="maybe")],
        )


async def test_post_plan_rejects_relevance_out_of_range(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    with pytest.raises(ValidationProblem, match="0 to 4"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="Bad relevance",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[_task_draft("intro")],
            plan=[_plan_entry("intro", relevance=9)],
        )


async def test_post_plan_rejects_a_plan_entry_with_no_why(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    entry = _plan_entry("intro")
    entry["why"] = ""
    with pytest.raises(ValidationProblem, match="no `why`"):
        await container.study_plans.post_plan(
            alice,
            project_id=project_id,
            title="No reason for the decision",
            short_description="",
            long_description="",
            memo="",
            proposed_tasks=[_task_draft("intro")],
            plan=[entry],
        )


# --- materialize --------------------------------------------------------------------------


async def test_materialize_creates_only_include_and_additional_tasks_in_dependency_order(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    run_id = new_run_id()
    plan = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A roadmap",
        short_description="",
        long_description="",
        memo="",
        run_id=run_id,
        proposed_tasks=[
            _task_draft("intro"),
            _task_draft("advanced", prerequisite_tasks=["intro"]),
            _task_draft("skip-me"),
            _task_draft("reject-me"),
        ],
        plan=[
            _plan_entry("intro", decision="include"),
            _plan_entry(
                "advanced", decision="additional", after="intro", prerequisite_tasks=["intro"]
            ),
            _plan_entry("skip-me", decision="exclude"),
            _plan_entry("reject-me", decision="reject"),
        ],
    )

    created = await container.study_plans.materialize(
        alice, project_id=project_id, plan_id=plan.id
    )

    assert [task.title for task in created] == ["Task intro", "Task advanced"]
    assert all(task.origin is Origin.AGENT for task in created)
    assert all(task.needs_research is False for task in created)
    # `required[]` promoted into a real checklist, the same projection a research report
    # uses — `kind` included, so the checklist item can show the same kind chip the
    # proposed item did.
    assert [item.short_description for item in created[0].items] == ["so you know what to do"]
    assert [item.kind for item in created[0].items] == [ReportItemKind.ARTICLE]
    assert created[0].state is TaskState.NOT_STARTED
    # `optional[]` is never promoted into `items[]` (same rule `post_research_report`
    # follows), but the task keeps a pointer back to the plan's own run so its optional
    # material stays reachable — the task workspace's "View roadmap" card.
    assert all(task.study_plan_run_id == plan.run_id for task in created)

    board = (await client.get(f"/api/projects/{project_id}/tasks")).json()["tasks"]
    assert {task["title"] for task in board} == {"Task intro", "Task advanced"}

    reloaded = await container.study_plans.get(project_id, plan.id)
    assert reloaded is not None
    assert reloaded.materialized_at is not None
    assert reloaded.materialized_task_ids == [task.id for task in created]


async def test_materialize_is_idempotent(container, alice, client: httpx.AsyncClient) -> None:
    project_id = await _project(client)
    plan = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A roadmap",
        short_description="",
        long_description="",
        memo="",
        proposed_tasks=[_task_draft("intro")],
        plan=[_plan_entry("intro")],
    )

    first = await container.study_plans.materialize(
        alice, project_id=project_id, plan_id=plan.id
    )
    second = await container.study_plans.materialize(
        alice, project_id=project_id, plan_id=plan.id
    )
    assert [task.id for task in first] == [task.id for task in second]

    board = (await client.get(f"/api/projects/{project_id}/tasks")).json()["tasks"]
    assert len(board) == 1


async def test_reset_materialization_lets_materialize_rebuild_the_board(
    container, alice, client: httpx.AsyncClient
) -> None:
    """The troubleshooting pair: `POST .../troubleshooting/delete-all-tasks` wipes the
    board and calls this, so a learner can recreate a materialized plan's tasks without
    paying for the roadmap workflow again — the same `StudyPlan.proposedTasks` a fresh
    `materialize` call reuses.
    """
    project_id = await _project(client)
    run_id = new_run_id()
    plan = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A roadmap",
        short_description="",
        long_description="",
        memo="",
        run_id=run_id,
        proposed_tasks=[_task_draft("intro")],
        plan=[_plan_entry("intro")],
    )
    first = await container.study_plans.materialize(
        alice, project_id=project_id, plan_id=plan.id
    )
    assert len(first) == 1

    reset_count = await container.study_plans.reset_materialization(project_id)
    assert reset_count == 1

    reloaded = await container.study_plans.get(project_id, plan.id)
    assert reloaded is not None
    assert reloaded.materialized_at is None
    assert reloaded.materialized_task_ids == []

    # Deleting the board out from under the plan is exactly what the troubleshooting
    # action does before this — `materialize` no longer has the old ids to resolve, so
    # this is the case the idempotency guard would otherwise return nothing for.
    await container.tasks.delete_all_tasks(alice, project_id)
    second = await container.study_plans.materialize(
        alice, project_id=project_id, plan_id=plan.id
    )
    assert len(second) == 1
    assert second[0].id != first[0].id
    assert second[0].title == first[0].title


async def test_reset_materialization_leaves_an_unmaterialized_plan_alone(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    plan = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A roadmap",
        short_description="",
        long_description="",
        memo="",
        proposed_tasks=[_task_draft("intro")],
        plan=[_plan_entry("intro")],
    )

    assert await container.study_plans.reset_materialization(project_id) == 0
    reloaded = await container.study_plans.get(project_id, plan.id)
    assert reloaded is not None
    assert reloaded.materialized_at is None


async def test_materialize_detects_a_prerequisite_cycle(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    plan = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A cyclic roadmap",
        short_description="",
        long_description="",
        memo="",
        proposed_tasks=[
            _task_draft("a", prerequisite_tasks=["b"]),
            _task_draft("b", prerequisite_tasks=["a"]),
        ],
        plan=[
            _plan_entry("a", prerequisite_tasks=["b"]),
            _plan_entry("b", prerequisite_tasks=["a"]),
        ],
    )
    with pytest.raises(ValidationProblem, match="prerequisite cycle"):
        await container.study_plans.materialize(alice, project_id=project_id, plan_id=plan.id)


async def test_materialize_refuses_an_unknown_plan(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    with pytest.raises(NotFound):
        await container.study_plans.materialize(
            alice, project_id=project_id, plan_id="sp_nonexistent"
        )


# --- get_latest -----------------------------------------------------------------------


async def test_get_latest_returns_none_with_no_plan(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    assert await container.study_plans.get_latest(alice, project_id) is None


async def test_get_latest_returns_the_most_recently_written_plan(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    first = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="First",
        short_description="",
        long_description="",
        memo="",
        proposed_tasks=[_task_draft("intro")],
        plan=[_plan_entry("intro")],
    )
    second = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="Second",
        short_description="",
        long_description="",
        memo="",
        proposed_tasks=[_task_draft("intro")],
        plan=[_plan_entry("intro")],
    )
    latest = await container.study_plans.get_latest(alice, project_id)
    assert latest is not None
    assert latest.id == second.id
    assert first.id != second.id


# --- revise ---------------------------------------------------------------------------


async def test_revise_writes_a_new_plan_and_leaves_the_original_untouched(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    original = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A roadmap",
        short_description="",
        long_description="",
        memo="",
        proposed_tasks=[_task_draft("intro"), _task_draft("advanced")],
        plan=[
            _plan_entry("intro", decision="include"),
            _plan_entry("advanced", decision="exclude"),
        ],
    )

    revised = await container.study_plans.revise(
        alice,
        project_id=project_id,
        plan_id=original.id,
        plan=[
            _plan_entry("intro", decision="include"),
            _plan_entry("advanced", decision="include", after="intro"),
        ],
    )

    assert revised.id != original.id
    assert revised.revised_from_plan_id == original.id
    assert [t.slug for t in revised.proposed_tasks] == ["intro", "advanced"]
    assert {e.task_slug: e.decision for e in revised.plan} == {
        "intro": "include",
        "advanced": "include",
    }

    reloaded_original = await container.study_plans.get(project_id, original.id)
    assert reloaded_original is not None
    assert {e.task_slug: e.decision for e in reloaded_original.plan} == {
        "intro": "include",
        "advanced": "exclude",
    }

    latest = await container.study_plans.get_latest(alice, project_id)
    assert latest is not None
    assert latest.id == revised.id


async def test_revise_refuses_an_already_materialized_plan(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    plan = await container.study_plans.post_plan(
        alice,
        project_id=project_id,
        title="A roadmap",
        short_description="",
        long_description="",
        memo="",
        proposed_tasks=[_task_draft("intro")],
        plan=[_plan_entry("intro")],
    )
    await container.study_plans.materialize(alice, project_id=project_id, plan_id=plan.id)

    with pytest.raises(ValidationProblem, match="already been materialized"):
        await container.study_plans.revise(
            alice,
            project_id=project_id,
            plan_id=plan.id,
            plan=[_plan_entry("intro", decision="exclude")],
        )


async def test_revise_refuses_an_unknown_plan(
    container, alice, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client)
    with pytest.raises(NotFound):
        await container.study_plans.revise(
            alice, project_id=project_id, plan_id="sp_nonexistent", plan=[]
        )
