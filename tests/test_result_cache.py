from conftest import ALICE, BOB
from helpers import add_job, wait_until
from leasejobs.worker import Worker


def test_result_read_through_then_hit(client, conn, dsn):
    jid = client.post("/jobs", json={"type": "report", "payload": {"rows": 50}}, headers=ALICE).json()["id"]
    assert client.get(f"/jobs/{jid}/result", headers=ALICE).status_code == 409   # not done yet
    cache = client.app.state.cache
    assert client.app.state.cache_listening.wait(5)
    Worker("w", dsn).run_once()
    wait_until(lambda: jid in cache, timeout=5, msg="notify warm-up")
    cache.delete(jid)  # take the warm-up out of this test: check read-through alone
    a = client.get(f"/jobs/{jid}/result", headers=ALICE)
    b = client.get(f"/jobs/{jid}/result", headers=ALICE)
    assert (a.headers["X-Cache"], b.headers["X-Cache"]) == ("MISS", "HIT")
    assert a.json() == b.json() and a.json()["result"]["rows"] == 50


def test_cached_result_is_still_owner_checked(client, conn, dsn):
    jid = client.post("/jobs", json={"type": "noop"}, headers=ALICE).json()["id"]
    Worker("w", dsn).run_once()
    client.get(f"/jobs/{jid}/result", headers=ALICE)
    assert client.get(f"/jobs/{jid}/result", headers=BOB).status_code == 404


def test_success_notify_warms_the_cache(client, conn, dsn):
    assert client.app.state.cache_listening.wait(5), "cache listener never started"
    jid = add_job(conn, "noop")
    Worker("w", dsn).run_once()
    cache = client.app.state.cache
    wait_until(lambda: jid in cache, timeout=5, msg="NOTIFY to warm the cache")
    r = client.get(f"/jobs/{jid}/result", headers=ALICE)
    assert r.headers["X-Cache"] == "HIT"
    assert {"op": "put", "key": jid, "source": "notify"} in list(cache.recent)
