/**
 * Selection status is always visible (the decision chip and the `why`, even collapsed);
 * everything else — the proposed material — is behind a click, and the card as a whole
 * reads as muted for a task that did not make the plan. Those are the product requirements
 * this file pins, not the general rendering the story around it would already cover.
 */

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ProposedTaskCard } from '@/components/research/ProposedTaskCard';
import { makePlanTaskEntry, makeProposedItem, makeProposedTask } from '@/test/factories';

describe('ProposedTaskCard', () => {
  it('shows the decision as a chip, and the why, without expanding', () => {
    render(
      <ul>
        <ProposedTaskCard
          task={makeProposedTask({ slug: 'stub-task', title: 'A stub proposed task' })}
          entry={makePlanTaskEntry({
            taskSlug: 'stub-task',
            decision: 'exclude',
            why: 'Already covered by an earlier task.',
          })}
          titleBySlug={{}}
        />
      </ul>,
    );

    const card = screen.getByTestId('proposed-task');
    expect(within(card).getByTestId('decision-chip')).toHaveTextContent('Not included');
    expect(within(card).getByTestId('decision-why')).toHaveTextContent(
      'Already covered by an earlier task.',
    );
  });

  it('mutes an excluded/rejected task, not an included one', () => {
    render(
      <ul>
        <ProposedTaskCard
          task={makeProposedTask({ slug: 'a' })}
          entry={makePlanTaskEntry({ taskSlug: 'a', decision: 'include' })}
          titleBySlug={{}}
        />
        <ProposedTaskCard
          task={makeProposedTask({ slug: 'b' })}
          entry={makePlanTaskEntry({ taskSlug: 'b', decision: 'reject' })}
          titleBySlug={{}}
        />
      </ul>,
    );

    const [included, rejected] = screen.getAllByTestId('proposed-task');
    expect(included).not.toHaveClass('opacity-70');
    expect(rejected).toHaveClass('opacity-70');
  });

  it('keeps the required/optional material collapsed until the card is expanded', async () => {
    render(
      <ul>
        <ProposedTaskCard
          task={makeProposedTask({
            slug: 'stub-task',
            required: [makeProposedItem({ title: 'A stub finding' })],
          })}
          entry={makePlanTaskEntry({ taskSlug: 'stub-task' })}
          titleBySlug={{}}
        />
      </ul>,
    );

    expect(screen.queryByText('A stub finding')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /expand/i }));
    expect(screen.getByText('A stub finding')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /collapse/i }));
    expect(screen.queryByText('A stub finding')).not.toBeInTheDocument();
  });

  it('shows the required minutes and a kind-icon summary below the title', () => {
    render(
      <ul>
        <ProposedTaskCard
          task={makeProposedTask({
            slug: 'stub-task',
            required: [
              makeProposedItem({ kind: 'article', minutes: 10 }),
              makeProposedItem({ kind: 'article', minutes: 15 }),
              makeProposedItem({ kind: 'exercise', minutes: 20 }),
            ],
          })}
          entry={makePlanTaskEntry({ taskSlug: 'stub-task' })}
          titleBySlug={{}}
        />
      </ul>,
    );

    const strip = screen.getByTestId('item-kind-strip');
    expect(strip).toHaveTextContent('45 min');
    expect(within(strip).getByTestId('item-kind-summary')).toHaveTextContent('2x');
  });

  it('counts optional items in the kind chip too, but not in the duration', () => {
    // A task whose material is entirely optional (a deep dive with nothing required)
    // must still show a kind chip on the collapsed card — otherwise it reads as empty
    // rather than as "click to see the optional material".
    render(
      <ul>
        <ProposedTaskCard
          task={makeProposedTask({
            slug: 'stub-task',
            required: [],
            optional: [makeProposedItem({ kind: 'video', minutes: 30 })],
          })}
          entry={makePlanTaskEntry({ taskSlug: 'stub-task' })}
          titleBySlug={{}}
        />
      </ul>,
    );

    const strip = screen.getByTestId('item-kind-strip');
    expect(strip).not.toHaveTextContent('30 min');
    // All-optional material renders as the muted, dotted-underline group on its own — no
    // required group and no "+" separator, since there is nothing to separate it from.
    expect(within(strip).queryByTestId('item-kind-required')).not.toBeInTheDocument();
    expect(within(strip).getByTestId('item-kind-optional')).toHaveTextContent('1x');
  });

  it('names prerequisites by title, not slug, once expanded', async () => {
    render(
      <ul>
        <ProposedTaskCard
          task={makeProposedTask({ slug: 'b', title: 'Second task' })}
          entry={makePlanTaskEntry({ taskSlug: 'b', after: 'a', prerequisiteTasks: ['a'] })}
          titleBySlug={{ a: 'First task' }}
        />
      </ul>,
    );

    await userEvent.click(screen.getByRole('button', { name: /expand/i }));
    expect(screen.getByText('First task')).toBeInTheDocument();
    expect(screen.queryByText('a')).not.toBeInTheDocument();
  });
});
