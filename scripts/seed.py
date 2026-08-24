#!/usr/bin/env python
"""Seed a demo user, project, and eight tasks into the local Firestore emulator.

`./scripts/dev.sh seed`. The shapes here are the ones the board renders, so the seed
doubles as a check that a fresh database produces a sensible-looking first screen: a
next-up task, a parent with subtasks and a rollup, and a couple of the less common states.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api" / "src"))

from coach.api.deps import Container  # noqa: E402
from coach.core.config import Settings  # noqa: E402
from coach.core.principal import Principal  # noqa: E402
from coach.repositories.plans import FREE_TIER  # noqa: E402
from coach.services.models import PlanLimits, TaskState  # noqa: E402

DEV_UID = os.environ.get("DEV_UID", "u_dev")

TASKS: list[tuple[str, int, str]] = [
    ("Read the structured concurrency proposal", 45, "current"),
    ("Write a toy task group by hand", 90, "not_started"),
    ("Compare cancellation semantics across runtimes", 60, "not_started"),
    ("Benchmark the toy against asyncio.gather", 45, "not_started"),
    ("Write up what surprised you", 30, "not_started"),
    ("Skim the Trio nursery docs", 25, "postponed"),
]

SUBTASKS: list[tuple[str, int]] = [
    ("Sketch the API on paper", 30),
    ("Implement the happy path", 60),
    ("Handle cancellation", 60),
]


async def main() -> int:
    settings = Settings()
    if not settings.is_local:
        raise SystemExit(
            f"Refusing to seed with ENV={settings.env!r}. This writes demo data and is "
            "for the local emulator only."
        )
    if not settings.firestore_emulator_host:
        raise SystemExit(
            "FIRESTORE_EMULATOR_HOST is unset. Run this through ./scripts/dev.sh seed, "
            "which starts the emulator and points at it."
        )

    container = Container(settings)
    principal = Principal(uid=DEV_UID, email=f"{DEV_UID}@localhost.dev", source="dev")

    # M8-quotas: written before the dev user, so `get_or_create` copies this preset onto
    # it rather than falling back to `PlanLimits`'s Python defaults — a fresh emulator
    # otherwise behaves identically either way, but seeding it here makes `plans/free`
    # visible in the emulator UI for anyone poking at the collection by hand.
    await container.plan_repository.set_preset(FREE_TIER, PlanLimits())

    await container.users.get_or_create(principal)
    await container.users.patch_global_prefs(principal, {"defaultTaskMinutes": 45})

    project = await container.projects.create(
        principal,
        title="Learn structured concurrency",
        goal="Ship a worker pool I actually trust under cancellation.",
    )
    # The brief's example, seeded so it is visible on the first screen: 45 minutes
    # globally, two hours in this project.
    await container.projects.patch(
        principal, project.id, prefs={"defaultTaskMinutes": 120}
    )

    created = []
    for title, minutes, state in TASKS:
        task = await container.tasks.create_task(
            principal, project.id, title=title, estimated_minutes=minutes
        )
        created.append((task, state))

    # A parent with subtasks, so the board shows a rollup and a progress ring.
    parent = await container.tasks.create_task(
        principal,
        project.id,
        title="Build the worker pool",
        estimated_minutes=150,
    )
    split = await container.tasks.split_task(
        principal,
        parent.id,
        [{"title": title, "estimatedMinutes": minutes} for title, minutes in SUBTASKS],
    )
    first_subtask = split.subtasks[0]
    await container.tasks.set_state(principal, first_subtask.id, TaskState.CURRENT)
    await container.tasks.set_state(principal, first_subtask.id, TaskState.COMPLETED)

    # Apply the non-default states last, so the single-`current` invariant settles where
    # this script intends rather than wherever the loop happened to leave it.
    for task, state in created:
        if state == "current":
            await container.tasks.set_state(principal, task.id, TaskState.CURRENT)
        elif state == "postponed":
            await container.tasks.set_state(principal, task.id, TaskState.CURRENT)
            await container.tasks.set_state(principal, task.id, TaskState.POSTPONED)

    board = await container.tasks.list_board(principal, project.id)
    print(f"Seeded {DEV_UID}: project {project.id!r} with {len(board)} top-level tasks.")
    print(f"Open http://127.0.0.1:5173/projects/{project.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
