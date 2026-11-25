"""LeaseManager: how a worker takes a job, starts it, and finishes it.

A lease is (lease_owner, lease_expires_at) on the job row, set by the
database clock so workers never have to agree on wall time.
"""
from __future__ import annotations

from typing import Any

import psycopg

from . import store
from .config import sabotage
from .db import Jsonb
from .states import State, check


class LeaseManager:
    def __init__(self, conn: psycopg.Connection, worker_id: str, ttl_s: float = 4.0):
        self.conn = conn
        self.worker_id = worker_id
        self.ttl_s = ttl_s

    # @learn:claim
    def claim(self) -> dict | None:
        """Take the best runnable job in one statement, skipping rows other workers hold.

        The inner SELECT ... FOR UPDATE SKIP LOCKED is the whole trick: a row
        another worker is in the middle of claiming is invisible to us instead of
        making us wait for it, so N workers spread over N different rows.
        """
        check(State.PENDING, State.LEASED)
        lock = "FOR UPDATE" if "no_skip_locked" in sabotage() else "FOR UPDATE SKIP LOCKED"
        with self.conn.transaction():
            row = self.conn.execute(
                f"""
                UPDATE jobs SET state = 'LEASED', lease_owner = %(me)s, leased_at = now(),
                       lease_expires_at = now() + make_interval(secs => %(ttl)s)
                WHERE id = (SELECT id FROM jobs
                            WHERE state = 'PENDING' AND run_at <= now()
                            ORDER BY priority DESC, run_at, id
                            LIMIT 1 {lock})
                RETURNING *""",
                {"me": self.worker_id, "ttl": self.ttl_s},
            ).fetchone()
            if row is not None:
                store.add_event(self.conn, row["id"], "claim", self.worker_id, frm="PENDING", to="LEASED",
                                detail={"lease_owner": self.worker_id,
                                        "lease_expires_at": row["lease_expires_at"].isoformat()})
        return row
    # @learn:end claim

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
                  "finished_at": store.Raw("now()"), "lease_owner": None,
                  "lease_expires_at": None},
            guard=self._mine(), detail={"outcome": outcome, **({"error": error} if error else {})},
        )
        if row is not None:
            self.conn.execute(
                "UPDATE job_attempts SET finished_at = now(), outcome = %s, error = %s "
                "WHERE job_id = %s AND attempt_no = %s",
                (outcome, error, job["id"], job["attempts"]),
            )
        return row
