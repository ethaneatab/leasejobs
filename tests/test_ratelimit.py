"""One token bucket per integration, shared by all workers through Postgres."""
import time

from conftest import FAST
from helpers import Proc, add_job, wait_until, worker
from leasejobs import store
from leasejobs.lease import LeaseManager
from leasejobs.ratelimit import RateLimiter, refill
from leasejobs.worker import Worker

CAP, RATE = 3.0, 2.0


def bucket(conn, name="test-api", cap=CAP, rate=RATE):
    conn.execute("INSERT INTO integrations (name, capacity, refill_per_s, tokens) VALUES (%s, %s, %s, %s)",
                 (name, cap, rate, cap))


def test_refill_math():
    assert refill(0, 5, 2, 1.0) == 2
    assert refill(4, 5, 2, 10) == 5          # capped
    assert refill(1, 5, 2, -3) == 1          # clock going backwards never removes tokens


def test_acquire_until_empty_then_wait_hint(conn):
    bucket(conn, cap=2, rate=0.5)
    rl = RateLimiter(conn)
    a, b, c = rl.try_acquire("test-api"), rl.try_acquire("test-api"), rl.try_acquire("test-api")
    assert a.ok and b.ok and not c.ok
    assert 1.5 < c.wait_s <= 2.0             # one token at 0.5/s is ~2 s away
    assert rl.try_acquire("not-configured").ok


def test_no_token_defers_instead_of_blocking(conn, dsn):
    bucket(conn, cap=1, rate=0.2)
    first = add_job(conn, "call_integration", {"integration": "test-api", "work_s": 0})
    second = add_job(conn, "call_integration", {"integration": "test-api", "work_s": 0})
    RateLimiter(conn).try_acquire("test-api")  # someone else just used the only token
    w = Worker("w-rl", dsn)
    w.run_once()
    job = store.get_job(conn, first)
    assert job["state"] == "SCHEDULED" and job["attempts"] == 0 and job["deferrals"] == 1
    assert job["lease_owner"] is None
    ev = store.events(conn, first)[-1]
    assert ev["kind"] == "rate_limited" and ev["detail"]["wait_s"] >= 4.9  # 1 token at 0.2/s, plus jitter
    # the worker is free at once: it takes the next job right away
    assert LeaseManager(conn, "probe").claim()["id"] == second


def accepted_times(ledger, integration):
    return sorted(e["at"] for k in ledger.keys.values() for e in k["accepted"] if e["integration"] == integration)


def run_burst(conn, ledger, tmp_path, n=16):
    bucket(conn)
    ids = [add_job(conn, "call_integration", {"integration": "test-api", "work_s": 0}) for _ in range(n)]
    sched = Proc(["-m", "leasejobs.scheduler", "--id", "sched-rl"], log=tmp_path / "s.log")
    ws = [worker(f"rl-{i}", env=FAST, log=tmp_path / f"w{i}.log") for i in range(3)]
    try:
        wait_until(lambda: conn.execute("SELECT count(*) AS n FROM jobs WHERE state = 'SUCCEEDED'").fetchone()["n"]
                   == n, timeout=60, msg="burst to finish")
    finally:
        for p in [sched, *ws]:
            p.stop()
    return ids, accepted_times(ledger, "test-api")


def test_shared_bucket_holds_the_global_rate_across_three_workers(conn, ledger, tmp_path):
    """For every prefix of the run, calls made <= capacity + rate * elapsed (+1 for clock slop)."""
    ids, ts = run_burst(conn, ledger, tmp_path)
    t0 = ts[0]
    worst = max(i + 1 - (CAP + RATE * (t - t0)) for i, t in enumerate(ts))
    deferred = conn.execute("SELECT sum(deferrals) AS d FROM jobs").fetchone()["d"]
    print(f"\n{len(ts)} calls over {ts[-1] - t0:.2f}s; worst excess over the bucket bound {worst:.2f}; "
          f"{deferred} deferrals")
    assert len(ts) == len(ids)
    assert worst <= 1.0, "more calls than the bucket allows"
    assert deferred > 0
