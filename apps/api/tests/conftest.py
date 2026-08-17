"""Shared fixtures.

Every backend test needs the gcloud Firestore emulator (docs/07-infra-deploy.md). If
`FIRESTORE_EMULATOR_HOST` is already set and reachable — which is what
`./scripts/dev.sh test api` and the CI job arrange — that instance is used. Otherwise one
is started here, so a bare `pytest` still works.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from coach.core.principal import Principal

PROJECT_ID = "demo-coach-test"
DEFAULT_EMULATOR_PORT = 8987
STARTUP_TIMEOUT_SECONDS = 90


def _is_listening(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex((host, port)) == 0


def _split_host_port(host_port: str) -> tuple[str, int]:
    host, _, port = host_port.rpartition(":")
    return (host or "127.0.0.1"), int(port)


@pytest.fixture(scope="session")
def emulator_host() -> Iterator[str]:
    """A running Firestore emulator, as `host:port`."""
    existing = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if existing and _is_listening(*_split_host_port(existing)):
        yield existing
        return

    host_port = f"127.0.0.1:{DEFAULT_EMULATOR_PORT}"
    process = subprocess.Popen(
        [
            "gcloud",
            "beta",
            "emulators",
            "firestore",
            "start",
            f"--host-port={host_port}",
            f"--project={PROJECT_ID}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    try:
        while not _is_listening(*_split_host_port(host_port)):
            if process.poll() is not None:
                raise RuntimeError(
                    "The Firestore emulator exited during startup. Check that a JRE is "
                    "on PATH and that the cloud-firestore-emulator component is "
                    "installed (./scripts/dev.sh reports both)."
                )
            if time.monotonic() > deadline:
                raise TimeoutError("The Firestore emulator did not start in time.")
            time.sleep(0.3)
        os.environ["FIRESTORE_EMULATOR_HOST"] = host_port
        yield host_port
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()


@pytest.fixture(scope="session", autouse=True)
def _emulator_env(emulator_host: str) -> Iterator[None]:
    """Make the emulator visible to the Firestore client before any client is built.

    The client caches on `(project, database)` and reads `FIRESTORE_EMULATOR_HOST` at
    construction time, so this has to be in place before the first repository call.
    """
    previous = os.environ.get("FIRESTORE_EMULATOR_HOST")
    os.environ["FIRESTORE_EMULATOR_HOST"] = emulator_host
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
    yield
    if previous is None:
        os.environ.pop("FIRESTORE_EMULATOR_HOST", None)
    else:
        os.environ["FIRESTORE_EMULATOR_HOST"] = previous


@pytest.fixture(autouse=True)
async def _clean_database(emulator_host: str) -> AsyncIterator[None]:
    """Wipe the emulator between tests, so each starts from an empty database."""
    url = (
        f"http://{emulator_host}/emulator/v1/projects/{PROJECT_ID}"
        "/databases/(default)/documents"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        await client.delete(url)
    yield


@pytest.fixture
def settings(emulator_host: str):
    from coach.core.config import Settings

    return Settings(
        env="local",
        google_cloud_project=PROJECT_ID,
        firestore_emulator_host=emulator_host,
        log_level="WARNING",
    )


@pytest.fixture
def app(settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A fresh application, rooted at a temp cwd with no SPA build present.

    `main.STATIC_DIR` is relative by design (docs/07-infra-deploy.md#container), so tests
    that care about the static mount control it by controlling the working directory.
    """
    from coach.main import create_app

    monkeypatch.chdir(tmp_path)
    return create_app(settings)


@pytest.fixture
def container(app):
    return app.state.container


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    """An authenticated client for the default test user."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer dev:u_alice"},
    ) as http_client:
        yield http_client


@pytest.fixture
async def other_client(app) -> AsyncIterator[httpx.AsyncClient]:
    """A second signed-in user, for the per-user isolation tests."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer dev:u_mallory"},
    ) as http_client:
        yield http_client


@pytest.fixture
def alice() -> Principal:
    return Principal(uid="u_alice", email="alice@localhost.dev", source="dev")


@pytest.fixture
def mallory() -> Principal:
    return Principal(uid="u_mallory", email="mallory@localhost.dev", source="dev")
