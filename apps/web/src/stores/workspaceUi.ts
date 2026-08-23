/**
 * Task workspace UI state — client-only, per tab, persisted to `localStorage`.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid: the task-detail column
 * (description, subtasks/checklist, research card) can be collapsed to give the chat more
 * room. Expanded by default, always — the toggle is purely something the learner reaches
 * for, never something that happens under them, and a choice made for one task is
 * remembered the next time that same task is opened.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

interface WorkspaceUiState {
  /** Only present once the learner has explicitly toggled a given task. */
  detailsCollapsedByTask: Record<string, boolean>;
  isDetailsCollapsed: (taskId: string) => boolean;
  toggleDetails: (taskId: string) => void;
}

export const useWorkspaceUiStore = create<WorkspaceUiState>()(
  persist(
    (set, get) => ({
      detailsCollapsedByTask: {},

      isDetailsCollapsed(taskId) {
        return get().detailsCollapsedByTask[taskId] ?? false;
      },

      toggleDetails(taskId) {
        set((state) => ({
          detailsCollapsedByTask: {
            ...state.detailsCollapsedByTask,
            [taskId]: !state.isDetailsCollapsed(taskId),
          },
        }));
      },
    }),
    {
      name: 'coach.workspaceUi',
      version: 1,
    },
  ),
);
