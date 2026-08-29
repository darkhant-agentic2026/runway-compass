/**
 * The low-points nag.
 *
 * docs/09-roadmap.md#research-concurrency. Behaviour worth pinning: it renders nothing
 * without a hint, shows the exact wording once one arrives, and dismissing it hides the
 * banner without clearing the store's `lowPoints` value (`stores/usage.ts`'s own test
 * covers dismissal staying sticky across later hints).
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { LowPointsBanner } from '@/components/session/LowPointsBanner';
import { useUsageStore } from '@/stores/usage';

beforeEach(() => {
  useUsageStore.setState({ lowPoints: null, dismissed: false });
});

describe('LowPointsBanner', () => {
  it('renders nothing without a low-points hint', () => {
    render(<LowPointsBanner />);
    expect(screen.queryByTestId('low-points-banner')).not.toBeInTheDocument();
  });

  it('shows the remaining points and the threshold', () => {
    useUsageStore.setState({ lowPoints: { remaining: 881, threshold: 800 }, dismissed: false });
    render(<LowPointsBanner />);

    expect(
      screen.getByText(
        'You have 881 usage points remaining. Your agent will not conduct research when usage points are below 800.',
      ),
    ).toBeInTheDocument();
  });

  it('hides on dismiss', async () => {
    useUsageStore.setState({ lowPoints: { remaining: 881, threshold: 800 }, dismissed: false });
    render(<LowPointsBanner />);

    await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(screen.queryByTestId('low-points-banner')).not.toBeInTheDocument();
    expect(useUsageStore.getState().dismissed).toBe(true);
  });
});
