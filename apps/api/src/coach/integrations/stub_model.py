"""A deterministic model, for the end-to-end harness.

docs/08-testing.md: "Runs against the gcloud Firestore emulator and **a stubbed model
server**, so e2e is deterministic."

This is a stubbed *model* rather than a stubbed *server*. A server would have to speak
the Gemini wire protocol convincingly enough for `google.genai` to parse it, which is a
large surface to maintain for no extra confidence — the thing under test in golden flow #4
is the socket, the checkpoints, and the resume path, none of which can tell where the
tokens came from.

**Guarded to `ENV=local`.** `Settings` refuses `MODEL_BACKEND=stub` for any other `ENV`,
so a deployed revision cannot silently serve canned answers — which would be a far worse
failure than not starting, because it would look like the product working.

The reply is derived from the prompt so that a test can assert an exact string, and it is
emitted in many small chunks with a pause between them so that a test has a window in
which to kill the socket mid-stream. That pacing is the whole reason this exists rather
than a one-shot canned response.

## Tool calls (M3)

Golden flows #1, #2, and #7 need the coach to *act*, so the stub also emits function
calls — and it derives them from the prompt for the same reason it derives its text from
it: a canned call would assert nothing about the prompt that produced it.

The rule is one sentence: **when the learner names a duration, plan for it.** The stub
reads the task budget out of the rendered instruction — the line `agents/prompt.py`
writes as `Default task length: 120 minutes` — and:

1. calls `add_task` for work of `min(named duration, 3 * budget)` minutes, that clamp
   being the same one `add_task`'s guard applies, so the call is accepted rather than
   refused;
2. seeing the created task in the function response, calls `add_subtask` — **one at a
   time**, once per pass, until the subtasks cover the parent's estimate. `split_task` did
   this in a single call and was removed: it made the model commit to every subtask before
   discussing any of them, and one bad estimate failed the lot;
3. once they cover it, answers in prose.

## Research (M4, reworked M9)

Golden flow #5 needs a report, so the stub also answers as `reviewer_writer`, the research
pipeline's last node. It recognises that node by its tool set — `post_research_report`
present, `add_task` absent — and emits one report call, with a required list sized from the
**research** budget line the prompt carries (`Budget: 45 minutes`, written by
`agents/prompt.py`'s `render_budget`). That is a different number from the coach's `Default
task length`, and parsing it separately is what makes the e2e assertion meaningful: the
checklist fits the *task's* estimate because the prompt said so, not because the test and
the stub agreed on 45.

That makes flow #7 a real assertion rather than a staged one: the *only* thing that
differs between a project with a two-hour override and one on the 45-minute global
default is the number the prompt carried, so subtask sizes that follow the override prove
the pref reached the model. If `render_prefs` ever stops emitting the minutes, the stub
falls back to `DEFAULT_BUDGET_MINUTES` and `tests/test_stub_model.py` fails on the
mismatch — which is the point of parsing rather than being told.

**Since M9, `research_planner` and `topic_researcher` need answering too**, and neither has
a tool to key on the way `reviewer_writer` does — `research_planner` has no tools at all,
and `topic_researcher`'s (`search_agent`, `fetch_url`, `youtube_find_by_duration`) are not
distinctive enough to spot reliably. Both, though, carry a `response_schema` on the request
(`agents/research_workflow.py` sets `output_schema=list[str]` / `list[dict[str, Any]]`),
which is a signal nothing else in this project's agent graph sets — `_structured_reply`
reads it straight off `llm_request.config.response_schema` and answers with schema-shaped,
content-free JSON. What the two intermediate nodes' output actually *says* is not asserted
anywhere: `reviewer_writer`'s stub reply is the same fixed `research_plan(budget)` it
always was, independent of what came before it in the transcript, so every existing
assertion about report shape holds unchanged. Report *quality* — whether a real model's
planner/researcher output is any good, and whether `reviewer_writer` actually deduplicates
and merges from it — is the nightly evalset's job, same as it always was.

**`StubModel.capabilities` reports `output_schema_and_tools=True`**, so that
`topic_researcher` (which has both an `output_schema` and real tools) takes the same
`response_schema`-on-the-request path as `research_planner` rather than ADK's
tool-injection workaround (`flows/llm_flows/_output_schema_processor.py`) for models that
cannot combine the two — the workaround exists for models this project does not use in
production either, so declaring the capability keeps the stub on the same code path a real
`gemini-3.7-flash` request already takes.

**`task_proposer` and `plan_tailor` (`build_roadmap_workflow`) are answered the same two
ways**, not a third: `task_proposer` has an `output_schema` and no tools, so it is spotted
and answered by `_structured_reply` exactly like `research_planner`; `plan_tailor` has
`write_study_plan` and no `output_schema`, so it is spotted and answered by
`_plan_tool_call` exactly like `reviewer_writer`. Not yet reachable through
`ResearchService`/`RunExecutor` — see `agents/research_workflow.py`'s module docstring —
but answering it here is what lets a test start an `agent="roadmap"` turn directly.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from coach.agents.research_workflow import TopicFindings
from coach.services.models import ProposedTaskCollection

#: Milliseconds between chunks. Overridable so a slow CI machine can widen the window
#: golden flow #4 disconnects inside without the test having to guess.
DELAY_ENV_VAR = "STUB_MODEL_DELAY_MS"
DEFAULT_DELAY_MS = 40

PREFIX = "Here is what I think about "
SUFFIX = (
    " Let us break it down together, one step at a time, and check your understanding as we go."
)

#: What the stub says once its tool calls have come back. Short and fixed, so a flow can
#: wait for it as the signal that the board is settled.
DONE_REPLY = "Done — your board is up to date."

#: Used when the instruction does not carry a budget, which in practice means the prompt
#: builder could not read the project. Matches `GlobalPrefs.default_task_minutes`.
DEFAULT_BUDGET_MINUTES = 45

#: The one prompt that makes the stub answer in markdown rather than in prose.
_FORMATTING_PATTERN = re.compile(r"\bshow me the formatting\b", re.IGNORECASE)

#: The prompt that makes the stub ask a question, so `ask_learner`'s whole handshake — the
#: dialog, the structured answer, and the chip that records it — is reachable end to end.
_ASK_PATTERN = re.compile(r"\bask me something\b", re.IGNORECASE)

#: The prompt that makes it ask a *multi-select* question. A separate phrase rather than a
#: flag on the first, so a test can reach either mode — and because the two modes are the
#: thing worth being able to tell apart: the model reaches for single-choice by default, and
#: an e2e that could only exercise that would never notice checkboxes breaking.
_ASK_MANY_PATTERN = re.compile(r"\bask me about several\b", re.IGNORECASE)

#: What it asks. Fixed, so an e2e can click a named option.
STUB_QUESTION = "Which should we do first?"
STUB_OPTIONS = ["The parser", "The lexer"]
STUB_MULTI_QUESTION = "Which of these have you used before?"
STUB_MULTI_OPTIONS = ["Generators", "Context managers", "Async iterators"]

#: And the one that makes it fail, so the `turn_error` path is reachable from a test.
#:
#: It exists because that path had a defect no unit test could see: a turn ending in
#: `turn_error` is never cleared from `useStreamStore`, and the pane read the *first*
#: buffered turn for the session rather than the newest — so the error stayed on screen
#: and every later reply streamed into a buffer nothing rendered. It took a real 429 from
#: Vertex to find, and a stub that cannot fail is a stub that could never have found it.
_FAILURE_PATTERN = re.compile(r"\bmake this turn fail\b", re.IGNORECASE)

#: What the stub raises for that prompt. Shaped like the model error it stands in for —
#: `services/turns.py` classifies 429 as retryable, so the UI offers "You can try again".
STUB_FAILURE_MESSAGE = "429 RESOURCE_EXHAUSTED: stub failure requested by the prompt"

#: M8-quotas: a fixed token count on every non-partial response, so a local e2e run (which
#: never sees a real model) still exercises point deduction — one call, one point, deducted
#: from both windows. `TurnService._generate` reads this off `usage_metadata`, never
#: off a partial chunk, so a multi-call turn (tool loop) charges once per call as intended.
STUB_USAGE_TOKENS = 1000

#: A reply exercising every construct the transcript renders: a GFM table, an equation, a
#: fenced code block, and a mermaid diagram (docs/06-frontend.md#markdown-in-the-transcript).
#:
#: This exists because the browser half of that rendering is unreachable from a unit test
#: in the way that matters. `Markdown.test.tsx` mocks both dynamic imports — it has to, or
#: it would be testing shiki's Python grammar — so the thing no local test can see is
#: whether those chunks actually resolve in a *built* bundle. That is exactly the class of
#: defect docs/09-roadmap.md#what-a-green-local-run-does-not-prove is about, and one
#: e2e against a real build is what closes it.
MARKDOWN_REPLY = r"""## Your plan

| Step | Minutes |
| --- | --- |
| Read the paper | 20 |
| Write notes | 25 |

Merging costs $O(n \log n)$ overall.

```python
def merge(left, right):
    return sorted(left + right)
```

```mermaid
graph TD;
  A[Read] --> B[Notes];
```
"""

#: The line `agents/prompt.py` renders. Parsed rather than passed, so that the stub is
#: reading the same prompt the real model would.
_BUDGET_PATTERN = re.compile(r"Default task length:\s*(\d+)\s*minutes")

#: `render_budget`'s line, which carries the *task's* estimate rather than the project
#: default. A separate pattern rather than a reused one, because the two numbers differ and
#: conflating them would make flow #5 assert the wrong one.
_RESEARCH_BUDGET_PATTERN = re.compile(r"Budget:\s*(\d+)\s*minutes")

#: "4 hours", "90 minutes", "2h". Whole units only: a stub that parsed "2.5 hours" would
#: be inventing precision no assertion depends on.
_DURATION_PATTERN = re.compile(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.IGNORECASE)

_HOUR_UNITS = frozenset({"h", "hr", "hrs", "hour", "hours"})

#: `add_task`'s guard: "minutes <= 3x default" (docs/03-agent-design.md).
_MAX_TASK_FACTOR = 3

#: How many subtasks the stub will add before giving up and answering. Not a product rule —
#: `add_subtask` has no such cap — but a stub that plans from its own function responses
#: needs a termination argument that does not depend on arithmetic working out.
_MAX_SUBTASKS = 8

#: "drop that", "discard the first one". The one instruction the stub takes that is not a
#: duration, and it exists so the confirmation gate on `discard_task` is reachable from a
#: test at all — a gate nothing can trip is a gate nothing verifies.
_DISCARD_PATTERN = re.compile(r"\b(discard|drop)\b", re.IGNORECASE)

#: Every tool name either `project_coach` or `task_teacher` might declare
#: (docs/03-agent-design.md#domain-tools). Used only to tell "an agent with *no* domain
#: tools at all" — the disconnect suite's plain streaming double — from either of the two
#: real ones, neither of which carries the full catalogue after
#: docs/09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher.
_DOMAIN_TOOLS = frozenset(
    {
        "list_tasks",
        "add_task",
        "add_subtask",
        "update_task",
        "set_task_state",
        "set_next_up",
        "reorder_task",
        "discard_task",
        "add_task_items",
        "update_task_item",
        "reorder_task_item",
        "move_task_items",
        "delete_task_item",
        "complete_task_item",
        "ask_learner",
        "update_project_prefs",
        "update_project_plan",
        "update_learner_profile",
        "remember",
        "load_memory",
    }
)

#: Task ids as `agents/prompt.py` renders them into the board: `(45 min, id=k_01J…)`.
_TASK_ID_PATTERN = re.compile(r"id=([A-Za-z0-9_-]+)")

#: The first *outstanding* checklist item, as `render_items` writes it:
#: `  [ ] Read §3 (id=i_01J…, 12 min)`. A completed one is `[x]` and must not match — a
#: coach that ticked an already-ticked step is not behaviour worth scripting, and a test
#: driving it would spin on the same item forever.
#:
#: Narrower than the task pattern in a second way too: both ids are spelled `id=`, and the
#: prefix is the only thing telling an item from a task (`coach.core.ids`).
_ITEM_ID_PATTERN = re.compile(r"\[ \][^\n]*?\(id=(i_[A-Za-z0-9]+)")

#: The prompt that makes the stub tick the first outstanding step, so the completion gate —
#: and the project preference that silences it — are reachable end to end.
_COMPLETE_PATTERN = re.compile(r"\bmark the first step done\b", re.IGNORECASE)


def stub_reply(prompt: str) -> str:
    """The exact text the stub will produce for `prompt`.

    Exported so a test can assert against it without hard-coding the same string twice.
    """
    return f"{PREFIX}{prompt.strip() or 'your task'}.{SUFFIX}"


def budget_minutes(instruction: str) -> int:
    """The task budget the prompt carried, or the global default if it carried none."""
    match = _BUDGET_PATTERN.search(instruction)
    return int(match.group(1)) if match else DEFAULT_BUDGET_MINUTES


def research_budget_minutes(instruction: str) -> int:
    """The minute budget the research prompt carried, or the global default."""
    match = _RESEARCH_BUDGET_PATTERN.search(instruction)
    return int(match.group(1)) if match else DEFAULT_BUDGET_MINUTES


def research_plan(budget: int) -> dict[str, Any]:
    """A report that fits `budget`: one thing to read, one thing to do.

    Two items rather than one so that flow #5 can tick one and see the task *not* complete,
    then tick the other and see it complete — which is the difference between testing
    invariant 6 and testing that a checkbox writes a boolean. One guided and one unguided,
    so the two renderings are both on screen.

    The reading takes the smaller share, so the two sum to exactly `budget` and the budget
    meter's "X of Y" is an equality.
    """
    reading = max(1, budget // 3)
    return {
        "summary": "Two things to get through: one to read, one to work through with me.",
        "required": [
            {
                "kind": "article",
                "title": "The official guide",
                "url": "https://example.com/guide",
                "minutes": reading,
                "why": "Read the official guide, so you have the vocabulary for the rest",
                "source": "web",
            },
            {
                "kind": "exercise",
                "title": "Work through it with your coach",
                "minutes": budget - reading,
                "why": "Work through the exercise with me, to check it actually landed",
                "details": "Ask them to explain it back before showing them the answer.",
                "source": "generated",
            },
        ],
        "optional": [
            {
                "kind": "article",
                "title": "A deeper treatment",
                "url": "https://example.com/deeper",
                "minutes": 30,
                "why": "If you want to go further than this task needs",
                "source": "web",
            }
        ],
    }


#: `research_planner`'s stub sub-topics. Content is never asserted on — `reviewer_writer`'s
#: reply is the fixed `research_plan(budget)`, independent of what came before it — only the
#: *count* matters, and 3 is inside the "3 to 5" the real instruction asks for.
_STUB_SUBTOPICS = ["First sub-topic", "Second sub-topic", "Third sub-topic"]

#: `task_proposer`'s stub proposal, and what `write_study_plan`'s stub call below reuses as
#: its own `proposed_tasks` — one shared definition, so the two can never drift apart the
#: way two independently hand-written stub payloads could. Content is never asserted on,
#: same rule as `_STUB_SUBTOPICS` and `reviewer_writer`'s stub reply: only the *shape* — one
#: task, with a `slug` the plan below can reference — is what makes the pipeline's contract
#: reachable from a test.
_STUB_PROPOSED_TASK: dict[str, Any] = {
    "slug": "stub-task",
    "title": "A stub proposed task",
    "description": "Stub content — never asserted on.",
    "required": [
        {
            "kind": "article",
            "title": "A stub finding",
            "url": "https://example.com/stub-finding",
            "minutes": 10,
            "why": "Stub research output — content is never asserted on.",
            "source": "web",
        }
    ],
    "optional": [],
    "prerequisite_tasks": [],
}

_STUB_PLAN_MEMO = "Stub memo — content is never asserted on."


def _structured_reply(llm_request: Any) -> str | None:
    """JSON text matching the request's `response_schema`, or `None` if it has none.

    `research_planner`, `topic_researcher`, and `task_proposer` all carry one
    (`agents/research_workflow.py` sets `output_schema=list[str]` /
    `output_schema=TopicFindings` / `output_schema=ProposedTaskCollection`); nothing else in
    this project's agent graph does, which is what makes this a reliable way to spot them
    without adding an agent-name special case. See the module docstring's M9 section.
    """
    config = getattr(llm_request, "config", None)
    schema = getattr(config, "response_schema", None) if config else None
    if schema is None:
        return None
    if schema == list[str]:
        return json.dumps(_STUB_SUBTOPICS)
    if schema is TopicFindings:
        return json.dumps(
            {
                "items": [
                    {
                        "kind": "article",
                        "title": "A stub finding for this sub-topic",
                        "url": "https://example.com/stub-finding",
                        "minutes": 10,
                        "why": "Stub research output — content is never asserted on.",
                        "source": "web",
                    }
                ]
            }
        )
    if schema is ProposedTaskCollection:
        return json.dumps({"tasks": [_STUB_PROPOSED_TASK], "memo": _STUB_PLAN_MEMO})
    return None


def first_task_id(instruction: str) -> str | None:
    """The first task id on the rendered board, or `None` if the board is empty."""
    match = _TASK_ID_PATTERN.search(instruction)
    return match.group(1) if match else None


def requested_minutes(text: str) -> int | None:
    """The duration the learner named, in minutes, or `None` if they named none."""
    match = _DURATION_PATTERN.search(text)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    return amount * 60 if unit in _HOUR_UNITS else amount


def split_sizes(minutes: int, budget: int) -> list[int]:
    """`minutes` of work as subtask estimates that each fit `budget`.

    The last one absorbs the remainder rather than every one being rounded, so the subtask
    minutes sum to exactly the parent's — which is what makes the parent card's
    "N subtasks · X" assertion in golden flow #2 an equality rather than an approximation.
    """
    count = min(_MAX_SUBTASKS, max(2, math.ceil(minutes / budget)))
    each = minutes // count
    sizes = [each] * count
    sizes[-1] += minutes - each * count
    return sizes


class StubModel(BaseLlm):
    """Echoes the prompt back, slowly, in many pieces — and acts on the board."""

    model: str = "stub-model"

    @property
    def capabilities(self) -> LlmCapabilities:
        """Declares `output_schema_and_tools=True`. See the module docstring's M9 section."""
        return LlmCapabilities(
            **super().capabilities.model_dump() | {"output_schema_and_tools": True}
        )

    @property
    def _delay_seconds(self) -> float:
        try:
            return int(os.environ.get(DELAY_ENV_VAR, DEFAULT_DELAY_MS)) / 1000
        except ValueError:
            return DEFAULT_DELAY_MS / 1000

    async def generate_content_async(
        self, llm_request: Any, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if _FAILURE_PATTERN.search(_last_user_text(llm_request)):
            raise RuntimeError(STUB_FAILURE_MESSAGE)

        structured = _structured_reply(llm_request)
        if structured is not None:
            # `research_planner` / `topic_researcher`: schema-shaped, content-free JSON.
            # See the module docstring's M9 section for why this skips tool calls entirely.
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=structured)]),
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    total_token_count=STUB_USAGE_TOKENS
                ),
            )
            return

        call = _plan_tool_call(llm_request)
        if call is not None:
            # Function calls are not streamed in pieces: a partial function call is not a
            # thing the flow can act on, and ADK aggregates the finalized event anyway.
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(function_call=call)]),
                usage_metadata=types.GenerateContentResponseUsageMetadata(
                    total_token_count=STUB_USAGE_TOKENS
                ),
            )
            return

        reply = DONE_REPLY if _turn_responses(llm_request) else _prose_reply(llm_request)
        # Split on spaces, keeping them, so the concatenation of the chunks is exactly
        # `reply` — the assertion golden flow #4 rests on is character equality between
        # an interrupted run and an uninterrupted one.
        chunks = [word + " " for word in reply.split(" ")]
        chunks[-1] = chunks[-1].rstrip()

        for chunk in chunks:
            await asyncio.sleep(self._delay_seconds)
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(text=chunk)]),
                partial=True,
            )
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text="".join(chunks))]),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                total_token_count=STUB_USAGE_TOKENS
            ),
        )


def _prose_reply(llm_request: Any) -> str:
    """What the stub says when it has nothing to call.

    Markdown only for the one prompt that asks for it, so every existing flow keeps
    getting the character-for-character reply it asserts against.
    """
    text = _last_user_text(llm_request)
    return MARKDOWN_REPLY if _FORMATTING_PATTERN.search(text) else stub_reply(text)


def _plan_tool_call(llm_request: Any) -> types.FunctionCall | None:
    """The next function call, or `None` if the stub should answer in prose instead.

    Everything is decided from **this turn's** function responses, not from the whole
    conversation, and that scoping is the loop's termination argument. The contents ADK
    sends carry the entire session, so "have I already split something?" asked of the full
    history answers yes forever after the first split — and asked of nothing at all
    answers no forever, which is worse: the stub re-issues `split_task` on every pass and
    the turn never ends. `_turn_responses` is the boundary between the two.
    """
    tools = _available_tools(llm_request)
    responses = _turn_responses(llm_request)

    if "post_research_report" in tools:
        # `reviewer_writer`. One report, then prose — and nothing else, because the real one
        # is instructed to deliver exactly one `post_research_report` call.
        if responses:
            return None
        plan = research_plan(research_budget_minutes(_instruction(llm_request)))
        return types.FunctionCall(name="post_research_report", args=plan)

    if "write_study_plan" in tools:
        # `plan_tailor`. One plan, then prose — the real one is instructed to deliver
        # exactly one `write_study_plan` call, same shape as `reviewer_writer` above.
        if responses:
            return None
        return types.FunctionCall(
            name="write_study_plan",
            args={
                "title": "A stub study plan",
                "short_description": "Stub content — never asserted on.",
                "long_description": "Stub content — never asserted on.",
                "memo": _STUB_PLAN_MEMO,
                "proposed_tasks": [_STUB_PROPOSED_TASK],
                "plan": [
                    {
                        "task_slug": _STUB_PROPOSED_TASK["slug"],
                        "after": None,
                        "prerequisite_tasks": [],
                        "relevance": 4,
                        "decision": "include",
                        "why": "Stub reasoning — content is never asserted on.",
                    }
                ],
            },
        )

    if not tools & _DOMAIN_TOOLS:
        # No domain tools on this agent — the disconnect suite builds one that way, and
        # it must keep getting the plain streaming reply it asserts against.
        return None

    budget = budget_minutes(_instruction(llm_request))

    # `task_teacher` has no `add_task` (docs/09-roadmap.md#m6), so `responses` can never
    # hold one on that agent and this is naturally a no-op there rather than needing its
    # own guard.
    created = _created_task(responses)
    if created is not None:
        task_id, minutes, title = created
        if minutes <= budget:
            return None
        sizes = split_sizes(minutes, budget)
        # One subtask per pass, counted from *this turn's* responses rather than from
        # anything remembered — the stub is stateless by construction, and two turns of one
        # conversation may be served by two processes.
        added = sum(1 for name, _ in responses if name == "add_subtask")
        if added >= len(sizes):
            return None
        return types.FunctionCall(
            name="add_subtask",
            args={
                "task_id": task_id,
                "title": f"{title} — part {added + 1}",
                "description": "",
                "estimated_minutes": sizes[added],
                "needs_research": True,
            },
        )

    if responses:
        # Something came back and it was not a task worth splitting — a refusal, or a
        # tool this stub does not plan around. The plan is finished; say so.
        return None

    text = _last_user_text(llm_request)

    if _ASK_MANY_PATTERN.search(text) and "ask_learner" in tools:
        return types.FunctionCall(
            name="ask_learner",
            args={
                "question": STUB_MULTI_QUESTION,
                "options": list(STUB_MULTI_OPTIONS),
                "allow_multiple": True,
                "allow_none": True,
                "note_prompt": "",
            },
        )

    if _ASK_PATTERN.search(text) and "ask_learner" in tools:
        return types.FunctionCall(
            name="ask_learner",
            args={
                "question": STUB_QUESTION,
                "options": list(STUB_OPTIONS),
                "allow_multiple": False,
                "allow_none": True,
                "note_prompt": "Anything else I should know?",
            },
        )

    if _COMPLETE_PATTERN.search(text) and "complete_task_item" in tools:
        match = _ITEM_ID_PATTERN.search(_instruction(llm_request))
        if match is None:
            return None
        return types.FunctionCall(
            name="complete_task_item",
            args={"item_id": match.group(1), "note": "you told me you had done it"},
        )

    if _DISCARD_PATTERN.search(text) and "discard_task" in tools:
        target = first_task_id(_instruction(llm_request))
        if target is not None:
            return types.FunctionCall(
                name="discard_task",
                args={"task_id": target, "reason": "You asked me to drop this one."},
            )
        return None

    if re.search(r"\b(propose a plan|update the plan|suggest a plan)\b", text, re.I) and (
        "update_project_plan" in tools
    ):
        return types.FunctionCall(
            name="update_project_plan",
            args={
                "summary": "Study plan for this project",
                "tasks": [
                    {
                        "title": "Phase 1: Foundations",
                        "description": "Core concepts and setup",
                        "estimated_minutes": budget,
                        "needs_research": True,
                    },
                    {
                        "title": "Phase 2: Practical project",
                        "description": "Hands-on implementation",
                        "estimated_minutes": budget,
                        "needs_research": True,
                    },
                ],
            },
        )

    if "add_task" not in tools:
        # `task_teacher`: nothing left in this stub's repertoire is one of its tools, so
        # it answers in prose rather than reaching for a call the agent does not have.
        return None

    asked = requested_minutes(text)
    if asked is None:
        return None

    return types.FunctionCall(
        name="add_task",
        args={
            "title": _task_title(text),
            "description": "Planned from what you described.",
            "estimated_minutes": min(asked, budget * _MAX_TASK_FACTOR),
            "needs_research": True,
        },
    )


def _task_title(text: str) -> str:
    """A stable title from the learner's message.

    The first clause, capped — deterministic, and readable enough that a Playwright
    assertion on it does not look like a hash.
    """
    first = re.split(r"[.,;\n]", text.strip(), maxsplit=1)[0].strip()
    return (first or "Next step")[:120]


def _available_tools(llm_request: Any) -> set[str]:
    return set(getattr(llm_request, "tools_dict", None) or {})


def _instruction(llm_request: Any) -> str:
    """The rendered system instruction, whatever shape `types.Content` it arrived in."""
    config = getattr(llm_request, "config", None)
    instruction = getattr(config, "system_instruction", None) if config else None
    if isinstance(instruction, str):
        return instruction
    parts = getattr(instruction, "parts", None) or []
    return "".join(part.text for part in parts if getattr(part, "text", None))


def _contents(llm_request: Any) -> list[Any]:
    return list(getattr(llm_request, "contents", None) or [])


def _turn_responses(llm_request: Any) -> list[tuple[str, Any]]:
    """`(name, response)` for every function response since the learner's last message.

    Walking backwards and stopping at the first content that has *text* is what makes
    this "this turn": a function response arrives as a `user`-role content with no text,
    so role alone cannot tell the learner's message from the tools' answers.
    """
    collected: list[tuple[str, Any]] = []
    for content in reversed(_contents(llm_request)):
        parts = getattr(content, "parts", None) or []
        texts = [part for part in parts if getattr(part, "text", None)]
        if texts and getattr(content, "role", None) == "user":
            break
        for part in parts:
            response = getattr(part, "function_response", None)
            if response is not None:
                collected.append((response.name or "", response.response))
    collected.reverse()
    return collected


def _created_task(responses: list[tuple[str, Any]]) -> tuple[str, int, str] | None:
    """`(taskId, estimatedMinutes, title)` from this turn's successful `add_task`.

    Read out of the function *response* rather than remembered between calls, because the
    stub is stateless by construction: two turns of one conversation may be served by two
    processes, and a stub that only worked when they were the same one would pass locally
    and hang in the e2e container.
    """
    for name, payload in reversed(responses):
        if name != "add_task":
            continue
        if not isinstance(payload, dict) or not payload.get("ok"):
            return None
        task = payload.get("task")
        if not isinstance(task, dict):
            return None
        return str(task["taskId"]), int(task["estimatedMinutes"]), str(task["title"])
    return None


def _last_user_text(llm_request: Any) -> str:
    """The most recent user message in the request, or an empty string."""
    for content in reversed(_contents(llm_request)):
        if getattr(content, "role", None) != "user":
            continue
        parts = getattr(content, "parts", None) or []
        text = "".join(part.text for part in parts if getattr(part, "text", None))
        if text:
            return text
    return ""


__all__ = [
    "DEFAULT_BUDGET_MINUTES",
    "DEFAULT_DELAY_MS",
    "DELAY_ENV_VAR",
    "DONE_REPLY",
    "STUB_FAILURE_MESSAGE",
    "STUB_MULTI_OPTIONS",
    "STUB_MULTI_QUESTION",
    "STUB_OPTIONS",
    "STUB_QUESTION",
    "STUB_USAGE_TOKENS",
    "StubModel",
    "budget_minutes",
    "first_task_id",
    "requested_minutes",
    "research_budget_minutes",
    "research_plan",
    "split_sizes",
    "stub_reply",
]
