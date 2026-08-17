/**
 * The question a gated tool is waiting on.
 *
 * `discard_task` "requires user confirmation" (docs/03-agent-design.md), and the gate is
 * ADK's, not the model's: the turn ends with an `adk_request_confirmation` call and the
 * tool body runs only once a matching response arrives. This is the control that sends
 * one — so without it the gate would be unanswerable, which looks exactly like a working
 * gate until somebody tries to say yes.
 *
 * Two deliberate choices in the copy. The destructive action is named ("Discard it"),
 * never "Yes", because a button that only makes sense next to a question it may have
 * scrolled away from is a button people press by accident. And "Keep it" is the primary
 * affordance: refusing is the safe answer, so it is the easy one.
 */

import { Button } from '@/components/ui/button'
import type { PendingConfirmation } from '@/lib/transcript'

const LABELS: Record<string, { question: string; confirm: string }> = {
  discard_task: { question: 'Discard this task?', confirm: 'Discard it' },
}

/** The task title is not in the tool's arguments — only its id — so `reason` is the copy. */
function describe(pending: PendingConfirmation): string {
  const reason = pending.args.reason
  return typeof reason === 'string' && reason ? reason : ''
}

export function ConfirmationPrompt({
  pending,
  disabled,
  onAnswer,
}: {
  pending: PendingConfirmation
  disabled: boolean
  onAnswer: (confirmed: boolean) => void
}) {
  const labels = LABELS[pending.toolName] ?? {
    question: `Let your coach ${pending.toolName.replaceAll('_', ' ')}?`,
    confirm: 'Go ahead',
  }

  return (
    <div
      className="bg-muted/40 m-3 space-y-2 rounded-lg border p-3"
      data-testid="confirmation-prompt"
      role="group"
      aria-label={labels.question}
    >
      <p className="text-sm font-medium">{labels.question}</p>
      {describe(pending) ? (
        <p className="text-muted-foreground text-sm">{describe(pending)}</p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={disabled} onClick={() => onAnswer(false)}>
          Keep it
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={() => onAnswer(true)}
        >
          {labels.confirm}
        </Button>
      </div>
    </div>
  )
}
