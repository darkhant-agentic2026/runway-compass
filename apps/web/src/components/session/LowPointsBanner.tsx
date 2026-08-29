/**
 * The low-points nag.
 *
 * docs/09-roadmap.md#research-concurrency: once the owner's remaining monthly points drop
 * under `runStartPointsThreshold + 100`, `turn_complete` starts carrying a hint
 * (`stores/usage.ts`) and this renders it — "You have 881 usage points remaining. Your
 * agent will not conduct research when usage points are below 800." Dismissing it is a
 * plain click; the store keeps that dismissal for the rest of the tab's session rather
 * than showing the same message again on the next turn.
 *
 * Self-contained, the same way `ConnectionBanner` is: every caller renders it and it
 * decides its own visibility, so a screen that wants the nag does not also need to read
 * the store itself.
 */

import { X } from 'lucide-react';

import { cn } from '@/lib/utils';
import { useUsageStore } from '@/stores/usage';

export function LowPointsBanner({ className }: { className?: string }) {
  const lowPoints = useUsageStore((state) => state.lowPoints);
  const dismissed = useUsageStore((state) => state.dismissed);
  const dismiss = useUsageStore((state) => state.dismiss);
  if (!lowPoints || dismissed) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid="low-points-banner"
      className={cn(
        'flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-foreground shadow-sm',
        className,
      )}
    >
      <p className="flex-1">
        You have {lowPoints.remaining} usage points remaining. Your agent will not conduct
        research when usage points are below {lowPoints.threshold}.
      </p>
      <button
        type="button"
        aria-label="Dismiss"
        className="shrink-0 text-muted-foreground hover:text-foreground"
        onClick={dismiss}
      >
        <X className="size-4" aria-hidden="true" />
      </button>
    </div>
  );
}
