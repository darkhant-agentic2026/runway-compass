import { createContext } from 'react';

import type { AuthUser } from '@/lib/auth';

export type AuthStatus = 'resolving' | 'signed-in' | 'signed-out';

export interface AuthContextValue {
  status: AuthStatus;
  user: AuthUser | null;
  mode: 'dev' | 'identity-platform';
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue | null>(null);
