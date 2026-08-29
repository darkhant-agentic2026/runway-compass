/**
 * A message's own usage cost, on a tooltip behind an info icon beside `CopyMessage`.
 *
 * `points` comes from the stored event's `usage_metadata` (`lib/transcript.ts`'s
 * `pointsOf`) — only present on a model-response event, and only since M10, so an older
 * turn or a tool-only event simply has no icon (`Transcript.tsx` skips rendering this
 * component when `points` is `null`, rather than this component rendering something empty).
 */

import { Info } from 'lucide-react';

import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';

export function MessageCost({ points }: { points: number }) {
  const label = `${points} usage point${points === 1 ? '' : 's'}`;
  return (
    <Tooltip>
      {/*
        The count lives in `aria-label`, not only in the tooltip's own popup content: a
        popup that mounts on hover/focus is easy to miss for a screen reader without
        careful `aria-describedby` wiring, and the button's accessible name is the one
        thing every assistive technology reads unconditionally.
      */}
      <TooltipTrigger
        render={
          <button
            type="button"
            aria-label={label}
            className="flex size-6 items-center justify-center rounded text-muted-foreground opacity-60 hover:opacity-100"
          />
        }
        data-testid="message-cost"
      >
        <Info className="size-3.5" aria-hidden="true" />
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}
