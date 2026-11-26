"""Retries, retry limits, timeouts, cancellation, and failing dependencies."""
import time

import httpx

from conftest import ALICE, FAST, TEST_INTEGRATION_PORT
from helpers import add_job, wait_until, worker
from leasejobs import store
from leasejobs.scheduler import Scheduler
from leasejobs.worker import Worker, backoff_s


def drive(conn, dsn, jid, max_rounds=10):
    """Run worker + scheduler by hand until the job is terminal. Backoff is
    skipped by pulling run_at to now before each scheduler tick."""
    w, s = Worker("w-f", dsn), Scheduler(dsn, "s-f")
    for _ in range(max_rounds):
        w.run_once()
        state = store.get_job(conn, jid)["state"]
        if state in ("SUCCEEDED", "FAILED_TERMINAL", "TIMED_OUT", "CANCELLED"):
            return store.get_job(conn, jid)
        conn.execute("UPDATE jobs SET run_at = now() WHERE id = %s AND state = 'SCHEDULED'", (jid,))
        s.resync()
        s.tick()
    raise AssertionError(f"job {jid} not terminal: {store.get_job(conn, jid)['state']}")


def outcomes(conn, jid):
    return [a["outcome"] for a in store.attempts(conn, jid)]


def test_backoff_grows_and_is_capped(monkeypatch):
    monkeypatch.setenv("LEASEJOBS_RETRY_BASE_S", "1")
    monkeypatch.setenv("LEASEJOBS_RETRY_CAP_S", "8")
    for attempt, (lo, hi) in {1: (0.5, 1), 2: (1, 2), 3: (2, 4), 6: (4, 8)}.items():
        assert all(lo <= backoff_s(attempt) <= hi for _ in range(50))


def test_transient_failures_retry_until_success(conn, dsn):
    jid = add_job(conn, "flaky", {"fail_times": 2}, max_attempts=3)
    job = drive(conn, dsn, jid)
    assert job["state"] == "SUCCEEDED" and job["attempts"] == 3
    assert outcomes(conn, jid) == ["failed", "failed", "succeeded"]
    retries = [e for e in store.events(conn, jid) if e["kind"] == "retry_scheduled"]
    assert [e["detail"]["attempt"] for e in retries] == [1, 2]
    assert [(e["from_state"], e["to_state"]) for e in retries] == [("FAILED_RETRYABLE", "SCHEDULED")] * 2


def test_retry_limit_ends_in_failed_terminal(conn, dsn):
    jid = add_job(conn, "flaky", {"fail_times": 5}, max_attempts=3)
    job = drive(conn, dsn, jid)
    assert job["state"] == "FAILED_TERMINAL" and job["attempts"] == 3 and "attempt 3" in job["last_error"]


def test_permanent_error_does_not_retry(conn, dsn):
    jid = add_job(conn, "flaky", {"fail_times": 5, "terminal": True}, max_attempts=3)
    job = drive(conn, dsn, jid)
    assert job["state"] == "FAILED_TERMINAL" and job["attempts"] == 1


def test_timeout_on_last_attempt_is_timed_out(conn, dsn):
    jid = add_job(conn, "sleep", {"seconds": 5}, max_attempts=2, timeout_s=0.3)
    job = drive(conn, dsn, jid)
    assert job["state"] == "TIMED_OUT" and outcomes(conn, jid) == ["timed_out", "timed_out"]


def test_a_job_that_ignores_its_deadline_is_abandoned(conn, dsn):
    jid = add_job(conn, "block", {"seconds": 3}, max_attempts=1, timeout_s=0.3)
    t = time.monotonic()
    Worker("w-f", dsn).run_once()
    took = time.monotonic() - t
    job = store.get_job(conn, jid)
    assert job["state"] == "TIMED_OUT" and "abandoned" in job["last_error"]
    assert took < 3, "worker waited for the blocking job instead of enforcing the deadline"


def test_dependency_5xx_is_retried(conn, dsn, ledger):
    httpx.post(f"http://127.0.0.1:{TEST_INTEGRATION_PORT}/admin/mode", json={"mode": "fail500", "count": 2})
    jid = add_job(conn, "call_integration", {"work_s": 0}, max_attempts=3)
    job = drive(conn, dsn, jid)
    assert job["state"] == "SUCCEEDED" and outcomes(conn, jid) == ["failed", "failed", "succeeded"]
    assert "integration 503" in store.attempts(conn, jid)[0]["error"]


def test_dependency_4xx_is_terminal(conn, dsn, ledger):
    httpx.post(f"http://127.0.0.1:{TEST_INTEGRATION_PORT}/admin/mode", json={"mode": "fail400", "count": 1})
    jid = add_job(conn, "call_integration", {"work_s": 0}, max_attempts=3)
    job = drive(conn, dsn, jid)
    assert job["state"] == "FAILED_TERMINAL" and job["attempts"] == 1 and "TerminalError" in job["last_error"]


def test_dependency_down_exhausts_retries(conn, dsn, monkeypatch):
    monkeypatch.setenv("LEASEJOBS_INTEGRATION_URL", "http://127.0.0.1:8917")  # nothing listens here
    jid = add_job(conn, "call_integration", {"work_s": 0}, max_attempts=2)
    job = drive(conn, dsn, jid)
    assert job["state"] == "FAILED_TERMINAL" and "integration unreachable" in job["last_error"]


def test_cancel_reaches_a_running_job_through_the_heartbeat(client, conn, tmp_path):
    jid = client.post("/jobs", json={"type": "sleep", "payload": {"seconds": 30}}, headers=ALICE).json()["id"]
    w = worker("w-c", env=FAST, log=tmp_path / "w.log")
    try:
        wait_until(lambda: store.get_job(conn, jid)["state"] == "RUNNING", msg="running")
        r = client.post(f"/jobs/{jid}/cancel", headers=ALICE)
        assert r.status_code == 202 and r.json()["cancel_requested"] is True
        wait_until(lambda: store.get_job(conn, jid)["state"] == "CANCELLED", timeout=5, msg="cancelled")
    finally:
        w.stop()
    assert outcomes(conn, jid) == ["cancelled"]
