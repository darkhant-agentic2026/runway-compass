/**
 * A task state's label, coloured and iconed for the two states that earn a hint —
 * `completed` and `in_progress` (`STATE_ACCENT`, `task-state.ts`). Every other state
 * renders as plain text.
 *
 * Shared between `TaskCard`'s bare inline text and `TaskInfoStrip`'s `Badge` pill
 * (`pages/TaskWorkspacePage.tsx`) so the two read the same colour and icon rather than
 * drifting apart. Rendered inside its own `<span>` rather than as a bare icon + text pair,
 * so a caller that wraps this in a `Badge` never makes the icon a *direct* child of it —
 * `badge.tsx`'s `[&>svg]:size-3!` would otherwise force the in-progress dot back up to a
 * card icon's size.
 */

import { STATE_ACCENT, STATE_LABELS } from '@/components/board/task-state';
import type { TaskState } from '@/lib/schemas';
import { cn } from '@/lib/utils';

export function StateLabel({ state }: { state: TaskState }) {
  const accent = STATE_ACCENT[state];
  if (!accent) {
    return <span data-testid="state-badge">{STATE_LABELS[state]}</span>;
  }
  const Icon = accent.icon;
  return (
    <span
      data-testid="state-badge"
      className={cn('inline-flex items-center gap-1', accent.className)}
    >
      <Icon className={cn('size-3', accent.iconClassName)} aria-hidden="true" />
      {STATE_LABELS[state]}
    </span>
  );
}
