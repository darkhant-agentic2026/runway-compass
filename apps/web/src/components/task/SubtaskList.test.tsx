/**
 * A composite task's subtasks on its own screen.
 *
 * docs/08-testing.md:
 *
 * > **Composite task workspace** — a parent renders one card per subtask with its state
 * > actions, and none of them is a link; a leaf task renders no subtask block at all.
 *
 * The "none of them is a link" assertion is the one carrying a product decision rather
 * than an implementation detail: a subtask deliberately has no workspace of its own
 * (docs/06-frontend.md), and the natural way to lose that is for someone to make the
 * titles clickable because the board's parent titles are.
 */

import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { SubtaskList } from '@/components/task/SubtaskList';
import { makeParent, makeTask } from '@/test/factories';

function renderList(subtasks = SUBTASKS, onSetState = vi.fn()) {
  const parent = makeParent({}, subtasks);
  render(
    <SubtaskList subtasks={parent.subtasks} rollup={parent.rollup} onSetState={onSetState} />,
  );
  return { onSetState };
}

const SUBTASKS = [
  makeTask({ id: 'k_read', title: 'Read the paper', state: 'completed', estimatedMinutes: 30 }),
  makeTask({ id: 'k_notes', title: 'Write notes', state: 'in_progress', estimatedMinutes: 45 }),
  makeTask({ id: 'k_quiz', title: 'Try the exercises', state: 'not_started' }),
];

describe('SubtaskList', () => {
  it('renders a card per subtask, and none of them navigates', () => {
    renderList();

    const cards = screen.getAllByTestId('subtask-card');
    expect(cards).toHaveLength(3);
    expect(cards.map((card) => card.dataset.taskId)).toEqual(['k_read', 'k_notes', 'k_quiz']);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });

  it('renders nothing at all for a leaf task', () => {
    render(<SubtaskList subtasks={[]} rollup={null} onSetState={vi.fn()} />);

    expect(screen.queryByTestId('subtask-cards')).not.toBeInTheDocument();
  });

  it('shows the parent rollup, which is the number the board card shows', () => {
    renderList();

    // 1 of 3 done, 2 h — from `rollup`, not recounted from the rows.
    expect(screen.getByTestId('subtask-rollup')).toHaveTextContent('1 of 3 subtasks done');
    expect(screen.getByTestId('subtask-rollup')).toHaveTextContent('2 h');
  });

  it('offers only the transition that is legal from each state', () => {
    renderList();

    const [read, notes, quiz] = screen.getAllByTestId('subtask-card');
    // Not "Complete" on a subtask nobody has started: `not_started` → `completed` is not
    // a transition, and offering it would produce a 409 from the state machine.
    expect(within(quiz!).getByRole('button', { name: 'Start' })).toBeInTheDocument();
    expect(within(notes!).getByRole('button', { name: 'Complete' })).toBeInTheDocument();
    expect(within(read!).getByRole('button', { name: 'Reopen' })).toBeInTheDocument();
  });

  it('reports the subtask and its target state when the quick action is used', async () => {
    const user = userEvent.setup();
    const { onSetState } = renderList();

    const notes = screen.getAllByTestId('subtask-card')[1]!;
    await user.click(within(notes).getByRole('button', { name: 'Complete' }));

    expect(onSetState).toHaveBeenCalledWith('k_notes', 'completed', undefined);
  });

  it('keeps a discarded subtask on screen, marked as such', () => {
    // `GET /api/tasks/{id}` returns every child, discarded ones included, and this is the
    // only screen from which one can be restored — hiding it would make the row's own
    // "Restore" action unreachable. It is excluded from the rollup by the server, which
    // is why the count below is 1 while three cards are on screen.
    renderList([
      makeTask({ id: 'k_a', title: 'Kept', state: 'not_started', estimatedMinutes: 45 }),
      makeTask({ id: 'k_b', title: 'Dropped', state: 'discarded' }),
    ]);

    const cards = screen.getAllByTestId('subtask-card');
    expect(cards).toHaveLength(2);
    expect(cards[1]).toHaveAttribute('data-state', 'discarded');
    expect(screen.getByTestId('subtask-rollup')).toHaveTextContent('0 of 1 subtask done');
  });
});
