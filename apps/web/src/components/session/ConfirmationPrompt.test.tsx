/**
 * The roadmap-brief approval dialog: rendered from the project's own stored document,
 * and its attachment checklist — every file the conversation has, not only the ones the
 * model already thought to reference, so the learner can correct the selection and
 * approve in one step (see `agents/tools.py`'s `CONFIRMED_ATTACHMENTS_KEY`).
 */

import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import {
  ConfirmationPrompt,
  CONFIRMED_ATTACHMENTS_KEY,
} from '@/components/session/ConfirmationPrompt';
import { createQueryClient, queryKeys } from '@/features/queries';
import type { TranscriptMessage } from '@/lib/transcript';
import { makeProject } from '@/test/factories';

function message(
  seq: number,
  attachments: TranscriptMessage['attachments'] = [],
): TranscriptMessage {
  return {
    id: `e_${seq}`,
    seq,
    role: 'user',
    author: 'user',
    text: '',
    tools: [],
    attachments,
  };
}

function renderConfirmation({
  projectOverrides = {},
  messages = [],
  toolName = 'propose_roadmap_brief',
  args = {},
}: {
  projectOverrides?: Parameters<typeof makeProject>[0];
  messages?: TranscriptMessage[];
  toolName?: string;
  args?: Record<string, unknown>;
}) {
  const queryClient = createQueryClient();
  const project = makeProject(projectOverrides);
  queryClient.setQueryData(queryKeys.project(project.id), project);
  const onAnswer = vi.fn();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  render(
    <ConfirmationPrompt
      pending={{ functionCallId: 'call-1', toolName, args, question: null }}
      projectId={project.id}
      sessionId="s_1"
      messages={messages}
      disabled={false}
      onAnswer={onAnswer}
    />,
    { wrapper },
  );
  return { onAnswer, project };
}

describe('the roadmap brief approval dialog', () => {
  it('renders the stored brief document, not the confirmation call’s own arguments', () => {
    renderConfirmation({
      projectOverrides: {
        roadmapBrief: {
          subject: 'Structured concurrency',
          specificTopics: ['cancellation'],
          timeBudget: 'two months',
          additionalNotes: '',
          attachments: [],
          updatedAt: null,
        },
      },
      // A mismatched restatement — the dialog must not show this.
      args: { subject: 'Something else entirely', time_budget: 'one week' },
    });

    expect(screen.getByText('Structured concurrency')).toBeInTheDocument();
    expect(screen.getByText(/two months/)).toBeInTheDocument();
    expect(screen.queryByText('Something else entirely')).not.toBeInTheDocument();
    expect(screen.queryByText(/one week/)).not.toBeInTheDocument();
  });

  it('offers no attachment checklist when the conversation has no attachments', () => {
    renderConfirmation({
      projectOverrides: {
        roadmapBrief: {
          subject: 'X',
          specificTopics: [],
          timeBudget: 'a month',
          additionalNotes: '',
          attachments: [],
          updatedAt: null,
        },
      },
      messages: [],
    });

    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('checks every session attachment the brief already selected, unchecked otherwise', () => {
    renderConfirmation({
      projectOverrides: {
        roadmapBrief: {
          subject: 'X',
          specificTopics: [],
          timeBudget: 'a month',
          additionalNotes: '',
          attachments: ['syllabus.pdf'],
          updatedAt: null,
        },
      },
      messages: [
        message(1, [{ mimeType: 'application/pdf', filename: 'syllabus.pdf' }]),
        message(2, [{ mimeType: 'application/pdf', filename: 'unrelated.pdf' }]),
      ],
    });

    expect(screen.getByRole('checkbox', { name: 'syllabus.pdf' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'unrelated.pdf' })).not.toBeChecked();
  });

  it('sends the learner’s edited selection, not the brief’s original one, on approval', async () => {
    const { onAnswer } = renderConfirmation({
      projectOverrides: {
        roadmapBrief: {
          subject: 'X',
          specificTopics: [],
          timeBudget: 'a month',
          additionalNotes: '',
          attachments: ['syllabus.pdf'],
          updatedAt: null,
        },
      },
      messages: [
        message(1, [{ mimeType: 'application/pdf', filename: 'syllabus.pdf' }]),
        message(2, [{ mimeType: 'application/pdf', filename: 'unrelated.pdf' }]),
      ],
    });

    // Untick the one the brief had, tick the one it did not.
    await userEvent.click(screen.getByRole('checkbox', { name: 'syllabus.pdf' }));
    await userEvent.click(screen.getByRole('checkbox', { name: 'unrelated.pdf' }));
    await userEvent.click(screen.getByRole('button', { name: 'Accept plan' }));

    expect(onAnswer).toHaveBeenCalledWith(true, {
      [CONFIRMED_ATTACHMENTS_KEY]: ['unrelated.pdf'],
    });
  });
});
