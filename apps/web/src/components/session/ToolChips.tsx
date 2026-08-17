/**
 * Tool activity, as inline status chips.
 *
 * docs/06-frontend.md#task-workspace: "tool activity as inline status chips ('Searching
 * the web…', 'Checking video lengths…') built from `tool_call`/`tool_result`."
 *
 * A chip opens on `tool_call` and closes on the matching `tool_result`, so an unfinished
 * one spins — which is the only signal a user gets that the coach is working rather than
 * stalled during a long tool step.
 */

import { Check, Loader2, X } from 'lucide-react'

import { labelForTool } from '@/lib/tool-labels'
import type { ToolChip } from '@/stores/stream'

export function ToolChips({ tools }: { tools: ToolChip[] }) {
  if (tools.length === 0) return null

  return (
    <ul className="flex flex-wrap gap-1.5" data-testid="tool-chips">
      {tools.map((chip) => (
        <li
          key={chip.seq}
          className="bg-muted text-muted-foreground flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs"
        >
          {!chip.done ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : chip.ok ? (
            <Check className="size-3" aria-hidden="true" />
          ) : (
            <X className="text-destructive size-3" aria-hidden="true" />
          )}
          <span>
            {labelForTool(chip.name)}
            {chip.done ? '' : '…'}
          </span>
        </li>
      ))}
    </ul>
  )
}
