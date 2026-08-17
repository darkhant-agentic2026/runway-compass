/**
 * An attachment in the transcript: a thumbnail when it is an image, a chip otherwise.
 *
 * Not specified anywhere in `docs/` — docs/06-frontend.md asks for preview thumbnails on
 * the *composer*, and says nothing about the transcript, so this is an addition rather
 * than an implementation of a decision. It is the behaviour every chat interface has, and
 * a conversation about a screenshot reads badly when the screenshot is a word.
 *
 * The bytes arrive through an authenticated `fetch` and become an object URL. An
 * `<img src>` pointing at the endpoint would be simpler and cannot work: an `<img>` sends
 * no `Authorization` header, and giving the URL its own credential would be a second way
 * into the data (docs/00-overview.md, decision 7: one auth path).
 *
 * Loading is lazy — an `IntersectionObserver` — because a long conversation should not
 * fetch every image in it to render the last screen.
 */

import { Paperclip } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { api } from '@/lib/api'
import { attachmentLabel, type TranscriptAttachment } from '@/lib/transcript'
import { cn } from '@/lib/utils'

function isImage(mimeType: string): boolean {
  return mimeType.startsWith('image/')
}

export function AttachmentPreview({
  attachment,
  sessionId,
  seq,
  index,
  tone,
}: {
  attachment: TranscriptAttachment
  sessionId: string
  seq: number
  index: number
  /** Which bubble it sits in, so the chip's background has enough contrast. */
  tone: 'user' | 'model'
}) {
  const label = attachmentLabel(attachment)
  const previewable = isImage(attachment.mimeType) && seq > 0
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  const holder = useRef<HTMLLIElement>(null)
  // Starts visible where there is no observer to ask — jsdom, and any environment without
  // it. Decided in the initializer rather than by setting state from inside the effect,
  // which is both a lint error and a wasted render.
  const [visible, setVisible] = useState(() => typeof IntersectionObserver === 'undefined')

  useEffect(() => {
    const node = holder.current
    if (!previewable || visible || !node || typeof IntersectionObserver === 'undefined') {
      return
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setVisible(true)
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [previewable, visible])

  useEffect(() => {
    if (!previewable || !visible) return
    let objectUrl: string | null = null
    let cancelled = false

    void api
      .getEventAttachment(sessionId, seq, index)
      .then((blob) => {
        if (cancelled) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      })
      .catch(() => {
        // A failed preview falls back to the chip. It is not worth a toast: the message
        // is still readable, and the attachment is still named.
        if (!cancelled) setFailed(true)
      })

    return () => {
      cancelled = true
      // Revoked on unmount, or a long conversation leaks a blob per image.
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [previewable, visible, sessionId, seq, index])

  if (previewable && !failed) {
    return (
      <li ref={holder} className="max-w-64">
        {url ? (
          <a href={url} target="_blank" rel="noreferrer">
            <img
              src={url}
              alt={label}
              className="max-h-48 w-auto rounded-md border object-contain"
              data-testid="attachment-image"
            />
          </a>
        ) : (
          <div
            className="bg-muted/60 h-24 w-40 animate-pulse rounded-md border"
            aria-label={`Loading ${label}`}
            data-testid="attachment-loading"
          />
        )}
        <span className="text-muted-foreground mt-1 block truncate text-xs">{label}</span>
      </li>
    )
  }

  return (
    <li
      ref={holder}
      className={cn(
        'flex items-center gap-1 rounded-full px-2 py-0.5 text-xs',
        tone === 'user' ? 'bg-primary-foreground/15' : 'bg-background/60',
      )}
    >
      <Paperclip className="size-3 shrink-0" aria-hidden="true" />
      <span>{label}</span>
    </li>
  )
}
