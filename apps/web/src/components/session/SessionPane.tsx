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
 * **A finished turn also invalidates the board**, and that is not a duplicate of the
 * `board_update` push. Board updates are frames, and frames are not checkpointed: a
 * client whose socket was down while a tool ran gets its *text* back on resume and never
 * hears that the board moved. From M5 the same is true of a run executing on another
 * instance. So the push is what makes the board feel live, and this is what makes it
 * correct — one refetch per turn, against a `staleTime` that would otherwise hold a stale
 * board for thirty seconds after the coach rewrote it.
 *
 * **The drop target is the whole pane, not the composer strip.** A two-line strip is a
 * target people miss, and missing it is worse than having none: the browser's default
 * action for a file dropped on a page is to navigate away from the app.
 *
 * **The pane is height-bounded on mobile, and that is a correctness constraint rather
 * than styling.** `Transcript` scrolls itself to the bottom on every delta — several times
 * a second while a reply streams. If the pane is free to grow with its content, the
 * element that scrolls is the *page*, so on a narrow viewport the composer is pushed below
 * the fold and then yanked out from under the reader's finger a few times a second: the
 * Cancel button becomes unclickable exactly while there is something to cancel. Bounded,
 * the scrolling stays inside the transcript, which is where the overflow was always meant
 * to be.
 */

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type DragEvent } from 'react';

import { Composer } from '@/components/session/Composer';
import { ConfirmationPrompt } from '@/components/session/ConfirmationPrompt';
import { ConnectionBanner } from '@/components/session/ConnectionBanner';
import { QuestionPrompt } from '@/components/session/QuestionPrompt';
import { Transcript } from '@/components/session/Transcript';
import { useCancelTurn, useSessionEvents, useStartTurn } from '@/features/queries';
import { useAttachmentUploads } from '@/features/use-uploads';
import { pendingConfirmation, toMessages } from '@/lib/transcript';
import { useComposerStore } from '@/stores/composer';
import { newestTurnFor, useStreamStore } from '@/stores/stream';

/** Whether a drag carries files, as opposed to selected text or a dragged link. */
function hasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer.types).includes('Files');
}

export interface SessionPaneProps {
  sessionId: string;
  /** The board a turn in this session may change. See the module docstring. */
  projectId: string;
  /** Announced to assistive technology; the heading itself is visually hidden. */
  heading: string;
  /** Shown above the composer while the conversation is still empty. */
  emptyHint?: string;
  className?: string;
}

const DEFAULT_CLASS =
  'relative flex h-[70svh] flex-col rounded-lg border lg:h-auto lg:min-h-0 lg:flex-1';

export function SessionPane({
  sessionId,
  projectId,
  heading,
  emptyHint,
  className = DEFAULT_CLASS,
}: SessionPaneProps) {
  const queryClient = useQueryClient();
  const events = useSessionEvents(sessionId);
  const startTurn = useStartTurn(sessionId);
  const cancelTurn = useCancelTurn(sessionId);

  const turns = useStreamStore((state) => state.turns);
  const clearTurn = useStreamStore((state) => state.clear);
  const resetComposer = useComposerStore((state) => state.reset);
  const { uploadAll } = useAttachmentUploads(sessionId);
  const [dragDepth, setDragDepth] = useState(0);

  // The *newest* turn for this session, never merely the first one found. See
  // `newestTurnFor`: a turn that ended in `turn_error` is not cleared by anything, so a
  // first-match lookup left a failed turn in front of every later one — red error stuck on
  // screen, next reply streaming into a buffer nobody rendered.
  const live = newestTurnFor(turns, sessionId);

  // The handoff. Refetch first, then drop the buffer.
  const handled = useRef<string | null>(null);
  useEffect(() => {
    if (!live || live.status !== 'complete' || handled.current === live.turnId) return;
    handled.current = live.turnId;
    const turnId = live.turnId;
    void events.refetch().then(() => clearTurn(turnId));
    if (projectId) {
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: ['project', projectId] });
    }
  }, [live, events, clearTurn, projectId, queryClient]);

  const messages = toMessages(events.data ?? []);
  const streaming = live?.status === 'running';
  // Only while nothing is generating: the request is answered by starting a turn, and a
  // second turn on a session that already has one running is a conflict.
  const pending = streaming ? null : pendingConfirmation(events.data ?? []);
  const headingId = `session-heading-${sessionId || 'pending'}`;

  return (
    <section
      className={className}
      aria-labelledby={headingId}
      onDragEnter={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        // `dragDepth` rather than a boolean, because `dragenter`/`dragleave` fire for
        // every descendant the pointer crosses — a boolean flickers off the moment the
        // cursor moves from the transcript onto a message bubble.
        setDragDepth((depth) => depth + 1);
      }}
      onDragOver={(event) => {
        if (hasFiles(event)) event.preventDefault();
      }}
      onDragLeave={() => setDragDepth((depth) => Math.max(0, depth - 1))}
      onDrop={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        setDragDepth(0);
        uploadAll(event.dataTransfer.files);
      }}
    >
      <h2 id={headingId} className="sr-only">
        {heading}
      </h2>

      {dragDepth > 0 ? (
        <div
          className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg border-2 border-dashed border-primary bg-background/85"
          data-testid="drop-overlay"
        >
          <p className="text-sm font-medium">Drop to attach</p>
        </div>
      ) : null}

      <div className="px-3 pt-3">
        <ConnectionBanner />
      </div>

      {emptyHint && messages.length === 0 && !events.isPending ? (
        <p className="px-4 pt-3 text-sm text-muted-foreground" data-testid="session-hint">
          {emptyHint}
        </p>
      ) : null}

      <Transcript
        messages={messages}
        live={live}
        pending={events.isPending}
        sessionId={sessionId}
      />

      {/*
        One pending confirmation, two shapes. `ask_learner` asks a *question* through the
        same handshake that gates `discard_task`, so what distinguishes them is the
        payload, not the mechanism — and a payload that fails to parse falls back to the
        buttons rather than rendering nothing.
      */}
      {pending?.question ? (
        <QuestionPrompt
          question={pending.question}
          disabled={startTurn.isPending}
          onAnswer={(answer) =>
            startTurn.mutate({
              text: '',
              confirmation: {
                functionCallId: pending.functionCallId,
                // Declining is `confirmed: false`, which is what ADK's own model means by
                // it, and `ask_learner` reads as "they answered none of these".
                confirmed: answer !== null,
                ...(answer
                  ? { payload: { selected: answer.selected, note: answer.note } }
                  : {}),
              },
            })
          }
        />
      ) : pending ? (
        <ConfirmationPrompt
          pending={pending}
          disabled={startTurn.isPending}
          onAnswer={(confirmed, payload) =>
            startTurn.mutate({
              text: '',
              confirmation: {
                functionCallId: pending.functionCallId,
                confirmed,
                ...(payload ? { payload } : {}),
              },
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
            startTurn.mutate({ text, attachments });
            resetComposer(sessionId);
          }}
          onCancel={() => {
            if (live) cancelTurn.mutate(live.turnId);
          }}
        />
      ) : null}
    </section>
  );
}
