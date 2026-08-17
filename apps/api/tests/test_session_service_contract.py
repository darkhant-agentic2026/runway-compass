"""The session-service contract suite.

docs/08-testing.md#session-service-contract-suite: one parametrized suite executed
against **both** `InMemorySessionService` and our `CoachSessionService`, asserting
identical behaviour.

Pairing our implementation against ADK's own reference is what keeps the two
behaviourally identical. If a semantic moves between pinned versions, the shared suite
fails on the in-memory side first and *names* what changed — which is more useful than
our side failing alone, where the natural reading is "our subclass is broken."

This is the gate for an ADK version bump
(docs/03-agent-design.md#bumping-the-adk-version).

Three assertions deliberately live elsewhere, in `test_session_service_firestore.py`: the
in-memory reference has no opinion on `seq`, on `StaleSessionError`, or on the linkage
fields, so asserting them here would mean a suite that is only half shared.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from google.adk.errors import StaleSessionError
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.sessions.base_session_service import BaseSessionService, GetSessionConfig
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.cloud.firestore import AsyncClient
from google.genai import types

from coach.adk_firestore import CoachSessionService

APP = "coach"
USER = "u_alice"


def text_event(author: str, text: str, **actions: object) -> Event:
    return Event(
        invocation_id="inv_1",
        author=author,
        content=types.Content(role=author, parts=[types.Part(text=text)]),
        actions=EventActions(**actions),  # type: ignore[arg-type]
    )


@pytest.fixture(params=["in_memory", "coach_firestore"])
async def service(
    request: pytest.FixtureRequest, settings
) -> AsyncIterator[BaseSessionService]:
    """The suite's subject: ADK's reference, then ours, with identical expectations."""
    if request.param == "in_memory":
        yield InMemorySessionService()
        return
    client = AsyncClient(
        project=settings.google_cloud_project, database=settings.firestore_database
    )
    yield CoachSessionService(
        client=client, root_collection=settings.adk_firestore_root_collection
    )


# --- create / get / list / delete ------------------------------------------------------


async def test_create_returns_a_session_with_the_requested_id(
    service: BaseSessionService,
) -> None:
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")

    assert session.id == "s_1"
    assert session.app_name == APP
    assert session.user_id == USER
    assert session.events == []


async def test_create_generates_an_id_when_none_is_given(service: BaseSessionService) -> None:
    session = await service.create_session(app_name=APP, user_id=USER)

    assert session.id


async def test_get_returns_none_for_an_unknown_session(service: BaseSessionService) -> None:
    assert await service.get_session(app_name=APP, user_id=USER, session_id="nope") is None


async def test_get_returns_none_for_another_users_session(service: BaseSessionService) -> None:
    """Isolation is by `(app, user)` in the storage path, not by a filter we could forget."""
    await service.create_session(app_name=APP, user_id=USER, session_id="s_1")

    assert (
        await service.get_session(app_name=APP, user_id="u_mallory", session_id="s_1") is None
    )


async def test_initial_state_round_trips(service: BaseSessionService) -> None:
    await service.create_session(
        app_name=APP, user_id=USER, session_id="s_1", state={"topic": "asyncio"}
    )

    session = await service.get_session(app_name=APP, user_id=USER, session_id="s_1")

    assert session is not None
    assert session.state["topic"] == "asyncio"


async def test_list_sessions_returns_the_users_sessions(service: BaseSessionService) -> None:
    await service.create_session(app_name=APP, user_id=USER, session_id="s_1")
    await service.create_session(app_name=APP, user_id=USER, session_id="s_2")
    await service.create_session(app_name=APP, user_id="u_mallory", session_id="s_3")

    listed = await service.list_sessions(app_name=APP, user_id=USER)

    assert {session.id for session in listed.sessions} == {"s_1", "s_2"}


async def test_delete_removes_the_session_and_its_events(service: BaseSessionService) -> None:
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")
    await service.append_event(session, text_event("user", "hello"))

    await service.delete_session(app_name=APP, user_id=USER, session_id="s_1")

    assert await service.get_session(app_name=APP, user_id=USER, session_id="s_1") is None


# --- append_event: state-delta scoping -------------------------------------------------


async def test_session_scoped_delta_lands_on_the_session(service: BaseSessionService) -> None:
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")

    await service.append_event(
        session, text_event("agent", "noted", state_delta={"last_topic": "locks"})
    )

    reloaded = await service.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert reloaded is not None
    assert reloaded.state["last_topic"] == "locks"


async def test_user_scoped_delta_is_visible_from_a_second_session(
    service: BaseSessionService,
) -> None:
    """`user:` state is the cross-session layer; a new session must see it."""
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")

    await service.append_event(
        session, text_event("agent", "noted", state_delta={"user:pace": "steady"})
    )

    other = await service.create_session(app_name=APP, user_id=USER, session_id="s_2")
    reloaded = await service.get_session(app_name=APP, user_id=USER, session_id=other.id)
    assert reloaded is not None
    assert reloaded.state["user:pace"] == "steady"


async def test_app_scoped_delta_is_visible_to_another_user(service: BaseSessionService) -> None:
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")

    await service.append_event(
        session, text_event("agent", "noted", state_delta={"app:banner": "v2"})
    )

    await service.create_session(app_name=APP, user_id="u_mallory", session_id="s_9")
    reloaded = await service.get_session(app_name=APP, user_id="u_mallory", session_id="s_9")
    assert reloaded is not None
    assert reloaded.state["app:banner"] == "v2"


async def test_temp_delta_is_readable_in_memory_but_never_persisted(
    service: BaseSessionService,
) -> None:
    """`temp:` is invocation scratch space: applied in memory, trimmed before the write."""
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")

    await service.append_event(
        session, text_event("agent", "thinking", state_delta={"temp:draft": "unsaved"})
    )

    assert session.state["temp:draft"] == "unsaved"
    reloaded = await service.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert reloaded is not None
    assert "temp:draft" not in reloaded.state


async def test_partial_events_are_not_persisted(service: BaseSessionService) -> None:
    """Only finalized events reach storage — this is what keeps write costs bounded."""
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")
    partial = text_event("agent", "half a sen")
    partial.partial = True

    await service.append_event(session, partial)

    reloaded = await service.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert reloaded is not None
    assert reloaded.events == []


# --- get_session bounded history -------------------------------------------------------


async def test_num_recent_events_truncates_to_the_most_recent(
    service: BaseSessionService,
) -> None:
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")
    for index in range(5):
        await service.append_event(session, text_event("user", f"message {index}"))

    reloaded = await service.get_session(
        app_name=APP,
        user_id=USER,
        session_id="s_1",
        config=GetSessionConfig(num_recent_events=2),
    )

    assert reloaded is not None
    assert [event.content.parts[0].text for event in reloaded.events] == [  # type: ignore[union-attr,index]
        "message 3",
        "message 4",
    ]


async def test_num_recent_events_zero_returns_no_events(service: BaseSessionService) -> None:
    """Callers use `0` to probe whether a session exists without paying for its history."""
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")
    await service.append_event(session, text_event("user", "hello"))

    reloaded = await service.get_session(
        app_name=APP,
        user_id=USER,
        session_id="s_1",
        config=GetSessionConfig(num_recent_events=0),
    )

    assert reloaded is not None
    assert reloaded.events == []


# --- get_user_state --------------------------------------------------------------------


async def test_get_user_state_without_a_session(service: BaseSessionService) -> None:
    """The prompt builder reads this at session start, before any session is loaded."""
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")
    await service.append_event(
        session,
        text_event("agent", "noted", state_delta={"user:pace": "steady", "session_only": "x"}),
    )

    state = await service.get_user_state(app_name=APP, user_id=USER)

    # Un-prefixed keys, and session-scoped keys are not user state.
    assert state["pace"] == "steady"
    assert "session_only" not in state


async def test_get_user_state_is_empty_for_an_unknown_user(service: BaseSessionService) -> None:
    assert await service.get_user_state(app_name=APP, user_id="u_nobody") == {}


async def test_flush_is_a_no_op(service: BaseSessionService) -> None:
    """Neither implementation buffers; the assertion is that calling it is safe."""
    session = await service.create_session(app_name=APP, user_id=USER, session_id="s_1")
    await service.append_event(session, text_event("user", "hello"))

    await service.flush()

    reloaded = await service.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert reloaded is not None
    assert len(reloaded.events) == 1


# --- ordering under concurrency --------------------------------------------------------


async def test_concurrent_appends_lose_nothing(service: BaseSessionService) -> None:
    """Six appends issued at once end up as six stored events, in both implementations.

    The concurrency is the in-process kind — `asyncio.gather` on one loop — which is
    exactly what a turn's tool calls and a REST mutation do to one session. The
    reload-append-retry shape is `Runner`'s: our side raises `StaleSessionError` when the
    in-hand session has been superseded, so a caller holding a stale handle is meant to
    reload rather than clobber. The in-memory reference never raises it, which is why the
    retry is written to tolerate rather than to expect it — and why the raising itself is
    asserted separately, against the emulator.

    What is shared is the outcome: nothing is lost, nothing is duplicated.
    """
    await service.create_session(app_name=APP, user_id=USER, session_id="s_1")

    async def append(index: int) -> None:
        for _ in range(20):
            session = await service.get_session(app_name=APP, user_id=USER, session_id="s_1")
            assert session is not None
            try:
                await service.append_event(session, text_event("user", f"message {index}"))
            except StaleSessionError:
                await asyncio.sleep(0)
                continue
            return
        raise AssertionError(f"append {index} never won a round")

    await asyncio.gather(*(append(index) for index in range(6)))

    reloaded = await service.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert reloaded is not None
    texts = [event.content.parts[0].text for event in reloaded.events]  # type: ignore[union-attr,index]
    assert sorted(texts) == sorted(f"message {index}" for index in range(6))
