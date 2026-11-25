"""LeaseManager: how a worker takes a job, starts it, and finishes it.

Step 1 version: a compare-and-set claim. A worker reads a candidate and then
flips it PENDING -> LEASED only if it is still PENDING, so two workers can
never both win the same row; the loser just tries the next one.
"""
from __future__ import annotations

from typing import Any

import psycopg

from . import store
from .db import Jsonb
from .states import State


class LeaseManager:
    def __init__(self, conn: psycopg.Connection, worker_id: str):
        self.conn = conn
        self.worker_id = worker_id

    def claim(self) -> dict | None:
        for cand in self.conn.execute(
            "SELECT id FROM jobs WHERE state = 'PENDING' AND run_at <= now() "
            "ORDER BY priority DESC, run_at, id LIMIT 5"
        ).fetchall():
            row = store.transition(self.conn, cand["id"], State.PENDING, State.LEASED, actor=self.worker_id,
                                   kind="claim", set_={"lease_owner": self.worker_id})
            if row is not None:
                return row
        return None

    def _mine(self) -> tuple[str, tuple]:
        return "lease_owner = %s", (self.worker_id,)

    def start(self, job: dict) -> dict | None:
        row = store.transition(self.conn, job["id"], State.LEASED, State.RUNNING, actor=self.worker_id,
                               kind="start", set_={"attempts": store.Raw("attempts + 1")}, guard=self._mine())
        if row is not None:
            self.conn.execute(
                "INSERT INTO job_attempts (job_id, attempt_no, worker_id) VALUES (%s, %s, %s)",
                (row["id"], row["attempts"], self.worker_id),
            )
        return row

    def finish(self, job: dict, to: State, *, result: dict[str, Any] | None = None,
               error: str | None = None, outcome: str) -> dict | None:
        row = store.transition(
            self.conn, job["id"], State.RUNNING, to, actor=self.worker_id, kind="finish",
            set_={"result": Jsonb(result) if result is not None else None, "last_error": error,
                  "finished_at": store.Raw("now()"), "lease_owner": None},
            guard=self._mine(), detail={"outcome": outcome, **({"error": error} if error else {})},
        )
        if row is not None:
            self.conn.execute(
                "UPDATE job_attempts SET finished_at = now(), outcome = %s, error = %s "
                "WHERE job_id = %s AND attempt_no = %s",
                (outcome, error, job["id"], job["attempts"]),
            )
        return row
