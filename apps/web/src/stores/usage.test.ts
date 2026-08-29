/**
 * `useUsageStore` — the low-points nag's account-scoped state.
 *
 * docs/09-roadmap.md#research-concurrency. The one rule worth pinning: dismissal is
 * sticky once set, even as `setLowPoints` keeps being called with fresh hints from later
 * turns — that stickiness is what keeps the banner from reappearing on every turn while
 * points stay low.
 */

import { beforeEach, describe, expect, it } from 'vitest';

import { useUsageStore } from '@/stores/usage';

beforeEach(() => {
  useUsageStore.setState({ lowPoints: null, dismissed: false });
});

describe('setLowPoints', () => {
  it('stores the hint', () => {
    useUsageStore.getState().setLowPoints({ remaining: 640, threshold: 800 });
    expect(useUsageStore.getState().lowPoints).toEqual({ remaining: 640, threshold: 800 });
  });

  it('clears a stale hint when passed null', () => {
    useUsageStore.getState().setLowPoints({ remaining: 640, threshold: 800 });
    useUsageStore.getState().setLowPoints(null);
    expect(useUsageStore.getState().lowPoints).toBeNull();
  });
});

describe('dismiss', () => {
  it('is sticky across later hints, so a dismissed nag does not return on the next turn', () => {
    useUsageStore.getState().setLowPoints({ remaining: 640, threshold: 800 });
    useUsageStore.getState().dismiss();
    expect(useUsageStore.getState().dismissed).toBe(true);

    useUsageStore.getState().setLowPoints({ remaining: 600, threshold: 800 });
    expect(useUsageStore.getState().dismissed).toBe(true);
  });
});
