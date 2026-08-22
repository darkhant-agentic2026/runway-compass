/**
 * Global settings — `/settings`.
 *
 * docs/06-frontend.md: "Global prefs, appearance (theme) + 'What your coach knows about
 * you' (learner profile, editable)".
 */

import { LearnerProfileEditor } from '@/components/settings/LearnerProfileEditor';
import { ThemeToggle } from '@/components/ThemeToggle';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { useMe, usePatchGlobalPrefs } from '@/features/queries';
import type { GlobalPrefs } from '@/lib/schemas';

const GUIDANCE_STYLES: GlobalPrefs['guidanceStyle'][] = ['socratic', 'direct', 'mixed'];
const VERBOSITIES: GlobalPrefs['verbosity'][] = ['terse', 'balanced', 'thorough'];

export default function SettingsPage() {
  const me = useMe();
  const patch = usePatchGlobalPrefs();
  const prefs = me.data?.globalPrefs;
  const profile = me.data?.learnerProfile;

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6 p-4 sm:p-6">
      <h1 className="text-2xl font-semibold">Settings</h1>

      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
        </CardHeader>
        <CardContent>
          <ThemeToggle />
          <p className="mt-3 text-sm text-muted-foreground">
            Saved on this device, so the sign-in screen matches too.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Coaching</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {!prefs ? (
            <p className="text-muted-foreground">Loading your preferences…</p>
          ) : (
            <>
              <div className="space-y-1">
                <Label htmlFor="default-minutes">Default task length (minutes)</Label>
                <Input
                  id="default-minutes"
                  type="number"
                  min={1}
                  max={1440}
                  defaultValue={prefs.defaultTaskMinutes}
                  onBlur={(event) => {
                    const value = Number(event.target.value);
                    if (value > 0 && value !== prefs.defaultTaskMinutes) {
                      patch.mutate({ defaultTaskMinutes: value });
                    }
                  }}
                />
                <p className="text-sm text-muted-foreground">
                  A project can override this in its own settings.
                </p>
              </div>

              <div className="space-y-1">
                <Label htmlFor="guidance-style">Guidance style</Label>
                <Select
                  value={prefs.guidanceStyle}
                  onValueChange={(value) =>
                    patch.mutate({ guidanceStyle: value as GlobalPrefs['guidanceStyle'] })
                  }
                >
                  <SelectTrigger id="guidance-style">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {GUIDANCE_STYLES.map((style) => (
                      <SelectItem key={style} value={style}>
                        {style}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1">
                <Label htmlFor="verbosity">Verbosity</Label>
                <Select
                  value={prefs.verbosity}
                  onValueChange={(value) =>
                    patch.mutate({ verbosity: value as GlobalPrefs['verbosity'] })
                  }
                >
                  <SelectTrigger id="verbosity">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {VERBOSITIES.map((verbosity) => (
                      <SelectItem key={verbosity} value={verbosity}>
                        {verbosity}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="space-y-1">
                <Label htmlFor="timezone">Timezone</Label>
                <Input
                  id="timezone"
                  defaultValue={prefs.timezone}
                  onBlur={(event) => {
                    const value = event.target.value.trim();
                    if (value && value !== prefs.timezone) patch.mutate({ timezone: value });
                  }}
                />
              </div>

              <div className="flex items-center gap-2">
                <Switch
                  id="autonomous"
                  checked={prefs.autonomousEnabled}
                  onCheckedChange={(checked) =>
                    patch.mutate({ autonomousEnabled: Boolean(checked) })
                  }
                />
                <Label htmlFor="autonomous" className="font-normal">
                  Let the coach prepare work while I&rsquo;m away
                </Label>
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {profile ? (
        <LearnerProfileEditor profile={profile} />
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>What your coach knows about you</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            <p>Loading your learner model…</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
