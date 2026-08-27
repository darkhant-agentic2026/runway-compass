/**
 * Research view — `/projects/:projectId/research/:runId`.
 *
 * docs/06-frontend.md#research-view-projectsprojectidresearchrunid. Added at M8 as a split
 * view — the run's own transcript on one side, its final report on the other. **Reworked
 * to a single pane, toggled rather than split**, once a roadmap run gave this screen a
 * second kind of result to show: the transcript (read-only — the coach is not conversing
 * with anyone) and the results (the report, or a roadmap run's `StudyPlan`) are each a
 * full-width view now, switched with the `ToggleGroup` in `ResearchInfoStrip`, rather than
 * two half-width panes fighting for room. **Both stay mounted regardless of which is
 * showing** — the hidden one gets `hidden`, not removed from the tree — deliberately,
 * rather than the simpler-looking conditional render: `SessionPane` owns a socket
 * subscription and a stream buffer for the run's turn, and unmounting it to switch views
 * would tear both down and force a resubscribe (and possibly a scroll jump) on switching
 * back, for a screen the learner may flip between more than once while a run is still
 * streaming. Defaults to results: the interesting thing about a *finished* run is what it
 * produced, not the transcript of how it got there.
 *
 * **Since the roadmap pipeline, this page also renders a roadmap run** — a run started
 * from `StartProjectRoadmap` rather than `StartProjectResearch`. It has no
 * `ResearchReport` (`GET /api/runs/{runId}/report` 404s for one; it wrote a `StudyPlan`
 * instead), so the results view fetches `GET /api/runs/{runId}/plan` instead and renders
 * `StudyPlanView` in place of `ResearchReport`
 * (docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer).
 * `steps[0].id` is how it tells the two kinds of taskless run apart: `"roadmap"` for
 * `ResearchService.start_roadmap`, `"research"` for `start_manual` — the same signal
 * `ROADMAP_STEPS`/`MANUAL_STEPS` exist to give a client, since neither run carries a field
 * naming its pipeline directly.
 *
 * **A failed roadmap run offers "Resume" rather than a link back to the board.**
 * `start_roadmap` is manual-only (docs/05-autonomous-runs.md) — there is no scheduled
 * retry — and unlike `start_manual`'s task-scoped "Try again", there is no task session to
 * relaunch it from. The failed run's own session already holds the learner's original
 * prompt as its first message (`start_roadmap` writes `reason.strip()` there verbatim), so
 * "Resume" reads it back and starts a fresh roadmap run with the same session and reason,
 * rather than sending the learner back to retype it.
 */

import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { StudyPlanView } from '@/components/research/StudyPlanView';
import { SessionPane } from '@/components/session/SessionPane';
import { ResearchReport } from '@/components/task/ResearchReport';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import {
  queryKeys,
  useReportFeedback,
  useRun,
  useRunPlan,
  useRunReport,
  useSessionEvents,
  useStartResearch,
  useStartRoadmap,
  useTask,
  useTaskSession,
} from '@/features/queries';
import { ApiError } from '@/lib/api';
import { getSocket } from '@/lib/socket';
import { toMessages } from '@/lib/transcript';
import { cn } from '@/lib/utils';
import { useStreamStore } from '@/stores/stream';

const RUN_STATUS_LABEL: Record<string, string> = {
  pending: 'Queued',
  running: 'Running',
  complete: 'Complete',
  failed: 'Failed',
  cancelled: 'Cancelled',
  skipped_owner_present: 'Skipped',
};

type ResearchView = 'transcript' | 'results';

/**
 * The status strip below the heading — `TaskWorkspacePage`'s `TaskInfoStrip`, adapted:
 * the always-visible glance at a run's status and when it started, plus (new here) the
 * control for which of the two full-width views below is showing.
 */
function ResearchInfoStrip({
  status,
  createdAt,
  view,
  onViewChange,
  resultsLabel,
}: {
  status?: string;
  createdAt?: string | null;
  view: ResearchView;
  onViewChange: (view: ResearchView) => void;
  resultsLabel: string;
}) {
  return (
    <div
      className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card px-3 py-2 text-sm"
      data-testid="research-info-strip"
    >
      <div className="flex flex-wrap items-center gap-2 text-muted-foreground">
        {status ? <Badge variant="outline">{RUN_STATUS_LABEL[status] ?? status}</Badge> : null}
        {createdAt ? <span>{new Date(createdAt).toLocaleString()}</span> : null}
      </div>
      <ToggleGroup
        value={[view]}
        onValueChange={(value) => {
          // Base UI reports an empty array when the active item is pressed again; keep the
          // current view rather than falling into an unset state where neither pane shows.
          const next = value[0];
          if (next) onViewChange(next as ResearchView);
        }}
        aria-label="Research view"
        variant="outline"
        size="sm"
        data-testid="research-view-toggle"
      >
        <ToggleGroupItem value="transcript">Transcript</ToggleGroupItem>
        <ToggleGroupItem value="results">{resultsLabel}</ToggleGroupItem>
      </ToggleGroup>
    </div>
  );
}

export default function ResearchViewPage() {
  const { projectId = '', runId = '' } = useParams();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [view, setView] = useState<ResearchView>('results');

  const run = useRun(runId);
  const isRoadmapRun = run.data?.steps[0]?.id === 'roadmap';
  const report = useRunReport(runId, run.data?.status === 'complete' && !isRoadmapRun);
  const plan = useRunPlan(runId, run.data?.status === 'complete' && isRoadmapRun);
  const task = useTask(run.data?.taskId ?? '');
  const taskSession = useTaskSession(run.data?.taskId ?? '');
  const retry = useStartResearch(run.data?.taskId ?? '', projectId);
  const feedback = useReportFeedback(run.data?.taskId ?? '');
  // The roadmap pipeline is taskless, so there is no `taskSession` to relaunch it from
  // the way `retry` does. The failed run's own session already carries the learner's
  // original prompt as its first message — `start_roadmap` writes it there verbatim,
  // unlike `start_manual`'s composed opening — so resuming reads it back rather than
  // asking the learner to retype it. `SessionPane` already fetches the same query for the
  // transcript view, so this shares its cache rather than costing a second request.
  const roadmapSession = useSessionEvents(run.data?.sessionId ?? '');
  const roadmapReason = toMessages(roadmapSession.data ?? []).find(
    (message) => message.role === 'user',
  )?.text;
  const resumeRoadmap = useStartRoadmap(projectId);

  /*
    A page loaded fresh — a reload, a link followed from somewhere that never started this
    run itself, or (since M9) simply the poll that first notices a queued run's `turnId` —
    has nothing in the stream store for this turn. Subscribing picks up the transcript
    regardless of whether the turn is still going: `ws/manager.py`'s `subscribe` replays
    every frame from `seq=0` whether the turn is running, ran on another instance, or has
    already finished — "the terminal frame is part of it" — so this does not require
    `status === 'running'` the way it used to. That condition was safe only as long as
    `turnId` never appeared before `running` did, which was true when a manual run started
    its turn inline and both landed in the same response; a queued run's `turnId` can now
    show up on a poll that catches it already `complete` (a fast local stub turn can finish
    between two 2-second polls), and gating on `running` would then never subscribe at all,
    leaving the transcript silent forever rather than merely late. `begin` and `subscribe`
    are both idempotent, so this is a no-op for the common case of navigating here right
    after starting the run.

    **Also invalidates the session's own events, since M9.** `SessionPane`'s `useSessionEvents`
    fetches once on mount and otherwise only refetches on `turn_complete` — fine when the
    turn already existed (with its opening message durably written) by the time this page's
    first fetch fired, which used to be guaranteed: `ResearchService` started the turn
    inline, in the same request that answered with `sessionId`. Queued, that write happens
    later, inside `RunExecutor`, after this page has typically already mounted and fetched
    an empty transcript once — so without this, a run that ends in `turn_error` (never
    `turn_complete`, so `SessionPane`'s own handoff never fires) would permanently render no
    opening message at all, the one thing about a research or roadmap session that has no
    composer echo to fall back on.
  */
  useEffect(() => {
    if (run.data?.turnId && run.data.sessionId) {
      useStreamStore.getState().begin(run.data.turnId, run.data.sessionId);
      getSocket().subscribe(run.data.turnId);
      void queryClient.invalidateQueries({
        queryKey: queryKeys.sessionEvents(run.data.sessionId),
      });
    }
  }, [run.data?.turnId, run.data?.sessionId, queryClient]);

  if (run.isError) {
    return <p className="p-6 text-muted-foreground">That research run could not be loaded.</p>;
  }

  const status = run.data?.status;
  const heading = run.data?.taskId
    ? (task.data?.task.title ?? 'Research for this task')
    : isRoadmapRun
      ? 'Roadmap for this project'
      : 'Research for this project';
  const resultsLabel = isRoadmapRun ? 'Study plan (draft)' : 'Report';

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 sm:p-6 lg:h-[calc(100vh-4rem)]">
      <header className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button variant="ghost" size="sm" render={<Link to={`/projects/${projectId}`} />}>
            ← Back to the board
          </Button>
          {run.data?.taskId ? (
            <Button
              variant="link"
              size="sm"
              render={<Link to={`/projects/${projectId}/tasks/${run.data.taskId}`} />}
            >
              Open task
            </Button>
          ) : null}
        </div>

        <h1 className="text-xl font-semibold">{heading}</h1>

        <ResearchInfoStrip
          status={status}
          createdAt={run.data?.createdAt}
          view={view}
          onViewChange={setView}
          resultsLabel={resultsLabel}
        />
      </header>

      <div className="flex flex-1 flex-col gap-4 lg:min-h-0">
        <div
          className={cn(
            'flex flex-col lg:min-h-0',
            view === 'transcript' ? 'flex-1' : 'hidden',
          )}
        >
          <SessionPane
            sessionId={run.data?.sessionId ?? ''}
            projectId={projectId}
            heading="Research session"
            readOnly
            className="relative flex h-[50svh] flex-col rounded-lg border lg:h-auto lg:flex-1"
            // The generic board/project invalidation `SessionPane` already does on
            // completion knows nothing about this run or its report — refresh both the
            // moment generation ends rather than waiting on `useRun`'s 2 s poll.
            onTurnComplete={() => {
              void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
              void queryClient.invalidateQueries({ queryKey: queryKeys.runReport(runId) });
              void queryClient.invalidateQueries({ queryKey: queryKeys.runPlan(runId) });
            }}
          />
        </div>

        <div
          className={cn(
            'lg:min-h-0 lg:overflow-y-auto',
            view === 'results' ? 'flex-1' : 'hidden',
          )}
          aria-label="Research results"
        >
          {status === 'running' || status === 'pending' ? (
            <p
              className="rounded-lg border border-dashed p-6 text-center text-muted-foreground"
              data-testid="research-running"
            >
              {isRoadmapRun
                ? 'Your coach is building a roadmap…'
                : 'Your coach is researching…'}
            </p>
          ) : status === 'failed' ? (
            <div className="space-y-3 rounded-lg border p-4" data-testid="research-failed">
              <p className="text-sm text-muted-foreground">
                {run.data?.error || "Your coach couldn't finish this research."}
              </p>
              {run.data?.taskId ? (
                <Button
                  size="sm"
                  disabled={retry.isPending || !taskSession.data?.id}
                  onClick={() => {
                    if (!taskSession.data?.id) return;
                    retry.mutate({ sessionId: taskSession.data.id, force: true });
                  }}
                >
                  Try again
                </Button>
              ) : isRoadmapRun ? (
                <Button
                  size="sm"
                  data-testid="resume-roadmap"
                  disabled={resumeRoadmap.isPending || !run.data?.sessionId || !roadmapReason}
                  onClick={() => {
                    const sessionId = run.data?.sessionId;
                    if (!sessionId || !roadmapReason) return;
                    resumeRoadmap.mutate(
                      { sessionId, reason: roadmapReason },
                      {
                        onSuccess: (resumed) => {
                          navigate(`/projects/${projectId}/research/${resumed.runId}`);
                        },
                        onError: (error) => {
                          const detail =
                            error instanceof ApiError
                              ? error.problem.detail
                              : 'Could not resume the roadmap.';
                          toast.error(detail || 'Could not resume the roadmap.');
                        },
                      },
                    );
                  }}
                >
                  {resumeRoadmap.isPending ? 'Resuming…' : 'Resume'}
                </Button>
              ) : (
                <p className="text-sm text-muted-foreground">
                  Ask your coach to research this again from{' '}
                  <Link to={`/projects/${projectId}`} className="underline underline-offset-2">
                    the board
                  </Link>
                  .
                </p>
              )}
            </div>
          ) : status === 'complete' && isRoadmapRun ? (
            <div data-testid="roadmap-complete">
              {plan.data ? (
                <StudyPlanView plan={plan.data} />
              ) : (
                <p className="p-6 text-muted-foreground">Loading your study plan…</p>
              )}
            </div>
          ) : status === 'complete' && report.data ? (
            <ResearchReport
              report={report.data}
              // Feedback is a task-scoped endpoint (docs/06-frontend.md): a project-scoped
              // report renders `optional[]` without the thumbs-up/down control at all.
              onFeedback={
                report.data.taskId
                  ? (itemId, value) =>
                      feedback.mutate(
                        { reportId: report.data.id, itemId, feedback: value },
                        {
                          // `useReportFeedback`'s own invalidation targets `['task', id,
                          // 'reports']`, which nothing here reads — this screen's report
                          // comes from `queryKeys.runReport`, so write the mutation's
                          // result there directly rather than round-tripping a refetch.
                          onSuccess: (updated) =>
                            queryClient.setQueryData(queryKeys.runReport(runId), updated),
                        },
                      )
                  : undefined
              }
            />
          ) : (
            <p className="p-6 text-muted-foreground">Loading…</p>
          )}
        </div>
      </div>
    </div>
  );
}
