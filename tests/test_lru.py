"""LRU cache: unit tests, a stateful differential test against OrderedDict,
and a scaling check that per-operation cost does not grow with size."""
import time
from collections import OrderedDict

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from leasejobs.cache import LRUCache


def test_evicts_least_recently_used():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1          # a is now most recent
    c.put("c", 3)                   # evicts b
    assert "b" not in c and c.keys_mru() == ["c", "a"] and c.evictions == 1


def test_update_moves_to_front_without_growing():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("a", 10)
    c.put("c", 3)                   # b is the LRU now
    assert c.keys_mru() == ["c", "a"] and c.get("a") == 10


def test_capacity_one_and_delete_and_stats():
    c = LRUCache(1)
    c.put("a", 1)
    c.put("b", 2)
    assert c.keys_mru() == ["b"] and c.get("a") is None
    assert c.delete("b") and not c.delete("b") and len(c) == 0
    assert c.stats() == {"size": 0, "capacity": 1, "hits": 0, "misses": 1, "evictions": 1, "hit_ratio": 0.0}
    with pytest.raises(ValueError):
        LRUCache(0)


class ModelCompare(RuleBasedStateMachine):
    """Same operations on our cache and on an OrderedDict-based reference."""

    def __init__(self):
        super().__init__()
        self.cap = 4
        self.c = LRUCache(self.cap)
        self.m: OrderedDict = OrderedDict()

    keys = st.integers(0, 9)

    @rule(k=keys, v=st.integers())
    def put(self, k, v):
        self.c.put(k, v)
        self.m[k] = v
        self.m.move_to_end(k)
        if len(self.m) > self.cap:
            self.m.popitem(last=False)

    @rule(k=keys)
    def get(self, k):
        want = self.m.get(k)
        if k in self.m:
            self.m.move_to_end(k)
        assert self.c.get(k) == want

    @rule(k=keys)
    def delete(self, k):
        assert self.c.delete(k) == (self.m.pop(k, None) is not None)

    @invariant()
    def same_order(self):
        assert self.c.keys_mru() == list(reversed(self.m))


TestLRUAgainstOrderedDict = ModelCompare.TestCase
TestLRUAgainstOrderedDict.settings = settings(max_examples=200, stateful_step_count=60, deadline=None)


def _ns_per_op(size: int, ops: int = 60_000) -> float:
    c = LRUCache(size)
    for i in range(size):
        c.put(i, i)
    keys = [(i * 7919) % (size * 2) for i in range(ops)]  # half hits, half misses+evictions
    t = time.perf_counter()
    for k in keys:
        if c.get(k) is None:
            c.put(k, k)
    return (time.perf_counter() - t) / ops * 1e9


def test_cost_per_operation_does_not_grow_with_size():
    """Relative check on the same machine: 100x more entries, not ~100x slower.
    Median of 5 runs each to dampen noise from other processes."""
    small = sorted(_ns_per_op(1_000) for _ in range(5))[2]
    big = sorted(_ns_per_op(100_000) for _ in range(5))[2]
    print(f"\nLRU get+put: {small:.0f} ns/op at 1e3 entries, {big:.0f} ns/op at 1e5 entries")
    assert big < small * 3
