/**
 * The message composer.
 *
 * docs/06-frontend.md#task-workspace: "Composer with drag-and-drop upload (image/PDF/text),
 * preview thumbnails, paste-image support" and a "Cancel button (the only thing that stops
 * generation)".
 *
 * The upload flow is two steps because the browser PUTs straight to GCS
 * (docs/04-api-contract.md#uploads): ask for a signed URL, PUT to it, then finalize so the
 * server can check what actually landed. An attachment is not sendable until that third
 * step succeeds — see `PendingAttachment.ready`.
 */

import { Paperclip, Send, X } from 'lucide-react'
import { useRef, type ChangeEvent, type ClipboardEvent } from 'react'

import { Button } from '@/components/ui/button'
import { ACCEPTED_MIME_TYPES, useAttachmentUploads } from '@/features/use-uploads'
import { NO_ATTACHMENTS, useComposerStore } from '@/stores/composer'

export function Composer({
  sessionId,
  sending,
  streaming,
  onSend,
  onCancel,
}: {
  sessionId: string
  sending: boolean
  /** A turn is in flight; the send button becomes cancel. */
  streaming: boolean
  onSend: (text: string, attachments: { uploadId: string; mimeType: string }[]) => void
  onCancel: () => void
}) {
  const draft = useComposerStore((state) => state.drafts[sessionId] ?? '')
  // `NO_ATTACHMENTS` rather than `?? []`: a fresh array here is a new value on every
  // render as far as Zustand is concerned, and the result is an infinite render loop.
  const attachments = useComposerStore(
    (state) => state.attachments[sessionId] ?? NO_ATTACHMENTS,
  )
  const setDraft = useComposerStore((state) => state.setDraft)
  const removeAttachment = useComposerStore((state) => state.removeAttachment)
  const { uploadAll } = useAttachmentUploads(sessionId)
  const filePicker = useRef<HTMLInputElement>(null)

  const ready = attachments.filter((attachment) => attachment.ready)
  const canSend = (draft.trim().length > 0 || ready.length > 0) && !sending

  function submit(): void {
    if (!canSend) return
    onSend(
      draft.trim(),
      ready.map((attachment) => ({
        uploadId: attachment.uploadId,
        mimeType: attachment.mimeType,
      })),
    )
  }

  return (
    <form
      className="space-y-2 border-t p-3"
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      {/* Dropping is handled by the whole chat pane, not this strip — see
          `features/use-uploads.ts`. */}
      {attachments.length > 0 ? (
        <ul className="flex flex-wrap gap-2" data-testid="attachments">
          {attachments.map((attachment) => (
            <li
              key={attachment.uploadId}
              className="bg-muted flex items-center gap-1.5 rounded-md px-2 py-1 text-xs"
            >
              <span className={attachment.ready ? '' : 'text-muted-foreground'}>
                {attachment.filename}
                {attachment.ready ? '' : ' — uploading…'}
              </span>
              <button
                type="button"
                aria-label={`Remove ${attachment.filename}`}
                onClick={() => removeAttachment(sessionId, attachment.uploadId)}
              >
                <X className="size-3" aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="flex items-end gap-2">
        <label className="sr-only" htmlFor="composer">
          Message your coach
        </label>
        <textarea
          id="composer"
          rows={2}
          value={draft}
          placeholder="Ask a question, or paste your work…"
          className="border-input bg-background focus-visible:ring-ring min-h-16 flex-1 resize-y rounded-md border px-3 py-2 text-sm focus-visible:ring-2 focus-visible:outline-none"
          onChange={(event) => setDraft(sessionId, event.target.value)}
          onPaste={(event: ClipboardEvent) => {
            // Paste-image support: a screenshot in the clipboard arrives as a file item.
            const files = Array.from(event.clipboardData.items)
              .filter((item) => item.kind === 'file')
              .map((item) => item.getAsFile())
              .filter((file): file is File => file !== null)
            if (files.length > 0) uploadAll(files)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault()
              submit()
            }
          }}
        />

        <input
          ref={filePicker}
          type="file"
          multiple
          accept={ACCEPTED_MIME_TYPES.join(',')}
          className="hidden"
          onChange={(event: ChangeEvent<HTMLInputElement>) => {
            uploadAll(event.target.files)
            // Cleared so that re-picking the same file fires `change` again.
            event.target.value = ''
          }}
        />
        <Button
          type="button"
          variant="outline"
          size="icon"
          aria-label="Attach a file"
          onClick={() => filePicker.current?.click()}
        >
          <Paperclip className="size-4" aria-hidden="true" />
        </Button>

        {streaming ? (
          <Button type="button" variant="outline" onClick={onCancel}>
            Cancel
          </Button>
        ) : (
          <Button type="submit" disabled={!canSend} aria-label="Send">
            <Send className="size-4" aria-hidden="true" />
          </Button>
        )}
      </div>
    </form>
  )
}
