"""Many worker processes, one queue: every job runs exactly once."""
import time

import psycopg

from helpers import add_job, add_jobs, worker
from leasejobs.lease import LeaseManager


def test_four_processes_run_each_job_exactly_once(conn):
    ids = add_jobs(conn, "tally", 400)
    t0 = time.monotonic()
    procs = [worker(f"cw-{i}", "--drain", "1.0") for i in range(4)]
    try:
        for p in procs:
            assert p.wait(90) == 0
    finally:
        for p in procs:
            p.stop()
    elapsed = time.monotonic() - t0
    per_job = conn.execute("SELECT job_id, count(*) AS n FROM tally GROUP BY job_id").fetchall()
    assert len(per_job) == len(ids) and {r["n"] for r in per_job} == {1}, "a job body ran twice or never"
    states = conn.execute("SELECT state, attempts, count(*) AS n FROM jobs GROUP BY 1, 2").fetchall()
    assert [(r["state"], r["attempts"], r["n"]) for r in states] == [("SUCCEEDED", 1, 400)]
    spread = conn.execute("SELECT worker_id, count(*) AS n FROM tally GROUP BY 1 ORDER BY 1").fetchall()
    assert len(spread) >= 2, f"work was not shared: {spread}"
    print(f"\n400 jobs / 4 worker processes in {elapsed:.2f}s (incl. startup); per worker: "
          + ", ".join(f"{r['worker_id']}={r['n']}" for r in spread))


def test_skip_locked_skips_a_row_another_transaction_holds(conn, dsn):
    first, second = add_job(conn, "noop"), add_job(conn, "noop")
    with psycopg.connect(dsn) as holder:  # not autocommit: the lock lives until we leave the block
        holder.execute("SELECT id FROM jobs WHERE id = %s FOR UPDATE", (first,))
        with psycopg.connect(dsn, autocommit=True) as c2:
            from psycopg.rows import dict_row
            c2.row_factory = dict_row
            c2.execute("SET statement_timeout = '2s'")  # if we block instead of skipping, this fails
            got = LeaseManager(c2, "w-skip").claim()
    assert got["id"] == second


def test_claim_sets_owner_and_db_clock_expiry(conn):
    jid = add_job(conn, "noop")
    row = LeaseManager(conn, "w-a", ttl_s=4).claim()
    assert row["id"] == jid and row["state"] == "LEASED" and row["lease_owner"] == "w-a"
    secs = (row["lease_expires_at"] - row["leased_at"]).total_seconds()
    assert abs(secs - 4) < 0.01
    assert LeaseManager(conn, "w-b").claim() is None  # nothing else runnable


def test_priority_then_age_order(conn):
    low = add_job(conn, "noop", priority=0)
    high = add_job(conn, "noop", priority=5)
    lm = LeaseManager(conn, "w")
    assert [lm.claim()["id"], lm.claim()["id"]] == [high, low]
