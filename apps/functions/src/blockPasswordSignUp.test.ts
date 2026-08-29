import * as gcipCloudFunctions from 'gcip-cloud-functions';
import { describe, expect, it } from 'vitest';

import { blockPasswordSignUp } from './blockPasswordSignUp';

// A minimal stub of the real context shape (gcip-cloud-functions' own AuthEventContext
// type, not a hand-guessed one): the handler only reads `additionalUserInfo.providerId`,
// so nothing else in the real context is worth faking here. `additionalUserInfo`, not
// `credential` — a password sign-up carries no OAuth/SAML credential, so the SDK never
// populates `credential` for it regardless of provider (see blockPasswordSignUp.ts).
function contextWithProvider(
  providerId: string | undefined,
): gcipCloudFunctions.AuthEventContext {
  return {
    additionalUserInfo: { providerId, isNewUser: true },
  } as gcipCloudFunctions.AuthEventContext;
}

// The handler never reads `user`, so an empty stub is enough.
const user = {} as gcipCloudFunctions.UserRecord;

describe('blockPasswordSignUp', () => {
  it('rejects a password-provider sign-up', () => {
    const context = contextWithProvider('password');
    expect(() => blockPasswordSignUp(user, context)).toThrow(
      gcipCloudFunctions.https.HttpsError,
    );
    try {
      blockPasswordSignUp(user, context);
    } catch (error) {
      expect((error as { status: string }).status).toBe('PERMISSION_DENIED');
    }
  });

  it('lets a Google sign-in through', () => {
    expect(() => blockPasswordSignUp(user, contextWithProvider('google.com'))).not.toThrow();
  });

  it('lets a create event with no credential through', () => {
    // e.g. anonymous sign-in, which has no provider credential at all. (Admin SDK user
    // creation is a different case again: it never fires this trigger in the first
    // place, which is what lets an operator hand out a password account unblocked.)
    expect(() => blockPasswordSignUp(user, contextWithProvider(undefined))).not.toThrow();
  });
});
