import { HttpsError, type AuthBlockingEvent } from 'firebase-functions/v2/identity';
import { describe, expect, it } from 'vitest';

import { blockPasswordSignUp } from './blockPasswordSignUp';

// A minimal stub of the real event shape (firebase-functions/v2/identity's own
// AuthBlockingEvent type, not a hand-guessed one): the handler only reads
// `credential.providerId`, so nothing else in the real EventContext is worth faking here.
function eventWithProvider(providerId: string | undefined): AuthBlockingEvent {
  return {
    credential: providerId ? { providerId, signInMethod: providerId } : undefined,
  } as AuthBlockingEvent;
}

describe('blockPasswordSignUp', () => {
  it('rejects a password-provider sign-up', () => {
    const event = eventWithProvider('password');
    expect(() => blockPasswordSignUp(event)).toThrow(HttpsError);
    try {
      blockPasswordSignUp(event);
    } catch (error) {
      expect((error as HttpsError).code).toBe('permission-denied');
    }
  });

  it('lets a Google sign-in through', () => {
    expect(() => blockPasswordSignUp(eventWithProvider('google.com'))).not.toThrow();
  });

  it('lets a create event with no credential through', () => {
    // e.g. anonymous sign-in, which has no provider credential at all. (Admin SDK user
    // creation is a different case again: it never fires this trigger in the first
    // place, which is what lets an operator hand out a password account unblocked.)
    expect(() => blockPasswordSignUp(eventWithProvider(undefined))).not.toThrow();
  });
});
