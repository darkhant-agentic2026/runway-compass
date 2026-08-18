"""`CoachSessionService` — ADK's shipped `FirestoreSessionService`, plus four things.

docs/03-agent-design.md#what-the-subclass-adds is the specification, and it is short on
purpose:

| Addition | Why |
| --- | --- |
| `seq` on each event doc | `?after_seq=` needs a stable cursor; `timestamp` is not one |
| `projectId` / `taskId` on the session doc | the board resolves a task to its session |
| `get_user_state()` | the shipped class inherits `BaseSessionService`'s `NotImplementedError` |
| `flush()` | the inherited no-op is correct, but is asserted rather than assumed |

**`append_event` reimplements the shipped transaction.** There is no extension point
inside it — the `seq` we persist is the `new_revision` the transaction already computes,
and that value exists only inside the closure. The body below therefore keeps the shipped
structure line for line (the `revision` check and its `StaleSessionError`, the
app/user/session state-delta split, the event document shape) and adds exactly two writes.

That copy is **the single most bump-sensitive thing in this project**. It is the first
item on the checklist in docs/03-agent-design.md#bumping-the-adk-version, and the order of
work there is: run the contract suite, then *diff* the newly installed
`firestore_session_service.py` against the version this was derived from, since the
contract suite cannot see a change to a transaction body that still behaves correctly for
everything except `seq`.

Derived from `google-adk==2.7.0`,
`google/adk/integrations/firestore/firestore_session_service.py`, `append_event`
(lines 478-595 of that file).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from google.adk.errors._stale_session_error import StaleSessionError
from google.adk.errors.session_not_found_error import SessionNotFoundError
from google.adk.events.event import Event
from google.adk.integrations.firestore.firestore_session_service import (
    FirestoreSessionService,
)
from google.adk.sessions import _session_util
from google.adk.sessions.session import Session
from google.adk.sessions.state import State
from google.cloud import firestore

#: Verbatim from the shipped module, which does not export it.
_STALE_SESSION_ERROR_MESSAGE = (
    "The session has been modified in storage since it was loaded. "
    "Please reload the session before appending more events."
)

#: The two linkage fields the subclass adds to the session document
#: (docs/02-data-model.md#sessions--events-adk-owned-layout).
PROJECT_ID_FIELD = "projectId"
TASK_ID_FIELD = "taskId"

#: The per-session event sequence. Top-level on the event document, alongside `timestamp`
#: — anything nested under `event_data` is read-back-only and cannot be ordered on.
SEQ_FIELD = "seq"

#: How many of a project's sessions `find_intake_session_id` will scan before giving up.
#: The intake session is one document among "one per task plus one", and the `taskId`
#: check runs in Python, so the limit has to clear a realistic project rather than be 1.
#: A project past this many tasks would re-create its intake session; the ceiling is
#: chosen to be far outside that, not to be exact.
_INTAKE_SCAN_LIMIT = 200


@dataclass(frozen=True, slots=True)
class SessionLinkage:
    """What a session is *about*.

    `task_id` is `None` for a project intake session, which is the shape
    docs/04-api-contract.md gives `POST /api/projects`.
    """

    session_id: str
    project_id: str | None
    task_id: str | None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One row of `GET /api/sessions/{sid}/events`.

    The transcript is served from the stored documents rather than from rehydrated
    `Event` models: the endpoint pages by `seq`, and `seq` is a property of the document,
    not of the ADK event nested inside it.
    """

    seq: int
    event_id: str
    event_data: dict[str, Any]


class CoachSessionService(FirestoreSessionService):
    """The project's session service. See the module docstring."""

    # --- create: linkage ---------------------------------------------------------------

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> Session:
        """Create a session, optionally linked to a project and a task.

        The linkage is merged onto the document after the shipped transaction rather than
        written inside it. That costs one extra write on a once-per-task operation and
        keeps `create_session` inherited, which is worth more: the shipped body reads
        app and user state and raises `AlreadyExistsError`, none of which we want to
        restate. The merge does not touch `revision`, so the returned session's
        `_storage_update_marker` stays correct.
        """
        session = await super().create_session(
            app_name=app_name, user_id=user_id, state=state, session_id=session_id
        )
        if project_id is not None or task_id is not None:
            reference = self._get_sessions_ref(app_name, user_id).document(session.id)
            await reference.set(
                {PROJECT_ID_FIELD: project_id, TASK_ID_FIELD: task_id}, merge=True
            )
        return session

    async def get_linkage(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> SessionLinkage | None:
        """The session's project/task linkage, without loading its events.

        `GET /api/sessions/{sid}` returns this, and every ownership check on a session
        goes through it — which is why it reads the session document directly instead of
        `get_session`, whose default config would pull the whole transcript.
        """
        reference = self._get_sessions_ref(app_name, user_id).document(session_id)
        snapshot = await reference.get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        return SessionLinkage(
            session_id=session_id,
            project_id=data.get(PROJECT_ID_FIELD),
            task_id=data.get(TASK_ID_FIELD),
        )

    async def find_session_id_for_task(self, *, app_name: str, task_id: str) -> str | None:
        """Resolve a task to its session.

        Uses the `sessions` collection-group index on `taskId`
        (docs/02-data-model.md#indexes), which is what lets the task document hold a
        `sessionId` pointer without the reverse pointer having to be kept in step by hand:
        this query is the authority, `task.sessionId` is the cache.

        **Exactly one filter, and that is not incidental.** The declared index is
        single-field (`google_firestore_field.sessions_task_id`, `COLLECTION_GROUP`
        scope). Adding a second `where` — `appName`, say — turns this into a composite
        collection-group query that needs an index nobody declared, and real Firestore
        answers `FAILED_PRECONDITION` while the emulator answers correctly, so the
        failure appears only once deployed. `appName` is therefore checked in Python
        below: same guarantee, no second indexed field.
        """
        query = (
            self.client.collection_group(self.sessions_collection)
            .where(filter=firestore.FieldFilter(TASK_ID_FIELD, "==", task_id))
            .limit(2)
        )
        async for document in query.stream():
            data = document.to_dict() or {}
            if data.get("appName") != app_name:
                continue
            identifier = data.get("id")
            return cast(str | None, identifier) or document.id
        return None

    async def find_intake_session_id(self, *, app_name: str, project_id: str) -> str | None:
        """Resolve a project to its intake session — the one with `taskId: null`.

        `POST /api/projects` opens that session and nothing stores a pointer back to it,
        so this query is how the workspace finds the conversation again on a later visit.

        **One indexed filter, for the reason `find_session_id_for_task` spells out.** The
        obvious query here is two filters — `projectId == …` *and* `taskId == null` — and
        it works perfectly against the emulator, which does not enforce index
        requirements, while real Firestore answers a composite collection-group query with
        `FAILED_PRECONDITION` (docs/09-roadmap.md#what-a-green-local-run-does-not-prove).
        So `projectId` is the indexed filter and `taskId` is checked in Python, which
        needs the declared single-field index and no more.
        """
        query = (
            self.client.collection_group(self.sessions_collection)
            .where(filter=firestore.FieldFilter(PROJECT_ID_FIELD, "==", project_id))
            .limit(_INTAKE_SCAN_LIMIT)
        )
        async for document in query.stream():
            data = document.to_dict() or {}
            if data.get("appName") != app_name or data.get(TASK_ID_FIELD) is not None:
                continue
            identifier = data.get("id")
            return cast(str | None, identifier) or document.id
        return None

    # --- events: the `seq` cursor ------------------------------------------------------

    async def list_events(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        after_seq: int = 0,
        limit: int = 100,
    ) -> list[StoredEvent]:
        """Transcript page, ordered by `seq`, exclusive of `after_seq`.

        This is the read `?after_seq=` exists for. `timestamp` ordering alone is not a
        stable cursor — two events appended inside the same millisecond have no defined
        order and a client paging on timestamps would either skip one or replay it
        forever.
        """
        events_reference = (
            self._get_sessions_ref(app_name, user_id)
            .document(session_id)
            .collection(self.events_collection)
        )
        query = (
            events_reference.where(filter=firestore.FieldFilter(SEQ_FIELD, ">", after_seq))
            .order_by(SEQ_FIELD)
            .limit(limit)
        )
        stored: list[StoredEvent] = []
        async for document in query.stream():
            data = document.to_dict() or {}
            stored.append(
                StoredEvent(
                    seq=int(data.get(SEQ_FIELD, 0)),
                    event_id=document.id,
                    event_data=data.get("event_data") or {},
                )
            )
        return stored

    # --- user state --------------------------------------------------------------------

    async def get_user_state(self, *, app_name: str, user_id: str) -> dict[str, Any]:
        """Cross-session facts, without loading a session.

        The shipped class does not override this, so it inherits
        `BaseSessionService.get_user_state`'s `NotImplementedError`. The prompt builder
        reads user-scoped state at session start, and the alternative the base class
        suggests — enumerate sessions, then `get_session` each one — is a fan-out to get
        at one document.

        `user_states/{app}/users/{uid}` stores keys already stripped of the `user:`
        prefix (ADK's `extract_state_delta` does that on the way in), and the base class
        contract says the returned dict is un-prefixed. The prefix is stripped again here
        rather than assumed absent, so a document written by an older path cannot leak a
        `user:user:` key into the prompt.
        """
        reference = (
            self.client.collection(self.user_state_collection)
            .document(app_name)
            .collection("users")
            .document(user_id)
        )
        snapshot = await reference.get()
        if not snapshot.exists:
            return {}
        data = snapshot.to_dict() or {}
        return {key.removeprefix(State.USER_PREFIX): value for key, value in data.items()}

    async def flush(self) -> None:
        """No-op, like the base class — overridden so the contract suite covers it.

        This service writes every event inside `append_event`'s transaction and buffers
        nothing, so there is nothing to flush. Stating that explicitly is the point: if a
        future version starts buffering, this is where the flush goes, and the contract
        suite already calls it.
        """
        return None

    # --- append: the reimplemented transaction ------------------------------------------

    async def append_event(self, session: Session, event: Event) -> Event:
        """Append a finalized event, assigning it a gap-free per-session `seq`.

        The body is the shipped 2.7.0 transaction with two additions, both marked below.
        Everything else — including the early return on `event.partial`, the
        `_storage_update_marker` comparison that raises `StaleSessionError`, and the
        app/user/session split of `state_delta` — is deliberately unchanged, because
        `Runner` and the rest of ADK depend on those semantics.

        `seq` is `new_revision`, not a counter of our own. The session document's
        `revision` increments exactly once per appended event, inside the same
        transaction that writes the event, so it is *already* a gap-free per-session
        sequence; deriving `seq` from it means the two can never disagree. If a future
        ADK version increments `revision` for anything other than an appended event, this
        assumption breaks silently — hence the dedicated `seq`-is-gap-free test rather
        than trust.
        """
        if event.partial:
            return event

        self._apply_temp_state(session, event)
        event = self._trim_temp_delta_state(event)

        session_ref = self._get_sessions_ref(session.app_name, session.user_id).document(
            session.id
        )

        state_delta = (
            event.actions.state_delta if event.actions and event.actions.state_delta else {}
        )
        state_deltas = _session_util.extract_state_delta(state_delta)
        app_updates = state_deltas["app"]
        user_updates = state_deltas["user"]
        session_updates = state_deltas["session"]

        app_ref = self.client.collection(self.app_state_collection).document(session.app_name)
        user_ref = (
            self.client.collection(self.user_state_collection)
            .document(session.app_name)
            .collection("users")
            .document(session.user_id)
        )

        async with self._with_session_lock(
            app_name=session.app_name, user_id=session.user_id, session_id=session.id
        ):

            @firestore.async_transactional
            async def _append_txn(transaction: firestore.AsyncTransaction) -> int:
                # 1. Reads
                session_snap = await session_ref.get(transaction=transaction)
                if not session_snap.exists:
                    raise SessionNotFoundError(f"Session {session.id} not found.")

                session_doc = session_snap.to_dict() or {}
                if session_doc.get("status") == "DELETING":
                    raise ValueError(f"Session {session.id} is currently being deleted.")

                current_revision = session_doc.get("revision", 0)

                if (
                    session._storage_update_marker is not None
                    and session._storage_update_marker != str(current_revision)
                ):
                    raise StaleSessionError(_STALE_SESSION_ERROR_MESSAGE)

                app_snap = await app_ref.get(transaction=transaction) if app_updates else None
                user_snap = (
                    await user_ref.get(transaction=transaction) if user_updates else None
                )

                # 2. Writes
                if app_updates and app_snap is not None:
                    current_app = (app_snap.to_dict() or {}) if app_snap.exists else {}
                    current_app.update(app_updates)
                    transaction.set(app_ref, current_app, merge=True)

                if user_updates and user_snap is not None:
                    current_user = user_snap.to_dict() if user_snap.exists else {}
                    current_user.update(user_updates)
                    transaction.set(user_ref, current_user, merge=True)

                new_revision = current_revision + 1

                session_only_state = {
                    key: value
                    for key, value in session.state.items()
                    if not key.startswith(State.APP_PREFIX)
                    and not key.startswith(State.USER_PREFIX)
                    and not key.startswith(State.TEMP_PREFIX)
                }
                session_only_state.update(session_updates)
                transaction.update(
                    session_ref,
                    {
                        "state": json.dumps(session_only_state),
                        "updateTime": firestore.SERVER_TIMESTAMP,
                        "revision": new_revision,
                    },
                )

                event_ref = session_ref.collection(self.events_collection).document(event.id)
                event_data = event.model_dump(exclude_none=True, mode="json")
                transaction.set(
                    event_ref,
                    {
                        "event_data": event_data,
                        "timestamp": firestore.SERVER_TIMESTAMP,
                        "appName": session.app_name,
                        "userId": session.user_id,
                        # ---- ADDITION: the gap-free per-session cursor ----------------
                        SEQ_FIELD: new_revision,
                    },
                )

                return cast(int, new_revision)

            transaction_obj = self.client.transaction()
            new_revision_count = await _append_txn(transaction_obj)
            session._storage_update_marker = str(new_revision_count)
            session.last_update_time = event.timestamp

        # Skips FirestoreSessionService.append_event (which would re-run the whole
        # transaction) and goes straight to the base class, whose job is the in-memory
        # half: apply the state delta to `session.state` and append to `session.events`.
        await super(FirestoreSessionService, self).append_event(session, event)
        return event


__all__ = [
    "PROJECT_ID_FIELD",
    "SEQ_FIELD",
    "TASK_ID_FIELD",
    "CoachSessionService",
    "SessionLinkage",
    "StoredEvent",
]
