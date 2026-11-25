"""Leases expire, other workers reclaim, and late writes from the old holder bounce.

These are the tests the failing controls switch off:
  LEASEJOBS_LAB_SABOTAGE=no_reclaim  -> test_kill9_* stays stuck and fails
  LEASEJOBS_LAB_SABOTAGE=no_fencing  -> test_stale_* / test_sigstop_* see the old worker's write win
"""
import signal
import time

from conftest import FAST
from helpers import add_job, wait_until, worker
from leasejobs import store
from leasejobs.lease import LeaseLost, LeaseManager
from leasejobs.states import State


def expire_now(conn, job_id):
    """Pretend the holder went quiet: move its expiry into the past."""
    conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (job_id,))


def _lease_view(conn, jid):
    r = conn.execute("SELECT state, lease_owner, fencing_token, round(extract(epoch FROM now() - lease_expires_at)"
                     "::numeric, 1) AS expired_s_ago FROM jobs WHERE id = %s", (jid,)).fetchone()
    return dict(r)


def test_heartbeat_pushes_the_expiry_forward(conn):
    add_job(conn, "noop")
    lm = LeaseManager(conn, "w-a", ttl_s=2)
    job = lm.claim()
    time.sleep(0.3)
    row = lm.heartbeat(job)
    assert row["lease_expires_at"] > job["lease_expires_at"]


def test_reaper_returns_expired_job_and_next_claim_gets_a_bigger_token(conn):
    jid = add_job(conn, "noop")
    a, b = LeaseManager(conn, "w-a"), LeaseManager(conn, "w-b")
    ja = a.claim()
    a.start(ja)
    assert ja["fencing_token"] == 1
    expire_now(conn, jid)
    reaped = b.reap_expired()
    assert [r["id"] for r in reaped] == [jid] and reaped[0]["state"] == "PENDING"
    jb = b.claim()
    assert jb["id"] == jid and jb["fencing_token"] == 2 and jb["lease_owner"] == "w-b"
    ev = [e for e in store.events(conn, jid) if e["kind"] == "lease_expired"][0]
    assert ev["actor"] == "w-b" and ev["detail"]["expired_owner"] == "w-a" and ev["detail"]["fencing_token"] == 1
    assert [a_["outcome"] for a_ in store.attempts(conn, jid)] == ["lease_expired"]


def test_stale_holder_cannot_heartbeat_or_finish(conn):
    """The core fencing test, in-process so it is exact."""
    jid = add_job(conn, "noop")
    a, b = LeaseManager(conn, "w-a"), LeaseManager(conn, "w-b")
    ja = a.start(a.claim())
    expire_now(conn, jid)
    b.reap_expired()
    jb = b.start(b.claim())
    # w-a wakes up and tries to keep going, then to publish its result.
    try:
        a.heartbeat(ja)
        heartbeat_bounced = False
    except LeaseLost:
        heartbeat_bounced = True
    a_wrote = a.finish(ja, State.SUCCEEDED, result={"by": "w-a"}, outcome="succeeded")
    b_wrote = b.finish(jb, State.SUCCEEDED, result={"by": "w-b"}, outcome="succeeded")
    final = store.get_job(conn, jid)
    assert final["result"] == {"by": "w-b"}, f"stale worker overwrote the result: {final['result']}"
    assert heartbeat_bounced and a_wrote is None and b_wrote is not None
    stale = [e["detail"] for e in store.events(conn, jid) if e["kind"] == "stale_write_rejected"]
    assert {(d["op"], d["my_token"], d["current_token"]) for d in stale} == {("heartbeat", 1, 2), ("finish", 1, 2)}


def test_expiry_on_the_last_attempt_is_terminal(conn):
    jid = add_job(conn, "noop", max_attempts=1)
    a = LeaseManager(conn, "w-a")
    a.start(a.claim())
    expire_now(conn, jid)
    LeaseManager(conn, "w-b").reap_expired()
    job = store.get_job(conn, jid)
    assert job["state"] == "FAILED_TERMINAL" and "lease expired" in job["last_error"]


def test_kill9_worker_job_is_reclaimed_with_history_intact(conn, ledger, tmp_path):
    jid = add_job(conn, "call_integration", {"work_s": 3, "integration": "ledger-api"})
    w1 = worker("w1", env=FAST, log=tmp_path / "w1.log")
    w2 = None
    try:
        wait_until(lambda: (j := store.get_job(conn, jid))["state"] == "RUNNING" and j["lease_owner"] == "w1",
                   msg="w1 running the job")
        killed_at = time.monotonic()
        w1.kill9()  # by PID, the process we started
        w2 = worker("w2", env=FAST, log=tmp_path / "w2.log")
        job = wait_until(lambda: (j := store.get_job(conn, jid))["state"] == "SUCCEEDED" and j, timeout=15,
                         msg="another worker to reclaim and finish the job",
                         describe=lambda: _lease_view(conn, jid))
        took = time.monotonic() - killed_at
    finally:
        w1.stop()
        if w2:
            w2.stop()
    hist = [(a["attempt_no"], a["worker_id"], a["fencing_token"], a["outcome"]) for a in store.attempts(conn, jid)]
    assert hist == [(1, "w1", 1, "lease_expired"), (2, "w2", 2, "succeeded")]
    assert job["result"]["worker"] == "w2" and job["fencing_token"] == 2
    print(f"\nkill -9 -> SUCCEEDED on w2 in {took:.2f}s (lease ttl 1.5s, job work 3s)")


def test_sigstop_worker_revives_and_is_fenced_out(conn, ledger, tmp_path):
    """w1 is not dead, only stopped. When it wakes up it tries to write; both the
    integration and our database must refuse its old token."""
    jid = add_job(conn, "call_integration", {"work_s": 1.0, "key": "acct-7"})
    w1 = worker("w1", env=FAST, log=tmp_path / "w1.log")
    w2 = None
    try:
        wait_until(lambda: store.get_job(conn, jid)["state"] == "RUNNING", msg="w1 running")
        w1.signal(signal.SIGSTOP)
        w2 = worker("w2", env=FAST, log=tmp_path / "w2.log")
        wait_until(lambda: store.get_job(conn, jid)["state"] == "SUCCEEDED", timeout=15, msg="w2 to finish")
        w1.signal(signal.SIGCONT)
        wait_until(lambda: any(e["kind"] == "stale_write_rejected" and e["actor"] == "w1"
                               for e in store.events(conn, jid)), timeout=10, msg="w1's late write to bounce")
        time.sleep(0.5)
    finally:
        w1.stop()
        if w2:
            w2.stop()
    job = store.get_job(conn, jid)
    acct = ledger.keys["acct-7"]
    assert job["result"]["worker"] == "w2", f"result came from {job['result']['worker']}"
    assert [w["worker"] for w in acct["accepted"]] == ["w2"], f"accepted writes: {acct['accepted']}"
    assert [w["token"] for w in acct["rejected"]] == [1]
