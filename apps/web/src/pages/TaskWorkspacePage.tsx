/**
 * Task workspace — `/projects/:projectId/tasks/:taskId`.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid: two panes, stacked on
 * mobile. Left is the task detail (the research report joins it at M4); right is the
 * session chat.
 *
 * The conversation itself is `SessionPane`, shared with the board's intake session since
 * M3 — the two screens differ in what sits beside the chat, not in the chat.
 */

import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'

import { SessionPane } from '@/components/session/SessionPane'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { useTask, useTaskSession } from '@/features/queries'
import { formatMinutes } from '@/lib/format'
import { getSocket } from '@/lib/socket'

export default function TaskWorkspacePage() {
  const { projectId = '', taskId = '' } = useParams()

  const task = useTask(taskId)
  const session = useTaskSession(taskId)
  const sessionId = session.data?.id ?? ''

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
      <section className="space-y-3 lg:w-2/5" aria-labelledby="task-detail-heading">
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
          <p className="text-muted-foreground text-sm whitespace-pre-wrap">
            {task.data.description}
          </p>
        ) : null}
        {/*
          The research report renders here from M4, with its required/optional split and
          budget meter (docs/06-frontend.md). The pane exists now so the two-column
          layout is the one that ships rather than one retrofitted around it.
        */}
        <p className="text-muted-foreground rounded-lg border border-dashed p-4 text-sm">
          Research materials appear here once your coach has prepared them.
        </p>
      </section>

      <SessionPane sessionId={sessionId} heading="Session with your coach" />
    </div>
  )
}
