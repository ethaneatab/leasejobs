"""REST API: submit, query, cancel and inspect jobs. `python -m leasejobs.api`."""
from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from . import jobs as jobtypes
from . import store
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
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO jobs (owner, type, payload, state, priority, max_attempts, timeout_s, idempotency_key) "
                "VALUES (%s,%s,%s,'PENDING',%s,%s,%s,%s) ON CONFLICT (owner, idempotency_key) DO NOTHING RETURNING *",
                (who, body.type, Jsonb(payload), body.priority, body.max_attempts, body.timeout_s,
                 body.idempotency_key),
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
