/**
 * `useUsageStore` — the low-points nag, account-scoped rather than per-turn.
 *
 * docs/09-roadmap.md#research-concurrency. Fed from `turn_complete`'s
 * `pointsRemaining`/`pointsThreshold` (`lib/socket.ts`), which is `null`/`null` on almost
 * every turn — set only once the owner's remaining monthly points drop under
 * `runStartPointsThreshold + 100`. A dedicated store rather than a field on
 * `stores/stream.ts`'s per-turn `StreamState`: points are a property of the *account*, not
 * of one turn, and a `StreamState` entry is dropped on the `turn_complete` handoff
 * (`SessionPane.tsx`) well before a banner reading it would get the chance.
 *
 * **Dismissal is sticky for the tab, not per-hint.** The whole point is to avoid nagging
 * the learner on every turn while points stay low — so once dismissed, the banner stays
 * gone even as later turns keep refreshing `lowPoints`, until a reload clears the store
 * (CLAUDE.md: no persistence asked for here, unlike `stores/theme.ts`).
 */

import { create } from 'zustand';

export interface LowPointsHint {
  remaining: number;
  threshold: number;
}

interface UsageStore {
  lowPoints: LowPointsHint | null;
  dismissed: boolean;
  setLowPoints: (hint: LowPointsHint | null) => void;
  dismiss: () => void;
}

export const useUsageStore = create<UsageStore>((set) => ({
  lowPoints: null,
  dismissed: false,
  setLowPoints: (hint) => set({ lowPoints: hint }),
  dismiss: () => set({ dismissed: true }),
}));
