#!/usr/bin/env python
"""Generate the session-event parity vectors.

`GET /api/sessions/{sid}/events` returns the serialized ADK `Event` **verbatim**
(docs/02-data-model.md nests the whole thing under `event_data`), so
`apps/web/src/lib/transcript.ts` is the one module that reads a shape this project does not
define. That shape belongs to `google-adk`, which is pinned — and it is not the shape the
same model uses on the wire elsewhere.

This script dumps real events exactly as `CoachSessionService.append_event` stores them
(`event.model_dump(exclude_none=True, mode="json")`) into
`apps/web/src/lib/session-event-vectors.json`, which `transcript.test.ts` replays.

**Why this exists rather than hand-written fixtures.** It was written after attachments
silently disappeared from reopened conversations. `Event.model_config` sets
`alias_generator=to_camel`, so the *aliases* are camelCase and it is natural to assume the
JSON is too — but `model_dump()` defaults to `by_alias=False`, so the stored keys are
`file_data` / `mime_type`, not `fileData` / `mimeType`. The frontend read the camelCase
names, found nothing, and rendered a message with no sign it had ever carried a file. Every
unit test passed, because the fixtures had been invented from the same wrong assumption.

An observed fixture cannot make that mistake. Run it via
`./scripts/dev.sh gen-event-vectors` after an ADK bump — the output is committed, so a
change of shape surfaces as a failing test rather than a file that silently regenerates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "apps" / "api" / "src"))

from google.adk.events.event import Event  # noqa: E402
from google.adk.events.event_actions import EventActions  # noqa: E402
from google.genai import types  # noqa: E402

OUTPUT = REPO_ROOT / "apps" / "web" / "src" / "lib" / "session-event-vectors.json"

#: Function-call ids as ADK mints them. Fixed rather than generated, because the vectors
#: are committed and a fresh uuid per run would make every regeneration a diff.
CALL_ID = "adk-a86ab509-631b-4261-8c28-054efab32bae"
REFUSED_CALL_ID = "adk-c17c543d-6c03-4e22-8a10-13a2a3ac4b9d"
MIXED_CALL_ID = "adk-37ec9330-f43f-44e4-b266-c41c3aa735c1"

#: The artifact URI shape `finalize` produces, so the vector carries a realistic one.
ARTIFACT_URI = "gs://coach-dev-coach-artifacts/coach/u_alice/user/user:up_01J7Z8/0"


def stored(event: Event) -> dict[str, Any]:
    """Exactly what `append_event` writes under `event_data`."""
    return event.model_dump(exclude_none=True, mode="json")


def image_part(display_name: str | None) -> types.Part:
    part = types.Part.from_uri(file_uri=ARTIFACT_URI, mime_type="image/png")
    if display_name and part.file_data is not None:
        part.file_data.display_name = display_name
    return part


def main() -> int:
    vectors: list[dict[str, Any]] = [
        {
            "name": "user text",
            "expect": {"role": "user", "text": "why does this deadlock?", "attachments": 0},
            "event": stored(
                Event(
                    invocation_id="inv_1",
                    author="user",
                    content=types.Content(
                        role="user", parts=[types.Part(text="why does this deadlock?")]
                    ),
                )
            ),
        },
        {
            "name": "model text",
            "expect": {"role": "model", "text": "Because both hold a lock.", "attachments": 0},
            "event": stored(
                Event(
                    invocation_id="inv_1",
                    author="coach_agent",
                    content=types.Content(
                        role="model", parts=[types.Part(text="Because both hold a lock.")]
                    ),
                )
            ),
        },
        {
            "name": "user text with a named image attachment",
            "expect": {
                "role": "user",
                "text": "what do you make of this?",
                "attachments": 1,
                "attachmentMimeType": "image/png",
                "attachmentFilename": "screenshot.png",
            },
            "event": stored(
                Event(
                    invocation_id="inv_2",
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[
                            types.Part(text="what do you make of this?"),
                            image_part("screenshot.png"),
                        ],
                    ),
                )
            ),
        },
        {
            "name": "attachment with no text",
            "expect": {
                "role": "user",
                "text": "",
                "attachments": 1,
                "attachmentMimeType": "image/png",
            },
            "event": stored(
                Event(
                    invocation_id="inv_3",
                    author="user",
                    content=types.Content(role="user", parts=[image_part(None)]),
                )
            ),
        },
        {
            "name": "pdf attachment",
            "expect": {
                "role": "user",
                "text": "",
                "attachments": 1,
                "attachmentMimeType": "application/pdf",
            },
            "event": stored(
                Event(
                    invocation_id="inv_4",
                    author="user",
                    content=types.Content(
                        role="user",
                        parts=[
                            types.Part.from_uri(
                                file_uri=ARTIFACT_URI, mime_type="application/pdf"
                            )
                        ],
                    ),
                )
            ),
        },
        {
            # The record of what the coach did, and the reason the transcript cannot drop
            # a call-only event: the live chips are cleared on `turn_complete`, so this
            # event is the *only* surviving evidence that a task was added by the agent.
            "name": "tool call only, which is still tool activity",
            # `ok: True` comes from the *next* vector, which is that call's stored result.
            # The two are replayed as one transcript, which is the pairing under test.
            "expect": {
                "role": "model",
                "text": "",
                "attachments": 0,
                "tools": [{"name": "add_task", "callId": CALL_ID, "ok": True}],
            },
            "event": stored(
                Event(
                    invocation_id="inv_5",
                    author="coach_agent",
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    id=CALL_ID,
                                    name="add_task",
                                    args={"title": "Read about locks"},
                                )
                            )
                        ],
                    ),
                )
            ),
        },
        {
            # ADK stores the outcome as its own event, authored `user`. It carries no
            # chip of its own — pairing it with the call above is what gives that chip a
            # tick — so this vector's job is to assert it produces no second entry.
            "name": "the tool's result, which is not a message of its own",
            "expect": {"dropped": True},
            "event": stored(
                Event(
                    invocation_id="inv_5",
                    author="coach_agent",
                    content=types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=CALL_ID,
                                    name="add_task",
                                    response={
                                        "ok": True,
                                        "task": {"taskId": "k_01J7Z8", "title": "Locks"},
                                    },
                                )
                            )
                        ],
                    ),
                )
            ),
        },
        {
            # A refused guard is a *result*, not an exception (`agents/tools.py`), so the
            # chip has to be able to say so. Paired with the call in the next vector.
            "name": "a tool that refused",
            "expect": {"dropped": True},
            "event": stored(
                Event(
                    invocation_id="inv_7",
                    author="coach_agent",
                    content=types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=REFUSED_CALL_ID,
                                    name="add_task",
                                    response={
                                        "ok": False,
                                        "error": {
                                            "code": "validation-error",
                                            "message": "300 minutes is more than "
                                            "this project allows for one task.",
                                        },
                                    },
                                )
                            )
                        ],
                    ),
                )
            ),
        },
        {
            "name": "the call the refusal answers",
            "expect": {
                "role": "model",
                "text": "",
                "attachments": 0,
                "tools": [{"name": "add_task", "callId": REFUSED_CALL_ID, "ok": False}],
            },
            "event": stored(
                Event(
                    invocation_id="inv_7",
                    author="coach_agent",
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(
                                function_call=types.FunctionCall(
                                    id=REFUSED_CALL_ID,
                                    name="add_task",
                                    args={"estimated_minutes": 300},
                                )
                            )
                        ],
                    ),
                )
            ),
        },
        {
            "name": "text alongside a tool call",
            "expect": {
                "role": "model",
                "text": "Adding that now.",
                "attachments": 0,
                "tools": [{"name": "add_task", "callId": MIXED_CALL_ID, "ok": None}],
            },
            "event": stored(
                Event(
                    invocation_id="inv_6",
                    author="coach_agent",
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part(text="Adding that now."),
                            types.Part(
                                function_call=types.FunctionCall(
                                    id=MIXED_CALL_ID, name="add_task", args={}
                                )
                            ),
                        ],
                    ),
                    actions=EventActions(),
                )
            ),
        },
    ]

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"events": vectors}, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(vectors)} event vectors to {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
