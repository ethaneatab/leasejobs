"""A small 5-field cron parser: minute hour day-of-month month day-of-week.

Supports *, n, a-b, lists (a,b,c) and steps (*/n, a-b/n). Day-of-week is 0-6
with 0 = Sunday (7 is accepted as Sunday too). As in classic cron, when both
day-of-month and day-of-week are restricted, a day matches if EITHER does.
All times are UTC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = ["minute", "hour", "day-of-month", "month", "day-of-week"]


def _field(text: str, lo: int, hi: int, name: str) -> frozenset[int]:
    out: set[int] = set()
    for part in text.split(","):
        rng, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise ValueError(f"cron {name}: step must be >= 1")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = b = int(rng)
            if step_s:
                b = hi
        if not (lo <= a <= hi and lo <= b <= hi and a <= b):
            raise ValueError(f"cron {name}: {part!r} outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    dom_star: bool
    dow_star: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        parts = expr.split()
        if len(parts) != 5:
            raise ValueError(f"cron needs 5 fields, got {len(parts)}: {expr!r}")
        try:
            f = [_field(p, lo, hi, n) for p, (lo, hi), n in zip(parts, _RANGES, _NAMES)]
        except ValueError as exc:
            raise ValueError(str(exc)) from None
        weekdays = frozenset(d % 7 for d in f[4])
        return cls(f[0], f[1], f[2], f[3], weekdays, parts[2] == "*", parts[4] == "*")

    def _day_ok(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.weekday() + 1) % 7 in self.weekdays  # Python Monday=0 -> cron Sunday=0
        if self.dom_star or self.dow_star:
            return dom and dow
        return dom or dow

    # @learn:cron-next
    def next_after(self, t: datetime) -> datetime:
        """The first fire time strictly after t."""
        t = t.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = t.replace(hour=0, minute=0)
        for _ in range(366 * 8):  # every combination repeats within 8 years (leap Feb 29 on a weekday)
            if self._day_ok(day):
                for h in sorted(self.hours):
                    for m in sorted(self.minutes):
                        cand = day.replace(hour=h, minute=m)
                        if cand >= t:
                            return cand
            day += timedelta(days=1)
        raise ValueError("cron expression never fires")
    # @learn:end cron-next
