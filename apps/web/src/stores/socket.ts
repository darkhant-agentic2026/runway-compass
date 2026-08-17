/**
 * `useSocketStore` — connection state, for the UI to render.
 *
 * docs/06-frontend.md#zustand-client-only-state: "Connection state, ticket refresh,
 * backoff, resume queue, presence heartbeat."
 *
 * The *mechanism* lives in `lib/socket.ts`; this is only the observable state, so a
 * component can render "Connection lost — your coach is still working. Reconnecting…"
 * without importing the socket module. Keeping them apart is what lets the socket module
 * be tested against a fake `WebSocket` with no React in the picture.
 */

import { create } from 'zustand'

export type ConnectionState = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'failed'

interface SocketStore {
  connection: ConnectionState
  /** How many consecutive reconnect attempts have failed. Drives the backoff. */
  attempts: number
  /** Where the presence heartbeat currently points, or `null` when it is stopped. */
  presence: { projectId: string | null; taskId: string | null } | null
  setConnection: (connection: ConnectionState) => void
  setAttempts: (attempts: number) => void
  setPresence: (presence: { projectId: string | null; taskId: string | null } | null) => void
}

export const useSocketStore = create<SocketStore>((set) => ({
  connection: 'idle',
  attempts: 0,
  presence: null,
  setConnection: (connection) => set({ connection }),
  setAttempts: (attempts) => set({ attempts }),
  setPresence: (presence) => set({ presence }),
}))

/**
 * Whether to tell the user their connection is gone.
 *
 * `connecting` is deliberately excluded: the first connect happens on every page load and
 * announcing it would make a normal load look like a fault.
 */
export function isDisconnected(connection: ConnectionState): boolean {
  return connection === 'reconnecting' || connection === 'failed'
}
