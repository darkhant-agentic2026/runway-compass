/**
 * The streaming reducer.
 *
 * docs/08-testing.md:
 *
 * > **Streaming reducer** — unit tests over the frame sequence: ordered deltas,
 * > out-of-order arrival, duplicates after resume, `turn_error`, and the Zustand→Query
 * > handoff on `turn_complete` (assert the transcript query is updated exactly once and
 * > the buffer is cleared).
 *
 * The dedupe rule is the one worth stating outright: **deltas with `seq <= lastSeq` are
 * dropped**. Replay after a reconnect overlaps on purpose, so exactly-once rendering is a
 * property of the sequence number rather than of the server and client agreeing on where
 * the boundary was.
 *
 * Most tests here use one author throughout ('coach', matching an ordinary chat turn),
 * so a turn accumulates exactly one segment and `text()`/`tools()` read as a flat buffer
 * would have. The "segments" block below is what actually pins the one-segment-per-author
 * behavior a research/roadmap turn depends on.
 */

import { QueryClient } from '@tanstack/react-query';
import { beforeEach, describe, expect, it } from 'vitest';

import { parseServerFrame } from '@/lib/frames';
import { newestTurnFor, useStreamStore } from '@/stores/stream';

const TURN = 't_1';
const SESSION = 's_1';
const AUTHOR = 'coach';

function reset(): void {
  useStreamStore.setState({ turns: {} });
}

function store() {
  return useStreamStore.getState();
}

/** The turn's state, asserted present — every caller here has just written to it. */
function turn(turnId = TURN) {
  const state = useStreamStore.getState().turns[turnId];
  if (!state) throw new Error(`no stream state for ${turnId}`);
  return state;
}

/** Every segment's text, concatenated — a single-author turn has exactly one segment. */
function text(turnId = TURN): string {
  return turn(turnId)
    .segments.map((segment) => segment.text)
    .join('');
}

/** Every segment's tool chips, in order. */
function tools(turnId = TURN) {
  return turn(turnId).segments.flatMap((segment) => segment.tools);
}

beforeEach(reset);

describe('ordered deltas', () => {
  it('accumulates text in sequence order', () => {
    store().begin(TURN, SESSION);
    store().appendDelta(TURN, 1, 'Hello', AUTHOR);
    store().appendDelta(TURN, 2, ', ', AUTHOR);
    store().appendDelta(TURN, 3, 'world.', AUTHOR);

    expect(text()).toBe('Hello, world.');
    expect(turn().lastSeq).toBe(3);
  });

  it('starts a turn implicitly if a delta arrives before turn_start', () => {
    // Ordering on the wire is not guaranteed to put `turn_start` first once a resume is
    // in play — a resumed turn gets checkpoints and no fresh start frame.
    store().appendDelta(TURN, 5, 'mid-stream', AUTHOR);

    expect(text()).toBe('mid-stream');
    expect(turn().status).toBe('running');
  });
});

describe('duplicates and out-of-order arrival', () => {
  it('drops a delta at or below lastSeq', () => {
    store().appendDelta(TURN, 1, 'one ', AUTHOR);
    store().appendDelta(TURN, 2, 'two ', AUTHOR);

    store().appendDelta(TURN, 2, 'two ', AUTHOR);
    store().appendDelta(TURN, 1, 'one ', AUTHOR);

    expect(text()).toBe('one two ');
  });

  it('drops the overlap a resume replays', () => {
    store().appendDelta(TURN, 1, 'a', AUTHOR);
    store().appendDelta(TURN, 2, 'b', AUTHOR);
    store().appendDelta(TURN, 3, 'c', AUTHOR);

    // The reconnect replays from an earlier cursor than this tab reached.
    for (const [seq, delta] of [
      [2, 'b'],
      [3, 'c'],
      [4, 'd'],
    ] as const) {
      store().appendDelta(TURN, seq, delta, AUTHOR);
    }

    expect(text()).toBe('abcd');
  });

  it('ignores an out-of-order late arrival rather than interleaving it', () => {
    store().appendDelta(TURN, 1, 'first ', AUTHOR);
    store().appendDelta(TURN, 3, 'third ', AUTHOR);
    store().appendDelta(TURN, 2, 'second ', AUTHOR);

    expect(text()).toBe('first third ');
  });
});

describe('tool chips', () => {
  it('opens a chip on tool_call and closes it on tool_result', () => {
    store().noteToolCall(TURN, 1, 'google_search', AUTHOR);
    expect(tools()[0]).toMatchObject({ name: 'google_search', done: false });

    store().noteToolResult(TURN, 2, 'google_search', true);
    expect(tools()[0]).toMatchObject({ done: true, ok: true });
  });

  it('closes the most recent open chip with that name', () => {
    store().noteToolCall(TURN, 1, 'fetch_url', AUTHOR);
    store().noteToolCall(TURN, 2, 'fetch_url', AUTHOR);
    store().noteToolResult(TURN, 3, 'fetch_url', false);

    const [first, second] = tools();
    expect(first?.done).toBe(false);
    expect(second).toMatchObject({ done: true, ok: false });
  });

  it('marks every chip done when the turn completes', () => {
    store().noteToolCall(TURN, 1, 'fetch_url', AUTHOR);
    store().complete(TURN, 2);

    expect(tools().every((chip) => chip.done)).toBe(true);
  });
});

describe('segments', () => {
  /**
   * The bug this pins: a `research_workflow`/`build_roadmap_workflow` turn is several
   * named agents writing into *one* turn (docs/03-agent-design.md), and before segments
   * existed, every delta for a turn landed in one flat `text` string regardless of which
   * node produced it — `research_planner` finishing and `topic_researcher` starting
   * looked like one bubble quietly growing rather than two separate messages. A new
   * segment has to start the moment the author changes, and *only* then: an ordinary
   * chat turn's single author must still produce exactly one segment.
   */
  it('starts a new segment when the author changes', () => {
    store().appendDelta(TURN, 1, 'planning the topics', 'research_planner');
    store().appendDelta(TURN, 2, 'here is the first finding', 'topic_researcher');

    expect(turn().segments.map((segment) => segment.author)).toEqual([
      'research_planner',
      'topic_researcher',
    ]);
    expect(turn().segments.map((segment) => segment.text)).toEqual([
      'planning the topics',
      'here is the first finding',
    ]);
  });

  it('keeps appending to the same segment while the author is unchanged', () => {
    store().appendDelta(TURN, 1, 'one', 'research_planner');
    store().appendDelta(TURN, 2, ' two', 'research_planner');
    store().appendDelta(TURN, 3, ' three', 'topic_researcher');
    store().appendDelta(TURN, 4, ' four', 'topic_researcher');

    expect(turn().segments).toHaveLength(2);
    expect(turn().segments[0]?.text).toBe('one two');
    expect(turn().segments[1]?.text).toBe(' three four');
  });

  it('a tool call from a new author also opens a new segment', () => {
    store().appendDelta(TURN, 1, 'deciding what to search', 'research_planner');
    store().noteToolCall(TURN, 2, 'google_search', 'topic_researcher', { q: 'x' });

    expect(turn().segments).toHaveLength(2);
    expect(turn().segments[1]).toMatchObject({ author: 'topic_researcher', text: '' });
    expect(turn().segments[1]?.tools).toHaveLength(1);
  });

  it('closes a tool call from an earlier segment even after a new author has started', () => {
    store().noteToolCall(TURN, 1, 'fetch_url', 'topic_researcher');
    store().appendDelta(TURN, 2, 'closing summary', 'reviewer_writer');
    store().noteToolResult(TURN, 3, 'fetch_url', true);

    expect(turn().segments[0]?.tools[0]).toMatchObject({ done: true, ok: true });
    expect(turn().segments[1]?.text).toBe('closing summary');
  });

  it("marks every segment's chips done when the turn completes", () => {
    store().noteToolCall(TURN, 1, 'fetch_url', 'research_planner');
    store().appendDelta(TURN, 2, 'x', 'topic_researcher');
    store().noteToolCall(TURN, 3, 'google_search', 'topic_researcher');
    store().complete(TURN, 4);

    const chips = turn().segments.flatMap((segment) => segment.tools);
    expect(chips.every((chip) => chip.done)).toBe(true);
  });
});

describe('terminal frames', () => {
  it('turn_complete sets the status without discarding the text', () => {
    store().appendDelta(TURN, 1, 'done thinking', AUTHOR);
    store().complete(TURN, 2);

    expect(turn().status).toBe('complete');
    expect(text()).toBe('done thinking');
  });

  it('turn_error records the code, message, and retryability', () => {
    store().appendDelta(TURN, 1, 'partial', AUTHOR);
    store().fail(TURN, 2, { code: 'RuntimeError', message: 'boom', retryable: true });

    expect(turn()).toMatchObject({
      status: 'error',
      error: { code: 'RuntimeError', message: 'boom', retryable: true },
    });
  });

  it('a cancelled turn is an error that is explicitly not retryable', () => {
    store().fail(TURN, 1, {
      code: 'cancelled',
      message: 'This turn was cancelled.',
      retryable: false,
    });

    expect(turn().error?.retryable).toBe(false);
  });
});

describe('the Zustand → Query handoff', () => {
  it('updates the transcript query once and clears the buffer', async () => {
    const queryClient = new QueryClient();
    const key = ['session', SESSION, 'events'];
    queryClient.setQueryData(key, []);
    let writes = 0;
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      if (event.type === 'updated' && event.query.queryKey[1] === SESSION) writes += 1;
    });

    store().begin(TURN, SESSION);
    store().appendDelta(TURN, 1, 'the answer', AUTHOR);
    store().complete(TURN, 2);

    // The page does exactly this on `turn_complete`: refetch first, then drop the buffer.
    queryClient.setQueryData(key, [
      { seq: 1, eventId: 'e_1', event: { author: 'coach', content: { parts: [] } } },
    ]);
    store().clear(TURN);

    expect(writes).toBe(1);
    expect(store().turns[TURN]).toBeUndefined();
    unsubscribe();
  });

  it('clearing one turn leaves another alone', () => {
    store().appendDelta('t_a', 1, 'a', AUTHOR);
    store().appendDelta('t_b', 1, 'b', AUTHOR);

    store().clear('t_a');

    expect(store().turns['t_a']).toBeUndefined();
    expect(text('t_b')).toBe('b');
  });
});

describe('the resume worklist', () => {
  it('lists every running turn with its cursor', () => {
    store().appendDelta('t_a', 4, 'still going', AUTHOR);
    store().appendDelta('t_b', 2, 'also going', AUTHOR);
    store().appendDelta('t_c', 9, 'finished', AUTHOR);
    store().complete('t_c', 10);

    expect(store().running()).toEqual([
      { turnId: 't_a', lastSeq: 4 },
      { turnId: 't_b', lastSeq: 2 },
    ]);
  });

  it('excludes a failed turn, so a reconnect does not resume a dead stream', () => {
    store().appendDelta('t_a', 1, 'x', AUTHOR);
    store().fail('t_a', 2, { code: 'x', message: 'y', retryable: true });

    expect(store().running()).toEqual([]);
  });
});

describe('frame parsing at the boundary', () => {
  it('accepts a well-formed frame', () => {
    expect(parseServerFrame({ type: 'delta', turnId: TURN, seq: 1, text: 'hi' })).toMatchObject(
      {
        type: 'delta',
        text: 'hi',
      },
    );
  });

  it('defaults a missing author to the empty string, forward-compatibly', () => {
    const frame = parseServerFrame({ type: 'delta', turnId: TURN, seq: 1, text: 'hi' });
    expect(frame).toMatchObject({ author: '' });
  });

  it('parses a JSON string, which is what arrives on the wire', () => {
    const raw = JSON.stringify({ type: 'turn_complete', turnId: TURN, seq: 4, eventIds: [] });

    expect(parseServerFrame(raw)).toMatchObject({ type: 'turn_complete', seq: 4 });
  });

  it('ignores an unknown type forward-compatibly', () => {
    // A server that learns to send `run_status` variants (M5) or new artifact kinds must
    // not break a tab that is already open.
    expect(parseServerFrame({ type: 'something_new', payload: 1 })).toBeNull();
  });

  it('ignores a malformed frame of a known type', () => {
    expect(parseServerFrame({ type: 'delta', turnId: TURN })).toBeNull();
    expect(parseServerFrame('not json at all')).toBeNull();
  });
});

describe('a failed turn does not shadow the ones after it', () => {
  /**
   * The defect a 429 from Vertex found on `coach-dev`.
   *
   * `clear` is called from the `turn_complete` handoff and nowhere else, so a turn that
   * ends in `turn_error` stays in the store for the life of the tab. The reader used
   * `Object.values(turns).find(…)`, which returns the *first-inserted* match — so the
   * failed turn sat in front of every later turn in that session: the red error stayed on
   * screen, the next reply streamed into a buffer nothing rendered, and only a reload
   * (which empties the store and refetches the transcript) showed the answer that had
   * genuinely been generated.
   *
   * Both halves are asserted, because either one alone still leaves a broken screen.
   */
  const SESSION = 's_1';

  beforeEach(() => {
    useStreamStore.setState({ turns: {} });
  });

  it('a turn that fails is still readable, so the error can be shown', () => {
    const store = useStreamStore.getState();
    store.begin('t_1', SESSION);
    store.fail('t_1', 3, { code: 'resource-exhausted', message: '429', retryable: true });

    const live = newestTurnFor(useStreamStore.getState().turns, SESSION);
    expect(live?.turnId).toBe('t_1');
    expect(live?.status).toBe('error');
    expect(live?.error?.message).toBe('429');
  });

  it('the next turn becomes the live one, error or no error', () => {
    const store = useStreamStore.getState();
    store.begin('t_1', SESSION);
    store.fail('t_1', 3, { code: 'resource-exhausted', message: '429', retryable: true });

    store.begin('t_2', SESSION);
    useStreamStore.getState().appendDelta('t_2', 1, 'a new answer', AUTHOR);

    const live = newestTurnFor(useStreamStore.getState().turns, SESSION);
    expect(live?.turnId).toBe('t_2');
    expect(live?.segments[0]?.text).toBe('a new answer');
    expect(live?.status).toBe('running');
  });

  it('starting a turn retires the failed one, so errors do not accumulate', () => {
    const store = useStreamStore.getState();
    store.begin('t_1', SESSION);
    store.fail('t_1', 1, { code: 'x', message: 'first', retryable: true });
    store.begin('t_2', SESSION);
    useStreamStore.getState().fail('t_2', 1, { code: 'x', message: 'second', retryable: true });
    useStreamStore.getState().begin('t_3', SESSION);

    expect(Object.keys(useStreamStore.getState().turns)).toEqual(['t_3']);
  });

  it('a completed turn is left for the transcript handoff to clear', () => {
    // Dropping it here instead would race `events.refetch()` and blink the coach's last
    // message off the screen between the buffer going and the transcript arriving.
    const store = useStreamStore.getState();
    store.begin('t_1', SESSION);
    store.appendDelta('t_1', 1, 'settled text', AUTHOR);
    store.complete('t_1', 2);
    store.begin('t_2', SESSION);

    expect(Object.keys(useStreamStore.getState().turns)).toContain('t_1');
    expect(newestTurnFor(useStreamStore.getState().turns, SESSION)?.turnId).toBe('t_2');
  });

  it('a failed turn in another session is left alone', () => {
    const store = useStreamStore.getState();
    store.begin('t_other', 's_2');
    store.fail('t_other', 1, { code: 'x', message: 'theirs', retryable: false });
    store.begin('t_1', SESSION);

    expect(newestTurnFor(useStreamStore.getState().turns, 's_2')?.status).toBe('error');
    expect(newestTurnFor(useStreamStore.getState().turns, SESSION)?.turnId).toBe('t_1');
  });

  it('insertion order does not decide which turn is live', () => {
    // The mechanism behind the original bug, pinned directly: `find` returns the first
    // match, and object key order is insertion order. `openedAt` is what makes this a
    // question about *when a turn opened* rather than about how a `Record` is laid out.
    const store = useStreamStore.getState();
    store.begin('t_first', SESSION);
    store.begin('t_second', SESSION);

    const [firstKey] = Object.keys(useStreamStore.getState().turns);
    expect(firstKey).toBe('t_first');
    expect(newestTurnFor(useStreamStore.getState().turns, SESSION)?.turnId).toBe('t_second');
  });
});
