"""Sessions, tickets, uploads, and presence — the M2 REST surface.

docs/04-api-contract.md. The streaming behaviour these endpoints exist to start is
covered in `test_streaming.py`; what is asserted here is the contract around it —
ownership, get-or-create, paging, and the two places the design makes a deliberate
security trade.
"""

from __future__ import annotations

import pytest

from coach.repositories.tickets import TICKET_TTL
from coach.services.uploads import ACCEPTED_MIME_TYPES, MAX_UPLOAD_BYTES
from streaming_doubles import ScriptedModel  # noqa: F401  (imported for the fixture's type)


async def _task(client) -> tuple[str, str]:
    project = (await client.post("/api/projects", json={"title": "Rust"})).json()
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Ownership", "estimatedMinutes": 45},
        )
    ).json()["task"]
    return str(project["id"]), str(task["id"])


# --- task sessions ---------------------------------------------------------------------


async def test_a_task_session_is_created_once_and_then_returned(client) -> None:
    """Get-or-create: every workspace open calls this, so twice must not be a conflict."""
    _project_id, task_id = await _task(client)

    first = await client.post(f"/api/tasks/{task_id}/session")
    second = await client.post(f"/api/tasks/{task_id}/session")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session"]["id"] == second.json()["session"]["id"]


async def test_the_session_is_linked_to_its_task_and_project(client) -> None:
    project_id, task_id = await _task(client)

    session = (await client.post(f"/api/tasks/{task_id}/session")).json()["session"]

    assert session["projectId"] == project_id
    assert session["taskId"] == task_id


async def test_the_task_document_learns_its_session_id(client) -> None:
    """`task.sessionId` is the cache; the collection-group query is the authority."""
    _project_id, task_id = await _task(client)

    session_id = (await client.post(f"/api/tasks/{task_id}/session")).json()["session"]["id"]

    task = (await client.get(f"/api/tasks/{task_id}")).json()["task"]
    assert task["sessionId"] == session_id


async def test_creating_a_project_opens_an_intake_session(client, container, alice) -> None:
    """docs/04-api-contract.md: `POST /api/projects` creates a session with `taskId: null`.

    There is no endpoint that lists a project's sessions, and inventing one for a test
    would be adding contract — so the assertion goes through the session service, which
    is the layer that owns the linkage.
    """
    from coach.agents.runner import APP_NAME

    project = (await client.post("/api/projects", json={"title": "Elixir"})).json()

    listed = await container.session_service.list_sessions(app_name=APP_NAME, user_id=alice.uid)
    linkages = [
        await container.session_service.get_linkage(
            app_name=APP_NAME, user_id=alice.uid, session_id=session.id
        )
        for session in listed.sessions
    ]
    intake = [
        linkage
        for linkage in linkages
        if linkage is not None
        and linkage.project_id == project["id"]
        and linkage.task_id is None
    ]

    assert len(intake) == 1


async def test_the_intake_session_is_readable_over_the_api(client, container, alice) -> None:
    project = (await client.post("/api/projects", json={"title": "Elixir"})).json()

    summary = await container.sessions.create_intake(alice, project["id"])
    fetched = (await client.get(f"/api/sessions/{summary.id}")).json()["session"]

    assert fetched["projectId"] == project["id"]
    assert fetched["taskId"] is None


async def test_another_users_session_is_not_found(client, other_client) -> None:
    """`NotFound`, not `Forbidden` — session ids must not be probeable."""
    _project_id, task_id = await _task(client)
    session_id = (await client.post(f"/api/tasks/{task_id}/session")).json()["session"]["id"]

    response = await other_client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 404


async def test_another_user_cannot_start_a_turn_on_your_session(
    client, other_client, scripted_model
) -> None:
    _project_id, task_id = await _task(client)
    session_id = (await client.post(f"/api/tasks/{task_id}/session")).json()["session"]["id"]

    response = await other_client.post(
        f"/api/sessions/{session_id}/turns", json={"text": "let me in"}
    )

    assert response.status_code == 404


# --- events ----------------------------------------------------------------------------


async def test_events_page_by_seq(client, container, session_id, scripted_model) -> None:
    from coach.services.models import TurnStatus

    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})
    turn_id = response.json()["turnId"]
    await _await_complete(container, turn_id, TurnStatus.COMPLETE)

    first = (await client.get(f"/api/sessions/{session_id}/events?after_seq=0&limit=1")).json()
    assert first["events"][0]["seq"] == 1
    assert first["hasMore"] is True
    assert first["nextAfterSeq"] == 1

    second = (
        await client.get(
            f"/api/sessions/{session_id}/events?after_seq={first['nextAfterSeq']}&limit=50"
        )
    ).json()
    assert all(event["seq"] > 1 for event in second["events"])


async def test_an_empty_page_echoes_the_cursor_back(client, session_id) -> None:
    """So a client can keep asking with the value it was given, page or no page."""
    page = (await client.get(f"/api/sessions/{session_id}/events?after_seq=99")).json()

    assert page["events"] == []
    assert page["nextAfterSeq"] == 99
    assert page["hasMore"] is False


# --- ws tickets ------------------------------------------------------------------------


async def test_a_ticket_is_issued_and_expires_within_a_minute(client) -> None:
    from datetime import datetime

    from coach.core.clock import now

    response = await client.post("/api/ws-ticket")

    assert response.status_code == 201
    body = response.json()
    assert body["ticket"]
    expires_at = datetime.fromisoformat(body["expiresAt"])
    assert expires_at <= now() + TICKET_TTL


async def test_a_ticket_is_single_use(client, container) -> None:
    """Two sockets racing on one ticket: exactly one gets in."""
    ticket = (await client.post("/api/ws-ticket")).json()["ticket"]

    assert await container.tickets.redeem(ticket) == "u_alice"
    assert await container.tickets.redeem(ticket) is None


async def test_an_unknown_ticket_redeems_to_nothing(container) -> None:
    assert await container.tickets.redeem("wst_not-a-real-ticket") is None


async def test_an_expired_ticket_is_refused(container, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    from coach.core.clock import now as real_now

    await container.tickets.issue("wst_expired", "u_alice")
    monkeypatch.setattr(
        "coach.repositories.tickets.now", lambda: real_now() + timedelta(minutes=5)
    )

    assert await container.tickets.redeem("wst_expired") is None


@pytest.mark.parametrize("bad_ticket", ["", "wst_nope"])
async def test_the_websocket_refuses_a_bad_ticket(app, bad_ticket: str) -> None:
    """Declined *before* `accept`, so an unauthenticated peer never holds an open socket.

    Driven against the fake socket rather than Starlette's `TestClient`, which runs the
    app on a second event loop in a portal thread. The Firestore `AsyncClient` caches a
    gRPC channel bound to the loop it was built on (see `pyproject.toml`'s
    `asyncio_default_fixture_loop_scope`), so a second loop reaches a channel that does
    not belong to it and the ticket read fails for a reason that has nothing to do with
    the ticket.
    """
    from coach.api.routers.ws import WS_POLICY_VIOLATION, websocket_endpoint
    from streaming_doubles import FakeWebSocket

    websocket = FakeWebSocket(app=app)

    await websocket_endpoint(websocket, ticket=bad_ticket)  # type: ignore[arg-type]

    assert websocket.accepted is False
    assert websocket.close_code == WS_POLICY_VIOLATION


async def test_the_websocket_accepts_a_valid_ticket(app, client) -> None:
    from coach.api.routers.ws import websocket_endpoint
    from streaming_doubles import FakeWebSocket

    ticket = (await client.post("/api/ws-ticket")).json()["ticket"]
    websocket = FakeWebSocket(app=app)
    websocket.disconnect()  # the client hangs up immediately; the handshake still ran

    await websocket_endpoint(websocket, ticket=ticket)  # type: ignore[arg-type]

    assert websocket.accepted is True
    assert websocket.close_code is None


# --- uploads ---------------------------------------------------------------------------


async def test_an_upload_returns_a_signed_url(client) -> None:
    response = await client.post(
        "/api/uploads",
        json={"filename": "screenshot.png", "mimeType": "image/png", "sizeBytes": 2048},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["uploadId"]
    # Same-origin under ENV=local, so a browser can actually PUT to it — see
    # `api/routers/local_storage.py`. A deployed environment returns a signed GCS URL.
    assert body["signedUrl"].startswith("/api/local-storage/")


@pytest.mark.parametrize("mime_type", ["application/zip", "text/html", "video/mp4"])
async def test_an_unaccepted_mime_type_is_refused(client, mime_type: str) -> None:
    response = await client.post(
        "/api/uploads",
        json={"filename": "x", "mimeType": mime_type, "sizeBytes": 10},
    )

    assert response.status_code == 422
    assert mime_type not in ACCEPTED_MIME_TYPES


async def test_an_oversized_upload_is_refused(client) -> None:
    response = await client.post(
        "/api/uploads",
        json={
            "filename": "huge.pdf",
            "mimeType": "application/pdf",
            "sizeBytes": MAX_UPLOAD_BYTES + 1,
        },
    )

    assert response.status_code == 422


async def test_finalize_refuses_an_object_that_was_never_uploaded(client) -> None:
    """The signed URL is a promise, not a delivery — finalize is where that is checked."""
    upload_id = (
        await client.post(
            "/api/uploads",
            json={"filename": "a.png", "mimeType": "image/png", "sizeBytes": 10},
        )
    ).json()["uploadId"]

    response = await client.post(f"/api/uploads/{upload_id}/finalize")

    assert response.status_code == 422


async def test_finalize_uses_the_stored_content_type_not_the_declared_one(
    client, container
) -> None:
    """MIME is decided by what landed, never by what the client said it would send."""
    created = (
        await client.post(
            "/api/uploads",
            json={"filename": "a.png", "mimeType": "image/png", "sizeBytes": 10},
        )
    ).json()
    record = await container.upload_repository.get(created["uploadId"])
    container.uploads._store.declare(record["objectName"], 4096, "application/pdf")

    finalized = (await client.post(f"/api/uploads/{created['uploadId']}/finalize")).json()

    assert finalized["mimeType"] == "application/pdf"


async def test_finalize_registers_the_upload_as_an_adk_artifact(
    client, container, alice
) -> None:
    """The contract's third clause, and the one with teeth.

    The staging bucket deletes every object at one day of age, so an upload that is only
    verified — never copied — stops resolving a day later, silently, long after the
    conversation that produced it.
    """
    from coach.agents.runner import APP_NAME

    upload_id = await _staged_upload(client, container, b"screenshot-bytes")

    await client.post(f"/api/uploads/{upload_id}/finalize")

    stored = await container.artifacts().load_artifact(
        app_name=APP_NAME, user_id=alice.uid, filename=f"user:{upload_id}"
    )
    assert stored is not None
    assert stored.inline_data is not None
    assert stored.inline_data.data == b"screenshot-bytes"


async def test_a_finalized_upload_resolves_to_the_artifact_not_the_staging_object(
    client, container, alice
) -> None:
    """The regression this whole path exists to prevent.

    `objectName` is retained on the record for provenance, so the failure mode is a URI
    that still *looks* plausible — hence an assertion on the staging bucket name rather
    than only on the artifact one.
    """
    upload_id = await _staged_upload(client, container)
    await client.post(f"/api/uploads/{upload_id}/finalize")

    resolved = await container.uploads.resolve(alice, upload_id)

    record = await container.upload_repository.get(upload_id)
    assert record["objectName"] not in resolved.uri
    assert container.uploads._store.bucket not in resolved.uri
    assert upload_id in resolved.uri


async def test_the_artifact_uri_matches_the_shipped_gcs_blob_layout() -> None:
    """Pins the one place this project restates an ADK internal.

    `artifact_part_uri` reads the blob path out of `GcsArtifactService._get_blob_name`
    instead of formatting it here, so an upstream *rename* is an immediate error and an
    upstream *format change* is picked up silently and correctly. This test is what makes
    the first of those true without a deploy: it fails at the `getattr`, naming the
    method, rather than at a model that cannot see an attachment.
    docs/03-agent-design.md#bumping-the-adk-version
    """
    from google.adk.artifacts.gcs_artifact_service import GcsArtifactService

    from coach.integrations.artifacts import artifact_part_uri

    # Constructed without touching GCS: `_get_blob_name` is pure string work, and the
    # client built in __init__ is never used by it.
    service = GcsArtifactService.__new__(GcsArtifactService)
    service.bucket_name = "coach-dev-coach-artifacts"

    uri = artifact_part_uri(
        service, app_name="coach", user_id="u_alice", filename="user:up_1", version=0
    )

    assert uri == "gs://coach-dev-coach-artifacts/coach/u_alice/user/user:up_1/0"


async def _staged_upload(client, container, content: bytes = b"x") -> str:
    """Create an upload and pretend the browser's PUT landed."""
    created = (
        await client.post(
            "/api/uploads",
            json={
                "filename": "shot.png",
                "mimeType": "image/png",
                "sizeBytes": max(len(content), 1),
            },
        )
    ).json()
    record = await container.upload_repository.get(created["uploadId"])
    container.uploads._store.declare(record["objectName"], len(content), "image/png", content)
    return str(created["uploadId"])


async def test_finalize_rejects_an_object_whose_real_type_is_not_accepted(
    client, container
) -> None:
    created = (
        await client.post(
            "/api/uploads",
            json={"filename": "a.png", "mimeType": "image/png", "sizeBytes": 10},
        )
    ).json()
    record = await container.upload_repository.get(created["uploadId"])
    container.uploads._store.declare(record["objectName"], 4096, "application/x-msdownload")

    response = await client.post(f"/api/uploads/{created['uploadId']}/finalize")

    assert response.status_code == 422


async def test_another_users_upload_is_not_found(client, other_client) -> None:
    upload_id = (
        await client.post(
            "/api/uploads",
            json={"filename": "a.png", "mimeType": "image/png", "sizeBytes": 10},
        )
    ).json()["uploadId"]

    response = await other_client.post(f"/api/uploads/{upload_id}/finalize")

    assert response.status_code == 404


async def test_an_unfinalized_upload_cannot_be_attached_to_a_turn(
    client, session_id, scripted_model
) -> None:
    """Otherwise the model gets a `gs://` URI nobody has checked the size or type of."""
    upload_id = (
        await client.post(
            "/api/uploads",
            json={"filename": "a.png", "mimeType": "image/png", "sizeBytes": 10},
        )
    ).json()["uploadId"]

    response = await client.post(
        f"/api/sessions/{session_id}/turns",
        json={
            "text": "what do you think?",
            "attachments": [{"uploadId": upload_id, "mimeType": "image/png"}],
        },
    )

    assert response.status_code == 422


async def test_a_turn_needs_text_or_an_attachment(client, session_id, scripted_model) -> None:
    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "   "})

    assert response.status_code == 422


# --- presence --------------------------------------------------------------------------


async def test_a_presence_heartbeat_is_recorded(container, alice) -> None:
    """Written from M2, read by the autonomous tick at M5."""
    await container.presence_repository.heartbeat(alice.uid, project_id="p_1", task_id="k_1")

    presence = await container.presence_repository.get(alice.uid)

    assert presence is not None
    assert presence.active_project_id == "p_1"
    assert presence.last_heartbeat_at is not None


async def test_connection_counting_survives_several_tabs(container, alice) -> None:
    """One tab closing must not read as the user leaving."""
    await container.presence_repository.connected(alice.uid)
    await container.presence_repository.connected(alice.uid)
    await container.presence_repository.disconnected(alice.uid)

    presence = await container.presence_repository.get(alice.uid)

    assert presence is not None
    assert presence.connections == 1


# --- helpers ---------------------------------------------------------------------------


async def _await_complete(container, turn_id: str, status, timeout: float = 15.0) -> None:
    import asyncio

    async def _poll() -> None:
        while True:
            turn = await container.turn_repository.get(turn_id)
            if turn is not None and turn.status is status:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout=timeout)
