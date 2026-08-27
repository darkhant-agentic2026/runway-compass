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
import { Button } from '@/components/ui/button';
import {
  useCancelTurn,
  useSessionEvents,
  useStartTurn,
  type StartTurnBody,
} from '@/features/queries';
import { useAttachmentUploads } from '@/features/use-uploads';
import { ApiError } from '@/lib/api';
import { pendingConfirmation, toMessages } from '@/lib/transcript';
import { useComposerStore } from '@/stores/composer';
import { useDebugUiStore } from '@/stores/debugUi';
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
  /**
   * No composer, no upload drop target, no confirmation prompts — added at M8 for the
   * research view (docs/06-frontend.md#research-view-projectsprojectidresearchrunid).
   * no node of the research pipeline has a tool that asks the learner anything, so this pane is a record
   * of what the coach did rather than a conversation; the one control still offered while
   * `readOnly` is the cancel button, because a research turn is generation like any other
   * and cancelling it is not a thing the composer alone should own.
   */
  readOnly?: boolean;
  /**
   * Fired once per turn, on the same handoff that refetches the transcript and
   * invalidates the board — added at M8 for the research view, which has its own
   * run/report queries this pane knows nothing about and needs refreshed the moment
   * generation ends, not on the next 2 s poll.
   */
  onTurnComplete?: () => void;
}

const DEFAULT_CLASS =
  'relative flex h-[70svh] flex-col rounded-lg border lg:h-auto lg:min-h-0 lg:flex-1';

export function SessionPane({
  sessionId,
  projectId,
  heading,
  emptyHint,
  className = DEFAULT_CLASS,
  readOnly = false,
  onTurnComplete,
}: SessionPaneProps) {
  const queryClient = useQueryClient();
  const events = useSessionEvents(sessionId);
  const startTurn = useStartTurn(sessionId);
  const cancelTurn = useCancelTurn(sessionId);

  const turns = useStreamStore((state) => state.turns);
  const clearTurn = useStreamStore((state) => state.clear);
  const resetComposer = useComposerStore((state) => state.reset);
  const showEventIds = useDebugUiStore((state) => state.showEventIds);
  const toggleShowEventIds = useDebugUiStore((state) => state.toggleShowEventIds);
  const { uploadAll } = useAttachmentUploads(sessionId);
  const [dragDepth, setDragDepth] = useState(0);

  // M8-quotas: a quota-blocked send never becomes a turn, so there is nothing to
  // "resume" the way a disconnected one is — this is the chat pane's own record of the
  // one message that did not go through, kept only long enough to offer a plain resend.
  const [blockedSend, setBlockedSend] = useState<{
    body: StartTurnBody;
    detail: string;
  } | null>(null);

  function sendTurn(body: StartTurnBody) {
    startTurn.mutate(body, {
      onSuccess() {
        setBlockedSend(null);
      },
      onError(error) {
        if (error instanceof ApiError && error.problem.type === '/problems/quota-exceeded') {
          setBlockedSend({ body, detail: error.problem.detail });
        }
      },
    });
  }

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
    onTurnComplete?.();
  }, [live, events, clearTurn, projectId, queryClient, onTurnComplete]);

  const messages = toMessages(events.data ?? []);
  const streaming = live?.status === 'running';
  // `live`, not `streaming`: a turn that just answered a confirmation goes `running` ->
  // `complete` before the transcript refetch (below) has actually landed, and
  // `pendingConfirmation` reading the *stale* `events.data` in that gap still finds the
  // same request unanswered — the confirmation prompt would flash back for the instant
  // between the turn completing and the refetch resolving. Gating on `live` itself (not
  // yet cleared until the refetch's `.then()` fires) means `events.data` is guaranteed
  // fresh by the time this ever falls through to computing from it.
  const pending = live ? null : pendingConfirmation(events.data ?? []);
  const headingId = `session-heading-${sessionId || 'pending'}`;

  return (
    <section
      className={className}
      aria-labelledby={headingId}
      onDragEnter={(event) => {
        if (readOnly || !hasFiles(event)) return;
        event.preventDefault();
        // `dragDepth` rather than a boolean, because `dragenter`/`dragleave` fire for
        // every descendant the pointer crosses — a boolean flickers off the moment the
        // cursor moves from the transcript onto a message bubble.
        setDragDepth((depth) => depth + 1);
      }}
      onDragOver={(event) => {
        if (!readOnly && hasFiles(event)) event.preventDefault();
      }}
      onDragLeave={() => {
        if (!readOnly) setDragDepth((depth) => Math.max(0, depth - 1));
      }}
      onDrop={(event) => {
        if (readOnly || !hasFiles(event)) return;
        event.preventDefault();
        setDragDepth(0);
        uploadAll(event.dataTransfer.files);
      }}
    >
      <h2 id={headingId} className="sr-only">
        {heading}
      </h2>

      {!readOnly && dragDepth > 0 ? (
        <div
          className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg border-2 border-dashed border-primary bg-background/85"
          data-testid="drop-overlay"
        >
          <p className="text-sm font-medium">Drop to attach</p>
        </div>
      ) : null}

      <div className="flex items-center justify-between gap-2 px-3 pt-3">
        <ConnectionBanner />
        {/*
          Debugging, not a feature the ordinary composer flow needs — see stores/debugUi.ts.
          Ghost + tiny so it reads as a developer affordance rather than a control the
          learner is meant to reach for.
        */}
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="shrink-0 text-xs text-muted-foreground"
          aria-pressed={showEventIds}
          data-testid="toggle-event-ids"
          onClick={toggleShowEventIds}
        >
          {showEventIds ? 'Hide event IDs' : 'Show event IDs'}
        </Button>
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
        showEventIds={showEventIds}
      />

      {/*
        One pending confirmation, two shapes. `ask_learner` asks a *question* through the
        same handshake that gates `discard_task`, so what distinguishes them is the
        payload, not the mechanism — and a payload that fails to parse falls back to the
        buttons rather than rendering nothing.
      */}
      {!readOnly && pending?.question ? (
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
      ) : !readOnly && pending ? (
        <ConfirmationPrompt
          pending={pending}
          projectId={projectId}
          sessionId={sessionId}
          messages={messages}
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

      {readOnly ? (
        streaming ? (
          <div className="flex justify-end border-t p-3">
            <Button
              type="button"
              variant="outline"
              onClick={() => {
                if (live) cancelTurn.mutate(live.turnId);
              }}
            >
              Cancel
            </Button>
          </div>
        ) : null
      ) : sessionId ? (
        <>
          {blockedSend ? (
            <div
              className="flex flex-wrap items-center justify-between gap-2 border-t bg-destructive/10 px-3 py-2 text-sm text-destructive"
              role="alert"
            >
              <p>{blockedSend.detail}</p>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={startTurn.isPending}
                onClick={() => sendTurn(blockedSend.body)}
              >
                Retry
              </Button>
            </div>
          ) : null}
          <Composer
            sessionId={sessionId}
            sending={startTurn.isPending}
            streaming={Boolean(streaming)}
            onSend={(text, attachments) => {
              // `attachments` carries `filename` for the optimistic echo; `api.startTurn`
              // strips it, because `TurnAttachment` forbids unknown fields.
              sendTurn({ text, attachments });
              resetComposer(sessionId);
            }}
            onCancel={() => {
              if (live) cancelTurn.mutate(live.turnId);
            }}
          />
        </>
      ) : null}
    </section>
  );
}
