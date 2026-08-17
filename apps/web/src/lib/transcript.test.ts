/**
 * Reading a stored ADK event into a renderable message.
 *
 * The event shape belongs to `google-adk` and the API returns it unprojected
 * (docs/02-data-model.md), so this module is the one place that knows how to read it —
 * and everything here is defensive on purpose: a field appearing or moving between pinned
 * versions must cost a missing bubble, not a failed parse of the whole page.
 *
 * The attachment cases are the ones with history. An earlier version reduced attachments
 * to a count and then dropped it, so a message carrying both a question and a screenshot
 * rendered as the question alone — nothing in the transcript showed that a file had been
 * sent, and a user rereading the conversation could not tell what the coach had been
 * looking at.
 */

import { describe, expect, it } from 'vitest'

import vectors from '@/lib/session-event-vectors.json'
import {
  CONFIRMATION_FUNCTION_NAME,
  attachmentLabel,
  pendingConfirmation,
  toMessages,
} from '@/lib/transcript'
import type { SessionEvent } from '@/lib/schemas'

function event(seq: number, body: Record<string, unknown>): SessionEvent {
  return { seq, eventId: `e_${seq}`, event: body }
}

/**
 * Against events dumped by the Python side, not by hand.
 *
 * This block exists because hand-written fixtures let a real bug through. `Event` declares
 * `alias_generator=to_camel`, so its *aliases* are camelCase — but `append_event` stores
 * `model_dump(...)` with the default `by_alias=False`, so the keys are `file_data` and
 * `mime_type`. The reader looked for `fileData`, found nothing, and rendered messages with
 * no sign they had ever carried a file. Every test passed, because the fixtures repeated
 * the same wrong assumption.
 *
 * Regenerate with `./scripts/dev.sh gen-event-vectors` after an ADK bump.
 */
describe('parity with events as the server stores them', () => {
  for (const vector of vectors.events) {
    it(vector.name, () => {
      const messages = toMessages([event(1, vector.event as Record<string, unknown>)])
      const expected = vector.expect as Record<string, unknown>

      if (expected.dropped) {
        expect(messages).toHaveLength(0)
        return
      }

      expect(messages).toHaveLength(1)
      const message = messages[0]!
      expect(message.role).toBe(expected.role)
      expect(message.text).toBe(expected.text)
      expect(message.attachments).toHaveLength(expected.attachments as number)
      if (expected.attachmentMimeType) {
        expect(message.attachments[0]?.mimeType).toBe(expected.attachmentMimeType)
      }
      if (expected.attachmentFilename) {
        expect(message.attachments[0]?.filename).toBe(expected.attachmentFilename)
      }
      if (expected.toolNames) {
        expect(message.toolNames).toEqual(expected.toolNames)
      }
    })
  }
})

describe('the stored spelling, spelled out', () => {
  it('reads snake_case file_data, which is what is actually in Firestore', () => {
    const messages = toMessages([
      event(1, {
        author: 'user',
        content: {
          role: 'user',
          parts: [
            { text: 'look' },
            {
              file_data: {
                mime_type: 'image/png',
                file_uri: 'gs://b/coach/u/user/user:up_1/0',
                display_name: 'shot.png',
              },
            },
          ],
        },
      }),
    ])

    expect(messages[0]?.attachments).toEqual([
      { mimeType: 'image/png', filename: 'shot.png' },
    ])
  })

  it('still reads camelCase, so a future ADK change is not a regression', () => {
    const messages = toMessages([
      event(1, {
        author: 'user',
        content: { parts: [{ fileData: { mimeType: 'application/pdf' } }] },
      }),
    ])

    expect(messages[0]?.attachments).toEqual([{ mimeType: 'application/pdf' }])
  })
})

function userText(seq: number, text: string): SessionEvent {
  return event(seq, { author: 'user', content: { role: 'user', parts: [{ text }] } })
}

describe('text', () => {
  it('orders by seq regardless of input order', () => {
    const messages = toMessages([userText(3, 'third'), userText(1, 'first'), userText(2, 'second')])

    expect(messages.map((message) => message.text)).toEqual(['first', 'second', 'third'])
  })

  it('joins several text parts into one message', () => {
    const messages = toMessages([
      event(1, { author: 'coach', content: { parts: [{ text: 'Hello' }, { text: ', world' }] } }),
    ])

    expect(messages[0]?.text).toBe('Hello, world')
  })

  it('leaves thought parts out of the transcript', () => {
    const messages = toMessages([
      event(1, {
        author: 'coach',
        content: { parts: [{ text: 'thinking…', thought: true }, { text: 'the answer' }] },
      }),
    ])

    expect(messages[0]?.text).toBe('the answer')
  })

  it('reads the role from the author when the content does not carry one', () => {
    const messages = toMessages([event(1, { author: 'user', content: { parts: [{ text: 'hi' }] } })])

    expect(messages[0]?.role).toBe('user')
  })

  it('treats an unknown author as the model rather than the user', () => {
    const messages = toMessages([event(1, { author: 'coach_agent', content: { parts: [{ text: 'x' }] } })])

    expect(messages[0]?.role).toBe('model')
  })
})

describe('attachments', () => {
  it('reads inline data as an attachment too', () => {
    const messages = toMessages([
      event(1, {
        author: 'user',
        content: { parts: [{ inline_data: { mime_type: 'image/jpeg' } }] },
      }),
    ])

    expect(messages[0]?.attachments).toEqual([{ mimeType: 'image/jpeg' }])
  })

  it('survives an attachment with no mime type at all', () => {
    const messages = toMessages([
      event(1, { author: 'user', content: { parts: [{ file_data: {} }] } }),
    ])

    expect(messages[0]?.attachments).toEqual([{ mimeType: '' }])
  })
})

describe('what is not a message', () => {
  it('drops an event carrying only a tool call', () => {
    // Tool activity is a chip during the stream, not part of the transcript after it
    // (docs/06-frontend.md).
    const messages = toMessages([
      event(1, { author: 'coach', content: { parts: [{ function_call: { name: 'add_task' } }] } }),
    ])

    expect(messages).toHaveLength(0)
  })

  it('drops an empty event rather than rendering a blank bubble', () => {
    const messages = toMessages([
      event(1, { author: 'coach', content: { parts: [] } }),
      event(2, { author: 'coach' }),
    ])

    expect(messages).toHaveLength(0)
  })

  it('tolerates content that is not shaped like content', () => {
    const messages = toMessages([event(1, { author: 'coach', content: 'not an object' })])

    expect(messages).toHaveLength(0)
  })
})

describe('attachmentLabel', () => {
  it('prefers a filename when one is known', () => {
    expect(attachmentLabel({ mimeType: 'image/png', filename: 'shot.png' })).toBe('shot.png')
  })

  it('falls back to the kind of file', () => {
    expect(attachmentLabel({ mimeType: 'image/webp' })).toBe('Image')
    expect(attachmentLabel({ mimeType: 'application/pdf' })).toBe('PDF')
    expect(attachmentLabel({ mimeType: 'text/markdown' })).toBe('Markdown')
  })

  it('says something rather than nothing for a type it does not know', () => {
    expect(attachmentLabel({ mimeType: 'application/x-unknown' })).toBe('Attachment')
    expect(attachmentLabel({ mimeType: '' })).toBe('Attachment')
  })
})

/**
 * The gated-tool handshake, read from the stored transcript.
 *
 * The event ordering here is the shape the server actually writes, and it is the reason
 * this is not a one-liner: ADK emits the `adk_request_confirmation` call and *then* a
 * function-response event carrying `requested_tool_confirmations`, so the request is
 * never the last event. A reader that looked only at the tail found nothing and the
 * buttons never appeared — which is exactly how it failed the first time.
 */
describe('a tool waiting on the learner', () => {
  const requestId = 'adk-request-1'
  const originalId = 'adk-call-1'

  function request(seq: number): SessionEvent {
    return event(seq, {
      author: 'coach_agent',
      content: {
        role: 'model',
        parts: [
          {
            function_call: {
              id: requestId,
              name: CONFIRMATION_FUNCTION_NAME,
              args: {
                originalFunctionCall: {
                  id: originalId,
                  name: 'discard_task',
                  args: { task_id: 'k_1', reason: 'You asked me to drop this one.' },
                },
                toolConfirmation: { confirmed: false, hint: 'Approve or reject.' },
              },
            },
          },
        ],
      },
    })
  }

  /** What ADK appends after the request, and what used to hide it. */
  function trailer(seq: number): SessionEvent {
    return event(seq, {
      author: 'coach_agent',
      actions: { requested_tool_confirmations: { [originalId]: { confirmed: false } } },
    })
  }

  function answer(seq: number, confirmed: boolean): SessionEvent {
    return event(seq, {
      author: 'user',
      content: {
        role: 'user',
        parts: [
          {
            function_response: {
              id: requestId,
              name: CONFIRMATION_FUNCTION_NAME,
              response: { confirmed },
            },
          },
        ],
      },
    })
  }

  it('is found even though it is not the last event', () => {
    const pending = pendingConfirmation([userText(1, 'discard that'), request(2), trailer(3)])
    expect(pending).toEqual({
      functionCallId: requestId,
      toolName: 'discard_task',
      args: { task_id: 'k_1', reason: 'You asked me to drop this one.' },
    })
  })

  it('is gone once it has been answered', () => {
    const events = [request(2), trailer(3), answer(4, true)]
    expect(pendingConfirmation(events)).toBeNull()
  })

  it('is gone after a refusal too, not just after an approval', () => {
    expect(pendingConfirmation([request(2), trailer(3), answer(4, false)])).toBeNull()
  })

  it('does not resurrect an older request when a new one is pending', () => {
    const events = [request(2), trailer(3), answer(4, true), request(6), trailer(7)]
    // Same id in this fixture, so the assertion that matters is that an *unanswered*
    // later request wins over the earlier answer rather than being cancelled by it.
    expect(pendingConfirmation(events)?.functionCallId).toBe(requestId)
  })

  it('is null for an ordinary conversation', () => {
    expect(pendingConfirmation([userText(1, 'hello')])).toBeNull()
  })

  it('is null for an empty transcript', () => {
    expect(pendingConfirmation([])).toBeNull()
  })
})
