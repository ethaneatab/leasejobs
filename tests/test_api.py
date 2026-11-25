from conftest import ALICE, BOB


def submit(client, headers=ALICE, **body):
    body.setdefault("type", "noop")
    return client.post("/jobs", json=body, headers=headers)


def test_submit_then_query(client):
    r = submit(client, type="report", payload={"rows": 10})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["state"] == "PENDING" and job["owner"] == "alice" and job["attempts"] == 0
    assert client.get(f"/jobs/{job['id']}", headers=ALICE).json()["id"] == job["id"]
    assert [j["id"] for j in client.get("/jobs", headers=ALICE).json()] == [job["id"]]
    ev = client.get(f"/jobs/{job['id']}/events", headers=ALICE).json()
    assert [(e["kind"], e["to_state"]) for e in ev] == [("submit", "PENDING")]


def test_missing_or_wrong_key_is_401(client):
    assert client.post("/jobs", json={"type": "noop"}).status_code == 401
    assert submit(client, headers={"X-API-Key": "nope"}).status_code == 401


def test_invalid_requests_are_422(client):
    assert submit(client, type="does-not-exist").status_code == 422
    assert submit(client, type="sleep", payload={"seconds": -1}).status_code == 422
    assert submit(client, max_attempts=0).status_code == 422
    assert submit(client, timeout_s=0).status_code == 422
    assert submit(client, surprise_field=1).status_code == 422
    assert client.post("/jobs", content=b"{not json", headers={**ALICE, "content-type": "application/json"}
                       ).status_code == 422


def test_other_owners_jobs_are_invisible(client):
    jid = submit(client).json()["id"]
    assert client.get(f"/jobs/{jid}", headers=BOB).status_code == 404
    assert client.get("/jobs", headers=BOB).json() == []
    assert client.post(f"/jobs/{jid}/cancel", headers=BOB).status_code == 404


def test_idempotency_key_returns_the_same_job(client):
    a = submit(client, idempotency_key="k1")
    b = submit(client, idempotency_key="k1")
    assert a.status_code == 201 and b.status_code == 200 and a.json()["id"] == b.json()["id"]
    # same key, different owner: a different job
    assert submit(client, headers=BOB, idempotency_key="k1").json()["id"] != a.json()["id"]


def test_cancel_pending_then_cancel_again_is_409(client):
    jid = submit(client).json()["id"]
    r = client.post(f"/jobs/{jid}/cancel", headers=ALICE)
    assert r.status_code == 200 and r.json()["state"] == "CANCELLED"
    assert client.post(f"/jobs/{jid}/cancel", headers=ALICE).status_code == 409


def test_health_and_readiness(client):
    assert client.get("/healthz").json() == {"status": "alive"}
    assert client.get("/readyz").json()["status"] == "ready"
