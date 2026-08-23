/**
 * The report block.
 *
 * docs/08-testing.md: "the checklist and optional blocks are distinct landmarks; optional
 * items render no completion checkbox". That last one is a product requirement — the two
 * lists mean different things to the learner and the affordance is what says so — and it
 * gets an explicit regression test rather than resting on the fact that the components
 * happen to be separate today.
 */

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Checklist } from '@/components/task/Checklist';
import { ResearchReport } from '@/components/task/ResearchReport';
import { makeReport, makeReportItem, makeTaskItem } from '@/test/factories';

describe('ResearchReport', () => {
  it('the checklist and the optional block are separate landmarks', () => {
    render(
      <>
        <Checklist
          items={[makeTaskItem({ shortDescription: 'Read the guide' })]}
          budgetMinutes={45}
          onToggle={vi.fn()}
        />
        <ResearchReport
          report={makeReport({ optional: [makeReportItem({ title: 'A deeper treatment' })] })}
        />
      </>,
    );

    const checklist = screen.getByTestId('checklist');
    const optional = screen.getByTestId('report-optional');
    expect(within(checklist).getByText('To complete this task')).toBeInTheDocument();
    expect(
      within(optional).getByText('Optional, if you want to go deeper'),
    ).toBeInTheDocument();
    expect(checklist).not.toContainElement(optional);
  });

  it('optional items have no completion checkbox', () => {
    render(
      <ResearchReport
        report={makeReport({
          optional: [makeReportItem({ title: 'A deeper treatment', minutes: 30 })],
        })}
      />,
    );

    const optional = screen.getByTestId('report-optional');
    expect(within(optional).getByText('A deeper treatment')).toBeInTheDocument();
    expect(within(optional).queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('the thumbs control reports the item and the new value, and toggles off', async () => {
    const onFeedback = vi.fn();
    const item = makeReportItem({ itemId: 'ri_1' });
    render(
      <ResearchReport
        report={makeReport({ optional: [item], progress: { feedback: { ri_1: 'down' } } })}
        onFeedback={onFeedback}
      />,
    );

    // Pressed already, so clicking it clears rather than re-setting: a thumbs-down is a
    // judgement the learner can take back.
    await userEvent.click(screen.getByRole('button', { name: 'This was not useful' }));
    expect(onFeedback).toHaveBeenCalledWith('ri_1', null);

    await userEvent.click(screen.getByRole('button', { name: 'This was useful' }));
    expect(onFeedback).toHaveBeenCalledWith('ri_1', 'up');
  });

  it('renders required items too, read-only — since M8 this is the research view, not the task workspace', () => {
    render(
      <ResearchReport
        report={makeReport({
          required: [makeReportItem({ itemId: 'ri_req', title: 'Read the guide' })],
        })}
      />,
    );

    const required = screen.getByTestId('report-required');
    expect(within(required).getByText('Read the guide')).toBeInTheDocument();
    expect(within(required).queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('a project-scoped report (no taskId) labels the required block as answering the question', () => {
    render(
      <ResearchReport
        report={makeReport({
          taskId: null,
          required: [makeReportItem({ title: 'The framework comparison' })],
        })}
      />,
    );

    expect(
      within(screen.getByTestId('report-required')).getByText('What answers the question'),
    ).toBeInTheDocument();
  });

  it('renders citations as links', () => {
    render(
      <ResearchReport
        report={makeReport({
          citations: [{ uri: 'https://example.com/source', title: 'The source' }],
        })}
      />,
    );
    expect(screen.getByRole('link', { name: 'The source' })).toHaveAttribute(
      'href',
      'https://example.com/source',
    );
  });
});
