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
  attachments: TranscriptAttachment[]
}

export interface TranscriptAttachment {
  mimeType: string
  /**
   * Known only for a message still being sent.
   *
   * A stored event carries a `gs://` URI ending in the artifact name — `user:up_01J…/0` —
   * and no human filename, because the display name lives on `uploads/{uploadId}` rather
   * than in the transcript. So history shows the *kind* of file and the composer's
   * optimistic echo shows its name, which is the most either source honestly knows.
   */
  filename?: string
}

const MIME_LABELS: Record<string, string> = {
  'image/png': 'Image',
  'image/jpeg': 'Image',
  'image/webp': 'Image',
  'application/pdf': 'PDF',
  'text/plain': 'Text file',
  'text/markdown': 'Markdown',
}

export function attachmentLabel(attachment: TranscriptAttachment): string {
  return attachment.filename ?? MIME_LABELS[attachment.mimeType] ?? 'Attachment'
}

/**
 * One part of an event's content, **as stored**.
 *
 * The keys are `snake_case`, and that is the whole subtlety of this module. ADK's `Event`
 * declares `alias_generator=to_camel`, so the aliases are camelCase and it is natural to
 * assume the JSON is too — but `append_event` stores
 * `event.model_dump(exclude_none=True, mode="json")`, and `model_dump` defaults to
 * `by_alias=False`. Reading `fileData` instead of `file_data` finds nothing and silently
 * renders a message with no sign it ever carried a file, which is exactly what happened.
 *
 * Both spellings are accepted here because the shape is not ours to fix: a future ADK
 * version could serialize by alias, and the synthetic in-flight event built in
 * `features/queries.ts` is ours to write either way. `session-event-vectors.json` pins the
 * shape that is actually stored today.
 */
interface EventPart {
  text?: unknown
  thought?: unknown
  function_call?: FunctionCallLike
  functionCall?: FunctionCallLike
  function_response?: FunctionCallLike
  functionResponse?: FunctionCallLike
  inline_data?: FileLike
  inlineData?: FileLike
  file_data?: FileLike
  fileData?: FileLike
}

interface FunctionCallLike {
  name?: unknown
  id?: unknown
  args?: unknown
}

interface FileLike {
  file_uri?: unknown
  fileUri?: unknown
  mime_type?: unknown
  mimeType?: unknown
  /** The user's own filename, set by `TurnService._build_content`. */
  display_name?: unknown
  displayName?: unknown
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
 * (docs/06-frontend.md renders it as "inline status chips"). An event with neither text,
 * a tool, nor an attachment is dropped rather than rendered as an empty bubble.
 *
 * **Attachments are carried through, not counted.** An earlier version reduced them to a
 * number and then dropped it, so a message with both a question and a screenshot rendered
 * as the question alone — the transcript gave no sign that a file had ever been sent, and
 * a user rereading the conversation could not tell what the coach had been looking at.
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
      .map(toolNameOf)
      .filter((name): name is string => name !== undefined)

    const attachments = attachmentsOf(parts)
    // Text or an attachment is what makes a bubble. An event carrying *only* tool calls
    // is not a message: rendering one produces an empty bubble, since the transcript shows
    // tool activity nowhere. `toolNames` still rides along on messages that have content
    // too, so M3 can surface "and it updated your board" once the agent has tools.
    if (!text && attachments.length === 0) continue

    messages.push({
      id: stored.eventId,
      seq: stored.seq,
      role: roleOf(event, author),
      author,
      text,
      toolNames,
      attachments,
    })
  }
  return messages
}

/** First of the candidates that is actually a string. */
function str(...candidates: unknown[]): string | undefined {
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate) return candidate
  }
  return undefined
}

function fileOf(part: EventPart): FileLike | undefined {
  return part.file_data ?? part.fileData ?? part.inline_data ?? part.inlineData
}

/**
 * ADK's own name for the synthetic call a `require_confirmation` tool produces.
 *
 * Mirrors `CONFIRMATION_FUNCTION_NAME` in `apps/api/src/coach/services/turns.py`, which
 * mirrors `google.adk.flows.llm_flows.functions` — a private module the server restates
 * and this file cannot import at all. The pair is on the ADK bump checklist.
 */
export const CONFIRMATION_FUNCTION_NAME = 'adk_request_confirmation'

export interface PendingConfirmation {
  /** The id of the `adk_request_confirmation` call, sent back to answer it. */
  functionCallId: string
  /** The tool waiting on the answer, e.g. `discard_task`. */
  toolName: string
  /** That tool's arguments, for saying *what* is about to happen. */
  args: Record<string, unknown>
}

/**
 * The question the transcript is waiting on, if any.
 *
 * A gated tool ends the turn with an `adk_request_confirmation` call and resumes only
 * when a function *response* to that call arrives, so "pending" means exactly: a request
 * exists and nothing has answered it.
 *
 * **It is not simply the last event.** ADK emits the request and then a function-response
 * event carrying `requested_tool_confirmations`, so the request is second-from-last the
 * moment it is created — a reader that looked only at the tail would find nothing and the
 * buttons would never appear. Scanning newest-first and cancelling requests against
 * answers is also what keeps an *older*, already-answered request from resurrecting its
 * buttons later in the conversation.
 */
export function pendingConfirmation(events: SessionEvent[]): PendingConfirmation | null {
  const answered = new Set<string>()

  for (const stored of [...events].sort((a, b) => b.seq - a.seq)) {
    for (const part of partsOf(stored.event)) {
      const response = part.function_response ?? part.functionResponse
      const responseId = str(response?.id)
      if (response?.name === CONFIRMATION_FUNCTION_NAME && responseId) {
        answered.add(responseId)
      }

      const call = part.function_call ?? part.functionCall
      if (!call || call.name !== CONFIRMATION_FUNCTION_NAME) continue
      const callId = str(call.id)
      if (!callId || answered.has(callId)) continue

      const args = (call.args ?? {}) as Record<string, unknown>
      const original = (args.originalFunctionCall ?? {}) as FunctionCallLike
      return {
        functionCallId: callId,
        toolName: str(original.name) ?? 'this change',
        args: (original.args ?? {}) as Record<string, unknown>,
      }
    }
  }
  return null
}

function toolNameOf(part: EventPart): string | undefined {
  return str(
    part.function_call?.name,
    part.functionCall?.name,
  )
}

function attachmentsOf(parts: EventPart[]): TranscriptAttachment[] {
  const attachments: TranscriptAttachment[] = []
  for (const part of parts) {
    const file = fileOf(part)
    if (!file) continue
    const filename = str(file.display_name, file.displayName)
    attachments.push({
      mimeType: str(file.mime_type, file.mimeType) ?? '',
      ...(filename ? { filename } : {}),
    })
  }
  return attachments
}
