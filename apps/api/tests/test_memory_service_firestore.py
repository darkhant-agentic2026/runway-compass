"""Firestore-specific tests for `CoachMemoryService`.

docs/02-data-model.md#collection-map:
> users/{uid}/memories/{memoryId}  ← CoachMemoryService

docs/03-agent-design.md#coachmemoryservicefirestorememoryservice:
> The subclass adds only per-user collection placement (`users/{uid}/memories/{memoryId}`)
> and the `sourceSessionId` / `projectId` fields the UI attributes memories by.
"""

from __future__ import annotations

import pytest
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.cloud.firestore import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter
from google.genai import types

from coach.adk_firestore import CoachMemoryService

APP = "coach"
USER = "u_alice"


def text_event(author: str, text: str, **actions: object) -> Event:
    return Event(
        invocation_id="inv_1",
        author=author,
        content=types.Content(role=author, parts=[types.Part(text=text)]),
        actions=EventActions(**actions),  # type: ignore[arg-type]
    )


@pytest.fixture
async def memory_service(settings) -> CoachMemoryService:
    client = AsyncClient(
        project=settings.google_cloud_project, database=settings.firestore_database
    )
    return CoachMemoryService(client=client)


async def test_memories_are_stored_under_user_subcollection(
    memory_service: CoachMemoryService, settings
) -> None:
    session = Session(
        id="s_unique_123",
        app_name=APP,
        user_id=USER,
        state={"projectId": "p_debug"},
        events=[text_event("user", "Debugging memory leaks with tracemalloc.")],
    )
    await memory_service.add_session_to_memory(session)

    # Inspect Firestore directly to verify document placement
    client = AsyncClient(
        project=settings.google_cloud_project, database=settings.firestore_database
    )
    memories_coll = client.collection("users").document(USER).collection("memories")
    query = memories_coll.where(filter=FieldFilter("sourceSessionId", "==", "s_unique_123"))
    docs = [doc async for doc in query.stream()]
    assert len(docs) == 1
    data = docs[0].to_dict()
    assert data is not None
    assert data["appName"] == APP
    assert data["userId"] == USER
    assert data["sourceSessionId"] == "s_unique_123"
    assert data["projectId"] == "p_debug"
    assert "tracemalloc" in data["keywords"]
    assert "leaks" in data["keywords"]


async def test_add_memory_with_tags_and_metadata(
    memory_service: CoachMemoryService, settings
) -> None:
    entry = MemoryEntry(
        content=types.Content(
            role="user", parts=[types.Part(text="Learned about Raft consensus")]
        ),
        author="user",
        custom_metadata={
            "tags": ["distributed-systems", "raft"],
            "sourceSessionId": "s_raft",
            "projectId": "p_raft",
        },
    )
    await memory_service.add_memory(
        app_name=APP,
        user_id=USER,
        memories=[entry],
    )

    response = await memory_service.search_memory(app_name=APP, user_id=USER, query="raft")
    assert len(response.memories) == 1
    found = response.memories[0]
    assert found.custom_metadata.get("sourceSessionId") == "s_raft"
    assert found.custom_metadata.get("projectId") == "p_raft"
