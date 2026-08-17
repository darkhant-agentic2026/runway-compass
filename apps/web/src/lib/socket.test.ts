/**
 * Reconnect, resume, and the presence heartbeat.
 *
 * docs/08-testing.md:
 *
 * > **Reconnect logic** — backoff schedule, ticket refresh, resume frames emitted for
 * > every running turn, presence heartbeat start/stop on visibility change.
 *
 * Everything is driven through injected `WebSocket`, timer, clock, and randomness, so
 * these assertions are about the schedule rather than about how long the test waits.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  CoachSocket,
  HIDDEN_GRACE_MS,
  MAX_BACKOFF_MS,
  PRESENCE_INTERVAL_MS,
  backoffDelay,
} from '@/lib/socket'
import { useSocketStore } from '@/stores/socket'
import { useStreamStore } from '@/stores/stream'

/** A `WebSocket` stand-in that records what was sent and can be closed on demand. */
class FakeSocket {
  static instances: FakeSocket[] = []
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  readonly url: string

  // A plain assignment rather than a parameter property: `erasableSyntaxOnly` is on, and
  // `constructor(readonly url: string)` is the one piece of TS that emits runtime code.
  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }

  open(): void {
    this.readyState = 1
    this.onopen?.()
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent)
  }

  drop(): void {
    this.readyState = 3
    this.onclose?.()
  }

  close(): void {
    this.readyState = 3
  }

  frames(): unknown[] {
    return this.sent.map((raw) => JSON.parse(raw))
  }

  send(raw: string): void {
    this.sent.push(raw)
  }
}

interface Scheduled {
  handler: () => void
  ms: number
  handle: number
}

function harness(options: { tickets?: string[] } = {}) {
  FakeSocket.instances = []
  const scheduled: Scheduled[] = []
  let nextHandle = 1
  let clock = 0
  const tickets = options.tickets ?? []
  let issued = 0

  const socket = new CoachSocket({
    createWebSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
    fetchTicket: async () => {
      issued += 1
      return tickets[issued - 1] ?? `ticket-${issued}`
    },
    now: () => clock,
    setTimeout: (handler, ms) => {
      const handle = nextHandle++
      scheduled.push({ handler, ms, handle })
      return handle
    },
    clearTimeout: (handle) => {
      const index = scheduled.findIndex((entry) => entry.handle === handle)
      if (index >= 0) scheduled.splice(index, 1)
    },
    // Full jitter draws from `[0, ceiling)`; pinning the draw to its maximum makes the
    // schedule assertable without making the production code less random.
    random: () => 0.999_999,
  })

  return {
    socket,
    scheduled,
    ticketsIssued: () => issued,
    advance(ms: number) {
      clock += ms
    },
    runNext() {
      const next = scheduled.shift()
      next?.handler()
      return next
    },
    last: () => FakeSocket.instances.at(-1)!,
  }
}

beforeEach(() => {
  useStreamStore.setState({ turns: {} })
  useSocketStore.setState({ connection: 'idle', attempts: 0, presence: null })
  vi.stubGlobal('location', { protocol: 'https:', host: 'coach.example' })
})

describe('backoff', () => {
  it('grows exponentially and caps at 30 s', () => {
    const ceilings = [0, 1, 2, 3, 4, 5, 6, 7, 20].map((attempt) =>
      backoffDelay(attempt, () => 0.999_999),
    )

    expect(ceilings[0]).toBeLessThan(500)
    expect(ceilings[1]).toBeLessThan(1000)
    expect(ceilings[3]).toBeLessThan(4000)
    expect(ceilings.at(-1)).toBeLessThan(MAX_BACKOFF_MS)
    expect(ceilings.at(-1)).toBeGreaterThan(MAX_BACKOFF_MS * 0.9)
  })

  it('draws from the whole interval, not a band around the mean', () => {
    // Full jitter is what pulls simultaneous reconnects apart. A schedule that only
    // varied slightly around a growing mean would leave every tab in a browser
    // reconnecting together.
    expect(backoffDelay(5, () => 0)).toBe(0)
    expect(backoffDelay(5, () => 0.999_999)).toBeGreaterThan(0)
  })
})

describe('connect', () => {
  it('fetches a ticket and connects with it in the query string', async () => {
    const rig = harness({ tickets: ['abc'] })

    await rig.socket.connect()

    expect(rig.last().url).toBe('wss://coach.example/ws?ticket=abc')
  })

  it('reports open once the socket opens', async () => {
    const rig = harness()
    await rig.socket.connect()

    rig.last().open()

    expect(useSocketStore.getState().connection).toBe('open')
  })

  it('takes a fresh ticket for every attempt', async () => {
    // Tickets are single-use and 60-second, so reusing one would fail every reconnect
    // after the first (docs/04-api-contract.md#authentication).
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()

    rig.last().drop()
    rig.runNext()
    await vi.waitFor(() => expect(rig.ticketsIssued()).toBe(2))

    expect(rig.last().url).toContain('ticket-2')
  })

  it('schedules a reconnect when the ticket request itself fails', async () => {
    const rig = harness()
    const failing = new CoachSocket({
      createWebSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      fetchTicket: async () => {
        throw new Error('offline')
      },
      setTimeout: (handler, ms) => {
        rig.scheduled.push({ handler, ms, handle: rig.scheduled.length + 1 })
        return rig.scheduled.length
      },
      clearTimeout: () => {},
      random: () => 0.5,
    })

    await failing.connect()

    expect(rig.scheduled).toHaveLength(1)
    expect(useSocketStore.getState().connection).toBe('reconnecting')
  })
})

describe('resume', () => {
  it('emits a resume frame for every running turn, with its cursor', async () => {
    useStreamStore.getState().appendDelta('t_a', 7, 'seven')
    useStreamStore.getState().appendDelta('t_b', 2, 'two')

    const rig = harness()
    await rig.socket.connect()
    rig.last().open()

    expect(rig.last().frames()).toEqual([
      { type: 'resume', turnId: 't_a', lastSeq: 7 },
      { type: 'resume', turnId: 't_b', lastSeq: 2 },
    ])
  })

  it('does not resume a turn that already finished', async () => {
    useStreamStore.getState().appendDelta('t_done', 3, 'x')
    useStreamStore.getState().complete('t_done', 4)

    const rig = harness()
    await rig.socket.connect()
    rig.last().open()

    expect(rig.last().frames()).toEqual([])
  })

  it('resumes at the cursor the tab actually reached, after a drop mid-stream', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()
    rig.last().receive({ type: 'turn_start', turnId: 't_1', sessionId: 's_1' })
    rig.last().receive({ type: 'delta', turnId: 't_1', seq: 1, text: 'Hel' })
    rig.last().receive({ type: 'delta', turnId: 't_1', seq: 2, text: 'lo' })

    rig.last().drop()
    rig.runNext()
    await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(2))
    rig.last().open()

    expect(rig.last().frames()).toEqual([{ type: 'resume', turnId: 't_1', lastSeq: 2 }])
  })

  it('renders the replayed remainder exactly once', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()
    rig.last().receive({ type: 'delta', turnId: 't_1', seq: 1, text: 'Hel' })
    rig.last().receive({ type: 'delta', turnId: 't_1', seq: 2, text: 'lo' })

    rig.last().drop()
    rig.runNext()
    await vi.waitFor(() => expect(FakeSocket.instances).toHaveLength(2))
    rig.last().open()
    // The server replays from an earlier point than this tab reached; the overlap is
    // dropped by seq.
    rig.last().receive({ type: 'delta', turnId: 't_1', seq: 2, text: 'lo' })
    rig.last().receive({ type: 'delta', turnId: 't_1', seq: 3, text: ' world' })
    rig.last().receive({ type: 'turn_complete', turnId: 't_1', seq: 4, eventIds: [] })

    expect(useStreamStore.getState().turns['t_1']).toMatchObject({
      text: 'Hello world',
      status: 'complete',
    })
  })
})

describe('board updates', () => {
  it('hands board_update to the invalidation callback', async () => {
    const onBoardUpdate = vi.fn()
    FakeSocket.instances = []
    const socket = new CoachSocket({
      createWebSocket: (url) => new FakeSocket(url) as unknown as WebSocket,
      fetchTicket: async () => 't',
      setTimeout: () => 0,
      clearTimeout: () => {},
      onBoardUpdate,
    })
    await socket.connect()
    const fake = FakeSocket.instances.at(-1)!
    fake.open()

    fake.receive({ type: 'board_update', projectId: 'p_1', taskIds: ['k_1'], origin: 'agent' })

    expect(onBoardUpdate).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 'p_1', taskIds: ['k_1'] }),
    )
  })

  it('ignores a frame type it does not know', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()

    expect(() =>
      rig.last().receive({ type: 'not_a_real_frame', turnId: 't_1' }),
    ).not.toThrow()
  })
})

describe('presence', () => {
  it('beats immediately and then on the interval', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()

    rig.socket.setPresenceTarget({ projectId: 'p_1', taskId: 'k_1' })

    expect(rig.last().frames()).toEqual([
      { type: 'presence', projectId: 'p_1', taskId: 'k_1' },
    ])
    expect(rig.scheduled.at(-1)?.ms).toBe(PRESENCE_INTERVAL_MS)
  })

  it('stops when the target is released', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()
    rig.socket.setPresenceTarget({ projectId: 'p_1', taskId: 'k_1' })

    rig.socket.setPresenceTarget(null)
    rig.runNext()

    expect(rig.last().frames()).toHaveLength(1)
    expect(useSocketStore.getState().presence).toBeNull()
  })

  it('keeps beating while briefly hidden', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()
    rig.socket.setPresenceTarget({ projectId: 'p_1', taskId: 'k_1' })

    rig.socket.setVisibility(false)
    rig.advance(30_000)
    rig.runNext()

    // A tab switch or a notification must not cost the user their claim on the project.
    expect(rig.last().frames()).toHaveLength(2)
  })

  it('stops beating after two minutes hidden', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()
    rig.socket.setPresenceTarget({ projectId: 'p_1', taskId: 'k_1' })

    rig.socket.setVisibility(false)
    rig.advance(HIDDEN_GRACE_MS)
    rig.runNext()

    // "so a forgotten background tab doesn't block the autonomous agent forever"
    // (docs/06-frontend.md).
    expect(rig.last().frames()).toHaveLength(1)
  })

  it('resumes beating when the tab comes back', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()
    rig.socket.setPresenceTarget({ projectId: 'p_1', taskId: 'k_1' })
    rig.socket.setVisibility(false)
    rig.advance(HIDDEN_GRACE_MS)
    rig.runNext()

    rig.socket.setVisibility(true)

    expect(rig.last().frames()).toHaveLength(2)
  })
})

describe('close', () => {
  it('does not reconnect after an intentional close', async () => {
    const rig = harness()
    await rig.socket.connect()
    rig.last().open()

    rig.socket.close()
    rig.last().drop()

    expect(rig.scheduled).toHaveLength(0)
    expect(useSocketStore.getState().connection).toBe('idle')
  })
})
