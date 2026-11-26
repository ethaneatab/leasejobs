"""Repo benchmarks. Each number is the median of N runs on this machine.

  .venv/bin/python scripts/bench.py [--runs 5] > docs/results/bench.txt

Uses its own database (leasejobs_bench) so it never touches the demo or tests.
Environment: whatever machine runs it; the numbers in README come from a laptop
with Postgres in colima (4 vCPU shared with other projects).
"""
from __future__ import annotations

import argparse
import os
import signal
import statistics
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from leasejobs.cache import LRUCache  # noqa: E402
from leasejobs.db import connect  # noqa: E402
from leasejobs.migrate import migrate  # noqa: E402

ADMIN = os.environ.get("LEASEJOBS_DSN", "postgresql://leasejobs:leasejobs-dev@127.0.0.1:54311/leasejobs")
DSN = ADMIN.rsplit("/", 1)[0] + "/leasejobs_bench"


def fresh_db():
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS leasejobs_bench WITH (FORCE)")
        c.execute("CREATE DATABASE leasejobs_bench")
    migrate(DSN)


def workers(n, env=None, prefix="b"):
    e = {**os.environ, "LEASEJOBS_DSN": DSN, "LEASEJOBS_POLL_S": "0.05", **(env or {})}
    return [subprocess.Popen([sys.executable, "-m", "leasejobs.worker", "--id", f"{prefix}{i}", "--drain", "1.5"],
                             cwd=ROOT, env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(n)]


def claim_throughput(n_workers, jobs, sabotage=""):
    fresh_db()
    with connect(DSN) as c:
        c.execute("INSERT INTO jobs (owner, type, state) SELECT 'bench', 'noop', 'PENDING' FROM generate_series(1, %s)",
                  (jobs,))
        ps = workers(n_workers, {"LEASEJOBS_LAB_SABOTAGE": sabotage})
        for p in ps:
            p.wait(300)
        r = c.execute("SELECT count(*) FILTER (WHERE state = 'SUCCEEDED') AS ok, "
                      "extract(epoch FROM max(finished_at) - min(leased_at)) AS span FROM jobs").fetchone()
        dup = c.execute("SELECT count(*) AS n FROM job_attempts GROUP BY job_id HAVING count(*) > 1").fetchall()
    assert r["ok"] == jobs and not dup, (r, dup)
    return jobs / float(r["span"])


def recovery_once():
    """kill -9 the worker holding a running job; how long until another worker holds it, and until it's done."""
    fresh_db()
    env = {**os.environ, "LEASEJOBS_DSN": DSN}
    with connect(DSN) as c:
        jid = c.execute("INSERT INTO jobs (owner, type, payload, state) VALUES ('bench', 'sleep', '{\"seconds\": 2}', "
                        "'PENDING') RETURNING id").fetchone()["id"]
        a = subprocess.Popen([sys.executable, "-m", "leasejobs.worker", "--id", "victim"], cwd=ROOT, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while c.execute("SELECT state FROM jobs WHERE id = %s", (jid,)).fetchone()["state"] != "RUNNING":
            time.sleep(0.02)
        time.sleep(0.5)
        b = subprocess.Popen([sys.executable, "-m", "leasejobs.worker", "--id", "rescuer"], cwd=ROOT, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)  # rescuer is up and idle-polling before the kill
        killed = c.execute("SELECT now() AS t").fetchone()["t"]
        os.kill(a.pid, signal.SIGKILL)
        a.wait()
        while c.execute("SELECT state FROM jobs WHERE id = %s", (jid,)).fetchone()["state"] != "SUCCEEDED":
            time.sleep(0.05)
        ev = {e["kind"] + ":" + e["actor"]: e["at"] for e in c.execute(
            "SELECT kind, actor, at FROM job_events WHERE job_id = %s AND kind IN ('claim', 'finish')", (jid,))}
        b.send_signal(signal.SIGTERM)
        b.wait(10)
    return ((ev["claim:rescuer"] - killed).total_seconds(), (ev["finish:rescuer"] - killed).total_seconds())


class ODLRU:
    def __init__(self, cap):
        self.cap, self.d = cap, OrderedDict()

    def get(self, k, default=None):
        if k in self.d:
            self.d.move_to_end(k)
            return self.d[k]
        return default

    def put(self, k, v):
        self.d[k] = v
        self.d.move_to_end(k)
        if len(self.d) > self.cap:
            self.d.popitem(last=False)


def lru_ns(cls, size, ops=200_000):
    c = cls(size)
    for i in range(size):
        c.put(i, i)
    keys = [(i * 7919) % (size * 2) for i in range(ops)]
    t = time.perf_counter()
    for k in keys:
        if c.get(k) is None:
            c.put(k, k)
    return (time.perf_counter() - t) / ops * 1e9


def med(xs):
    return statistics.median(xs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    args = ap.parse_args()
    n = args.runs
    print(f"# leasejobs bench, median of {n} runs; {time.strftime('%Y-%m-%d %H:%M')}; python {sys.version.split()[0]}")
    print("# Postgres 16 in colima (4 vCPU / 6 GiB shared with other projects); workers on the host.\n")
    for label, sab in (("SKIP LOCKED", ""), ("plain FOR UPDATE (no_skip_locked)", "no_skip_locked")):
        for w in (1, 4, 8):
            xs = [claim_throughput(w, 2000, sab) for _ in range(n)]
            print(f"claim+run noop jobs, {w} worker(s), {label}: median {med(xs):.0f} jobs/s "
                  f"(min {min(xs):.0f}, max {max(xs):.0f}); 0 duplicate attempts")
    print()
    rec = [recovery_once() for _ in range(n)]
    print(f"kill -9 recovery (lease ttl 4 s, heartbeat 1 s, 2 s job): kill -> reclaimed median "
          f"{med([r[0] for r in rec]):.2f} s (min {min(r[0] for r in rec):.2f}, max {max(r[0] for r in rec):.2f}); "
          f"kill -> SUCCEEDED median {med([r[1] for r in rec]):.2f} s")
    print()
    for size in (1_000, 100_000):
        ours = med([lru_ns(LRUCache, size) for _ in range(n)])
        od = med([lru_ns(ODLRU, size) for _ in range(n)])
        print(f"LRU get+put at {size:>6} entries: ours {ours:.0f} ns/op, OrderedDict version {od:.0f} ns/op")


if __name__ == "__main__":
    main()
