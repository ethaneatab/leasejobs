"""The /learn sample against real processes: every stage must light up from real rows."""
from conftest import ALICE
from helpers import Proc, wait_until, worker


def test_learn_run_lights_every_stage_in_order(client, conn, ledger, tmp_path):
    procs = [Proc(["-m", "leasejobs.scheduler", "--id", "sched-learn"], log=tmp_path / "s.log")]
    procs += [worker(f"lw-{i}", log=tmp_path / f"w{i}.log") for i in range(2)]  # default 4 s TTL
    try:
        wait_until(lambda: conn.execute("SELECT 1 FROM scheduler_state").fetchone(), msg="scheduler")
        run = client.post("/learn/runs", headers=ALICE).json()
        assert [s["status"] for s in run["stages"]][:2] == ["done", "active"]

        def view():
            return client.get(f"/learn/runs/{run['run']}", headers=ALICE).json()
        wait_until(lambda: view()["stages"][9]["status"] == "done", timeout=40, msg="done stage",
                   describe=lambda: [(s["id"], s["status"]) for s in view()["stages"]])
        r = client.get(f"/jobs/{run['job_id']}/result", headers=ALICE)
        assert r.headers["X-Cache"] == "HIT"   # warmed by NOTIFY, so even the first read hits
        v = view()
    finally:
        for p in procs:
            p.stop()
    st = {s["id"]: s for s in v["stages"]}
    assert all(s["status"] == "done" for s in v["stages"]) and v["finished"]
    a, b = st["claim"]["data"]["lease_owner"], st["reclaim"]["data"]["lease_owner"]
    assert a != b and st["claim"]["data"]["fencing_token"] == 1 and st["reclaim"]["data"]["fencing_token"] == 2
    assert st["expire"]["data"]["expired_owner"] == a and st["expire"]["data"]["reaped_by"] == b
    assert st["stale"]["data"]["worker"] == a and st["stale"]["data"]["op"] == "finish"
    assert st["done"]["data"]["result"]["worker"] == b
    assert [x["outcome"] for x in st["done"]["data"]["attempts"]] == ["lease_expired", "succeeded"]
    times = [s["at"] for s in v["stages"] if s["at"]]
    assert times[:8] == sorted(times[:8]), "stages up to reclaim happened in page order"
