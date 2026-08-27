import { beforeEach, describe, expect, it } from 'vitest';

import { useDebugUiStore } from '@/stores/debugUi';

describe('useDebugUiStore', () => {
  beforeEach(() => {
    localStorage.clear();
    useDebugUiStore.setState({ showEventIds: false });
  });

  it('starts off', () => {
    expect(useDebugUiStore.getState().showEventIds).toBe(false);
  });

  it('flips on toggle, and flips back', () => {
    useDebugUiStore.getState().toggleShowEventIds();
    expect(useDebugUiStore.getState().showEventIds).toBe(true);

    useDebugUiStore.getState().toggleShowEventIds();
    expect(useDebugUiStore.getState().showEventIds).toBe(false);
  });

  it('survives a remount, like every other persisted UI toggle in this app', () => {
    useDebugUiStore.getState().toggleShowEventIds();
    expect(localStorage.getItem('coach.debugUi')).toContain('"showEventIds":true');
  });
});
