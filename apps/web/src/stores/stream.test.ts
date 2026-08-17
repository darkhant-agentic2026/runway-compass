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
 */

import { QueryClient } from '@tanstack/react-query'
import { beforeEach, describe, expect, it } from 'vitest'

import { parseServerFrame } from '@/lib/frames'
import { useStreamStore } from '@/stores/stream'

const TURN = 't_1'
const SESSION = 's_1'

function reset(): void {
  useStreamStore.setState({ turns: {} })
}

function store() {
  return useStreamStore.getState()
}

/** The turn's state, asserted present — every caller here has just written to it. */
function turn(turnId = TURN) {
  const state = useStreamStore.getState().turns[turnId]
  if (!state) throw new Error(`no stream state for ${turnId}`)
  return state
}

beforeEach(reset)

describe('ordered deltas', () => {
  it('accumulates text in sequence order', () => {
    store().begin(TURN, SESSION)
    store().appendDelta(TURN, 1, 'Hello')
    store().appendDelta(TURN, 2, ', ')
    store().appendDelta(TURN, 3, 'world.')

    expect(turn().text).toBe('Hello, world.')
    expect(turn().lastSeq).toBe(3)
  })

  it('starts a turn implicitly if a delta arrives before turn_start', () => {
    // Ordering on the wire is not guaranteed to put `turn_start` first once a resume is
    // in play — a resumed turn gets checkpoints and no fresh start frame.
    store().appendDelta(TURN, 5, 'mid-stream')

    expect(turn().text).toBe('mid-stream')
    expect(turn().status).toBe('running')
  })
})

describe('duplicates and out-of-order arrival', () => {
  it('drops a delta at or below lastSeq', () => {
    store().appendDelta(TURN, 1, 'one ')
    store().appendDelta(TURN, 2, 'two ')

    store().appendDelta(TURN, 2, 'two ')
    store().appendDelta(TURN, 1, 'one ')

    expect(turn().text).toBe('one two ')
  })

  it('drops the overlap a resume replays', () => {
    store().appendDelta(TURN, 1, 'a')
    store().appendDelta(TURN, 2, 'b')
    store().appendDelta(TURN, 3, 'c')

    // The reconnect replays from an earlier cursor than this tab reached.
    for (const [seq, text] of [
      [2, 'b'],
      [3, 'c'],
      [4, 'd'],
    ] as const) {
      store().appendDelta(TURN, seq, text)
    }

    expect(turn().text).toBe('abcd')
  })

  it('ignores an out-of-order late arrival rather than interleaving it', () => {
    store().appendDelta(TURN, 1, 'first ')
    store().appendDelta(TURN, 3, 'third ')
    store().appendDelta(TURN, 2, 'second ')

    expect(turn().text).toBe('first third ')
  })
})

describe('tool chips', () => {
  it('opens a chip on tool_call and closes it on tool_result', () => {
    store().noteToolCall(TURN, 1, 'google_search')
    expect(turn().tools[0]).toMatchObject({ name: 'google_search', done: false })

    store().noteToolResult(TURN, 2, 'google_search', true)
    expect(turn().tools[0]).toMatchObject({ done: true, ok: true })
  })

  it('closes the most recent open chip with that name', () => {
    store().noteToolCall(TURN, 1, 'fetch_url')
    store().noteToolCall(TURN, 2, 'fetch_url')
    store().noteToolResult(TURN, 3, 'fetch_url', false)

    const [first, second] = turn().tools
    expect(first?.done).toBe(false)
    expect(second).toMatchObject({ done: true, ok: false })
  })

  it('marks every chip done when the turn completes', () => {
    store().noteToolCall(TURN, 1, 'fetch_url')
    store().complete(TURN, 2)

    expect(turn().tools.every((chip) => chip.done)).toBe(true)
  })
})

describe('terminal frames', () => {
  it('turn_complete sets the status without discarding the text', () => {
    store().appendDelta(TURN, 1, 'done thinking')
    store().complete(TURN, 2)

    expect(turn().status).toBe('complete')
    expect(turn().text).toBe('done thinking')
  })

  it('turn_error records the code, message, and retryability', () => {
    store().appendDelta(TURN, 1, 'partial')
    store().fail(TURN, 2, { code: 'RuntimeError', message: 'boom', retryable: true })

    expect(turn()).toMatchObject({
      status: 'error',
      error: { code: 'RuntimeError', message: 'boom', retryable: true },
    })
  })

  it('a cancelled turn is an error that is explicitly not retryable', () => {
    store().fail(TURN, 1, {
      code: 'cancelled',
      message: 'This turn was cancelled.',
      retryable: false,
    })

    expect(turn().error?.retryable).toBe(false)
  })
})

describe('the Zustand → Query handoff', () => {
  it('updates the transcript query once and clears the buffer', async () => {
    const queryClient = new QueryClient()
    const key = ['session', SESSION, 'events']
    queryClient.setQueryData(key, [])
    let writes = 0
    const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
      if (event.type === 'updated' && event.query.queryKey[1] === SESSION) writes += 1
    })

    store().begin(TURN, SESSION)
    store().appendDelta(TURN, 1, 'the answer')
    store().complete(TURN, 2)

    // The page does exactly this on `turn_complete`: refetch first, then drop the buffer.
    queryClient.setQueryData(key, [
      { seq: 1, eventId: 'e_1', event: { author: 'coach', content: { parts: [] } } },
    ])
    store().clear(TURN)

    expect(writes).toBe(1)
    expect(store().turns[TURN]).toBeUndefined()
    unsubscribe()
  })

  it('clearing one turn leaves another alone', () => {
    store().appendDelta('t_a', 1, 'a')
    store().appendDelta('t_b', 1, 'b')

    store().clear('t_a')

    expect(store().turns['t_a']).toBeUndefined()
    expect(turn('t_b').text).toBe('b')
  })
})

describe('the resume worklist', () => {
  it('lists every running turn with its cursor', () => {
    store().appendDelta('t_a', 4, 'still going')
    store().appendDelta('t_b', 2, 'also going')
    store().appendDelta('t_c', 9, 'finished')
    store().complete('t_c', 10)

    expect(store().running()).toEqual([
      { turnId: 't_a', lastSeq: 4 },
      { turnId: 't_b', lastSeq: 2 },
    ])
  })

  it('excludes a failed turn, so a reconnect does not resume a dead stream', () => {
    store().appendDelta('t_a', 1, 'x')
    store().fail('t_a', 2, { code: 'x', message: 'y', retryable: true })

    expect(store().running()).toEqual([])
  })
})

describe('frame parsing at the boundary', () => {
  it('accepts a well-formed frame', () => {
    expect(parseServerFrame({ type: 'delta', turnId: TURN, seq: 1, text: 'hi' })).toMatchObject({
      type: 'delta',
      text: 'hi',
    })
  })

  it('parses a JSON string, which is what arrives on the wire', () => {
    const raw = JSON.stringify({ type: 'turn_complete', turnId: TURN, seq: 4, eventIds: [] })

    expect(parseServerFrame(raw)).toMatchObject({ type: 'turn_complete', seq: 4 })
  })

  it('ignores an unknown type forward-compatibly', () => {
    // A server that learns to send `run_status` variants (M5) or new artifact kinds must
    // not break a tab that is already open.
    expect(parseServerFrame({ type: 'something_new', payload: 1 })).toBeNull()
  })

  it('ignores a malformed frame of a known type', () => {
    expect(parseServerFrame({ type: 'delta', turnId: TURN })).toBeNull()
    expect(parseServerFrame('not json at all')).toBeNull()
  })
})
