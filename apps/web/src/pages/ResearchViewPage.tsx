/**
 * Research view — `/projects/:projectId/research/:runId`.
 *
 * docs/06-frontend.md#research-view-projectsprojectidresearchrunid. Added at M8, once
 * research runs got their own session: two panes, the run's own transcript on the left
 * (read-only — the coach is not conversing with anyone) and its final report on the
 * right, once there is one.
 */

import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { Link, useParams } from 'react-router-dom';

import { SessionPane } from '@/components/session/SessionPane';
import { ResearchReport } from '@/components/task/ResearchReport';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  queryKeys,
  useReportFeedback,
  useRun,
  useRunReport,
  useStartResearch,
  useTask,
  useTaskSession,
} from '@/features/queries';
import { getSocket } from '@/lib/socket';
import { useStreamStore } from '@/stores/stream';

const RUN_STATUS_LABEL: Record<string, string> = {
  pending: 'Queued',
  running: 'Running',
  complete: 'Complete',
  failed: 'Failed',
  cancelled: 'Cancelled',
  skipped_owner_present: 'Skipped',
};

export default function ResearchViewPage() {
  const { projectId = '', runId = '' } = useParams();
  const queryClient = useQueryClient();

  const run = useRun(runId);
  const report = useRunReport(runId, run.data?.status === 'complete');
  const task = useTask(run.data?.taskId ?? '');
  const taskSession = useTaskSession(run.data?.taskId ?? '');
  const retry = useStartResearch(run.data?.taskId ?? '', projectId);
  const feedback = useReportFeedback(run.data?.taskId ?? '');

  /*
    A page loaded fresh — a reload, or a link followed from somewhere that never started
    this run itself — has nothing in the stream store for this turn. If the run is still
    going, pick up where it is rather than showing a silent transcript until the next
    poll settles (docs/06-frontend.md). `begin` and `subscribe` are both idempotent, so
    this is a no-op for the common case of navigating here right after starting the run.
  */
  useEffect(() => {
    if (run.data?.status === 'running' && run.data.turnId && run.data.sessionId) {
      useStreamStore.getState().begin(run.data.turnId, run.data.sessionId);
      getSocket().subscribe(run.data.turnId);
    }
  }, [run.data?.status, run.data?.turnId, run.data?.sessionId]);

  if (run.isError) {
    return <p className="p-6 text-muted-foreground">That research run could not be loaded.</p>;
  }

  const status = run.data?.status;
  const heading = run.data?.taskId
    ? (task.data?.task.title ?? 'Research for this task')
    : 'Research for this project';

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 sm:p-6 lg:h-[calc(100vh-4rem)] lg:flex-row">
      <div className="flex w-full flex-col gap-4 lg:min-h-0 lg:flex-row">
        <section className="flex flex-col gap-3 lg:w-1/2">
          <header className="space-y-1">
            <Button variant="ghost" size="sm" render={<Link to={`/projects/${projectId}`} />}>
              ← Back to the board
            </Button>
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold">{heading}</h1>
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
            <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              {status ? (
                <Badge variant="outline">{RUN_STATUS_LABEL[status] ?? status}</Badge>
              ) : null}
              {run.data?.createdAt ? (
                <span>{new Date(run.data.createdAt).toLocaleString()}</span>
              ) : null}
            </div>
          </header>

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
            }}
          />
        </section>

        <section
          className="lg:min-h-0 lg:w-1/2 lg:overflow-y-auto"
          aria-label="Research report"
        >
          {status === 'running' || status === 'pending' ? (
            <p
              className="rounded-lg border border-dashed p-6 text-center text-muted-foreground"
              data-testid="research-running"
            >
              Your coach is researching…
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
        </section>
      </div>
    </div>
  );
}
