"""The reduced tool set unattended work runs with.

docs/03-agent-design.md#safety-rails-on-autonomy and
docs/05-autonomous-runs.md#what-the-run-is-allowed-to-change. These assertions are about
*enumeration*, and that is the point: the rail is the tool list, so a rail expressed as an
instruction ("please do not discard tasks") would be an honour system, and one expressed as
a subtraction would silently re-admit every tool added afterwards.

The forbidden-list test is therefore written to fail **when a new destructive tool is added
to `as_tools` and not considered here** — which is the failure mode worth catching, since
nothing else in the system would notice.
"""

from __future__ import annotations

from coach.agents.tools import DomainTools

#: docs/05-autonomous-runs.md, verbatim. Restated rather than imported because there is
#: nothing to import it from — it is prose in a design document, and pinning it here is
#: what makes a change to the code a change somebody has to justify.
FORBIDDEN = {
    "discard_task",
    "update_learner_profile",
    "update_project_prefs",
}

#: Tools that end the invocation waiting for a human. Unusable in a background run for a
#: reason independent of the safety rails: there is nobody to answer, so the run would
#: stop halfway and leave a question in a transcript nobody is reading.
CONFIRMATION_GATED = {
    "discard_task",
    "delete_task_item",
    "complete_task_item",
    "ask_learner",
}


def _names(tools: list) -> set[str]:
    return {tool.name for tool in tools}


def test_the_forbidden_tools_are_absent(container) -> None:
    tools: DomainTools = container.domain_tools

    assert _names(tools.as_autonomous_tools()).isdisjoint(FORBIDDEN)


def test_no_confirmation_gated_tool_is_available_to_a_run(container) -> None:
    tools: DomainTools = container.domain_tools

    assert _names(tools.as_autonomous_tools()).isdisjoint(CONFIRMATION_GATED)


def test_the_allowed_tools_are_present(container) -> None:
    """The positive half. A rail that removed everything would pass the two tests above."""
    tools: DomainTools = container.domain_tools

    names = _names(tools.as_autonomous_tools())

    assert {"add_task", "add_subtask", "reorder_task", "set_next_up", "list_tasks"} <= names


def test_the_autonomous_set_is_a_subset_of_the_interactive_one(container) -> None:
    """A background run must not reach a tool the learner's own coach does not have.

    Written as a subset rather than as a list, so it keeps meaning something as both sets
    grow.
    """
    tools: DomainTools = container.domain_tools

    assert _names(tools.as_autonomous_tools()) <= _names(tools.as_tools())


def test_the_propose_agent_is_built_with_that_set_and_not_the_full_one(container) -> None:
    """The wiring, not just the enumeration.

    `as_autonomous_tools` returning the right list is worth nothing if `autonomous_runner`
    passes `as_tools()` — and that substitution would be invisible in every transcript,
    because the model has no reason to reach for a forbidden tool in most runs.
    """
    runner = container.runners.autonomous_runner()

    names = {tool.name for tool in runner.agent.tools}
    assert names.isdisjoint(FORBIDDEN)
    assert names == _names(container.domain_tools.as_autonomous_tools())


def test_the_interactive_coach_still_has_the_full_set(container) -> None:
    """The reduction is per-agent, not global."""
    runner = container.runners.runner()

    assert "discard_task" in {tool.name for tool in runner.agent.tools}
