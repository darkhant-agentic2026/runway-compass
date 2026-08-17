/**
 * Task workspace — `/projects/:projectId/tasks/:taskId`.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid: two panes, stacked on
 * mobile. Left is the task detail (the research report joins it at M4); right is the
 * session chat.
 *
 * The one interaction worth reading carefully is the `turn_complete` handoff. While a
 * turn streams, its text lives in `useStreamStore` and only the bubble re-renders. When
 * it completes, the transcript query is refetched once and the buffer is cleared — "one
 * handoff, one re-render of the transcript". Doing it in that order matters: clearing
 * first would blank the message for however long the refetch takes.
 */

import { useEffect, useRef, useState, type DragEvent } from 'react'
import { Link, useParams } from 'react-router-dom'

import { Composer } from '@/components/session/Composer'
import { ConnectionBanner } from '@/components/session/ConnectionBanner'
import { Transcript } from '@/components/session/Transcript'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  useCancelTurn,
  useSessionEvents,
  useStartTurn,
  useTask,
  useTaskSession,
} from '@/features/queries'
import { useAttachmentUploads } from '@/features/use-uploads'
import { formatMinutes } from '@/lib/format'
import { getSocket } from '@/lib/socket'
import { toMessages } from '@/lib/transcript'
import { useComposerStore } from '@/stores/composer'
import { useStreamStore } from '@/stores/stream'

/** Whether a drag carries files, as opposed to selected text or a dragged link. */
function hasFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer.types).includes('Files')
}

export default function TaskWorkspacePage() {
  const { projectId = '', taskId = '' } = useParams()

  const task = useTask(taskId)
  const session = useTaskSession(taskId)
  const sessionId = session.data?.id ?? ''
  const events = useSessionEvents(sessionId)
  const startTurn = useStartTurn(sessionId)
  const cancelTurn = useCancelTurn(sessionId)

  const turns = useStreamStore((state) => state.turns)
  const clearTurn = useStreamStore((state) => state.clear)
  const resetComposer = useComposerStore((state) => state.reset)
  const { uploadAll } = useAttachmentUploads(sessionId)
  const [dragDepth, setDragDepth] = useState(0)

  const live = Object.values(turns).find((turn) => turn.sessionId === sessionId) ?? null

  // Presence: "every 30 s while a task workspace is focused" (docs/06-frontend.md).
  // Pointed here on mount and released on unmount, so a user who navigates back to the
  // board stops claiming the project — which is what lets the autonomous agent work on it.
  useEffect(() => {
    if (!projectId) return
    const socket = getSocket()
    socket.setPresenceTarget({ projectId, taskId })
    return () => socket.setPresenceTarget(null)
  }, [projectId, taskId])

  // The handoff. Refetch first, then drop the buffer.
  const handled = useRef<string | null>(null)
  useEffect(() => {
    if (!live || live.status !== 'complete' || handled.current === live.turnId) return
    handled.current = live.turnId
    const turnId = live.turnId
    void events.refetch().then(() => clearTurn(turnId))
  }, [live, events, clearTurn])

  const messages = toMessages(events.data ?? [])
  const streaming = live?.status === 'running'

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

      {/*
        The drop target is the whole chat pane, not the composer strip. A two-line strip
        at the bottom of the window is a target people miss, and missing it is worse than
        having none: the browser's default action for a file dropped on a page is to
        navigate away from the app and open the file.

        `dragCounter` rather than a boolean, because `dragenter`/`dragleave` fire for every
        descendant the pointer crosses — a boolean flickers off the moment the cursor
        moves from the transcript onto a message bubble.
      */}
      <section
        className="relative flex min-h-[28rem] flex-1 flex-col rounded-lg border lg:min-h-0"
        aria-labelledby="session-heading"
        onDragEnter={(event) => {
          if (!hasFiles(event)) return
          event.preventDefault()
          setDragDepth((depth) => depth + 1)
        }}
        onDragOver={(event) => {
          if (hasFiles(event)) event.preventDefault()
        }}
        onDragLeave={() => setDragDepth((depth) => Math.max(0, depth - 1))}
        onDrop={(event) => {
          if (!hasFiles(event)) return
          event.preventDefault()
          setDragDepth(0)
          uploadAll(event.dataTransfer.files)
        }}
      >
        <h2 id="session-heading" className="sr-only">
          Session with your coach
        </h2>

        {dragDepth > 0 ? (
          <div
            className="bg-background/85 border-primary pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-lg border-2 border-dashed"
            data-testid="drop-overlay"
          >
            <p className="text-sm font-medium">Drop to attach</p>
          </div>
        ) : null}

        <div className="px-3 pt-3">
          <ConnectionBanner />
        </div>

        <Transcript messages={messages} live={live} pending={events.isPending} />

        {sessionId ? (
          <Composer
            sessionId={sessionId}
            sending={startTurn.isPending}
            streaming={Boolean(streaming)}
            onSend={(text, attachments) => {
              startTurn.mutate({ text, attachments })
              resetComposer(sessionId)
            }}
            // `attachments` carries `filename` for the optimistic echo; `api.startTurn`
            // strips it, because `TurnAttachment` forbids unknown fields.
            onCancel={() => {
              if (live) cancelTurn.mutate(live.turnId)
            }}
          />
        ) : null}
      </section>
    </div>
  )
}
