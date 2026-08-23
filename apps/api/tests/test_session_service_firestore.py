"""What the shared contract suite cannot see.

docs/08-testing.md: because we subclass rather than implement from scratch, three
assertions do not belong to the shared suite — the in-memory reference has no opinion on
them. Two of the three live here (the third, optional-item completion, is an M4 endpoint):

- **`seq` is gap-free and equals `revision`.** The invariant `?after_seq=` pagination
  rests on.
- **`StaleSessionError` still fires.** `append_event` reimplements the shipped
  transaction, so this is the check most likely to be dropped in a careless edit — and
  losing it corrupts state silently rather than loudly.

Plus the linkage fields, which are the subclass's other reason to exist.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from google.adk.errors import StaleSessionError
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.cloud.firestore import AsyncClient
from google.genai import types

from coach.adk_firestore import CoachSessionService
from coach.adk_firestore.session_service import SEQ_FIELD

APP = "coach"
USER = "u_alice"


def text_event(text: str, **actions: object) -> Event:
    return Event(
        invocation_id="inv_1",
        author="user",
        content=types.Content(role="user", parts=[types.Part(text=text)]),
        actions=EventActions(**actions),  # type: ignore[arg-type]
    )


@pytest.fixture
async def sessions(settings) -> AsyncIterator[CoachSessionService]:
    client = AsyncClient(
        project=settings.google_cloud_project, database=settings.firestore_database
    )
    yield CoachSessionService(
        client=client, root_collection=settings.adk_firestore_root_collection
    )


async def _raw_events(service: CoachSessionService, session_id: str) -> list[dict[str, object]]:
    """The event documents as stored, so `seq` can be read at the top level."""
    reference = (
        service._get_sessions_ref(APP, USER)
        .document(session_id)
        .collection(service.events_collection)
    )
    return [document.to_dict() or {} async for document in reference.stream()]


# --- seq -------------------------------------------------------------------------------


async def test_seq_is_one_to_n_with_no_holes(sessions: CoachSessionService) -> None:
    session = await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")

    for index in range(6):
        await sessions.append_event(session, text_event(f"message {index}"))

    stored = await _raw_events(sessions, "s_1")
    assert sorted(int(document[SEQ_FIELD]) for document in stored) == [1, 2, 3, 4, 5, 6]  # type: ignore[arg-type]


async def test_seq_equals_the_session_revision(sessions: CoachSessionService) -> None:
    """`seq` is derived from `revision`, so the two can never disagree — assert it.

    If a future ADK version increments `revision` for anything other than an appended
    event, `seq` silently develops gaps or duplicates. This is the assertion that turns
    that into a loud failure.
    """
    session = await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")
    for index in range(3):
        await sessions.append_event(session, text_event(f"message {index}"))

    document = await sessions._get_sessions_ref(APP, USER).document("s_1").get()
    stored = await _raw_events(sessions, "s_1")

    assert (document.to_dict() or {})["revision"] == max(
        int(event[SEQ_FIELD])
        for event in stored  # type: ignore[arg-type]
    )


async def test_seq_survives_an_interleaved_get_session(sessions: CoachSessionService) -> None:
    """A read between two appends must not perturb the sequence.

    `get_session` returns a session whose `_storage_update_marker` reflects storage, so
    appending through the reloaded handle continues the sequence rather than restarting
    or skipping it.
    """
    session = await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")
    await sessions.append_event(session, text_event("first"))

    reloaded = await sessions.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert reloaded is not None
    await sessions.append_event(reloaded, text_event("second"))

    stored = await _raw_events(sessions, "s_1")
    assert sorted(int(document[SEQ_FIELD]) for document in stored) == [1, 2]  # type: ignore[arg-type]


async def test_list_events_pages_by_seq(sessions: CoachSessionService) -> None:
    session = await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")
    for index in range(5):
        await sessions.append_event(session, text_event(f"message {index}"))

    first = await sessions.list_events(
        app_name=APP, user_id=USER, session_id="s_1", after_seq=0, limit=2
    )
    second = await sessions.list_events(
        app_name=APP, user_id=USER, session_id="s_1", after_seq=first[-1].seq, limit=10
    )

    assert [event.seq for event in first] == [1, 2]
    assert [event.seq for event in second] == [3, 4, 5]
    assert second[0].event_data["content"]["parts"][0]["text"] == "message 2"  # type: ignore[index,call-overload]


async def test_list_events_excludes_partial_events(sessions: CoachSessionService) -> None:
    session = await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")
    partial = text_event("half a sen")
    partial.partial = True

    await sessions.append_event(session, partial)
    await sessions.append_event(session, text_event("half a sentence"))

    events = await sessions.list_events(app_name=APP, user_id=USER, session_id="s_1")
    assert [event.seq for event in events] == [1]


# --- StaleSessionError -----------------------------------------------------------------


async def test_stale_session_error_still_fires(sessions: CoachSessionService) -> None:
    """Load a session twice, append on both; the second must raise.

    Losing this check does not break a test that only ever appends through one handle —
    it breaks concurrent writers, silently, by letting the loser overwrite the winner's
    session state.
    """
    await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")
    first = await sessions.get_session(app_name=APP, user_id=USER, session_id="s_1")
    second = await sessions.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert first is not None and second is not None

    await sessions.append_event(first, text_event("winner"))

    with pytest.raises(StaleSessionError):
        await sessions.append_event(second, text_event("loser"))


async def test_a_stale_append_writes_nothing(sessions: CoachSessionService) -> None:
    """The refusal is transactional: the loser's event must not be stored either."""
    await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")
    first = await sessions.get_session(app_name=APP, user_id=USER, session_id="s_1")
    second = await sessions.get_session(app_name=APP, user_id=USER, session_id="s_1")
    assert first is not None and second is not None

    await sessions.append_event(first, text_event("winner"))
    with pytest.raises(StaleSessionError):
        await sessions.append_event(second, text_event("loser"))

    stored = await _raw_events(sessions, "s_1")
    assert len(stored) == 1


async def test_concurrent_appends_through_one_handle_get_distinct_seqs(
    sessions: CoachSessionService,
) -> None:
    """Two appends racing on one session handle both land, with `seq` 1 and 2.

    Neither is stale, and that is the in-process lock's doing rather than luck: it
    serializes the pair, and the winner updates the shared handle's
    `_storage_update_marker` before releasing, so the loser's revision check passes
    against the value the winner just wrote. `StaleSessionError` is for a *second handle*
    (above) — a second instance, or a caller that reloaded.

    The invariant under test is the one `?after_seq=` depends on: two events must never
    share a `seq`. A lock that stopped serializing would show up here as a duplicate, not
    as an error.
    """
    session = await sessions.create_session(app_name=APP, user_id=USER, session_id="s_1")

    await asyncio.gather(
        sessions.append_event(session, text_event("a")),
        sessions.append_event(session, text_event("b")),
    )

    stored = await _raw_events(sessions, "s_1")
    assert sorted(int(document[SEQ_FIELD]) for document in stored) == [1, 2]  # type: ignore[arg-type]


# --- linkage ---------------------------------------------------------------------------


async def test_linkage_round_trips(sessions: CoachSessionService) -> None:
    await sessions.create_session(
        app_name=APP,
        user_id=USER,
        session_id="s_1",
        project_id="p_1",
        task_id="k_1",
    )

    linkage = await sessions.get_linkage(app_name=APP, user_id=USER, session_id="s_1")

    assert linkage is not None
    assert (linkage.project_id, linkage.task_id) == ("p_1", "k_1")


async def test_an_intake_session_has_a_null_task_id(sessions: CoachSessionService) -> None:
    """docs/04-api-contract.md: `POST /api/projects` creates a session with `taskId: null`."""
    await sessions.create_session(
        app_name=APP, user_id=USER, session_id="s_1", project_id="p_1", task_id=None
    )

    linkage = await sessions.get_linkage(app_name=APP, user_id=USER, session_id="s_1")

    assert linkage is not None
    assert linkage.project_id == "p_1"
    assert linkage.task_id is None


async def test_get_linkage_returns_none_for_an_unknown_session(
    sessions: CoachSessionService,
) -> None:
    assert await sessions.get_linkage(app_name=APP, user_id=USER, session_id="nope") is None


async def test_the_session_by_task_query_filters_on_exactly_one_field(
    sessions: CoachSessionService,
) -> None:
    """The declared index is single-field; a second `where` needs one nobody declared.

    This asserts the shape of the query rather than its result, which is unusual and is
    the point. The emulator does not enforce index requirements, so a composite
    collection-group query passes every functional test here and then fails in a deployed
    environment with `FAILED_PRECONDITION` — which is exactly how this reached a real
    Cloud Run revision once already. The result-level tests below cannot see it; only the
    filter count can.

    The index is `google_firestore_field.sessions_task_id` in
    `infra/terraform/modules/firestore/main.tf`, `COLLECTION_GROUP` scope, one field. If
    a filter genuinely has to be added here, add the composite index in the same change —
    and a row to docs/02-data-model.md#indexes — rather than relaxing this test.
    """
    captured: list[object] = []

    class _Recorder:
        def where(self, *, filter: object) -> _Recorder:
            captured.append(filter)
            return self

        def limit(self, _count: int) -> _Recorder:
            return self

        async def stream(self):  # type: ignore[no-untyped-def]
            return
            yield  # pragma: no cover - makes this an async generator

    original = sessions.client.collection_group
    sessions.client.collection_group = lambda _name: _Recorder()  # type: ignore[method-assign]
    try:
        await sessions.find_session_id_for_task(app_name=APP, task_id="k_1")
    finally:
        sessions.client.collection_group = original  # type: ignore[method-assign]

    assert len(captured) == 1


async def test_find_session_id_for_task(sessions: CoachSessionService) -> None:
    await sessions.create_session(
        app_name=APP, user_id=USER, session_id="s_1", project_id="p_1", task_id="k_1"
    )
    await sessions.create_session(
        app_name=APP, user_id="u_mallory", session_id="s_2", project_id="p_2", task_id="k_2"
    )

    assert await sessions.find_session_id_for_task(app_name=APP, task_id="k_1") == "s_1"
    assert await sessions.find_session_id_for_task(app_name=APP, task_id="k_2") == "s_2"
    assert await sessions.find_session_id_for_task(app_name=APP, task_id="k_9") is None


async def test_create_research_session_round_trips_kind_and_run_id(
    sessions: CoachSessionService,
) -> None:
    """+ M8: `kind`/`runId`, added so a research session can be told apart from the task's
    own conversation even though both carry the same `taskId`."""
    await sessions.create_research_session(
        app_name=APP,
        user_id=USER,
        session_id="s_1",
        project_id="p_1",
        task_id="k_1",
        run_id="r_1",
    )

    linkage = await sessions.get_linkage(app_name=APP, user_id=USER, session_id="s_1")

    assert linkage is not None
    assert linkage.kind == "research"
    assert linkage.run_id == "r_1"
    assert (linkage.project_id, linkage.task_id) == ("p_1", "k_1")


async def test_an_ordinary_session_has_kind_coach(sessions: CoachSessionService) -> None:
    await sessions.create_session(
        app_name=APP, user_id=USER, session_id="s_1", project_id="p_1", task_id="k_1"
    )

    linkage = await sessions.get_linkage(app_name=APP, user_id=USER, session_id="s_1")

    assert linkage is not None
    assert linkage.kind == "coach"
    assert linkage.run_id is None


async def test_find_session_id_for_task_skips_research_sessions(
    sessions: CoachSessionService,
) -> None:
    """The bug M8 would otherwise reintroduce: a task researched more than once has
    several research sessions sharing its `taskId`, and this query must still resolve to
    the one real conversation rather than an arbitrary research session."""
    await sessions.create_research_session(
        app_name=APP,
        user_id=USER,
        session_id="s_research_1",
        project_id="p_1",
        task_id="k_1",
        run_id="r_1",
    )
    await sessions.create_session(
        app_name=APP, user_id=USER, session_id="s_conversation", project_id="p_1", task_id="k_1"
    )
    await sessions.create_research_session(
        app_name=APP,
        user_id=USER,
        session_id="s_research_2",
        project_id="p_1",
        task_id="k_1",
        run_id="r_2",
    )

    assert (
        await sessions.find_session_id_for_task(app_name=APP, task_id="k_1") == "s_conversation"
    )


async def test_find_intake_session_id_skips_a_taskless_research_session(
    sessions: CoachSessionService,
) -> None:
    """A project-scoped research session also has `taskId: null`, on the same footing as
    the intake conversation — `kind` is what keeps this query from returning one instead
    of the other."""
    await sessions.create_research_session(
        app_name=APP, user_id=USER, session_id="s_research", project_id="p_1", run_id="r_1"
    )
    await sessions.create_session(
        app_name=APP, user_id=USER, session_id="s_intake", project_id="p_1", task_id=None
    )

    assert await sessions.find_intake_session_id(app_name=APP, project_id="p_1") == "s_intake"


async def test_linkage_survives_an_append(sessions: CoachSessionService) -> None:
    """`append_event` rewrites the session document; the linkage must not be a casualty."""
    session = await sessions.create_session(
        app_name=APP, user_id=USER, session_id="s_1", project_id="p_1", task_id="k_1"
    )

    await sessions.append_event(session, text_event("hello"))

    linkage = await sessions.get_linkage(app_name=APP, user_id=USER, session_id="s_1")
    assert linkage is not None
    assert (linkage.project_id, linkage.task_id) == ("p_1", "k_1")
