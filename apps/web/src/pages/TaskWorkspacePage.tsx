/**
 * Task workspace — `/projects/:projectId/tasks/:taskId`.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid: below the breadcrumbs
 * and the compact task-info strip (always visible), two panes, stacked on mobile. Left is
 * the task detail — description, then either its subtasks (composite) or its checklist
 * (leaf), then a compact "latest research" card; right is the session chat. Since M8 the
 * full report — summary, optional material, citations — lives on its own research view,
 * reached through that card, rather than rendering inline here.
 *
 * **The left pane is collapsible**, for a task whose checklist is mostly being worked
 * through in conversation with the coach rather than by clicking checkboxes directly — the
 * detail column is reference material at that point, and hiding it gives the chat the
 * room. Expanded by default, always; `useWorkspaceUiStore` remembers an explicit toggle
 * per task, but nothing collapses on its own. The info strip below the breadcrumbs is the
 * "how far along is this" glance that stays visible either way.
 *
 * `Checklist` and `SubtaskList` are never both present — a task's plan is one or the other
 * (docs/02-data-model.md#task-items).
 *
 * The conversation itself is `SessionPane`, shared with the board's intake session since
 * M3 — the two screens differ in what sits beside the chat, not in the chat. That is also
 * the reason the subtasks live *here* rather than on screens of their own: one session
 * covers the whole composite task, and it is this one.
 */

import { ChevronDown, ChevronUp } from 'lucide-react';
import { useEffect } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { ProgressRing } from '@/components/board/ProgressRing';
import { STATE_LABELS } from '@/components/board/task-state';
import { ResearchCard } from '@/components/research/ResearchCard';
import { SessionPane } from '@/components/session/SessionPane';
import { Checklist } from '@/components/task/Checklist';
import { SubtaskList } from '@/components/task/SubtaskList';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  useCreateSubtask,
  useProject,
  useResearchRequest,
  useSetSubtaskState,
  useStartResearch,
  useSubtaskItemMutation,
  useTask,
  useTaskItemMutation,
  useTaskRuns,
  useTaskSession,
} from '@/features/queries';
import { ApiError } from '@/lib/api';
import { formatMinutes, pluralize } from '@/lib/format';
import type { AutonomousRun, TaskWithSubtasks } from '@/lib/schemas';
import { getSocket } from '@/lib/socket';
import { newestTurnFor, useStreamStore } from '@/stores/stream';
import { useWorkspaceUiStore } from '@/stores/workspaceUi';

/** As `BoardPage`'s: a fresh `[]` on every render of an unsettled query is a new value
 * as far as anything memoizing on it is concerned. */
const EMPTY_RUNS: AutonomousRun[] = [];

/**
 * The narrow, always-visible echo of a `TaskCard` row: state, estimate, and progress.
 * No title — the breadcrumb beside it already names the task, and repeating it here would
 * be the same fact twice in adjacent elements.
 */
function TaskInfoStrip({
  task,
  isComposite,
}: {
  task: TaskWithSubtasks;
  isComposite: boolean;
}) {
  return (
    <div
      className="flex flex-wrap items-center gap-3 rounded-lg border bg-card px-3 py-2 text-sm"
      data-testid="task-info-strip"
    >
      <Badge variant="secondary">{formatMinutes(task.estimatedMinutes)}</Badge>
      <Badge variant="outline">{STATE_LABELS[task.state]}</Badge>
      {isComposite && task.rollup && task.rollup.subtaskCount > 0 ? (
        <div className="flex items-center gap-2 text-muted-foreground">
          <ProgressRing
            completed={task.rollup.completedSubtasks}
            total={task.rollup.subtaskCount}
          />
          <span>
            {pluralize(task.rollup.subtaskCount, 'subtask')} ·{' '}
            {formatMinutes(task.rollup.totalEstimatedMinutes)}
          </span>
        </div>
      ) : !isComposite && task.items.length > 0 ? (
        <div className="flex items-center gap-2 text-muted-foreground">
          <ProgressRing
            completed={task.items.filter((item) => item.completed).length}
            total={task.items.length}
          />
          <span>
            {task.items.filter((item) => item.completed).length} of {task.items.length} done
          </span>
        </div>
      ) : null}
    </div>
  );
}

export default function TaskWorkspacePage() {
  const { projectId = '', taskId = '' } = useParams();
  const navigate = useNavigate();

  const project = useProject(projectId);
  const detail = useTask(taskId);
  const task = detail.data?.task;
  const report = detail.data?.latestReport ?? null;
  const session = useTaskSession(taskId);
  const sessionId = session.data?.id ?? '';
  const setSubtaskState = useSetSubtaskState(taskId, projectId);
  const itemMutation = useTaskItemMutation(taskId, projectId);
  const subtaskItem = useSubtaskItemMutation(taskId, projectId);
  const addSubtask = useCreateSubtask(taskId, projectId);
  const startResearch = useStartResearch(taskId, projectId);
  const researchRequest = useResearchRequest(taskId, projectId);
  const runs = useTaskRuns(taskId);

  const isComposite = (task?.subtasks.length ?? 0) > 0;
  const detailsCollapsed = useWorkspaceUiStore((state) => state.isDetailsCollapsed(taskId));
  const toggleDetails = useWorkspaceUiStore((state) => state.toggleDetails);

  /*
    Whether a research run this page started is still going.
    `task.researchStatus` is *not* enough on its own, and the reason is a genuine race the
    e2e caught: `post_research_report` writes the report and pushes `board_update` — so the
    checklist renders and `researchStatus` flips to `done` — while the turn is still
    streaming its closing prose and the project's agent lease is still held. A button that
    re-enabled at that moment invites a click the server answers with
    "your coach is already working on this project".

    The turn is the honest signal, so the button follows the turn.
  */
  const turns = useStreamStore((state) => state.turns);
  const liveTurn = newestTurnFor(turns, sessionId);
  const researching =
    startResearch.isPending ||
    task?.researchStatus === 'in_progress' ||
    (startResearch.data?.turnId != null &&
      liveTurn?.turnId === startResearch.data.turnId &&
      liveTurn.status === 'running');

  /*
    Whether the learner has queued research that no run has picked up yet. Read from
    `researchStatus` rather than from `researchRequestedAt`, even though the two are
    written together: `pending` is the state the *scheduler* keys on, and a control that
    disagreed with the queue about what is queued would be worse than one that lagged.
  */
  const queued = task?.researchStatus === 'pending';

  function research(force: boolean) {
    if (!sessionId) return;
    startResearch.mutate(
      { sessionId, force },
      {
        // Since M8 the turn runs in a session of its own — navigate straight to its
        // research view, which is where progress is watched now
        // (docs/06-frontend.md#research-view-projectsprojectidresearchrunid).
        onSuccess(run) {
          navigate(`/projects/${projectId}/research/${run.runId}`);
        },
        onError(error) {
          // A 409 carrying a `runId` means the coach is already working on this project.
          // Saying so is the difference between an actionable answer and an invitation to
          // press the button again (docs/04-api-contract.md).
          const detail_ =
            error instanceof ApiError ? error.problem.detail : 'Could not start research.';
          toast.error(detail_ || 'Could not start research.');
        },
      },
    );
  }

  // Presence: "every 30 s while a task workspace is focused" (docs/06-frontend.md).
  // Pointed here on mount and released on unmount, so a user who navigates back to the
  // board stops claiming the project — which is what lets the autonomous agent work on it.
  useEffect(() => {
    if (!projectId) return;
    const socket = getSocket();
    socket.setPresenceTarget({ projectId, taskId });
    return () => socket.setPresenceTarget(null);
  }, [projectId, taskId]);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 sm:p-6 lg:h-[calc(100vh-4rem)]">
      {/* A visible on-screen title lives in the breadcrumb below; this is only for
          assistive technology and the tab's landmark structure. */}
      <h1 className="sr-only">{task?.title ?? 'Task'}</h1>

      <header className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button variant="ghost" size="sm" render={<Link to={`/projects/${projectId}`} />}>
            ← Back to the board
          </Button>
          {task ? (
            <Button
              variant="outline"
              size="sm"
              onClick={() => toggleDetails(taskId)}
              aria-expanded={!detailsCollapsed}
              data-testid="toggle-task-details"
            >
              {detailsCollapsed ? (
                <ChevronDown className="size-4" aria-hidden="true" />
              ) : (
                <ChevronUp className="size-4" aria-hidden="true" />
              )}
              {detailsCollapsed ? 'Show details' : 'Hide details'}
            </Button>
          ) : null}
        </div>

        {/*
          Breadcrumbs, added beside the existing "back" link rather than instead of it —
          the two answer different questions ("where can I go" vs "where am I").
        */}
        <nav aria-label="Breadcrumb" className="text-sm text-muted-foreground">
          <ol className="flex flex-wrap items-center gap-1">
            <li>
              <Link to="/" className="hover:underline">
                Projects
              </Link>
            </li>
            <li aria-hidden="true">/</li>
            <li>
              <Link to={`/projects/${projectId}`} className="hover:underline">
                {project.data?.title ?? 'Project'}
              </Link>
            </li>
            <li aria-hidden="true">/</li>
            <li
              aria-current="page"
              className="max-w-[16rem] truncate font-medium text-foreground"
            >
              {task?.title ?? 'Task'}
            </li>
          </ol>
        </nav>

        {/*
          The narrow, always-visible echo of the task card — progress and status survive
          collapsing the detail column, so "how far along is this" never requires
          expanding it (docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid).
        */}
        {task ? <TaskInfoStrip task={task} isComposite={isComposite} /> : null}
      </header>

      <div className="flex flex-1 flex-col gap-4 lg:min-h-0 lg:flex-row">
        {/*
          `lg:overflow-y-auto` because this column now holds an arbitrary number of subtask
          cards inside a fixed-height row. Without it the whole page scrolls, which is the
          same failure the chat pane was bounded to avoid: the transcript pins itself to its
          own bottom several times a second, and it can only do that if the page is not the
          thing that scrolls (docs/06-frontend.md).
        */}
        {task && !detailsCollapsed ? (
          <section
            className="space-y-3 lg:min-h-0 lg:w-2/5 lg:overflow-y-auto"
            aria-label="Task details"
          >
            {task.description ? (
              <p className="text-sm whitespace-pre-wrap text-muted-foreground">
                {task.description}
              </p>
            ) : null}

            <SubtaskList
              subtasks={task.subtasks}
              rollup={task.rollup}
              onSetState={(subtaskId, state, postponedUntil) =>
                setSubtaskState.mutate({
                  taskId: subtaskId,
                  state,
                  ...(postponedUntil ? { postponedUntil } : {}),
                })
              }
              itemsDisabled={subtaskItem.isPending}
              onToggleItem={(subtaskId, itemId, completed) =>
                subtaskItem.mutate({ taskId: subtaskId, itemId, completed })
              }
              hasItems={task.items.length > 0}
              addDisabled={addSubtask.isPending}
              onAdd={(title, estimatedMinutes) =>
                addSubtask.mutate({ title, estimatedMinutes, parentTaskId: taskId })
              }
            />

            {!isComposite ? (
              <Checklist
                items={task.items}
                budgetMinutes={report?.budgetMinutes ?? null}
                disabled={itemMutation.isPending}
                onToggle={(itemId, completed) =>
                  itemMutation.mutate({ kind: 'toggle', itemId, completed })
                }
              />
            ) : null}

            {/*
              The latest research job for this task, if it has one — a compact card since
              M8, linking into its own research view rather than rendering the full report
              inline (docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid).
            */}
            <ResearchCard projectId={projectId} runs={runs.data ?? EMPTY_RUNS} />

            {/*
              "Research this task now" is the screen's primary action on a task with no
              materials, and moves to a quieter "Research again" once there are some
              (docs/06-frontend.md). Hidden entirely on a composite task: its subtasks are
              its plan, and each is researched on its own.
            */}
            {!isComposite ? (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant={report ? 'outline' : 'default'}
                    size="sm"
                    disabled={researching || !sessionId}
                    onClick={() => research(report !== null)}
                  >
                    {researching
                      ? 'Your coach is preparing materials…'
                      : report
                        ? 'Research again'
                        : 'Research this task now'}
                  </Button>
                  {/*
                    The queued half of the pair. Same research, run headless by the next
                    tick with priority over auto-scheduled work, so the learner can mark
                    the task and close the tab (docs/06-frontend.md).

                    Two buttons for one outcome is a real cost and it is paid
                    deliberately: the queued path is the one intended to become the
                    *only* path once the autonomous agent is proven unattended, and the
                    inline button is what keeps the feature usable while that is being
                    established.

                    Hidden while a run holds the task, because by then `researchStatus`
                    has left `pending` and the honest control is the turn's cancel, not
                    this one.
                  */}
                  {!researching ? (
                    <Button
                      variant={queued ? 'secondary' : 'outline'}
                      size="sm"
                      disabled={researchRequest.isPending}
                      data-testid="queue-research"
                      onClick={() => researchRequest.mutate({ queued: !queued })}
                    >
                      {queued ? 'Starts soon — cancel' : 'Have my coach prepare this'}
                    </Button>
                  ) : null}
                </div>
                {queued ? (
                  <p
                    className="text-sm text-muted-foreground"
                    data-testid="research-queued-note"
                  >
                    Queued. Your coach will prepare this in the background, ahead of anything it
                    planned for itself — you can close this tab.
                  </p>
                ) : null}
                {/*
                  A failed run reads as an offer rather than an error, and the retry is a
                  press rather than an automatic re-enqueue: a task the research agent
                  cannot handle should cost one run per decision the learner makes, not
                  one per tick.
                */}
                {task.researchStatus === 'failed' && !researching ? (
                  <p className="text-sm text-muted-foreground" data-testid="research-failed">
                    Your coach couldn&apos;t prepare this last time. Try again when you like.
                  </p>
                ) : null}
                {!report && !researching && !queued ? (
                  <p className="text-sm text-muted-foreground">
                    No materials yet. Your coach can find reading, videos, and exercises sized
                    to this task.
                  </p>
                ) : null}
              </div>
            ) : null}
          </section>
        ) : null}

        <SessionPane
          sessionId={sessionId}
          projectId={projectId}
          heading="Session with your coach"
        />
      </div>
    </div>
  );
}
