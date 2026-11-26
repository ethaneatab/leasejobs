"""The scheduler process: releases delayed jobs and fires recurring schedules.

`python -m leasejobs.scheduler --port 8916`

* Postgres is the source of truth. On start (and every RESYNC_S) the heap is
  rebuilt from SCHEDULED jobs and enabled schedules, so a crash loses nothing.
* Between resyncs it sleeps until the heap top is due, or until a NOTIFY on
  channel leasejobs_scheduler says something new was scheduled.
* Only one scheduler is active: it holds a Postgres advisory lock. A second
  copy waits as a standby and takes over when the lock frees up.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone

from . import metrics, store
from .db import Jsonb, connect
from .heap import IndexedMinHeap
from .logs import JsonLog
from .schedules import due_slots, step_fn
from .states import State

LEADER_LOCK = 7310002
RESYNC_S = 15.0


def _ts(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class Scheduler:
    def __init__(self, dsn: str | None = None, scheduler_id: str | None = None):
        self.dsn = dsn
        self.id = scheduler_id or f"scheduler-{socket.gethostname().split('.')[0]}-{os.getpid()}"
        self.conn = connect(dsn)
        self.heap: IndexedMinHeap[tuple[str, int]] = IndexedMinHeap()
        self.skew = 0.0  # db_now - local_now, refreshed on every resync
        self.log = JsonLog("scheduler", scheduler=self.id)
        self.stop = threading.Event()
        self.is_leader = False
        self.last_tick = time.monotonic()

    def now(self) -> float:
        return time.time() + self.skew

    # ---- leadership --------------------------------------------------------------
    def try_lead(self) -> bool:
        if not self.is_leader:
            self.is_leader = self.conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LEADER_LOCK,)
                                               ).fetchone()["ok"]
            if self.is_leader:
                self.log.info("became leader")
        return self.is_leader

    # ---- heap maintenance -------------------------------------------------------
    def resync(self) -> None:
        """Rebuild the heap from the database."""
        self.skew = self.conn.execute("SELECT extract(epoch FROM now()) AS t").fetchone()["t"]
        self.skew = float(self.skew) - time.time()
        self.heap.clear()
        for r in self.conn.execute("SELECT id, extract(epoch FROM run_at) AS t FROM jobs WHERE state = 'SCHEDULED'"):
            self.heap.push(("job", r["id"]), float(r["t"]))
        for r in self.conn.execute("SELECT id, extract(epoch FROM next_run_at) AS t FROM schedules WHERE enabled"):
            self.heap.push(("schedule", r["id"]), float(r["t"]))

    # @learn:sched-push
    def on_notify(self, payload: str) -> None:
        kind, ident, when = payload.split(":", 2)
        key = (kind, int(ident))
        if when == "off":
            self.heap.remove(key)
            return
        is_new = self.heap.push(key, float(when))
        if kind == "job" and is_new:
            store.add_event(self.conn, key[1], "heap_push", self.id, detail={
                "run_at": _ts(float(when)), "heap_size": len(self.heap),
                "heap_top": [{"key": f"{k}:{i}", "due": _ts(p)} for (k, i), p in self.heap.smallest(5)]})
    # @learn:end sched-push

    # ---- firing -----------------------------------------------------------------
    def tick(self, now: float | None = None) -> list[tuple[str, int]]:
        now = self.now() if now is None else now
        done = []
        for key, _prio in list(self.heap.pop_due(now)):
            if key[0] == "job":
                self.release(key[1], now)
            else:
                self.fire(key[1], now)
            done.append(key)
        self.last_tick = time.monotonic()
        return done

    # @learn:release
    def release(self, job_id: int, now: float) -> dict | None:
        """SCHEDULED -> PENDING, only if it is still scheduled and really due."""
        cur = self.conn.execute("SELECT extract(epoch FROM run_at) AS t FROM jobs WHERE id = %s", (job_id,)).fetchone()
        if cur is None:
            return None
        row = store.transition(self.conn, job_id, State.SCHEDULED, State.PENDING, actor=self.id, kind="due",
                               guard=("run_at <= to_timestamp(%s)", (now,)),
                               detail={"run_at": _ts(float(cur["t"])), "late_ms": round((now - float(cur["t"])) * 1000)})
        if row is not None:
            metrics.RELEASED.inc()
        return row
    # @learn:end release

    # @learn:fire
    def fire(self, schedule_id: int, now: float) -> list[int]:
        """Enqueue the due slot(s) of one schedule and move next_run_at forward.

        Each slot becomes a job with (schedule_id, scheduled_for) unique, so a
        scheduler that dies between the INSERT and the UPDATE, and fires the
        same slot again after restart, still creates only one job.
        """
        with self.conn.transaction():
            s = self.conn.execute("SELECT * FROM schedules WHERE id = %s AND enabled FOR UPDATE",
                                  (schedule_id,)).fetchone()
            if s is None:
                return []
            slots, nxt = due_slots(s["next_run_at"], datetime.fromtimestamp(now, timezone.utc),
                                   step_fn(s["cron"], s["every_s"]), s["misfire"], s["max_catchup"])
            created = []
            for slot in slots:
                row = self.conn.execute(
                    """INSERT INTO jobs (owner, type, payload, state, max_attempts, timeout_s, schedule_id, scheduled_for)
                       VALUES (%s, %s, %s, 'PENDING', %s, %s, %s, %s)
                       ON CONFLICT (schedule_id, scheduled_for) WHERE schedule_id IS NOT NULL DO NOTHING
                       RETURNING id""",
                    (s["owner"], s["job_type"], Jsonb(s["payload"]), s["max_attempts"], s["timeout_s"],
                     s["id"], slot)).fetchone()
                if row:
                    metrics.FIRED.inc()
                    created.append(row["id"])
                    store.add_event(self.conn, row["id"], "submit", self.id, to="PENDING",
                                    detail={"schedule": s["name"], "scheduled_for": slot.isoformat(),
                                            "misfire_policy": s["misfire"], "slots_this_fire": len(slots)})
            self.conn.execute("UPDATE schedules SET next_run_at = %s, last_fired_at = now() WHERE id = %s",
                              (nxt, s["id"]))
        # The UPDATE's NOTIFY will re-push the schedule; push now too so a test
        # driving tick() by hand sees it without a LISTEN loop.
        self.heap.push(("schedule", schedule_id), nxt.timestamp())
        if slots:
            self.log.info("fired schedule", schedule=s["name"], slots=len(slots), created=created)
        return created
    # @learn:end fire

    def snapshot(self) -> None:
        metrics.HEAP_SIZE.set(len(self.heap))
        top = [{"key": f"{k}:{i}", "due": _ts(p), "in_s": round(p - self.now(), 2)}
               for (k, i), p in self.heap.smallest(8)]
        self.conn.execute(
            """INSERT INTO scheduler_state (id, leader, heap_size, top, updated_at) VALUES (1, %s, %s, %s, now())
               ON CONFLICT (id) DO UPDATE SET leader = EXCLUDED.leader, heap_size = EXCLUDED.heap_size,
                   top = EXCLUDED.top, updated_at = now()""",
            (self.id, len(self.heap), Jsonb(top)))

    # ---- main loop ----------------------------------------------------------------
    def run_forever(self) -> None:
        self.log.info("scheduler started")
        while not self.stop.is_set() and not self.try_lead():
            self.last_tick = time.monotonic()
            self.stop.wait(2.0)  # standby
        if self.stop.is_set():
            return
        listen = connect(self.dsn)
        listen.execute("LISTEN leasejobs_scheduler")
        self.resync()
        last_resync = time.monotonic()
        while not self.stop.is_set():
            self.tick()
            self.snapshot()
            top = self.heap.peek()
            wait = 1.0 if top is None else max(0.0, min(1.0, top[1] - self.now()))
            for n in listen.notifies(timeout=wait, stop_after=1):
                self.on_notify(n.payload)
            if time.monotonic() - last_resync > RESYNC_S:  # heal anything a lost NOTIFY missed
                self.resync()
                last_resync = time.monotonic()
        listen.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--port", type=int, default=None, help="health/metrics port (8916 in the demo)")
    args = ap.parse_args()
    s = Scheduler(scheduler_id=args.id)
    if args.port:
        from .health import serve_health
        serve_health(args.port, lambda: {"leader": s.is_leader, "heap_size": len(s.heap),
                                         "tick_age_s": round(time.monotonic() - s.last_tick, 2)},
                     ready=lambda: time.monotonic() - s.last_tick < 10)
    signal.signal(signal.SIGTERM, lambda *_: s.stop.set())
    s.run_forever()


if __name__ == "__main__":
    main()
