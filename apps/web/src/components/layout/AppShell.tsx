import { Link, Outlet } from 'react-router-dom';

import { Button } from '@/components/ui/button';
import { useMe } from '@/features/queries';
import { useAuth } from '@/features/use-auth';
import { useCoachSocket } from '@/features/use-socket';

export function AppShell() {
  const auth = useAuth();
  const me = useMe();
  // One socket per tab, held open for as long as a user is signed in — not per screen,
  // so navigating between the board and a workspace never drops a running turn's stream
  // (docs/06-frontend.md#websocket-client).
  useCoachSocket();

  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b">
        <nav className="mx-auto flex w-full max-w-3xl items-center gap-3 p-3 sm:px-6">
          <Link to="/" className="font-semibold">
            Self-Study Coach
          </Link>
          <span className="flex-1" />

          {/*
            The signed-in identity, and M0's exit criterion in one element: originally
            "a signed-in user sees their email on a deployed dev URL" (docs/09-roadmap.md);
            since the display-name feature, the *visible* text prefers the learner's chosen
            name (falling back to email when none is set) and the email moves to the
            hover title — the round trip the criterion cares about is unchanged, only which
            field is primary.

            Read from `useMe()` — the server's answer — rather than from the auth context,
            which has the same value client-side from the token. Only the server's copy
            demonstrates the whole path: Identity Platform issued a token, the fetch
            wrapper attached it, `verify_id_token` accepted it offline, and the user
            document resolved. The client-side value would render identically while
            proving none of that.
          */}
          <span
            className="hidden max-w-[16rem] truncate text-sm text-muted-foreground sm:inline"
            title={me.data?.email ?? undefined}
            data-testid="signed-in-identity"
          >
            {me.data?.displayName || me.data?.email || ''}
          </span>

          <Button variant="ghost" render={<Link to="/settings" />}>
            Settings
          </Button>
          <Button variant="ghost" onClick={() => void auth.signOut()}>
            Sign out
          </Button>
        </nav>
      </header>
      <main className="flex-1">
        <Outlet />
      </main>
    </div>
  );
}
