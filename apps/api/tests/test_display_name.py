"""`PATCH /api/me` — the one identity field a learner may override.

The dev-token auth path always sets `principal.display_name` to the uid itself
(`api/auth.py`), so a customized name that differs from the caller's own uid is exactly
the case `UserService.get_or_create`'s refresh loop must not clobber on the next request.
"""

from __future__ import annotations

import httpx


async def test_setting_a_display_name_updates_it(client: httpx.AsyncClient) -> None:
    response = await client.patch("/api/me", json={"displayName": "Jane Doe"})
    assert response.status_code == 200, response.text
    assert response.json()["displayName"] == "Jane Doe"


async def test_a_customized_name_survives_the_next_request(
    client: httpx.AsyncClient,
) -> None:
    """The dev token's own claim is always the uid (`u_alice`), so a later `GET /api/me`
    re-running `get_or_create`'s token-refresh loop is the regression this pins: without
    `display_name_customized`, "Jane Doe" would revert to "u_alice" right here."""
    await client.patch("/api/me", json={"displayName": "Jane Doe"})

    again = await client.get("/api/me")

    assert again.json()["displayName"] == "Jane Doe"


async def test_an_uncustomized_name_still_follows_the_token(
    client: httpx.AsyncClient,
) -> None:
    """Without ever calling `PATCH /api/me`, the token's own claim is what shows up —
    the refresh loop's ordinary behaviour, unchanged by this feature."""
    response = await client.get("/api/me")
    assert response.json()["displayName"] == "u_alice"


async def test_an_empty_display_name_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.patch("/api/me", json={"displayName": "   "})
    assert response.status_code == 422


async def test_display_name_is_trimmed(client: httpx.AsyncClient) -> None:
    response = await client.patch("/api/me", json={"displayName": "  Jane Doe  "})
    assert response.status_code == 200, response.text
    assert response.json()["displayName"] == "Jane Doe"
