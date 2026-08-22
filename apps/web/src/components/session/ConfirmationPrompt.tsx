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
 *
 * **`complete_task_item` gets a third button**, which approves *and* turns the gate off for
 * this project. On a project of short, obvious tasks a dialog per step is friction rather
 * than a safeguard, and the moment that becomes obvious is the moment you are looking at
 * one — so the setting is offered there as well as in project settings. It rides in the
 * answer's payload rather than being a second request, so one click is one round trip and
 * the preference cannot land without the completion it was attached to.
 */

import { Button } from '@/components/ui/button';
import type { PendingConfirmation } from '@/lib/transcript';

const LABELS: Record<string, { question: string; confirm: string; decline?: string }> = {
  discard_task: { question: 'Discard this task?', confirm: 'Discard it', decline: 'Keep it' },
  complete_task_item: {
    question: 'Mark this step done?',
    confirm: 'Mark it done',
    // "Keep it" reads as the safe answer to a *discard*; for a completion the safe answer
    // is that they have not finished yet, and saying so is what the button should say.
    decline: 'Not yet',
  },
  delete_task_item: {
    question: 'Remove this step from the checklist?',
    confirm: 'Remove step',
    decline: 'Keep it',
  },
  update_project_plan: {
    question: 'Update the project plan with these tasks?',
    confirm: 'Accept plan',
    decline: 'Keep refining',
  },
};

/**
 * The flag that also silences the gate. Mirrors `STOP_CONFIRMING_KEY` in
 * `apps/api/src/coach/agents/tools.py`, which this file cannot import — the same restated
 * constant arrangement as `adk_request_confirmation` itself.
 */
export const STOP_CONFIRMING_KEY = 'stopConfirming';

/** The task title is not in the tool's arguments — only its id — so `reason` is the copy. */
function describe(pending: PendingConfirmation): string {
  const reason = pending.args.reason;
  if (typeof reason === 'string' && reason) return reason;
  if (pending.toolName === 'update_project_plan' && typeof pending.args.summary === 'string') {
    return pending.args.summary;
  }
  return '';
}

export function ConfirmationPrompt({
  pending,
  disabled,
  onAnswer,
}: {
  pending: PendingConfirmation;
  disabled: boolean;
  onAnswer: (confirmed: boolean, payload?: Record<string, unknown>) => void;
}) {
  const labels = LABELS[pending.toolName] ?? {
    question: `Let your coach ${pending.toolName.replaceAll('_', ' ')}?`,
    confirm: 'Go ahead',
  };
  // Only this tool's gate is a preference. `discard_task` and `delete_task_item` are
  // destructive and not routine, and a learner who silenced one did not ask for the others
  // (`agents/tools.py`).
  const offerToSilence = pending.toolName === 'complete_task_item';
  const isPlanUpdate =
    pending.toolName === 'update_project_plan' && Array.isArray(pending.args.tasks);

  return (
    <div
      className="m-3 space-y-2 rounded-lg border bg-muted/40 p-3"
      data-testid="confirmation-prompt"
      role="group"
      aria-label={labels.question}
    >
      <p className="text-sm font-medium">{labels.question}</p>
      {describe(pending) && !isPlanUpdate ? (
        <p className="text-sm text-muted-foreground">{describe(pending)}</p>
      ) : null}

      {isPlanUpdate ? (
        <div className="space-y-1.5 rounded-md border bg-background/50 p-2.5 text-xs">
          {typeof pending.args.summary === 'string' && pending.args.summary ? (
            <p className="font-medium text-foreground">{pending.args.summary}</p>
          ) : null}
          <ol className="list-inside list-decimal space-y-1 text-muted-foreground">
            {(pending.args.tasks as Array<Record<string, unknown>>).map((task, index) => {
              const minutes = task.estimated_minutes ?? task.estimatedMinutes;
              const title = typeof task.title === 'string' ? task.title : 'Task';
              const description =
                typeof task.description === 'string' ? task.description : undefined;
              return (
                <li key={index}>
                  <span className="font-medium text-foreground">{title}</span>
                  {typeof minutes === 'number' ? ` (${minutes} min)` : ''}
                  {description ? ` — ${description}` : ''}
                </li>
              );
            })}
          </ol>
        </div>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button size="sm" disabled={disabled} onClick={() => onAnswer(false)}>
          {labels.decline ?? 'Keep it'}
        </Button>
        <Button size="sm" variant="outline" disabled={disabled} onClick={() => onAnswer(true)}>
          {labels.confirm}
        </Button>
        {offerToSilence ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled}
            onClick={() => onAnswer(true, { [STOP_CONFIRMING_KEY]: true })}
          >
            Mark it done and stop asking in this project
          </Button>
        ) : null}
      </div>
    </div>
  );
}
