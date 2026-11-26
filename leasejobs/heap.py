"""An indexed binary min-heap.

Unlike heapq, every entry has a key, and we keep key -> array index, so we can
also remove or re-prioritise an entry in O(log n). The scheduler needs that: a
cancelled delayed job, or a schedule whose next run moved, must leave the heap
without a linear scan or a pile of tombstones.
Ties on priority pop in insertion order (FIFO), via a sequence number.
"""
from __future__ import annotations

from typing import Generic, Hashable, Iterator, TypeVar

K = TypeVar("K", bound=Hashable)


class IndexedMinHeap(Generic[K]):
    def __init__(self) -> None:
        self._a: list[list] = []          # [priority, seq, key]
        self._pos: dict[K, int] = {}
        self._seq = 0

    def __len__(self) -> int:
        return len(self._a)

    def __contains__(self, key: K) -> bool:
        return key in self._pos

    def priority(self, key: K) -> float:
        return self._a[self._pos[key]][0]

    # @learn:heap-push
    def push(self, key: K, priority: float) -> bool:
        """Insert, or move an existing key to a new priority. Returns True if the key was new."""
        if key in self._pos:
            i = self._pos[key]
            old = self._a[i][0]
            self._a[i][0] = priority
            self._sift_up(i) if priority < old else self._sift_down(i)
            return False
        self._seq += 1
        self._a.append([priority, self._seq, key])
        self._pos[key] = len(self._a) - 1
        self._sift_up(len(self._a) - 1)
        return True
    # @learn:end heap-push

    def remove(self, key: K) -> bool:
        i = self._pos.pop(key, None)
        if i is None:
            return False
        last = self._a.pop()
        if i < len(self._a):  # move the last entry into the hole, then restore order
            self._a[i] = last
            self._pos[last[2]] = i
            self._sift_down(i)
            self._sift_up(i)
        return True

    def peek(self) -> tuple[K, float] | None:
        return (self._a[0][2], self._a[0][0]) if self._a else None

    def pop(self) -> tuple[K, float]:
        if not self._a:
            raise IndexError("pop from empty heap")
        key, prio = self._a[0][2], self._a[0][0]
        self.remove(key)
        return key, prio

    def pop_due(self, now: float) -> Iterator[tuple[K, float]]:
        while self._a and self._a[0][0] <= now:
            yield self.pop()

    def smallest(self, n: int) -> list[tuple[K, float]]:
        """The n next entries in order, without changing the heap (for snapshots)."""
        import heapq
        return [(e[2], e[0]) for e in heapq.nsmallest(n, self._a)]

    def clear(self) -> None:
        self._a.clear()
        self._pos.clear()

    def _less(self, i: int, j: int) -> bool:
        a, b = self._a[i], self._a[j]
        return (a[0], a[1]) < (b[0], b[1])

    def _swap(self, i: int, j: int) -> None:
        a = self._a
        a[i], a[j] = a[j], a[i]
        self._pos[a[i][2]] = i
        self._pos[a[j][2]] = j

    def _sift_up(self, i: int) -> None:
        while i > 0:
            parent = (i - 1) // 2
            if not self._less(i, parent):
                break
            self._swap(i, parent)
            i = parent

    def _sift_down(self, i: int) -> None:
        n = len(self._a)
        while True:
            left, smallest = 2 * i + 1, i
            if left < n and self._less(left, smallest):
                smallest = left
            if left + 1 < n and self._less(left + 1, smallest):
                smallest = left + 1
            if smallest == i:
                return
            self._swap(i, smallest)
            i = smallest
