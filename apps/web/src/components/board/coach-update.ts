/**
 * What the "Updated by your coach" banner says, and which runs it says it about.
 *
 * Separate from the component for the same reason `task-state.ts` is: these are pure
 * functions over the ledger, so a test can assert the wording without a render — and a
 * module that exports both a component and a constant breaks Fast Refresh.
 */

import type { AutonomousRun, Task } from '@/lib/schemas';

/** How many runs the banner shows before it stops being a banner. */
export const MAX_BANNER_RUNS = 3;

/**
 * The runs worth announcing.
 *
 * **Only runs that actually changed something.** The common outcome of a run is a report
 * and no board change at all — `propose_tasks` is explicitly told that adding nothing is a
 * good result — and a banner that announced every run would be one people learn to dismiss
 * without reading.
 */
export function bannerRuns(runs: AutonomousRun[]): AutonomousRun[] {
  return runs.filter((run) => run.changes.length > 0).slice(0, MAX_BANNER_RUNS);
}

/**
 * One line per run, in the learner's terms rather than the ledger's.
 *
 * Titles are looked up on the board rather than read off the change: a task the learner has
 * since renamed should read by its current name, and the ledger is a record of *what
 * happened*, not a cache of how things were labelled at the time. A task that is no longer
 * on the board falls back to a count, which is also what happens to a run whose created
 * tasks the board's filters are hiding.
 */
export function describeRun(run: AutonomousRun, byId: Map<string, Task>): string {
  const created = run.changes.filter((change) => change.kind === 'task_created');
  const moved = run.changes.filter((change) => change.kind === 'task_reordered');
  const parts: string[] = [];
  if (created.length > 0) {
    const names = created
      .map((change) => byId.get(change.taskId)?.title)
      .filter((title): title is string => Boolean(title));
    parts.push(
      names.length > 0
        ? `added ${names.map((name) => `“${name}”`).join(', ')}`
        : `added ${created.length} task${created.length === 1 ? '' : 's'}`,
    );
  }
  if (moved.length > 0) {
    const name = byId.get(moved[0]!.taskId)?.title;
    parts.push(name ? `moved “${name}” to the top` : 'reordered the board');
  }
  return parts.join(' and ');
}
