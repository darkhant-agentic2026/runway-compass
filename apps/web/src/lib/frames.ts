/**
 * WebSocket frames, validated at the boundary.
 *
 * docs/06-frontend.md: "Frames are validated with Zod at the boundary; unknown `type` is
 * ignored forward-compatibly." Both halves of that sentence are load-bearing.
 *
 * - *Validated* — a frame is the one input to the stream reducer, and a malformed one
 *   would corrupt a transcript rather than throw somewhere useful.
 * - *Unknown types ignored* — the server may learn to send `run_status` (M5) or new
 *   artifact kinds before this client learns to render them, and a deployed tab must not
 *   break when it does. Note the asymmetry with the server, which *rejects* unknown
 *   client frames: the client is ours and a typo there is a bug, not a version skew.
 *
 * Mirrors `apps/api/src/coach/ws/protocol.py`.
 */

import { z } from 'zod'

// --- server → client -------------------------------------------------------------------

export const turnStartFrame = z.object({
  type: z.literal('turn_start'),
  turnId: z.string(),
  sessionId: z.string(),
})

export const deltaFrame = z.object({
  type: z.literal('delta'),
  turnId: z.string(),
  seq: z.number().int(),
  text: z.string(),
})

export const toolCallFrame = z.object({
  type: z.literal('tool_call'),
  turnId: z.string(),
  seq: z.number().int(),
  name: z.string(),
  argsPreview: z.record(z.string(), z.unknown()).default({}),
})

export const toolResultFrame = z.object({
  type: z.literal('tool_result'),
  turnId: z.string(),
  seq: z.number().int(),
  name: z.string(),
  ok: z.boolean().default(true),
})

export const artifactFrame = z.object({
  type: z.literal('artifact'),
  turnId: z.string(),
  seq: z.number().int(),
  kind: z.string(),
  reportId: z.string().nullable().default(null),
  taskId: z.string().nullable().default(null),
})

export const turnCompleteFrame = z.object({
  type: z.literal('turn_complete'),
  turnId: z.string(),
  seq: z.number().int(),
  eventIds: z.array(z.string()).default([]),
})

export const turnErrorFrame = z.object({
  type: z.literal('turn_error'),
  turnId: z.string(),
  seq: z.number().int(),
  code: z.string(),
  message: z.string(),
  retryable: z.boolean().default(true),
})

export const boardUpdateFrame = z.object({
  type: z.literal('board_update'),
  projectId: z.string(),
  taskIds: z.array(z.string()).default([]),
  origin: z.string().default('agent'),
  runId: z.string().nullable().default(null),
})

export const runStatusFrame = z.object({
  type: z.literal('run_status'),
  runId: z.string(),
  step: z.string(),
  status: z.string(),
})

export const pongFrame = z.object({ type: z.literal('pong') })

export const serverFrameSchema = z.discriminatedUnion('type', [
  turnStartFrame,
  deltaFrame,
  toolCallFrame,
  toolResultFrame,
  artifactFrame,
  turnCompleteFrame,
  turnErrorFrame,
  boardUpdateFrame,
  runStatusFrame,
  pongFrame,
])

export type ServerFrame = z.infer<typeof serverFrameSchema>
export type DeltaFrame = z.infer<typeof deltaFrame>
export type TurnErrorFrame = z.infer<typeof turnErrorFrame>
export type BoardUpdateFrame = z.infer<typeof boardUpdateFrame>

/**
 * Parse one raw message.
 *
 * Returns `null` for anything unrecognised — an unknown `type`, a malformed payload, or
 * a non-JSON message. The caller drops it silently, which is what forward compatibility
 * looks like from the inside.
 */
export function parseServerFrame(raw: unknown): ServerFrame | null {
  const source = typeof raw === 'string' ? safeJsonParse(raw) : raw
  if (source === null) return null
  const parsed = serverFrameSchema.safeParse(source)
  return parsed.success ? parsed.data : null
}

function safeJsonParse(raw: string): unknown {
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

// --- client → server -------------------------------------------------------------------

export type ClientFrame =
  | { type: 'subscribe'; turnId: string }
  | { type: 'subscribe'; runId: string }
  | { type: 'resume'; turnId: string; lastSeq: number }
  | { type: 'unsubscribe'; turnId: string }
  | { type: 'presence'; projectId: string | null; taskId: string | null }
  | { type: 'ping' }
