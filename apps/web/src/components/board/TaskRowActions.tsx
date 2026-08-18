/**
 * The row action menu.
 *
 * Two things here are requirements rather than polish:
 *
 * - Only *legal* transitions are offered (`transitionsFor`), so the menu cannot produce
 *   a 409 from the state machine.
 * - The menu carries the **keyboard fallback for drag-and-drop** — "Move up" and "Move
 *   down" — which docs/06-frontend.md requires, because a pointer-only reorder is
 *   unusable without a mouse.
 */

import { ChevronDown, ChevronUp, MoreHorizontal } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { Task, TaskState } from '@/lib/schemas'
import { transitionsFor } from '@/components/board/task-state'

interface TaskRowActionsProps {
  task: Task
  canMoveUp: boolean
  canMoveDown: boolean
  onSetState: (state: TaskState, postponedUntil?: string) => void
  onMove: (direction: -1 | 1) => void
  onSplit?: () => void
}

export function TaskRowActions({
  task,
  canMoveUp,
  canMoveDown,
  onSetState,
  onMove,
  onSplit,
}: TaskRowActionsProps) {
  const [pendingDate, setPendingDate] = useState(false)
  const options = transitionsFor(task.state)

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button variant="ghost" size="icon" aria-label={`Actions for ${task.title}`}>
              <MoreHorizontal className="size-4" aria-hidden="true" />
            </Button>
          }
        />
        <DropdownMenuContent align="end">
          {options.map((option) => (
            <DropdownMenuItem
              key={option.transition}
              variant={option.destructive ? 'destructive' : 'default'}
              onClick={() => {
                if (option.needsDate) setPendingDate(true)
                else onSetState(option.target)
              }}
            >
              {option.label}
            </DropdownMenuItem>
          ))}

          {onSplit && task.parentTaskId === null && !task.rollup ? (
            <>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={onSplit}>Split into subtasks…</DropdownMenuItem>
            </>
          ) : null}

          <DropdownMenuSeparator />
          {/* Keyboard fallback for drag-and-drop. */}
          <DropdownMenuItem disabled={!canMoveUp} onClick={() => onMove(-1)}>
            <ChevronUp className="size-4" aria-hidden="true" />
            Move up
          </DropdownMenuItem>
          <DropdownMenuItem disabled={!canMoveDown} onClick={() => onMove(1)}>
            <ChevronDown className="size-4" aria-hidden="true" />
            Move down
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {pendingDate ? (
        <PostponeUntilDialog
          onCancel={() => setPendingDate(false)}
          onConfirm={(iso) => {
            setPendingDate(false)
            onSetState('postponed_until', iso)
          }}
        />
      ) : null}
    </>
  )
}

function PostponeUntilDialog({
  onCancel,
  onConfirm,
}: {
  onCancel: () => void
  onConfirm: (iso: string) => void
}) {
  // Read the clock lazily, in the state initialiser: calling `Date.now()` during render
  // would make the default date change on every re-render.
  const [value, setValue] = useState(() =>
    new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString().slice(0, 10),
  )
  const [today] = useState(() => new Date().toISOString().slice(0, 10))

  return (
    <div
      role="dialog"
      aria-label="Postpone until"
      className="fixed inset-x-4 top-1/3 z-50 mx-auto max-w-sm rounded-lg border bg-card p-4 shadow-lg"
    >
      <label className="block text-sm font-medium" htmlFor="postpone-until">
        Postpone until
      </label>
      <input
        id="postpone-until"
        type="date"
        className="mt-2 w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
        value={value}
        min={today}
        onChange={(event) => setValue(event.target.value)}
      />
      <div className="mt-4 flex justify-end gap-2">
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button
          onClick={() => {
            // The server refuses a past timestamp, so send end-of-day in local time —
            // "postpone until tomorrow" means the whole of tomorrow, not 00:00.
            const date = new Date(`${value}T23:59:59`)
            onConfirm(date.toISOString())
          }}
        >
          Postpone
        </Button>
      </div>
    </div>
  )
}
