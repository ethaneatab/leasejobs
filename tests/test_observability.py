"""Metrics, stalled-job detection, health vs readiness, and the supervisor."""
import json
import urllib.request

from conftest import ALICE, FAST
from helpers import Proc, add_job, wait_until
from leasejobs import store
from leasejobs.lease import LeaseManager


def _metric(text, name, **labels):
    want = name + ("{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else "")
    for line in text.splitlines():
        if line.startswith(want + " "):
            return float(line.split()[-1])
    raise AssertionError(f"{want} not in metrics")


def test_queue_gauges(client, conn):
    add_job(conn, "noop")
    conn.execute("UPDATE jobs SET run_at = now() - interval '90 seconds'")
    text = client.get("/metrics").text
    assert _metric(text, "leasejobs_jobs", state="PENDING") == 1
    assert 89 < _metric(text, "leasejobs_oldest_runnable_age_seconds") < 120


def test_stalled_detection_names_the_broken_part(client, conn):
    # 1. held, lease long expired, nobody reaped it (all workers gone)
    a = add_job(conn, "noop")
    lm = LeaseManager(conn, "ghost")
    lm.start(lm.claim())
    conn.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s", (a,))
    # 2. scheduled long ago and never released (no scheduler leader)
    b = add_job(conn, "noop", state="SCHEDULED")
    conn.execute("UPDATE jobs SET run_at = now() - interval '5 minutes' WHERE id = %s", (b,))
    got = {(r["reason"], r["id"]) for r in client.get("/stalled", headers=ALICE).json()}
    assert got == {("lease_expired_unreclaimed", a), ("scheduled_overdue", b)}
    text = client.get("/metrics").text
    assert _metric(text, "leasejobs_stalled_jobs", reason="lease_expired_unreclaimed") == 1
    assert _metric(text, "leasejobs_stalled_jobs", reason="scheduled_overdue") == 1


def test_running_past_timeout_with_live_heartbeats_is_stalled(client, conn):
    a = add_job(conn, "noop", timeout_s=1)
    lm = LeaseManager(conn, "slowpoke", ttl_s=60)
    lm.start(lm.claim())
    conn.execute("UPDATE job_attempts SET started_at = now() - interval '30 seconds' WHERE job_id = %s", (a,))
    assert [r["reason"] for r in client.get("/stalled", headers=ALICE).json()] == ["running_over_timeout"]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except OSError:  # not listening yet
        return 0, {}


def test_supervisor_restarts_a_killed_worker_and_the_restart_reclaims_the_job(client, conn, ledger, tmp_path):
    # one worker on 8919 so this test never collides with a running demo (8911-8913)
    sup = Proc(["-m", "leasejobs.supervisor", "--workers", "1", "--base-port", "8919", "--restart-delay", "0.5",
                "--runtime-dir", str(tmp_path / "rt")],
               env=FAST, log=tmp_path / "sup.log")
    try:
        wait_until(lambda: _get("http://127.0.0.1:8919/readyz")[0] == 200, timeout=15, msg="worker ready")
        jid = client.post("/jobs", json={"type": "call_integration", "payload": {"work_s": 2}},
                          headers=ALICE).json()["id"]
        holder = wait_until(lambda: store.get_job(conn, jid)["lease_owner"], msg="claimed")
        wait_until(lambda: conn.execute("SELECT current_job FROM workers WHERE worker_id = %s",
                                        (holder,)).fetchone()["current_job"] == jid, msg="registry shows the job")
        r = client.post(f"/workers/{holder}/kill", headers=ALICE)
        assert r.status_code == 202 and r.json()["held_job"] == jid
        job = wait_until(lambda: (j := store.get_job(conn, jid))["state"] == "SUCCEEDED" and j, timeout=20,
                         msg="job to finish elsewhere")
        restarted = wait_until(lambda: conn.execute(
            "SELECT worker_id FROM workers WHERE worker_id LIKE %s AND status = 'up'", (holder + ".g2",)).fetchone(),
            timeout=10, msg="supervisor restart")
    finally:
        sup.stop()
    assert job["fencing_token"] == 2 and job["lease_owner"] is None
    assert restarted["worker_id"] == holder + ".g2"
    assert [a["outcome"] for a in store.attempts(conn, jid)] == ["lease_expired", "succeeded"]
    assert not list((tmp_path / "rt").glob("worker-*.pid")), "supervisor left pid files behind"
