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
import { makeParent, makeTask, makeTaskItem } from '@/test/factories';

function renderList(
  subtasks = SUBTASKS,
  onSetState = vi.fn(),
  onToggleItem = vi.fn(),
  onAdd = vi.fn(),
) {
  const parent = makeParent({}, subtasks);
  render(
    <SubtaskList
      subtasks={parent.subtasks}
      rollup={parent.rollup}
      onSetState={onSetState}
      onToggleItem={onToggleItem}
      onAdd={onAdd}
      hasItems={false}
    />,
  );
  return { onSetState, onToggleItem, onAdd };
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

  it('offers the add control on a leaf task, with no cards', async () => {
    // A leaf still renders the section, because adding the first subtask is how a task
    // *becomes* composite — and since `POST /api/tasks/{id}/split` was removed this form is
    // the only hand path to a subtask there is.
    const onAdd = vi.fn();
    renderList([], vi.fn(), vi.fn(), onAdd);

    expect(screen.queryByTestId('subtask-card')).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('New subtask'), 'The parser');
    await userEvent.click(screen.getByRole('button', { name: 'Add subtask' }));
    expect(onAdd).toHaveBeenCalledWith('The parser', 45);
  });

  it('warns that a first subtask will take the checklist', () => {
    // The consequence the learner cannot see from the form: their steps move. Said before
    // the click rather than reported after it.
    render(
      <SubtaskList
        subtasks={[]}
        rollup={null}
        onSetState={vi.fn()}
        onToggleItem={vi.fn()}
        onAdd={vi.fn()}
        hasItems
      />,
    );
    expect(screen.getByText(/moves this task’s steps onto it/)).toBeInTheDocument();
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

describe('a subtask’s own checklist', () => {
  /**
   * A subtask has no route (docs/06-frontend.md), which has to mean "reachable from the
   * parent" rather than "unreachable". It holds items exactly as a leaf task does — the
   * first subtask inherits the parent's — so without these the coach could plan work the
   * learner had no way to see, let alone tick off.
   */
  const WITH_ITEMS = [
    makeTask({
      id: 'k_child',
      title: 'The parser',
      state: 'not_started',
      items: [
        makeTaskItem({ itemId: 'i_a', shortDescription: 'Read the grammar' }),
        makeTaskItem({ itemId: 'i_b', shortDescription: 'Write the tokenizer', completed: true }),
      ],
    }),
  ];

  it('shows the subtask’s steps inside its card', () => {
    renderList(WITH_ITEMS);
    const card = screen.getByTestId('subtask-card');
    expect(within(card).getByText('Read the grammar')).toBeInTheDocument();
    expect(within(card).getByText('Steps for this subtask')).toBeInTheDocument();
  });

  it('reports a tick against the subtask, not the parent', async () => {
    // The whole reason this needs its own mutation: the write is against a different task
    // from the one the workspace is keyed on.
    const { onToggleItem } = renderList(WITH_ITEMS);
    await userEvent.click(screen.getByRole('checkbox', { name: 'Read the grammar' }));
    expect(onToggleItem).toHaveBeenCalledWith('k_child', 'i_a', true);
  });

  it('gives each checklist its own heading id', () => {
    // Several checklists share one screen — the task's own and one per subtask — and
    // `aria-labelledby` pointing at a duplicated id names whichever came first, which is
    // the wrong section for every checklist but one.
    renderList([
      ...WITH_ITEMS,
      makeTask({
        id: 'k_second',
        title: 'The evaluator',
        items: [makeTaskItem({ itemId: 'i_c', shortDescription: 'Read about environments' })],
      }),
    ]);
    const ids = screen
      .getAllByTestId('checklist')
      .map((section) => section.getAttribute('aria-labelledby'));
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('renders no checklist for a subtask that has none', () => {
    renderList();
    expect(screen.queryByTestId('checklist')).not.toBeInTheDocument();
  });
});
