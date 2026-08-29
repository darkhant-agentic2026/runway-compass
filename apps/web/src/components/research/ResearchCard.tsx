/**
 * The compact "latest research" preview — added at M8 — plus, since then, a disclosure
 * for everything before it.
 *
 * docs/06-frontend.md#task-board-projectsprojectid and
 * #task-workspace-projectsprojectidtaskstaskid: the board shows one of these for the
 * project's most recent research job, and the task workspace shows one for the task's
 * own. Same component, different feed (`useProjectRuns` vs `useTaskRuns`) — the caller
 * passes the whole list, newest first, and this renders the first entry as the card and
 * the rest behind a "View previous research" toggle.
 *
 * Earlier runs are not summarised until the toggle is opened — `useQueries` below fetches
 * each one's report lazily, so a task or a board with a long research history costs
 * nothing extra until the learner actually asks to see it.
 *
 * Renders nothing when there is no run yet, which is the common case for a fresh project
 * or an unresearched task.
 */

import { useQueries } from '@tanstack/react-query';
import { FlaskConical } from 'lucide-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';

import { Badge } from '@/components/ui/badge';
import { queryKeys, useRunPlan, useRunReport } from '@/features/queries';
import { api } from '@/lib/api';
import type { AutonomousRun, ResearchReport, StudyPlan } from '@/lib/schemas';

const STATUS_LABEL: Record<string, string> = {
  running: 'Your coach is researching…',
  pending: 'Starts soon',
  failed: "Couldn't finish — try again",
  cancelled: 'Cancelled',
  skipped_owner_present: 'Skipped — you were already here',
};

/** `steps[0].id` is how a roadmap run is told apart from an ordinary one — neither run
 * carries a field naming its pipeline directly (`ResearchViewPage.tsx`, same convention). */
function isRoadmapRun(run: AutonomousRun): boolean {
  return run.steps[0]?.id === 'roadmap';
}

/** The report's `summary`, as a one-line preview. Markdown is not rendered here — this
 * is a card, not the report — so a heading or a list marker in the first line shows up
 * as literal characters rather than as a stray heading in a two-line card. */
function briefDescription(summary: string): string {
  const firstLine = summary.split('\n').find((line) => line.trim().length > 0) ?? '';
  return firstLine.length > 140 ? `${firstLine.slice(0, 139)}…` : firstLine;
}

function summarize(run: AutonomousRun, report: ResearchReport | undefined): string {
  if (run.status !== 'complete') return STATUS_LABEL[run.status] ?? run.status;
  return report ? briefDescription(report.summary) : 'Materials ready';
}

/** A roadmap run has no `ResearchReport` — it wrote a `StudyPlan` instead — so its title
 * names the plan rather than summarizing a report that does not exist for it. */
function summarizeRoadmap(run: AutonomousRun, plan: StudyPlan | undefined): string {
  if (run.status !== 'complete') return STATUS_LABEL[run.status] ?? run.status;
  return plan ? `Roadmap: ${plan.title || 'Untitled roadmap'}` : 'Materials ready';
}

function RunCard({
  projectId,
  run,
  report,
  plan,
}: {
  projectId: string;
  run: AutonomousRun;
  report: ResearchReport | undefined;
  plan: StudyPlan | undefined;
}) {
  const roadmap = isRoadmapRun(run);
  return (
    <Link
      to={`/projects/${projectId}/research/${run.id}`}
      className="block rounded-lg border bg-card p-3 transition-colors hover:bg-accent/50"
      data-testid="research-card"
      data-run-status={run.status}
    >
      <div className="flex items-center gap-2">
        <FlaskConical className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="truncate text-sm font-medium">
          {roadmap ? summarizeRoadmap(run, plan) : summarize(run, report)}
        </span>
        {run.taskId === null ? (
          <Badge variant="outline" className="shrink-0 text-[0.7rem]">
            Project
          </Badge>
        ) : null}
        {/* A roadmap is a draft until the learner accepts it into real tasks
            (`materialize_study_plan`) — the chip is what tells a materialized plan apart
            from one still sitting there as a proposal. */}
        {roadmap && plan?.materializedAt ? (
          <Badge variant="secondary" className="shrink-0 text-[0.7rem]" data-testid="approved">
            Approved
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

export function ResearchCard({
  projectId,
  runs,
}: {
  projectId: string;
  runs: AutonomousRun[];
}) {
  const [showPrevious, setShowPrevious] = useState(false);
  const [latest, ...previous] = runs;

  const latestIsRoadmap = latest !== undefined && isRoadmapRun(latest);
  const latestReport = useRunReport(
    latest?.id ?? '',
    latest?.status === 'complete' && !latestIsRoadmap,
  );
  const latestPlan = useRunPlan(
    latest?.id ?? '',
    latest?.status === 'complete' && latestIsRoadmap,
  );

  // Fetched only once the disclosure is open, and only for runs that actually have
  // something to fetch — a `running` or `failed` earlier run has nothing behind it yet,
  // and a roadmap run has a `StudyPlan` where an ordinary one has a `ResearchReport`.
  const previousReports = useQueries({
    queries: showPrevious
      ? previous.map((run) => ({
          queryKey: queryKeys.runReport(run.id),
          queryFn: () => api.getRunReport(run.id),
          enabled: run.status === 'complete' && !isRoadmapRun(run),
        }))
      : [],
  });
  const previousPlans = useQueries({
    queries: showPrevious
      ? previous.map((run) => ({
          queryKey: queryKeys.runPlan(run.id),
          queryFn: () => api.getRunPlan(run.id),
          enabled: run.status === 'complete' && isRoadmapRun(run),
        }))
      : [],
  });

  if (!latest) return null;

  return (
    <div className="space-y-2">
      <RunCard
        projectId={projectId}
        run={latest}
        report={latestReport.data}
        plan={latestPlan.data}
      />

      {previous.length > 0 ? (
        <div>
          <button
            type="button"
            className="text-xs text-muted-foreground underline underline-offset-2"
            onClick={() => setShowPrevious((current) => !current)}
            aria-expanded={showPrevious}
            data-testid="toggle-previous-research"
          >
            {showPrevious
              ? 'Hide previous research'
              : `View previous research (${previous.length})`}
          </button>

          {showPrevious ? (
            <ul className="mt-2 space-y-2" data-testid="previous-research">
              {previous.map((run, index) => (
                <li key={run.id}>
                  <RunCard
                    projectId={projectId}
                    run={run}
                    report={previousReports[index]?.data}
                    plan={previousPlans[index]?.data}
                  />
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
