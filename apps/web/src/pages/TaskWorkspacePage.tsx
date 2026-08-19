/**
 * Task workspace — `/projects/:projectId/tasks/:taskId`.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid: two panes, stacked on
 * mobile. Left is the task detail, then either its subtasks (composite) or its checklist
 * (leaf), then the research report; right is the session chat.
 *
 * The left pane's order is the order the work happens in: what to do, then the material
 * behind it. `Checklist` and `SubtaskList` are never both present — a task's plan is one or
 * the other (docs/02-data-model.md#task-items).
 *
 * The conversation itself is `SessionPane`, shared with the board's intake session since
 * M3 — the two screens differ in what sits beside the chat, not in the chat. That is also
 * the reason the subtasks live *here* rather than on screens of their own: one session
 * covers the whole composite task, and it is this one.
 */

import { useEffect } from 'react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { STATE_LABELS } from '@/components/board/task-state';
import { SessionPane } from '@/components/session/SessionPane';
import { Checklist } from '@/components/task/Checklist';
import { ResearchReport } from '@/components/task/ResearchReport';
import { SubtaskList } from '@/components/task/SubtaskList';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  useCreateSubtask,
  useReportFeedback,
  useReportHistory,
  useSetSubtaskState,
  useStartResearch,
  useTask,
  useSubtaskItemMutation,
  useTaskItemMutation,
  useTaskSession,
} from '@/features/queries';
import { ApiError } from '@/lib/api';
import { formatMinutes } from '@/lib/format';
import { getSocket } from '@/lib/socket';
import { newestTurnFor, useStreamStore } from '@/stores/stream';

export default function TaskWorkspacePage() {
  const { projectId = '', taskId = '' } = useParams();

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
  const feedback = useReportFeedback(taskId);
  const history = useReportHistory(taskId);

  const isComposite = (task?.subtasks.length ?? 0) > 0;

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

  function research(force: boolean) {
    if (!sessionId) return;
    startResearch.mutate(
      { sessionId, force },
      {
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
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 sm:p-6 lg:h-[calc(100vh-4rem)] lg:flex-row">
      {/*
        `lg:overflow-y-auto` because this column now holds an arbitrary number of subtask
        cards inside a fixed-height row. Without it the whole page scrolls, which is the
        same failure the chat pane was bounded to avoid: the transcript pins itself to its
        own bottom several times a second, and it can only do that if the page is not the
        thing that scrolls (docs/06-frontend.md).
      */}
      <section
        className="space-y-3 lg:min-h-0 lg:w-2/5 lg:overflow-y-auto"
        aria-labelledby="task-detail-heading"
      >
        <Button variant="ghost" size="sm" render={<Link to={`/projects/${projectId}`} />}>
          ← Back to the board
        </Button>
        <h1 id="task-detail-heading" className="text-xl font-semibold">
          {task?.title ?? 'Task'}
        </h1>
        {task ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{formatMinutes(task.estimatedMinutes)}</Badge>
            <Badge variant="outline">{STATE_LABELS[task.state]}</Badge>
          </div>
        ) : null}
        {task?.description ? (
          <p className="text-sm whitespace-pre-wrap text-muted-foreground">
            {task.description}
          </p>
        ) : null}

        {task ? (
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
        ) : null}

        {task && !isComposite ? (
          <Checklist
            items={task.items}
            budgetMinutes={report?.budgetMinutes ?? null}
            disabled={itemMutation.isPending}
            onToggle={(itemId, completed) =>
              itemMutation.mutate({ kind: 'toggle', itemId, completed })
            }
          />
        ) : null}

        {report ? (
          <ResearchReport
            report={report}
            earlier={(history.data ?? []).filter((old) => old.id !== report.id)}
            onFeedback={(itemId, value) =>
              feedback.mutate({ reportId: report.id, itemId, feedback: value })
            }
          />
        ) : null}

        {/*
          "Research this task now" is the screen's primary action on a task with no
          materials, and moves to a quieter "Research again" once there are some
          (docs/06-frontend.md). Hidden entirely on a composite task: its subtasks are its
          plan, and each is researched on its own.
        */}
        {task && !isComposite ? (
          <div className="space-y-2">
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
            {!report && !researching ? (
              <p className="text-sm text-muted-foreground">
                No materials yet. Your coach can find reading, videos, and exercises sized to
                this task.
              </p>
            ) : null}
          </div>
        ) : null}
      </section>

      <SessionPane
        sessionId={sessionId}
        projectId={projectId}
        heading="Session with your coach"
      />
    </div>
  );
}
