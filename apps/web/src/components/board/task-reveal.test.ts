/**
 * The sequencing math for a new board card's entrance, and the loading-race
 * `useTaskRevealDelays` has to get right: `data` is `undefined` both before the board's
 * first load and while a not-yet-fetched filter combination is loading, and those two must
 * never be confused with "the board has loaded and genuinely has zero tasks" — the latter
 * would make every task in the next real response look new the instant it arrives.
 */
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  computeRevealDelays,
  LETTER_INTERVAL_MS,
  titleRevealMs,
  useDescriptionReveal,
  useTaskRevealDelays,
} from '@/components/board/task-reveal';

interface HookProps {
  data?: { id: string; title: string }[];
}

describe('titleRevealMs', () => {
  it('grows with the title length, at the configured letters-per-second', () => {
    const short = titleRevealMs('Hi');
    const long = titleRevealMs('A much longer task title');
    expect(long).toBeGreaterThan(short);
    expect(long - short).toBeCloseTo(
      ([...'A much longer task title'].length - [...'Hi'].length) * LETTER_INTERVAL_MS,
      5,
    );
  });
});

describe('computeRevealDelays', () => {
  it('marks nothing new when seenIds is null — the board has never loaded', () => {
    const delays = computeRevealDelays([{ id: 't1', title: 'Read chapter 1' }], null);
    expect(delays.size).toBe(0);
  });

  it('marks nothing new for a task already in seenIds', () => {
    const delays = computeRevealDelays(
      [{ id: 't1', title: 'Read chapter 1' }],
      new Set(['t1']),
    );
    expect(delays.size).toBe(0);
  });

  it('gives each new task in a batch the previous ones’ cumulative title-reveal time', () => {
    const tasks = [
      { id: 't1', title: 'Old task' },
      { id: 't2', title: 'First new' },
      { id: 't3', title: 'Second new' },
    ];
    const delays = computeRevealDelays(tasks, new Set(['t1']));

    expect(delays.has('t1')).toBe(false);
    expect(delays.get('t2')).toBe(0);
    expect(delays.get('t3')).toBe(titleRevealMs('First new'));
  });
});

describe('useTaskRevealDelays', () => {
  it('never animates the board’s first load, even though the loading render has no data', () => {
    const { result, rerender } = renderHook<Map<string, number>, HookProps>(
      ({ data }) => useTaskRevealDelays(data),
      { initialProps: { data: undefined } },
    );

    // The loading render: `board.data` is still `undefined`.
    expect(result.current.size).toBe(0);

    // The real response lands — nothing in it should read as "new".
    act(() => {
      rerender({
        data: [
          { id: 't1', title: 'Already there' },
          { id: 't2', title: 'Also already there' },
        ],
      });
    });
    expect(result.current.size).toBe(0);
  });

  it('animates a task that appears after the board already has data', () => {
    const { result, rerender } = renderHook<Map<string, number>, HookProps>(
      ({ data }) => useTaskRevealDelays(data),
      { initialProps: { data: [{ id: 't1', title: 'Already there' }] } },
    );
    expect(result.current.size).toBe(0);

    act(() => {
      rerender({
        data: [
          { id: 't1', title: 'Already there' },
          { id: 't2', title: 'Brand new' },
        ],
      });
    });
    expect(result.current.has('t1')).toBe(false);
    expect(result.current.get('t2')).toBe(0);
  });

  it('does not treat a still-loading filter change as "zero tasks, seen"', () => {
    // Regression: an effect keyed on the tasks fallback rather than on `data` itself would
    // seed the seen set from the `[]` the loading render falls back to, making the real
    // response look entirely new.
    const { result, rerender } = renderHook<Map<string, number>, HookProps>(
      ({ data }) => useTaskRevealDelays(data),
      { initialProps: { data: [{ id: 't1', title: 'Already there' }] } },
    );
    expect(result.current.size).toBe(0);

    // Switching filters: the new query key hasn't resolved yet.
    act(() => {
      rerender({ data: undefined });
    });
    expect(result.current.size).toBe(0);

    // It resolves to the same task, now joined by the filter's own view of what's there.
    act(() => {
      rerender({ data: [{ id: 't1', title: 'Already there' }] });
    });
    expect(result.current.size).toBe(0);
  });
});

describe('useDescriptionReveal', () => {
  interface DescriptionProps {
    description?: string;
  }

  it('never animates a task that already has a description on first load', () => {
    const { result, rerender } = renderHook<boolean, DescriptionProps>(
      ({ description }) => useDescriptionReveal(description),
      { initialProps: { description: undefined } },
    );
    expect(result.current).toBe(false);

    // The task loads with a description already set — not an appearance, a first load.
    act(() => {
      rerender({ description: 'Ship a worker pool that survives cancellation.' });
    });
    expect(result.current).toBe(false);
  });

  it('never animates a task that loads with no description at all', () => {
    const { result, rerender } = renderHook<boolean, DescriptionProps>(
      ({ description }) => useDescriptionReveal(description),
      { initialProps: { description: undefined } },
    );
    act(() => {
      rerender({ description: '' });
    });
    expect(result.current).toBe(false);
  });

  it('animates a description that appears while the page is already open', () => {
    const { result, rerender } = renderHook<boolean, DescriptionProps>(
      ({ description }) => useDescriptionReveal(description),
      { initialProps: { description: '' } },
    );
    expect(result.current).toBe(false);

    // Plan materialization writes the description while the workspace is still mounted.
    act(() => {
      rerender({ description: 'Ship a worker pool that survives cancellation.' });
    });
    expect(result.current).toBe(true);
  });

  it('does not re-trigger on a later edit to an already-present description', () => {
    const { result, rerender } = renderHook<boolean, DescriptionProps>(
      ({ description }) => useDescriptionReveal(description),
      { initialProps: { description: '' } },
    );
    act(() => {
      rerender({ description: 'First version.' });
    });
    expect(result.current).toBe(true);

    act(() => {
      rerender({ description: 'Edited version.' });
    });
    // Still true — the flag never resets — but nothing here asserts it *replays*, matching
    // the same "an insertion, not a general mutation" rule the board's own reveal draws.
    expect(result.current).toBe(true);
  });
});
