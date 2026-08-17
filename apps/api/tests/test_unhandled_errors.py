"""An unplanned failure answers like a planned one.

Every deliberate error in this service is RFC 9457 `problem+json`
(docs/04-api-contract.md). A *bug* was the one exception: it produced Starlette's bare
`Internal Server Error` in `text/plain`, the client's parser fell back to the HTTP status
text, and the user saw "request failed" while a complete traceback sat in the logs with
nothing tying it to their request.

That is not a cosmetic gap. It cost a whole diagnosis round on a 403 from IAM `signBlob`:
the UI could report only that something had gone wrong, so the first guesses went to the
frontend. The handler under test closes it — a problem document, and a `traceId` that
turns a screenshot into a log query.
"""

from __future__ import annotations

import httpx
import pytest

from coach.core.errors import PROBLEM_CONTENT_TYPE


class _Boom(Exception):
    pass


@pytest.fixture
def exploding_app(app, monkeypatch: pytest.MonkeyPatch):
    """The real app with a real endpoint made to fail.

    A purpose-built `/api/explode` route was the obvious approach and is wrong here: a
    router added after `create_app` lands *after* the SPA catch-all, which then answers
    it with a 404 (`main.API_PREFIXES`). Breaking a real dependency instead keeps this
    honest — the failure travels the same middleware stack a genuine bug would.

    Returns a callable so a test can pick the `ENV` the handler sees. Only
    `app.state.settings` is swapped, not the container: rebuilding for `dev` or `prod`
    would demand real buckets and real credentials, and the branch under test reads
    nothing but `env`.
    """

    def _with_env(env: str = "local"):
        async def _raise(*_args: object, **_kwargs: object) -> None:
            raise _Boom("the bucket said no")

        monkeypatch.setattr(app.state.container.users, "get_or_create", _raise)
        app.state.settings = app.state.settings.model_copy(update={"env": env})
        return app

    return _with_env


@pytest.fixture
def unauthenticated_failure(app, monkeypatch: pytest.MonkeyPatch):
    """A failure on a route that takes no `Principal`, so `ENV` can be varied freely.

    The env-gated branch cannot be reached through `/api/me`: auth and the error handler
    read the same `app.state.settings`, so setting `ENV=dev` to test redaction also turns
    off the `Bearer dev:<uid>` path and the request 401s before any endpoint runs. That
    guard is working as designed (`test_auth_local_bypass.py`), so the test moves rather
    than the guard.

    The failure is raised from the idempotency middleware, which sits inside
    `ServerErrorMiddleware` just as a route does — the handler cannot tell the difference,
    and `/livez` needs no credentials.
    """

    def _with_env(env: str):
        from coach.api.idempotency import IdempotencyMiddleware

        async def _raise(self: object, request: object, call_next: object) -> None:
            raise _Boom("the bucket said no")

        monkeypatch.setattr(IdempotencyMiddleware, "dispatch", _raise)
        app.state.settings = app.state.settings.model_copy(update={"env": env})
        return app

    return _with_env


async def _get(app, path: str = "/api/me") -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers={"Authorization": "Bearer dev:u_alice"})


async def test_an_unhandled_exception_is_rendered_as_problem_json(exploding_app) -> None:
    response = await _get(exploding_app())

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body = response.json()
    assert body["status"] == 500
    assert body["type"] == "/problems/internal-error"
    assert body["instance"] == "/api/me"


async def test_the_response_carries_a_trace_id(exploding_app) -> None:
    """Without one, a user's report and the log line that explains it never meet."""
    body = (await _get(exploding_app())).json()

    assert body["traceId"]


async def test_cloud_run_s_own_trace_id_is_echoed_back(exploding_app) -> None:
    """So the id in the response is the one `gcloud logging read 'trace:"…"'` matches.

    Cloud Run sends `X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1`; only the first segment
    is the trace.
    """
    transport = httpx.ASGITransport(app=exploding_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/api/me",
            headers={
                "Authorization": "Bearer dev:u_alice",
                "X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1",
            },
        )

    assert response.json()["traceId"] == "105445aa7843bc8bf206b12000100000"


@pytest.mark.parametrize("env", ["local", "dev"])
async def test_outside_production_the_detail_names_the_cause(
    unauthenticated_failure, env: str
) -> None:
    """In local and dev the person reading the toast is the person fixing the bug."""
    body = (await _get(unauthenticated_failure(env), "/livez")).json()

    assert "_Boom" in body["detail"]
    assert "the bucket said no" in body["detail"]


async def test_in_production_the_detail_says_nothing_about_the_exception(
    unauthenticated_failure,
) -> None:
    """An exception message can carry a bucket name, a query, or a row of user data."""
    body = (await _get(unauthenticated_failure("prod"), "/livez")).json()

    assert "_Boom" not in body["detail"]
    assert "the bucket said no" not in body["detail"]
    # Still correlatable: redaction removes the cause from the response, not from the
    # logs, and the trace id is what joins the two.
    assert body["traceId"]


async def test_a_deliberate_error_is_unaffected(client) -> None:
    """The handler must not swallow the planned path — `CoachError` still renders itself."""
    response = await client.get("/api/projects/p_nope")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["type"] == "/problems/not-found"
    assert "traceId" not in response.json()
