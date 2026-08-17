"""Time, behind one function.

Every timestamp the service writes goes through :func:`now`, so `freezegun` can move the
whole system clock in tests — which the lease, TTL, and `postponed_until` sweep tests
depend on (docs/08-testing.md).
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    """The current instant, always timezone-aware and always UTC.

    Firestore returns timezone-aware datetimes; producing naive ones here would make
    every comparison against a stored value raise.
    """
    return datetime.now(UTC)
