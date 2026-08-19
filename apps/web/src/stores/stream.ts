/**
 * `useStreamStore` — the hot path.
 *
 * docs/06-frontend.md#zustand-client-only-state: "Per-`turnId`: accumulated text,
 * `lastSeq`, tool-call chips, status."
 *
 * **Why streaming tokens must not go through the Query cache** (same document):
 *
 * > a delta arrives every few tens of milliseconds; writing each into
 * > `queryClient.setQueryData` invalidates observers and re-renders every consumer of
 * > that key. The stream buffer lives in Zustand with a selector subscribed only by the
 * > message bubble component. On `turn_complete`, the buffer is flushed once into the
 * > Query cache and cleared. One handoff, one re-render of the transcript.
 *
 * The reducer's one non-obvious rule: **deltas with `seq <= lastSeq` are dropped**. Replay
 * after a reconnect deliberately overlaps, so exactly-once rendering is guaranteed by
 * sequence number rather than by the server and client agreeing on a boundary. Ordering
 * is by the same number, so an out-of-order arrival cannot interleave text.
 */

import { create } from 'zustand';

export type StreamStatus = 'idle' | 'running' | 'complete' | 'error';

export interface ToolChip {
  seq: number;
  name: string;
  /** `false` until the matching `tool_result` arrives; drives the spinner on the chip. */
  done: boolean;
  ok: boolean;
}

export interface StreamState {
  turnId: string;
  sessionId: string | null;
  text: string;
  lastSeq: number;
  status: StreamStatus;
  tools: ToolChip[];
  error: { code: string; message: string; retryable: boolean } | null;
  /**
   * Monotonic, assigned when the buffer is created. It exists so "the turn this session is
   * showing" can be answered with *newest*, rather than with whichever entry a `find`
   * happened to reach first.
   *
   * A counter rather than `Date.now()`: two turns can open inside one millisecond, and a
   * tie here is exactly the bug this field was added to remove.
   */
  openedAt: number;
}

let opened = 0;

function blank(turnId: string, sessionId: string | null = null): StreamState {
  opened += 1;
  return {
    turnId,
    sessionId,
    text: '',
    lastSeq: 0,
    status: 'running',
    tools: [],
    error: null,
    openedAt: opened,
  };
}

/**
 * The turn a session's UI should be showing: the most recently opened one, or `null`.
 *
 * **Not `Object.values(turns).find(…)`.** That returns the *first-inserted* match, and a
 * turn only leaves the store when its text is handed to the transcript — which happens on
 * `turn_complete` and nowhere else. So a turn that ended in `turn_error` stayed forever,
 * and every later turn in that session was rendered *behind* it: the red error remained on
 * screen, the new reply streamed into a buffer nothing read, and only a reload — which
 * empties the store and refetches the transcript — showed the answer that had in fact been
 * generated. A 429 from Vertex was how this was found.
 *
 * Returning a stored object rather than a computed one matters: a Zustand selector is
 * compared with `Object.is`, so a fresh literal here would re-render forever
 * (CLAUDE.md, and `DEFAULT_FILTERS` in `stores/boardUi.ts`).
 */
export function newestTurnFor(
  turns: Record<string, StreamState>,
  sessionId: string,
): StreamState | null {
  let newest: StreamState | null = null;
  for (const turn of Object.values(turns)) {
    if (turn.sessionId !== sessionId) continue;
    if (newest === null || turn.openedAt > newest.openedAt) newest = turn;
  }
  return newest;
}

interface StreamStore {
  turns: Record<string, StreamState>;

  /** Register a turn started by `POST /turns`, before any frame has arrived. */
  begin: (turnId: string, sessionId: string) => void;
  appendDelta: (turnId: string, seq: number, text: string) => void;
  noteToolCall: (turnId: string, seq: number, name: string) => void;
  noteToolResult: (turnId: string, seq: number, name: string, ok: boolean) => void;
  complete: (turnId: string, seq: number) => void;
  fail: (
    turnId: string,
    seq: number,
    error: { code: string; message: string; retryable: boolean },
  ) => void;
  /** Drop a turn's buffer after its text has been handed to the Query cache. */
  clear: (turnId: string) => void;
  /** Every turn still running, with its cursor — the reconnect's resume worklist. */
  running: () => { turnId: string; lastSeq: number }[];
}

export const useStreamStore = create<StreamStore>((set, get) => ({
  turns: {},

  begin(turnId, sessionId) {
    set((state) => {
      if (state.turns[turnId]) return state;
      // Starting a turn retires the *failed* one before it. Nothing else ever removes a
      // failed turn — `clear` is called from the `turn_complete` handoff and only there —
      // so without this the store accumulates one entry per error, for the life of the tab.
      //
      // Only `error`. A `complete` turn is dropped by that handoff, *after* its text has
      // been refetched into the transcript; dropping it here instead would race the
      // refetch and blink the coach's last message off the screen.
      const kept = Object.entries(state.turns).filter(
        ([, turn]) => !(turn.sessionId === sessionId && turn.status === 'error'),
      );
      return {
        turns: {
          ...Object.fromEntries(kept),
          [turnId]: blank(turnId, sessionId),
        },
      };
    });
  },

  appendDelta(turnId, seq, text) {
    set((state) => {
      const current = state.turns[turnId] ?? blank(turnId);
      // The dedupe. Replay after a reconnect overlaps on purpose.
      if (seq <= current.lastSeq) return state;
      return {
        turns: {
          ...state.turns,
          [turnId]: {
            ...current,
            text: current.text + text,
            lastSeq: seq,
            status: 'running',
          },
        },
      };
    });
  },

  noteToolCall(turnId, seq, name) {
    set((state) => {
      const current = state.turns[turnId] ?? blank(turnId);
      if (seq <= current.lastSeq) return state;
      return {
        turns: {
          ...state.turns,
          [turnId]: {
            ...current,
            lastSeq: seq,
            tools: [...current.tools, { seq, name, done: false, ok: true }],
          },
        },
      };
    });
  },

  noteToolResult(turnId, seq, name, ok) {
    set((state) => {
      const current = state.turns[turnId] ?? blank(turnId);
      if (seq <= current.lastSeq) return state;
      // Close the most recent open chip with this name. Matching by name rather than by
      // an id because the contract's `tool_result` carries no call id — and the model
      // cannot have two calls to the same tool outstanding within one turn.
      let closed = false;
      const tools = [...current.tools].reverse().map((chip) => {
        if (closed || chip.done || chip.name !== name) return chip;
        closed = true;
        return { ...chip, done: true, ok };
      });
      return {
        turns: {
          ...state.turns,
          [turnId]: { ...current, lastSeq: seq, tools: tools.reverse() },
        },
      };
    });
  },

  complete(turnId, seq) {
    set((state) => {
      const current = state.turns[turnId] ?? blank(turnId);
      return {
        turns: {
          ...state.turns,
          [turnId]: {
            ...current,
            lastSeq: Math.max(current.lastSeq, seq),
            status: 'complete',
            tools: current.tools.map((chip) => ({ ...chip, done: true })),
          },
        },
      };
    });
  },

  fail(turnId, seq, error) {
    set((state) => {
      const current = state.turns[turnId] ?? blank(turnId);
      return {
        turns: {
          ...state.turns,
          [turnId]: {
            ...current,
            lastSeq: Math.max(current.lastSeq, seq),
            status: 'error',
            error,
          },
        },
      };
    });
  },

  clear(turnId) {
    set((state) => {
      const { [turnId]: _dropped, ...rest } = state.turns;
      return { turns: rest };
    });
  },

  running() {
    return Object.values(get().turns)
      .filter((turn) => turn.status === 'running')
      .map((turn) => ({ turnId: turn.turnId, lastSeq: turn.lastSeq }));
  },
}));
