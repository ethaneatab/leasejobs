"""An O(1) LRU cache: a hash map from key to node, plus a doubly linked list
that keeps nodes in recency order (most recent right after the head sentinel).

get: map lookup, then unlink the node and relink it at the front.
put: same for an existing key; for a new key, link at the front and, if over
capacity, unlink the node just before the tail sentinel (least recent).
Every step is a constant number of pointer moves. No OrderedDict, no
functools.lru_cache: those would hide exactly the part worth showing.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class _Node:
    __slots__ = ("key", "value", "prev", "next")

    def __init__(self, key=None, value=None):
        self.key, self.value = key, value
        self.prev: _Node | None = None
        self.next: _Node | None = None


class LRUCache(Generic[K, V]):
    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._map: dict[K, _Node] = {}
        self._head, self._tail = _Node(), _Node()   # sentinels: no None checks when linking
        self._head.next, self._tail.prev = self._tail, self._head
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = 0
        self.recent: deque[dict] = deque(maxlen=40)  # last operations, for /cache and the learn page

    # @learn:lru-core
    def _unlink(self, n: _Node) -> None:
        n.prev.next, n.next.prev = n.next, n.prev

    def _push_front(self, n: _Node) -> None:
        n.prev, n.next = self._head, self._head.next
        self._head.next.prev = n
        self._head.next = n

    def get(self, key: K, default: Any = None) -> V | Any:
        with self._lock:
            n = self._map.get(key)
            if n is None:
                self.misses += 1
                self.recent.append({"op": "miss", "key": key})
                return default
            self._unlink(n)
            self._push_front(n)
            self.hits += 1
            self.recent.append({"op": "hit", "key": key})
            return n.value

    def put(self, key: K, value: V, source: str = "") -> None:
        with self._lock:
            n = self._map.get(key)
            if n is not None:
                n.value = value
                self._unlink(n)
                self._push_front(n)
            else:
                n = _Node(key, value)
                self._map[key] = n
                self._push_front(n)
                if len(self._map) > self.capacity:
                    lru = self._tail.prev
                    self._unlink(lru)
                    del self._map[lru.key]
                    self.evictions += 1
                    self.recent.append({"op": "evict", "key": lru.key})
            self.recent.append({"op": "put", "key": key, "source": source})
    # @learn:end lru-core

    def delete(self, key: K) -> bool:
        with self._lock:
            n = self._map.pop(key, None)
            if n is None:
                return False
            self._unlink(n)
            return True

    def peek(self, key: K, default: Any = None):
        """Read without touching recency or stats."""
        n = self._map.get(key)
        return default if n is None else n.value

    def __contains__(self, key: K) -> bool:
        return key in self._map

    def __len__(self) -> int:
        return len(self._map)

    def keys_mru(self) -> list[K]:
        with self._lock:
            out, n = [], self._head.next
            while n is not self._tail:
                out.append(n.key)
                n = n.next
            return out

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"size": len(self._map), "capacity": self.capacity, "hits": self.hits, "misses": self.misses,
                "evictions": self.evictions, "hit_ratio": round(self.hits / total, 4) if total else None}
