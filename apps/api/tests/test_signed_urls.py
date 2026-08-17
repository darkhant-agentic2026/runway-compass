"""V4 signing picks the right path for the credentials it has.

docs/07-infra-deploy.md:

> V4 signed upload URLs are signed through the IAM SignBlob API rather than a downloaded
> key. Without this the upload flow fails at runtime with a signing error, **and only in
> a deployed environment** — local dev uses the developer's impersonated credentials, so
> it passes there.

That parenthesis is why these tests exist and why they fake the credentials rather than
the store. The two environments hand the process different credential objects:

| Environment | Credentials | can sign for itself? |
| --- | --- | --- |
| Cloud Run | `compute_engine.Credentials` | no — no key, no `signer_email` |
| Local dev (`--impersonate-service-account`) | `impersonated_credentials.Credentials` | yes |

`generate_signed_url()` uses `credentials.signer_email` and `credentials.sign_bytes`
unless it is given `service_account_email` **and** `access_token`, in which case it signs
through the IAM API. So the only thing that distinguishes a working deployment from a
broken one is which arguments we pass — nothing about the request, the bucket, or the
file. No integration test can reach this; only the argument choice can be asserted.
"""

from __future__ import annotations

from typing import Any

import pytest

from coach.integrations.storage import GcsObjectStore


class _Blob:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def generate_signed_url(self, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return "https://signed.example/put"


class _SelfSigningCredentials:
    """What `--impersonate-service-account` produces locally."""

    valid = True
    token = "local-token"
    signer_email = "coach-api-sa@coach-dev.iam.gserviceaccount.com"

    def sign_bytes(self, _message: bytes) -> bytes:  # pragma: no cover - never called
        return b"signature"


class _ComputeCredentials:
    """What the metadata server produces on Cloud Run: no key, no signer."""

    def __init__(
        self,
        *,
        valid: bool = True,
        email: str = "coach-api-sa@dev.iam.gserviceaccount.com",
    ) -> None:
        self.valid = valid
        self.token = "metadata-token"
        self.service_account_email = email
        self.refreshes = 0

    def refresh(self, _request: Any) -> None:
        self.refreshes += 1
        self.valid = True
        if self.service_account_email == "default":
            # The real address arrives with the token, not before it.
            self.service_account_email = "coach-api-sa@dev.iam.gserviceaccount.com"


def _store(credentials: Any, blob: _Blob) -> GcsObjectStore:
    """A `GcsObjectStore` with no GCS behind it.

    Built with `__new__` because the constructor makes a `storage.Client()`, which
    resolves credentials — the very thing under test.
    """
    store = GcsObjectStore.__new__(GcsObjectStore)
    store._bucket_name = "coach-dev-coach-uploads"
    store._client = type("_Client", (), {"_credentials": credentials})()
    store._bucket = type("_Bucket", (), {"blob": staticmethod(lambda _name: blob)})()
    return store


async def test_impersonated_credentials_sign_for_themselves() -> None:
    """The local path. Passing IAM arguments here would be wrong, not merely redundant."""
    blob = _Blob()

    url = await _store(_SelfSigningCredentials(), blob).signed_put_url(
        "u_alice/up_1/shot.png", mime_type="image/png"
    )

    assert url == "https://signed.example/put"
    assert "service_account_email" not in blob.kwargs
    assert "access_token" not in blob.kwargs


async def test_compute_credentials_sign_through_iam() -> None:
    """The Cloud Run path, and the one that was missing.

    Without these two arguments `generate_signed_url` reaches for
    `credentials.signer_email`, which compute credentials do not have, and the upload
    endpoint 500s — in production only.
    """
    blob = _Blob()
    credentials = _ComputeCredentials()

    await _store(credentials, blob).signed_put_url(
        "u_alice/up_1/shot.png", mime_type="image/png"
    )

    assert blob.kwargs["service_account_email"] == credentials.service_account_email
    assert blob.kwargs["access_token"] == "metadata-token"


async def test_the_signed_url_is_a_v4_put_bound_to_the_content_type() -> None:
    """A signature that did not cover the method or the type would sign more than it should."""
    blob = _Blob()

    await _store(_ComputeCredentials(), blob).signed_put_url("o", mime_type="application/pdf")

    assert blob.kwargs["version"] == "v4"
    assert blob.kwargs["method"] == "PUT"
    assert blob.kwargs["content_type"] == "application/pdf"


async def test_expired_compute_credentials_are_refreshed_before_signing() -> None:
    """The token is part of the signature request, so a stale one is a 401 from IAM."""
    credentials = _ComputeCredentials(valid=False)

    await _store(credentials, _Blob()).signed_put_url("o", mime_type="image/png")

    assert credentials.refreshes == 1


async def test_a_credential_still_naming_itself_default_is_refreshed_again() -> None:
    """On the metadata server the identity arrives with the token, not before it."""
    credentials = _ComputeCredentials(email="default")
    blob = _Blob()

    await _store(credentials, blob).signed_put_url("o", mime_type="image/png")

    assert blob.kwargs["service_account_email"] != "default"


async def test_credentials_that_can_neither_sign_nor_name_themselves_fail_loudly() -> None:
    """Better a 500 naming the cause than a signed URL that GCS will reject."""

    class _Anonymous:
        valid = True
        token = "t"

        def refresh(self, _request: Any) -> None:
            pass

    with pytest.raises(RuntimeError, match="Cannot sign an upload URL"):
        await _store(_Anonymous(), _Blob()).signed_put_url("o", mime_type="image/png")
