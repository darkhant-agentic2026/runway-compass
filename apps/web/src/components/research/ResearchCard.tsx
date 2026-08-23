/**
 * The compact "latest research" preview — added at M8.
 *
 * docs/06-frontend.md#task-board-projectsprojectid and
 * #task-workspace-projectsprojectidtaskstaskid: the board shows one of these for the
 * project's most recent research job, and the task workspace shows one for the task's
 * own. Same component, different feed (`useProjectRuns` vs `useTaskRuns`) — the caller
 * decides which run is "latest" and passes it in.
 *
 * Renders nothing when there is no run yet, which is the common case for a fresh project
 * or an unresearched task.
 */

import { FlaskConical } from 'lucide-react';
import { Link } from 'react-router-dom';

import { Badge } from '@/components/ui/badge';
import { useRunReport } from '@/features/queries';
import type { AutonomousRun } from '@/lib/schemas';

const STATUS_LABEL: Record<string, string> = {
  running: 'Your coach is researching…',
  pending: 'Starts soon',
  failed: "Couldn't finish — try again",
  cancelled: 'Cancelled',
  skipped_owner_present: 'Skipped — you were already here',
};

/** The report's `summary`, as a one-line preview. Markdown is not rendered here — this
 * is a card, not the report — so a heading or a list marker in the first line shows up
 * as literal characters rather than as a stray heading in a two-line card. */
function briefDescription(summary: string): string {
  const firstLine = summary.split('\n').find((line) => line.trim().length > 0) ?? '';
  return firstLine.length > 140 ? `${firstLine.slice(0, 139)}…` : firstLine;
}

export function ResearchCard({
  projectId,
  run,
}: {
  projectId: string;
  run: AutonomousRun | undefined;
}) {
  const report = useRunReport(run?.id ?? '', run?.status === 'complete');

  if (!run) return null;

  const description =
    run.status === 'complete'
      ? report.data
        ? briefDescription(report.data.summary)
        : 'Materials ready'
      : (STATUS_LABEL[run.status] ?? run.status);

  return (
    <Link
      to={`/projects/${projectId}/research/${run.id}`}
      className="block rounded-lg border bg-card p-3 transition-colors hover:bg-accent/50"
      data-testid="research-card"
      data-run-status={run.status}
    >
      <div className="flex items-center gap-2">
        <FlaskConical className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="truncate text-sm font-medium">{description}</span>
        {run.taskId === null ? (
          <Badge variant="outline" className="shrink-0 text-[0.7rem]">
            Project
          </Badge>
        ) : null}
      </div>
      {run.createdAt ? (
        <p className="mt-1 text-xs text-muted-foreground">
          {new Date(run.createdAt).toLocaleString()}
        </p>
      ) : null}
    </Link>
  );
}
