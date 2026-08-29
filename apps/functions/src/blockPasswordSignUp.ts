import * as gcipCloudFunctions from 'gcip-cloud-functions';

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
 *
 * `context.additionalUserInfo.providerId`, not `context.credential.providerId`: a plain
 * password sign-up carries no OAuth/SAML credential at all, so `gcip-cloud-functions`'
 * `AuthCredential.fromDecodedJwt` — which requires an `oauth_*` token or SAML
 * `sign_in_attributes` to construct anything — returns `null` for it, and `credential`
 * stays unset regardless of provider. `additionalUserInfo.providerId` is populated
 * unconditionally straight from the JWT's `sign_in_method`, which is exactly the field
 * that distinguishes `password` from `google.com` on every sign-up.
 */
export function blockPasswordSignUp(
  _user: gcipCloudFunctions.UserRecord,
  context: gcipCloudFunctions.AuthEventContext,
): gcipCloudFunctions.UserEventUpdateRequest {
  if (context.additionalUserInfo?.providerId === 'password') {
    throw new gcipCloudFunctions.https.HttpsError(
      'permission-denied',
      'Email/password accounts are created by an operator, not self-service sign-up.',
    );
  }
  return {};
}
