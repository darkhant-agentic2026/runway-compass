"""The memory-service contract suite.

docs/08-testing.md#session-service-contract-suite:
> Same approach for `CoachMemoryService` against `InMemoryMemoryService`.

Pairing our implementation against ADK's own reference is what keeps the two
behaviourally identical across ADK versions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.sessions.session import Session
from google.cloud.firestore import AsyncClient
from google.genai import types

from coach.adk_firestore import CoachMemoryService

APP = "coach"
USER = "u_alice"
USER_BOB = "u_bob"


def text_event(author: str, text: str, **actions: object) -> Event:
    return Event(
        invocation_id="inv_1",
        author=author,
        content=types.Content(role=author, parts=[types.Part(text=text)]),
        actions=EventActions(**actions),  # type: ignore[arg-type]
    )


@pytest.fixture(params=["in_memory", "coach_firestore"])
async def service(request: pytest.FixtureRequest, settings) -> AsyncIterator[BaseMemoryService]:
    """The suite's subject: ADK's reference, then ours, with identical expectations."""
    if request.param == "in_memory":
        yield InMemoryMemoryService()
        return
    client = AsyncClient(
        project=settings.google_cloud_project, database=settings.firestore_database
    )
    yield CoachMemoryService(client=client)


# --- add_session_to_memory & search_memory --------------------------------------------


async def test_search_memory_finds_event_by_keyword(service: BaseMemoryService) -> None:
    session = Session(
        id="s_1",
        app_name=APP,
        user_id=USER,
        events=[
            text_event("user", "We implemented Dijkstra shortest path algorithm in Python."),
            text_event("model", "Great job! The priority queue implementation was efficient."),
        ],
    )
    await service.add_session_to_memory(session)

    response = await service.search_memory(app_name=APP, user_id=USER, query="dijkstra")
    assert len(response.memories) >= 1
    texts = [
        " ".join([part.text for part in m.content.parts if part.text])
        for m in response.memories
        if m.content and m.content.parts
    ]
    assert any("Dijkstra" in t for t in texts)


async def test_search_memory_returns_empty_when_no_match(service: BaseMemoryService) -> None:
    session = Session(
        id="s_2",
        app_name=APP,
        user_id=USER,
        events=[text_event("user", "Learning asynchronous programming with Python asyncio.")],
    )
    await service.add_session_to_memory(session)

    response = await service.search_memory(
        app_name=APP, user_id=USER, query="kubernetes docker"
    )
    assert response.memories == []


async def test_search_memory_enforces_user_isolation(service: BaseMemoryService) -> None:
    session_alice = Session(
        id="s_alice",
        app_name=APP,
        user_id=USER,
        events=[text_event("user", "Alice secret topic is quantum computing.")],
    )
    await service.add_session_to_memory(session_alice)

    # Bob searches for the same keyword
    response_bob = await service.search_memory(app_name=APP, user_id=USER_BOB, query="quantum")
    assert response_bob.memories == []

    # Alice searches and finds it
    response_alice = await service.search_memory(app_name=APP, user_id=USER, query="quantum")
    assert len(response_alice.memories) == 1


async def test_search_memory_enforces_app_isolation(service: BaseMemoryService) -> None:
    session = Session(
        id="s_app",
        app_name=APP,
        user_id=USER,
        events=[text_event("user", "Rust memory safety and borrow checker.")],
    )
    await service.add_session_to_memory(session)

    response_other = await service.search_memory(
        app_name="other_app", user_id=USER, query="rust"
    )
    assert response_other.memories == []


async def test_search_memory_deduplicates_results_for_multi_word_queries(
    service: BaseMemoryService,
) -> None:
    session = Session(
        id="s_multi",
        app_name=APP,
        user_id=USER,
        events=[
            text_event("user", "Concurrency in Python using asyncio and trio frameworks."),
        ],
    )
    await service.add_session_to_memory(session)

    # Both "python" and "asyncio" match the same event
    response = await service.search_memory(
        app_name=APP, user_id=USER, query="python asyncio concurrency"
    )
    assert len(response.memories) == 1


async def test_add_events_to_memory_and_search(service: BaseMemoryService) -> None:
    events = [
        text_event("user", "Studied binary search tree rotations."),
        text_event("model", "AVL trees maintain balance with rotations."),
    ]
    await service.add_events_to_memory(
        app_name=APP,
        user_id=USER,
        events=events,
        session_id="s_tree",
    )

    response = await service.search_memory(app_name=APP, user_id=USER, query="rotations")
    assert len(response.memories) >= 1
