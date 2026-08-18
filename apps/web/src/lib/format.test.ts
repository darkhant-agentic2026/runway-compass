/** docs/08-testing.md: "Duration formatting edge cases (0, 45, 60, 90, 150 min)." */

import { describe, expect, it } from 'vitest';

import { formatMinutes, pluralize } from '@/lib/format';

describe('formatMinutes', () => {
  it.each([
    [0, '0 min'],
    [1, '1 min'],
    [45, '45 min'],
    [59, '59 min'],
    [60, '1 h'],
    [90, '1 h 30 m'],
    [120, '2 h'],
    [150, '2 h 30 m'],
    [1440, '24 h'],
  ])('formats %i as %s', (minutes, expected) => {
    expect(formatMinutes(minutes)).toBe(expected);
  });

  it('treats negative and non-finite values as nothing rather than throwing', () => {
    expect(formatMinutes(-5)).toBe('0 min');
    expect(formatMinutes(Number.NaN)).toBe('0 min');
    expect(formatMinutes(Number.POSITIVE_INFINITY)).toBe('0 min');
  });

  it('rounds fractional minutes', () => {
    expect(formatMinutes(44.6)).toBe('45 min');
  });
});

describe('pluralize', () => {
  it.each([
    [0, '0 subtasks'],
    [1, '1 subtask'],
    [4, '4 subtasks'],
  ])('%i renders as %s', (count, expected) => {
    expect(pluralize(count, 'subtask')).toBe(expected);
  });
});
