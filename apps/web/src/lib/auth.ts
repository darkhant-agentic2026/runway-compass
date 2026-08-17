/**
 * Authentication.
 *
 * Two providers behind one interface, chosen by build-time configuration:
 *
 * - **Identity Platform** (`firebase/auth`, the client library for `identitytoolkit`) in
 *   every deployed environment. The SDK silently refreshes the ID token before its
 *   one-hour expiry and surfaces it via `onIdTokenChanged`, so the fetch wrapper and the
 *   WebSocket ticket flow always attach a live token without a hand-rolled refresh loop
 *   (docs/06-frontend.md).
 * - **A `dev:<uid>` token** when `VITE_AUTH_MODE=dev`, which is local development and the
 *   Playwright suite. This is the client half of the `ENV=local` server path in
 *   docs/04-api-contract.md#authentication, and it exists for the same reason: Identity
 *   Platform has no local emulator. The server refuses these tokens for any other `ENV`,
 *   which is what keeps this from being a production bypass.
 *
 * `firebase/auth` is imported lazily so the SDK is not in the bundle a local developer
 * downloads, and so a missing Identity Platform config cannot break the dev path.
 */

import type { User as FirebaseUser } from 'firebase/auth'

export interface AuthUser {
  uid: string
  email: string | null
  displayName: string | null
  photoUrl: string | null
}

export interface AuthProvider {
  readonly mode: 'dev' | 'identity-platform'
  /** Resolves the initial auth state; the route guard awaits it to avoid a login flash. */
  subscribe(listener: (user: AuthUser | null) => void): () => void
  getIdToken(): Promise<string | null>
  signIn(): Promise<void>
  signOut(): Promise<void>
}

const DEV_UID_STORAGE_KEY = 'coach.devUid'

interface ImportMetaEnvShape {
  VITE_AUTH_MODE?: string
  VITE_DEV_UID?: string
  VITE_IDENTITY_API_KEY?: string
  VITE_IDENTITY_AUTH_DOMAIN?: string
  VITE_IDENTITY_PROJECT_ID?: string
}

function env(): ImportMetaEnvShape {
  return import.meta.env as unknown as ImportMetaEnvShape
}

/** True when the app is running against a local API with `ENV=local`. */
export function isDevAuthMode(): boolean {
  return env().VITE_AUTH_MODE === 'dev'
}

// --- dev provider --------------------------------------------------------------------

function createDevProvider(): AuthProvider {
  const defaultUid = env().VITE_DEV_UID ?? 'u_dev'
  let listener: ((user: AuthUser | null) => void) | null = null

  const read = (): string | null => {
    try {
      return localStorage.getItem(DEV_UID_STORAGE_KEY)
    } catch {
      return null
    }
  }

  const toUser = (uid: string): AuthUser => ({
    uid,
    email: `${uid}@localhost.dev`,
    displayName: uid,
    photoUrl: null,
  })

  return {
    mode: 'dev',
    subscribe(next) {
      listener = next
      const uid = read()
      // Deliver asynchronously so subscribers see the same "resolve later" shape they
      // get from the real SDK, rather than a synchronous callback only dev hits.
      queueMicrotask(() => next(uid ? toUser(uid) : null))
      return () => {
        listener = null
      }
    },
    async getIdToken() {
      const uid = read()
      return uid ? `dev:${uid}` : null
    },
    async signIn() {
      const uid = read() ?? defaultUid
      localStorage.setItem(DEV_UID_STORAGE_KEY, uid)
      listener?.(toUser(uid))
    },
    async signOut() {
      localStorage.removeItem(DEV_UID_STORAGE_KEY)
      listener?.(null)
    },
  }
}

// --- Identity Platform provider ------------------------------------------------------

function createIdentityPlatformProvider(): AuthProvider {
  const config = {
    apiKey: env().VITE_IDENTITY_API_KEY ?? '',
    authDomain: env().VITE_IDENTITY_AUTH_DOMAIN ?? '',
    projectId: env().VITE_IDENTITY_PROJECT_ID ?? '',
  }

  const authPromise = (async () => {
    const [{ initializeApp, getApps }, auth] = await Promise.all([
      import('firebase/app'),
      import('firebase/auth'),
    ])
    const app = getApps()[0] ?? initializeApp(config)
    return { auth: auth.getAuth(app), sdk: auth }
  })()

  const toUser = (user: FirebaseUser): AuthUser => ({
    uid: user.uid,
    email: user.email,
    displayName: user.displayName,
    photoUrl: user.photoURL,
  })

  return {
    mode: 'identity-platform',
    subscribe(next) {
      let unsubscribe: (() => void) | null = null
      let cancelled = false
      void authPromise.then(({ auth, sdk }) => {
        if (cancelled) return
        // onIdTokenChanged rather than onAuthStateChanged: it also fires on the silent
        // hourly refresh, which is what keeps a long-lived tab's token fresh.
        unsubscribe = sdk.onIdTokenChanged(auth, (user) => next(user ? toUser(user) : null))
      })
      return () => {
        cancelled = true
        unsubscribe?.()
      }
    },
    async getIdToken() {
      const { auth } = await authPromise
      return auth.currentUser ? auth.currentUser.getIdToken() : null
    },
    async signIn() {
      const { auth, sdk } = await authPromise
      const provider = new sdk.GoogleAuthProvider()
      await sdk.signInWithPopup(auth, provider)
    },
    async signOut() {
      const { auth, sdk } = await authPromise
      await sdk.signOut(auth)
    },
  }
}

let provider: AuthProvider | null = null

export function getAuthProvider(): AuthProvider {
  provider ??= isDevAuthMode() ? createDevProvider() : createIdentityPlatformProvider()
  return provider
}

/** Test seam: lets a test install a fake provider. */
export function setAuthProviderForTesting(next: AuthProvider | null): void {
  provider = next
}
