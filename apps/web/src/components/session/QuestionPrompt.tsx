/**
 * A question the coach asked, as controls rather than as prose.
 *
 * `ask_learner` posts its question through the same `adk_request_confirmation` handshake
 * that gates `discard_task` — ADK's `ToolConfirmation` has a free-form `payload`, so a
 * tool can ask for a *choice* and get the selection back. This renders that payload.
 *
 * **Why it is not a modal.** It sits in the transcript, where the question was asked, for
 * the same reason the confirmation prompt does: the answer becomes part of the
 * conversation, and a dialog that covers the conversation hides the context the question
 * is about. It also means an unanswered question is still there after a reload — the
 * pending state is derived from stored events, not from component state.
 *
 * Single-select uses radios and multi-select checkboxes, rather than one component with a
 * flag, because the two have genuinely different keyboard semantics and a radio group that
 * sometimes lets you pick two is worse than either.
 */

import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import type { ConfirmationQuestion } from '@/lib/transcript';

export interface QuestionAnswer {
  selected: string[];
  note: string;
}

export function QuestionPrompt({
  question,
  disabled,
  onAnswer,
}: {
  question: ConfirmationQuestion;
  disabled: boolean;
  /** `null` means the learner declined — "none of these" is a real answer. */
  onAnswer: (answer: QuestionAnswer | null) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);
  const [note, setNote] = useState('');

  const name = `question-${question.question.slice(0, 24)}`;
  const canSubmit = selected.length > 0;

  function toggle(option: string) {
    setSelected((current) => {
      if (!question.allowMultiple) return [option];
      return current.includes(option)
        ? current.filter((entry) => entry !== option)
        : [...current, option];
    });
  }

  return (
    <div
      className="m-3 space-y-3 rounded-lg border bg-muted/40 p-3"
      data-testid="question-prompt"
      role="group"
      aria-label={question.question}
    >
      <p className="text-sm font-medium">{question.question}</p>

      <div className="space-y-1.5">
        {question.options.map((option) =>
          question.allowMultiple ? (
            <div key={option} className="flex items-center gap-2">
              <Checkbox
                id={`${name}-${option}`}
                checked={selected.includes(option)}
                disabled={disabled}
                onCheckedChange={() => toggle(option)}
                // The design system's checkbox is a styled button beside a visually-hidden
                // native input, and `<Label htmlFor>` names the *input*. Without this the
                // control the learner actually clicks has no accessible name at all — which
                // is a screen-reader defect first and an untestable control second.
                aria-label={option}
              />
              <Label htmlFor={`${name}-${option}`} className="text-sm font-normal">
                {option}
              </Label>
            </div>
          ) : (
            <div key={option} className="flex items-center gap-2">
              {/*
                A native radio rather than the design system's toggle: this is a
                single-choice group, and the browser's roving-focus and arrow-key
                behaviour is the accessible default we would otherwise have to rebuild.
              */}
              <input
                type="radio"
                id={`${name}-${option}`}
                name={name}
                className="size-4 accent-primary"
                checked={selected[0] === option}
                disabled={disabled}
                onChange={() => toggle(option)}
              />
              <Label htmlFor={`${name}-${option}`} className="text-sm font-normal">
                {option}
              </Label>
            </div>
          ),
        )}
      </div>

      {question.notePrompt ? (
        <div className="space-y-1">
          <Label htmlFor={`${name}-note`} className="text-xs text-muted-foreground">
            {question.notePrompt}
          </Label>
          <Input
            id={`${name}-note`}
            value={note}
            disabled={disabled}
            maxLength={2000}
            onChange={(event) => setNote(event.target.value)}
          />
        </div>
      ) : null}

      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={disabled || !canSubmit}
          onClick={() => onAnswer({ selected, note })}
        >
          Send
        </Button>
        {/*
          Only when the tool said so. "None of these" is a real answer to some questions
          and a way to dodge others, and the coach is the one that knows which — so the
          affordance follows `allowNone` rather than always being there.
        */}
        {question.allowNone ? (
          <Button
            size="sm"
            variant="outline"
            disabled={disabled}
            onClick={() => onAnswer(null)}
          >
            None of these
          </Button>
        ) : null}
      </div>
    </div>
  );
}
