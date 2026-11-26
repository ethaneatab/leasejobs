"""Delayed jobs, recurring schedules, misfires, restarts, and leadership."""
import time
from datetime import timedelta

from conftest import ALICE, FAST
from helpers import Proc, wait_until, worker
from leasejobs import store
from leasejobs.scheduler import Scheduler


def test_delayed_job_is_released_only_when_due(client, conn, dsn):
    job = client.post("/jobs", json={"type": "noop", "delay_s": 30}, headers=ALICE).json()
    assert job["state"] == "SCHEDULED"
    s = Scheduler(dsn, "sched-test")
    s.resync()
    due = job_run_at = store.get_job(conn, job["id"])["run_at"].timestamp()
    assert s.tick(now=due - 1) == [] and store.get_job(conn, job["id"])["state"] == "SCHEDULED"
    assert s.tick(now=due + 0.2) == [("job", job["id"])]
    assert store.get_job(conn, job["id"])["state"] == "PENDING"
    ev = store.events(conn, job["id"])[-1]
    assert (ev["kind"], ev["from_state"], ev["to_state"]) == ("due", "SCHEDULED", "PENDING")
    assert ev["detail"]["late_ms"] == 200 and job_run_at == due


def test_restart_rebuilds_the_heap_from_postgres(client, conn, dsn):
    ids = [client.post("/jobs", json={"type": "noop", "delay_s": d}, headers=ALICE).json()["id"] for d in (50, 10, 30)]
    s1 = Scheduler(dsn, "s1")
    s1.resync()
    del s1  # "crash": nothing in memory survives
    s2 = Scheduler(dsn, "s2")
    s2.resync()
    assert [k for k, _ in s2.heap.smallest(3)] == [("job", ids[1]), ("job", ids[2]), ("job", ids[0])]


def test_cancelled_delayed_job_is_not_released(client, conn, dsn):
    jid = client.post("/jobs", json={"type": "noop", "delay_s": 5}, headers=ALICE).json()["id"]
    s = Scheduler(dsn, "s")
    s.resync()
    assert client.post(f"/jobs/{jid}/cancel", headers=ALICE).json()["state"] == "CANCELLED"
    s.tick(now=time.time() + 60)
    assert store.get_job(conn, jid)["state"] == "CANCELLED"


def _schedule(client, conn, misfire, missed_minutes, cap=10):
    sch = client.post("/schedules", json={"name": f"rollup-{misfire}", "every_s": 60, "misfire": misfire,
                                          "max_catchup": cap, "job": {"type": "noop"}}, headers=ALICE).json()
    conn.execute("UPDATE schedules SET next_run_at = now() - make_interval(secs => %s * 60) WHERE id = %s",
                 (missed_minutes, sch["id"]))
    return sch["id"]


def test_misfire_skip_enqueues_one_run(client, conn, dsn):
    sid = _schedule(client, conn, "skip", 42.5)
    s = Scheduler(dsn, "s")
    s.resync()
    s.tick()
    jobs = conn.execute("SELECT * FROM jobs WHERE schedule_id = %s", (sid,)).fetchall()
    nxt = conn.execute("SELECT next_run_at FROM schedules WHERE id = %s", (sid,)).fetchone()["next_run_at"]
    assert len(jobs) == 1 and jobs[0]["state"] == "PENDING"
    assert timedelta(0) < nxt - conn.execute("SELECT now() AS t").fetchone()["t"] <= timedelta(seconds=60)


def test_misfire_catch_up_is_capped(client, conn, dsn):
    sid = _schedule(client, conn, "catch_up", 42.5, cap=5)
    s = Scheduler(dsn, "s")
    s.resync()
    s.tick()
    slots = [r["scheduled_for"] for r in conn.execute(
        "SELECT scheduled_for FROM jobs WHERE schedule_id = %s ORDER BY scheduled_for", (sid,))]
    assert len(slots) == 5 and all(b - a == timedelta(minutes=1) for a, b in zip(slots, slots[1:]))


def test_refiring_the_same_slot_creates_no_second_job(client, conn, dsn):
    sid = _schedule(client, conn, "skip", 0.5)
    before = conn.execute("SELECT next_run_at FROM schedules WHERE id = %s", (sid,)).fetchone()["next_run_at"]
    s = Scheduler(dsn, "s")
    s.fire(sid, time.time())
    # crash before next_run_at was saved: put it back and fire again
    conn.execute("UPDATE schedules SET next_run_at = %s WHERE id = %s", (before, sid))
    assert s.fire(sid, time.time()) == []
    assert conn.execute("SELECT count(*) AS n FROM jobs WHERE schedule_id = %s", (sid,)).fetchone()["n"] == 1


def test_only_one_scheduler_leads(dsn, conn):
    a, b = Scheduler(dsn, "a"), Scheduler(dsn, "b")
    assert a.try_lead() is True and b.try_lead() is False
    a.conn.close()  # leader dies; its session lock goes with it
    assert wait_until(lambda: b.try_lead(), timeout=5, msg="standby takeover")


def test_end_to_end_delay_with_real_processes(client, conn, tmp_path):
    sched = Proc(["-m", "leasejobs.scheduler", "--id", "sched-e2e"], log=tmp_path / "sched.log")
    w = worker("w-e2e", env=FAST, log=tmp_path / "w.log")
    try:
        wait_until(lambda: conn.execute("SELECT leader FROM scheduler_state").fetchone(), msg="scheduler up")
        job = client.post("/jobs", json={"type": "noop", "delay_s": 1.0}, headers=ALICE).json()
        wait_until(lambda: store.get_job(conn, job["id"])["state"] == "SUCCEEDED", timeout=15, msg="job done")
    finally:
        sched.stop()
        w.stop()
    ev = {e["kind"]: e for e in store.events(conn, job["id"])}
    run_at = store.get_job(conn, job["id"])["run_at"]
    assert ev["heap_push"]["actor"] == "sched-e2e" and ev["heap_push"]["detail"]["heap_size"] >= 1
    assert ev["due"]["at"] >= run_at, "released before run_at"
    print(f"\nreleased {ev['due']['detail']['late_ms']} ms after run_at")
