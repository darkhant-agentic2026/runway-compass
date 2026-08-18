/**
 * Task workspace — `/projects/:projectId/tasks/:taskId`.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid: two panes, stacked on
 * mobile. Left is the task detail and, for a composite task, its subtasks (the research
 * report joins them at M4); right is the session chat.
 *
 * The conversation itself is `SessionPane`, shared with the board's intake session since
 * M3 — the two screens differ in what sits beside the chat, not in the chat. That is also
 * the reason the subtasks live *here* rather than on screens of their own: one session
 * covers the whole composite task, and it is this one.
 */

import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'

import { SessionPane } from '@/components/session/SessionPane'
import { SubtaskList } from '@/components/task/SubtaskList'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useSetSubtaskState, useTask, useTaskSession } from '@/features/queries'
import { formatMinutes } from '@/lib/format'
import { getSocket } from '@/lib/socket'

export default function TaskWorkspacePage() {
  const { projectId = '', taskId = '' } = useParams()

  const task = useTask(taskId)
  const session = useTaskSession(taskId)
  const sessionId = session.data?.id ?? ''
  const setSubtaskState = useSetSubtaskState(taskId, projectId)

  // Presence: "every 30 s while a task workspace is focused" (docs/06-frontend.md).
  // Pointed here on mount and released on unmount, so a user who navigates back to the
  // board stops claiming the project — which is what lets the autonomous agent work on it.
  useEffect(() => {
    if (!projectId) return
    const socket = getSocket()
    socket.setPresenceTarget({ projectId, taskId })
    return () => socket.setPresenceTarget(null)
  }, [projectId, taskId])

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
          {task.data?.title ?? 'Task'}
        </h1>
        {task.data ? (
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{formatMinutes(task.data.estimatedMinutes)}</Badge>
            <Badge variant="outline">{task.data.state.replaceAll('_', ' ')}</Badge>
          </div>
        ) : null}
        {task.data?.description ? (
          <p className="text-sm whitespace-pre-wrap text-muted-foreground">
            {task.data.description}
          </p>
        ) : null}
        {task.data ? (
          <SubtaskList
            subtasks={task.data.subtasks}
            rollup={task.data.rollup}
            onSetState={(subtaskId, state, postponedUntil) =>
              setSubtaskState.mutate({
                taskId: subtaskId,
                state,
                ...(postponedUntil ? { postponedUntil } : {}),
              })
            }
          />
        ) : null}

        {/*
          The research report renders here from M4, with its required/optional split and
          budget meter (docs/06-frontend.md). The pane exists now so the two-column
          layout is the one that ships rather than one retrofitted around it.
        */}
        <p className="rounded-lg border border-dashed p-4 text-sm text-muted-foreground">
          Research materials appear here once your coach has prepared them.
        </p>
      </section>

      <SessionPane
        sessionId={sessionId}
        projectId={projectId}
        heading="Session with your coach"
      />
    </div>
  )
}
