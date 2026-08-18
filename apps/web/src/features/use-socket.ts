/**
 * The socket's React lifetime: connect on auth, disconnect on sign-out.
 *
 * Split from `lib/socket.ts` so that module stays free of React and can be driven by a
 * fake `WebSocket` in tests. What lives here is only the wiring that needs a component
 * tree: when to connect, and where `board_update` frames go.
 */

import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';

import { useAuth } from '@/features/use-auth';
import { getSocket, resetSocket } from '@/lib/socket';

/**
 * Hold one socket open while a user is signed in.
 *
 * `board_update` is turned into a TanStack Query invalidation rather than a cache patch —
 * "the server tells us *what* changed; Query decides *when* to refetch"
 * (docs/06-frontend.md#the-bridge). Patching from the message would mean the board's
 * shape was defined in two places.
 *
 * The handler is *registered*, not passed to `getSocket`. The socket is a singleton and
 * its constructor arguments belong to whoever built it first — which, because React runs
 * child effects before parent ones, was `TaskWorkspacePage` on any direct load of a
 * workspace. See `lib/socket.ts`.
 */
export function useCoachSocket(): void {
  const { user } = useAuth();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!user) {
      resetSocket();
      return;
    }
    const socket = getSocket();
    const unsubscribe = socket.onBoardUpdate((frame) => {
      void queryClient.invalidateQueries({ queryKey: ['tasks', frame.projectId] });
      void queryClient.invalidateQueries({ queryKey: ['project', frame.projectId] });
    });
    void socket.connect();

    const onVisibility = () => socket.setVisibility(!document.hidden);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      unsubscribe();
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // Deliberately keyed on the uid alone: re-running this on every render of a parent
    // would tear down and rebuild the socket, which is the one thing a long-lived
    // connection must not do.
  }, [user?.uid, queryClient, user]);
}
