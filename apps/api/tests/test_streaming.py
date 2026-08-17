"""The disconnect matrix — the critical suite.

docs/08-testing.md#streaming-and-disconnect-resilience-the-critical-suite is a table of
nine rows; each has a test here, named after it. The requirement all nine serve is one
sentence from docs/04-api-contract.md: *generation must complete even if the client
disconnects, so inference is not wasted.*

What is real in these tests: `TurnService`, `TurnRegistry`, `StreamBroker`,
`CheckpointWriter`, `SocketSession`, `CoachSessionService`, and Firestore (the emulator).
What is faked: the model and the browser (`tests/streaming_doubles.py`). That ratio is
the point — a suite that stubbed the broker or the checkpoint writer would pass without
proving anything about the guarantee.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from coach.core.principal import Principal
from coach.services.models import TurnStatus
from coach.ws.manager import SocketSession
from streaming_doubles import FakeWebSocket, ScriptedModel


@pytest.fixture
def socket_for(container, alice: Principal):
    """Open a `SocketSession` against a fake browser, running as a background task."""

    class _Opener:
        def __init__(self) -> None:
            self.tasks: list[asyncio.Task[None]] = []

        def open(self, principal: Principal | None = None) -> FakeWebSocket:
            websocket = FakeWebSocket()
            session = SocketSession(
                websocket,  # type: ignore[arg-type]
                principal or alice,
                turns=container.turns,
                broker=container.broker,
                presence=container.presence_repository,
            )
            self.tasks.append(asyncio.create_task(session.run()))
            return websocket

    opener = _Opener()
    yield opener
    for task in opener.tasks:
        task.cancel()


@pytest.fixture
async def drain_turns(container) -> AsyncIterator[None]:
    """Make sure no detached generation task outlives its test.

    Without this, a turn started by one test can still be writing to Firestore while the
    next test's `_clean_database` fixture wipes it — which shows up as an unrelated
    failure somewhere else entirely.
    """
    yield
    await container.registry.drain(timeout=5.0)


pytestmark = pytest.mark.usefixtures("drain_turns")


async def _start_turn(client, session_id: str, text: str = "hello") -> str:
    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": text})
    assert response.status_code == 202, response.text
    return str(response.json()["turnId"])


# --- Happy path ------------------------------------------------------------------------


async def test_happy_path_delivers_every_seq_then_turn_complete(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for
) -> None:
    """Client receives `seq` 0…N with no gaps, then `turn_complete`."""
    websocket = socket_for.open()
    turn_id = await _start_turn(client, session_id)
    websocket.send({"type": "subscribe", "turnId": turn_id})

    await websocket.wait_for("turn_complete")

    assert websocket.text() == scripted_model.full_text
    sequences = websocket.seqs()
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert sequences == list(range(sequences[0], sequences[-1] + 1))


async def test_the_turn_reaches_complete_in_firestore(
    client, container, session_id: str, scripted_model: ScriptedModel, alice: Principal
) -> None:
    turn_id = await _start_turn(client, session_id)
    await _await_status(container, turn_id, TurnStatus.COMPLETE)

    turn = await container.turns.get(alice, turn_id)
    assert turn.status is TurnStatus.COMPLETE
    assert turn.ended_at is not None  # the TTL field; see docs/02-data-model.md#retention


async def test_the_finalized_events_land_in_the_session(
    client, container, session_id: str, scripted_model: ScriptedModel, alice: Principal
) -> None:
    """The transcript is ADK's to write; this asserts the runner actually wrote it."""
    turn_id = await _start_turn(client, session_id)
    await _await_status(container, turn_id, TurnStatus.COMPLETE)

    events = await container.sessions.list_events(alice, session_id)
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert any(
        scripted_model.full_text in str(event.event_data.get("content")) for event in events
    )


# --- Disconnect mid-generation ---------------------------------------------------------


async def test_disconnect_mid_generation_still_completes(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for, alice
) -> None:
    """Socket closed mid-stream; the turn still completes, and the model ran **once**.

    The model-invocation count is the assertion that distinguishes "generation survived
    the disconnect" from "generation was quietly restarted" — every other assertion in
    this test passes either way.
    """
    scripted_model.chunks = [f"chunk-{index} " for index in range(12)]
    scripted_model.delay = 0.02

    websocket = socket_for.open()
    turn_id = await _start_turn(client, session_id)
    websocket.send({"type": "subscribe", "turnId": turn_id})

    await websocket.wait_for_seq(3)
    websocket.disconnect()

    await _await_status(container, turn_id, TurnStatus.COMPLETE)
    turn = await container.turns.get(alice, turn_id)
    assert turn.status is TurnStatus.COMPLETE
    assert len(scripted_model.invocations) == 1

    events = await container.sessions.list_events(alice, session_id)
    assert any(
        scripted_model.full_text in str(event.event_data.get("content")) for event in events
    )


async def test_no_subscribers_at_all_still_completes_and_checkpoints(
    client, container, session_id: str, scripted_model: ScriptedModel, alice: Principal
) -> None:
    """Turn started, socket never opened → still completes and checkpoints.

    Zero subscribers is a normal state, not an edge case: a scheduled run has no socket
    at all (docs/04-api-contract.md).
    """
    scripted_model.chunks = ["a" * 200, "b" * 200, "c" * 200]
    turn_id = await _start_turn(client, session_id)

    await _await_status(container, turn_id, TurnStatus.COMPLETE)
    turn = await container.turns.get(alice, turn_id)

    assert turn.checkpoints
    assert "".join(slice_.text for slice_ in turn.checkpoints) == scripted_model.full_text


async def test_checkpoint_slices_describe_their_own_seq_range(
    client, container, session_id: str, scripted_model: ScriptedModel, alice: Principal
) -> None:
    """`sum(lengths) == len(text)` and one length per seq in `[fromSeq, toSeq]`.

    This is the invariant exact resume rests on — a slice whose `lengths` did not line up
    with its seq range would replay from the wrong offset, and the symptom would be a few
    duplicated or missing characters in the middle of a resumed message rather than an
    error.
    """
    scripted_model.chunks = [f"{index:03d}-" * 40 for index in range(6)]
    turn_id = await _start_turn(client, session_id)
    await _await_status(container, turn_id, TurnStatus.COMPLETE)

    turn = await container.turns.get(alice, turn_id)
    assert turn.checkpoints
    for slice_ in turn.checkpoints:
        assert sum(slice_.lengths) == len(slice_.text)
        assert len(slice_.lengths) == slice_.to_seq - slice_.from_seq + 1


# --- Resume ----------------------------------------------------------------------------


async def test_resume_same_instance_replays_without_duplicates_or_gaps(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for
) -> None:
    """Reconnect with `lastSeq=N` → replays N+1…M with no duplicates and no gaps."""
    scripted_model.chunks = [f"part{index} " for index in range(14)]
    scripted_model.delay = 0.02

    first = socket_for.open()
    turn_id = await _start_turn(client, session_id)
    first.send({"type": "subscribe", "turnId": turn_id})
    await first.wait_for_seq(4)
    seen = first.text()
    last_seq = max(first.seqs())
    first.disconnect()

    second = socket_for.open()
    second.send({"type": "resume", "turnId": turn_id, "lastSeq": last_seq})
    await second.wait_for("turn_complete")

    assert all(seq > last_seq for seq in second.seqs())
    assert seen + second.text() == scripted_model.full_text


async def test_resume_after_completion_replays_the_whole_turn(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for
) -> None:
    """Reconnect after `turn_complete` → full replay from checkpoints, then complete."""
    turn_id = await _start_turn(client, session_id)
    await _await_status(container, turn_id, TurnStatus.COMPLETE)

    websocket = socket_for.open()
    websocket.send({"type": "resume", "turnId": turn_id, "lastSeq": 0})
    await websocket.wait_for("turn_complete")

    assert websocket.text() == scripted_model.full_text


async def test_resume_cross_instance_delivers_the_remainder(
    client, container, settings, session_id: str, scripted_model: ScriptedModel, alice
) -> None:
    """Two app instances sharing the emulator; resume on B for a turn owned by A.

    Instance B is a second `Container` over the same database — which is exactly what a
    second Cloud Run instance is. It has its own registry and its own broker, so
    `TurnService.owns` is false there and the follower path is the only way it can serve
    the client. Nothing about this test would fail if B secretly attached to A's broker,
    so B is built with its own.
    """
    from coach.api.deps import Container

    scripted_model.chunks = [f"seg{index} " for index in range(16)]
    scripted_model.delay = 0.02

    instance_b = Container(settings)
    socket_b = FakeWebSocket()
    session_b = SocketSession(
        socket_b,  # type: ignore[arg-type]
        alice,
        turns=instance_b.turns,
        broker=instance_b.broker,
        presence=instance_b.presence_repository,
    )
    pump = asyncio.create_task(session_b.run())

    try:
        turn_id = await _start_turn(client, session_id)
        assert instance_b.instance_id != container.instance_id
        # Non-vacuity: if B could serve this from its own broker the test would prove
        # nothing about cross-instance resume, so assert it cannot before relying on it.
        assert container.registry.is_running(turn_id)
        assert not instance_b.registry.is_running(turn_id)
        assert not instance_b.turns.owns(await instance_b.turns.get(alice, turn_id))

        socket_b.send({"type": "resume", "turnId": turn_id, "lastSeq": 0})
        await socket_b.wait_for("turn_complete", timeout=15.0)
        assert socket_b.text() == scripted_model.full_text
    finally:
        pump.cancel()


# --- Cancel ----------------------------------------------------------------------------


async def test_explicit_cancel_stops_generation_and_notifies_subscribers(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for, alice
) -> None:
    scripted_model.chunks = [f"tok{index} " for index in range(60)]
    scripted_model.delay = 0.03

    websocket = socket_for.open()
    turn_id = await _start_turn(client, session_id)
    websocket.send({"type": "subscribe", "turnId": turn_id})
    await websocket.wait_for_seq(2)

    response = await client.post(f"/api/sessions/{session_id}/turns/{turn_id}/cancel")
    assert response.status_code == 200

    frame = await websocket.wait_for("turn_error")
    assert frame["code"] == "cancelled"
    assert frame["retryable"] is False

    await _await_status(container, turn_id, TurnStatus.CANCELLED)
    turn = await container.turns.get(alice, turn_id)
    assert turn.status is TurnStatus.CANCELLED
    assert turn.ended_at is not None


async def test_cancelling_a_finished_turn_is_a_no_op(
    client, container, session_id: str, scripted_model: ScriptedModel
) -> None:
    """A double-clicked cancel must not turn a completed answer into an error."""
    turn_id = await _start_turn(client, session_id)
    await _await_status(container, turn_id, TurnStatus.COMPLETE)

    response = await client.post(f"/api/sessions/{session_id}/turns/{turn_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == TurnStatus.COMPLETE.value


async def test_a_closed_socket_does_not_cancel_anything(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for, alice
) -> None:
    """The guarantee stated as an absence: dropping a subscriber cancels nothing.

    Distinct from the disconnect test above, which asserts the *result*. This asserts the
    mechanism — the registry still holds the task after the socket is gone — because that
    is the thing a refactor is most likely to break by moving the task into the handler's
    scope.
    """
    scripted_model.chunks = [f"tok{index} " for index in range(30)]
    scripted_model.delay = 0.03

    websocket = socket_for.open()
    turn_id = await _start_turn(client, session_id)
    websocket.send({"type": "subscribe", "turnId": turn_id})
    await websocket.wait_for_seq(2)

    websocket.disconnect()
    await asyncio.sleep(0.1)

    assert container.registry.is_running(turn_id)
    await _await_status(container, turn_id, TurnStatus.COMPLETE)


# --- Shutdown --------------------------------------------------------------------------


async def test_sigterm_drain_awaits_an_in_flight_turn(
    client, container, session_id: str, scripted_model: ScriptedModel, alice
) -> None:
    """An in-flight turn is awaited within the grace period."""
    scripted_model.chunks = ["a ", "b ", "c "]
    scripted_model.delay = 0.02

    turn_id = await _start_turn(client, session_id)
    await container.turns.drain(timeout=10.0)

    turn = await container.turns.get(alice, turn_id)
    assert turn.status is TurnStatus.COMPLETE


async def test_a_turn_that_outlives_the_grace_period_is_failed_retryable(
    client, container, session_id: str, scripted_model: ScriptedModel, alice
) -> None:
    """A survivor is marked `failed, retryable` — and gets an `endedAt`, so it expires."""
    scripted_model.chunks = [f"tok{index} " for index in range(200)]
    scripted_model.delay = 0.05

    turn_id = await _start_turn(client, session_id)
    await asyncio.sleep(0.1)
    await container.turns.drain(timeout=0.05)

    turn = await container.turns.get(alice, turn_id)
    assert turn.status in {TurnStatus.FAILED, TurnStatus.CANCELLED}
    assert turn.ended_at is not None


async def test_a_draining_instance_refuses_new_turns(
    client, container, session_id: str, scripted_model: ScriptedModel
) -> None:
    await container.turns.drain(timeout=1.0)

    response = await client.post(f"/api/sessions/{session_id}/turns", json={"text": "hi"})

    assert response.status_code == 409


# --- Duplicates ------------------------------------------------------------------------


async def test_deltas_at_or_below_last_seq_are_never_sent(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for
) -> None:
    """Server-side half of "duplicate deltas are dropped".

    The client drops `seq <= lastSeq` too (asserted in the web tests), but a server that
    replayed them would make the guarantee depend on the client being correct — and on
    the resumed transcript, a duplicated sentence is silent corruption rather than a
    visible error.
    """
    scripted_model.chunks = [f"w{index} " for index in range(20)]
    scripted_model.delay = 0.02

    first = socket_for.open()
    turn_id = await _start_turn(client, session_id)
    first.send({"type": "subscribe", "turnId": turn_id})
    await first.wait_for_seq(5)
    last_seq = max(first.seqs())
    first.disconnect()

    second = socket_for.open()
    second.send({"type": "resume", "turnId": turn_id, "lastSeq": last_seq})
    await second.wait_for("turn_complete")

    assert all(seq > last_seq for seq in second.seqs())


# --- Failure ---------------------------------------------------------------------------


async def test_a_model_failure_ends_the_turn_as_failed_and_retryable(
    client, container, session_id: str, scripted_model: ScriptedModel, socket_for, alice
) -> None:
    scripted_model.chunks = ["partial answer "]
    scripted_model.fail_with = "the model went away"

    websocket = socket_for.open()
    turn_id = await _start_turn(client, session_id)
    websocket.send({"type": "subscribe", "turnId": turn_id})

    frame = await websocket.wait_for("turn_error")
    assert frame["retryable"] is True

    turn = await container.turns.get(alice, turn_id)
    assert turn.status is TurnStatus.FAILED
    assert turn.error is not None
    assert turn.ended_at is not None


# --- helpers ---------------------------------------------------------------------------


async def _await_status(
    container, turn_id: str, status: TurnStatus, timeout: float = 15.0
) -> None:
    """Wait for a turn to reach `status` in Firestore.

    Polls rather than awaiting the registry's task, because "the turn finished" has to be
    observable from *storage* — that is what a second instance sees, and it is the only
    thing the resume path can rely on.
    """

    async def _poll() -> None:
        while True:
            turn = await container.turn_repository.get(turn_id)
            if turn is not None and turn.status is status:
                return
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout=timeout)
