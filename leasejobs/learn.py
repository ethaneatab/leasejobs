"""Teaching mode: one sample job that walks through every mechanism, and a
view that reads its real state back as a list of stages.

The sample is an ordinary call_integration job with a failpoint: on attempt 1,
right before writing its result, the worker SIGSTOPs itself for FREEZE_S
seconds. Its lease expires, another worker reclaims the job with a bigger
fencing token, and when the frozen worker wakes up its write is refused.
Nothing on the page is animated by a timer: each stage lights up when the
matching row exists in job_events (or the cache saw a hit).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from . import store
from .db import Jsonb
from .ratelimit import RateLimiter

DELAY_S = 2.0
WORK_S = 3.0
FREEZE_S = 6.0

# id, label, part (video), anchor (source link). Order is the order things happen.
STAGES = [
    ("submit", "提交样例任务", "p1-submit", "transition"),
    ("heap", "进调度堆", "p2-heap", "sched-push"),
    ("claim", "到期，worker 认领", "p3-claim", "claim"),
    ("token", "拿集成的令牌", "p4-ratelimit", "bucket"),
    ("heartbeat", "执行中，心跳续租", "p5-heartbeat", "heartbeat"),
    ("stall", "worker 卡死（SIGSTOP）", "p5-heartbeat", "freeze"),
    ("expire", "租约过期", "p5-heartbeat", "reap"),
    ("reclaim", "另一个 worker 回收，token 加一", "p6-fencing", "fence"),
    ("stale", "旧 worker 醒来，写结果被拒", "p6-fencing", "finish"),
    ("done", "成功，结果进 LRU 缓存", "p7-cache", "cache-warm"),
    ("cache", "再查一次，命中缓存", "p7-cache", "lru-core"),
]


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def sample_payload(n: int) -> dict:
    return {"integration": "ledger-api", "work_s": WORK_S, "key": f"learn-{n}", "amount": 42,
            "_failpoint": {"at": "before_commit", "attempt": 1, "freeze_s": FREEZE_S}}


def build_stages(conn, run: dict, cache) -> dict:
    jid = run["job_id"]
    job = store.get_job(conn, jid)
    ev = store.events(conn, jid)
    by_kind: dict[str, list[dict]] = {}
    for e in ev:
        by_kind.setdefault(e["kind"], []).append(e)
    claims = by_kind.get("claim", [])
    first_owner = claims[0]["actor"] if claims else None
    second = claims[1] if len(claims) > 1 else None
    hbs = [e for e in by_kind.get("heartbeat", []) if e["actor"] == first_owner]
    stale = [e for e in by_kind.get("stale_write_rejected", []) if e["actor"] == first_owner]
    done = [e for e in by_kind.get("finish", []) if e["to_state"] == "SUCCEEDED"]
    hits = [r for r in list(cache.recent) if r.get("key") == jid and r["op"] == "hit"]
    puts = [r for r in list(cache.recent) if r.get("key") == jid and r["op"] == "put"]

    def ev_view(e):
        return {"at": _iso(e["at"]), "kind": e["kind"], "from": e["from_state"], "to": e["to_state"],
                "actor": e["actor"], "detail": e["detail"]}

    data: dict[str, Any] = {}
    at: dict[str, Any] = {}
    if by_kind.get("submit"):
        e = by_kind["submit"][0]
        at["submit"] = e["at"]
        data["submit"] = {"job": {k: _iso(job[k]) for k in ("id", "owner", "type", "state", "run_at",
                                                              "max_attempts", "timeout_s", "fencing_token")},
                          "payload": job["payload"], "transition": f"(new) → {e['to_state']}"}
    if by_kind.get("heap_push"):
        e = by_kind["heap_push"][0]
        at["heap"] = e["at"]
        data["heap"] = {**e["detail"], "due_event": ev_view(by_kind["due"][0]) if by_kind.get("due") else None}
    if claims:
        at["claim"] = claims[0]["at"]
        data["claim"] = {"transition": "PENDING → LEASED", **claims[0]["detail"]}
    if by_kind.get("rate_token") or by_kind.get("rate_limited"):
        e = (by_kind.get("rate_token") or by_kind["rate_limited"])[0]
        at["token"] = e["at"]
        bucket = [dict(b) for b in RateLimiter(conn).snapshot() if b["name"] == "ledger-api"]
        data["token"] = {"event": e["kind"], **e["detail"], "bucket_now": bucket[0] if bucket else None}
    if len(hbs) >= 2:
        at["heartbeat"] = hbs[1]["at"]
        data["heartbeat"] = {"owner": first_owner, "heartbeats": len(hbs),
                             "last": [{"at": _iso(h["at"]), "lease_expires_at": h["detail"]["lease_expires_at"]}
                                      for h in hbs[-3:]]}
    if by_kind.get("worker_frozen"):
        e = by_kind["worker_frozen"][0]
        at["stall"] = e["at"]
        data["stall"] = {"worker": e["actor"], **e["detail"], "signal": "SIGSTOP"}
    if by_kind.get("lease_expired"):
        e = by_kind["lease_expired"][0]
        at["expire"] = e["at"]
        data["expire"] = {"transition": f"{e['from_state']} → {e['to_state']}", "reaped_by": e["actor"],
                          **e["detail"]}
    if second:
        at["reclaim"] = second["at"]
        data["reclaim"] = {"transition": "PENDING → LEASED", "previous_owner": first_owner, **second["detail"]}
    if stale:
        at["stale"] = stale[0]["at"]
        main = next((e for e in stale if e["detail"]["op"] == "finish"), stale[0])
        data["stale"] = {"worker": first_owner, **main["detail"], "verdict": "rejected",
                         "all_rejected": [{"at": _iso(e["at"]), "op": e["detail"]["op"],
                                           "my_token": e["detail"]["my_token"],
                                           "current_token": e["detail"]["current_token"]} for e in stale]}
    if done:
        at["done"] = done[0]["at"]
        data["done"] = {"transition": "RUNNING → SUCCEEDED", "by": done[0]["actor"], "result": job["result"],
                        "attempts": [{"attempt": a["attempt_no"], "worker": a["worker_id"],
                                      "fencing_token": a["fencing_token"], "outcome": a["outcome"]}
                                     for a in store.attempts(conn, jid)],
                        "cache_put": puts[0] if puts else None}
    if hits:
        at["cache"] = True
        data["cache"] = {"x_cache": "HIT", "stats": cache.stats(), "keys_mru": cache.keys_mru()[:6]}

    stages, active_given = [], False
    for sid, label, part, anchor in STAGES:
        status = "done" if sid in data else ("active" if not active_given else "pending")
        if status == "active":
            active_given = True
        stages.append({"id": sid, "label": label, "part": part, "anchor": anchor, "status": status,
                       "at": _iso(at.get(sid)) if at.get(sid) is not True else None, "data": data.get(sid)})
    return {"run": run["id"], "job_id": jid, "state": job["state"], "stages": stages,
            "finished": "cache" in data}


def router(db, owner, cache) -> APIRouter:
    r = APIRouter(prefix="/learn")

    @r.post("/runs", status_code=201)
    def start(who: str = Depends(owner), conn=Depends(db)):
        with conn.transaction():
            n = conn.execute("SELECT coalesce(max(id), 0) + 1 AS n FROM learn_runs").fetchone()["n"]
            job = conn.execute(
                """INSERT INTO jobs (owner, type, payload, state, run_at, max_attempts, timeout_s)
                   VALUES (%s, 'call_integration', %s, 'SCHEDULED', now() + make_interval(secs => %s), 3, 60)
                   RETURNING *""", (who, Jsonb(sample_payload(n)), DELAY_S)).fetchone()
            store.add_event(conn, job["id"], "submit", f"api:{who}", to="SCHEDULED",
                            detail={"type": job["type"], "learn_run": n, "delay_s": DELAY_S})
            run = conn.execute("INSERT INTO learn_runs (job_id, owner) VALUES (%s, %s) RETURNING *",
                               (job["id"], who)).fetchone()
        return build_stages(conn, run, cache)

    @r.get("/runs/{run_id}")
    def view(run_id: int, who: str = Depends(owner), conn=Depends(db)):
        run = conn.execute("SELECT * FROM learn_runs WHERE id = %s AND owner = %s", (run_id, who)).fetchone()
        if run is None:
            raise HTTPException(404, f"learn run {run_id} not found")
        return build_stages(conn, run, cache)

    @r.get("/runs")
    def latest(who: str = Depends(owner), conn=Depends(db)):
        return conn.execute("SELECT * FROM learn_runs WHERE owner = %s ORDER BY id DESC LIMIT 5", (who,)).fetchall()

    return r
