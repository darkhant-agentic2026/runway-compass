/**
 * The per-message usage-cost tooltip, beside the copy control.
 *
 * `Transcript.tsx` only renders this when `points` is non-null, so there is nothing here
 * about the "no metadata" case — that is simply the component never mounting. The count
 * is asserted through the button's accessible name rather than by hovering to reveal the
 * tooltip popup: that name is what every assistive technology reads unconditionally,
 * where the popup itself needs careful `aria-describedby` wiring to be as reliable.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MessageCost } from '@/components/session/MessageCost';

describe('MessageCost', () => {
  it('names the point count', () => {
    render(<MessageCost points={3} />);
    expect(screen.getByRole('button', { name: '3 usage points' })).toBeInTheDocument();
  });

  it('uses the singular for one point', () => {
    render(<MessageCost points={1} />);
    expect(screen.getByRole('button', { name: '1 usage point' })).toBeInTheDocument();
  });
});
