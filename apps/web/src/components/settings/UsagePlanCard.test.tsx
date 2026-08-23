import { QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { UsagePlanCard } from '@/components/settings/UsagePlanCard';
import { createQueryClient } from '@/features/queries';
import { api, ApiError } from '@/lib/api';
import { makeUsageStatus } from '@/test/factories';

function renderCard(usage = makeUsageStatus()) {
  const queryClient = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(<UsagePlanCard usage={usage} />, { wrapper });
}

describe('UsagePlanCard', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('shows spend against limit for all three windows', () => {
    renderCard(
      makeUsageStatus({
        monthly: { spent: 120, limit: 500, resetsAt: '2026-09-01T00:00:00+00:00' },
        daily: { spent: 40, limit: 200, resetsAt: '2026-08-25T00:00:00+00:00' },
        fourHour: { spent: 80, limit: 80, resetsAt: '2026-08-24T20:00:00+00:00' },
      }),
    );

    expect(screen.getByText('120 / 500 points')).toBeInTheDocument();
    expect(screen.getByText('40 / 200 points')).toBeInTheDocument();
    expect(screen.getByText('80 / 80 points')).toBeInTheDocument();
    // The one window at its limit is the one labeled exhausted — not all three.
    expect(screen.getByText(/^Exhausted —/)).toBeInTheDocument();
    expect(screen.getAllByText(/^Resets /)).toHaveLength(2);
  });

  it('claims a coupon and clears the input on success', async () => {
    const claimSpy = vi.spyOn(api, 'claimCoupon').mockResolvedValue({
      plan: {
        tier: 'beta',
        limits: {
          autonomousRunsPerDay: 20,
          monthlyPoints: 5000,
          dailyPoints: 2000,
          fourHourPoints: 800,
        },
      },
    });

    renderCard();
    fireEvent.change(screen.getByLabelText('Have a beta coupon?'), {
      target: { value: 'BETA-XYZ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Claim' }));

    await waitFor(() => expect(claimSpy).toHaveBeenCalledWith('BETA-XYZ', expect.any(String)));
    await waitFor(() =>
      expect(screen.getByText('Coupon applied — limits updated.')).toBeInTheDocument(),
    );
    expect(screen.getByLabelText('Have a beta coupon?')).toHaveValue('');
  });

  it('shows the server error on a rejected claim', async () => {
    vi.spyOn(api, 'claimCoupon').mockRejectedValue(
      new ApiError(409, {
        type: '/problems/conflict',
        title: 'Conflict',
        status: 409,
        detail: 'This coupon has already been claimed.',
      }),
    );

    renderCard();
    fireEvent.change(screen.getByLabelText('Have a beta coupon?'), {
      target: { value: 'USED' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Claim' }));

    await waitFor(() =>
      expect(screen.getByText('This coupon has already been claimed.')).toBeInTheDocument(),
    );
  });
});
