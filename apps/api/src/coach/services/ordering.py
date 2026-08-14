"""Fractional index ordering for tasks.

docs/02-data-model.md#ordering:

    `order` is a fractional index string. Inserting between neighbours computes a
    midpoint key, so "make this the next-up task" is a single-document write, not a
    renumbering of the board. Subtasks are ordered within their parent using the same
    scheme. On the rare key collision or exhaustion, `TaskService` rebalances the whole
    project in one batch.

The encoding is the base-62 fractional-index scheme (an integer part whose *length* is
encoded in its head character, followed by an arbitrary-precision fraction), which is the
same family as the LexoRank the data-model doc names, with a simpler encoding and no
bucket prefix — the doc's `"0|hzzzzz:"` is an illustrative rank string, not a wire format
anything parses. What the doc actually requires holds exactly: keys are plain strings
ordered by byte comparison, a midpoint between any two adjacent keys always exists, and
inserting is one document write.

`apps/web/src/lib/ordering.ts` is a line-for-line port of this module. The two must agree:
the board's optimistic drag-and-drop computes the new key client-side and the server
recomputes it, and docs/08-testing.md asserts "the optimistic fractional index equals the
server's". Change one, change both.

Algorithm after the `fractional-indexing` reference implementation
(https://github.com/rocicorp/fractional-indexing, MIT), which is itself the scheme
described in David Greenspan's "Implementing Fractional Indexing".
"""

from __future__ import annotations

BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: The key handed out when a list is empty. `a` is the mid-range integer head, so the
#: first key leaves maximal room on both sides.
FIRST_KEY = "a0"

#: The smallest representable integer part. Reserved as a sentinel: a key equal to it
#: has no room left below, which is what triggers a rebalance.
_SMALLEST_INTEGER = "A" + "0" * 26


class OrderKeyError(ValueError):
    """A malformed order key, or an ordering that cannot be represented."""


class OrderKeyExhausted(OrderKeyError):
    """No key can be generated in the requested position.

    Only reachable at the extreme ends of the key space, after roughly 2^26 successive
    appends in one direction. `TaskService` catches this and rebalances the project.
    """


def _integer_length(head: str) -> int:
    """Decode the integer part's length from its head character.

    Lowercase heads count up (`a` = 2 chars, `b` = 3, …), uppercase count down
    (`Z` = 2, `Y` = 3, …). This is what makes integer parts of different lengths sort
    correctly against each other without padding.
    """
    if "a" <= head <= "z":
        return ord(head) - ord("a") + 2
    if "A" <= head <= "Z":
        return ord("Z") - ord(head) + 2
    raise OrderKeyError(f"invalid order key head: {head!r}")


def _integer_part(key: str) -> str:
    if not key:
        raise OrderKeyError("empty order key")
    length = _integer_length(key[0])
    if length > len(key):
        raise OrderKeyError(f"invalid order key: {key!r}")
    return key[:length]


def validate_key(key: str) -> None:
    """Raise :class:`OrderKeyError` unless `key` is a well-formed order key."""
    if key == _SMALLEST_INTEGER:
        raise OrderKeyError(f"invalid order key: {key!r}")
    integer = _integer_part(key)
    fraction = key[len(integer) :]
    if fraction.endswith(BASE62[0]):
        # A trailing zero would give two distinct strings for the same position.
        raise OrderKeyError(f"invalid order key (trailing zero): {key!r}")


def _validate_integer(value: str) -> None:
    if len(value) != _integer_length(value[0]):
        raise OrderKeyError(f"invalid integer part: {value!r}")


def _midpoint(a: str, b: str | None) -> str:
    """A fraction strictly between fractions `a` and `b`, exclusive.

    Both arguments are fraction parts (no integer head) with no trailing zero. `b` of
    `None` means "no upper bound".
    """
    if b is not None and a >= b:
        raise OrderKeyError(f"{a!r} >= {b!r}")
    if a.endswith("0") or (b is not None and b.endswith("0")):
        raise OrderKeyError("trailing zero in fraction")

    if b is not None:
        # Strip the common prefix and recurse on the first differing position.
        n = 0
        while n < len(b) and (a[n] if n < len(a) else "0") == b[n]:
            n += 1
        if n > 0:
            return b[:n] + _midpoint(a[n:], b[n:])

    digit_a = BASE62.index(a[0]) if a else 0
    digit_b = BASE62.index(b[0]) if b else len(BASE62)
    if digit_b - digit_a > 1:
        # There is at least one digit strictly between them.
        #
        # `(a + b + 1) // 2` rather than `round(0.5 * (a + b))`: Python's `round` is
        # banker's rounding (half to even) while JavaScript's `Math.round` is half up, so
        # the obvious transcription silently disagrees with `ordering.ts` on every pair
        # whose digits sum to an odd number. That would make the board's optimistic key
        # differ from the server's confirmed key, and the row would jump on response.
        # This form is half-up in both languages.
        return BASE62[(digit_a + digit_b + 1) // 2]
    # The digits are consecutive, so we have to grow the fraction by one place.
    if b is not None and len(b) > 1:
        return b[:1]
    return BASE62[digit_a] + _midpoint(a[1:], None)


def _increment_integer(value: str) -> str | None:
    """The next integer part, or `None` at the top of the space."""
    _validate_integer(value)
    head, digits = value[0], list(value[1:])
    carry = True
    for i in range(len(digits) - 1, -1, -1):
        if not carry:
            break
        d = BASE62.index(digits[i]) + 1
        if d == len(BASE62):
            digits[i] = BASE62[0]
        else:
            digits[i] = BASE62[d]
            carry = False
    if carry:
        if head == "Z":
            return "a" + BASE62[0]
        if head == "z":
            return None
        h = chr(ord(head) + 1)
        if h > "a":
            digits.append(BASE62[0])
        else:
            digits.pop()
        return h + "".join(digits)
    return head + "".join(digits)


def _decrement_integer(value: str) -> str | None:
    """The previous integer part, or `None` at the bottom of the space."""
    _validate_integer(value)
    head, digits = value[0], list(value[1:])
    borrow = True
    for i in range(len(digits) - 1, -1, -1):
        if not borrow:
            break
        d = BASE62.index(digits[i]) - 1
        if d == -1:
            digits[i] = BASE62[-1]
        else:
            digits[i] = BASE62[d]
            borrow = False
    if borrow:
        if head == "a":
            return "Z" + BASE62[-1]
        if head == "A":
            return None
        h = chr(ord(head) - 1)
        if h < "Z":
            digits.append(BASE62[-1])
        else:
            digits.pop()
        return h + "".join(digits)
    return head + "".join(digits)


def key_between(a: str | None, b: str | None) -> str:
    """A key that sorts strictly after `a` and strictly before `b`.

    `None` means unbounded on that side, so `key_between(None, None)` is the first key
    on an empty board, `key_between(last, None)` appends, and `key_between(None, first)`
    prepends.

    Raises:
        OrderKeyError: if either key is malformed or `a >= b`.
        OrderKeyExhausted: if the key space is used up in that direction. Callers
            rebalance rather than propagating this to the user.
    """
    if a is not None:
        validate_key(a)
    if b is not None:
        validate_key(b)
    if a is not None and b is not None and a >= b:
        raise OrderKeyError(f"order keys out of sequence: {a!r} >= {b!r}")

    if a is None:
        if b is None:
            return FIRST_KEY
        integer_b = _integer_part(b)
        fraction_b = b[len(integer_b) :]
        if integer_b == _SMALLEST_INTEGER:
            return integer_b + _midpoint("", fraction_b)
        if integer_b < b:
            # `b` has a fraction, so its own integer part sorts before it.
            return integer_b
        decremented = _decrement_integer(integer_b)
        if decremented is None:
            raise OrderKeyExhausted("no room below the smallest order key")
        return decremented

    if b is None:
        integer_a = _integer_part(a)
        fraction_a = a[len(integer_a) :]
        incremented = _increment_integer(integer_a)
        if incremented is None:
            return integer_a + _midpoint(fraction_a, None)
        return incremented

    integer_a = _integer_part(a)
    fraction_a = a[len(integer_a) :]
    integer_b = _integer_part(b)
    fraction_b = b[len(integer_b) :]
    if integer_a == integer_b:
        return integer_a + _midpoint(fraction_a, fraction_b)
    incremented = _increment_integer(integer_a)
    if incremented is None:
        raise OrderKeyExhausted("no room above the largest order key")
    if incremented < b:
        return incremented
    return integer_a + _midpoint(fraction_a, None)


def n_keys_between(a: str | None, b: str | None, n: int) -> list[str]:
    """`n` keys, in ascending order, strictly between `a` and `b`.

    Used to rebalance a whole list in one batch and to bulk-insert (seeding, and
    `split_task` creating several subtasks at once).
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return []
    if n == 1:
        return [key_between(a, b)]

    if b is None:
        key = key_between(a, b)
        keys = [key]
        for _ in range(n - 1):
            key = key_between(key, b)
            keys.append(key)
        return keys

    if a is None:
        key = key_between(a, b)
        keys = [key]
        for _ in range(n - 1):
            key = key_between(a, key)
            keys.append(key)
        keys.reverse()
        return keys

    # Bisect, so the generated keys stay short instead of degrading into a long chain.
    mid = n // 2
    key = key_between(a, b)
    return [*n_keys_between(a, key, mid), key, *n_keys_between(key, b, n - mid - 1)]


def rebalance(count: int) -> list[str]:
    """`count` evenly spread keys covering the whole space, for a full re-key.

    The rebalance path preserves visible order by assigning these to the existing items
    in their current sorted order (docs/08-testing.md).
    """
    return n_keys_between(None, None, count)
