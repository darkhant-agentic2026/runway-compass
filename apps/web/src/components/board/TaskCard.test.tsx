/**
 * Board card rendering.
 *
 * docs/08-testing.md: "**Board filters and rollups** — hide-completed default on; parent
 * card renders '4 subtasks · 2 h 30 m' from `rollup`."
 */

import { DndContext } from '@dnd-kit/core';
import { SortableContext } from '@dnd-kit/sortable';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { transitionsFor } from '@/components/board/task-state';
import { TaskCard } from '@/components/board/TaskCard';
import type { TaskWithSubtasks } from '@/lib/schemas';
import { DEFAULT_FILTERS } from '@/stores/boardUi';
import { makeParent, makeTask } from '@/test/factories';

function renderCard(
  task: TaskWithSubtasks,
  overrides: Partial<Parameters<typeof TaskCard>[0]> = {},
) {
  const props = {
    task,
    isNextUp: false,
    collapsed: false,
    canMoveUp: true,
    canMoveDown: true,
    dragDisabled: false,
    onToggleCollapsed: vi.fn(),
    onSetState: vi.fn(),
    onMove: vi.fn(),
    onSplit: vi.fn(),
    ...overrides,
  };
  render(
    <DndContext>
      <SortableContext items={[task.id]}>
        <ul>
          <TaskCard {...props} />
        </ul>
      </SortableContext>
    </DndContext>,
  );
  return props;
}

describe('parent cards', () => {
  it('renders the rollup as "4 subtasks · 2 h 30 m"', () => {
    const subtasks = [
      makeTask({ estimatedMinutes: 45 }),
      makeTask({ estimatedMinutes: 45 }),
      makeTask({ estimatedMinutes: 30 }),
      makeTask({ estimatedMinutes: 30 }),
    ];
    const parent = makeParent({ title: 'Big thing' }, subtasks);

    renderCard(parent);

    expect(screen.getByTestId('rollup')).toHaveTextContent('4 subtasks · 2 h 30 m');
  });

  it('exposes completion progress as text, not colour alone', () => {
    const subtasks = [
      makeTask({ estimatedMinutes: 30, state: 'completed' }),
      makeTask({ estimatedMinutes: 30 }),
    ];
    renderCard(makeParent({}, subtasks));

    expect(screen.getByRole('img', { name: '1 of 2 subtasks complete' })).toBeInTheDocument();
  });

  it('renders subtasks inline and can be collapsed', async () => {
    const subtasks = [makeTask({ title: 'First half' }), makeTask({ title: 'Second half' })];
    const parent = makeParent({ title: 'Split me' }, subtasks);

    const props = renderCard(parent);
    expect(within(screen.getByTestId('subtask-list')).getAllByTestId('subtask')).toHaveLength(
      2,
    );

    await userEvent.click(screen.getByRole('button', { name: 'Collapse Split me' }));
    expect(props.onToggleCollapsed).toHaveBeenCalled();
  });

  it('hides the subtask list when collapsed', () => {
    const parent = makeParent({ title: 'Split me' }, [makeTask(), makeTask()]);
    renderCard(parent, { collapsed: true });
    expect(screen.queryByTestId('subtask-list')).not.toBeInTheDocument();
  });

  it('shows no rollup for a leaf task', () => {
    renderCard(makeParent({ title: 'Leaf' }));
    expect(screen.queryByTestId('rollup')).not.toBeInTheDocument();
  });
});

describe('card chrome', () => {
  it('shows the duration chip and state badge', () => {
    renderCard(makeParent({ estimatedMinutes: 90, state: 'postponed' }));
    expect(screen.getByTestId('estimate')).toHaveTextContent('1 h 30 m');
    expect(screen.getByTestId('state-badge')).toHaveTextContent('Postponed');
  });

  it('badges a task the coach created', () => {
    renderCard(makeParent({ origin: 'agent' }));
    expect(screen.getByText('From your coach')).toBeInTheDocument();
  });

  it('marks the next-up task', () => {
    renderCard(makeParent({ state: 'current' }), { isNextUp: true });
    expect(screen.getByText('Next up')).toBeInTheDocument();
  });

  it('shows a materials-ready indicator only once research is done', () => {
    renderCard(makeParent({ researchStatus: 'done' }));
    expect(screen.getByTestId('materials-ready')).toBeInTheDocument();
  });
});

describe('row actions offer only legal transitions', () => {
  it('a not-started task can be started or discarded, but not completed', async () => {
    renderCard(makeParent({ title: 'Fresh', state: 'not_started' }));
    await userEvent.click(screen.getByRole('button', { name: 'Actions for Fresh' }));

    expect(await screen.findByRole('menuitem', { name: 'Start' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Discard' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: 'Complete' })).not.toBeInTheDocument();
  });

  it('a current task can be completed and postponed', async () => {
    renderCard(makeParent({ title: 'Doing', state: 'current' }));
    await userEvent.click(screen.getByRole('button', { name: 'Actions for Doing' }));

    expect(await screen.findByRole('menuitem', { name: 'Complete' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Postpone' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Postpone until…' })).toBeInTheDocument();
  });

  it('offers the keyboard fallback for drag-and-drop', async () => {
    // docs/06-frontend.md requires this: a pointer-only reorder is unusable without a
    // mouse, so the row menu carries move up / move down.
    const props = renderCard(makeParent({ title: 'Movable' }));
    await userEvent.click(screen.getByRole('button', { name: 'Actions for Movable' }));
    await userEvent.click(await screen.findByRole('menuitem', { name: 'Move up' }));

    expect(props.onMove).toHaveBeenCalledWith(expect.any(String), -1);
  });

  it('disables the move actions at the ends of the list', async () => {
    renderCard(makeParent({ title: 'Only' }), { canMoveUp: false, canMoveDown: false });
    await userEvent.click(screen.getByRole('button', { name: 'Actions for Only' }));

    expect(await screen.findByRole('menuitem', { name: 'Move up' })).toHaveAttribute(
      'data-disabled',
    );
    expect(screen.getByRole('menuitem', { name: 'Move down' })).toHaveAttribute(
      'data-disabled',
    );
  });

  it('a discarded task offers restore and nothing destructive', () => {
    const options = transitionsFor('discarded');
    expect(options.map((option) => option.transition)).toEqual(['restore']);
  });
});

describe('board filter defaults', () => {
  it('hides completed and discarded by default, and shows postponed', () => {
    expect(DEFAULT_FILTERS).toEqual({
      hideCompleted: true,
      hideDiscarded: true,
      hidePostponed: false,
    });
  });
});
