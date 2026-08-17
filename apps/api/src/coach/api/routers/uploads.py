"""Uploads (`/api/uploads`).

docs/04-api-contract.md#uploads. Two steps, because the browser PUTs directly to GCS and
the server is out of the data path in between — see `coach.services.uploads` for why the
finalize step is where the MIME type is actually decided.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status

from coach.api.deps import CurrentUser, Uploads
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import UploadCreate, UploadCreated, UploadFinalized

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


@router.post(
    "",
    response_model=UploadCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(idempotency_guard)],
)
async def create_upload(
    body: UploadCreate, principal: CurrentUser, uploads: Uploads
) -> UploadCreated:
    signed = await uploads.create(
        principal,
        filename=body.filename,
        mime_type=body.mime_type,
        size_bytes=body.size_bytes,
    )
    return UploadCreated(upload_id=signed.upload_id, signed_url=signed.signed_url)


@router.post(
    "/{upload_id}/finalize",
    response_model=UploadFinalized,
    dependencies=[Depends(idempotency_guard)],
)
async def finalize_upload(
    upload_id: str, principal: CurrentUser, uploads: Uploads
) -> UploadFinalized:
    """Verify what actually landed, then make the upload referenceable by a turn."""
    resolved = await uploads.finalize(principal, upload_id)
    return UploadFinalized(upload_id=resolved.upload_id, mime_type=resolved.mime_type)
