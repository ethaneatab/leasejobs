from leasejobs import store
from leasejobs.worker import Worker


def add(conn, type_, payload=None, **cols):
    from leasejobs.db import Jsonb
    cols = {"owner": "alice", "type": type_, "payload": Jsonb(payload or {}), "state": "PENDING", **cols}
    names = ", ".join(cols)
    return conn.execute(f"INSERT INTO jobs ({names}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                        list(cols.values())).fetchone()["id"]


def test_worker_runs_a_job_to_succeeded(conn, dsn):
    jid = add(conn, "report", {"rows": 100, "seed": 7})
    w = Worker("w-test", dsn)
    assert w.run_once() is True
    job = store.get_job(conn, jid)
    assert job["state"] == "SUCCEEDED" and job["result"]["rows"] == 100 and job["attempts"] == 1
    assert [a["outcome"] for a in store.attempts(conn, jid)] == ["succeeded"]
    assert [(e["from_state"], e["to_state"]) for e in store.events(conn, jid)] == [
        ("PENDING", "LEASED"), ("LEASED", "RUNNING"), ("RUNNING", "SUCCEEDED")]
    assert w.run_once() is False  # nothing left


def test_report_is_deterministic(conn, dsn):
    a, b = add(conn, "report", {"rows": 500, "seed": 3}), add(conn, "report", {"rows": 500, "seed": 3})
    w = Worker("w-test", dsn)
    w.run_once(), w.run_once()
    assert store.get_job(conn, a)["result"] == store.get_job(conn, b)["result"]
