import { HttpsError, type AuthBlockingEvent } from 'firebase-functions/v2/identity';

/**
 * Identity Platform's `beforeCreate` blocking event.
 *
 * The app's email/password provider has no self-serve sign-up screen — accounts are
 * created by an operator, by hand (docs/04-api-contract.md#authentication) — but Identity
 * Platform's `accounts:signUp` REST endpoint accepts a password sign-up from anyone
 * holding the public Web API key, regardless of what the SPA exposes. This trigger is
 * what actually enforces "no self-serve sign-up" rather than merely reflecting it in the
 * UI.
 *
 * Accounts created through the Admin SDK or the Cloud Console — the intended way to hand a
 * tester a login — never reach this function: blocking triggers fire only for a client-SDK
 * sign-up, not for Admin SDK user creation. The Google provider is untouched by this
 * check; auto-provisioning any Google account on first sign-in is the existing, deliberate
 * design (docs/04-api-contract.md#authentication).
 */
export function blockPasswordSignUp(event: AuthBlockingEvent): void {
  if (event.credential?.providerId === 'password') {
    throw new HttpsError(
      'permission-denied',
      'Email/password accounts are created by an operator, not self-service sign-up.',
    );
  }
}
