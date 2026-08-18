/**
 * The one module that owns the WebSocket.
 *
 * docs/06-frontend.md#websocket-client:
 *
 * - Connect on auth, with ticket fetch → connect → subscribe to any active turns.
 * - Reconnect with exponential backoff + jitter, capped at 30 s; **a fresh ticket per
 *   attempt** — tickets are single-use and expire in 60 s, so reusing one would fail every
 *   reconnect after the first.
 * - On reconnect, for every turn in `useStreamStore` with `status === 'running'`, send
 *   `{type:'resume', turnId, lastSeq}`. Deltas with `seq <= lastSeq` are dropped, so
 *   replay overlap is harmless and exactly-once rendering is guaranteed by sequence
 *   number, not by luck.
 * - Presence heartbeat every 30 s while a task workspace is focused; stopped on
 *   `visibilitychange` to hidden after 2 min, so a forgotten background tab doesn't block
 *   the autonomous agent forever.
 *
 * `WebSocket` and `setTimeout` are injected rather than reached for, so the reconnect
 * schedule and the resume frames can be asserted against a fake socket with no browser
 * and no real timers involved.
 *
 * **`board_update` is delivered to *listeners*, not to a constructor argument**, and that
 * distinction is load-bearing rather than stylistic. `getSocket` is a singleton: whoever
 * calls it first builds the socket and every later call gets that instance with its
 * arguments ignored. React runs child effects before parent ones, so a direct load of the
 * task workspace had `TaskWorkspacePage` create the socket — for the presence heartbeat,
 * with no arguments — before `AppShell`'s `useCoachSocket` could pass its invalidation
 * callback. The callback was then dropped for the lifetime of the tab, and the board
 * silently stopped refreshing when the coach changed it. A registration cannot be lost
 * that way, because it does not depend on who constructed the socket.
 */

import { api } from '@/lib/api'
import { parseServerFrame, type BoardUpdateFrame, type ClientFrame } from '@/lib/frames'
import { useSocketStore } from '@/stores/socket'
import { useStreamStore } from '@/stores/stream'

/** docs/06-frontend.md: capped at 30 s. */
export const MAX_BACKOFF_MS = 30_000
const BASE_BACKOFF_MS = 500

export const PRESENCE_INTERVAL_MS = 30_000
/** docs/06-frontend.md: stopped after 2 minutes hidden. */
export const HIDDEN_GRACE_MS = 120_000

/**
 * Full-jitter backoff, for the same reason the server uses it on contended transactions:
 * every tab in a browser reconnects at the moment the service comes back, and a narrow
 * jitter band around a growing mean leaves them nearly as synchronized as none at all.
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const ceiling = Math.min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** attempt)
  return Math.floor(random() * ceiling)
}

export interface SocketDeps {
  createWebSocket?: (url: string) => WebSocket
  fetchTicket?: () => Promise<string>
  now?: () => number
  setTimeout?: (handler: () => void, ms: number) => number
  clearTimeout?: (handle: number) => void
  random?: () => number
}

/** Called on `board_update`, so the socket module does not import the query client. */
export type BoardUpdateListener = (frame: BoardUpdateFrame) => void

export function socketUrl(ticket: string): string {
  // Same origin in every environment — the SPA ships inside the API image — so the URL is
  // derived from the page rather than configured (docs/01-architecture.md).
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`
}

export class CoachSocket {
  private socket: WebSocket | null = null
  private attempts = 0
  private closedByUs = false
  private reconnectHandle: number | null = null
  private presenceHandle: number | null = null
  private hiddenSince: number | null = null
  private target: { projectId: string | null; taskId: string | null } | null = null

  private readonly createWebSocket: (url: string) => WebSocket
  private readonly fetchTicket: () => Promise<string>
  private readonly now: () => number
  private readonly schedule: (handler: () => void, ms: number) => number
  private readonly cancel: (handle: number) => void
  private readonly random: () => number
  private readonly boardListeners = new Set<BoardUpdateListener>()

  constructor(deps: SocketDeps = {}) {
    this.createWebSocket = deps.createWebSocket ?? ((url) => new WebSocket(url))
    this.fetchTicket = deps.fetchTicket ?? (async () => (await api.createWsTicket()).ticket)
    this.now = deps.now ?? (() => Date.now())
    this.schedule = deps.setTimeout ?? ((handler, ms) => window.setTimeout(handler, ms))
    this.cancel = deps.clearTimeout ?? ((handle) => window.clearTimeout(handle))
    this.random = deps.random ?? Math.random
  }

  /**
   * Subscribe to `board_update`. Returns the unsubscribe.
   *
   * A set rather than a single slot, so two screens can listen without one silently
   * replacing the other — and so registering is idempotent under React's
   * mount/unmount/remount in strict mode.
   */
  onBoardUpdate(listener: BoardUpdateListener): () => void {
    this.boardListeners.add(listener)
    return () => {
      this.boardListeners.delete(listener)
    }
  }

  // --- lifecycle -------------------------------------------------------------------

  async connect(): Promise<void> {
    if (this.socket) return
    this.closedByUs = false
    const store = useSocketStore.getState()
    store.setConnection(this.attempts === 0 ? 'connecting' : 'reconnecting')

    let ticket: string
    try {
      // A fresh ticket per attempt, always: they are single-use and 60-second.
      ticket = await this.fetchTicket()
    } catch {
      this.scheduleReconnect()
      return
    }

    const socket = this.createWebSocket(socketUrl(ticket))
    this.socket = socket
    socket.onopen = () => this.handleOpen()
    socket.onmessage = (event: MessageEvent) => this.handleMessage(event.data)
    socket.onclose = () => this.handleClose()
    socket.onerror = () => {
      /* `close` always follows; handling both would double the backoff */
    }
  }

  close(): void {
    this.closedByUs = true
    this.stopPresence()
    if (this.reconnectHandle !== null) {
      this.cancel(this.reconnectHandle)
      this.reconnectHandle = null
    }
    this.socket?.close()
    this.socket = null
    useSocketStore.getState().setConnection('idle')
  }

  private handleOpen(): void {
    this.attempts = 0
    const store = useSocketStore.getState()
    store.setConnection('open')
    store.setAttempts(0)

    // The resume queue. Every turn this tab still believes is running gets a `resume`
    // carrying its cursor — including turns started before the socket ever dropped, which
    // is what makes a reload mid-generation pick the stream back up.
    for (const { turnId, lastSeq } of useStreamStore.getState().running()) {
      this.send({ type: 'resume', turnId, lastSeq })
    }
    if (this.target) this.startPresence()
  }

  private handleClose(): void {
    this.socket = null
    if (this.closedByUs) return
    this.scheduleReconnect()
  }

  private scheduleReconnect(): void {
    const store = useSocketStore.getState()
    store.setConnection('reconnecting')
    store.setAttempts(this.attempts + 1)
    const delay = backoffDelay(this.attempts, this.random)
    this.attempts += 1
    this.reconnectHandle = this.schedule(() => {
      this.reconnectHandle = null
      void this.connect()
    }, delay)
  }

  // --- frames ----------------------------------------------------------------------

  send(frame: ClientFrame): void {
    if (!this.socket || this.socket.readyState !== 1 /* OPEN */) return
    this.socket.send(JSON.stringify(frame))
  }

  subscribe(turnId: string): void {
    this.send({ type: 'subscribe', turnId })
  }

  unsubscribe(turnId: string): void {
    this.send({ type: 'unsubscribe', turnId })
  }

  private handleMessage(raw: unknown): void {
    const frame = parseServerFrame(raw)
    // Unknown or malformed frames are dropped: forward compatibility, per
    // docs/06-frontend.md.
    if (!frame) return

    const stream = useStreamStore.getState()
    switch (frame.type) {
      case 'turn_start':
        stream.begin(frame.turnId, frame.sessionId)
        break
      case 'delta':
        stream.appendDelta(frame.turnId, frame.seq, frame.text)
        break
      case 'tool_call':
        stream.noteToolCall(frame.turnId, frame.seq, frame.name)
        break
      case 'tool_result':
        stream.noteToolResult(frame.turnId, frame.seq, frame.name, frame.ok)
        break
      case 'turn_complete':
        stream.complete(frame.turnId, frame.seq)
        break
      case 'turn_error':
        stream.fail(frame.turnId, frame.seq, {
          code: frame.code,
          message: frame.message,
          retryable: frame.retryable,
        })
        break
      case 'board_update':
        for (const listener of [...this.boardListeners]) listener(frame)
        break
      default:
        // `artifact`, `run_status`, `pong` — nothing to do with them yet.
        break
    }
  }

  // --- presence --------------------------------------------------------------------

  /** Point the heartbeat at a workspace, or pass `null` to stop it. */
  setPresenceTarget(target: { projectId: string; taskId: string | null } | null): void {
    this.target = target
    useSocketStore.getState().setPresence(target)
    if (target === null) {
      this.stopPresence()
      return
    }
    this.startPresence()
  }

  private startPresence(): void {
    this.stopPresence()
    this.beat()
    const tick = () => {
      this.beat()
      this.presenceHandle = this.schedule(tick, PRESENCE_INTERVAL_MS)
    }
    this.presenceHandle = this.schedule(tick, PRESENCE_INTERVAL_MS)
  }

  private stopPresence(): void {
    if (this.presenceHandle !== null) {
      this.cancel(this.presenceHandle)
      this.presenceHandle = null
    }
  }

  private beat(): void {
    if (!this.target) return
    // The two-minute rule. A tab left open in the background must stop claiming the
    // project, or the autonomous agent is blocked from it forever — but a tab hidden for
    // a moment (a tab switch, a notification) should not have to re-establish presence.
    if (this.hiddenSince !== null && this.now() - this.hiddenSince >= HIDDEN_GRACE_MS) {
      this.stopPresence()
      return
    }
    this.send({
      type: 'presence',
      projectId: this.target.projectId,
      taskId: this.target.taskId,
    })
  }

  /** Called from a `visibilitychange` listener. */
  setVisibility(visible: boolean): void {
    if (visible) {
      this.hiddenSince = null
      if (this.target) this.startPresence()
      return
    }
    this.hiddenSince = this.now()
  }
}

let singleton: CoachSocket | null = null

export function getSocket(deps?: SocketDeps): CoachSocket {
  singleton ??= new CoachSocket(deps)
  return singleton
}

/** Test seam, and the hook a sign-out needs so the next user does not inherit a socket. */
export function resetSocket(): void {
  singleton?.close()
  singleton = null
}
