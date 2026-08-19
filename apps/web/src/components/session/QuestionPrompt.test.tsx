/**
 * The choice dialog `ask_learner` puts in the transcript.
 *
 * The interesting assertions are about what the *tool* asked for being honoured: a
 * single-select that lets you pick two, or a "none of these" button on a question that
 * did not offer one, would each be the coach's own instruction quietly ignored by the UI.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { QuestionPrompt } from '@/components/session/QuestionPrompt';
import type { ConfirmationQuestion } from '@/lib/transcript';

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

    await userEvent.click(screen.getByLabelText('The parser'));
    await userEvent.click(screen.getByLabelText('The evaluator'));
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
