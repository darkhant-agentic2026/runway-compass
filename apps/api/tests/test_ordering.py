"""Fractional index properties.

docs/08-testing.md:

    Fractional index: property test that any sequence of inserts/reorders produces
    strictly increasing keys, and that the rebalance path preserves visible order.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from coach.services.ordering import (
    FIRST_KEY,
    OrderKeyError,
    key_between,
    n_keys_between,
    rebalance,
    validate_key,
)

# A move is (index to take from, index to insert at); inserts are (index to insert at).
Operation = tuple[str, int, int]


@st.composite
def operations(draw: st.DrawFn) -> list[Operation]:
    """A random sequence of inserts and moves against a growing list."""
    count = draw(st.integers(min_value=1, max_value=40))
    ops: list[Operation] = []
    size = 0
    for _ in range(count):
        if size < 2 or draw(st.booleans()):
            at = draw(st.integers(min_value=0, max_value=size))
            ops.append(("insert", at, 0))
            size += 1
        else:
            source = draw(st.integers(min_value=0, max_value=size - 1))
            target = draw(st.integers(min_value=0, max_value=size - 1))
            ops.append(("move", source, target))
    return ops


def _insert(keys: list[str], at: int) -> list[str]:
    previous = keys[at - 1] if at > 0 else None
    following = keys[at] if at < len(keys) else None
    return [*keys[:at], key_between(previous, following), *keys[at:]]


def _move(keys: list[str], source: int, target: int) -> list[str]:
    remaining = [*keys[:source], *keys[source + 1 :]]
    previous = remaining[target - 1] if target > 0 else None
    following = remaining[target] if target < len(remaining) else None
    return [*remaining[:target], key_between(previous, following), *remaining[target:]]


@given(ops=operations())
@hypothesis_settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_any_sequence_of_inserts_and_moves_keeps_keys_strictly_increasing(
    ops: list[Operation],
) -> None:
    keys: list[str] = []
    for kind, a, b in ops:
        keys = _insert(keys, a) if kind == "insert" else _move(keys, a, b)
        assert keys == sorted(keys), "Keys must sort in the order they were placed in: " + repr(
            keys
        )
        assert len(set(keys)) == len(keys), "Keys must be unique: " + repr(keys)
        for key in keys:
            validate_key(key)


@given(count=st.integers(min_value=0, max_value=200))
def test_rebalance_produces_the_requested_number_of_increasing_keys(count: int) -> None:
    keys = rebalance(count)
    assert len(keys) == count
    assert keys == sorted(keys)
    assert len(set(keys)) == count


@given(count=st.integers(min_value=1, max_value=60))
def test_rebalance_preserves_visible_order(count: int) -> None:
    """The rebalance path assigns new keys to items in their existing sorted order."""
    original = [f"item-{i}" for i in range(count)]
    rekeyed = dict(zip(original, rebalance(count), strict=True))
    assert sorted(original, key=lambda item: rekeyed[item]) == original


def test_first_key_on_an_empty_list() -> None:
    assert key_between(None, None) == FIRST_KEY


def test_keys_stay_short_under_repeated_appends() -> None:
    """Appending must not grow the key, or a long-lived board pays for it forever."""
    key = key_between(None, None)
    for _ in range(500):
        key = key_between(key, None)
    assert len(key) <= 4, f"append-only growth produced {key!r}"


def test_keys_stay_bounded_under_repeated_prepends() -> None:
    key = key_between(None, None)
    for _ in range(500):
        key = key_between(None, key)
    assert len(key) <= 5, f"prepend-only growth produced {key!r}"


def test_repeatedly_splitting_the_same_gap_grows_the_key_slowly() -> None:
    """The pathological case: always inserting between the same two neighbours."""
    low, high = key_between(None, None), key_between(key_between(None, None), None)
    for _ in range(50):
        high = key_between(low, high)
    assert low < high
    validate_key(high)


@pytest.mark.parametrize(
    ("a", "b"),
    [("a1", "a1"), ("a2", "a1"), ("b0", "a0")],
)
def test_out_of_sequence_bounds_are_refused(a: str, b: str) -> None:
    """A duplicate or inverted pair is what triggers the rebalance path upstream."""
    with pytest.raises(OrderKeyError):
        key_between(a, b)


@pytest.mark.parametrize("key", ["", "0", "a", "a0000000000", "!", "a10"])
def test_malformed_keys_are_refused(key: str) -> None:
    with pytest.raises(OrderKeyError):
        validate_key(key)


@given(n=st.integers(min_value=0, max_value=50))
def test_n_keys_between_two_bounds_are_all_inside_them(n: int) -> None:
    low, high = "a0", "a1"
    keys = n_keys_between(low, high, n)
    assert keys == sorted(keys)
    assert all(low < key < high for key in keys)


def test_n_keys_between_open_bounds() -> None:
    assert n_keys_between(None, None, 0) == []
    keys = n_keys_between(None, None, 5)
    assert len(keys) == 5
    assert keys == sorted(keys)
