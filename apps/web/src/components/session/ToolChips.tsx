/**
 * Tool activity, as inline status chips.
 *
 * docs/06-frontend.md#task-workspace: "tool activity as inline status chips ('Searching
 * the web…', 'Checking video lengths…') built from `tool_call`/`tool_result`."
 *
 * Rendered from two sources and deliberately identical in both: the live stream, where a
 * chip opens on `tool_call` and closes on the matching `tool_result` so an unfinished one
 * spins; and the stored transcript, where every chip is already settled. The second is
 * what makes the record survive `turn_complete`, a reload, and coming back to the session
 * tomorrow — without it, a finished turn erases every sign that the coach touched the
 * board.
 *
 * Three outcomes, not two. `ok: null` is "no outcome recorded" — an interrupted turn, or
 * a call still waiting on the learner's confirmation — and it renders as neither a tick
 * nor a cross, because both would be a claim the transcript cannot support.
 */

import { Check, Dot, Loader2, X } from 'lucide-react';

import { labelForTool } from '@/lib/tool-labels';

export interface ChipView {
  /** Unique within one list; the React key. */
  id: string;
  name: string;
  done: boolean;
  ok: boolean | null;
  /** A one-line summary of what the call did; `''` when the tool has no summariser. */
  detail: string;
}

export function ToolChips({ tools }: { tools: ChipView[] }) {
  if (tools.length === 0) return null;

  return (
    <ul className="flex flex-wrap gap-1.5" data-testid="tool-chips">
      {tools.map((chip) => (
        <li
          key={chip.id}
          data-tool={chip.name}
          className="flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1 text-xs text-muted-foreground"
        >
          {!chip.done ? (
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          ) : chip.ok === null ? (
            <Dot className="size-3" aria-hidden="true" />
          ) : chip.ok ? (
            <Check className="size-3" aria-hidden="true" />
          ) : (
            <X className="size-3 text-destructive" aria-hidden="true" />
          )}
          <span>
            {labelForTool(chip.name)}
            {chip.done ? '' : '…'}
          </span>
          {/*
            The detail, when there is one. Separated by a middot and dimmed rather than put
            on its own line: a chip is a status, and a two-line chip in a stream of them
            reads as a list of paragraphs. `title` carries the untruncated text, because
            `max-w` will clip a long one and the whole value of the detail is being able to
            read it.
          */}
          {chip.detail ? (
            <span
              className="max-w-[22rem] truncate text-muted-foreground/80"
              title={chip.detail}
              data-testid="chip-detail"
            >
              · {chip.detail}
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
