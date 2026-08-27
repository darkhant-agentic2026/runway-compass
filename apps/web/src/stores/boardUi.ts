/**
 * Board UI state — client-only, per tab, persisted to `localStorage`.
 *
 * docs/06-frontend.md draws the line: "TanStack Query owns anything the server can also
 * change. Zustand owns anything that exists only in this tab." Filters and collapsed
 * parents are the second kind — no other device or session has an opinion about them.
 *
 * Filters are per project, because "hide completed" on a finished project and on one
 * just starting are different wishes.
 */

import { create } from 'zustand';
import { persist } from 'zustand/middleware';

export interface BoardFilters {
  /** docs/06-frontend.md: default **on**. */
  hideCompleted: boolean;
  /** Default on. */
  hideDiscarded: boolean;
  /** Default **off**. */
  hidePostponed: boolean;
}

export const DEFAULT_FILTERS: BoardFilters = {
  hideCompleted: false,
  hideDiscarded: true,
  hidePostponed: false,
};

interface BoardUiState {
  filtersByProject: Record<string, BoardFilters>;
  collapsedParents: Record<string, boolean>;
  filtersFor: (projectId: string) => BoardFilters;
  toggleFilter: (projectId: string, filter: keyof BoardFilters) => void;
  isCollapsed: (taskId: string) => boolean;
  toggleCollapsed: (taskId: string) => void;
}

export const useBoardUiStore = create<BoardUiState>()(
  persist(
    (set, get) => ({
      filtersByProject: {},
      collapsedParents: {},

      filtersFor(projectId) {
        return get().filtersByProject[projectId] ?? DEFAULT_FILTERS;
      },

      toggleFilter(projectId, filter) {
        const current = get().filtersFor(projectId);
        set((state) => ({
          filtersByProject: {
            ...state.filtersByProject,
            [projectId]: { ...current, [filter]: !current[filter] },
          },
        }));
      },

      isCollapsed(taskId) {
        // Parents start expanded: the subtasks are the point of a split.
        return get().collapsedParents[taskId] ?? false;
      },

      toggleCollapsed(taskId) {
        set((state) => ({
          collapsedParents: {
            ...state.collapsedParents,
            [taskId]: !(state.collapsedParents[taskId] ?? false),
          },
        }));
      },
    }),
    {
      // Unlike the theme, this key is read only by Zustand, so the `persist` envelope is
      // fine here. See `stores/theme.ts` for why the theme is the exception.
      name: 'coach.boardUi',
      version: 1,
    },
  ),
);
