"""`ModelThrottle`, `_carry_attachments_into_subtopics`, and the `research_workflow` graph
itself.

docs/03-agent-design.md#the-research-pipeline-since-m9. Three things worth testing at this
altitude, below a full turn:

- **The throttle never lets two calls for the same run overlap**, instrumented directly
  rather than by timing a real turn — the exit criterion
  (docs/09-roadmap.md#m9--reworking-the-autonomous-research-workflow) asks for exactly
  this: "asserted to never have more than one model call in flight at once by
  instrumenting the throttle itself, not by timing it."
- **`_carry_attachments_into_subtopics` threads the run's own attachments onto every
  sub-topic**, the decision `docs/09-roadmap.md#m10-beta--agent-consistency-projecttask-ui-
  polish-and-readme` asks the M10 review to confirm — pinned directly against the
  function rather than against a full turn's persisted events, since the branch's own
  input event is session-local (built only to feed the LLM request,
  `workflow/_llm_agent_wrapper.py::prepare_llm_agent_input`) and never itself a persisted
  Firestore event.
- **The graph builds** against real `LlmAgent`/`Workflow` construction — `output_schema`,
  `parallel_worker`, and the `node()` wrapping are all new surface for this project
  (docs/03-agent-design.md#the-research-pipeline-since-m9), and a signature or semantics
  change on any of them fails here first, at construction time, rather than inside a
  detached generation task.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from google.adk.workflow import Workflow
from google.genai import types

from coach.agents.context import RUN_ID_KEY
from coach.agents.research_tools import ResearchTools
from coach.agents.research_workflow import (
    PLAN_TAILOR_NAME,
    RESEARCH_PLANNER_NAME,
    REVIEWER_WRITER_NAME,
    ROADMAP_WORKFLOW_NAME,
    TASK_PROPOSER_NAME,
    TOPIC_RESEARCHER_BRIEF_NAME,
    TOPIC_RESEARCHER_NAME,
    ModelThrottle,
    _carry_attachments_into_subtopics,
    build_research_workflow,
    build_roadmap_workflow,
)
from coach.ws.hub import BoardUpdateHub


class _FakeCallbackContext:
    def __init__(self, run_id: str) -> None:
        self.state = {RUN_ID_KEY: run_id}


async def test_the_throttle_never_lets_two_calls_for_one_run_overlap() -> None:
    """5 concurrent `before_model` callers for the same run; at most 1 "in flight" at once."""
    throttle = ModelThrottle()
    ctx = _FakeCallbackContext("r_1")
    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def one_call() -> None:
        nonlocal concurrent, max_concurrent
        await throttle.before_model(cast(Any, ctx), cast(Any, None))
        async with lock:
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.01)
        async with lock:
            concurrent -= 1
        await throttle.after_model(cast(Any, ctx), cast(Any, None))

    await asyncio.gather(*(one_call() for _ in range(5)))
    assert max_concurrent == 1


async def test_the_throttle_does_not_serialize_across_different_runs() -> None:
    """Two runs' semaphores are independent — a burst in one job must not stall another."""
    throttle = ModelThrottle()
    concurrent = 0
    max_concurrent = 0
    lock = asyncio.Lock()

    async def one_call(run_id: str) -> None:
        nonlocal concurrent, max_concurrent
        ctx = _FakeCallbackContext(run_id)
        await throttle.before_model(cast(Any, ctx), cast(Any, None))
        async with lock:
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
        await asyncio.sleep(0.01)
        async with lock:
            concurrent -= 1
        await throttle.after_model(cast(Any, ctx), cast(Any, None))

    await asyncio.gather(one_call("r_1"), one_call("r_2"))
    assert max_concurrent == 2


async def test_on_model_error_releases_the_same_as_after_model() -> None:
    """A call that raises must not leave the semaphore held forever."""
    throttle = ModelThrottle()
    ctx = _FakeCallbackContext("r_1")
    await throttle.before_model(cast(Any, ctx), cast(Any, None))
    await throttle.on_model_error(cast(Any, ctx), cast(Any, None), RuntimeError("boom"))

    # A second acquire must not block — the first was released, not leaked.
    await asyncio.wait_for(throttle.before_model(cast(Any, ctx), cast(Any, None)), timeout=1.0)
    await throttle.after_model(cast(Any, ctx), cast(Any, None))


async def test_a_release_with_nothing_held_is_a_no_op() -> None:
    """`after_model`/`on_model_error` firing without a matching `before_model` must not
    over-release the semaphore — `release_run` calling both dicts' `.pop` twice, say."""
    throttle = ModelThrottle()
    ctx = _FakeCallbackContext("r_1")
    await throttle.after_model(cast(Any, ctx), cast(Any, None))
    throttle.release_run("r_1")
    throttle.release_run("r_1")  # idempotent


def test_carry_attachments_into_subtopics_threads_them_onto_every_branch() -> None:
    """The decision the M10 review item pins: every sub-topic gets its own copy of the
    run's attachment parts, alongside its own text, in the same order `research_planner`
    produced them."""
    file_part = types.Part(
        file_data=types.FileData(file_uri="gs://bucket/rubric.pdf", mime_type="application/pdf")
    )
    opening_text = types.Part(text="the run's own opening message, not a sub-topic")
    ctx = SimpleNamespace(
        user_content=types.Content(role="user", parts=[opening_text, file_part])
    )

    result = _carry_attachments_into_subtopics(cast(Any, ctx), ["First sub-topic", "Second"])

    assert [content.parts[0].text for content in result] == ["First sub-topic", "Second"]
    assert all(content.parts[1:] == [file_part] for content in result)


def test_carry_attachments_into_subtopics_with_no_attachments_is_just_the_subtopic() -> None:
    ctx = SimpleNamespace(
        user_content=types.Content(role="user", parts=[types.Part(text="no files here")])
    )

    result = _carry_attachments_into_subtopics(cast(Any, ctx), ["Only sub-topic"])

    assert result == [types.Content(role="user", parts=[types.Part(text="Only sub-topic")])]


def test_carry_attachments_into_subtopics_tolerates_no_user_content() -> None:
    """A resumed invocation's `ctx.user_content` should still be the original opening
    message (module docstring), but this must not raise if it were ever unset."""
    ctx = SimpleNamespace(user_content=None)

    result = _carry_attachments_into_subtopics(cast(Any, ctx), ["Only sub-topic"])

    assert result == [types.Content(role="user", parts=[types.Part(text="Only sub-topic")])]


def test_the_workflow_builds_with_three_nodes_and_a_bounded_fan_out() -> None:
    """Construction-time coverage for `output_schema`, `parallel_worker`, and `node()`.

    docs/03-agent-design.md#the-research-pipeline-since-m9: a signature or semantic change to
    any of these fails here, at import/construction time, rather than inside a detached
    generation task on the first real research turn of a deployed revision.
    """
    research_tools = ResearchTools(cast(Any, None), cast(Any, None), BoardUpdateHub())
    workflow = build_research_workflow(
        "stub-model",
        research_tools=research_tools,
        throttle=ModelThrottle(),
    )
    assert isinstance(workflow, Workflow)
    assert workflow.graph is not None
    names = {node.name for node in workflow.graph.nodes}
    assert {
        RESEARCH_PLANNER_NAME,
        TOPIC_RESEARCHER_BRIEF_NAME,
        TOPIC_RESEARCHER_NAME,
        REVIEWER_WRITER_NAME,
    } <= names


def test_the_roadmap_workflow_builds_with_the_shared_fan_out_and_its_own_terminal_nodes() -> (
    None
):
    """`build_roadmap_workflow`'s construction-time coverage, alongside
    `build_research_workflow`'s above: same reason, same new-surface risk, applied to
    `ProposedTaskCollection`'s `output_schema` and `write_study_plan`'s tool declaration.
    """
    research_tools = ResearchTools(cast(Any, None), cast(Any, None), BoardUpdateHub())
    workflow = build_roadmap_workflow(
        "stub-model",
        research_tools=research_tools,
        throttle=ModelThrottle(),
    )
    assert isinstance(workflow, Workflow)
    assert workflow.graph is not None
    names = {node.name for node in workflow.graph.nodes}
    expected = {
        RESEARCH_PLANNER_NAME,
        TOPIC_RESEARCHER_BRIEF_NAME,
        TOPIC_RESEARCHER_NAME,
        TASK_PROPOSER_NAME,
        PLAN_TAILOR_NAME,
    }
    assert expected <= names
    assert workflow.name == ROADMAP_WORKFLOW_NAME

    # Pinning the M10 review decision (docs/09-roadmap.md#m10-beta--agent-consistency-
    # projecttask-ui-polish-and-readme): `task_proposer` shares the `research`/`roadmap`
    # step's own `STEP_TIMEOUT_SECONDS` budget with the `topic_researcher` fan-out, so it
    # must not creep back up to `"high"`.
    proposer = next(node for node in workflow.graph.nodes if node.name == TASK_PROPOSER_NAME)
    thinking_level = proposer.generate_content_config.thinking_config.thinking_level
    assert thinking_level != types.ThinkingLevel.HIGH
