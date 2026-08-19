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
2. on the next turn, seeing the created task in the function response, calls `split_task`
   if it does not fit the budget, into `ceil(minutes / budget)` equal subtasks;
3. on the turn after that, answers in prose.

## Research (M4)

Golden flow #5 needs a report, so the stub also answers as `research_agent`. It recognises
that agent by its tool set — `post_research_report` present, `add_task` absent — and emits
one report call, with a required list sized from the **research** budget line the prompt
carries (`Budget: 45 minutes`, written by `agents/prompt.py`'s `render_budget`). That is a
different number from the coach's `Default task length`, and parsing it separately is what
makes the e2e assertion meaningful: the checklist fits the *task's* estimate because the
prompt said so, not because the test and the stub agreed on 45.

That makes flow #7 a real assertion rather than a staged one: the *only* thing that
differs between a project with a two-hour override and one on the 45-minute global
default is the number the prompt carried, so subtask sizes that follow the override prove
the pref reached the model. If `render_prefs` ever stops emitting the minutes, the stub
falls back to `DEFAULT_BUDGET_MINUTES` and `tests/test_stub_model.py` fails on the
mismatch — which is the point of parsing rather than being told.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types

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

#: `split_task`'s: "2-8 subtasks" (docs/03-agent-design.md, `services/tasks.py`).
_MAX_SUBTASKS = 8

#: "drop that", "discard the first one". The one instruction the stub takes that is not a
#: duration, and it exists so the confirmation gate on `discard_task` is reachable from a
#: test at all — a gate nothing can trip is a gate nothing verifies.
_DISCARD_PATTERN = re.compile(r"\b(discard|drop)\b", re.IGNORECASE)

#: Task ids as `agents/prompt.py` renders them into the board: `(45 min, id=k_01J…)`.
_TASK_ID_PATTERN = re.compile(r"id=([A-Za-z0-9_-]+)")


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


def split_plan(title: str, minutes: int, budget: int) -> list[dict[str, Any]]:
    """`minutes` of work as subtasks that each fit `budget`.

    The last one absorbs the remainder rather than every one being rounded, so the
    subtask minutes sum to exactly the parent's — which is what makes the parent card's
    "N subtasks · X" assertion in golden flow #2 an equality rather than an approximation.
    """
    count = min(_MAX_SUBTASKS, max(2, math.ceil(minutes / budget)))
    each = minutes // count
    sizes = [each] * count
    sizes[-1] += minutes - each * count
    return [
        {
            "title": f"{title} — part {index + 1}",
            "description": "",
            "estimatedMinutes": size,
            "needsResearch": True,
        }
        for index, size in enumerate(sizes)
    ]


class StubModel(BaseLlm):
    """Echoes the prompt back, slowly, in many pieces — and acts on the board."""

    model: str = "stub-model"

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

        call = _plan_tool_call(llm_request)
        if call is not None:
            # Function calls are not streamed in pieces: a partial function call is not a
            # thing the flow can act on, and ADK aggregates the finalized event anyway.
            yield LlmResponse(
                content=types.Content(role="model", parts=[types.Part(function_call=call)])
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
            content=types.Content(role="model", parts=[types.Part(text="".join(chunks))])
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
        # `research_agent`. One report, then prose — and nothing else, because the real one
        # is instructed to deliver exactly one `post_research_report` call.
        if responses:
            return None
        plan = research_plan(research_budget_minutes(_instruction(llm_request)))
        return types.FunctionCall(name="post_research_report", args=plan)

    if "add_task" not in tools:
        # No domain tools on this agent — the disconnect suite builds one that way, and
        # it must keep getting the plain streaming reply it asserts against.
        return None

    budget = budget_minutes(_instruction(llm_request))

    if any(name == "split_task" for name, _ in responses):
        return None

    created = _created_task(responses)
    if created is not None:
        task_id, minutes, title = created
        if minutes > budget:
            return types.FunctionCall(
                name="split_task",
                args={"task_id": task_id, "subtasks": split_plan(title, minutes, budget)},
            )
        return None

    if responses:
        # Something came back and it was not a task worth splitting — a refusal, or a
        # tool this stub does not plan around. The plan is finished; say so.
        return None

    text = _last_user_text(llm_request)

    if _DISCARD_PATTERN.search(text):
        target = first_task_id(_instruction(llm_request))
        if target is not None:
            return types.FunctionCall(
                name="discard_task",
                args={"task_id": target, "reason": "You asked me to drop this one."},
            )
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
    "StubModel",
    "budget_minutes",
    "first_task_id",
    "requested_minutes",
    "research_budget_minutes",
    "research_plan",
    "split_plan",
    "stub_reply",
]
