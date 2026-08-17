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
from typing import Protocol

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

    def signed_put_url(self, object_name: str, *, mime_type: str) -> str:
        """A V4 resumable PUT URL for `object_name`."""

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
    """The real thing. Signs through IAM SignBlob — no service-account key anywhere."""

    def __init__(self, bucket_name: str) -> None:
        from google.cloud import storage  # type: ignore[attr-defined]

        self._bucket_name = bucket_name
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)

    @property
    def bucket(self) -> str:
        return self._bucket_name

    def signed_put_url(self, object_name: str, *, mime_type: str) -> str:
        blob = self._bucket.blob(object_name)
        return str(
            blob.generate_signed_url(
                version="v4",
                expiration=SIGNED_URL_TTL,
                method="PUT",
                content_type=mime_type,
            )
        )

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
        """Stand in for a PUT that never happened."""
        self._declared[object_name] = (size_bytes, mime_type)
        self._content[object_name] = content

    def signed_put_url(self, object_name: str, *, mime_type: str) -> str:
        stamp = int(now().timestamp())
        return f"https://storage.local/{self._bucket_name}/{object_name}?upload={stamp}"

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
