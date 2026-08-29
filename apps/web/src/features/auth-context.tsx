/**
 * Auth state as React context, plus the route guard.
 *
 * docs/06-frontend.md: "Auth is a route guard resolving the Identity Platform auth state
 * before first render to avoid a login flash." `status` starts at `resolving`, and the
 * guard renders nothing until it settles — so a signed-in user never sees `/login` flash
 * past on a reload.
 *
 * The `useAuth` hook lives in `use-auth.ts` so this file exports components only, which
 * is what keeps react-refresh able to hot-reload it.
 */

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import {
  AuthContext,
  type AuthContextValue,
  type AuthStatus,
} from '@/features/auth-context-value';
import { useAuth } from '@/features/use-auth';
import { getAuthProvider, type AuthUser } from '@/lib/auth';

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthStatus>('resolving');

  useEffect(() => {
    const provider = getAuthProvider();
    return provider.subscribe((next) => {
      setUser(next);
      setStatus(next ? 'signed-in' : 'signed-out');
    });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      status,
      user,
      mode: getAuthProvider().mode,
      signIn: () => getAuthProvider().signIn(),
      signInWithPassword: (email, password) =>
        getAuthProvider().signInWithPassword(email, password),
      signOut: () => getAuthProvider().signOut(),
    }),
    [status, user],
  );

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation();
  const auth = useAuth();

  if (auth.status === 'resolving') {
    // Deliberately blank rather than a spinner: this resolves in a few milliseconds and
    // a flashed spinner reads worse than nothing at all.
    return <div className="h-full" aria-busy="true" />;
  }
  if (auth.status === 'signed-out') {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  return <>{children}</>;
}
