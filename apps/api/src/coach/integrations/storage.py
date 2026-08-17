"""Object storage: V4 signed upload URLs, and a local stand-in for them.

docs/07-infra-deploy.md#the-two-local-dependencies-that-are-not-emulated:

> **Storage.** Uploads use V4 signed URLs, which need a real signer. Local dev points at
> a real `coach-dev` bucket and developers authenticate with
> `gcloud auth application-default login --impersonate-service-account=…`. Signing then
> works through the IAM SignBlob API, which keeps the no-service-account-keys rule
> intact. Unit tests fake the artifact service outright and touch neither GCS nor a
> signer.

Hence two implementations behind one protocol. The fake is not a test-only convenience:
without it, `ENV=local` without an `UPLOAD_BUCKET` could not start, and the e2e harness —
which runs the real image against an emulator and no GCP project at all — could not run.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Protocol

from coach.core.clock import now
from coach.core.config import Settings

logger = logging.getLogger(__name__)

#: How long a signed PUT stays valid. Long enough for a 20 MB upload on a poor
#: connection, short enough that a leaked URL is not a lasting write capability.
SIGNED_URL_TTL = timedelta(minutes=30)


class ObjectStore(Protocol):
    """The operations the upload flow needs from the **staging** bucket.

    Staging, not storage: `{project}-coach-uploads` carries a
    `lifecycle_rule { age = 1 → Delete }`, so everything here is gone within a day. That
    is why `download` exists — finalize has to move the bytes somewhere durable before
    the rule collects them (see `coach.services.uploads`).
    """

    @property
    def bucket(self) -> str: ...

    async def signed_put_url(self, object_name: str, *, mime_type: str) -> str:
        """A V4 resumable PUT URL for `object_name`.

        Async because signing is not necessarily local arithmetic: on Cloud Run it is two
        network calls (a token refresh and an IAM `signBlob`), and doing those on the
        event loop would stall every other request and every in-flight turn.
        """

    async def stat(self, object_name: str) -> tuple[int, str] | None:
        """`(size_bytes, content_type)` of an uploaded object, or `None` if absent."""

    async def download(self, object_name: str) -> bytes | None:
        """The object's bytes, or `None` if it is not there.

        Bounded by the 20 MB upload cap, and called once per upload, so pulling the
        bytes through the process is affordable. A server-side copy would avoid that but
        would mean naming the destination blob ourselves — and the destination layout
        belongs to ADK's `GcsArtifactService`, not to us.
        """


class GcsObjectStore:
    """The real thing. Signs through IAM SignBlob — no service-account key anywhere.

    ## Why signing needs two code paths

    V4 signing needs something that can sign bytes. Which credentials the process holds
    decides whether that is possible locally:

    | Environment | Credentials | `sign_bytes`? |
    | --- | --- | --- |
    | Cloud Run | `google.auth.compute_engine.Credentials` | **no** |
    | Local dev (impersonation) | `impersonated_credentials.Credentials` | yes |

    Compute credentials have no private key and no `signer_email`, so
    `generate_signed_url()` raises there — while passing locally, because impersonated
    credentials sign for themselves. docs/07-infra-deploy.md predicted exactly this
    ("Without this the upload flow fails at runtime with a signing error, and **only in a
    deployed environment**"), and the IAM binding it calls for
    (`iam.serviceAccountTokenCreator` on `coach-api-sa`, on itself) is already in
    `modules/identity`. What was missing was the code using it.

    Passing `service_account_email` **and** `access_token` switches
    `google-cloud-storage` to signing through the IAM `signBlob` API, which is what that
    binding grants and what keeps the no-service-account-keys rule intact.

    ## …and why the token cannot be the storage client's

    `storage.Client()` resolves ADC **scoped to storage**. Handing that token to
    `iamcredentials.googleapis.com` gets:

        403 ACCESS_TOKEN_SCOPE_INSUFFICIENT
        method: google.iam.credentials.v1.IAMCredentials.SignBlob

    which is easy to misread as the missing IAM binding, because it is a 403 mentioning
    IAM. It is not: the binding can be present and correct — the *token* is simply not
    allowed to exercise it. Signing therefore resolves its own credentials at
    `cloud-platform` scope rather than reusing the client's.
    """

    #: `signBlob` is an `iamcredentials` method, and that API is only reachable with a
    #: `cloud-platform` token. Narrower storage scopes are refused, and the refusal names
    #: the *scope*, not the role.
    SIGNING_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)

    def __init__(self, bucket_name: str) -> None:
        # `google.cloud` is a namespace package, so mypy cannot see the attribute even
        # with the module's own stubs present.
        from google.cloud import storage  # type: ignore[attr-defined]

        self._bucket_name = bucket_name
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        #: Resolved on first use and reused. `google-auth` caches the token internally
        #: and only talks to the metadata server when it has expired, so this is one
        #: object rather than one round-trip per upload.
        self._signing_credentials: Any | None = None

    @property
    def bucket(self) -> str:
        return self._bucket_name

    async def signed_put_url(self, object_name: str, *, mime_type: str) -> str:
        import asyncio

        return await asyncio.to_thread(self._sign_put, object_name, mime_type)

    def _sign_put(self, object_name: str, mime_type: str) -> str:
        blob = self._bucket.blob(object_name)
        options: dict[str, Any] = {
            "version": "v4",
            "expiration": SIGNED_URL_TTL,
            "method": "PUT",
            "content_type": mime_type,
        }

        client_credentials = self._client._credentials
        if getattr(client_credentials, "signer_email", None) and hasattr(
            client_credentials, "sign_bytes"
        ):
            # A key or an impersonated identity: it can sign for itself.
            return str(blob.generate_signed_url(**options))

        email, token = self._iam_signer()
        return str(
            blob.generate_signed_url(**options, service_account_email=email, access_token=token)
        )

    def _iam_signer(self) -> tuple[str, str]:
        """The `(service account, access token)` pair the IAM signing path needs.

        Deliberately *not* the storage client's credentials — see the class docstring.
        """
        import google.auth
        from google.auth.transport.requests import Request

        if self._signing_credentials is None:
            self._signing_credentials, _project = google.auth.default(
                scopes=list(self.SIGNING_SCOPES)
            )

        credentials = self._signing_credentials
        request = Request()
        if not credentials.valid:
            credentials.refresh(request)

        email = getattr(credentials, "service_account_email", None)
        if not email or email == "default":
            # On the metadata server the real address arrives with the token, not before
            # it, so an unrefreshed credential reports the literal string "default".
            credentials.refresh(request)
            email = getattr(credentials, "service_account_email", None)

        if not email or email == "default":
            raise RuntimeError(
                "Cannot sign an upload URL: these credentials can neither sign for "
                "themselves nor name a service account to sign as. On Cloud Run this "
                "means the metadata server did not return an identity."
            )
        return str(email), str(credentials.token)

    async def stat(self, object_name: str) -> tuple[int, str] | None:
        import asyncio

        def _stat() -> tuple[int, str] | None:
            blob = self._bucket.get_blob(object_name)
            if blob is None:
                return None
            return int(blob.size or 0), str(blob.content_type or "")

        return await asyncio.to_thread(_stat)

    async def download(self, object_name: str) -> bytes | None:
        import asyncio

        def _download() -> bytes | None:
            blob = self._bucket.get_blob(object_name)
            return None if blob is None else bytes(blob.download_as_bytes())

        return await asyncio.to_thread(_download)


class InMemoryObjectStore:
    """Local and test stand-in. Accepts a PUT that never happens.

    `stat` reports whatever size the caller declared, because there is no object to
    measure. That makes the *finalize* step's size and MIME verification vacuous locally
    — which is honest: without a real bucket there is nothing to verify against, and
    pretending otherwise would give a false sense that the check is covered.
    """

    def __init__(self, bucket_name: str = "local-uploads") -> None:
        self._bucket_name = bucket_name
        self._declared: dict[str, tuple[int, str]] = {}
        self._content: dict[str, bytes] = {}

    @property
    def bucket(self) -> str:
        return self._bucket_name

    def declare(
        self, object_name: str, size_bytes: int, mime_type: str, content: bytes = b""
    ) -> None:
        """Stand in for a PUT that never happened. For unit tests."""
        self._declared[object_name] = (size_bytes, mime_type)
        self._content[object_name] = content

    def put(self, object_name: str, content: bytes, mime_type: str) -> None:
        """Record a PUT that *did* happen, via `api/routers/local_storage.py`.

        The size and type recorded here are the ones actually received, so `finalize`'s
        checks do real work locally instead of agreeing with whatever the client declared.
        """
        self._declared[object_name] = (len(content), mime_type)
        self._content[object_name] = content

    async def signed_put_url(self, object_name: str, *, mime_type: str) -> str:
        """A URL on this service rather than on GCS.

        Same-origin, so a browser can PUT to it without CORS, and reachable — which the
        previous `https://storage.local/…` placeholder was not, leaving the whole upload
        path without end-to-end coverage. The receiving route exists only under
        `ENV=local`; see `api/routers/local_storage.py`.
        """
        stamp = int(now().timestamp())
        return f"/api/local-storage/{object_name}?upload={stamp}"

    async def stat(self, object_name: str) -> tuple[int, str] | None:
        return self._declared.get(object_name)

    async def download(self, object_name: str) -> bytes | None:
        if object_name not in self._declared:
            return None
        return self._content.get(object_name, b"")


def build_object_store(settings: Settings) -> ObjectStore:
    if not settings.upload_bucket:
        if not settings.is_local:  # pragma: no cover - Settings rejects this earlier
            raise ValueError("UPLOAD_BUCKET is required outside ENV=local.")
        logger.info("no UPLOAD_BUCKET set; uploads use the in-memory object store")
        return InMemoryObjectStore()
    return GcsObjectStore(settings.upload_bucket)


__all__ = [
    "SIGNED_URL_TTL",
    "GcsObjectStore",
    "InMemoryObjectStore",
    "ObjectStore",
    "build_object_store",
]
