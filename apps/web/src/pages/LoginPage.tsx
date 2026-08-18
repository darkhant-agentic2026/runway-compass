/**
 * Sign in — `/login`.
 *
 * Google sign-in through Identity Platform's popup in deployed environments. Under
 * `VITE_AUTH_MODE=dev` the same button mints a `dev:<uid>` token instead, which is what
 * the local loop and the Playwright fixture use — and which the server accepts only when
 * `ENV=local` (docs/04-api-contract.md#authentication).
 *
 * The login screen needs a theme too, which is why the theme is read from `localStorage`
 * before auth resolves rather than from `globalPrefs`.
 */

import { useEffect, useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useAuth } from '@/features/use-auth';

export default function LoginPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? '/';
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /**
   * Sign-in failures have to be visible.
   *
   * Identity Platform rejects with a coded error — `auth/unauthorized-domain`,
   * `auth/invalid-api-key`, `auth/popup-blocked` — and every one of them is a
   * configuration problem the person looking at the screen can fix. Discarding the
   * rejection leaves a login page that silently does nothing after a popup that
   * apparently succeeded, which is indistinguishable from the app being broken.
   */
  async function signIn(): Promise<void> {
    setError(null);
    setBusy(true);
    try {
      await auth.signIn();
    } catch (cause) {
      const code =
        typeof cause === 'object' && cause !== null && 'code' in cause
          ? String((cause as { code: unknown }).code)
          : null;
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(code ? `${code} — ${message}` : message);
      // Also on the console, where the full object with its stack is more useful than
      // anything that fits on a card.
      console.error('sign-in failed', cause);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (auth.status === 'signed-in') void navigate(from, { replace: true });
  }, [auth.status, from, navigate]);

  if (auth.status === 'signed-in') return <Navigate to={from} replace />;

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Self-Study Coach</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Turn a technical goal into bite-sized tasks, with a coach that prepares the material
            before you sit down.
          </p>
          <Button
            className="w-full"
            disabled={busy || auth.status === 'resolving'}
            onClick={() => void signIn()}
          >
            {auth.mode === 'dev' ? 'Continue as the local dev user' : 'Sign in with Google'}
          </Button>

          {error ? (
            <p
              role="alert"
              className="rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs break-words text-destructive"
              data-testid="signin-error"
            >
              {error}
            </p>
          ) : null}
          {auth.mode === 'dev' ? (
            <p className="text-xs text-muted-foreground">
              Local development: this signs in with a <code>dev:</code> token, which the API
              accepts only when <code>ENV=local</code>.
            </p>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
