/**
 * The choice dialog `ask_learner` puts in the transcript.
 *
 * The interesting assertions are about what the *tool* asked for being honoured: a
 * single-select that lets you pick two, or a "none of these" button on a question that
 * did not offer one, would each be the coach's own instruction quietly ignored by the UI.
 */

import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps, ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { ConfirmationPrompt } from '@/components/session/ConfirmationPrompt';
import { QuestionPrompt } from '@/components/session/QuestionPrompt';
import { createQueryClient } from '@/features/queries';
import type { ConfirmationQuestion } from '@/lib/transcript';

function renderConfirmation(
  props: Omit<
    ComponentProps<typeof ConfirmationPrompt>,
    'projectId' | 'sessionId' | 'messages'
  >,
) {
  const queryClient = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(
    <ConfirmationPrompt projectId="p_1" sessionId="s_1" messages={[]} {...props} />,
    { wrapper },
  );
}

function question(overrides: Partial<ConfirmationQuestion> = {}): ConfirmationQuestion {
  return {
    question: 'Which should come first?',
    options: ['The parser', 'The lexer', 'The evaluator'],
    allowMultiple: false,
    allowNone: false,
    notePrompt: '',
    ...overrides,
  };
}

describe('QuestionPrompt', () => {
  it('sends exactly one selection when the tool asked for one', async () => {
    const onAnswer = vi.fn();
    render(<QuestionPrompt question={question()} disabled={false} onAnswer={onAnswer} />);

    await userEvent.click(screen.getByLabelText('The lexer'));
    await userEvent.click(screen.getByLabelText('The parser'));
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));

    // The second click *replaces* the first. A radio group is what the tool asked for.
    expect(onAnswer).toHaveBeenCalledWith({ selected: ['The parser'], note: '' });
  });

  it('accumulates selections when the tool asked for several', async () => {
    const onAnswer = vi.fn();
    render(
      <QuestionPrompt
        question={question({ allowMultiple: true })}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    // By role rather than by label. The design system's checkbox is a styled button beside
    // a visually-hidden native input, and both carry the option's text — the button through
    // `aria-label`, the input through `<Label htmlFor>` — so a label lookup is ambiguous.
    // The role names the control the learner actually clicks.
    await userEvent.click(screen.getByRole('checkbox', { name: 'The parser' }));
    await userEvent.click(screen.getByRole('checkbox', { name: 'The evaluator' }));
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(onAnswer).toHaveBeenCalledWith({
      selected: ['The parser', 'The evaluator'],
      note: '',
    });
  });

  it('carries a note when the tool asked for one', async () => {
    const onAnswer = vi.fn();
    render(
      <QuestionPrompt
        question={question({ notePrompt: 'Anything else I should know?' })}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );

    await userEvent.click(screen.getByLabelText('The parser'));
    await userEvent.type(
      screen.getByLabelText('Anything else I should know?'),
      'I have done lexing before',
    );
    await userEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(onAnswer).toHaveBeenCalledWith({
      selected: ['The parser'],
      note: 'I have done lexing before',
    });
  });

  it('offers no note box unless the tool asked for one', () => {
    render(<QuestionPrompt question={question()} disabled={false} onAnswer={vi.fn()} />);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('offers "none of these" only when the tool said it was a real answer', async () => {
    const onAnswer = vi.fn();
    const { rerender } = render(
      <QuestionPrompt question={question()} disabled={false} onAnswer={onAnswer} />,
    );
    // Some questions have no honest "none": offering one there is a way to dodge, and the
    // coach is what knows which kind it asked.
    expect(screen.queryByRole('button', { name: 'None of these' })).not.toBeInTheDocument();

    rerender(
      <QuestionPrompt
        question={question({ allowNone: true })}
        disabled={false}
        onAnswer={onAnswer}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'None of these' }));
    // `null`, which `SessionPane` sends as `confirmed: false` — ADK's own word for a
    // declined confirmation, and what `ask_learner` reads as "none of these".
    expect(onAnswer).toHaveBeenCalledWith(null);
  });

  it('cannot be sent empty', async () => {
    render(<QuestionPrompt question={question()} disabled={false} onAnswer={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();

    await userEvent.click(screen.getByLabelText('The parser'));
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('is inert while a turn is in flight', () => {
    render(<QuestionPrompt question={question()} disabled onAnswer={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(screen.getByLabelText('The parser')).toBeDisabled();
  });
});

describe('ConfirmationPrompt’s third option', () => {
  /**
   * "Mark it done and stop asking in this project."
   *
   * The setting is offered in the dialog as well as in project settings because the moment
   * the friction becomes obvious is the moment you are looking at it. It rides in the
   * answer's payload rather than being a second request, so one click is one round trip and
   * the preference cannot land without the completion it was attached to.
   */
  const pending = {
    functionCallId: 'adk-1',
    toolName: 'complete_task_item',
    args: { note: 'you talked me through it' },
    question: null,
  };

  it('answers and asks to stop confirming, in one payload', async () => {
    const onAnswer = vi.fn();
    renderConfirmation({ pending, disabled: false, onAnswer });

    await userEvent.click(
      screen.getByRole('button', { name: 'Mark it done and stop asking in this project' }),
    );
    expect(onAnswer).toHaveBeenCalledWith(true, { stopConfirming: true });
  });

  it('plain approval carries no payload', async () => {
    const onAnswer = vi.fn();
    renderConfirmation({ pending, disabled: false, onAnswer });

    await userEvent.click(screen.getByRole('button', { name: 'Mark it done' }));
    expect(onAnswer).toHaveBeenCalledWith(true);
  });

  it('declines with wording that fits a completion', async () => {
    // "Keep it" is the safe answer to a *discard*; the safe answer to a completion is that
    // they have not finished yet.
    const onAnswer = vi.fn();
    renderConfirmation({ pending, disabled: false, onAnswer });

    await userEvent.click(screen.getByRole('button', { name: 'Not yet' }));
    expect(onAnswer).toHaveBeenCalledWith(false);
  });

  it('offers no opt-out on the destructive gates', () => {
    // A learner who silenced completions did not ask to silence discarding.
    renderConfirmation({
      pending: { ...pending, toolName: 'discard_task', args: { reason: 'obsolete' } },
      disabled: false,
      onAnswer: vi.fn(),
    });
    expect(screen.getByRole('button', { name: 'Keep it' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /stop asking/ })).not.toBeInTheDocument();
  });
});
