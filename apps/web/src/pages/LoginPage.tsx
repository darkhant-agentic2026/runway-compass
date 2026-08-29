/**
 * Sign in — `/login`.
 *
 * Two sign-in methods in deployed environments, both through Identity Platform: Google's
 * popup, and email/password for accounts created by hand in the Identity Platform console
 * (no self-serve sign-up screen — an operator hands out the credentials). Under
 * `VITE_AUTH_MODE=dev` a single button mints a `dev:<uid>` token instead, which is what
 * the local loop and the Playwright fixture use — and which the server accepts only when
 * `ENV=local` (docs/04-api-contract.md#authentication).
 *
 * The login screen needs a theme too, which is why the theme is read from `localStorage`
 * before auth resolves rather than from `globalPrefs`.
 */

import { useEffect, useState, type FormEvent } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAuth } from '@/features/use-auth';

export default function LoginPage() {
  const auth = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? '/';
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  /**
   * Sign-in failures have to be visible.
   *
   * Identity Platform rejects with a coded error — `auth/unauthorized-domain`,
   * `auth/invalid-api-key`, `auth/popup-blocked`, `auth/invalid-credential` — and every one
   * of them is a problem the person looking at the screen can act on. Discarding the
   * rejection leaves a login page that silently does nothing after an attempt that
   * apparently succeeded, which is indistinguishable from the app being broken.
   */
  async function attempt(action: () => Promise<void>): Promise<void> {
    setError(null);
    setBusy(true);
    try {
      await action();
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

  function signInWithGoogle(): Promise<void> {
    return attempt(() => auth.signIn());
  }

  function submitPassword(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    void attempt(() => auth.signInWithPassword(email, password));
  }

  useEffect(() => {
    if (auth.status === 'signed-in') void navigate(from, { replace: true });
  }, [auth.status, from, navigate]);

  if (auth.status === 'signed-in') return <Navigate to={from} replace />;

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Runway Compass</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Turn a technical goal into bite-sized tasks, with a coach that prepares the material
            before you sit down.
          </p>
          <Button
            className="w-full"
            disabled={busy || auth.status === 'resolving'}
            onClick={() => void signInWithGoogle()}
          >
            {auth.mode === 'dev' ? 'Continue as the local dev user' : 'Sign in with Google'}
          </Button>

          {auth.mode === 'identity-platform' ? (
            <>
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <div className="h-px flex-1 bg-border" />
                or
                <div className="h-px flex-1 bg-border" />
              </div>
              <form className="space-y-3" onSubmit={submitPassword}>
                <div className="space-y-1">
                  <Label htmlFor="email">Email</Label>
                  <Input
                    id="email"
                    type="email"
                    autoComplete="email"
                    required
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="password">Password</Label>
                  <Input
                    id="password"
                    type="password"
                    autoComplete="current-password"
                    required
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </div>
                <Button
                  type="submit"
                  variant="secondary"
                  className="w-full"
                  disabled={busy || auth.status === 'resolving'}
                >
                  Sign in with email
                </Button>
              </form>
            </>
          ) : null}

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
