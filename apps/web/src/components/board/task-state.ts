/**
 * The state machine, client side.
 *
 * A transcription of `apps/api/src/coach/services/state_machine.py`, which is itself a
 * transcription of the diagram in docs/02-data-model.md. The board uses it to decide
 * which row actions to offer: only legal transitions are shown, so "Complete" never
 * appears on a task that has not been started and cannot produce a 409.
 *
 * The server remains the authority — this is presentation, not enforcement.
 */

import type { TaskState } from '@/lib/schemas';

export type Transition =
  'start' | 'complete' | 'defer' | 'defer_until' | 'reopen' | 'restore' | 'discard';

export interface TransitionOption {
  transition: Transition;
  target: TaskState;
  label: string;
  /** Opens the date picker rather than firing immediately. */
  needsDate?: boolean;
  destructive?: boolean;
}

const BY_STATE: Record<TaskState, TransitionOption[]> = {
  not_started: [{ transition: 'start', target: 'current', label: 'Start' }],
  current: [
    { transition: 'complete', target: 'completed', label: 'Complete' },
    { transition: 'defer', target: 'postponed', label: 'Postpone' },
    {
      transition: 'defer_until',
      target: 'postponed_until',
      label: 'Postpone until…',
      needsDate: true,
    },
  ],
  completed: [{ transition: 'reopen', target: 'not_started', label: 'Reopen' }],
  postponed: [{ transition: 'restore', target: 'not_started', label: 'Un-postpone' }],
  postponed_until: [{ transition: 'restore', target: 'not_started', label: 'Un-postpone' }],
  discarded: [{ transition: 'restore', target: 'not_started', label: 'Restore' }],
};

/** `discard` is reachable from every state except `discarded`, where it is a no-op. */
export function transitionsFor(state: TaskState): TransitionOption[] {
  const base = BY_STATE[state] ?? [];
  if (state === 'discarded') return base;
  return [
    ...base,
    { transition: 'discard', target: 'discarded', label: 'Discard', destructive: true },
  ];
}

export const STATE_LABELS: Record<TaskState, string> = {
  not_started: 'Not started',
  // Not "Next up": that phrase belongs to the pin badge on the card
  // (docs/06-frontend.md), and having both say it makes the row read as if two different
  // things were being claimed.
  current: 'Started',
  completed: 'Completed',
  postponed: 'Postponed',
  postponed_until: 'Postponed until',
  discarded: 'Discarded',
};
