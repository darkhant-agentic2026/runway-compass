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
 *
 * **The live buffer is one or more segments, not one bubble** (`stores/stream.ts`'s module
 * docstring): a research/roadmap turn is several named agents writing into the same turn,
 * and each gets its own card here, the same way each becomes its own stored event once the
 * turn settles. `MessageBlock` is the one place that renders "an author line, its tool
 * chips, its bubble" and is used for both a settled message and a still-streaming segment,
 * so the two cannot drift into looking different.
 */

import { useEffect, useRef } from 'react';

import { Markdown } from '@/components/markdown/Markdown';
import { AttachmentPreview } from '@/components/session/AttachmentPreview';
import { CopyMessage } from '@/components/session/CopyMessage';
import { ToolChips, type ChipView } from '@/components/session/ToolChips';
import { describeTool } from '@/lib/tool-labels';
import type { TranscriptAttachment, TranscriptMessage } from '@/lib/transcript';
import { cn } from '@/lib/utils';
import type { StreamSegment, StreamState, ToolChip } from '@/stores/stream';

/** A settled message's tools. Stored chips are always closed — the turn is over. */
function settledChips(message: TranscriptMessage): ChipView[] {
  return message.tools.map((tool) => ({
    id: tool.callId,
    name: tool.name,
    done: true,
    ok: tool.ok,
    detail: tool.detail,
  }));
}

/** The live buffer's chips, which may still be open. Keyed by the `seq` they arrived on. */
function liveChips(tools: ToolChip[]): ChipView[] {
  return tools.map((chip) => ({
    id: String(chip.seq),
    name: chip.name,
    done: chip.done,
    ok: chip.ok,
    // Arguments only: `tool_result` carries no payload, so a live chip says what the coach
    // set out to do and the settled one says what came of it.
    detail: describeTool(chip.name, chip.args),
  }));
}

/**
 * "An author line, its tool chips, its bubble" — one settled message or one still-streaming
 * segment, rendered identically either way.
 *
 * `streaming` is only true for the live segment still receiving deltas — every earlier
 * segment of the same turn (and every settled message) renders its text as plain markdown,
 * not the streaming variant `Markdown` reserves for text still arriving.
 */
function MessageBlock({
  id,
  eventId,
  role,
  author,
  text,
  chips,
  attachments = [],
  sessionId,
  seq,
  showEventIds = false,
  streaming = false,
  placeholder = null,
}: {
  id: string;
  /**
   * The stored ADK event id the debug toggle shows, or `null` for a live segment — it has
   * none yet, so the toggle shows the author alone rather than a synthetic stand-in.
   */
  eventId: string | null;
  role: 'user' | 'model';
  author: string;
  text: string;
  chips: ChipView[];
  attachments?: TranscriptAttachment[];
  sessionId: string;
  seq: number;
  showEventIds?: boolean;
  streaming?: boolean;
  /** Shown in place of a bubble when there is no text yet — "Your coach is thinking…". */
  placeholder?: React.ReactNode;
}) {
  return (
    <div className="space-y-2">
      {/*
        The author line. Shown for a model message whenever there is more than one
        possible author to distinguish — which for an ordinary coach conversation is
        never true, but for a research or roadmap transcript is the whole point: several
        named agents (`research_planner`, `topic_researcher`, `task_proposer`,
        `plan_tailor`, …) write into the *same* session, and nothing else on screen
        says which one produced a given message. Not shown for the learner's own
        messages — `author` there is always literally `"user"`, which the bubble's own
        alignment already says.
      */}
      {role === 'model' || showEventIds ? (
        <div
          className={cn(
            'flex text-xs text-muted-foreground',
            role === 'user' ? 'justify-end' : 'justify-start',
          )}
          data-testid="message-meta"
        >
          <span>
            {role === 'model' ? author : null}
            {role === 'model' && showEventIds && eventId ? ' · ' : null}
            {showEventIds && eventId ? <code>{eventId}</code> : null}
          </span>
        </div>
      ) : null}
      {/*
        Above the bubble, matching the live stream's order: the coach says what it is
        about to do, does it, then reports. Reading the settled transcript top to
        bottom should feel like having watched it happen.
      */}
      <ToolChips tools={chips} />
      {text || attachments.length > 0 ? (
        <Bubble role={role}>
          {text ? (
            streaming ? (
              // docs/06-frontend.md: streaming text uses `aria-live="polite"` on a
              // debounced *container*, not per token — announcing every delta would be
              // screen-reader spam. The container is the whole bubble, so a reader
              // announces settled text rather than each arriving fragment.
              <div aria-live="polite" aria-atomic="false">
                <Markdown text={text} streaming />
              </div>
            ) : (
              <MessageText role={role} text={text} />
            )
          ) : null}
          {attachments.length > 0 ? (
            <ul
              className={cn('flex flex-wrap items-end gap-2', text && 'mt-2')}
              data-testid="message-attachments"
            >
              {attachments.map((attachment, index) => (
                <AttachmentPreview
                  key={`${id}-${index}`}
                  attachment={attachment}
                  sessionId={sessionId}
                  // A message still in flight has no stored event to fetch bytes
                  // from, so `seq: 0` tells the preview to stay a chip until the
                  // refetch replaces it.
                  seq={id.startsWith('pending:') ? 0 : seq}
                  index={index}
                  tone={role}
                />
              ))}
            </ul>
          ) : null}
        </Bubble>
      ) : (
        placeholder
      )}
      {/*
        Under the bubble and aligned with it, so the control sits with the message it
        copies rather than in a corner of the row. Only when there is text: copying an
        attachment-only message would put an empty string on the clipboard.
      */}
      {text ? (
        <div className={cn('flex', role === 'user' ? 'justify-end' : 'justify-start')}>
          <CopyMessage text={text} />
        </div>
      ) : null}
    </div>
  );
}

/** One still-streaming segment of the live turn. */
function LiveSegment({
  segment,
  index,
  turnId,
  status,
  sessionId,
  showEventIds,
  isLast,
}: {
  segment: StreamSegment;
  index: number;
  turnId: string;
  status: StreamState['status'];
  sessionId: string;
  showEventIds: boolean;
  isLast: boolean;
}) {
  // Only the trailing segment of a still-running turn is "streaming" — every earlier
  // segment in this turn already has its final text, the same as a settled message does.
  const streaming = isLast && status === 'running';
  return (
    <MessageBlock
      id={`${turnId}:${index}`}
      eventId={null}
      role="model"
      author={segment.author}
      text={segment.text}
      chips={liveChips(segment.tools)}
      sessionId={sessionId}
      seq={0}
      showEventIds={showEventIds}
      streaming={streaming}
      placeholder={
        streaming ? (
          <Bubble role="model">
            <p className="text-muted-foreground" data-testid="still-working">
              Your coach is thinking…
            </p>
          </Bubble>
        ) : null
      }
    />
  );
}

export function Transcript({
  messages,
  live,
  pending,
  sessionId,
  showEventIds = false,
}: {
  messages: TranscriptMessage[];
  /** The turn currently streaming, if any. */
  live: StreamState | null;
  pending: boolean;
  /** Needed to fetch an attachment's bytes for a preview. */
  sessionId: string;
  /**
   * Debugging (`stores/debugUi.ts`): shows each stored message's raw ADK event id next to
   * its author. Off by default — a normal coach conversation has one author for the whole
   * transcript and nothing to compare; it earns its place once a session holds several
   * named agents' events, which is a research or roadmap run's transcript.
   */
  showEventIds?: boolean;
}) {
  const viewport = useRef<HTMLDivElement>(null);
  const liveTextLength =
    live?.segments.reduce((total, segment) => total + segment.text.length, 0) ?? 0;

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
  }, [messages.length, liveTextLength]);

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
        <MessageBlock
          key={message.id}
          id={message.id}
          eventId={message.id}
          role={message.role}
          author={message.author}
          text={message.text}
          chips={settledChips(message)}
          attachments={message.attachments}
          sessionId={sessionId}
          seq={message.seq}
          showEventIds={showEventIds}
        />
      ))}

      {live ? (
        <div className="space-y-4" data-testid="live-turn">
          {live.segments.length > 0 ? (
            live.segments.map((segment, index) => (
              <LiveSegment
                key={`${live.turnId}:${index}`}
                segment={segment}
                index={index}
                turnId={live.turnId}
                status={live.status}
                sessionId={sessionId}
                showEventIds={showEventIds}
                isLast={index === live.segments.length - 1}
              />
            ))
          ) : live.status === 'running' ? (
            <Bubble role="model">
              <p className="text-muted-foreground" data-testid="still-working">
                Your coach is thinking…
              </p>
            </Bubble>
          ) : null}
          {live.status === 'error' && live.error ? (
            <p role="alert" className="text-sm text-destructive" data-testid="turn-error">
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
 * **Both roles render as markdown, and the learner's with soft breaks.** This reverses an
 * earlier decision, so the reasoning that overturned it is worth keeping: the objection was
 * that rendering a learner's message "would collapse the line breaks they put in, silently
 * reflow a pasted stack trace, and turn a `*` they meant literally into emphasis", because
 * the transcript is the record of what they sent.
 *
 * Two of those three are now answered rather than accepted. `remark-breaks` keeps the line
 * breaks, so a pasted traceback stays a pasted traceback. And every message carries a copy
 * control that yields the *source* text, so the exact thing they sent is still one click
 * away — which is what "the record of what they sent" actually needed, rather than
 * withholding the formatting they meant. The literal `*` remains a real cost, and it is the
 * smaller one: people who write `**important**` in a chat box mean emphasis far more often
 * than people who mean a star.
 *
 * A settled message is never streaming, since streaming text lives in the live buffer.
 */
function MessageText({ role, text }: { role: 'user' | 'model'; text: string }) {
  return <Markdown text={text} softBreaks={role === 'user'} />;
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
