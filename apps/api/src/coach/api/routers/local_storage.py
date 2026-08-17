"""A stand-in for the GCS bucket, so uploads are reachable from a test.

**Registered only when `ENV=local`** (`coach.main.create_app`), on the same footing as the
`Bearer dev:<uid>` auth path and `MODEL_BACKEND=stub`: deliberate test-only surface, one
guard, and a named regression test for every other `ENV`
(`tests/test_local_storage_guard.py`).

## Why it exists

docs/07-infra-deploy.md lists storage as one of the two local dependencies that are *not*
emulated, and that was true in a way that mattered: `InMemoryObjectStore` handed the
browser a `https://storage.local/…` URL, so no Playwright flow could ever complete an
upload. The whole attachment path — the picker, the drop zone, finalize, the transcript —
had no end-to-end coverage at all.

Two defects shipped through that gap. The `<Toaster />` was never mounted, so a 500 on
`POST /api/uploads` produced no visible change whatsoever; and attachments vanished from
reopened conversations because the transcript read `fileData` where Firestore holds
`file_data`. Neither is exotic, and a flow that attached a file and reopened the tab would
have caught both.

## What it deliberately does not do

It is not a GCS emulator. It accepts a PUT and records the bytes and the content type in
the process's `InMemoryObjectStore`, which is exactly enough for `finalize` to do real work
locally — its size and MIME checks stop being vacuous, because there is finally an object
to check. Signing is still not exercised (the in-memory store returns an unsigned URL), and
that remains unreachable without a real signer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response, status

from coach.api.deps import Container, get_container
from coach.core.errors import NotFound
from coach.integrations.storage import InMemoryObjectStore
from coach.services.uploads import MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

#: Matches the path `InMemoryObjectStore.signed_put_url` hands out.
PREFIX = "/api/local-storage"

router = APIRouter(prefix=PREFIX, tags=["local-storage"])


@router.put("/{object_name:path}", status_code=status.HTTP_200_OK)
async def put_object(object_name: str, request: Request) -> Response:
    """Accept the browser's direct PUT, as GCS would.

    Unauthenticated, like the signed URL it stands in for: the capability is the URL. That
    is only acceptable because this router does not exist outside `ENV=local`.
    """
    container: Container = get_container(request)
    store = container.object_store
    if not isinstance(store, InMemoryObjectStore):  # pragma: no cover - local only
        raise NotFound("This build does not use the in-memory object store.")

    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        # GCS would refuse on the signed URL's own terms; the shape of the refusal is what
        # matters to the client, not the wording.
        return Response(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    store.put(
        object_name,
        body,
        request.headers.get("Content-Type", "application/octet-stream"),
    )
    logger.info(
        "local-storage accepted an object",
        extra={"object_name": object_name, "size_bytes": len(body)},
    )
    return Response(status_code=status.HTTP_200_OK)


__all__ = ["PREFIX", "router"]
