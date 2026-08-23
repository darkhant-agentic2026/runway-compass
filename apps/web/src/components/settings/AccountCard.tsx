/**
 * Account — `/settings`. Display name (editable) and email (read-only).
 *
 * The display name is what the header shows in place of the signed-in email
 * (`components/layout/AppShell.tsx`) once set — see `PATCH /api/me` and
 * `display_name_customized` (docs/02-data-model.md#usersuid).
 */

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { usePatchDisplayName } from '@/features/queries';
import type { Me } from '@/lib/schemas';

interface AccountCardProps {
  me: Me;
}

export function AccountCard({ me }: AccountCardProps) {
  const patchDisplayName = usePatchDisplayName();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Account</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1">
          <Label htmlFor="display-name">Display name</Label>
          <Input
            id="display-name"
            // Remounts when the server value changes (a successful save, or a different
            // account loading in), so the field never shows stale text next to a value
            // that has already moved on. `defaultValue` alone only applies on mount.
            key={me.displayName ?? ''}
            defaultValue={me.displayName ?? ''}
            onBlur={(event) => {
              const value = event.target.value.trim();
              if (value && value !== me.displayName) {
                patchDisplayName.mutate(value);
              }
            }}
          />
          <p className="text-sm text-muted-foreground">
            Shown instead of your email throughout the app.
          </p>
        </div>

        <div className="space-y-1">
          <Label htmlFor="account-email">Email</Label>
          <Input id="account-email" value={me.email ?? ''} disabled readOnly />
        </div>
      </CardContent>
    </Card>
  );
}
