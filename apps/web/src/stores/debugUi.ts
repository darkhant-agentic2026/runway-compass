/**
 * Debugging toggles — client-only, per tab, persisted to `localStorage`.
 *
 * `showEventIds` is the one control so far: since the research/roadmap pipelines put
 * several named agents' events into one session (`research_planner`, `topic_researcher`,
 * `task_proposer`, `plan_tailor`, …), being able to see each message's raw ADK event id
 * next to its author is worth a switch a developer can flip without opening devtools.
 * Global rather than per-session, because the point is comparing events *across* a
 * transcript, not toggling it open and shut per pane.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface DebugUiState {
  showEventIds: boolean;
  toggleShowEventIds: () => void;
}

export const useDebugUiStore = create<DebugUiState>()(
  persist(
    (set) => ({
      showEventIds: false,
      toggleShowEventIds: () => set((state) => ({ showEventIds: !state.showEventIds })),
    }),
    {
      name: 'coach.debugUi',
      version: 1,
    },
  ),
);
