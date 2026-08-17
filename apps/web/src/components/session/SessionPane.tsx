/**
 * One conversation with the coach: transcript, composer, drop target, reconnect banner.
 *
 * Extracted at M3, when the board grew an intake conversation of its own
 * (docs/04-api-contract.md: `POST /api/projects` opens a session with `taskId: null`).
 * The two places a session appears differ only in what sits *next* to them — a task's
 * detail, or the board — and the pane itself is the same object in both. Copying it would
 * have meant two `turn_complete` handoffs to keep in step, and that handoff is the one
 * piece of this screen with an ordering that matters.
 *
 * **The handoff, since it is the subtle part.** While a turn streams, its text lives in
 * `useStreamStore` and only the bubble re-renders. On completion the transcript query is
 * refetched *and then* the buffer is cleared — in that order, because clearing first
 * blanks the finished message for however long the refetch takes
 * (docs/06-frontend.md#state-management-split).
 *
 * **The drop target is the whole pane, not the composer strip.** A two-line strip is a
 * target people miss, and missing it is worse than having none: the browser's default
 * action for a file dropped on a page is to navigate away from the app.
 */

import { useEffect, useRef, useState, type DragEvent } from 'react'

import { Composer } from '@/components/session/Composer'
import { ConfirmationPrompt } from '@/components/session/ConfirmationPrompt'
import { ConnectionBanner } from '@/components/session/ConnectionBanner'
import { Transcript } from '@/components/session/Transcript'
import { useCancelTurn, useSessionEvents, useStartTurn } from '@/features/queries'
import { useAttachmentUploads } from '@/features/use-uploads'
import { pendingConfirmation, toMessages } from '@/lib/transcript'
import { useComposerStore } from '@/stores/composer'
import { useStreamStore } from '@/stores/stream'

/** Whether a drag carries files, as opposed to selected text or a dragged link. */
function hasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer.types).includes('Files')
}

export interface SessionPaneProps {
  sessionId: string
  /** Announced to assistive technology; the heading itself is visually hidden. */
  heading: string
  /** Shown above the composer while the conversation is still empty. */
  emptyHint?: string
  className?: string
}

const DEFAULT_CLASS =
  'relative flex min-h-[28rem] flex-1 flex-col rounded-lg border lg:min-h-0'

export function SessionPane({
  sessionId,
  heading,
  emptyHint,
  className = DEFAULT_CLASS,
}: SessionPaneProps) {
  const events = useSessionEvents(sessionId)
  const startTurn = useStartTurn(sessionId)
  const cancelTurn = useCancelTurn(sessionId)

  const turns = useStreamStore((state) => state.turns)
  const clearTurn = useStreamStore((state) => state.clear)
  const resetComposer = useComposerStore((state) => state.reset)
  const { uploadAll } = useAttachmentUploads(sessionId)
  const [dragDepth, setDragDepth] = useState(0)

  const live = Object.values(turns).find((turn) => turn.sessionId === sessionId) ?? null

  // The handoff. Refetch first, then drop the buffer.
  const handled = useRef<string | null>(null)
  useEffect(() => {
    if (!live || live.status !== 'complete' || handled.current === live.turnId) return
    handled.current = live.turnId
    const turnId = live.turnId
    void events.refetch().then(() => clearTurn(turnId))
  }, [live, events, clearTurn])

  const messages = toMessages(events.data ?? [])
  const streaming = live?.status === 'running'
  // Only while nothing is generating: the request is answered by starting a turn, and a
  // second turn on a session that already has one running is a conflict.
  const pending = streaming ? null : pendingConfirmation(events.data ?? [])
  const headingId = `session-heading-${sessionId || 'pending'}`

  return (
    <section
      className={className}
      aria-labelledby={headingId}
      onDragEnter={(event) => {
        if (!hasFiles(event)) return
        event.preventDefault()
        // `dragDepth` rather than a boolean, because `dragenter`/`dragleave` fire for
        // every descendant the pointer crosses — a boolean flickers off the moment the
        // cursor moves from the transcript onto a message bubble.
        setDragDepth((depth) => depth + 1)
      }}
      onDragOver={(event) => {
        if (hasFiles(event)) event.preventDefault()
      }}
      onDragLeave={() => setDragDepth((depth) => Math.max(0, depth - 1))}
      onDrop={(event) => {
        if (!hasFiles(event)) return
        event.preventDefault()
        setDragDepth(0)
        uploadAll(event.dataTransfer.files)
      }}
    >
      <h2 id={headingId} className="sr-only">
        {heading}
      </h2>

      {dragDepth > 0 ? (
        <div
          className="bg-background/85 border-primary pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg border-2 border-dashed"
          data-testid="drop-overlay"
        >
          <p className="text-sm font-medium">Drop to attach</p>
        </div>
      ) : null}

      <div className="px-3 pt-3">
        <ConnectionBanner />
      </div>

      {emptyHint && messages.length === 0 && !events.isPending ? (
        <p className="text-muted-foreground px-4 pt-3 text-sm" data-testid="session-hint">
          {emptyHint}
        </p>
      ) : null}

      <Transcript
        messages={messages}
        live={live}
        pending={events.isPending}
        sessionId={sessionId}
      />

      {pending ? (
        <ConfirmationPrompt
          pending={pending}
          disabled={startTurn.isPending}
          onAnswer={(confirmed) =>
            startTurn.mutate({
              text: '',
              confirmation: { functionCallId: pending.functionCallId, confirmed },
            })
          }
        />
      ) : null}

      {sessionId ? (
        <Composer
          sessionId={sessionId}
          sending={startTurn.isPending}
          streaming={Boolean(streaming)}
          onSend={(text, attachments) => {
            // `attachments` carries `filename` for the optimistic echo; `api.startTurn`
            // strips it, because `TurnAttachment` forbids unknown fields.
            startTurn.mutate({ text, attachments })
            resetComposer(sessionId)
          }}
          onCancel={() => {
            if (live) cancelTurn.mutate(live.turnId)
          }}
        />
      ) : null}
    </section>
  )
}
