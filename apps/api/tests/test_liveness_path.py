"""The liveness endpoint must not be `/healthz`.

`/healthz` is the obvious name, it is what the design documents originally used, and it
does not work on Cloud Run: Google's frontend intercepts that exact path and answers it
with its own HTML 404 — "The requested URL /healthz was not found on this server" —
without forwarding the request to the container.

Observed on the deployed `coach-api` service in `us-central1`:

    /healthz     404   (text/html, from Google's frontend)
    /healthz/    404   (same)
    /readyz      200   (our app)
    /api/me      401   (our app)
    /nope        200   (our app, SPA fallback)
    /livez       200   /health, /_health, /ping, /status also reachable

Nothing local can catch this, which is why it gets a test of its own rather than a
comment. Cloud Run's startup and liveness probes are issued **to the container** and
bypass the frontend, so `/healthz` satisfies them and the revision reports healthy. The
e2e harness, the local container, and every test here bypass it too. The only things that
notice are the deploy smoke test and the Cloud Monitoring uptime check — and the uptime
check fails silently, so nobody would have noticed at all.

This test cannot detect the interception. What it can do is stop the name drifting back,
and carry the reason to whoever tries.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from coach.api.routers.health import INTERCEPTED_LIVENESS_PATH, LIVENESS_PATH
from coach.main import API_PREFIXES

#: Files that name the liveness path and must agree with the application.
COUPLED_FILES = (
    "infra/terraform/modules/cloud_run_service/main.tf",  # startup + liveness probes
    "infra/terraform/modules/observability/main.tf",  # the uptime check
    "scripts/smoke.sh",  # the deploy gate
    "docker-compose.e2e.yml",  # the harness healthcheck
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def test_the_liveness_path_is_not_healthz() -> None:
    assert LIVENESS_PATH != INTERCEPTED_LIVENESS_PATH
    assert LIVENESS_PATH == "/livez"


def test_the_app_does_not_serve_healthz(app) -> None:
    """If someone re-adds it, the interception makes it a lie on Cloud Run.

    A route that answers locally and 404s in production is worse than no route: it
    reports healthy everywhere the developer looks.
    """
    paths = [getattr(route, "path", None) for route in app.routes]
    assert INTERCEPTED_LIVENESS_PATH not in paths


async def test_liveness_responds_without_authentication(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get(LIVENESS_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_liveness_is_guarded_from_the_spa_catch_all() -> None:
    assert LIVENESS_PATH in API_PREFIXES
    assert INTERCEPTED_LIVENESS_PATH not in API_PREFIXES


def test_everything_that_probes_liveness_uses_the_same_path() -> None:
    """The path is repeated in Terraform, the smoke test, and the compose harness.

    None of those import from Python, so nothing but this test keeps them in step — and a
    stale one fails in a different place each time: a probe that always passes, an uptime
    check that always fails, or a deploy gate that rejects a healthy revision.
    """
    root = _repo_root()
    for relative in COUPLED_FILES:
        text = (root / relative).read_text(encoding="utf-8")
        assert LIVENESS_PATH in text, f"{relative} does not mention {LIVENESS_PATH}"
        # Prose may explain why /healthz is avoided; a probe path must not use it.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            assert INTERCEPTED_LIVENESS_PATH not in line, (
                f"{relative} still probes {INTERCEPTED_LIVENESS_PATH}: {line.strip()}"
            )
