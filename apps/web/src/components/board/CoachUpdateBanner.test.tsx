/**
 * The "Updated by your coach" banner: which runs it announces, and how it says it.
 *
 * The wording and the filtering are pure functions (`coach-update.ts`), so most of this is
 * assertable without a render — which is the point of the split. The render tests cover
 * the two things that are not: that an undone run stays visible, and that undo is offered
 * exactly once per run that has not been undone.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { bannerRuns, describeRun } from '@/components/board/coach-update';
import { CoachUpdateBanner } from '@/components/board/CoachUpdateBanner';
import type { AutonomousRun, RunChange } from '@/lib/schemas';
import { makeTask } from '@/test/factories';

let counter = 0;

function makeRun(overrides: Partial<AutonomousRun> = {}): AutonomousRun {
  counter += 1;
  return {
    id: `r_${counter}`,
    ownerUid: 'u_alice',
    projectId: 'p_1',
    taskId: null,
    trigger: 'scheduled',
    mode: 'queued',
    status: 'complete',
    attempts: 1,
    maxAttempts: 3,
    steps: [],
    turnId: null,
    sessionId: null,
    changes: [],
    undoneAt: null,
    createdAt: null,
    updatedAt: null,
    error: null,
    ...overrides,
  };
}

function created(taskId: string): RunChange {
  return { kind: 'task_created', taskId, previousOrder: null };
}

function reordered(taskId: string): RunChange {
  return { kind: 'task_reordered', taskId, previousOrder: 'a1' };
}

describe('bannerRuns', () => {
  it('announces only runs that changed something', () => {
    /*
      The common outcome of a run is a report and no board change at all — `propose_tasks`
      is explicitly told that adding nothing is a good result. A banner that announced
      every run is one people learn to dismiss without reading.
    */
    const quiet = makeRun();
    const noisy = makeRun({ changes: [created('k_1')] });

    expect(bannerRuns([quiet, noisy])).toEqual([noisy]);
  });

  it('stops at three, so a banner stays a banner', () => {
    const runs = Array.from({ length: 5 }, () => makeRun({ changes: [created('k_1')] }));

    expect(bannerRuns(runs)).toHaveLength(3);
  });
});

describe('describeRun', () => {
  it('names the tasks by their current titles', () => {
    /*
      Looked up on the board rather than read off the change: a task the learner has since
      renamed should read by its current name. The ledger is a record of what happened, not
      a cache of how things were labelled at the time.
    */
    const task = makeTask({ id: 'k_1', title: 'Set up a virtualenv' });
    const run = makeRun({ changes: [created('k_1')] });

    expect(describeRun(run, new Map([[task.id, task]]))).toBe('added “Set up a virtualenv”');
  });

  it('falls back to a count when the task is not on the board', () => {
    const run = makeRun({ changes: [created('k_gone'), created('k_also_gone')] });

    expect(describeRun(run, new Map())).toBe('added 2 tasks');
  });

  it('joins a creation and a reorder into one sentence', () => {
    const added = makeTask({ id: 'k_1', title: 'Virtualenv' });
    const moved = makeTask({ id: 'k_2', title: 'Structured concurrency' });
    const run = makeRun({ changes: [created('k_1'), reordered('k_2')] });

    expect(
      describeRun(
        run,
        new Map([
          [added.id, added],
          [moved.id, moved],
        ]),
      ),
    ).toBe('added “Virtualenv” and moved “Structured concurrency” to the top');
  });
});

describe('CoachUpdateBanner', () => {
  it('renders nothing when no run changed anything', () => {
    const { container } = render(
      <CoachUpdateBanner runs={[makeRun()]} tasks={[]} onUndo={vi.fn()} undoing={false} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it('offers undo for a run and calls back with its id', async () => {
    const task = makeTask({ id: 'k_1', title: 'Virtualenv' });
    const run = makeRun({ changes: [created('k_1')] });
    const onUndo = vi.fn();
    render(<CoachUpdateBanner runs={[run]} tasks={[task]} onUndo={onUndo} undoing={false} />);

    await userEvent.click(screen.getByTestId('undo-run'));

    expect(onUndo).toHaveBeenCalledWith(run.id);
  });

  it('keeps an undone run on screen instead of removing it', () => {
    /*
      Making it vanish would leave someone who mis-clicked with no evidence of what they
      just reversed — and no way to tell "undone" from "the banner refreshed".
    */
    const task = makeTask({ id: 'k_1', title: 'Virtualenv' });
    const run = makeRun({ changes: [created('k_1')], undoneAt: '2026-08-20T10:00:00Z' });
    render(<CoachUpdateBanner runs={[run]} tasks={[task]} onUndo={vi.fn()} undoing={false} />);

    expect(screen.getByTestId('coach-update-line')).toBeVisible();
    expect(screen.getByText('Undone')).toBeVisible();
    expect(screen.queryByTestId('undo-run')).toBeNull();
  });
});
