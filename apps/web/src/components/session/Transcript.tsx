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

import { useEffect, useRef } from 'react';

import { Markdown } from '@/components/markdown/Markdown';
import { AttachmentPreview } from '@/components/session/AttachmentPreview';
import { ToolChips, type ChipView } from '@/components/session/ToolChips';
import type { TranscriptMessage } from '@/lib/transcript';
import { cn } from '@/lib/utils';
import type { StreamState, ToolChip } from '@/stores/stream';

/** A settled message's tools. Stored chips are always closed — the turn is over. */
function settledChips(message: TranscriptMessage): ChipView[] {
  return message.tools.map((tool) => ({
    id: tool.callId,
    name: tool.name,
    done: true,
    ok: tool.ok,
  }));
}

/** The live buffer's chips, which may still be open. Keyed by the `seq` they arrived on. */
function liveChips(tools: ToolChip[]): ChipView[] {
  return tools.map((chip) => ({
    id: String(chip.seq),
    name: chip.name,
    done: chip.done,
    ok: chip.ok,
  }));
}

export function Transcript({
  messages,
  live,
  pending,
  sessionId,
}: {
  messages: TranscriptMessage[];
  /** The turn currently streaming, if any. */
  live: StreamState | null;
  pending: boolean;
  /** Needed to fetch an attachment's bytes for a preview. */
  sessionId: string;
}) {
  const viewport = useRef<HTMLDivElement>(null);

  // Scroll *this* container, not `bottom.scrollIntoView()`.
  //
  // `scrollIntoView` scrolls every scrollable ancestor, the document included, so on a
  // viewport too short to hold the whole screen it moved the *page* — several times a
  // second, once per delta, for the whole of a streaming reply. The composer walked under
  // the reader's finger and the Cancel button was unreachable exactly while there was
  // something to cancel. It also fought anyone scrolling up to reread an earlier message.
  //
  // Assigning `scrollTop` cannot escape the element, which is what "keep the transcript
  // pinned to its own bottom" actually means.
  useEffect(() => {
    const element = viewport.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [messages.length, live?.text]);

  if (pending && messages.length === 0) {
    return <p className="p-4 text-muted-foreground">Loading the conversation…</p>;
  }

  const empty = messages.length === 0 && !live;

  return (
    <div
      ref={viewport}
      className="flex-1 space-y-4 overflow-y-auto p-4"
      data-testid="transcript"
    >
      {empty ? (
        <p className="rounded-lg border border-dashed p-6 text-center text-muted-foreground">
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
              {message.text ? <MessageText role={message.role} text={message.text} /> : null}
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
              <div aria-live="polite" aria-atomic="false">
                <Markdown text={live.text} streaming={live.status === 'running'} />
              </div>
            </Bubble>
          ) : live.status === 'running' ? (
            <Bubble role="model">
              <p className="text-muted-foreground" data-testid="still-working">
                Your coach is thinking…
              </p>
            </Bubble>
          ) : null}
          {live.status === 'error' && live.error ? (
            <p role="alert" className="text-sm text-destructive">
              {live.error.message}
              {live.error.retryable ? ' You can try again.' : ''}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * A message's text.
 *
 * **The coach's messages are markdown; the learner's are what they typed.** Rendering a
 * user's message as markdown would collapse the line breaks they put in, silently reflow
 * a pasted stack trace, and turn a `*` they meant literally into emphasis — the transcript
 * is the record of what they sent, so it shows that. The coach writes markdown on purpose
 * (docs/06-frontend.md#markdown-in-the-transcript), and a settled message is never
 * streaming, since streaming text lives in the live buffer instead.
 */
function MessageText({ role, text }: { role: 'user' | 'model'; text: string }) {
  if (role === 'user') return <p className="whitespace-pre-wrap">{text}</p>;
  return <Markdown text={text} />;
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
  );
}
