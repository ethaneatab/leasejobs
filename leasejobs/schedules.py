"""Next-run arithmetic for recurring schedules, including what to do about
runs that were missed while no scheduler was up (misfires).

Policy, per schedule:
  skip      (default) enqueue ONE run for the most recent missed slot, then
            jump to the first slot in the future. For "bring things up to date"
            work (reports, syncs), running 40 stale copies back to back is waste.
  catch_up  enqueue every missed slot, oldest first, but at most max_catchup of
            them (the most recent ones). For per-slot work where each window
            matters (an hourly rollup), with a cap so a week-long outage can't
            flood the queue.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .cron import Cron


def step_fn(cron: str | None, every_s: float | None):
    if cron:
        c = Cron.parse(cron)
        return c.next_after
    if not every_s or every_s < 1:
        raise ValueError("a schedule needs cron or every_s >= 1")
    delta = timedelta(seconds=every_s)
    return lambda t: t + delta  # anchored to the previous slot, so no drift


# @learn:misfire
def due_slots(next_run_at: datetime, now: datetime, step, policy: str, max_catchup: int
              ) -> tuple[list[datetime], datetime]:
    """Return (slots to enqueue now, the new next_run_at)."""
    if next_run_at > now:
        return [], next_run_at
    missed = [next_run_at]
    nxt = step(next_run_at)
    while nxt <= now:
        if policy == "catch_up":
            missed.append(nxt)
            if len(missed) > max_catchup:
                missed.pop(0)          # keep only the most recent max_catchup slots
        else:
            missed = [nxt]             # skip: only the latest missed slot survives
        nxt = step(nxt)
    return missed, nxt
# @learn:end misfire
