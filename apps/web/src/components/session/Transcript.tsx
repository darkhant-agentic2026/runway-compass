/**
 * The conversation.
 *
 * Two sources, joined here and nowhere else:
 *
 * - **finalized events** from `['session', sid, 'events']` — the durable transcript;
 * - **the live buffer** from `useStreamStore` — the turn currently streaming.
 *
 * They never overlap, because a turn's text moves from the second to the first exactly
 * once, on `turn_complete` (docs/06-frontend.md). Rendering the streaming bubble only
 * while its turn is not yet in the finalized set is what keeps a completed answer from
 * appearing twice for the moment between the frame and the refetch.
 */

import { useEffect, useRef } from 'react'

import { AttachmentPreview } from '@/components/session/AttachmentPreview'
import { ToolChips, type ChipView } from '@/components/session/ToolChips'
import type { TranscriptMessage } from '@/lib/transcript'
import { cn } from '@/lib/utils'
import type { StreamState, ToolChip } from '@/stores/stream'

/** A settled message's tools. Stored chips are always closed — the turn is over. */
function settledChips(message: TranscriptMessage): ChipView[] {
  return message.tools.map((tool) => ({
    id: tool.callId,
    name: tool.name,
    done: true,
    ok: tool.ok,
  }))
}

/** The live buffer's chips, which may still be open. Keyed by the `seq` they arrived on. */
function liveChips(tools: ToolChip[]): ChipView[] {
  return tools.map((chip) => ({
    id: String(chip.seq),
    name: chip.name,
    done: chip.done,
    ok: chip.ok,
  }))
}

export function Transcript({
  messages,
  live,
  pending,
  sessionId,
}: {
  messages: TranscriptMessage[]
  /** The turn currently streaming, if any. */
  live: StreamState | null
  pending: boolean
  /** Needed to fetch an attachment's bytes for a preview. */
  sessionId: string
}) {
  const bottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' })
  }, [messages.length, live?.text])

  if (pending && messages.length === 0) {
    return <p className="text-muted-foreground p-4">Loading the conversation…</p>
  }

  const empty = messages.length === 0 && !live

  return (
    <div className="flex-1 space-y-4 overflow-y-auto p-4" data-testid="transcript">
      {empty ? (
        <p className="text-muted-foreground rounded-lg border border-dashed p-6 text-center">
          Nothing here yet. Tell your coach what you are working on.
        </p>
      ) : null}

      {messages.map((message) => (
        <div key={message.id} className="space-y-2">
          {/*
            Above the bubble, matching the live stream's order: the coach says what it is
            about to do, does it, then reports. Reading the settled transcript top to
            bottom should feel like having watched it happen.
          */}
          <ToolChips tools={settledChips(message)} />
          {message.text || message.attachments.length > 0 ? (
            <Bubble role={message.role}>
              {message.text ? <p className="whitespace-pre-wrap">{message.text}</p> : null}
              {message.attachments.length > 0 ? (
                <ul
                  className={cn('flex flex-wrap items-end gap-2', message.text && 'mt-2')}
                  data-testid="message-attachments"
                >
                  {message.attachments.map((attachment, index) => (
                    <AttachmentPreview
                      key={`${message.id}-${index}`}
                      attachment={attachment}
                      sessionId={sessionId}
                      // A message still in flight has no stored event to fetch bytes
                      // from, so `seq: 0` tells the preview to stay a chip until the
                      // refetch replaces it.
                      seq={message.id.startsWith('pending:') ? 0 : message.seq}
                      index={index}
                      tone={message.role}
                    />
                  ))}
                </ul>
              ) : null}
            </Bubble>
          ) : null}
        </div>
      ))}

      {live ? (
        <div className="space-y-2" data-testid="live-turn">
          <ToolChips tools={liveChips(live.tools)} />
          {live.text ? (
            <Bubble role="model">
              {/*
                docs/06-frontend.md: streaming text uses `aria-live="polite"` on a
                debounced *container*, not per token — announcing every delta would be
                screen-reader spam. The container is the whole bubble, so a reader
                announces settled text rather than each arriving fragment.
              */}
              <p className="whitespace-pre-wrap" aria-live="polite" aria-atomic="false">
                {live.text}
              </p>
            </Bubble>
          ) : live.status === 'running' ? (
            <Bubble role="model">
              <p className="text-muted-foreground" data-testid="still-working">
                Your coach is thinking…
              </p>
            </Bubble>
          ) : null}
          {live.status === 'error' && live.error ? (
            <p role="alert" className="text-destructive text-sm">
              {live.error.message}
              {live.error.retryable ? ' You can try again.' : ''}
            </p>
          ) : null}
        </div>
      ) : null}

      <div ref={bottom} />
    </div>
  )
}

function Bubble({ role, children }: { role: 'user' | 'model'; children: React.ReactNode }) {
  return (
    <div className={cn('flex', role === 'user' ? 'justify-end' : 'justify-start')}>
      <div
        data-role={role}
        className={cn(
          'max-w-[85%] rounded-lg px-3 py-2 text-sm',
          role === 'user' ? 'bg-primary text-primary-foreground' : 'bg-muted',
        )}
      >
        {children}
      </div>
    </div>
  )
}
