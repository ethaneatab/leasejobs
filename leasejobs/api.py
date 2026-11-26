"""REST API: submit, query, cancel and inspect jobs. `python -m leasejobs.api`."""
from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import jobs as jobtypes
from . import store
from .cron import Cron
from .schedules import step_fn
from .config import settings
from .db import Jsonb
from .logs import JsonLog
from .states import State, TERMINAL

log = JsonLog("api")


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(0, ge=-100, le=100)
    max_attempts: int = Field(3, ge=1, le=20)
    timeout_s: float = Field(30, gt=0, le=3600)
    idempotency_key: str | None = Field(None, min_length=1, max_length=200)
    delay_s: float | None = Field(None, ge=0, le=30 * 86400, description="run no earlier than now + delay_s")
    run_at: datetime | None = Field(None, description="run no earlier than this time (with timezone)")

    @model_validator(mode="after")
    def _one_time(self):
        if self.delay_s is not None and self.run_at is not None:
            raise ValueError("give delay_s or run_at, not both")
        if self.run_at is not None and self.run_at.tzinfo is None:
            raise ValueError("run_at needs a timezone, e.g. 2026-01-01T09:00:00Z")
        return self


class ScheduleJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(3, ge=1, le=20)
    timeout_s: float = Field(30, gt=0, le=3600)


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    cron: str | None = None
    every_s: float | None = Field(None, ge=1, le=30 * 86400)
    job: ScheduleJob
    misfire: Literal["skip", "catch_up"] = "skip"
    max_catchup: int = Field(10, ge=1, le=1000)

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.cron is None) == (self.every_s is None):
            raise ValueError("give exactly one of cron or every_s")
        if self.cron is not None:
            Cron.parse(self.cron)  # ValueError -> 422 with the parser's message
        return self


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_app(dsn: str | None = None) -> FastAPI:
    pool = ConnectionPool(dsn or settings().dsn, min_size=1, max_size=8, open=False,
                          kwargs={"autocommit": True, "row_factory": dict_row})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool.open(wait=True, timeout=15)
        yield
        pool.close()

    app = FastAPI(title="leasejobs", version="0.1.0", lifespan=lifespan)
    app.state.pool = pool

    def db():
        with pool.connection() as conn:
            yield conn

    def owner(x_api_key: str | None = Header(default=None), conn=Depends(db)) -> str:
        if not x_api_key:
            raise HTTPException(401, "missing X-API-Key header")
        row = conn.execute("SELECT owner FROM api_keys WHERE key_hash = %s", (hash_key(x_api_key),)).fetchone()
        if row is None:
            raise HTTPException(401, "unknown API key")
        return row["owner"]

    @app.exception_handler(ValueError)
    async def bad_value(request: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz():
        return {"status": "alive"}

    @app.get("/readyz")
    def readyz():
        try:
            with pool.connection(timeout=2) as conn:
                n = conn.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()["n"]
        except Exception as exc:  # readiness is about dependencies, so report, don't crash
            return JSONResponse(status_code=503, content={"status": "not ready", "error": str(exc)})
        return {"status": "ready", "migrations": n}

    @app.post("/jobs", status_code=201)
    def submit(body: JobIn, who: str = Depends(owner), conn=Depends(db)):
        if body.type not in jobtypes.Job.registry:
            raise HTTPException(422, f"unknown job type {body.type!r}; known: {jobtypes.known_types()}")
        payload = jobtypes.Job.registry[body.type].validate(body.payload)  # ValueError -> 422
        if body.idempotency_key:
            old = conn.execute("SELECT * FROM jobs WHERE owner = %s AND idempotency_key = %s",
                               (who, body.idempotency_key)).fetchone()
            if old is not None:
                return JSONResponse(status_code=200, content=_jsonable(old))
        # Delayed jobs start in SCHEDULED; a trigger NOTIFYs the scheduler on commit.
        if body.delay_s:
            when, state = "now() + make_interval(secs => %s)", "SCHEDULED"
            when_arg: Any = body.delay_s
        elif body.run_at is not None:
            when, when_arg = "greatest(now(), %s::timestamptz)", body.run_at
            state = "SCHEDULED" if body.run_at > datetime.now(timezone.utc) else "PENDING"
        else:
            when, when_arg, state = "now() + make_interval(secs => %s)", 0, "PENDING"
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO jobs (owner, type, payload, state, priority, max_attempts, timeout_s, idempotency_key, run_at) "
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,{when}) ON CONFLICT (owner, idempotency_key) DO NOTHING RETURNING *",
                (who, body.type, Jsonb(payload), state, body.priority, body.max_attempts, body.timeout_s,
                 body.idempotency_key, when_arg),
            ).fetchone()
            if row is None:  # lost an idempotency race to a concurrent identical submit
                row = conn.execute("SELECT * FROM jobs WHERE owner = %s AND idempotency_key = %s",
                                   (who, body.idempotency_key)).fetchone()
                return JSONResponse(status_code=200, content=_jsonable(row))
            store.add_event(conn, row["id"], "submit", f"api:{who}", to=row["state"],
                            detail={"type": body.type, "payload": payload})
        log.info("submitted", job_id=row["id"], type=body.type, owner=who)
        return row

    @app.get("/jobs")
    def list_jobs(state: State | None = None, limit: int = Query(50, ge=1, le=500),
                  who: str = Depends(owner), conn=Depends(db)):
        return store.list_jobs(conn, who, state.value if state else None, limit)

    def _own(job_id: int, who: str, conn) -> dict:
        row = store.get_job(conn, job_id, who)
        if row is None:  # someone else's job looks exactly like a missing one
            raise HTTPException(404, f"job {job_id} not found")
        return row

    @app.get("/jobs/{job_id}")
    def get_job(job_id: int, who: str = Depends(owner), conn=Depends(db)):
        return _own(job_id, who, conn)

    @app.get("/jobs/{job_id}/attempts")
    def get_attempts(job_id: int, who: str = Depends(owner), conn=Depends(db)):
        _own(job_id, who, conn)
        return store.attempts(conn, job_id)

    @app.get("/jobs/{job_id}/events")
    def get_events(job_id: int, who: str = Depends(owner), conn=Depends(db)):
        _own(job_id, who, conn)
        return store.events(conn, job_id)

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: int, who: str = Depends(owner), conn=Depends(db)):
        job = _own(job_id, who, conn)
        st = State(job["state"])
        if st in TERMINAL:
            raise HTTPException(409, f"job {job_id} is already {st.value}")
        if st in (State.SCHEDULED, State.PENDING):
            row = store.transition(conn, job_id, st, State.CANCELLED, actor=f"api:{who}", kind="cancel",
                                   set_={"finished_at": store.Raw("now()")})
            if row is not None:
                return row
        # Held by a worker: ask it to stop; it notices on its next heartbeat.
        conn.execute("UPDATE jobs SET cancel_requested = true WHERE id = %s", (job_id,))
        store.add_event(conn, job_id, "cancel_requested", f"api:{who}")
        return JSONResponse(status_code=202, content=_jsonable(store.get_job(conn, job_id)))

    @app.post("/schedules", status_code=201)
    def create_schedule(body: ScheduleIn, who: str = Depends(owner), conn=Depends(db)):
        if body.job.type not in jobtypes.Job.registry:
            raise HTTPException(422, f"unknown job type {body.job.type!r}")
        payload = jobtypes.Job.registry[body.job.type].validate(body.job.payload)
        first = step_fn(body.cron, body.every_s)(conn.execute("SELECT now() AS t").fetchone()["t"])
        row = conn.execute(
            """INSERT INTO schedules (owner, name, cron, every_s, job_type, payload, max_attempts, timeout_s,
                   misfire, max_catchup, next_run_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (owner, name) DO NOTHING RETURNING *""",
            (who, body.name, body.cron, body.every_s, body.job.type, Jsonb(payload), body.job.max_attempts,
             body.job.timeout_s, body.misfire, body.max_catchup, first)).fetchone()
        if row is None:
            raise HTTPException(409, f"schedule {body.name!r} already exists")
        return row

    @app.get("/schedules")
    def list_schedules(who: str = Depends(owner), conn=Depends(db)):
        return conn.execute("SELECT * FROM schedules WHERE owner = %s ORDER BY id", (who,)).fetchall()

    @app.delete("/schedules/{schedule_id}")
    def disable_schedule(schedule_id: int, who: str = Depends(owner), conn=Depends(db)):
        row = conn.execute("UPDATE schedules SET enabled = false WHERE id = %s AND owner = %s RETURNING *",
                           (schedule_id, who)).fetchone()
        if row is None:
            raise HTTPException(404, f"schedule {schedule_id} not found")
        return row

    @app.get("/scheduler")
    def scheduler_state(_: str = Depends(owner), conn=Depends(db)):
        row = conn.execute("SELECT *, extract(epoch FROM now() - updated_at) AS age_s FROM scheduler_state"
                           ).fetchone()
        return row or {"leader": None, "heap_size": 0, "top": [], "age_s": None}

    return app


def _jsonable(row: dict) -> dict:
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(row)


def main() -> None:
    import uvicorn
    port = int(os.environ.get("LEASEJOBS_API_PORT", "8910"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
