/**
 * The visible reconnecting state.
 *
 * docs/06-frontend.md#task-workspace: "A visible **reconnecting** state that makes the
 * resume guarantee legible: 'Connection lost — your coach is still working.
 * Reconnecting…' then the stream continues from where it left off."
 *
 * The wording matters as much as the presence of the banner. The guarantee is that
 * generation survives the disconnect, and a user who is told "connection lost" without
 * being told the work continues will assume the opposite and start over — which is
 * exactly the wasted inference the design exists to avoid.
 */

import { Loader2 } from 'lucide-react';

import { isDisconnected, useSocketStore } from '@/stores/socket';

export function ConnectionBanner() {
  const connection = useSocketStore((state) => state.connection);
  if (!isDisconnected(connection)) return null;

  return (
    <div
      role="status"
      // `polite`, not `assertive`: a reconnect is not an emergency, and interrupting a
      // screen reader mid-sentence to say so would be worse than the disconnect.
      aria-live="polite"
      data-testid="connection-banner"
      className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-foreground"
    >
      <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden="true" />
      <span>
        {connection === 'failed'
          ? 'Connection lost — your coach is still working. Retrying…'
          : 'Connection lost — your coach is still working. Reconnecting…'}
      </span>
    </div>
  );
}
