"""`board_update` reaches a socket on *another* instance.

docs/09-roadmap.md#status-after-m3 deferred this to M5 and named the reason: until now
every board mutation came from a tool call inside a turn the user's own request started,
and session affinity put their socket on the same instance. **A scheduled run has no such
relationship** — it executes wherever Cloud Tasks lands it, and the owner may be connected
anywhere or nowhere.

**Driven by two `Container`s over one emulator**, which is what a second Cloud Run instance
is — the same arrangement the M2 cross-instance resume test uses. That matters here more
than usual: a single-process test of this feature passes with the relay deleted, because
the local hub delivers the frame anyway. So the *interesting* assertion is the one made
from a container that did not publish.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coach.api.deps import Container
from coach.ws.hub import RELAY_POLL_SECONDS


@pytest.fixture
def instance_b(settings) -> Container:
    """A second process against the same database."""
    return Container(settings)


async def _wait_for(frames: list[dict[str, Any]], *, timeout: float) -> None:
    async with asyncio.timeout(timeout):
        while not frames:
            await asyncio.sleep(0.05)


async def test_a_frame_published_on_one_instance_reaches_a_socket_on_another(
    container: Container, instance_b: Container
) -> None:
    received: list[dict[str, Any]] = []

    async def sink(frame: dict[str, Any]) -> None:
        received.append(frame)

    instance_b.board_updates.attach("u_alice", sink)
    try:
        # The poller establishes its cursor on the first read without delivering, so a
        # frame published before that read would be correctly skipped as history. Waiting
        # one interval is what makes this test about the relay rather than about the race.
        await asyncio.sleep(RELAY_POLL_SECONDS * 1.2)
        await container.board_updates.publish(
            "u_alice", project_id="p_1", task_ids=["k_1"], origin="agent", run_id="r_1"
        )

        await _wait_for(received, timeout=RELAY_POLL_SECONDS * 4)
    finally:
        instance_b.board_updates.detach("u_alice", sink)
        await instance_b.board_updates.aclose()

    assert received[0]["type"] == "board_update"
    assert received[0]["projectId"] == "p_1"
    assert received[0]["taskIds"] == ["k_1"]
    assert received[0]["runId"] == "r_1"


async def test_the_publishing_instance_does_not_deliver_its_own_frame_twice(
    container: Container,
) -> None:
    """The writer has already fanned out locally; its own poller must skip the row.

    Without the `instanceId` tag every board mutation would reach the originating tab
    twice. Harmless for an invalidation — until somebody hangs a toast off it.
    """
    received: list[dict[str, Any]] = []

    async def sink(frame: dict[str, Any]) -> None:
        received.append(frame)

    container.board_updates.attach("u_alice", sink)
    try:
        await asyncio.sleep(RELAY_POLL_SECONDS * 1.2)
        await container.board_updates.publish("u_alice", project_id="p_1", task_ids=["k_1"])
        await _wait_for(received, timeout=2.0)
        # Two further poll intervals: long enough for a duplicate to arrive if one were
        # coming, which is the only way to assert an absence about a poller.
        await asyncio.sleep(RELAY_POLL_SECONDS * 2.2)
    finally:
        container.board_updates.detach("u_alice", sink)
        await container.board_updates.aclose()

    assert len(received) == 1


async def test_a_frame_for_a_user_with_no_sockets_here_is_still_written(
    container: Container, instance_b: Container
) -> None:
    """The case the relay exists for: a run executing where nobody is connected.

    `publish` writes to the channel even when this instance has no sinks — the early return
    is on the *local* fan-out only. A version that returned before the relay write would
    pass every test above, because in those the publisher happens to have a socket.
    """
    await container.board_updates.publish("u_bob", project_id="p_9", task_ids=["k_9"])

    latest, entries = await instance_b.board_event_repository.read_since("u_bob", 0)

    assert latest == 1
    assert [entry["frame"]["projectId"] for entry in entries] == ["p_9"]
