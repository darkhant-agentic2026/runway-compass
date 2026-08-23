/**
 * Usage & plan — `/settings`. M8-quotas.
 *
 * Three meters (monthly, daily, 4-hour), each a spend against a limit, plus a coupon
 * redeem form. A window at or past its limit is the one place this reaches for the
 * destructive token rather than primary — reserved for that state, never a fourth
 * "series" color, so a learner scanning three bars reads "this one is the problem" from
 * color alone only where that is actually true.
 */

import { useState } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useClaimCoupon } from '@/features/queries';
import { ApiError } from '@/lib/api';
import type { UsageStatus, UsageWindow } from '@/lib/schemas';

interface UsagePlanCardProps {
  usage: UsageStatus;
}

const WINDOWS: Array<{ key: keyof UsageStatus; label: string; countdown: boolean }> = [
  { key: 'monthly', label: 'Monthly', countdown: false },
  { key: 'daily', label: 'Daily', countdown: true },
  { key: 'fourHour', label: '4-hour', countdown: true },
];

/** "0h 34m" until `resetsAt`. Rounds up, so "a few seconds left" never reads as "0h 0m". */
function remainingLabel(resetsAt: string): string {
  const totalMinutes = Math.max(
    0,
    Math.ceil((new Date(resetsAt).getTime() - Date.now()) / 60_000),
  );
  return `${Math.floor(totalMinutes / 60)}h ${totalMinutes % 60}m`;
}

function Meter({
  label,
  window,
  countdown,
}: {
  label: string;
  window: UsageWindow;
  countdown: boolean;
}) {
  const fraction = window.limit > 0 ? window.spent / window.limit : 1;
  const exhausted = window.spent >= window.limit;
  const width = `${Math.min(100, Math.max(0, fraction * 100))}%`;

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium">{label}</span>
        <span className="text-muted-foreground">
          {window.spent} / {window.limit} points
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted" role="presentation">
        <div
          className={`h-full rounded-full ${exhausted ? 'bg-destructive' : 'bg-primary'}`}
          style={{ width }}
        />
      </div>
      <p className="text-xs text-muted-foreground">
        {exhausted ? 'Exhausted — ' : ''}Resets {new Date(window.resetsAt).toLocaleString()}
        {countdown ? (
          <>
            {' '}
            (in <strong>{remainingLabel(window.resetsAt)}</strong> from now)
          </>
        ) : null}
      </p>
    </div>
  );
}

export function UsagePlanCard({ usage }: UsagePlanCardProps) {
  const claim = useClaimCoupon();
  const [code, setCode] = useState('');

  function submit() {
    const trimmed = code.trim();
    if (!trimmed) return;
    claim.mutate(trimmed, { onSuccess: () => setCode('') });
  }

  const errorMessage =
    claim.error instanceof ApiError
      ? claim.error.problem.detail
      : 'Could not claim this coupon.';

  return (
    <Card>
      <CardHeader>
        <CardTitle>Usage & plan</CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {WINDOWS.map(({ key, label, countdown }) => (
          <Meter key={key} label={label} window={usage[key]} countdown={countdown} />
        ))}

        <div className="space-y-1 border-t pt-4">
          <Label htmlFor="coupon-code">Have a beta coupon?</Label>
          <div className="flex gap-2">
            <Input
              id="coupon-code"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') submit();
              }}
              placeholder="Enter code"
              disabled={claim.isPending}
            />
            <Button type="button" onClick={submit} disabled={claim.isPending || !code.trim()}>
              Claim
            </Button>
          </div>
          {claim.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {errorMessage}
            </p>
          ) : null}
          {claim.isSuccess ? (
            <p className="text-sm text-muted-foreground">Coupon applied — limits updated.</p>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}
