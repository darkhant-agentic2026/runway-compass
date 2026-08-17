"""The SPA catch-all must not shadow the API.

docs/08-testing.md:

    **SPA catch-all does not shadow the API.** Asserts `/api/*`, `/ws`, `/internal/*`,
    `/livez`, and `/readyz` all resolve to their handlers rather than `index.html`, and
    that an unknown path does serve the SPA. Guards the route-registration order that
    docs/07-infra-deploy.md#container depends on.

The failure this catches is silent and total: register the catch-all one line too early
and every API route starts returning HTML with a 200, which looks like a frontend bug for
a long time before anyone suspects routing.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from coach.main import API_PREFIXES, create_app

#: A representative path under every prefix the SPA must not swallow. `/ws` and
#: `/internal/*` have no handlers until M2 and M5; they are listed anyway, because the
#: point is to catch the day someone adds them *below* the catch-all.
GUARDED_PATHS = [
    "/api/me",
    "/api/projects",
    "/ws",
    "/internal/tick",
    "/livez",
    "/readyz",
]

SPA_MARKER = "<!doctype html><title>coach-spa-under-test</title>"


@pytest.fixture
def app_with_spa(settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An app whose working directory contains a built SPA."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(SPA_MARKER, encoding="utf-8")
    (static / "assets" / "app.js").write_text("export default 1;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return create_app(settings)


@pytest.fixture
async def spa_client(app_with_spa):
    transport = httpx.ASGITransport(app=app_with_spa)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer dev:u_alice"},
    ) as http_client:
        yield http_client


def test_the_catch_all_is_registered_last(app_with_spa) -> None:
    """Structural assertion, independent of any request.

    A request-level test can only prove the ordering for the paths it happens to try;
    this proves it for every route that exists.
    """
    paths = [getattr(route, "path", None) for route in app_with_spa.routes]
    assert paths[-1] == "/{full_path:path}", (
        "The SPA catch-all must be the last route registered, after every router "
        "(docs/07-infra-deploy.md#container). Found: " + repr(paths)
    )


@pytest.mark.parametrize("path", GUARDED_PATHS)
async def test_api_paths_do_not_resolve_to_the_spa(
    spa_client: httpx.AsyncClient, path: str
) -> None:
    response = await spa_client.get(path)
    assert SPA_MARKER not in response.text, (
        f"{path} was served by the SPA fallback instead of its handler."
    )
    assert not response.headers["content-type"].startswith("text/html")


def test_every_guarded_prefix_has_a_representative() -> None:
    """Keeps `GUARDED_PATHS` honest as prefixes are added to `API_PREFIXES`."""
    for prefix in API_PREFIXES:
        assert any(path.startswith(prefix) for path in GUARDED_PATHS), (
            f"No representative path is tested for the {prefix!r} prefix."
        )


async def test_unknown_paths_do_serve_the_spa(spa_client: httpx.AsyncClient) -> None:
    """Deep links into client-side routes must reach the SPA, not a 404."""
    for path in ("/", "/projects/p_123", "/settings", "/some/deep/client/route"):
        response = await spa_client.get(path)
        assert response.status_code == 200, path
        assert SPA_MARKER in response.text, path


async def test_assets_are_served_from_the_mount(spa_client: httpx.AsyncClient) -> None:
    response = await spa_client.get("/assets/app.js")
    assert response.status_code == 200
    assert "export default 1;" in response.text


async def test_api_404s_are_problem_json_not_html(spa_client: httpx.AsyncClient) -> None:
    """An unknown path *under* /api is a 404 from the router, not an SPA document.

    Without this, a typo'd endpoint would return the index page with a 200 and the
    client would parse HTML as JSON.
    """
    response = await spa_client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert SPA_MARKER not in response.text
