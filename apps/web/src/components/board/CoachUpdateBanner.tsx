/**
 * "Updated by your coach" — what a background run changed, with a one-click undo.
 *
 * docs/05-autonomous-runs.md#what-the-run-is-allowed-to-change:
 *
 * > On the user's next visit the board shows an "Updated by your coach" banner listing
 * > what changed, with a one-click undo that reverses the run's writes.
 *
 * It renders from the ledger's own `changes[]` rather than from a diff of the board,
 * because a diff cannot tell the coach's writes from the learner's own — and by the time
 * anyone reads this, the learner has had the board in front of them.
 *
 * **An undone run stays visible, struck through, for the rest of the visit.** Making it
 * vanish would leave someone who mis-clicked with no evidence of what they just reversed.
 *
 * The wording and the filtering live in `coach-update.ts`, so both are assertable without
 * a render.
 */

import { Undo2 } from 'lucide-react';

import { bannerRuns, describeRun } from '@/components/board/coach-update';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import type { AutonomousRun, Task } from '@/lib/schemas';

export function CoachUpdateBanner({
  runs,
  tasks,
  onUndo,
  undoing,
}: {
  runs: AutonomousRun[];
  tasks: Task[];
  onUndo: (runId: string) => void;
  undoing: boolean;
}) {
  const visible = bannerRuns(runs);
  if (visible.length === 0) return null;
  const byId = new Map(tasks.map((task) => [task.id, task]));

  return (
    <Card className="gap-2 p-3" data-testid="coach-update-banner">
      <h2 className="text-sm font-medium">Updated by your coach</h2>
      <ul className="space-y-1">
        {visible.map((run) => (
          <li
            key={run.id}
            className="flex flex-wrap items-center justify-between gap-2 text-sm"
          >
            <span
              className={run.undoneAt ? 'text-muted-foreground line-through' : undefined}
              data-testid="coach-update-line"
            >
              While you were away, your coach {describeRun(run, byId)}.
            </span>
            {run.undoneAt ? (
              <span className="text-sm text-muted-foreground">Undone</span>
            ) : (
              <Button
                variant="ghost"
                size="sm"
                disabled={undoing}
                data-testid="undo-run"
                onClick={() => onUndo(run.id)}
              >
                <Undo2 className="size-3" aria-hidden="true" />
                Undo
              </Button>
            )}
          </li>
        ))}
      </ul>
    </Card>
  );
}
