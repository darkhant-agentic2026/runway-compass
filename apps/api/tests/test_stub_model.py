"""The stubbed model itself.

`MODEL_BACKEND=stub` is one of the three test-only surfaces whose failure mode is *silent
success* (CLAUDE.md, docs/07-infra-deploy.md), so it gets tests of its own rather than
being trusted because the suites that use it pass. Two things are worth pinning:

- **It reads the budget out of the rendered instruction.** That coupling is what makes
  golden flow #7 evidence about the prompt rather than about the test's own arithmetic.
  Asserted against a real `render_prefs` output, so a change to the wording that breaks
  the parse fails here and not in a Playwright timeout.
- **Its tool loop terminates.** The stub plans from this turn's function responses, and
  the failure mode of getting that wrong is a turn that never completes — which surfaces
  as a hung test with no message about why.
"""

from __future__ import annotations

from typing import Any

import pytest

from coach.agents.prompt import render_prefs
from coach.integrations.stub_model import (
    DEFAULT_BUDGET_MINUTES,
    MARKDOWN_REPLY,
    _prose_reply,
    budget_minutes,
    requested_minutes,
    split_sizes,
    stub_reply,
)
from coach.services.models import EffectivePrefs


def _prefs(minutes: int) -> EffectivePrefs:
    return EffectivePrefs(
        default_task_minutes=minutes,
        guidance_style="socratic",
        verbosity="balanced",
        timezone="UTC",
        research_depth="standard",
        allow_videos=True,
        preferred_sources=[],
        avoid_sources=[],
    )


@pytest.mark.parametrize("minutes", [15, 45, 120, 480])
def test_the_budget_survives_the_round_trip_through_the_prompt(minutes: int) -> None:
    """`render_prefs` writes it; the stub reads it back. One test owns both ends."""
    assert budget_minutes(render_prefs(_prefs(minutes))) == minutes


def test_an_instruction_without_a_budget_falls_back_to_the_global_default() -> None:
    assert budget_minutes("nothing useful here") == DEFAULT_BUDGET_MINUTES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("about 4 hours of work", 240),
        ("2h a week", 120),
        ("90 minutes", 90),
        ("give me 30 mins", 30),
        ("I want to learn Rust", None),
        ("chapter 3 of the book", None),
    ],
)
def test_a_named_duration_is_what_makes_the_stub_plan(text: str, expected: int | None) -> None:
    """No duration means no tool call, which is what keeps flow #1's first turn a question.

    "chapter 3" is the case worth having: a bare number is not a duration, and a stub that
    read one would add a three-minute task to every conversation about a book.
    """
    assert requested_minutes(text) == expected


@pytest.mark.parametrize(
    ("minutes", "budget"), [(135, 45), (240, 120), (90, 45), (100, 45), (600, 45)]
)
def test_a_split_plan_fits_the_budget_and_sums_to_the_parent(minutes: int, budget: int) -> None:
    sizes = split_sizes(minutes, budget)

    assert 2 <= len(sizes) <= 8
    assert sum(sizes) == minutes
    if minutes <= 8 * budget:
        # Beyond eight subtasks the stub's own cap binds and pieces are necessarily
        # larger; `add_task`'s guard is what keeps a parent from getting there.
        assert all(size <= budget for size in sizes)


async def test_the_tool_loop_ends_after_the_subtasks(container, client: Any) -> None:
    """One `add_task`, then one `add_subtask` per pass, then prose.

    The termination argument is the interesting part and it changed when `split_task` was
    removed: the stub used to stop because it saw a `split_task` response, and now it stops
    because it *counts* the `add_subtask` responses in this turn against the sizes it
    planned. A miscount is an infinite tool loop, which is why this is driven through the
    real runner — what could hang is the interaction between the stub's planning and ADK's
    function-response contents, not the planner in isolation.
    """
    import asyncio

    from coach.integrations.stub_model import StubModel

    container.runners.set_model(StubModel())
    project = (await client.post("/api/projects", json={"title": "Loop"})).json()
    session_id = (await client.post(f"/api/projects/{project['id']}/session")).json()["id"]

    started = await client.post(
        f"/api/sessions/{session_id}/turns", json={"text": "roughly 3 hours of work"}
    )
    turn_id = started.json()["turnId"]
    async with asyncio.timeout(20):
        while (await client.get(f"/api/turns/{turn_id}")).json()["status"] == "running":
            await asyncio.sleep(0.02)

    events = (await client.get(f"/api/sessions/{session_id}/events?limit=100")).json()
    calls = [
        part["function_call"]["name"]
        for stored in events["events"]
        for part in (stored["event"].get("content") or {}).get("parts", [])
        if "function_call" in part
    ]
    # 180 minutes clamped to 135 by `add_task`'s 3x guard, then three 45-minute subtasks.
    assert calls == ["add_task", "add_subtask", "add_subtask", "add_subtask"]


def _request(text: str) -> Any:
    """The smallest thing `_prose_reply` reads: a request with one user message."""
    from types import SimpleNamespace

    from google.genai import types

    return SimpleNamespace(contents=[types.Content(role="user", parts=[types.Part(text=text)])])


def test_only_one_prompt_switches_the_reply_to_markdown() -> None:
    """The markdown sample is opt-in, and every other prompt still gets prose.

    The e2e that renders tables, equations, code, and a diagram needs the stub to emit
    them; every flow written before it asserts `stub_reply` character for character. So
    what is pinned here is the *boundary*: one phrase crosses it, and the phrasings that
    surround the other flows do not.
    """
    assert _prose_reply(_request("show me the formatting")) == MARKDOWN_REPLY
    assert "| Step | Minutes |" in MARKDOWN_REPLY
    assert "```mermaid" in MARKDOWN_REPLY

    for prompt in ["roughly 3 hours of work", "discard the first one", "formatting"]:
        assert _prose_reply(_request(prompt)) == stub_reply(prompt)


def test_the_stub_can_be_made_to_fail() -> None:
    """The `turn_error` path needs to be reachable, or nothing exercises it.

    A stub that always succeeds makes the whole error half of the UI untestable end to
    end, which is how a failed turn came to shadow every later turn in its session for a
    milestone. Guarded by an exact phrase so no ordinary prompt trips it.
    """
    from coach.integrations.stub_model import _FAILURE_PATTERN

    assert _FAILURE_PATTERN.search("please make this turn fail")
    assert _FAILURE_PATTERN.search("Make This Turn Fail")
    assert not _FAILURE_PATTERN.search("what happens when a turn fails?")
    assert not _FAILURE_PATTERN.search("this task will make me fail my exam")
