/**
 * Attaching a file, from any of the three ways docs/06-frontend.md asks for.
 *
 * > Composer with drag-and-drop upload (image/PDF/text), preview thumbnails,
 * > paste-image support.
 *
 * The picker and the paste live on the composer; the **drop target is the whole chat
 * pane**, because a strip two lines tall at the bottom of the screen is a target people
 * miss — and missing it is worse than nothing, since the browser's default action for a
 * file dropped on a page is to navigate away from the app to the file. That is why this
 * is a hook rather than three copies of the same code: two components need it, and both
 * must write to the same store or the composer would not know what was dropped.
 *
 * The flow is the contract's two steps plus the PUT in between
 * (docs/04-api-contract.md#uploads): ask for a signed URL, PUT straight to GCS, then
 * finalize so the server can check what actually landed. An attachment is not sendable
 * until the third step succeeds — `ready` is what the composer gates on.
 */

import { useCallback } from 'react'
import { toast } from 'sonner'

import { api, ApiError } from '@/lib/api'
import { useComposerStore } from '@/stores/composer'

/** docs/04-api-contract.md#uploads, mirrored so the picker filters and errors come early. */
export const ACCEPTED_MIME_TYPES = [
  'image/png',
  'image/jpeg',
  'image/webp',
  'application/pdf',
  'text/plain',
  'text/markdown',
]

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024

export interface Uploader {
  upload: (file: File) => Promise<void>
  uploadAll: (files: FileList | File[] | null) => void
}

export function useAttachmentUploads(sessionId: string): Uploader {
  const addAttachment = useComposerStore((state) => state.addAttachment)
  const markReady = useComposerStore((state) => state.markReady)
  const removeAttachment = useComposerStore((state) => state.removeAttachment)

  const upload = useCallback(
    async (file: File) => {
      if (!ACCEPTED_MIME_TYPES.includes(file.type)) {
        toast.error(`${file.name}: ${file.type || 'that file type'} cannot be attached.`)
        return
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        toast.error(`${file.name} is over the 20 MB limit.`)
        return
      }

      let uploadId: string | null = null
      try {
        const created = await api.createUpload({
          filename: file.name,
          mimeType: file.type,
          sizeBytes: file.size,
        })
        uploadId = created.uploadId
        // Shown immediately, before the bytes move, so a slow upload looks like progress
        // rather than like nothing having happened.
        addAttachment(sessionId, {
          uploadId: created.uploadId,
          filename: file.name,
          mimeType: file.type,
          ready: false,
        })

        const put = await fetch(created.signedUrl, {
          method: 'PUT',
          headers: { 'Content-Type': file.type },
          body: file,
        })
        if (!put.ok) {
          throw new Error(`the storage service rejected the upload (${put.status})`)
        }

        const finalized = await api.finalizeUpload(created.uploadId)
        markReady(sessionId, created.uploadId, finalized.mimeType)
      } catch (error) {
        // Drop the pending chip: leaving it would let the composer look armed with an
        // attachment the server will refuse.
        if (uploadId) removeAttachment(sessionId, uploadId)
        const detail =
          error instanceof ApiError
            ? error.problem.detail || error.problem.title
            : error instanceof Error
              ? error.message
              : 'unknown error'
        toast.error(`${file.name} could not be attached — ${detail}`)
      }
    },
    [sessionId, addAttachment, markReady, removeAttachment],
  )

  const uploadAll = useCallback(
    (files: FileList | File[] | null) => {
      for (const file of Array.from(files ?? [])) void upload(file)
    },
    [upload],
  )

  return { upload, uploadAll }
}
