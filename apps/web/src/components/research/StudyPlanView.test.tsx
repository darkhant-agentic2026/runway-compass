import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { StudyPlanView } from '@/components/research/StudyPlanView';
import { makePlanTaskEntry, makeProposedTask, makeStudyPlan } from '@/test/factories';

describe('StudyPlanView', () => {
  it('renders the plan title, description, and one card per proposed task', () => {
    render(
      <StudyPlanView
        plan={makeStudyPlan({
          title: 'Becoming a data engineer',
          shortDescription: 'A roadmap for your goal.',
          proposedTasks: [
            makeProposedTask({ slug: 'a', title: 'Learn SQL' }),
            makeProposedTask({ slug: 'b', title: 'Learn a warehouse' }),
          ],
          plan: [
            makePlanTaskEntry({ taskSlug: 'a', decision: 'include' }),
            makePlanTaskEntry({ taskSlug: 'b', decision: 'reject' }),
          ],
        })}
      />,
    );

    expect(
      screen.getByRole('heading', { name: 'Becoming a data engineer' }),
    ).toBeInTheDocument();
    expect(screen.getByText('A roadmap for your goal.')).toBeInTheDocument();
    expect(screen.getAllByTestId('proposed-task')).toHaveLength(2);
  });

  it('tallies included vs. left-out tasks in the plan order', () => {
    render(
      <StudyPlanView
        plan={makeStudyPlan({
          proposedTasks: [
            makeProposedTask({ slug: 'a' }),
            makeProposedTask({ slug: 'b' }),
            makeProposedTask({ slug: 'c' }),
          ],
          plan: [
            makePlanTaskEntry({ taskSlug: 'a', decision: 'include' }),
            makePlanTaskEntry({ taskSlug: 'b', decision: 'additional' }),
            makePlanTaskEntry({ taskSlug: 'c', decision: 'exclude' }),
          ],
        })}
      />,
    );

    expect(screen.getByTestId('study-plan-tally')).toHaveTextContent(
      '2 tasks in your plan · 1 task your coach left out',
    );
  });

  it('folds the memo behind a disclosure, rendered as markdown once opened', async () => {
    render(<StudyPlanView plan={makeStudyPlan({ memo: '**A** closing note.' })} />);

    const toggle = screen.getByRole('button', { name: 'Task composer’s memo' });
    expect(screen.queryByTestId('plan-memo')).not.toBeInTheDocument();

    await userEvent.click(toggle);
    const memo = screen.getByTestId('plan-memo');
    expect(memo).toHaveTextContent('A closing note.');
    expect(memo.querySelector('strong')).toHaveTextContent('A');

    await userEvent.click(toggle);
    expect(screen.queryByTestId('plan-memo')).not.toBeInTheDocument();
  });

  it('shows no memo disclosure when the coach left none', () => {
    render(<StudyPlanView plan={makeStudyPlan({ memo: '' })} />);
    expect(
      screen.queryByRole('button', { name: 'Task composer’s memo' }),
    ).not.toBeInTheDocument();
  });
});
