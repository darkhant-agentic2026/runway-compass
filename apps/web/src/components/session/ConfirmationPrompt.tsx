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

import { Paperclip } from 'lucide-react';
import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { useProject } from '@/features/queries';
import { api } from '@/lib/api';
import {
  findAttachmentRef,
  sessionAttachmentNames,
  type PendingConfirmation,
  type TranscriptMessage,
} from '@/lib/transcript';

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
  propose_roadmap_brief: {
    question: 'Start a roadmap run with this brief?',
    confirm: 'Accept plan',
    decline: 'Keep refining',
  },
  materialize_study_plan: {
    question: 'Create these tasks on your board from the study plan?',
    confirm: 'Create tasks',
    decline: 'Not yet',
  },
};

/**
 * The flag that also silences the gate. Mirrors `STOP_CONFIRMING_KEY` in
 * `apps/api/src/coach/agents/tools.py`, which this file cannot import — the same restated
 * constant arrangement as `adk_request_confirmation` itself.
 */
export const STOP_CONFIRMING_KEY = 'stopConfirming';

/**
 * The flag the study-plan approval dialog's "Also update project description" checkbox
 * sets in its answer payload. Mirrors `UPDATE_PROJECT_DESCRIPTION_KEY` in
 * `apps/api/src/coach/agents/tools.py`, which this file cannot import.
 */
export const UPDATE_PROJECT_DESCRIPTION_KEY = 'updateProjectDescription';

/**
 * The key the roadmap brief approval dialog's own attachment checklist sets in its
 * answer payload. Mirrors `CONFIRMED_ATTACHMENTS_KEY` in
 * `apps/api/src/coach/agents/tools.py`, which this file cannot import.
 */
export const CONFIRMED_ATTACHMENTS_KEY = 'confirmedAttachments';

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
  projectId,
  sessionId,
  messages,
  disabled,
  onAnswer,
}: {
  pending: PendingConfirmation;
  /** Which project's stored documents back the dialog's own rendering — see the
   * `isRoadmapBrief` and `isStudyPlanApproval` blocks below. */
  projectId: string;
  sessionId: string;
  /** So a roadmap brief's referenced attachment names can be resolved back to the
   * transcript event that carries them (`findAttachmentRef`). */
  messages: TranscriptMessage[];
  disabled: boolean;
  onAnswer: (confirmed: boolean, payload?: Record<string, unknown>) => void;
}) {
  const project = useProject(projectId);
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
  const isRoadmapBrief = pending.toolName === 'propose_roadmap_brief';
  const isStudyPlanApproval = pending.toolName === 'materialize_study_plan';

  // What the learner reviews is the brief document `write_roadmap_brief` last stored on
  // the project, not the arguments this confirmation call happens to carry — those are
  // the model's own restatement of it, and asking them to approve a restatement is not
  // the same thing as asking them to approve the brief. Falling back to the call's
  // arguments only if the project's own document is not (yet) loaded, which should not
  // happen in practice: `write_roadmap_brief` always runs before `propose_roadmap_brief`
  // can.
  const brief = project.data?.roadmapBrief;

  // `null` means "no opinion yet — use the project's own default"; once the learner
  // clicks the checkbox it becomes an explicit true/false that a background refetch of
  // `project` (which would otherwise change `descriptionEmpty`) must not override.
  const [touchedUpdateDescription, setTouchedUpdateDescription] = useState<boolean | null>(
    null,
  );
  const descriptionEmpty = (project.data?.description ?? '').trim() === '';
  const updateDescription = touchedUpdateDescription ?? descriptionEmpty;

  // Every file the conversation actually has, so the learner can tick or untick any of
  // them right in the dialog — not only the ones the model already thought to reference —
  // and approve in one step instead of typing a correction and waiting on another turn.
  const availableAttachments = isRoadmapBrief ? sessionAttachmentNames(messages) : [];
  // `null` means "no edits yet — default to what the brief document already selected",
  // the same pattern `touchedUpdateDescription` uses and for the same reason: a
  // background refetch of `project` must not silently discard what the learner just
  // ticked. Keyed lowercase throughout so a toggle matches regardless of casing.
  const [touchedAttachments, setTouchedAttachments] = useState<Set<string> | null>(null);
  const selectedAttachments =
    touchedAttachments ?? new Set((brief?.attachments ?? []).map((name) => name.toLowerCase()));
  const confirmedAttachmentNames = availableAttachments.filter((name) =>
    selectedAttachments.has(name.toLowerCase()),
  );
  function toggleAttachment(name: string, checked: boolean) {
    const next = new Set(selectedAttachments);
    const key = name.toLowerCase();
    if (checked) next.add(key);
    else next.delete(key);
    setTouchedAttachments(next);
  }

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

      {isRoadmapBrief ? (
        <div className="space-y-1.5 rounded-md border bg-background/50 p-2.5 text-xs">
          <p className="font-medium text-foreground">
            {brief?.subject ||
              (typeof pending.args.subject === 'string' && pending.args.subject) ||
              'Roadmap'}
          </p>
          <p className="text-muted-foreground">
            Time budget:{' '}
            {brief?.timeBudget ||
              (typeof (pending.args.time_budget ?? pending.args.timeBudget) === 'string' &&
                String(pending.args.time_budget ?? pending.args.timeBudget)) ||
              'unspecified'}
          </p>
          {(
            brief?.specificTopics ??
            ((pending.args.specific_topics ?? pending.args.specificTopics) as
              string[] | undefined) ??
            []
          ).length > 0 ? (
            <p className="text-muted-foreground">
              Topics:{' '}
              {(
                brief?.specificTopics ??
                ((pending.args.specific_topics ?? pending.args.specificTopics) as string[])
              ).join(', ')}
            </p>
          ) : null}
          {brief?.additionalNotes ||
          (typeof (pending.args.additional_notes ?? pending.args.additionalNotes) ===
            'string' &&
            (pending.args.additional_notes ?? pending.args.additionalNotes)) ? (
            <p className="text-muted-foreground">
              Notes:{' '}
              {brief?.additionalNotes ||
                String(pending.args.additional_notes ?? pending.args.additionalNotes)}
            </p>
          ) : null}
          {availableAttachments.length > 0 ? (
            <div className="space-y-1 border-t pt-1.5">
              <p className="text-muted-foreground">
                Attachments in this conversation — select the ones that belong on this roadmap:
              </p>
              <ul className="space-y-1">
                {availableAttachments.map((name, index) => (
                  <li key={name}>
                    <BriefAttachmentOption
                      id={`brief-attachment-${index}`}
                      name={name}
                      checked={selectedAttachments.has(name.toLowerCase())}
                      disabled={disabled}
                      sessionId={sessionId}
                      messages={messages}
                      onToggle={(checked) => toggleAttachment(name, checked)}
                    />
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}

      {isStudyPlanApproval ? (
        <div className="flex items-center gap-2 rounded-md border bg-background/50 p-2.5 text-xs">
          <Checkbox
            id="update-project-description"
            checked={updateDescription}
            disabled={disabled}
            onCheckedChange={(checked) => setTouchedUpdateDescription(checked === true)}
            aria-label="Also update project description"
          />
          <Label
            htmlFor="update-project-description"
            className="text-xs font-normal text-foreground"
          >
            Also update project description
          </Label>
        </div>
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
        <Button
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={() => {
            if (isStudyPlanApproval) {
              onAnswer(true, { [UPDATE_PROJECT_DESCRIPTION_KEY]: updateDescription });
              return;
            }
            // Only when the dialog actually offered a checklist — an empty list here
            // would mean "the learner approved zero attachments" to the server, where
            // what it actually means is "this conversation has none to offer", and the
            // model's own `attachments` argument should stand in that case instead
            // (`_confirmed_attachments`'s own `None`-vs-`[]` distinction, `agents/tools.py`).
            if (isRoadmapBrief && availableAttachments.length > 0) {
              onAnswer(true, { [CONFIRMED_ATTACHMENTS_KEY]: confirmedAttachmentNames });
              return;
            }
            onAnswer(true);
          }}
        >
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

/**
 * One row of the roadmap-brief approval dialog's own attachment checklist: a checkbox
 * for whether it belongs on the brief, plus — for an image, resolved against the
 * transcript via `findAttachmentRef` — a small hover preview, fetched lazily so opening
 * the dialog does not fetch every attachment in the conversation at once.
 */
function BriefAttachmentOption({
  id,
  name,
  checked,
  disabled,
  sessionId,
  messages,
  onToggle,
}: {
  id: string;
  name: string;
  checked: boolean;
  disabled: boolean;
  sessionId: string;
  messages: TranscriptMessage[];
  onToggle: (checked: boolean) => void;
}) {
  const ref = findAttachmentRef(messages, name);
  const isImage = ref?.mimeType.startsWith('image/') ?? false;
  const [url, setUrl] = useState<string | null>(null);

  return (
    <div className="flex items-center gap-2">
      <Checkbox
        id={id}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(value) => onToggle(value === true)}
        aria-label={name}
      />
      <Label htmlFor={id} className="flex-1 truncate font-normal text-foreground">
        {name}
      </Label>
      {isImage && ref ? (
        <Tooltip
          onOpenChange={(open) => {
            if (!open || url) return;
            void api
              .getEventAttachment(sessionId, ref.seq, ref.index)
              .then((blob) => setUrl(URL.createObjectURL(blob)))
              .catch(() => {
                // No preview; the checklist row alone still names the file.
              });
          }}
        >
          <TooltipTrigger
            render={
              <button
                type="button"
                className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground"
              />
            }
          >
            <Paperclip className="size-3" aria-hidden="true" />
            <span className="sr-only">Preview {name}</span>
          </TooltipTrigger>
          <TooltipContent>
            {url ? (
              <img src={url} alt={name} className="max-h-32 w-auto rounded" />
            ) : (
              <span>Loading preview…</span>
            )}
          </TooltipContent>
        </Tooltip>
      ) : null}
    </div>
  );
}
