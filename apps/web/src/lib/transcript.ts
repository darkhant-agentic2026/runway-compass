/**
 * Turning stored ADK events into renderable messages.
 *
 * `GET /api/sessions/{sid}/events` returns the serialized ADK `Event` verbatim
 * (docs/02-data-model.md nests the whole event under `event_data`, and the API does not
 * reshape it). That is the right call on the wire — a projection would be a second
 * definition of a conversation turn, coupled to a pinned dependency's model — but it
 * means the UI needs one place that knows how to read it. This is that place.
 *
 * Everything here is defensive rather than schema-validated, for the same reason: the
 * event shape belongs to `google-adk`, so a field appearing or moving must degrade to a
 * missing bubble rather than a failed parse of the whole page.
 */

import type { SessionEvent } from '@/lib/schemas'

export interface TranscriptMessage {
  id: string
  seq: number
  /** `user` or the agent's name; `role` is what the bubble is styled by. */
  role: 'user' | 'model'
  author: string
  text: string
  toolNames: string[]
}

interface EventPart {
  text?: unknown
  thought?: unknown
  functionCall?: { name?: unknown }
  functionResponse?: { name?: unknown }
  inlineData?: unknown
  fileData?: { fileUri?: unknown; mimeType?: unknown }
}

function partsOf(event: Record<string, unknown>): EventPart[] {
  const content = event.content
  if (!content || typeof content !== 'object') return []
  const parts = (content as { parts?: unknown }).parts
  return Array.isArray(parts) ? (parts as EventPart[]) : []
}

function roleOf(event: Record<string, unknown>, author: string): 'user' | 'model' {
  const content = event.content
  const role =
    content && typeof content === 'object' ? (content as { role?: unknown }).role : undefined
  if (role === 'user' || author === 'user') return 'user'
  return 'model'
}

/**
 * One message per stored event, in `seq` order.
 *
 * Events carrying only a function call or response become no message at all — tool
 * activity is a chip during the stream and is not part of the transcript afterwards
 * (docs/06-frontend.md renders it as "inline status chips"). An event with neither text
 * nor a tool is dropped rather than rendered as an empty bubble.
 */
export function toMessages(events: SessionEvent[]): TranscriptMessage[] {
  const messages: TranscriptMessage[] = []
  for (const stored of [...events].sort((a, b) => a.seq - b.seq)) {
    const event = stored.event
    const author = typeof event.author === 'string' ? event.author : 'model'
    const parts = partsOf(event)

    const text = parts
      .filter((part) => typeof part.text === 'string' && !part.thought)
      .map((part) => part.text as string)
      .join('')

    const toolNames = parts
      .map((part) => part.functionCall?.name)
      .filter((name): name is string => typeof name === 'string')

    const attachments = parts.filter((part) => part.fileData || part.inlineData).length
    if (!text && toolNames.length === 0 && attachments === 0) continue

    messages.push({
      id: stored.eventId,
      seq: stored.seq,
      role: roleOf(event, author),
      author,
      text: text || (attachments > 0 ? '' : ''),
      toolNames,
    })
  }
  return messages
}

/** How many attachments an event carried, for the "sent a file" affordance. */
export function attachmentCount(stored: SessionEvent): number {
  return partsOf(stored.event).filter((part) => part.fileData || part.inlineData).length
}
